"""Logging, settings shared by the modules, and the data of the tabs and subtabs"""

import argparse
import asyncio
import codecs
import collections
import fcntl
import functools
import json
import logging
import os
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import humanize
from rich.color import Color
from rich.progress_bar import ProgressBar as RichProgressBar
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, MarkdownViewer, OptionList, ProgressBar, RadioButton, RadioSet, Select, Static, Switch, TextArea, Tree, Checkbox, Sparkline, Markdown
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual.suggester import SuggestFromList
from textual.worker import get_current_worker

from . import slurm as sl



log = logging.getLogger("slurp")

TAIL_BYTES = 1024*1024 # files larger than this are shown from their end (more on request, in parts this size)
PROGRESS_TAIL_BYTES = 64*1024
PROGRESS_TAIL_LINES = 30
PROGRESS_PATTERN = re.compile(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)")
STEP_NAME = "slurp"  # job name of the steps that run commands on the nodes
OLD_STEP_NAMES = (STEP_NAME, "slurm_tui") # also of earlier versions
PRESETS = {
  "nvidia-smi": "nvidia-smi",
  "GPU csv":    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv",
  "free":       "free -h",
  "top":        "top -bn1 | head -30",
  "uptime":     "uptime",
  "ps":         "ps -u $USER -o pid,pcpu,rss,etime,cmd",
}

# ---------------------------------------------------------------------------
# logging: never write to the terminal, collect warnings to show them in the UI

class Collector(logging.Handler):
  def __init__(self):
    super().__init__(logging.WARNING)
    self.messages = []
  def emit(self, record):
    from .remote import tab_name # (remote builds on this module)
    self.messages.append((tab_name(), record.getMessage())) # the tab of the thread that logs it
  def drain(self):
    msgs, self.messages = self.messages, []
    return msgs

collector = Collector()
initial_ssh_destinations = []

@dataclass
class ClusterSession:
  """A tab: the local machine or an SSH destination, with its own connection, shell and job list"""
  name: str
  destination: str | None = None
  connection: object | None = None
  terminal: object | None = None       # shell in the pane on the right while this tab is active
  login_terminal: object | None = None # the ssh login (control master) of the connection
  jobs: dict = field(default_factory=dict)
  since: object | None = None          # finished-since time of the loaded list
  summary: str = ""                    # status line of the loaded list
  loaded: bool = False
  listed: bool = False                 # the list of the current load is shown, the files of its jobs are being read
  drive: object | None = None          # mounts.LocalDrive: the login directory as a drive on this machine
  loader: object | None = None         # worker loading the job list
  accounts: list | None = None         # Slurm accounts of the user (sacctmgr), None: not asked yet
  since_value: str = "1d"              # finished jobs listed: within this time (e.g. 1d, 3d12h, 2w, 1mo), "off": none
  collapse_arrays: bool = True        # big arrays as one row in the job list (else every task)
  expanded: set = field(default_factory=set) # collapsed array rows that are open ("array id|state")
  scope: str = "me"                    # whose jobs are listed: "me", "all" (all accounts) or an account
  views: list = field(default_factory=list) # the subtabs besides the job list
  current_view: str = "_default"       # mode of the subtab shown ("_default": the job list)
  load_started: float = 0.0
  updated: object | None = None        # when the list was loaded
  show_login: bool = False             # the login is shown in the pane (until it is done)
  attempts: int = 0                    # logins since the session was last ready
  ready_generation: int | None = None  # session generation the app has seen ready
  broken_at: float | None = None       # when the session broke (or the last reconnect started)
  categories: object | None = None     # categories.Store of its Slurm cluster (None: not read yet)
  slurm_cluster: str | None = None     # Slurm's name of the cluster (ClusterName; the categories are kept by it)
  marked: set = field(default_factory=set) # rows of the job list marked (m): job ids, "group|<array>" for an array
  from_cache: bool = False             # the job list is the one kept from an earlier load (jobcache.py), not loaded now
  pending_categories: list = field(default_factory=list) # changes to the categories made while the cluster could not
                                                         # be reached: [op, args...], written to it with the next load

@dataclass
class View:
  """A subtab of a tab: a job window, a file or directory, a shell. It is a Textual mode, with its own window stack."""
  id: int
  title: str
  kind: str                   # "job", "window" or "shell"
  key: str = ""               # identifies what it shows (e.g. the job), to show it again instead of opening it twice
  session: object | None = None # TerminalSession of a shell

  @property
  def mode(self):
    return f"view-{self.id}"

def is_local_destination(destination):
  return destination.lower() in {"localhost", "127.0.0.1", "::1"}

def setup_logging(logfile):
  for name in ("slurp", "asyncssh"): # asyncssh: the logger of links
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel((logging.INFO if name == "asyncssh" else logging.DEBUG) if logfile else logging.WARNING)
    lg.addHandler(collector)
    if logfile:
      fh = logging.FileHandler(logfile)
      fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(funcName)s] %(message)s"))
      lg.addHandler(fh)
