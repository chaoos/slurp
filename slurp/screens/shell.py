"""A shell in a subtab"""

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
from ..widgets import *
from ..terminal import *
from ..screens.base import *

class TerminalScreen(SplitScreen):
  """A shell in a subtab of its own"""
  def __init__(self, session):
    super().__init__()
    self.session = session

  def compose_left(self):
    yield Static(id="shell-status")
    yield TerminalView(session=self.session)

  def on_mount(self):
    self.sub_title = self.session.title
    self.watch(self.app, "shell_version", self.update_status)
    self.query_one(TerminalView).focus()

  def on_screen_resume(self):
    self.query_one(TerminalView).focus()
    self.call_after_refresh(self.query_one(TerminalView).attach)

  def update_status(self, _=None):
    s = self.session
    text = f"{s.title} · ended (exit code {s.code}) · Enter: start it again · ✕ of the subtab: close" if s.done else \
           f"{s.title} · all keys go to the shell, Ctrl-] goes to the job list · ✕ of the subtab closes it"
    self.query_one("#shell-status", Static).update(Text(text, style="bold" if not s.done else "yellow"))

  def restart(self):
    old = self.session
    self.session = TerminalSession(self.app, old.args, old.title, via=old.via)
    for view in self.app.cluster.views:
      if view.session is old:
        view.session = self.session
    view = self.query_one(TerminalView)
    view.own_session = self.session
    self.call_after_refresh(view.attach)
    self.update_status()
