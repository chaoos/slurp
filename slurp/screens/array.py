"""The window of an array job: what the array is, and all its task ids by state (and exit code), to copy"""

from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from ..state import *
from ..remote import *
from ..jobs import *
from ..widgets import *
from .base import *
from .dialogs import ConfirmScreen, copy_text

def epoch(value):
  try:
    return datetime.fromisoformat(value).timestamp() if is_time(value) else None
  except ValueError:
    return None

def stats_text(values):
  """min · avg · max (and median) of durations in seconds"""
  values = sorted(values)
  avg = sum(values) / len(values)
  return (f"min {duration_text(values[0])} · avg {duration_text(avg)} · median {duration_text(values[len(values) // 2])} · "
          f"max {duration_text(values[-1])}  ({len(values)} task{'s' if len(values) != 1 else ''})")

def seconds(elapsed):
  """Slurm elapsed time ([d-]hh:mm:ss) in seconds, None if it is not one"""
  try:
    return sl.hours(elapsed) * 3600 if elapsed else None
  except ValueError:
    return None

GROUP_ORDER = ("RUNNING", "COMPLETING", "CONFIGURING", "PENDING", "SUSPENDED", "REQUEUED", "COMPLETED")

def group_rank(group):
  state = group.split()[0]
  return (GROUP_ORDER.index(state) if state in GROUP_ORDER else len(GROUP_ORDER), group)

class ArrayScreen(SplitScreen):
  """All tasks of an array job by state, with the exit code of ended tasks and the reason of pending ones: how many,
  their ids (comma separated, and as ranges) and a button to copy them"""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back"), Binding("r", "reload", "Reload")]
  DEFAULT_CSS = """
  #arr-bar { height: auto; padding: 0 1; }
  #arr-info { width: 1fr; height: auto; }
  #arr-buttons { width: auto; height: auto; }
  #arr-buttons Button { margin: 0 0 0 1; width: 26; }
  #arr-groups { height: 1fr; padding: 0 1; }
  .arr-group { height: auto; margin-top: 1; }
  .arr-head { height: auto; }
  .arr-title { width: 1fr; height: auto; padding-top: 1; }
  .arr-ids { height: auto; }
  """

  def __init__(self, array_id, job=None):
    super().__init__()
    self.array_id, self.job = str(array_id), job
    self.crumbs = ["Jobs", f"array {array_id}"]
    self.overview, self.groups, self.loading = None, {}, False

  def compose_left(self):
    with Horizontal(id="arr-bar"):
      yield Static(id="arr-info")
      with Vertical(id="arr-buttons"):
        yield Button("Reload", id="arr-reload", variant="primary")
        yield Button("Copy all ids", id="arr-copy-all")
        yield Button("Cancel pending tasks", id="arr-cancel-pending", variant="warning",
                     tooltip="scancel --state=PENDING: the tasks that have not started")
        yield Button("Cancel the whole array", id="arr-cancel", variant="error", tooltip="scancel of the array job")
    yield VerticalScroll(id="arr-groups")

  def on_mount(self):
    self.sub_title = f"array {self.array_id}"
    self.action_reload()

  def action_back(self):
    self.pop_back()

  @on(Button.Pressed, "#arr-reload")
  def action_reload(self):
    if self.loading:
      return
    self.loading = True
    self.show_info()
    def loaded(overview):
      self.loading = False
      if overview is not None:
        self.overview = overview
      self.show_info()
      self.show_groups()
    in_background(self, lambda: array_overview(self.array_id), loaded, group="array")

  def show_info(self):
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    rec = self.job.rec if self.job else {}
    grid.add_row("Array", Text(f"{self.array_id}  {rec.get('JobName', '')}", style="bold"))
    if rec:
      grid.add_row("User / account", f"{rec.get('User', '')} / {rec.get('Account', '')}   partition {rec.get('Partition', '')}")
      grid.add_row("Submitted with", rec.get("SubmitLine", ""))
    tasks = (self.overview or {}).get("tasks", {})
    if tasks:
      count = lambda states: sum(t["state"] in states for t in tasks.values())
      grid.add_row("Tasks", f"{len(tasks)}: {compress_array_ids(tasks)}")
      grid.add_row("", Text.assemble((f"{count(('COMPLETED',))} completed", "green"), ", ",
                                     (f"{count(('RUNNING', 'COMPLETING', 'CONFIGURING'))} running", "green"), ", ",
                                     (f"{count(('PENDING',))} pending", "blue"), ", ",
                                     (f"{len(tasks) - count(('COMPLETED', 'RUNNING', 'COMPLETING', 'CONFIGURING', 'PENDING'))} "
                                      "failed, cancelled or else", "red")))
      starts = [t["start"] for t in tasks.values() if is_time(t["start"])]
      ends = [t["end"] for t in tasks.values() if is_time(t["end"])]
      if starts:
        grid.add_row("First start / last end", f"{fmt_time(min(starts))} / {fmt_time(max(ends)) if ends else '-'}")
      now = datetime.now().timestamp()
      waits = [s - q for t in tasks.values() if t["state"] != "PENDING" and (s := epoch(t.get("start")))
               and (q := epoch(t.get("submit"))) and s <= now] # (the start of a pending task is only an estimate)
      if waits:
        grid.add_row("Queue wait (started)", stats_text(waits))
      pending = [now - q for t in tasks.values() if t["state"] == "PENDING" and (q := epoch(t.get("submit")))]
      if pending:
        grid.add_row("Waiting so far (pending)", stats_text(pending))
      ended = [s for t in tasks.values() if is_time(t.get("end")) and (s := seconds(t.get("elapsed"))) is not None]
      if ended:
        grid.add_row("Run time (ended)", stats_text(ended))
      done = sorted(s for t in tasks.values() if t["state"] == "COMPLETED" and (s := seconds(t.get("elapsed"))) is not None)
      if done:
        grid.add_row("Run time (completed)", stats_text(done))
      running_for = [now - s for t in tasks.values() if t["state"] == "RUNNING" and (s := epoch(t.get("start")))]
      if running_for:
        grid.add_row("Running for (running)", stats_text(running_for))
        left = count(("PENDING",)) + count(("RUNNING", "COMPLETING", "CONFIGURING"))
        running = count(("RUNNING", "COMPLETING", "CONFIGURING"))
        if left and running:
          grid.add_row("Rest, roughly", f"{left} tasks × median / {running} at a time ≈ "
                       f"{duration_text(left * done[len(done) // 2] / running)}")
      nodes = {t["node"] for t in tasks.values() if t["state"] == "RUNNING" and t.get("node")}
      if nodes:
        grid.add_row("Running on", f"{len(nodes)} node{'s' if len(nodes) != 1 else ''}: " + ", ".join(sorted(nodes)[:8])
                     + (" …" if len(nodes) > 8 else ""))
    for err in (self.overview or {}).get("errors", []):
      grid.add_row(Text("error", style="red"), err)
    if self.loading:
      grid.add_row("", Text("asking Slurm …", style="dim"))
    elif self.overview:
      grid.add_row("", Text(f"as of {datetime.now():%H:%M:%S}", style="dim"))
    self.info_table = grid
    self.query_one("#arr-info", Static).update(grid)

  def show_groups(self):
    tasks = (self.overview or {}).get("tasks", {})
    groups = {}
    for index, task in sorted(tasks.items()):
      groups.setdefault(task_group(task), []).append(index)
    self.groups = groups
    box = self.query_one("#arr-groups", VerticalScroll)
    box.remove_children()
    widgets = []
    for n, group in enumerate(sorted(groups, key=group_rank)):
      ids = groups[group]
      head = Horizontal(Static(Text.assemble(state_text(group), f"  {len(ids)} task{'s' if len(ids) != 1 else ''}"),
                               classes="arr-title"),
                        Button("Copy ids", id=f"arr-copy-{n}", compact=True, tooltip="copy the ids of these tasks"),
                        classes="arr-head")
      body = Static(Text.assemble(", ".join(map(str, ids)), (f"\n= {compress_array_ids(ids)}", "dim")), classes="arr-ids")
      widgets.append(Vertical(head, body, classes="arr-group"))
      head.group = group
    if not widgets:
      widgets.append(Static(Text("no tasks found", style="dim")))
    box.mount_all(widgets)

  def copy(self, ids, what):
    text = ",".join(map(str, ids))
    self.copied = text
    copy_text(self.app, text, f"{len(ids)} ids of array {self.array_id} ({what})")

  @on(Button.Pressed, "#arr-cancel, #arr-cancel-pending")
  def cancel_pressed(self, event):
    event.stop()
    pending = event.button.id == "arr-cancel-pending"
    tasks = (self.overview or {}).get("tasks", {})
    states = ("PENDING",) if pending else ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED")
    hit = sorted(i for i, t in tasks.items() if t["state"] in states)
    what = "the pending tasks" if pending else "the whole array"
    message = (f"Cancel {what} of job {self.array_id}"
               + (f": {len(hit)} task{'s' if len(hit) != 1 else ''} ({compress_array_ids(hit)[:200]})" if hit else "")
               + ("." if pending else ", running tasks included. Ended tasks are not affected."))
    args = ["scancel", *(["--state=PENDING"] if pending else []), self.array_id]
    self.app.push_screen(ConfirmScreen(f"Cancel {what}?", message + f"\n\n{shlex.join(args)}", "Cancel " + ("pending" if pending else "array")),
                         lambda ok: self.cancel(args, what) if ok else None)

  def cancel(self, args, what):
    def done(res):
      if res is None:
        return
      if res.returncode:
        self.notify(f"scancel failed: {(res.stderr or res.stdout).strip()[:300]}", severity="error", timeout=10)
      else:
        self.notify(f"cancelled {what} of job {self.array_id}", timeout=5)
      self.action_reload()
    in_background(self, lambda: run_cmd(args, timeout=60, retry=False), done, group="array-cancel")

  @on(Button.Pressed, "#arr-copy-all")
  def copy_all(self, event):
    event.stop()
    if self.overview:
      self.copy(sorted(self.overview["tasks"]), "all")

  @on(Button.Pressed, ".arr-head Button")
  def copy_group(self, event):
    event.stop()
    group = event.button.parent.group
    self.copy(self.groups[group], group)
