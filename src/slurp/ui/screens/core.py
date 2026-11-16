"""Core dumps: what they are from, and GDB"""

import os
import shlex
import shutil
from datetime import datetime

import humanize
from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


from ...cluster.remote import cluster_work, conn, fs_isfile, fs_stat, run_cmd
from ...cluster.paths import is_core_dump
from ..widgets import crumb_title
from .base import SplitScreen
from .files import register_file_view

class GdbCommandScreen(ModalScreen[list[str] | None]):
  """Edit the command that will be run interactively in the terminal pane."""
  DEFAULT_CSS = """
  GdbCommandScreen { align: center middle; }
  #gdb-dialog { width: 100; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #gdb-title { text-style: bold; margin-bottom: 1; }
  #gdb-command { width: 1fr; }
  #gdb-help { height: auto; margin-top: 1; color: $text-muted; }
  #gdb-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #gdb-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, default_args):
    super().__init__()
    self.default_command = shlex.join(default_args)

  def compose(self) -> ComposeResult:
    with Vertical(id="gdb-dialog"):
      yield Static("Open core dump with GDB", id="gdb-title")
      yield Label("Command (editable)", id="gdb-command-label")
      yield Input(self.default_command, id="gdb-command")
      yield Static("Arguments are parsed as a command line; shell operators and expansion are not evaluated.", id="gdb-help")
      with Horizontal(id="gdb-buttons"):
        yield Button("Cancel", id="gdb-cancel", compact=True)
        yield Button("Run in terminal", id="gdb-run", variant="primary", compact=True)

  def on_mount(self):
    command = self.query_one("#gdb-command", Input)
    command.focus()
    command.cursor_position = len(command.value)

  @on(Button.Pressed, "#gdb-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  @on(Button.Pressed, "#gdb-run")
  @on(Input.Submitted, "#gdb-command")
  def run_pressed(self, event):
    event.stop()
    command = self.query_one("#gdb-command", Input).value
    try:
      args = shlex.split(command)
    except ValueError as e:
      self.notify(f"Invalid command: {e}", severity="error")
      return
    if not args:
      self.notify("Enter a command to run", severity="warning")
      return
    self.dismiss(args)

  def action_cancel(self):
    self.dismiss(None)


def inspect_core_file(path):
  """Read process notes from a core file through GDB without starting an interactive session."""
  gdb = conn().which("gdb") if conn() else shutil.which("gdb")
  if not gdb:
    return {}, "gdb was not found on PATH"
  try:
    args = [gdb, "--batch", "--nx", f"--core={path}", "-ex", "info proc"]
    result = run_cmd(args, timeout=20)
  except OSError as e:
    return {}, f"could not inspect core metadata: {e}"
  output = (result.stdout or "") + "\n" + (result.stderr or "")
  info = {}
  for line in output.splitlines():
    key, sep, value = line.partition("=")
    key = key.strip()
    if sep and key in ("exe", "cwd", "cmdline"):
      value = value.strip()
      try:
        parsed = shlex.split(value)
        value = parsed[0] if len(parsed) == 1 else value
      except ValueError:
        value = value.strip("\"'")
      info[key] = value
  error = ""
  if not info:
    error = (result.stderr or result.stdout or "GDB could not read process details").strip()[:240]
  return info, error


class CoreScreen(SplitScreen):
  """Overview for an ELF core dump, with an editable GDB command launcher."""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back")]

  def __init__(self, crumbs, path, bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.bases, self.env = list(bases), env
    self.process_info = {}
    self.metadata_error = "Reading process details with GDB …"
    self.stat = None        # (size, mtime) or the error, read in the background
    self.exe_exists = False

  def compose_left(self) -> ComposeResult:
    with VerticalScroll(id="core-overview"):
      yield Static(id="core-info")
      yield Button("Open with GDB", id="open-gdb", variant="primary", compact=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.show_info()
    self.inspect_metadata()

  def show_info(self):
    if isinstance(self.stat, tuple):
      size = humanize.naturalsize(self.stat[0])
      modified = datetime.fromtimestamp(self.stat[1]).strftime("%Y-%m-%d %H:%M:%S")
    else:
      size, modified = ("…", "…") if self.stat is None else ("unavailable", self.stat)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Type", "ELF core dump")
    grid.add_row("File", self.path)
    grid.add_row("Size", size)
    grid.add_row("Modified", modified)
    for key, label in (("exe", "Executable"), ("cmdline", "Command line"), ("cwd", "Working directory")):
      if self.process_info.get(key):
        grid.add_row(label, self.process_info[key])
    if self.metadata_error:
      grid.add_row("Process details", self.metadata_error)
    self.query_one("#core-info", Static).update(grid)

  @cluster_work("core-metadata")
  def inspect_metadata(self):
    try:
      st = fs_stat(self.path)
      stat = (st.st_size, st.st_mtime)
    except OSError as e:
      stat = str(e)
    info, error = inspect_core_file(self.path)
    exe_exists = bool(info.get("exe")) and fs_isfile(info["exe"])
    self.app.call_from_thread(self.metadata_loaded, info, error, stat, exe_exists)

  def metadata_loaded(self, info, error, stat, exe_exists):
    if not self.is_mounted:
      return
    self.process_info, self.metadata_error, self.stat, self.exe_exists = info, error, stat, exe_exists
    self.show_info()

  def gdb_args(self):
    executable = self.process_info.get("exe")
    if executable and self.exe_exists:
      return ["gdb", executable, self.path]
    return ["gdb", f"--core={self.path}"]

  @on(Button.Pressed, "#open-gdb")
  def open_gdb(self, event):
    event.stop()
    title = f"GDB · {os.path.basename(self.path)}"
    self.app.push_screen(GdbCommandScreen(self.gdb_args()),
                         lambda args: self.app.open_shell(args, title, focus=True) if args is not None else None)

  def action_back(self):
    self.pop_back()


def open_core_view(app, crumbs, path, bases=(), env=None):
  """(runs in a worker)"""
  return lambda: app.open_window(CoreScreen(crumbs, path, bases=bases, env=env))
register_file_view("ELF core dump", is_core_dump, open_core_view)
