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

from . import slurm as sl

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

class Spinning:
  """A cell whose value is still coming: a spinner that turns when the table is redrawn"""
  def __rich_console__(self, console, options):
    yield Text("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 8) % 10], style="dim")
  def __rich_measure__(self, console, options):
    from rich.measure import Measurement
    return Measurement(1, 1)

SPINNING = Spinning()

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

def url_text(url, label=None):
  """A link in a text: underlined; a click on it opens it (open_clicked_url), a terminal that knows hyperlinks also
  opens it itself (Ctrl+click)"""
  from rich.style import Style
  return Text(label or url, style=Style(underline=True, link=url, meta={"open_url": url}))

def open_clicked_url(app, event):
  """A click on a url_text: open it (True), else False"""
  url = (event.style.meta or {}).get("open_url") if event.style is not None else None
  if not url:
    return False
  app.open_page(url)
  return True

class ConnectionStatus(Static):
  """State of the SSH session in the footer; a click opens the connection window (on a link in it: the link)"""
  def on_click(self, event):
    event.stop()
    if not open_clicked_url(self.app, event):
      self.app.action_connection()

class TimePlot(Widget):
  """Values over time as bars from zero (the last value at each moment), with the largest value on the y axis and
  the first and last time on the x axis. data: [(epoch seconds, value or None)]"""
  DEFAULT_CSS = "TimePlot { height: 5; }"
  BLOCKS = " ▁▂▃▄▅▆▇█"
  LABEL = 9 # columns of the y axis

  def __init__(self, color="cyan", fmt=str, **kwargs):
    super().__init__(**kwargs)
    self.color, self.fmt, self.data = color, fmt, []

  def set_data(self, data):
    self.data = [(t, v) for t, v in data if v is not None]
    self.refresh()

  def render_line(self, y):
    width, height = self.size.width, self.size.height
    rows = max(1, height - 1) # the last line is the time axis
    plot = max(1, width - self.LABEL)
    base = self.rich_style
    data = self.data
    if y == rows: # time axis
      if not data:
        return Strip([Segment(" " * width, base)])
      left, right = datetime.fromtimestamp(data[0][0]).strftime("%H:%M"), datetime.fromtimestamp(data[-1][0]).strftime("%H:%M")
      axis = " " * self.LABEL + left + " " * max(1, plot - len(left) - len(right)) + right
      return Strip([Segment(axis[:width].ljust(width), base + Style(dim=True))])
    if len(data) < 2:
      text = ("" if y else "  needs two measurements for a plot" if data else "  no measurements yet")
      return Strip([Segment(text[:width].ljust(width), base + Style(dim=True))])
    top = max(v for _, v in data) or 1
    t0, t1 = data[0][0], data[-1][0]
    label = (self.fmt(top) if y == 0 else "0" if y == rows - 1 else "").rjust(self.LABEL - 1)[:self.LABEL - 1] + "│"
    cells, j = [], 0
    for x in range(plot):
      t = t0 + (t1 - t0) * x / max(plot - 1, 1)
      while j + 1 < len(data) and data[j + 1][0] <= t:
        j += 1
      eighths = data[j][1] / top * rows * 8 # height of the bar, in eighths of a line
      fill = int(min(8, max(0, eighths - (rows - 1 - y) * 8)))
      cells.append(self.BLOCKS[fill])
    return Strip([Segment(label, base + Style(dim=True)), Segment("".join(cells), base + Style(color=self.color))])
