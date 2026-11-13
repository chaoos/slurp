"""The job list"""

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

from ..state import *
from ..remote import *
from ..jobs import *
from ..widgets import *
from ..screens.base import *

COLUMNS = [("jobid", "Job ID"), ("name", "Name"), ("user", "User"), ("state", "State"), ("progress", "Progress"), ("eta", "Done in"),
           ("nodes", "Nodes"), ("time", "Elapsed / Limit"),
           ("start", "Start"), ("end", "End"), ("err", "Err"), ("cmd", "Command")]

SINCE_CHOICES = [("only queued", "off"), ("1 hour", "1h"), ("6 hours", "6h"), ("12 hours", "12h"), ("1 day", "1d"),
                 ("2 days", "2d"), ("1 week", "1w"), ("2 weeks", "2w"), ("1 month", "1mo"), ("custom …", "custom")]

SORT_KEYS = {
  "arrayid": lambda j: (int(t) if (t := array_task_id(j)) is not None else -1, job_id_key(j.jid)), # array index first
  "jobid": lambda j: job_id_key(j.jid),
  "name":  lambda j: j.rec["JobName"],
  "user":  lambda j: j.rec["User"].casefold(),
  "state": lambda j: j.state,
  "progress": lambda j: (p.current / p.total if (p := progress_for_job(j)) and p.total else 0),
  "eta": lambda j: best_eta(j.progress) or float("inf"),
  "nodes": lambda j: int(j.rec["NNodes"] or 0),
  "time":  lambda j: j.node_hours() / max(int(j.rec["NNodes"] or 1), 1),
  "start": lambda j: j.rec["Start"] if is_time(j.rec["Start"]) else "",
  "end":   lambda j: j.rec["End"] if is_time(j.rec["End"]) else "",
  "err":   lambda j: (j.err[0] == "file", j.err[1]),
  "cmd":   lambda j: j.command,
}

class MainScreen(SplitScreen):
  """The job table"""
  SHOW_NOTABS = True
  BINDINGS = [
    Binding("slash", "filter", "Filter jobs"),
    Binding("escape", "clear_filter", "Clear filter", show=False),
    Binding("space", "app.toggle_group", "Open/close array", show=False),
  ]

  def compose_left(self):
    with Horizontal(id="toolbar"):
      yield Button("Reload", id="reload", variant="primary")
      yield Button("Shell", id="cluster-shell", tooltip="shell on the cluster of this tab, in the pane on the right (t)")
      yield Button("Interactive job", id="interactive-job", tooltip="submit a job that holds nodes for shells (i)")
      yield Button("Cluster info", id="cluster-info", tooltip="health and occupancy of the cluster (C)")
      yield Button("Quota", id="quota", tooltip="your quotas on the filesystems (Q)")
      yield Button("Scratch", id="scratch", tooltip="your oldest files in $SCRATCH, which a cleanup may delete (X)")
      yield Label("finished since")
      yield Select(SINCE_CHOICES, value="1d", allow_blank=False, id="since-choice",
                   tooltip="also list the jobs that finished within this time")
      yield Input("1d", id="since", placeholder="e.g. 3d12h", tooltip="s, m, h, d, w, mo, y, combined (1d12h); Enter: load")
      yield Select([("my jobs", "me"), ("all my accounts", "all")], value="me", allow_blank=False, id="scope",
                   tooltip="whose jobs are listed: yours, or those of one or all of your Slurm accounts")
    yield Static(id="status")
    yield ClickTable(id="jobs", cursor_type="row", zebra_stripes=True)
    yield Input(id="jfilter", placeholder="filter jobs by name, command, ID, state, user, account, path…")

  def action_filter(self):
    search = self.query_one("#jfilter", Input)
    search.add_class("visible")
    search.value = self.app.job_filter
    search.focus()

  def action_clear_filter(self):
    search = self.query_one("#jfilter", Input)
    if search.has_class("visible"):
      search.value = ""
      search.remove_class("visible")
      self.app.job_filter = ""
      self.app.populate()
      self.query_one("#jobs", ClickTable).focus()

  def on_mount(self):
    self.app.main = self
    # the job list is the default mode; Textual does not register that one, so it could not be switched back to
    self.app._modes.setdefault(self.app.DEFAULT_MODE, lambda: self)
    self.app.setup_job_columns()
    table = self.query_one("#jobs", ClickTable)
    table.focus()
    if self.app.clusters:
      self.app.activate_cluster(self.app.cluster_index)
    else:
      self.set_class(True, "-no-tabs")
