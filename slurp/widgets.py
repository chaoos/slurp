"""Small widgets and helpers shared by the windows"""

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

from .state import *
from .remote import *
from .jobs import *
from .details import *

# ---------------------------------------------------------------------------
# widgets and screens

STATE_STYLES = {"RUNNING": "green", "COMPLETED": "green", "COMPLETING": "green", "CONFIGURING": "green",
                "PENDING": "blue", "REQUEUED": "blue", "SUSPENDED": "yellow"}

def state_text(state):
  return Text(state, style=STATE_STYLES.get(state.split(" ")[0], "red"))

def err_text(err):
  kind, size = err
  if kind == "file":
    return Text(f"! {humanize.naturalsize(size)}", style="bold red") if size else Text("")
  if kind == "merged":
    return Text("(stdout)", style="dim")
  if kind == "missing":
    return Text("?", style="yellow")
  return Text("")

class ClickTable(DataTable):
  """Row table where a single click selects (opens) a row and remembers the clicked column"""
  clicked_column = None

  async def _on_click(self, event):
    meta = event.style.meta
    row, col = meta.get("row"), meta.get("column")
    if row is None or col is None or row < 0 or col < 0 or self.cursor_type != "row":
      return # header clicks etc.: Textual calls DataTable._on_click next (handlers run along the MRO)
    event.prevent_default() # but not for clicks on rows, they are handled here
    event.stop()
    self.clicked_column = self.ordered_columns[col].key.value
    self.move_cursor(row=row)
    self.post_message(self.RowSelected(self, row, self.coordinate_to_cell_key(Coordinate(row, col)).row_key))

  def take_clicked_column(self):
    col, self.clicked_column = self.clicked_column, None
    return col

def clean(text):
  """Remove terminal escape sequences and lines overwritten with carriage returns"""
  text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07', '', text)
  return re.sub(r'[^\n]*\r(?!\n)', '', text.replace("\r\n", "\n"))

def crumb_title(crumbs):
  """Breadcrumbs for the header, shortened in the middle when they get long"""
  crumbs = list(crumbs)
  if len(crumbs) > 5:
    crumbs = crumbs[:2] + ["…"] + crumbs[-2:]
  return " › ".join(crumbs)

class PathArea(TextArea):
  """Read-only text where a click on the name of an existing file or directory opens it"""
  class PathClicked(Message):
    """A word was clicked; the screen looks it up (in the background) and opens it if it names a file or directory"""
    def __init__(self, word):
      super().__init__()
      self.word = word

  def word_at_cursor(self):
    if self.selected_text.strip():
      return self.selected_text.strip()
    row, col = self.cursor_location
    return self.word_at(row, col)

  def word_at(self, row, col):
    if row >= self.document.line_count:
      return None
    line = self.document.get_line(row)
    return word_at(line, col) or word_at(line, col - 1) # also just behind the end of a word

  def on_click(self, event):
    if event.chain > 1 or self.selected_text: # double clicks and dragging select text
      return
    row, col = self.get_target_document_location(event)
    word = self.word_at(row, col)
    if word:
      self.post_message(self.PathClicked(word))


def bash_editor(text, id, read_only=False):
  """Multi-line command editor with bash syntax highlighting"""
  editor = TextArea.code_editor(text, id=id, read_only=read_only, soft_wrap=True, show_line_numbers=False)
  if "bash" in editor.available_languages:
    editor.language = "bash"
  return editor

def command_args(text):
  """argv of a command typed in an editor: lines may end with a backslash, the lines are joined"""
  return shlex.split(text.replace("\\\n", " ").replace("\n", " "))

class ConnectionStatus(Static):
  """State of the SSH session in the footer; a click opens the connection window"""
  def on_click(self, event):
    event.stop()
    self.app.action_connection()
