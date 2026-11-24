"""Dialogs: confirmations, commands to submit (interactive jobs, restarts, array ranges), new tabs"""

import os
import re
import shlex
import base64
import shutil
import subprocess
import sys

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
  Button, Checkbox, Collapsible, DataTable, Input, Label, RadioButton,
  RadioSet, Select, Static, TextArea
)
from textual.suggester import SuggestFromList


from ...cluster.remote import in_background, run
from ...cluster.jobs import compress_array_ids, env_summary, with_array_spec
from ..widgets import bash_editor, command_args, open_clicked_url

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
        yield Button("No", id="confirm-no", compact=True)
        yield Button(self.confirm_label, id="confirm-yes", variant=self.variant, compact=True)

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
        yield Button("Cancel", id="command-cancel", compact=True)
        yield Button(self.submit_label, id="command-submit", variant="primary", compact=True)

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
        yield Button("Cancel", id="command-cancel", compact=True)
        yield Button(self.submit_label, id="command-submit", variant="primary", compact=True)

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
        yield Button("Cancel", id="array-cancel", compact=True)
        yield Button("Submit array", id="array-submit", variant="primary", compact=True)

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
    shown, off = array_ids_text(array_range(start, stop, step))
    if busy:
      text = Text("Warning: the range overlaps tasks of this array that are " + "; ".join(
        f"{state} ({compress_array_ids(tasks)})" for state, tasks in busy.items()) + ". They would run again.",
        style="bold yellow")
    else:
      text = Text(f"{len(ids)} task{'s' if len(ids) != 1 else ''}, none of them running, queued or completed", style="green")
    text.append(f"\n{shown}")
    if off:
      text.append(f" · {off}", style="bold yellow")
    warning.update(text)
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

ARRAY_SPEC = re.compile(r"(\d+(-\d+(:\d+)?)?)(,\d+(-\d+(:\d+)?)?)*(%\d+)?")

def array_parts(spec):
  """The ranges of an sbatch --array expression (1-100:2,200%10): [(start, stop, step, last)], last the last id the
  range gets (with a step not always its stop: 1-10:4 is 1, 5, 9); None if it is not one"""
  if not ARRAY_SPEC.fullmatch(spec or ""):
    return None
  parts = []
  for part in spec.split("%")[0].split(","):
    m = re.fullmatch(r"(\d+)(?:-(\d+)(?::(\d+))?)?", part)
    start = int(m[1]); stop = int(m[2]) if m[2] else start; step = int(m[3]) if m[3] else 1
    if stop < start or step < 1:
      return None
    parts.append((start, stop, step, start + (stop - start) // step * step))
  return parts

def array_task_count(spec):
  """The number of tasks of an sbatch --array expression (1-100:2,200%10), None if it is not one"""
  parts = array_parts(spec)
  if parts is None:
    return None
  if sum((stop - start) // step + 1 for start, stop, step, _ in parts) > 2_000_000: # (no set of millions: as if apart)
    return sum((stop - start) // step + 1 for start, stop, step, _ in parts)
  ids = set()
  for start, stop, step, _ in parts:
    ids.update(range(start, stop + 1, step))
  return len(ids)

def array_ids_text(spec):
  """Which ids an --array expression gives: (the first and the last id, with the first few; a warning for each range
  whose last id is not its end, or "")"""
  parts = array_parts(spec)
  if not parts:
    return "", ""
  first, last = min(p[0] for p in parts), max(p[3] for p in parts)
  few = []
  for start, stop, step, _ in sorted(parts):
    for i in range(start, min(stop, start + 4 * step) + 1, step):
      if i not in few:
        few.append(i)
  few = sorted(few)
  count = array_task_count(spec)
  if count == 1:
    ids = f"id {first}"
  elif count <= 4:
    ids = "ids " + ", ".join(map(str, sorted(set(few) | {last})[:count]))
  else:
    ids = f"ids {', '.join(map(str, few[:3]))}, …, {last}"
  ids += f" (first {first}, last {last})" if count > 1 else ""
  off = [f"{start}-{stop}:{step}: the last id is {end}, not {stop} (steps of {step} from {start} do not reach {stop})"
         for start, stop, step, end in parts if end != stop]
  return ids, "; ".join(off)

def without_array(args):
  """An sbatch argv without its --array options"""
  return [args[0], *with_array_spec(args, "x")[2:]] if args else args

class SubmitScriptScreen(ModalScreen[list[str] | None]):
  """Submit a job script: the sbatch command, editable, and an array range (empty: as the script says) that writes
  its --array option; returns the argv to run, or None"""
  DEFAULT_CSS = """
  SubmitScriptScreen { align: center middle; }
  #submit-dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #submit-title { text-style: bold; margin-bottom: 1; }
  #submit-fields { height: auto; }
  #submit-fields Label { padding: 1 1 0 0; }
  #submit-array { width: 30; }
  #submit-array-note { height: auto; margin: 1 0; }
  #submit-command { height: auto; min-height: 3; max-height: 12; }
  #submit-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #submit-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

  def __init__(self, path, problems, script_array="", uses_task_id=True, errors=False):
    super().__init__()
    self.path, self.problems, self.script_array = path, problems, script_array or ""
    self.uses_task_id, self.errors = uses_task_id, errors

  def compose(self) -> ComposeResult:
    with Vertical(id="submit-dialog"):
      yield Static(f"Submit {os.path.basename(self.path)}", id="submit-title", markup=False)
      yield Static(self.problems + f" It is submitted in {os.path.dirname(self.path)}.", markup=False)
      with Horizontal(id="submit-fields"):
        yield Label("Array (task ids)")
        yield Input(self.script_array, id="submit-array", placeholder="e.g. 1-100, 0-9:2, 1,5,7, 1-1000%50",
                    tooltip="an array job: its task ids (sbatch --array); empty: not an array (or as the script says)")
      yield Static(id="submit-array-note")
      yield bash_editor(shlex.join(["sbatch", self.path]), "submit-command")
      with Horizontal(id="submit-buttons"):
        yield Button("Cancel", id="submit-cancel", compact=True)
        yield Button("Submit", id="submit-submit", variant="error" if self.errors else "primary", compact=True)

  def on_mount(self):
    self.array_changed()
    self.query_one("#submit-command").focus()

  def command(self):
    try:
      return command_args(self.query_one("#submit-command", TextArea).text)
    except ValueError:
      return None

  @on(Input.Changed, "#submit-array")
  def array_changed(self, event=None):
    """The range writes the command's --array option (the rest of the command, edited or not, stays)"""
    spec = self.query_one("#submit-array", Input).value.strip()
    note = self.query_one("#submit-array-note", Static)
    count = array_task_count(spec) if spec else None
    if spec and count is None:
      note.update(Text("not an array range: ids like 1-100, 0-9:2 (every 2nd), 1,5,7, and %50 for at most 50 at a time",
                       style="red"))
      return
    args = self.command() or ["sbatch", self.path]
    args = without_array(args)
    if spec and spec != self.script_array: # (the script's own --array needs no option)
      args = with_array_spec(args, spec)
    if not spec:
      note.update(Text("not an array: one job" if not self.script_array else
                       f"the script asks for --array={self.script_array} itself (sbatch cannot take it back)", style="dim"))
    else:
      limit = spec.split("%")[1] if "%" in spec else ""
      ids, off = array_ids_text(spec)
      text = Text(f"an array of {count} task{'s' if count != 1 else ''}, {ids}" + (f", at most {limit} at a time" if limit else ""),
                  style="green")
      if off:
        text.append(f" · {off}", style="bold yellow")
      if not self.uses_task_id:
        text.append(" · the script never uses $SLURM_ARRAY_TASK_ID: every task would do the same (B03)", style="bold yellow")
      note.update(text)
    editor = self.query_one("#submit-command", TextArea)
    if self.command() != args: # (only when it changes: the user's line breaks stay otherwise)
      editor.load_text(shlex.join(args))

  @on(Button.Pressed, "#submit-submit")
  def submit(self, event):
    event.stop()
    try:
      args = command_args(self.query_one("#submit-command", TextArea).text)
    except ValueError as e:
      self.notify(f"invalid command: {e}", severity="error")
      return
    if not args or os.path.basename(args[0]) != "sbatch":
      self.notify("the command must start with sbatch", severity="error")
      return
    self.dismiss(args)

  @on(Button.Pressed, "#submit-cancel")
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
                     "the environment of the login is used)"), value=bool(env), disabled=not env, id="restart-same-env", compact=True)
      usable = bool(script) and self.original["script_arg"] is not None
      yield Checkbox("Same batch script as submitted" + (" (from Slurm; the file on disk may have changed)" if usable else
                     " (not available: Slurm has not kept it, or the script is not an argument of the command)"
                     if not script else " (kept by Slurm, but the script argument of the command is not clear)"),
                     value=usable, disabled=not usable, id="restart-same-script", compact=True)
      with Horizontal(id="restart-buttons"):
        yield Button("Cancel", id="restart-cancel", compact=True)
        yield Button("Submit job", id="restart-submit", variant="primary", compact=True)

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

class SshCheckScreen(ModalScreen):
  """The check of an SSH connection in a terminal, as the tab's login goes (cluster/sshcheck.py): what needs no login,
  the command before the login, the login itself (answer what it asks: a host key, a passphrase, a password, a
  code), then the verdict and the findings; Again, Close (Ctrl-] while it runs: the keys go to the terminal)"""
  DEFAULT_CSS = """
  SshCheckScreen { align: center middle; }
  #sc-dialog { width: 96%; height: 92%; border: thick $accent; background: $surface; padding: 0 1; }
  #sc-title { text-style: bold; height: 1; margin: 1 0 0 0; }
  #sc-status { height: 1; color: $text-muted; }
  #sc-buttons { height: auto; align-horizontal: right; margin: 1 0 0 0; }
  #sc-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("ctrl+right_square_bracket", "close", "Close", priority=True)]

  def __init__(self, destination, settings=None, pre_login="", pre_login_fallback=False):
    super().__init__()
    import json
    from ...cluster import sshsetup
    self.what = sshsetup.label(settings) if settings else destination
    self.args = [sys.executable, "-m", "slurp.cluster.sshcheck",
                 json.dumps({"destination": destination, "ssh": settings or None, "pre_login": pre_login or "",
                             "pre_login_fallback": bool(pre_login_fallback)})]
    self.session = None

  def compose(self) -> ComposeResult:
    from ..terminal import TerminalView
    with Vertical(id="sc-dialog"):
      yield Static(f"Check of the connection to {self.what}", id="sc-title", markup=False)
      yield Static(id="sc-status")
      yield TerminalView(session=self.new_session())
      with Horizontal(id="sc-buttons"):
        yield Button("Again", id="sc-again", compact=True, tooltip="check again from the start")
        yield Button("Close", id="sc-close", variant="primary", compact=True, tooltip="close (Ctrl-] while it runs)")

  def new_session(self):
    from ..terminal import TerminalSession
    self.session = TerminalSession(self.app, self.args, f"check {self.what}")
    return self.session

  def on_mount(self):
    from ..terminal import TerminalView
    view = self.query_one(TerminalView)
    view.focus()
    self.call_after_refresh(view.attach)
    self.focused_done = False
    self.set_interval(0.3, self.show_status)

  def show_status(self):
    s = self.session
    if s is not None and s.done:
      self.query_one("#sc-status", Static).update(Text("done · Again checks once more, Esc or Close closes", style="green"))
      if not self.focused_done:
        self.focused_done = True
        self.query_one("#sc-close", Button).focus()
    else:
      self.query_one("#sc-status", Static).update(Text("running · answer what it asks in the terminal (it has the keys) · "
                                                       "Ctrl-] closes", style="yellow"))

  @on(Button.Pressed, "#sc-again")
  def again(self, event):
    event.stop()
    from ..terminal import TerminalView
    if self.session is not None and not self.session.done:
      self.session.terminate()
    view = self.query_one(TerminalView)
    view.own_session = self.new_session()
    self.focused_done = False
    view.focus()
    self.call_after_refresh(view.attach)

  @on(Button.Pressed, "#sc-close")
  def action_close(self, event=None):
    if event is not None:
      event.stop()
    if self.session is not None and not self.session.done:
      self.session.terminate()
    self.dismiss()


def sites_requirable():
  from ...sites import REQUIRABLE
  return REQUIRABLE

def sshsetup_label(settings):
  from ...cluster import sshsetup
  return sshsetup.label(settings)

def site_login_text(entry):
  """What a site's cluster's login is, what it needs here (found or not), what to do once; where it comes from"""
  out = Text()
  out.append(f"{entry['site']}", style="bold")
  out.append(f" (site pack {entry['version'] or 'without version'}, {'built in' if entry['builtin'] else 'yours'}): ", style="dim")
  how = [f"through the jump host {entry['jump'].replace('{user}@', '')}" if entry.get("jump") else "directly",
         f"with {', '.join(entry.get('identities', []))}" if entry.get("identities") else ""]
  out.append("logs in " + " ".join(h for h in how if h))
  if entry.get("pre_login"):
    out.append(f"; first {entry['pre_login']} here, in the login pane"
               + (" (when a login without it fails)" if entry.get("pre_login_fallback") else " (before every login)"))
  out.append(". Fill in " + (entry.get("user_label") or "your user there") + " (User).\n")
  if entry.get("scheduler", "slurm") != "slurm":
    from ... import sites
    out.append(f"Its batch system is {sites.SCHEDULERS.get(entry['scheduler'], entry['scheduler'])}, not Slurm: slurp logs in and has "
               "its files, shells and transfers, but no job list and no checks of job scripts there.\n", style="yellow")
  for need in entry.get("needs", []):
    found = shutil.which(need["command"])
    out.append(f"✔ {need['command']}: {found}\n" if found else f"✖ {need['command']} is not installed here: ", style="green" if found else "red")
    if not found:
      out.append(need.get("install", "") + "\n")
  if entry.get("setup"):
    out.append(entry["setup"].strip() + "\n")
  if not entry["builtin"]:
    out.append("From your own pack: check what it fills in (the command runs on this machine).\n", style="yellow")
  return out

def site_login_way(entry):
  """How a site's cluster logs in, in a few words (a column of the search window)"""
  parts = [f"via {entry['jump'].replace('{user}@', '')}" if entry.get("jump") else "direct"]
  if entry.get("pre_login"):
    parts.append(entry["pre_login"].split()[0])
  elif entry.get("identities"):
    parts.append("key")
  if entry.get("scheduler", "slurm") != "slurm": # (no job list there: said in the list already)
    from ... import sites
    parts.append(f"no Slurm: {sites.SCHEDULERS.get(entry['scheduler'], entry['scheduler'])}")
  return ", ".join(parts)

def site_cluster_words(entry):
  """What a search of the sites' clusters looks in: the cluster, its site, its host and jump host, the pack (its file)"""
  words = [entry.get("title", ""), entry.get("name", ""), entry.get("site", ""), entry.get("host", ""), entry.get("id", ""),
           entry.get("jump", "").replace("{user}@", ""), entry.get("scheduler", "slurm")]
  return " ".join(words).lower()

class SiteClusterScreen(ModalScreen[str | None]):
  """The clusters of the site packs, to choose one for a new SSH connection: a search (each word must match the
  cluster, its site, host or pack), the list (↑ ↓ while typing; Enter or a click chooses), and how the highlighted one
  logs in. Dismissed with its id, or None"""
  DEFAULT_CSS = """
  SiteClusterScreen { align: center middle; }
  #sc-dialog { width: 120; max-width: 96%; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #sc-title { text-style: bold; height: auto; }
  #sc-search { margin-top: 1; }
  #sc-list { height: 1fr; margin-top: 1; }
  #sc-count { height: auto; color: $text-muted; }
  #sc-info-scroll { height: 12; border: round $panel; padding: 0 1; }
  #sc-info { height: auto; }
  #sc-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #sc-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, clusters, chosen=None):
    super().__init__()
    self.clusters, self.chosen = clusters, chosen
    self.shown = []

  def compose(self) -> ComposeResult:
    from ..widgets import ClickTable
    with Vertical(id="sc-dialog"):
      yield Static("Choose a cluster to log in to", id="sc-title")
      yield Input(placeholder="search: a cluster, a site, a host (e.g. daint, jülich, lumi, cineca); ↑ ↓ choose, Enter takes it",
                  id="sc-search")
      yield ClickTable(id="sc-list", cursor_type="row", zebra_stripes=True)
      yield Static(id="sc-count")
      with VerticalScroll(id="sc-info-scroll"):
        yield Static(id="sc-info")
      with Horizontal(id="sc-buttons"):
        yield Button("Cancel", id="sc-cancel", compact=True)
        yield Button("Choose", id="sc-ok", variant="primary", compact=True)

  def on_mount(self):
    from ..widgets import ClickTable
    table = self.query_one("#sc-list", ClickTable)
    for key, label in (("cluster", "Cluster"), ("site", "Site"), ("host", "Host"), ("login", "Login"), ("pack", "Pack")):
      table.add_column(label, key=key)
    self.populate()
    if self.chosen in [c["id"] for c in self.shown]:
      table.move_cursor(row=[c["id"] for c in self.shown].index(self.chosen))
    self.query_one("#sc-search", Input).focus()

  def populate(self):
    from ..widgets import ClickTable
    table = self.query_one("#sc-list", ClickTable)
    terms = self.query_one("#sc-search", Input).value.lower().split()
    terms = [t.replace("ü", "u").replace("ue", "u") for t in terms] # (jülich, juelich, julich)
    def matches(entry):
      words = site_cluster_words(entry).replace("ü", "u").replace("ue", "u")
      return all(t in words for t in terms)
    self.shown = [c for c in self.clusters if matches(c)]
    self.shown.sort(key=lambda c: (c["site"].lower(), c["title"].lower()))
    table.clear()
    for c in self.shown:
      pack = f"{c['version'] or '-'}" + ("" if c["builtin"] else " (yours)")
      table.add_row(Text(c["title"], style="bold"), c["site"], c.get("host", ""), site_login_way(c), pack, key=c["id"])
    if self.shown:
      table.move_cursor(row=0) # (a new search: its first match)
    self.query_one("#sc-count", Static).update(
      f"{len(self.shown)} of {len(self.clusters)} clusters" + (f" match {' '.join(terms)}" if terms else "")
      + " · from the site packs (built in, and yours in ~/.config/slurp/rules)")
    self.query_one("#sc-ok", Button).disabled = not self.shown
    self.show_info()

  def current(self):
    from ..widgets import ClickTable
    table = self.query_one("#sc-list", ClickTable)
    if not self.shown or not table.row_count:
      return None
    return self.shown[min(table.cursor_row, len(self.shown) - 1)]

  def show_info(self):
    entry = self.current()
    self.query_one("#sc-info", Static).update(site_login_text(entry) if entry else Text("no cluster matches", style="dim"))

  @on(Input.Changed, "#sc-search")
  def search_changed(self, event):
    self.populate()

  def on_key(self, event):
    """↑ ↓ (and page up, page down) move in the list while the search has the focus"""
    from ..widgets import ClickTable
    if self.focused is not self.query_one("#sc-search", Input):
      return
    table = self.query_one("#sc-list", ClickTable)
    moves = {"down": table.action_cursor_down, "up": table.action_cursor_up, "pagedown": table.action_page_down,
             "pageup": table.action_page_up}
    if event.key in moves:
      event.stop()
      event.prevent_default()
      moves[event.key]()

  @on(DataTable.RowHighlighted, "#sc-list")
  def highlighted(self, event):
    self.show_info()

  @on(DataTable.RowSelected, "#sc-list")
  def selected(self, event):
    event.stop()
    self.dismiss(event.row_key.value)

  @on(Input.Submitted, "#sc-search")
  @on(Button.Pressed, "#sc-ok")
  def ok(self, event):
    event.stop()
    entry = self.current()
    if entry is not None:
      self.dismiss(entry["id"])

  @on(Button.Pressed, "#sc-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

class AddClusterScreen(ModalScreen):
  """A new tab, as the forms of the endpoints: its name (the tab's), then the main way: an SSH destination, a Host of
  ~/.ssh/config (its user, jump hosts and keys are there; suggested as you type) or user@host; empty: this machine.
  The fallback: the full SSH settings (user, host, port, jump hosts, identity files, more options). Test tries the
  connection in a terminal and says what is wrong; Add adds the tab, tested or not. Dismissed with the destination
  (str), {"destination" or "ssh": settings, "name", "pre_login", …}, or None.
  With a cluster: its SSH settings, to see and change (Save): filled in, with the blocks of ~/.ssh/config that apply
  and what ssh makes of it all (ssh -G)."""
  DEFAULT_CSS = """
  AddClusterScreen { align: center middle; }
  #cluster-dialog { width: 100; max-width: 98%; height: auto; max-height: 96%; border: thick $accent; background: $surface; padding: 1 2; }
  #cluster-body { height: auto; max-height: 80vh; } /* (the buttons below stay in view, whatever is above) */
  #cluster-title { text-style: bold; margin-bottom: 1; }
  .cluster-note { height: auto; color: $text-muted; }
  .cluster-row { height: auto; margin-top: 1; }
  .cluster-row Label { width: 18; }
  .cluster-row Input { width: 1fr; }
  #cluster-hint { height: auto; color: $text-muted; margin: 0 0 1 18; }
  #cluster-site { width: 1fr; height: 1; }
  #cluster-site-choose, #cluster-site-clear { margin-left: 1; }
  #cluster-site-info { height: auto; margin: 1 0 0 18; padding: 0 1; border-left: wide $accent; }
  #cluster-full { height: auto; margin-bottom: 1; }
  .ssh-row { height: 1; margin-bottom: 1; }
  /* a field to fill in: yellow, red after Add or Test without it; the terminal's yellow and red, in every theme ($warning
     is a red in some) and when focused too (the cursor is often there). The one-line fields: a bar at each end, no
     lines above and below (they stay one line high); the others have a border anyway: its colour */
  #cluster-dialog Input.-required, #cluster-dialog Input.-required:focus { border: round ansi_yellow; }
  #cluster-dialog Input.-missing, #cluster-dialog Input.-missing:focus { border: round ansi_red; }
  #cluster-dialog .ssh-row Input.-required, #cluster-dialog .ssh-row Input.-required:focus {
    height: 1; padding: 0; border: none; border-left: outer ansi_yellow; border-right: outer ansi_yellow; }
  #cluster-dialog .ssh-row Input.-missing, #cluster-dialog .ssh-row Input.-missing:focus {
    border-left: outer ansi_red; border-right: outer ansi_red; }
  .required-hint { height: auto; color: ansi_red; margin: 0 0 1 16; }
  .ssh-row Label { width: 16; padding: 0 1 0 0; }
  .ssh-row Input { width: 1fr; }
  #ssh-identities-browse { margin-left: 1; }
  #ssh-port { width: 12; }
  #ssh-options { height: 5; }
  #cluster-config { height: auto; margin-bottom: 1; }
  #cluster-actions { height: auto; align-horizontal: right; margin-top: 1; }
  #cluster-actions Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, cluster=None):
    super().__init__()
    self.cluster = cluster # (its SSH settings to change; None: a new tab)

  def compose(self) -> ComposeResult:
    from ...cluster import sshsetup
    hosts = sshsetup.config_hosts()
    with Vertical(id="cluster-dialog"):
      with VerticalScroll(id="cluster-body"):
        yield Static(f"The SSH connection {self.cluster.name}" if self.cluster else "A new SSH connection", id="cluster-title", markup=False)
        yield Static("A cluster (or this machine) as a tab: its jobs, files, shells; and an endpoint of transfers.",
                     classes="cluster-note", markup=False)
        sites = self.site_choices()
        if sites:
          with Horizontal(classes="cluster-row"):
            yield Label("From a site")
            yield Static(Text("(none: fill in yourself)", style="dim"), id="cluster-site")
            yield Button("Choose…", id="cluster-site-choose", compact=True,
                         tooltip=f"a cluster slurp knows how to log in to ({len(sites)}, searchable): fills in the fields "
                         "below (see and change them before Add)")
            yield Button("Clear", id="cluster-site-clear", compact=True, tooltip="no site: the form as you fill it in")
          yield Static(id="cluster-site-info")
        with Horizontal(classes="cluster-row"):
          yield Label("Name")
          yield Input(placeholder="the tab's name, shown everywhere (default: the destination)", id="cluster-name")
        with Horizontal(classes="cluster-row"):
          yield Label("SSH destination")
          yield Input(placeholder="a Host of your ~/.ssh/config (best), or user@host; empty: this machine", id="cluster-destination",
                      suggester=SuggestFromList(hosts, case_sensitive=False) if hosts else None)
        yield Static("The best way: an entry of ~/.ssh/config with the user, jump hosts and keys of the cluster; ssh, "
                     "scp and your other tools use it too." + (f" Yours: {', '.join(hosts[:12])}" + (" …" if len(hosts) > 12 else "")
                     if hosts else " You have none yet."), id="cluster-hint", markup=False)
        with Collapsible(title="Full SSH settings (instead of a ~/.ssh/config entry)", id="cluster-full", collapsed=True):
          for label, key, placeholder in (("Host", "host", "host name or address, e.g. daint.alps.cscs.ch"),
                                          ("User", "user", "your user there (default: as here, or ~/.ssh/config)"),
                                          ("Port", "port", "22"),
                                          ("Jump hosts", "jump", "ProxyJump: user@gateway[:port], more separated by commas"),
                                          ("Identity files", "identities", "private keys, e.g. ~/.ssh/cscs-key (more: commas)")):
            with Horizontal(classes="ssh-row", id=f"ssh-{key}-row"):
              yield Label(label)
              yield Input(placeholder=placeholder, id=f"ssh-{key}", compact=True)
              if key == "identities":
                yield Button("Browse…", id="ssh-identities-browse", compact=True,
                             tooltip="choose a private key on this machine (its content is not read or shown)")
            yield Static(id=f"ssh-{key}-hint", classes="required-hint", markup=False)
          yield Label("More ssh options, one per line: Key=Value (e.g. ForwardAgent=yes, ServerAliveInterval=30, "
                      "UserKnownHostsFile=…, PreferredAuthentications=…, ProxyCommand=…)")
          yield TextArea(id="ssh-options", soft_wrap=False)
        with Horizontal(classes="cluster-row"):
          yield Label("Run before login")
          yield Input(placeholder="(optional) a command here before every login, e.g. one that signs your key (cscs-keygen, step ssh login …)",
                      id="ssh-pre-login", tooltip="it runs in the login pane: answer it there (a password, a code); then the login follows. "
                      "What it exports (eval $(ssh-agent): SSH_AUTH_SOCK, …) is there for the login")
        yield Static(id="ssh-pre-login-hint", classes="required-hint", markup=False)
        yield Checkbox("only when the login fails without it (first a login without it: a key or certificate that is still "
                       "valid is not made anew)", id="ssh-pre-login-fallback", compact=True)
        if self.cluster is not None:
          yield Static(id="cluster-config")
      with Horizontal(id="cluster-actions"): # (as the endpoints' forms: Test, Cancel, Add)
        yield Button("Test", id="cluster-check-button", compact=True,
                     tooltip="try the connection in a terminal, as the login goes (answer what it asks), and say what is wrong")
        yield Button("Cancel", id="cluster-cancel", compact=True)
        yield Button("Save" if self.cluster else "Add", id="cluster-confirm", variant="primary", compact=True,
                     tooltip="the tab takes these settings (the next login uses them)" if self.cluster else
                     "add the tab (checked or not); it logs in")

  site = None # (the site's cluster chosen in From a site: its entry of sites.site_clusters())

  def site_choices(self):
    """The clusters of the site packs (a new tab only: editing one keeps its own settings)"""
    if self.cluster is not None:
      return []
    from ... import sites
    try:
      return sites.site_clusters()
    except Exception: # (a pack that cannot be read must not keep the form from opening)
      return []

  def on_mount(self):
    self.query_one("#cluster-name", Input).focus()
    if self.query("#cluster-site-info"):
      self.query_one("#cluster-site-info").display = False
      self.query_one("#cluster-site-clear").display = False
    for hint in self.query(".required-hint"):
      hint.display = False
    cluster = self.cluster
    if cluster is None:
      return
    settings = cluster.ssh or {}
    default = sshsetup_label(settings) if settings else cluster.destination
    if cluster.name != default: # (its own name: shown; the default stays empty, it follows the destination)
      self.query_one("#cluster-name", Input).value = cluster.name
    self.query_one("#ssh-pre-login", Input).value = cluster.pre_login or ""
    self.query_one("#ssh-pre-login-fallback", Checkbox).value = bool(cluster.pre_login_fallback)
    if settings:
      self.query_one("#cluster-full", Collapsible).collapsed = False
      for key in ("host", "user", "port", "jump"):
        self.query_one(f"#ssh-{key}", Input).value = str(settings.get(key) or "")
      if settings.get("name"):
        self.query_one("#cluster-name", Input).value = settings["name"]
      self.query_one("#ssh-identities", Input).value = ", ".join(settings.get("identities") or [])
      self.call_after_refresh(self.mark_required)
      self.query_one("#ssh-options", TextArea).load_text("\n".join(settings.get("options") or []))
      self.query_one("#ssh-host", Input).focus()
    else:
      self.query_one("#cluster-destination", Input).value = cluster.destination or ""
    self.show_config()

  @on(Button.Pressed, "#cluster-site-choose")
  def choose_site(self, event=None):
    """The search window of the sites' clusters; the one chosen fills in the form"""
    if event is not None:
      event.stop()
    def chosen(cluster_id):
      if cluster_id is not None:
        self.site_chosen(cluster_id)
    self.app.push_screen(SiteClusterScreen(self.site_choices(), (self.site or {}).get("id")), chosen)

  @on(Button.Pressed, "#cluster-site-clear")
  def clear_site(self, event=None):
    if event is not None:
      event.stop()
    self.site_chosen(None)

  def site_chosen(self, cluster_id):
    """A site's cluster (its id): its login fills in the form (the full settings, the command before the login), and
    what it needs and what to do once is said; None: no site, the form as it was filled in"""
    from ... import sites
    entry = next((c for c in self.site_choices() if c["id"] == cluster_id), None)
    self.site = entry
    info = self.query_one("#cluster-site-info", Static)
    info.display = entry is not None
    self.query_one("#cluster-site", Static).update(Text.assemble((entry["title"], "bold"), f" · {entry['site']}") if entry
                                                    else Text("(none: fill in yourself)", style="dim"))
    self.query_one("#cluster-site-clear").display = entry is not None
    self.mark_required()
    if entry is None:
      return
    user = self.query_one("#ssh-user", Input).value.strip()
    fill = sites.login_settings(entry, user)
    self.query_one("#cluster-name", Input).value = fill["name"]
    self.query_one("#cluster-destination", Input).value = ""
    self.query_one("#cluster-full", Collapsible).collapsed = False
    for key in ("host", "port", "jump"):
      self.query_one(f"#ssh-{key}", Input).value = fill[key]
    self.query_one("#ssh-identities", Input).value = ", ".join(fill["identities"])
    self.query_one("#ssh-options", TextArea).load_text("\n".join(fill["options"]))
    self.query_one("#ssh-pre-login", Input).value = fill["pre_login"]
    self.query_one("#ssh-pre-login-fallback", Checkbox).value = fill["pre_login_fallback"]
    field = self.query_one("#ssh-user", Input)
    field.placeholder = entry.get("user_label") or "your user there"
    info.update(self.site_text(entry))
    self.mark_required()
    first = next((key for key in entry.get("required", []) if not self.field_value(key)), None)
    if first:
      self.query_one(f"#ssh-{first.replace('_', '-')}", Input).focus()

  def site_text(self, entry):
    return site_login_text(entry)

  REQUIRED_LABELS = {"user": "the user", "host": "the host", "port": "the port", "jump": "the jump host",
                     "identities": "the identity files", "pre_login": "the command before the login"}

  def field_value(self, key):
    return self.query_one(f"#ssh-{key.replace('_', '-')}", Input).value.strip()

  def required_fields(self):
    """The fields the chosen site's cluster needs filled in (its pack's required)"""
    return list((self.site or {}).get("required", []))

  def field_problem(self, key):
    """What is wrong with a filled-in field, "" if nothing: the identity files must be on this machine, files (not
    directories), and private keys (not the .pub half). Only whether they are there: none is read"""
    if key != "identities":
      return ""
    problems = []
    for path in (p.strip() for p in self.field_value("identities").split(",")):
      full = os.path.expanduser(path)
      if not path:
        continue
      if not os.path.exists(full):
        problems.append(f"{path} does not exist (here, on this machine)")
      elif os.path.isdir(full):
        problems.append(f"{path} is a directory, not a key")
      elif path.endswith(".pub"):
        problems.append(f"{path} is the public half: choose the private key ({path[:-4]})")
    return "; ".join(problems)

  def mark_required(self, pressed=False):
    """The fields to fill in: a yellow bar (border) while empty; pressed (Add, Test): the empty ones red, each with
    what to fill in below it. A field that is wrong (an identity file that is not there): red at once, what is wrong
    below it. The others (or no site chosen): as usual. [the empty required ones and the wrong ones]"""
    required, missing = self.required_fields(), []
    for key in sites_requirable():
      field, hint = self.query_one(f"#ssh-{key.replace('_', '-')}", Input), self.query_one(f"#ssh-{key.replace('_', '-')}-hint", Static)
      empty = key in required and not field.value.strip()
      problem = self.field_problem(key)
      marked = empty or bool(problem)
      if marked:
        missing.append(key)
      if not hasattr(field, "_was_compact"):
        field._was_compact = field.compact
      field.compact = field._was_compact and not marked # (a compact input has no border, whatever the CSS says)
      field.set_class(marked, "-required")
      field.set_class(bool(problem) or empty and (pressed or field.has_class("-missing")), "-missing")
      row = self.query(f"#ssh-{key}-row")
      if row:
        row.first().set_class(marked, "-required")
      if problem:
        hint.update(f"↑ {problem}")
        hint.display = True
      elif empty and field.has_class("-missing"):
        site = (self.site or {}).get("site", "the site")
        if key == "identities": # (a site that takes keys only: which one, and where to find it)
          hint.update(f"↑ fill in the private key whose public half you registered at {site} (Browse… finds it): "
                      "it logs in with a key only")
        else:
          what = (self.site or {}).get("user_label") if key == "user" else ""
          hint.update(f"↑ fill in {what or self.REQUIRED_LABELS[key]}: {site} needs it")
        hint.display = True
      else:
        hint.display = False
    if not missing and self.anyway:
      self.set_anyway(False)
    return missing

  anyway = False # (Add and Test were pressed with marked fields: they say "… anyway", the next press goes on)

  def set_anyway(self, on):
    """Add and Test as "Add anyway", "Test anyway" (red): the marks are only advice (the command before the login may
    make the key, the user may come from ~/.ssh/config, …), the next press goes on; off: as they were"""
    self.anyway = on
    for button, label, variant in (("#cluster-check-button", "Test", "default"), ("#cluster-confirm", "Save" if self.cluster else "Add", "primary")):
      b = self.query_one(button, Button)
      b.label, b.variant = (f"{label} anyway", "error") if on else (label, variant)

  def missing_refused(self, action):
    """Add or Test with fields marked (empty required ones, identity files that are not there): they turn red, said,
    and the buttons say "… anyway": True (nothing happens yet). Pressed again: False (it goes on)"""
    missing = self.mark_required(pressed=True)
    if not missing:
      self.set_anyway(False)
      return False
    if self.anyway:
      return False
    if missing[0] != "pre_login":
      self.query_one("#cluster-full", Collapsible).collapsed = False
    self.query_one(f"#ssh-{missing[0].replace('_', '-')}", Input).focus()
    said = [self.field_problem(k) or f"fill in {self.REQUIRED_LABELS[k]}" for k in missing]
    self.set_anyway(True)
    self.notify(f"{action}: " + "; ".join(said) + f" (marked red). {action} anyway: press it again", severity="error")
    return True

  config_timer = None

  @on(Input.Changed, "#ssh-user, #ssh-host, #ssh-port, #ssh-jump, #ssh-identities, #ssh-pre-login")
  def required_changed(self, event):
    """(a required field filled in: its mark goes; emptied again: yellow, red only after the next Add or Test; an
    identity file: red at once while it is not there, with or without a site)"""
    if self.site is not None or event.input.id == "ssh-identities":
      if not event.value.strip():
        event.input.remove_class("-missing")
      self.mark_required()

  @on(Button.Pressed, "#ssh-identities-browse")
  def browse_identity(self, event):
    """A private key on this machine to choose (nothing of it read or shown): it replaces the field's last file if
    that is not there, else it is added (more keys: commas)"""
    event.stop()
    from .files import FilePickScreen
    field = self.query_one("#ssh-identities", Input)
    paths = [p.strip() for p in field.value.split(",") if p.strip()]
    current = os.path.expanduser(paths[-1]) if paths else ""
    ssh_dir = os.path.expanduser("~/.ssh")
    start = os.path.dirname(current) if current and os.path.isdir(os.path.dirname(current)) else ssh_dir if os.path.isdir(ssh_dir) else os.path.expanduser("~")
    def chosen(path):
      if not path:
        return
      home = os.path.expanduser("~")
      short = "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
      field.value = ", ".join(paths[:-1] + [short]) if paths and not os.path.exists(current) else ", ".join(paths + [short]) if paths else short
      field.focus()
    self.app.push_screen(FilePickScreen("An identity file (a private key) on this machine", start, chosen=current, local=True, preview=False), chosen)

  @on(Input.Changed, "#ssh-user")
  def site_user_changed(self, event):
    """(a site's jump host or command before the login that wants the user too: it follows the user filled in, unless
    you changed it yourself)"""
    if self.site is None:
      return
    from ... import sites
    for key, field_id, wants in (("jump", "#ssh-jump", "{user}@"), ("pre_login", "#ssh-pre-login", "{user}")):
      if wants not in (self.site.get(key) or ""):
        continue
      field = self.query_one(field_id, Input)
      if field.value in (sites.login_settings(self.site, u)[key] for u in ("", getattr(self, "site_user", ""))):
        field.value = sites.login_settings(self.site, event.value.strip())[key]
    self.site_user = event.value.strip()

  @on(Input.Changed)
  def destination_changed(self, event):
    """(editing a tab: what applies follows the destination and the full settings, when the typing pauses)"""
    if self.cluster is None or not (event.input.id or "").startswith(("cluster-destination", "ssh-")):
      return
    if self.config_timer is not None:
      self.config_timer.stop()
    self.config_timer = self.set_timer(0.6, self.show_config)

  def show_config(self):
    """What applies to the destination: the blocks of ~/.ssh/config, and what ssh makes of it all (ssh -G)"""
    from ...cluster import sshsetup
    destination, settings = self.query_one("#cluster-destination", Input).value.strip(), self.settings()
    target = sshsetup.target(destination, settings)
    def work():
      blocks = sshsetup.config_blocks(target) if target else []
      conf, err = sshsetup.effective_config(destination, settings) if target else (None, "")
      return blocks, conf, err
    def show(result):
      if result is None or not self.is_mounted:
        return
      blocks, conf, err = result
      out = Text()
      if not target:
        out.append("no destination", style="dim")
      else:
        out.append(f"In ~/.ssh/config for {target}:\n", style="bold")
        if blocks:
          for path, line, text in blocks:
            out.append(f"{path.replace(os.path.expanduser('~'), '~', 1)}, line {line}:\n", style="dim")
            out.append(text + "\n")
        else:
          out.append("nothing (not an entry there: the host name as it is)\n", style="dim")
        if conf:
          one = lambda key: (conf.get(key) or [""])[0]
          keys = [p.replace(os.path.expanduser("~"), "~", 1) for p in conf.get("identityfile", [])
                  if os.path.exists(os.path.expanduser(p))]
          out.append("\nWhat ssh makes of it all (ssh -G):\n", style="bold")
          out.append(f"{one('user')}@{one('hostname')} port {one('port')}"
                     + (f", through {one('proxyjump')}" if one("proxyjump") not in ("", "none") else "")
                     + (f", through the command {one('proxycommand')}" if one("proxycommand") not in ("", "none") else "")
                     + (f"\nkeys: {', '.join(keys)}" if keys else "") + "\n")
        elif err:
          out.append(f"\nssh does not take it: {err}\n", style="red")
      self.query_one("#cluster-config", Static).update(out)
    in_background(self, work, show, group="ssh-config", connection=None)

  def settings(self):
    """The full settings filled in ({} if no host)"""
    from ...cluster import sshsetup
    value = lambda key: self.query_one(f"#ssh-{key}", Input).value.strip()
    raw = {key: value(key) for key in ("host", "user", "port", "jump")}
    raw["name"] = self.query_one("#cluster-name", Input).value.strip()
    raw["identities"] = [v for v in value("identities").split(",") if v.strip()]
    raw["options"] = [line for line in self.query_one("#ssh-options", TextArea).text.splitlines() if line.strip()]
    return sshsetup.clean(raw) or {}

  def result(self):
    settings = self.settings()
    pre_login = self.query_one("#ssh-pre-login", Input).value.strip()
    destination = self.query_one("#cluster-destination", Input).value.strip()
    name = self.query_one("#cluster-name", Input).value.strip()
    before = {"pre_login": pre_login, **({"pre_login_fallback": True} if self.query_one("#ssh-pre-login-fallback", Checkbox).value else {})} if pre_login else {}
    if settings:
      return {"ssh": settings, **before}
    return {"destination": destination, **before, **({"name": name} if name else {})} if pre_login or name else destination

  @on(Button.Pressed, "#cluster-check-button")
  def check_pressed(self, event):
    """The check in a window with a terminal, as the login goes (it may ask: a passphrase, a password, a code)"""
    event.stop()
    from ...cluster import sshsetup
    destination, settings = self.query_one("#cluster-destination", Input).value.strip(), self.settings()
    errors = sshsetup.option_errors(settings) if settings else []
    if errors:
      self.notify("; ".join(errors), severity="error")
      return
    if self.missing_refused("Test"):
      return
    if not destination and not settings:
      self.notify("this machine: nothing to check (no SSH)", timeout=4)
      return
    self.app.push_screen(SshCheckScreen(destination, settings, self.query_one("#ssh-pre-login", Input).value.strip(),
                                        self.query_one("#ssh-pre-login-fallback", Checkbox).value))

  @on(Button.Pressed, "#cluster-cancel")
  def cancel_pressed(self):
    self.dismiss(None)

  @on(Button.Pressed, "#cluster-confirm")
  @on(Input.Submitted, "#cluster-destination")
  @on(Input.Submitted, "#cluster-name")
  def add_pressed(self):
    from ...cluster import sshsetup
    settings = self.settings()
    errors = sshsetup.option_errors(settings) if settings else []
    name = self.query_one("#cluster-name", Input).value.strip()
    if name and any(c.name == name for c in self.app.clusters if c is not self.cluster):
      errors.append(f"a tab is called {name} already")
    if errors:
      self.notify("; ".join(errors), severity="error")
      return
    if self.missing_refused("Add"):
      return
    self.dismiss(self.result())

  def action_cancel(self):
    self.dismiss(None)

class UpdateScreen(ModalScreen):
  """A newer slurp is there: what changed, the command that updates it (as it was installed here), and to start slurp
  again; Skip this version (no reminder until a newer one)"""
  DEFAULT_CSS = """
  UpdateScreen { align: center middle; }
  #up-dialog { width: 100; max-width: 96%; height: auto; border: thick $success; background: $surface; padding: 1 2; }
  #up-title { text-style: bold; margin-bottom: 1; }
  .up-text { height: auto; margin-bottom: 1; }
  #up-command { height: auto; padding: 0 1; margin-bottom: 1; background: $boost; }
  #up-buttons { height: auto; align-horizontal: right; }
  #up-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close")]

  def __init__(self, version):
    super().__init__()
    from ... import update
    self.version = version
    self.how, self.command = update.how_to_update()

  def compose(self) -> ComposeResult:
    from ... import __version__, update
    with Vertical(id="up-dialog"):
      yield Static(f"slurp {self.version} is available (this is {__version__})", id="up-title", markup=False)
      yield Static(Text.assemble("What changed: ", (update.CHANGELOG, f"underline link {update.CHANGELOG}"),
                                 f" (and on PyPI: https://pypi.org/project/{update.PACKAGE}/)"), classes="up-text")
      yield Static(f"slurp is installed here with {self.how}. To update it, run in a terminal:", classes="up-text", markup=False)
      yield Static(self.command, id="up-command", markup=False)
      yield Static("Then start slurp again: quit it (q; this ends slurp in the background too, with its shells, links "
                   "and drives) and run slurp. Until then the running slurp stays as it is; the update changes nothing "
                   "in it.", classes="up-text", markup=False)
      with Horizontal(id="up-buttons"):
        yield Button("Skip this version", id="up-skip", compact=True, tooltip="no reminder until a newer one")
        yield Button("Close", id="up-close", compact=True)
        yield Button(icons_label("copy", "Copy the command"), id="up-copy", variant="primary", compact=True)

  def on_mount(self):
    self.query_one("#up-copy", Button).focus()

  def on_click(self, event):
    open_clicked_url(self.app, event)

  @on(Button.Pressed, "#up-copy")
  def copy_pressed(self, event):
    event.stop()
    copy_text(self.app, self.command, "the update command")

  @on(Button.Pressed, "#up-skip")
  def skip(self, event):
    event.stop()
    from ...config import update_config
    update_config(update_skip=self.version)
    self.app.new_version = None
    self.notify(f"no reminder of slurp {self.version} (a newer one is said again)", timeout=5)
    self.dismiss()

  @on(Button.Pressed, "#up-close")
  def action_close(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss()


def icons_label(icon, text):
  from ...resources import icons
  return icons.label(icon, text)


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

def copied_file():
  """Where Save to a file puts the text: in the cache directory ($XDG_CACHE_HOME, read when it is saved)"""
  return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "slurp", "copied.txt")

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
        yield Button("Plain text", id="copy-plain", variant="primary", compact=True)
        yield Button("Save to a file", id="copy-save", compact=True)
        yield Button("It arrived, don't ask again", id="copy-arrived", compact=True)
        yield Button("Close", id="copy-close", compact=True)

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
      path = copied_file()
      os.makedirs(os.path.dirname(path), exist_ok=True)
      with open(path, "w") as f:
        f.write(self.text + "\n")
      self.notify(f"saved {self.what} to {path}", timeout=8)
    except OSError as e:
      self.notify(f"cannot save to {path}: {e}", severity="error")

  @on(Button.Pressed, "#copy-arrived")
  def arrived(self):
    from ...config import update_config
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
  from ...config import load_config
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
