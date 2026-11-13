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
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, MarkdownViewer, OptionList, ProgressBar, RadioButton, RadioSet, Select, Static, Switch, TextArea, Tree
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual.suggester import SuggestFromList
from textual.worker import get_current_worker

import slurm as sl



log = logging.getLogger("slurp")

TAIL_BYTES = 4*1024*1024 # files larger than this are shown from their end
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
  loader: object | None = None         # worker loading the job list
  accounts: list | None = None         # Slurm accounts of the user (sacctmgr), None: not asked yet
  scope: str = "me"                    # whose jobs are listed: "me", "all" (all accounts) or an account
  views: list = field(default_factory=list) # the subtabs besides the job list
  current_view: str = "_default"       # mode of the subtab shown ("_default": the job list)
  load_started: float = 0.0
  updated: object | None = None        # when the list was loaded
  show_login: bool = False             # the login is shown in the pane (until it is done)
  attempts: int = 0                    # logins since the session was last ready
  ready_generation: int | None = None  # session generation the app has seen ready
  broken_at: float | None = None       # when the session broke (or the last reconnect started)

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
  for name in ("slurp", "utils"): # "utils" is the logger of slurm.py / helpers.py
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.DEBUG if logfile else logging.WARNING)
    lg.addHandler(collector)
    if logfile:
      fh = logging.FileHandler(logfile)
      fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(funcName)s] %(message)s"))
      lg.addHandler(fh)
