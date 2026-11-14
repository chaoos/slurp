"""The script view: a batch script with the findings of the checks, Explain, Check environment, Edit (in place, checked
as you type), Test, Submit"""

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
    Binding("escape", "back", "Back", priority=True), # (also from the editor, which would keep Esc)
    Binding("r", "recheck", "Check again"),
    Binding("x", "explain", "Explain"),
    Binding("e", "edit", "Edit"),
    Binding("ctrl+s", "save", "Save", priority=True),
  ]
  DEFAULT_CSS = """
  #script-bar { height: auto; }
  #script-bar Button { margin-right: 1; }
  #script-status { height: auto; padding: 0 1; }
  #script-text { height: 1fr; }
  #script-findings { height: 45%; border-top: solid $panel-lighten-2; padding: 0 1; }
  ScriptScreen.-editing #script-text { border: round $accent; }
  ScriptScreen #sc-save, ScriptScreen #sc-revert, ScriptScreen #sc-done { display: none; }
  ScriptScreen.-editing #sc-save, ScriptScreen.-editing #sc-revert, ScriptScreen.-editing #sc-done { display: block; }
  ScriptScreen.-editing #sc-edit, ScriptScreen.-editing #sc-explain { display: none; }
  """
  LIVE_DELAY = 0.6 # seconds after the last change until the script is checked as typed

  def __init__(self, crumbs, path):
    super().__init__()
    self.crumbs, self.path = crumbs, path
    self.text = ""
    self.ctx = None
    self.findings = []
    self.explaining = False
    self.replay = None
    self.mtime = None
    self.editing = False
    self.saved_text = ""   # the text as it is in the file (while editing)
    self.live_timer = None

  def compose_left(self):
    with Horizontal(id="script-bar"):
      yield Button("Check again", id="sc-check", compact=True)
      yield Button("Explain", id="sc-explain", compact=True, tooltip="what Slurm makes of each #SBATCH line (x)")
      yield Button("Check environment", id="sc-env", compact=True,
                   tooltip="replay the module loads and exports on the login node and check the programs' shared "
                           "libraries (ldd); runs only setup lines, never the job's commands")
      yield Button("Edit", id="sc-edit", compact=True, tooltip="edit it here, checked as you type; Ctrl+S saves (e)")
      yield Button("Save", id="sc-save", compact=True, variant="success", tooltip="write it to the file and check it fully (Ctrl+S)")
      yield Button("Revert", id="sc-revert", compact=True, tooltip="back to the file as it is saved")
      yield Button("Done", id="sc-done", compact=True, tooltip="stop editing (Esc)")
      yield Button("Edit outside", id="sc-edit-outside", compact=True,
                   tooltip="open it in your own editor ($VISUAL / $EDITOR, in the terminal); checked again when saved")
      yield Button("Test", id="sc-test", compact=True, tooltip="sbatch --test-only: would Slurm accept it, and when would it start?")
      yield Button("Fixed copy…", id="sc-fix", compact=True, tooltip="the fixes that are safe to make, in a copy of the script")
      yield Button("Submit…", id="sc-submit", compact=True, variant="primary")
      yield Button("Rule catalogue", id="sc-catalogue", compact=True)
    yield Static(Text("checking …", style="dim"), id="script-status")
    yield TextArea(id="script-text", read_only=True, show_line_numbers=True, soft_wrap=False, language="bash",
                   tab_behavior="indent")
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
    if self.editing:
      self.saved_text = self.text
    else:
      self.show_text()
    view = self.query_one("#sc-findings", FindingsView)
    view.show(findings, "" if self.replay is None else "(with the environment check)")
    c = checks.counts(findings)
    self.show_status()

  def show_status(self):
    findings = self.findings
    status = Text(self.path, style="bold")
    status.append(f" · {checks.summary(findings) or 'no problems'}" + (" · Explain on" if self.explaining else ""))
    if self.editing:
      dirty = self.current_text() != self.saved_text
      status.append(" · editing" + (" · modified" if dirty else ""), style="bold yellow" if dirty else "bold")
      status.append(" · Ctrl+S saves, Esc stops editing, Ctrl+Z undoes", style="dim")
    self.query_one("#script-status", Static).update(status)

  def current_text(self):
    return self.query_one("#script-text", TextArea).text if self.editing else self.text

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
        if self.editing: # (not under the user's edits: Save asks before it overwrites)
          if not getattr(self, "told_changed", False):
            self.told_changed = True
            self.notify("the file was changed on disk while you edit it: Save asks before it overwrites, Revert loads it",
                        severity="warning", timeout=8)
          return
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
    if self.editing:
      return
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

  # ---- edit: in place, checked as you type
  @on(Button.Pressed, "#sc-edit")
  def action_edit(self, event=None):
    if self.editing or self.ctx is None:
      return
    self.editing, self.explaining = True, False
    self.saved_text = self.text
    self.told_changed = False
    area = self.query_one("#script-text", TextArea)
    pos = area.cursor_location
    area.load_text(self.text.replace("\r", ""))
    try:
      area.cursor_location = pos
    except Exception:
      pass
    area.read_only = False
    self.add_class("-editing")
    area.focus()
    self.show_status()

  @on(TextArea.Changed, "#script-text")
  def text_changed(self, event):
    if not self.editing:
      return
    self.show_status()
    if self.live_timer is not None:
      self.live_timer.stop()
    self.live_timer = self.set_timer(self.LIVE_DELAY, self.live_check)

  def live_check(self):
    """The checks of the script as typed: the text itself (files on the cluster and the quota are checked on Save)"""
    self.live_timer = None
    if not self.editing or self.ctx is None:
      return
    text = self.current_text()
    ctx = checks.make_context(text, self.path, facts=self.ctx.facts)
    ctx.script_dir = os.path.dirname(self.path)
    ctx.quota = self.ctx.quota # (the quota as last asked)
    self.findings = checks.run_rules(ctx)
    self.query_one("#sc-findings", FindingsView).show(self.findings, "(as you type; checking the paths on the cluster …)")
    self.show_status()
    self.probe_worker(text, ctx)

  @cluster_work("live-probe")
  def probe_worker(self, text, ctx):
    """The paths, directories and programs of the script as typed, on the cluster (one round trip)"""
    ctx.probes = checks.run_probe([f"{k}:{p}" for k, p in checks.interesting_paths(ctx)])
    findings = checks.run_rules(ctx)
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.probed, text, findings)

  def probed(self, text, findings):
    if not self.editing or self.current_text() != text: # (typed on meanwhile: a newer check follows)
      return
    self.findings = findings
    self.query_one("#sc-findings", FindingsView).show(findings, "(as you type, with the paths on the cluster)")
    self.show_status()

  @on(Button.Pressed, "#sc-save")
  def action_save(self, event=None):
    if not self.editing:
      return
    text = self.current_text()
    expected = self.mtime
    def stat():
      try:
        return fs_stat(self.path).st_mtime
      except OSError:
        return None
    def compare(mtime):
      if expected is not None and mtime is not None and mtime != expected:
        self.app.push_screen(ConfirmScreen("The file changed on disk", f"{self.path} was changed by something else since "
                                           "you opened it. Overwrite it with your version?", "Overwrite"),
                             lambda ok: self.write(text) if ok else None)
      else:
        self.write(text)
    in_background(self, stat, compare, group="save")

  def write(self, text):
    def work():
      from ..remote import fs_write
      fs_write(self.path, text)
      try:
        return fs_stat(self.path).st_mtime
      except OSError:
        return None
    def written(mtime):
      self.saved_text = self.text = text
      self.mtime = mtime
      self.told_changed = False
      self.notify(f"saved {os.path.basename(self.path)}", timeout=3)
      self.show_status()
      self.check() # (the full check: files on the cluster, quota)
    in_background(self, work, written, group="save")

  @on(Button.Pressed, "#sc-revert")
  def revert_pressed(self, event):
    event.stop()
    def answer(ok):
      if ok:
        self.stop_editing()
        self.check()
    if self.current_text() == self.saved_text:
      answer(True)
    else:
      self.app.push_screen(ConfirmScreen("Revert?", "Your changes are lost; the script is loaded from the file again.",
                                         "Revert"), answer)

  @on(Button.Pressed, "#sc-done")
  def done_pressed(self, event):
    event.stop()
    self.leave_editing(lambda: None)

  def leave_editing(self, then):
    """Stop editing (asking first if there are unsaved changes), then run then()"""
    if not self.editing:
      then()
      return
    if self.current_text() == self.saved_text:
      self.stop_editing()
      then()
      return
    def answer(ok):
      if ok:
        self.stop_editing()
        self.check()
        then()
    self.app.push_screen(ConfirmScreen("Unsaved changes", "Leave without saving? Your changes are lost (Ctrl+S saves).",
                                       "Discard"), answer)

  def stop_editing(self):
    self.editing = False
    if self.live_timer is not None:
      self.live_timer.stop()
      self.live_timer = None
    area = self.query_one("#script-text", TextArea)
    area.read_only = True
    self.remove_class("-editing")
    self.show_text()
    self.show_status()

  @on(Button.Pressed, "#sc-edit-outside")
  def edit_outside(self, event=None):
    if self.editing and self.current_text() != self.saved_text:
      self.notify("save or revert your changes first", severity="warning")
      return
    if self.editing:
      self.stop_editing()
    args = ["sh", "-c", 'exec ${VISUAL:-${EDITOR:-vi}} "$1"', "sh", self.path]
    try:
      with self.app.suspend():
        subprocess.run(conn().command_argv(args, interactive=True) if conn() else args)
    except SuspendNotSupported:
      self.notify("cannot suspend the terminal here (in the browser): use Edit, or edit the file elsewhere; it is "
                  "checked again when saved", severity="warning")
      return
    self.check()

  def unsaved(self):
    return self.editing and self.current_text() != self.saved_text

  # ---- test, submit
  @on(Button.Pressed, "#sc-test")
  def test_pressed(self, event):
    event.stop()
    if self.unsaved():
      self.notify("save first (Ctrl+S): Test uses the file", severity="warning")
      return
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
    if self.unsaved():
      self.notify("save first (Ctrl+S): Submit uses the file", severity="warning")
      return
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

  def recheck(self):
    self.check()

  def mute_in_script(self, rule_id):
    """# slurp: ignore <id> after the #! line (or added to an existing ignore comment); while editing, in the editor"""
    lines = self.current_text().split("\n")
    for i, l in enumerate(lines):
      m = re.match(r"(\s*#\s*slurp:\s*ignore\s+)(.*)", l)
      if m:
        lines[i] = l.rstrip() + f" {rule_id}"
        break
    else:
      lines.insert(1 if lines and lines[0].startswith("#!") else 0, f"# slurp: ignore {rule_id}")
    text = "\n".join(lines)
    if self.editing:
      area = self.query_one("#script-text", TextArea)
      pos = area.cursor_location
      area.load_text(text)
      try:
        area.cursor_location = (pos[0] + 1, pos[1]) if text.count("\n") > self.saved_text.count("\n") else pos
      except Exception:
        pass
      return
    def write():
      from ..remote import fs_write
      fs_write(self.path, text)
      return True
    in_background(self, write, lambda ok: self.check() if ok else None, group="write")

  @on(Button.Pressed, "#sc-fix")
  def fix_pressed(self, event):
    event.stop()
    if self.ctx is None or self.ctx.script is None:
      return
    from ..fixes import apply_fixes, fixed_name
    from .checks import FixPreviewScreen
    new, done = apply_fixes(self.current_text(), self.findings)
    if not done:
      self.notify("none of the findings can be fixed automatically: see How to fix in each", timeout=6)
      return
    target = fixed_name(self.path)
    def answer(ok):
      if ok:
        write_fixed_copy(self, target, new)
    self.app.push_screen(FixPreviewScreen(self.path, target, self.current_text(), new, done), answer)

  def action_back(self):
    """Esc: while editing, stop editing (asking about unsaved changes); else back"""
    if self.editing:
      self.leave_editing(lambda: None)
      return
    self.pop_back()

def write_fixed_copy(screen, target, text):
  """Write the copy (target, or target with a number if it exists) and open it in the script view"""
  from ..remote import fs_write, fs_exists
  def write():
    path, n = target, 1
    while fs_exists(path):
      n += 1
      stem, ext = os.path.splitext(target)
      path = f"{stem}{n}{ext}"
    fs_write(path, text, exclusive=True)
    return path
  def written(path):
    if path:
      screen.notify(f"wrote {path}", timeout=5)
      from ..screens.files import open_path
      open_path(screen.app, ["Jobs"], path)
  in_background(screen, write, written, group="write")

register_file_view("Job script", is_job_script, open_job_script)
