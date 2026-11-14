"""The script view: a batch script with the findings of the checks, Explain, Check environment, Edit, Test, Submit"""

import os
import re
import subprocess

from rich.text import Text
from textual import on
from textual.app import SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static, TextArea
from textual.worker import get_current_worker

from .. import checks
from ..remote import cluster_work, conn, fs_read, fs_stat, in_background, run_cmd, tab_name
from ..screens.base import SplitScreen, crumb_title
from ..screens.dialogs import ConfirmScreen
from ..screens.files import register_file_view
from .checks import FindingsView

def is_job_script(path):
  """(runs in a worker) a text file with #SBATCH lines, or #! and srun"""
  if os.path.splitext(path)[1].lower() not in ("", ".sh", ".sbatch", ".slurm", ".job", ".bash", ".run", ".batch"):
    return False
  try:
    head = fs_read(path, size=16384).decode("utf-8", errors="replace")
  except OSError:
    return False
  return "\0" not in head and checks.looks_like_job_script(head)

def open_job_script(app, crumbs, path, bases=(), env=None):
  """(runs in a worker)"""
  return lambda: app.open_window(ScriptScreen([*crumbs, os.path.basename(path)], path))

class ScriptScreen(SplitScreen):
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("r", "recheck", "Check again"),
    Binding("x", "explain", "Explain"),
    Binding("e", "edit", "Edit"),
  ]
  DEFAULT_CSS = """
  #script-bar { height: auto; }
  #script-bar Button { margin-right: 1; }
  #script-status { height: auto; padding: 0 1; }
  #script-text { height: 1fr; }
  #script-findings { height: 45%; border-top: solid $panel-lighten-2; padding: 0 1; }
  """

  def __init__(self, crumbs, path):
    super().__init__()
    self.crumbs, self.path = crumbs, path
    self.text = ""
    self.ctx = None
    self.findings = []
    self.explaining = False
    self.replay = None
    self.mtime = None

  def compose_left(self):
    with Horizontal(id="script-bar"):
      yield Button("Check again", id="sc-check", compact=True)
      yield Button("Explain", id="sc-explain", compact=True, tooltip="what Slurm makes of each #SBATCH line (x)")
      yield Button("Check environment", id="sc-env", compact=True,
                   tooltip="replay the module loads and exports on the login node and check the programs' shared "
                           "libraries (ldd); runs only setup lines, never the job's commands")
      yield Button("Edit", id="sc-edit", compact=True, tooltip="open it in your editor ($VISUAL / $EDITOR); checked again when saved (e)")
      yield Button("Test", id="sc-test", compact=True, tooltip="sbatch --test-only: would Slurm accept it, and when would it start?")
      yield Button("Submit…", id="sc-submit", compact=True, variant="primary")
      yield Button("Rule catalogue", id="sc-catalogue", compact=True)
    yield Static(Text("checking …", style="dim"), id="script-status")
    yield TextArea(id="script-text", read_only=True, show_line_numbers=True, soft_wrap=False)
    with VerticalScroll(id="script-findings"):
      yield FindingsView(id="sc-findings", line_buttons=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.check()
    self.set_interval(3, self.poll)

  # ---- checking
  def check(self):
    self.query_one("#sc-findings", FindingsView).show_text(Text("checking the script …", style="dim"))
    self.check_worker(self.replay)

  @cluster_work("check")
  def check_worker(self, replay):
    try:
      mtime = fs_stat(self.path).st_mtime
    except OSError:
      mtime = None
    findings, ctx = checks.check_file(self.path)
    if replay is not None:
      ctx.replay = replay
      findings = checks.run_rules(ctx)
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.checked, findings, ctx, mtime)

  def checked(self, findings, ctx, mtime):
    self.findings, self.ctx, self.mtime = findings, ctx, mtime
    self.text = ctx.script.text if ctx.script else ""
    self.show_text()
    view = self.query_one("#sc-findings", FindingsView)
    view.show(findings, "" if self.replay is None else "(with the environment check)")
    c = checks.counts(findings)
    status = Text(self.path, style="bold")
    status.append(f" · {checks.summary(findings) or 'no problems'}" + (" · Explain on" if self.explaining else ""))
    self.query_one("#script-status", Static).update(status)

  def show_text(self):
    area = self.query_one("#script-text", TextArea)
    text = checks.explained_text(self.ctx) if self.explaining and self.ctx else self.text
    pos = area.cursor_location
    area.load_text(text.replace("\r", ""))
    try:
      area.cursor_location = pos
    except Exception:
      pass

  def go_to_line(self, line):
    area = self.query_one("#script-text", TextArea)
    from textual.widgets.text_area import Selection
    row = max(line - 1, 0)
    length = len(area.document.get_line(row)) if row < area.document.line_count else 0
    area.selection = Selection((row, 0), (row, length))
    area.scroll_cursor_visible(center=True)
    area.focus()

  def poll(self):
    """Check again when the file changed (e.g. saved in an editor)"""
    if self.mtime is None:
      return
    def stat():
      try:
        return fs_stat(self.path).st_mtime
      except OSError:
        return None
    def compare(mtime):
      if mtime is not None and mtime != self.mtime:
        self.mtime = mtime
        self.check()
    in_background(self, stat, compare, group="poll")

  def action_recheck(self):
    self.check()

  @on(Button.Pressed, "#sc-check")
  def check_pressed(self, event):
    event.stop()
    self.check()

  @on(Button.Pressed, "#sc-explain")
  def action_explain(self, event=None):
    self.explaining = not self.explaining
    self.show_text()
    if self.ctx:
      self.checked(self.findings, self.ctx, self.mtime)

  @on(Button.Pressed, "#sc-catalogue")
  def catalogue_pressed(self, event):
    event.stop()
    self.app.action_rule_catalogue("")

  @on(Button.Pressed, "#sc-env")
  def env_pressed(self, event):
    event.stop()
    if self.ctx is None or self.ctx.script is None:
      return
    self.query_one("#sc-findings", FindingsView).show_text(Text("replaying the environment on the login node …", style="dim"))
    self.env_worker(self.ctx)

  @cluster_work("check")
  def env_worker(self, ctx):
    from ..environment import replay_environment
    replay = replay_environment(ctx)
    self.app.call_from_thread(self.env_done, replay)

  def env_done(self, replay):
    self.replay = replay
    self.check()

  # ---- edit, test, submit
  @on(Button.Pressed, "#sc-edit")
  def action_edit(self, event=None):
    args = ["sh", "-c", 'exec ${VISUAL:-${EDITOR:-vi}} "$1"', "sh", self.path]
    try:
      with self.app.suspend():
        subprocess.run(conn().command_argv(args, interactive=True) if conn() else args)
    except SuspendNotSupported:
      self.notify("cannot suspend the terminal here (in the browser): edit the file elsewhere, it is checked again when saved",
                  severity="warning")
      return
    self.check()

  @on(Button.Pressed, "#sc-test")
  def test_pressed(self, event):
    event.stop()
    self.query_one("#script-status", Static).update(Text("sbatch --test-only …", style="dim"))
    def test():
      return run_cmd(["sbatch", "--test-only", self.path], timeout=60, cwd=os.path.dirname(self.path), retry=False)
    def done(res):
      text = ((res.stderr or "") + (res.stdout or "")).strip()
      ok = res.returncode == 0
      m = re.search(r"to start at (\S+)", text)
      msg = (f"Slurm accepts it; it would start at {m.group(1).replace('T', ' ')}" if ok and m else
             "Slurm accepts it" if ok else f"Slurm rejects it: {text[:300]}")
      self.notify(msg + ("" if ok and m else ""), title="sbatch --test-only", severity="information" if ok else "error", timeout=12)
      status = Text(self.path, style="bold")
      status.append(f" · {msg}", style="green" if ok else "red")
      self.query_one("#script-status", Static).update(status)
    in_background(self, test, done, group="test")

  @on(Button.Pressed, "#sc-submit")
  def submit_pressed(self, event):
    event.stop()
    c = checks.counts(self.findings)
    problems = (f"The checks found {c[checks.ERROR]} errors and {c[checks.WARNING]} warnings. "
                if c[checks.ERROR] or c[checks.WARNING] else "The checks found no errors or warnings. ")
    self.app.push_screen(ConfirmScreen(f"Submit {os.path.basename(self.path)}?",
                                       problems + f"sbatch {self.path}, run in {os.path.dirname(self.path)}.",
                                       "Submit", variant="error" if c[checks.ERROR] else "primary"),
                         lambda ok: self.submit() if ok else None)

  def submit(self):
    cluster = self.app.cluster
    def run():
      return run_cmd(["sbatch", self.path], timeout=60, cwd=os.path.dirname(self.path), retry=False)
    def done(res):
      if res.returncode:
        self.notify(f"sbatch failed: {(res.stderr or res.stdout).strip()[:300]}", severity="error", timeout=12)
        return
      jid = (re.findall(r"\d+", res.stdout) or ["?"])[-1]
      self.notify(f"submitted job {jid}", timeout=6)
      if cluster is self.app.cluster:
        self.app.reload_cluster(cluster)
    in_background(self, run, done, group="submit")

  def action_back(self):
    self.pop_back()

register_file_view("Job script", is_job_script, open_job_script)
