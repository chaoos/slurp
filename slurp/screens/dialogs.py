"""Dialogs: confirmations, commands to submit (interactive jobs, restarts, array ranges), new tabs"""

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
import base64
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

class ConfirmScreen(ModalScreen[bool]):
  """Confirmation dialog for an action that changes a Slurm job"""
  DEFAULT_CSS = """
  ConfirmScreen { align: center middle; }
  #confirm-dialog { width: 72; max-width: 90%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #confirm-title { text-style: bold; margin-bottom: 1; }
  #confirm-message { height: auto; margin-bottom: 1; }
  #confirm-buttons { height: auto; align-horizontal: right; }
  #confirm-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "No")]

  def __init__(self, title, message, confirm_label, variant="error"):
    super().__init__()
    self.dialog_title, self.dialog_message = title, message
    self.confirm_label, self.variant = confirm_label, variant

  def compose(self) -> ComposeResult:
    with Vertical(id="confirm-dialog"):
      yield Static(self.dialog_title, id="confirm-title")
      yield Static(self.dialog_message, id="confirm-message", markup=False)
      with Horizontal(id="confirm-buttons"):
        yield Button("No", id="confirm-no")
        yield Button(self.confirm_label, id="confirm-yes", variant=self.variant)

  @on(Button.Pressed, "#confirm-no")
  def no(self, event):
    event.stop()
    self.dismiss(False)

  @on(Button.Pressed, "#confirm-yes")
  def yes(self, event):
    event.stop()
    self.dismiss(True)

  def action_cancel(self):
    self.dismiss(False)

class CommandScreen(ModalScreen[list[str] | None]):
  """Confirm (and edit) a command before it is run: returns its argv, or None"""
  DEFAULT_CSS = """
  CommandScreen { align: center middle; }
  #command-dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #command-title { text-style: bold; margin-bottom: 1; }
  #command-text { height: auto; min-height: 3; max-height: 16; }
  #command-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #command-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)] # also while the editor has the focus

  def __init__(self, title, explanation, command, submit_label="Submit", first_word=None):
    super().__init__()
    self.dialog_title, self.explanation, self.command = title, explanation, command
    self.submit_label, self.first_word = submit_label, first_word

  def compose(self) -> ComposeResult:
    with Vertical(id="command-dialog"):
      yield Static(self.dialog_title, id="command-title")
      yield Static(self.explanation, markup=False)
      yield bash_editor(self.command, "command-text")
      with Horizontal(id="command-buttons"):
        yield Button("Cancel", id="command-cancel")
        yield Button(self.submit_label, id="command-submit", variant="primary")

  def on_mount(self):
    self.query_one("#command-text").focus()

  @on(Button.Pressed, "#command-submit")
  def submit(self, event):
    event.stop()
    try:
      args = command_args(self.query_one("#command-text", TextArea).text)
    except ValueError as e:
      self.notify(f"invalid command: {e}", severity="error")
      return
    if not args or (self.first_word and os.path.basename(args[0]) != self.first_word):
      self.notify(f"the command must start with {self.first_word}", severity="error")
      return
    self.dismiss(args)

  @on(Button.Pressed, "#command-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  def action_cancel(self):
    self.dismiss(None)

def array_range(start, stop, step):
  """sbatch --array expression of a range"""
  return f"{start}-{stop}" + (f":{step}" if step != 1 else "") if stop != start else str(start)

TIME_LIMITS = ["00:30:00", "01:00:00", "02:00:00", "04:00:00", "08:00:00", "12:00:00", "1-00:00:00"]

def interactive_job_command(name="interactive", nodes="1", time_limit="01:00:00", partition="", account="", gpus=""):
  """sbatch command of a job that only holds its nodes (sleep infinity), one option per line"""
  lines = [f"sbatch --job-name={shlex.quote(name or 'interactive')}", f"--nodes={nodes or 1} --ntasks-per-node=1",
           f"--time={time_limit or '01:00:00'}"]
  lines += [f"--partition={shlex.quote(partition)}"] if partition else []
  lines += [f"--account={shlex.quote(account)}"] if account else []
  lines += [f"--gpus-per-node={gpus}"] if gpus else []
  lines += ["--output=/dev/null", "--wrap='sleep infinity'"]
  return " \\\n  ".join(lines)

class InteractiveJobScreen(CommandScreen):
  """CommandScreen for an interactive job: fields for account (from a list or typed), partition, time, nodes and
  GPUs write the command; it stays editable"""
  DEFAULT_CSS = CommandScreen.DEFAULT_CSS + """
  .ij-row { height: auto; margin-bottom: 1; }
  .ij-row Label { padding: 1 1 0 0; width: auto; }
  #ij-account { width: 20; }
  #ij-account-list { width: 24; margin-right: 2; }
  #ij-partition { width: 26; margin-right: 2; }
  #ij-time { width: 16; margin-right: 2; }
  #ij-nodes, #ij-gpus { width: 8; margin-right: 2; }
  #ij-name { width: 22; }
  """

  def __init__(self, title, explanation, accounts, connection):
    super().__init__(title, explanation, interactive_job_command(), "Submit", "sbatch")
    self.accounts, self.connection = list(accounts or []), connection

  def compose(self) -> ComposeResult:
    with Vertical(id="command-dialog"):
      yield Static(self.dialog_title, id="command-title")
      yield Static(self.explanation, markup=False)
      with Horizontal(classes="ij-row"):
        yield Label("account")
        yield Input(id="ij-account", placeholder="default", suggester=SuggestFromList(self.accounts, case_sensitive=False))
        yield Select([(a, a) for a in self.accounts], prompt="pick", id="ij-account-list")
        yield Label("partition")
        yield Select([], prompt="default", id="ij-partition")
      with Horizontal(classes="ij-row"):
        yield Label("time")
        yield Input("01:00:00", id="ij-time", suggester=SuggestFromList(TIME_LIMITS))
        yield Label("nodes")
        yield Input("1", id="ij-nodes", type="integer")
        yield Label("GPUs/node")
        yield Input("", id="ij-gpus", type="integer", placeholder="-")
        yield Label("name")
        yield Input("interactive", id="ij-name")
      yield bash_editor(self.command, "command-text")
      with Horizontal(id="command-buttons"):
        yield Button("Cancel", id="command-cancel")
        yield Button(self.submit_label, id="command-submit", variant="primary")

  def on_mount(self):
    self.query_one("#ij-time").focus()
    def got(partitions):
      if partitions:
        self.query_one("#ij-partition", Select).set_options([(p, p) for p in partitions])
    in_background(self, lambda: [p.rstrip("*") for p in run(["sinfo", "-h", "-o", "%P"], quiet=True).split()],
                  got, group="partitions", connection=self.connection)

  @on(Select.Changed, "#ij-account-list")
  def account_picked(self, event):
    if event.value is not Select.NULL:
      self.query_one("#ij-account", Input).value = event.value

  @on(Input.Changed, ".ij-row Input")
  @on(Select.Changed, "#ij-partition")
  def fields_changed(self, event):
    value = lambda key: self.query_one(f"#ij-{key}", Input).value.strip()
    partition = self.query_one("#ij-partition", Select).value
    self.query_one("#command-text", TextArea).load_text(interactive_job_command(
      value("name"), value("nodes"), value("time"), "" if partition is Select.NULL else partition, value("account"), value("gpus")))

class RunArrayScreen(ModalScreen[list[str] | None]):
  """Submit the job of an array again with another --array range (start, stop, step), with a warning when the
  range covers tasks of the array that are running, queued or completed"""
  DEFAULT_CSS = """
  RunArrayScreen { align: center middle; }
  #array-dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #array-title { text-style: bold; margin-bottom: 1; }
  #array-fields { height: auto; }
  #array-fields Label { padding: 1 1 0 0; }
  #array-fields Input { width: 14; margin-right: 2; }
  #array-warning { height: auto; margin: 1 0; }
  #array-command { height: auto; min-height: 3; max-height: 10; }
  #array-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #array-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

  def __init__(self, jid, args, states, ids):
    super().__init__()
    self.jid, self.args, self.states = jid, list(args), states
    self.start, self.stop = (min(ids), max(ids)) if ids else (0, 0)

  def compose(self) -> ComposeResult:
    with Vertical(id="array-dialog"):
      yield Static(f"Run the array of job {self.jid} with another range", id="array-title")
      with Horizontal(id="array-fields"):
        for label, key, value in (("start", "array-start", self.start), ("stop", "array-stop", self.stop), ("step", "array-step", 1)):
          yield Label(label)
          yield Input(str(value), id=key, type="integer")
      yield Static(id="array-warning")
      yield bash_editor("", "array-command")
      with Horizontal(id="array-buttons"):
        yield Button("Cancel", id="array-cancel")
        yield Button("Submit array", id="array-submit", variant="primary")

  def on_mount(self):
    self.update()
    self.query_one("#array-start").focus()

  def task_ids(self):
    try:
      start, stop, step = (int(self.query_one(f"#array-{k}", Input).value) for k in ("start", "stop", "step"))
    except ValueError:
      return None
    if start < 0 or stop < start or step < 1:
      return None
    return start, stop, step

  @on(Input.Changed)
  def update(self, event=None):
    warning = self.query_one("#array-warning", Static)
    r = self.task_ids()
    if r is None:
      warning.update(Text("start, stop and step must be numbers with 0 ≤ start ≤ stop and step ≥ 1", style="red"))
      return
    start, stop, step = r
    ids = range(start, stop + 1, step)
    busy = {}
    for i in ids:
      state = self.states.get(str(i), "").split()
      state = state[0].rstrip("+") if state else ""
      if state in ("RUNNING", "COMPLETED", "PENDING", "COMPLETING", "CONFIGURING"):
        busy.setdefault(state.lower(), []).append(i)
    if busy:
      warning.update(Text("Warning: the range overlaps tasks of this array that are " + "; ".join(
        f"{state} ({compress_array_ids(tasks)})" for state, tasks in busy.items()) + ". They would run again.",
        style="bold yellow"))
    else:
      warning.update(Text(f"{len(ids)} tasks, none of them running, queued or completed", style="green"))
    self.query_one("#array-command", TextArea).load_text(shlex.join(with_array_spec(self.args, array_range(start, stop, step))))

  @on(Button.Pressed, "#array-submit")
  def submit(self, event):
    event.stop()
    try:
      args = command_args(self.query_one("#array-command", TextArea).text)
    except ValueError as e:
      self.notify(f"invalid command: {e}", severity="error")
      return
    if not args or os.path.basename(args[0]) != "sbatch":
      self.notify("the command must start with sbatch", severity="error")
      return
    self.dismiss(args)

  @on(Button.Pressed, "#array-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  def action_cancel(self):
    self.dismiss(None)

class RestartScreen(ModalScreen[dict | None]):
  """Choose the command to submit when restarting a job"""
  DEFAULT_CSS = """
  RestartScreen { align: center middle; }
  #restart-dialog { width: 100; max-width: 95%; max-height: 90%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #restart-title { text-style: bold; margin-bottom: 1; }
  #restart-options { max-height: 10; }
  #restart-command-label { margin-top: 1; }
  #restart-command { width: 1fr; height: auto; min-height: 3; max-height: 12; }
  #restart-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #restart-same-env, #restart-same-script { margin-top: 1; }
  #restart-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)] # also while the editor has the focus

  def __init__(self, jid, default_args, retry_args=None, retry_label="Retry unfinished array tasks unavailable", original=None):
    super().__init__()
    self.original = original or {"env": None, "script": None, "script_arg": None}
    self.jid = jid
    self.default_args = list(default_args)
    self.retry_args = list(retry_args) if retry_args else None
    self.retry_label = retry_label
    self.default_command = shlex.join(self.default_args)
    self.retry_command = shlex.join(self.retry_args) if self.retry_args else self.default_command
    self.custom_command = self.default_command

  def compose(self) -> ComposeResult:
    with Vertical(id="restart-dialog"):
      yield Static(f"Restart job {self.jid}", id="restart-title")
      with RadioSet(id="restart-options"):
        yield RadioButton("Use the current restart command", value=True)
        yield RadioButton(self.retry_label, disabled=self.retry_args is None)
        yield RadioButton("Custom command")
      yield Label("Command to submit (editable in Custom mode)", id="restart-command-label")
      yield bash_editor(self.default_command, "restart-command", read_only=True)
      env, script = self.original["env"], self.original["script"]
      yield Checkbox("Same environment as the original job" + (f" ({env_summary(env)})" if env else
                     " (not available: this cluster does not store job environments, sacct --env-vars is empty; "
                     "the environment of the login is used)"), value=bool(env), disabled=not env, id="restart-same-env")
      usable = bool(script) and self.original["script_arg"] is not None
      yield Checkbox("Same batch script as submitted" + (" (from Slurm; the file on disk may have changed)" if usable else
                     " (not available: Slurm has not kept it, or the script is not an argument of the command)"
                     if not script else " (kept by Slurm, but the script argument of the command is not clear)"),
                     value=usable, disabled=not usable, id="restart-same-script")
      with Horizontal(id="restart-buttons"):
        yield Button("Cancel", id="restart-cancel")
        yield Button("Submit job", id="restart-submit", variant="primary")

  def on_mount(self):
    self.query_one("#restart-options", RadioSet).focus()

  @on(RadioSet.Changed, "#restart-options")
  def option_changed(self, event):
    command = self.query_one("#restart-command", TextArea)
    if event.index == 2:
      command.read_only = False
      command.load_text(self.custom_command)
      command.focus()
    else:
      command.read_only = True
      command.load_text(self.default_command if event.index == 0 else self.retry_command)

  @on(TextArea.Changed, "#restart-command")
  def custom_command_changed(self, event):
    if self.query_one("#restart-options", RadioSet).pressed_index == 2:
      self.custom_command = event.text_area.text

  @on(Button.Pressed, "#restart-cancel")
  def cancel(self, event):
    event.stop()
    self.dismiss(None)

  @on(Button.Pressed, "#restart-submit")
  def submit(self, event):
    event.stop()
    index = self.query_one("#restart-options", RadioSet).pressed_index
    if index == 0:
      args = self.default_args
    elif index == 1 and self.retry_args is not None:
      args = self.retry_args
    elif index == 2:
      try:
        args = command_args(self.query_one("#restart-command", TextArea).text)
      except ValueError as e:
        self.notify(f"Invalid command: {e}", severity="error")
        return
      if not args or os.path.basename(args[0]) != "sbatch":
        self.notify("The restart command must start with sbatch", severity="error")
        return
    else:
      self.notify("Select a restart command", severity="warning")
      return
    self.dismiss({"args": list(args), "same_env": self.query_one("#restart-same-env", Checkbox).value,
                  "same_script": self.query_one("#restart-same-script", Checkbox).value and index != 2})

  def action_cancel(self):
    self.dismiss(None)

class AddClusterScreen(ModalScreen[str | None]):
  DEFAULT_CSS = """
  AddClusterScreen { align: center middle; }
  #cluster-dialog { width: 72; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #cluster-destination { width: 1fr; }
  #cluster-actions { height: auto; align-horizontal: right; margin-top: 1; }
  #cluster-actions Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def compose(self) -> ComposeResult:
    with Vertical(id="cluster-dialog"):
      yield Label("SSH destination (blank for local machine)")
      yield Input(placeholder="user@host or ~/.ssh/config alias", id="cluster-destination")
      with Horizontal(id="cluster-actions"):
        yield Button("Cancel", id="cluster-cancel")
        yield Button("Add", id="cluster-confirm", variant="primary")

  def on_mount(self):
    self.query_one("#cluster-destination", Input).focus()

  @on(Button.Pressed, "#cluster-cancel")
  def cancel_pressed(self):
    self.dismiss(None)

  @on(Button.Pressed, "#cluster-confirm")
  @on(Input.Submitted, "#cluster-destination")
  def add_pressed(self):
    self.dismiss(self.query_one("#cluster-destination", Input).value.strip())

  def action_cancel(self):
    self.dismiss(None)

CLIPBOARD_TOOLS = [ # (needed environment, command): the first one there gets the text
  ("WAYLAND_DISPLAY", ["wl-copy"]),
  ("DISPLAY", ["xclip", "-selection", "clipboard"]),
  ("DISPLAY", ["xsel", "--clipboard", "--input"]),
  (None, ["pbcopy"]),
  (None, ["clip.exe"]),
]

def osc52(text):
  """The escape sequence that asks the terminal to put text on the clipboard (OSC 52), wrapped for tmux and GNU screen,
  which otherwise keep it to themselves (tmux also needs 'set -g allow-passthrough on' or 'set -g set-clipboard on')"""
  seq = f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\x07"
  if os.environ.get("TMUX"):
    return "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
  if os.environ.get("STY"): # screen: in pieces of at most 768 bytes
    return "".join(f"\x1bP{seq[i:i + 760]}\x1b\\" for i in range(0, len(seq), 760))
  return seq

def send_osc52(app, text):
  app._clipboard = text # (as App.copy_to_clipboard)
  if app._driver is not None:
    app._driver.write(osc52(text))
    app._driver.flush()

def local_clipboard(text):
  """Put text on the clipboard of this machine with its tool (wl-copy, xclip, xsel, pbcopy, clip.exe): the tool
  used, or None if there is none (or it failed). Over SSH only a forwarded display counts: pbcopy or clip.exe
  would fill the clipboard of this machine, not the one of the person at the keyboard"""
  over_ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
  for env, cmd in CLIPBOARD_TOOLS:
    if env and not os.environ.get(env) or not env and over_ssh or not shutil.which(cmd[0]):
      continue
    try:
      if subprocess.run(cmd, input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3).returncode == 0:
        return cmd[0]
    except (OSError, subprocess.TimeoutExpired):
      continue
  return None

COPIED_FILE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "slurp", "copied.txt")

class CopyScreen(ModalScreen):
  """Text that went to the terminal (OSC 52) without knowing if it arrived: ways to get it by hand"""
  DEFAULT_CSS = """
  CopyScreen { align: center middle; }
  #copy-dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #copy-text { height: auto; min-height: 3; max-height: 12; }
  #copy-buttons { height: auto; margin-top: 1; }
  #copy-buttons Button { margin-right: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close", priority=True)]

  def __init__(self, text, what):
    super().__init__()
    self.text, self.what = text, what

  def compose(self) -> ComposeResult:
    with Vertical(id="copy-dialog"):
      yield Static(f"Copy {self.what}", classes="section")
      yield Static("Sent to your terminal (OSC 52), which puts it on your clipboard if it allows that: kitty, "
                   "WezTerm, iTerm2, Alacritty, foot, Windows Terminal do; GNOME Terminal and Konsole don't; in tmux "
                   "it needs 'set -g set-clipboard on'. Over SSH that is the only way to your clipboard. "
                   "If it didn't arrive: Plain text shows it outside of slurp, to select with the mouse.", markup=False)
      yield TextArea(self.text, id="copy-text", read_only=True, soft_wrap=True)
      with Horizontal(id="copy-buttons"):
        yield Button("Plain text", id="copy-plain", variant="primary")
        yield Button("Save to a file", id="copy-save")
        yield Button("It arrived, don't ask again", id="copy-arrived")
        yield Button("Close", id="copy-close")

  def on_mount(self):
    self.query_one("#copy-plain", Button).focus()

  @on(Button.Pressed, "#copy-plain")
  def plain(self):
    """The text in the normal screen of the terminal, where its own selection works"""
    try:
      with self.app.suspend():
        sys.stdout.write("\x1b[2J\x1b[H" + self.text + "\n\n(select it with the mouse; Enter goes back to slurp) ")
        sys.stdout.flush()
        try:
          input()
        except EOFError:
          pass
    except Exception as e: # (no suspending in some drivers, e.g. the web or headless)
      self.notify(f"cannot leave the app for that: {e}", severity="warning")

  @on(Button.Pressed, "#copy-save")
  def save(self):
    try:
      os.makedirs(os.path.dirname(COPIED_FILE), exist_ok=True)
      with open(COPIED_FILE, "w") as f:
        f.write(self.text + "\n")
      self.notify(f"saved {self.what} to {COPIED_FILE}", timeout=8)
    except OSError as e:
      self.notify(f"cannot save to {COPIED_FILE}: {e}", severity="error")

  @on(Button.Pressed, "#copy-arrived")
  def arrived(self):
    from ..config import update_config
    key = copy_key(self.app)
    update_config(**{key: "terminal"})
    where = "in a browser" if key == "copy_web" else "in a terminal"
    self.notify(f"copying {where} now only goes to the terminal, without asking (to ask again: Options, F3)", timeout=6)
    self.dismiss()

  @on(Button.Pressed, "#copy-close")
  def action_close(self):
    self.dismiss()

def copy_key(app):
  """The config key of how copying goes, for the kind of view that is active: "copy" in a terminal, "copy_web" in a
  browser (they differ: a browser's terminal may not pass OSC 52 on, a terminal's may)"""
  return "copy_web" if getattr(app, "view_kind", "terminal") == "web" or app.is_web else "copy"

def copy_text(app, text, what="the text"):
  """Copy text: to the terminal (OSC 52; only terminals that allow it put it on the clipboard) and with the clipboard
  tool of this machine if there is one (not while a browser is the active view: that would be this machine's
  clipboard, not the browser's); without a tool, a window offers ways to get the text by hand, unless copying was
  confirmed to work for this kind of view ("copy" / "copy_web": "terminal" in the config)"""
  from ..config import load_config
  send_osc52(app, text) # OSC 52, through the terminal (wrapped for tmux and screen)
  key = copy_key(app)
  tool = local_clipboard(text) if key == "copy" else None
  if tool:
    app.notify(f"copied {what} ({tool})", timeout=4)
  elif load_config().get(key) == "terminal":
    app.notify(f"copied {what} (through the terminal)", timeout=4)
  else:
    app.push_screen(CopyScreen(text, what))
  return tool
