"""The frame of every window: tabs, subtabs, the window, the login pane, the footer"""

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
from ..widgets import *
from ..terminal import *
from ..screens.dialogs import *

class SplitScreen(Screen):
  """Window with its content on the left and, while a shell is open, the terminal pane on the right"""
  SHOW_BACK = False
  SHOW_NOTABS = False # the hint shown when no tab is open (the job list)

  def compose(self) -> ComposeResult:
    with Horizontal(id="cluster-tabs"):
      for index, cluster in enumerate(self.app.clusters):
        variant = "primary" if index == self.app.cluster_index else "default"
        yield Button(cluster.name, id=f"cluster-tab-{index}", classes="cluster-tab", variant=variant, compact=True)
        yield Button("✕", id=f"cluster-close-{index}", classes="cluster-close", compact=True, tooltip="close the tab")
      yield Button("+", id="cluster-add", tooltip="Add a local or SSH cluster", compact=True)
    with Horizontal(id="view-tabs"):
      yield Button("Jobs", id="view-jobs", classes="view-tab", compact=True, tooltip="the job list")
    with Horizontal(id="topbar"):
      if self.SHOW_BACK:
        yield Button("←", id="back", compact=True, tooltip="Back")
      yield Header()
      yield Button("?", id="view-help", compact=True, tooltip="about this window: what it shows, what it can do, its keys (?)")
    if self.SHOW_NOTABS:
      yield Static("No tabs. The + button above adds a connection (blank for this machine, or an SSH destination).", id="notabs")
    with Horizontal(classes="split"):
      with Vertical(classes="left"):
        yield from self.compose_left()
      yield Splitter()
      yield TerminalPane()
    with Horizontal(id="bottombar"):
      yield Footer()
      yield Static(id="loadstatus")
      yield ConnectionStatus(id="connstatus")

  def on_mount(self):
    self.call_after_refresh(self.app.refresh_status)

  @on(Button.Pressed)
  def back_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "cluster-add":
      event.stop()
      self.app.push_screen(AddClusterScreen(), self.app.add_cluster)
      return
    if event.button.id and event.button.id.startswith("cluster-tab-"):
      event.stop()
      self.app.select_cluster(int(event.button.id.rsplit("-", 1)[1]))
      return
    if event.button.id and event.button.id.startswith("cluster-close-"):
      event.stop()
      self.app.confirm_close_cluster(int(event.button.id.rsplit("-", 1)[1]))
      return
    if event.button.id == "view-help":
      event.stop()
      self.app.action_view_help()
      return
    if event.button.id == "view-jobs":
      event.stop()
      self.app.show_view(None)
      return
    if event.button.id and event.button.id.startswith(("view-tab-", "view-close-")):
      event.stop()
      vid = int(event.button.id.rsplit("-", 1)[1])
      view = next((v for v in self.app.cluster.views if v.id == vid), None)
      if view is not None:
        (self.app.show_view if event.button.id.startswith("view-tab-") else self.app.close_view)(view)
      return
    if event.button.id != "back":
      return
    event.stop()
    self.action_back()

  def compose_left(self):
    return []

  def pop_back(self):
    if len(self.app.screen_stack) <= 1: # the first window of a subtab: back to the job list (the subtab stays)
      self.app.show_view(None)
      return
    self.app.pop_screen()
    # pop_screen completes after this key action; queue a full redraw behind it.
    self.app.call_later(self.app.refresh, layout=True)
