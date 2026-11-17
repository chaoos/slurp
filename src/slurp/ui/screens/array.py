"""The window of an array job: what the array is, and all its task ids by state (and exit code), to copy"""

import time
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Static, TabbedContent, TabPane
from .. import icons

import shlex
from rich.progress_bar import ProgressBar as RichProgressBar
from textual.coordinate import Coordinate
from ...cluster import slurm as sl
from ...state import log
from ...cluster.remote import in_background, run_cmd
from ...cluster.jobs import (
  annotate, array_overview, compress_array_ids, duration_text, fmt_time, is_time, Job, load_jobs, progress_for_job, task_group
)
from ..widgets import ClickTable, state_text
from .base import SplitScreen
from .dialogs import ConfirmScreen, copy_text
from .job import JobScreen

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

def node_hours_text(tasks):
  """The node hours all tasks of an array used together (nodes × elapsed time of each), bold, with the part of the
  running tasks (so far) and the tasks counted"""
  total, running, counted = 0.0, 0.0, 0
  for t in tasks.values():
    s = seconds(t.get("elapsed"))
    try:
      nodes = int(t.get("nodes") or 0)
    except ValueError:
      nodes = 0
    if not s or not nodes or t["state"] == "PENDING": # (a requeued task may still show its last run)
      continue
    counted += 1
    total += nodes * s / 3600
    if t["state"] in ("RUNNING", "COMPLETING", "CONFIGURING"):
      running += nodes * s / 3600
  text = Text(f"{total:.2f}", style="bold")
  text.append(f"   over {counted} task{'s' if counted != 1 else ''} that ran", style="dim")
  if running:
    text.append(f", {running:.2f} of them by running tasks (so far)", style="yellow")
  return text

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
  #arr-tabs { height: 1fr; }
  #arr-groups { height: 1fr; padding: 0 1; }
  #arr-tasks { height: 1fr; }
  .arr-group { height: auto; margin-top: 1; }
  .arr-head { height: auto; }
  .arr-title { width: 1fr; height: auto; padding-top: 1; }
  .arr-ids { height: auto; }
  """

  def __init__(self, array_id, job=None):
    super().__init__()
    self.array_id, self.job = str(array_id), job
    self.crumbs = ["Jobs", f"array {array_id}"]
    self.overview, self.groups, self.fetching = None, {}, False

  def compose_left(self):
    with Horizontal(id="arr-bar"):
      yield Static(id="arr-info")
      with Vertical(id="arr-buttons"):
        yield Button(icons.label("reload", "Reload"), id="arr-reload", variant="primary", compact=True)
        yield Button(icons.label("copy", "Copy all ids"), id="arr-copy-all", compact=True)
        yield Button(icons.label("cancel_job", "Cancel pending tasks"), id="arr-cancel-pending", variant="warning",
                     tooltip="scancel --state=PENDING: the tasks that have not started", compact=True)
        yield Button(icons.label("cancel_job", "Cancel the whole array"), id="arr-cancel", variant="error", tooltip="scancel of the array job", compact=True)
    with TabbedContent(id="arr-tabs"):
      with TabPane("By state", id="arr-tab-states"):
        yield VerticalScroll(id="arr-groups")
      with TabPane("Tasks", id="arr-tab-tasks"):
        yield ClickTable(id="arr-tasks", cursor_type="row", zebra_stripes=True)

  TASK_COLUMNS = [("task", "Task"), ("state", "State"), ("progress", "Progress"), ("exit", "Exit"), ("submit", "Submit"), ("wait", "Queue wait"),
                  ("start", "Start"), ("end", "End"), ("elapsed", "Run time"), ("node", "Node")]

  def on_mount(self):
    self.sub_title = f"array {self.array_id}"
    table = self.query_one("#arr-tasks", ClickTable)
    for key, label in self.TASK_COLUMNS:
      table.add_column(label, key=key, **({"width": 10} if key == "progress" else {}))
    self.action_reload()
    self.set_interval(5, self.refresh_task_progress) # (the job list reads the progress of running tasks)
    self.set_interval(1, lambda: self.show_info() if self.fetching else None) # the seconds asked so far

  def action_back(self):
    self.pop_back()

  @on(Button.Pressed, "#arr-reload")
  def action_reload(self):
    if self.fetching:
      return
    self.fetching, self.load_started, self.load_failed = True, time.monotonic(), False
    self.show_info()
    def loaded(overview):
      self.fetching = False
      if overview is None:
        self.load_failed = True # (the error was shown) the tasks as they were
      else:
        self.overview = overview
        self.show_groups()
        self.show_tasks()
      self.show_info()
    in_background(self, lambda: array_overview(self.array_id), loaded, group="array", name=f"loading array {self.array_id}")

  def show_info(self):
    """The information about the array; a mistake in it is shown there and logged, it does not end the app"""
    try:
      self.fill_info()
    except Exception as e:
      log.exception(f"array {self.array_id}: showing the information failed")
      grid = Table.grid(padding=(0, 2))
      grid.add_row(Text("error", style="red"), f"showing the information failed: {type(e).__name__}: {e}")
      self.info_table = grid
      self.query_one("#arr-info", Static).update(grid)

  def fill_info(self):
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
      grid.add_row("Node hours (total)", node_hours_text(tasks))
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
        if left and running and done:
          grid.add_row("Rest, roughly", f"{left} tasks × median / {running} at a time ≈ "
                       f"{duration_text(left * done[len(done) // 2] / running)}")
        elif left and running:
          grid.add_row("Rest, roughly", Text("no estimate until a task has completed", style="dim"))
      nodes = {t["node"] for t in tasks.values() if t["state"] == "RUNNING" and t.get("node")}
      if nodes:
        grid.add_row("Running on", f"{len(nodes)} node{'s' if len(nodes) != 1 else ''}: " + ", ".join(sorted(nodes)[:8])
                     + (" …" if len(nodes) > 8 else ""))
    for err in (self.overview or {}).get("errors", []):
      grid.add_row(Text("error", style="red"), err)
    if self.fetching:
      grid.add_row("", Text(f"asking Slurm … {time.monotonic() - self.load_started:.0f} s", style="yellow"))
    elif getattr(self, "load_failed", False):
      grid.add_row("", Text("loading failed (see the message); Reload tries again", style="red"))
    elif self.overview:
      grid.add_row("", Text(f"as of {datetime.now():%H:%M:%S}", style="dim"))
    self.info_table = grid
    self.query_one("#arr-info", Static).update(grid)

  sort_column, sort_reverse = "task", False

  def task_sort_key(self, column):
    """(index, task) -> what the task list is sorted by: times and durations by their value, not their text"""
    def wait(t):
      start, submit = epoch(t.get("start")), epoch(t.get("submit"))
      return start - submit if start and submit and t["state"] != "PENDING" else -1
    def progress(i, t):
      bar = self.task_progress(i, t)
      return bar.completed / bar.total if bar else -1
    keys = {
      "task": lambda i, t: i,
      "state": lambda i, t: (t["state"], t.get("reason") or ""),
      "progress": progress,
      "exit": lambda i, t: t.get("exit") or "",
      "submit": lambda i, t: epoch(t.get("submit")) or 0,
      "wait": lambda i, t: wait(t),
      "start": lambda i, t: epoch(t.get("start")) or 0,
      "end": lambda i, t: epoch(t.get("end")) or 0,
      "elapsed": lambda i, t: seconds(t.get("elapsed")) or -1 if is_time(t.get("start", "")) and t["state"] != "PENDING" else -1,
      "node": lambda i, t: t.get("node") or "",
    }
    key = keys.get(column, keys["task"])
    return lambda item: (key(*item), item[0]) # (equal values: by task)

  @on(DataTable.HeaderSelected, "#arr-tasks")
  def sort_tasks(self, event):
    event.stop()
    column = event.column_key.value
    if column == self.sort_column:
      self.sort_reverse = not self.sort_reverse
    else:
      self.sort_column, self.sort_reverse = column, column in ("submit", "wait", "start", "end", "elapsed")
    self.show_tasks()

  def mark_sort_column(self, table):
    """▲ (ascending) or ▼ (descending) after the label of the column the tasks are sorted by"""
    labels = dict(self.TASK_COLUMNS)
    for key, column in table.columns.items():
      label = labels.get(key.value, str(column.label)) + ((" ▼" if self.sort_reverse else " ▲") if key.value == self.sort_column else "")
      if str(column.label) != label:
        column.label = Text(label)
      if key.value != "progress":
        column.content_width = len(label) # (the rows added next widen it again as needed)
    table.refresh()

  def show_tasks(self):
    """Every task in a table, like the job list (sorted by the column clicked last): a click opens its job window"""
    table = self.query_one("#arr-tasks", ClickTable)
    tasks = (self.overview or {}).get("tasks", {})
    cursor = table.cursor_row
    current = None
    if table.row_count and 0 <= cursor < table.row_count:
      current = table.coordinate_to_cell_key(Coordinate(cursor, 0)).row_key.value
    table.clear()
    self.mark_sort_column(table)
    for i, t in sorted(tasks.items(), key=self.task_sort_key(self.sort_column), reverse=self.sort_reverse):
      start, submit = epoch(t.get("start")), epoch(t.get("submit"))
      wait = duration_text(start - submit) if start and submit and t["state"] != "PENDING" else ""
      state = t["state"] + (f" ({t['reason']})" if t.get("reason") and t["reason"] != "None" else "")
      table.add_row(f"{self.array_id}_{i}", state_text(state), self.task_progress(i, t), t.get("exit", ""), fmt_time(t.get("submit", "")), wait,
                    ("~" if t["state"] == "PENDING" else "") + fmt_time(t.get("start", "")), fmt_time(t.get("end", "")),
                    t.get("elapsed", "") if is_time(t.get("start", "")) and t["state"] != "PENDING" else "", t.get("node", ""),
                    key=str(i))
    if current is not None and current in table.rows: # (the same task, wherever it is now)
      table.move_cursor(row=table.get_row_index(current))
    elif tasks and 0 <= cursor < table.row_count:
      table.move_cursor(row=cursor)

  def task_progress(self, index, task):
    """The progress bar of a task, as in the job list: from its job there if it is listed (a running task: its n/N
    line), else from its state (completed: full, failed: empty)"""
    job = self.app.jobs.get(f"{self.array_id}_{index}")
    if job is not None:
      progress = progress_for_job(job)
    else:
      progress = progress_for_job(Job(dict(State=task["state"])))
    return RichProgressBar(total=max(progress.total, 1), completed=progress.current, width=8) if progress else ""

  def refresh_task_progress(self):
    """The bars of the running tasks again (their progress changes)"""
    table = self.query_one("#arr-tasks", ClickTable)
    for i, t in (self.overview or {}).get("tasks", {}).items():
      if t["state"] == "RUNNING" and str(i) in table.rows:
        table.update_cell(str(i), "progress", self.task_progress(i, t))

  @on(DataTable.RowSelected, "#arr-tasks")
  def task_selected(self, event):
    event.stop()
    self.query_one("#arr-tasks", ClickTable).take_clicked_column()
    self.action_open_task(event.row_key.value)

  def action_open_task(self, index):
    """The job window of a task (loaded from Slurm first if it is not in the job list)"""
    jid = f"{self.array_id}_{index}"
    job = self.app.jobs.get(jid)
    if job is not None:
      self.app.push_screen(JobScreen(job))
      return
    self.notify(f"loading job {jid} …", timeout=3)
    def loaded(jobs):
      job = next((j for j in jobs or [] if j.jid == jid), None)
      if job is None:
        self.notify(f"job {jid} is not known to Slurm", severity="warning")
      elif self.is_attached:
        self.app.push_screen(JobScreen(job))
    in_background(self, lambda: annotate(load_jobs(None, jids=[jid])), loaded, group="array-task")

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
                        Button(icons.label("copy", "Copy ids"), classes="arr-copy", compact=True, tooltip="copy the ids of these tasks"),
                        classes="arr-head")
      # each id a link to the job window of its task
      links = ", ".join(f"[@click=screen.open_task({i})]{i}[/]" for i in ids)
      body = Static(f"{links}\n[dim]= {compress_array_ids(ids)}[/]", classes="arr-ids")
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
