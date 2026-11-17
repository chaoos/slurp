"""Categories of jobs (see cluster/categories.py): the dialog that puts the marked jobs in one, and the window that browses
them as a tree, with the numbers and the jobs of the one selected"""

import time

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Markdown, Static, TextArea, Tree
from .. import icons

from ...cluster import categories as cat
from ...cluster.remote import in_background
from ...cluster.jobs import fmt_time, is_time, load_jobs
from ..widgets import ClickTable, state_text
from .base import SplitScreen
from .dialogs import ConfirmScreen

def category_text(path, style=""):
  """a/b/c with the categories it is in dimmed"""
  if not path:
    return Text("")
  up = cat.parent(path)
  return Text.assemble((up + "/", "dim") if up else "", (cat.leaf(path), style))

def fill_tree(tree, store, counts=True):
  """The categories as the nodes of a tree (data: the path; the root: "" for all of them)"""
  tree.clear()
  tree.root.data = ""
  def add(node, path):
    for child in store.children(path):
      n = store.count(child)
      label = Text.assemble(cat.leaf(child), (f"  {n}", "dim") if counts and n else "")
      sub = node.add(label, data=child, expand=True)
      add(sub, child)
  add(tree.root, "")
  tree.root.expand()

def find_node(tree, path):
  stack = [tree.root]
  while stack:
    node = stack.pop()
    if node.data == path:
      return node
    stack.extend(node.children)
  return None

def change_categories(app, cluster, op, done, what="changing the categories"):
  """A change ([name, args...], see categories.OPS) on the file of the tab's cluster (in a worker), then
  done(store, result) on the UI thread. The cluster cannot be reached: done here (the categories kept with the job
  list) and remembered, the next load writes it to the cluster."""
  import threading
  from ...cluster import jobcache
  if app.offline(cluster) and cluster.categories is None: # (no connection: the commands would run on this machine)
    app.notify("the cluster cannot be reached, and no categories of it are kept here yet", severity="error")
    return
  if app.offline(cluster):
    store = cat.Store(cluster.categories.cluster, cluster.categories.to_data())
    try:
      result = cat.apply_op(store, op)
    except ValueError as e:
      app.notify(str(e), severity="error")
      return
    cluster.categories = store
    cluster.pending_categories.append(list(op))
    threading.Thread(target=jobcache.save_pending, args=(cluster,), daemon=True, name="keeping the categories").start()
    app.notify("the cluster cannot be reached: the change is kept here and written to the cluster with the next load",
               title=cluster.name, severity="warning", timeout=8)
    done(store, result)
    return
  def work():
    if cluster.slurm_cluster is None:
      cluster.slurm_cluster = cat.cluster_name()
    return cat.change(cluster.slurm_cluster, lambda st: cat.apply_op(st, op))
  def changed(res):
    if res is None:
      return
    cluster.categories = res[0]
    threading.Thread(target=jobcache.save_pending, args=(cluster,), daemon=True, name="keeping the categories").start()
    done(*res)
  in_background(app, work, changed, group="categories", name=what, connection=cluster.connection)

class NameScreen(ModalScreen[str | None]):
  """The name (path) of a category: a new one, or a rename"""
  DEFAULT_CSS = """
  NameScreen { align: center middle; }
  #nm-dialog { width: 76; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #nm-title { text-style: bold; margin-bottom: 1; }
  #nm-note { color: $text-muted; height: auto; margin-top: 1; }
  #nm-error { color: $error; height: auto; }
  #nm-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #nm-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, title, value, note, ok_label):
    super().__init__()
    self.title_text, self.value, self.note, self.ok_label = title, value, note, ok_label

  def compose(self) -> ComposeResult:
    with Vertical(id="nm-dialog"):
      yield Static(self.title_text, id="nm-title", markup=False)
      yield Input(self.value, id="nm-path", placeholder="project X/production runs")
      yield Static(self.note, id="nm-note", markup=False)
      yield Static(id="nm-error")
      with Horizontal(id="nm-buttons"):
        yield Button("Cancel", id="nm-cancel", compact=True)
        yield Button(self.ok_label, id="nm-ok", variant="primary", compact=True)

  def on_mount(self):
    self.query_one("#nm-path", Input).focus()

  @on(Button.Pressed, "#nm-ok")
  @on(Input.Submitted, "#nm-path")
  def ok(self, event):
    event.stop()
    try:
      path = cat.normalize(self.query_one("#nm-path", Input).value)
    except ValueError as e:
      self.query_one("#nm-error", Static).update(str(e))
      return
    self.dismiss(path)

  @on(Button.Pressed, "#nm-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

class NotesScreen(ModalScreen[str | None]):
  """The notes of a category, in Markdown; dismissed with the text ("": none) or None (cancelled)"""
  DEFAULT_CSS = """
  NotesScreen { align: center middle; }
  #nt-dialog { width: 100; max-width: 95%; height: 32; max-height: 95%; border: thick $accent; background: $surface; padding: 1 2; }
  #nt-title { text-style: bold; height: auto; }
  #nt-note { color: $text-muted; height: auto; margin-bottom: 1; }
  #nt-text { height: 1fr; }
  #nt-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #nt-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "save", "Save", priority=True)]

  def __init__(self, path, text):
    super().__init__()
    self.path, self.text = path, text

  def compose(self) -> ComposeResult:
    with Vertical(id="nt-dialog"):
      yield Static(f"Notes of {self.path}", id="nt-title", markup=False)
      yield Static("Markdown: # headings, **bold**, - lists, `code`, [links](https://…). Shown in the categories window. "
                   "Empty: no notes. Ctrl+S saves.", id="nt-note", markup=False)
      yield TextArea(self.text, id="nt-text", language="markdown", soft_wrap=True, show_line_numbers=False)
      with Horizontal(id="nt-buttons"):
        yield Button("Cancel", id="nt-cancel", compact=True)
        yield Button("Save", id="nt-ok", variant="primary", compact=True)

  def on_mount(self):
    self.query_one("#nt-text", TextArea).focus()

  @on(Button.Pressed, "#nt-ok")
  def action_save(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(self.query_one("#nt-text", TextArea).text)

  @on(Button.Pressed, "#nt-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

class CategorizeScreen(ModalScreen[tuple | None]):
  """Put jobs in a category: one of the tree, or a new one typed in; or take them out of theirs.
  Dismissed with ("assign", path), ("remove", None) or None."""
  DEFAULT_CSS = """
  CategorizeScreen { align: center middle; }
  #cz-dialog { width: 90; max-width: 96%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #cz-title { text-style: bold; }
  #cz-now { color: $text-muted; height: auto; margin-bottom: 1; }
  #cz-tree { height: 14; border: round $panel; margin-bottom: 1; }
  #cz-note { color: $text-muted; height: auto; }
  #cz-error { color: $error; height: auto; }
  #cz-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #cz-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, store, jids, describe):
    super().__init__()
    self.store, self.jids, self.describe = store, list(jids), describe

  def compose(self) -> ComposeResult:
    now = {}
    for jid in self.jids:
      c = self.store.category_of(jid)
      now[c] = now.get(c, 0) + 1
    self.current = [c for c in now if c]
    with Vertical(id="cz-dialog"):
      yield Static(f"Categorize {self.describe}", id="cz-title", markup=False)
      yield Static("now in: " + ", ".join(f"{c or 'no category'}" + (f" ({n})" if len(now) > 1 else "") for c, n in sorted(now.items())),
                   id="cz-now", markup=False)
      tree = Tree("all categories", id="cz-tree")
      tree.show_root = False
      yield tree
      yield Input(self.current[0] if len(self.current) == 1 else "", id="cz-path", placeholder="a category: pick one above, or type a new one (project X/production runs)")
      yield Static("A new name makes the category (a/b: b in a). An array goes in as a whole, with all its tasks.", id="cz-note")
      yield Static(id="cz-error")
      with Horizontal(id="cz-buttons"):
        yield Button("Cancel", id="cz-cancel", compact=True)
        yield Button("Take out of its category", id="cz-remove", variant="warning", disabled=not self.current, compact=True)
        yield Button("Put in the category", id="cz-ok", variant="primary", compact=True)

  def on_mount(self):
    tree = self.query_one("#cz-tree", Tree)
    fill_tree(tree, self.store)
    if not self.store.paths:
      tree.display = False
    if len(self.current) == 1 and (node := find_node(tree, self.current[0])) is not None:
      tree.move_cursor(node)
    self.query_one("#cz-path", Input).focus()

  @on(Tree.NodeHighlighted, "#cz-tree")
  @on(Tree.NodeSelected, "#cz-tree")
  def picked(self, event):
    if event.node.data:
      self.query_one("#cz-path", Input).value = event.node.data

  @on(Button.Pressed, "#cz-ok")
  @on(Input.Submitted, "#cz-path")
  def ok(self, event):
    event.stop()
    try:
      path = cat.normalize(self.query_one("#cz-path", Input).value)
    except ValueError as e:
      self.query_one("#cz-error", Static).update(str(e))
      return
    self.dismiss(("assign", path))

  @on(Button.Pressed, "#cz-remove")
  def take_out(self, event):
    event.stop()
    self.dismiss(("remove", None))

  @on(Button.Pressed, "#cz-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

NONE = "\x00none" # (the tree's "not categorized": the jobs of the job list that are in no category)

def hours_text(h):
  return f"{h:,.1f}" if h < 10_000 else f"{h:,.0f}"

class CategoriesScreen(SplitScreen):
  """The categories of the tab's cluster as a tree; the one selected (with the ones in it): its numbers and its jobs"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("n", "new", "New category"),
    Binding("e", "rename", "Rename / move"),
    Binding("delete", "delete", "Delete category"),
    Binding("x", "take_out", "Take job out"),
    Binding("c", "categorize", "Categorize job"),
    Binding("l", "in_list", "In the job list"),
    Binding("r", "reload", "Reload"),
    Binding("o", "notes", "Notes"),
  ]
  DEFAULT_CSS = """
  #cat-body { height: 1fr; }
  #cat-side { width: 42; height: 1fr; }
  #cat-tree { height: 1fr; border: round $panel; }
  #cat-side-buttons { height: auto; }
  #cat-side-buttons Button { width: 1fr; margin: 0 1 0 0; min-width: 8; }
  #cat-main { width: 1fr; height: 1fr; padding: 0 0 0 1; }
  #cat-info { height: auto; }
  #cat-notes-box { height: auto; max-height: 40%; border: round $panel; border-title-color: $text-muted; padding: 0 1; }
  #cat-notes-box.-empty { display: none; }
  #cat-notes { margin: 0; padding: 0; }
  #cat-main-buttons { height: auto; margin: 1 0 0 0; }
  #cat-main-buttons Button { margin: 0 1 0 0; }
  #cat-jobs { height: 1fr; }
  """
  COLUMNS = [("job", "Job"), ("name", "Name"), ("category", "Category"), ("state", "State"), ("nodes", "Nodes"),
             ("elapsed", "Elapsed"), ("node_h", "Node h"), ("gpu_h", "GPU h"), ("submit", "Submit"), ("end", "End")]

  def __init__(self, path=""):
    super().__init__()
    self.crumbs = ["Jobs", "categories"]
    self.path = path           # the category shown ("": all of them)
    self.known = {}            # job id -> [Job] (an array: its tasks), as Slurm reported them
    self.asked = set()         # job ids asked from Slurm (also those it no longer knows)
    self.fetching = False
    self.loose = {}            # "not categorized" shown: its jobs, {job or array id: [jobs]}

  @property
  def cluster(self):
    return self.app.cluster

  @property
  def store(self):
    return self.cluster.categories or cat.Store(self.cluster.slurm_cluster or "default")

  def compose_left(self):
    with Horizontal(id="cat-body"):
      with Vertical(id="cat-side"):
        tree = Tree("all categories", id="cat-tree")
        yield tree
        with Horizontal(id="cat-side-buttons"):
          yield Button("New", id="cat-new", tooltip="a new category in the one selected (n)", compact=True)
          yield Button("Rename", id="cat-rename", tooltip="rename or move it, with what is in it (e)", compact=True)
          yield Button("Delete", id="cat-delete", variant="error", tooltip="its jobs go to the category it is in (Del)", compact=True)
      with Vertical(id="cat-main"):
        yield Static(id="cat-info")
        with VerticalScroll(id="cat-notes-box", classes="-empty"):
          yield Markdown(id="cat-notes", open_links=False)
        with Horizontal(id="cat-main-buttons"):
          yield Button(icons.label("reload", "Reload"), id="cat-reload", variant="primary", tooltip="ask Slurm again (r)", compact=True)
          yield Button("In the job list", id="cat-list", tooltip="the job list filtered to this category (l)", compact=True)
          yield Button("Categorize…", id="cat-categorize", tooltip="the job selected in a category, another or a new one (c)", compact=True)
          yield Button("Take job out", id="cat-take-out", variant="warning", tooltip="the job selected leaves its category (x)", compact=True)
          yield Button("Notes…", id="cat-notes-edit", tooltip="notes of the category, in Markdown (o)", compact=True)
        yield ClickTable(id="cat-jobs", cursor_type="row", zebra_stripes=True)

  def on_mount(self):
    self.sub_title = "categories"
    table = self.query_one("#cat-jobs", ClickTable)
    for key, label in self.COLUMNS:
      table.add_column(label, key=key)
    self.refresh_tree()
    self.show_category()

  def on_screen_resume(self):
    self.refresh_tree()
    self.show_category()

  def refresh_tree(self, select=None):
    tree = self.query_one("#cat-tree", Tree)
    store = self.store
    tree.root.set_label(Text.assemble("all categories", (f"  {len(store.jobs)}", "dim") if store.jobs else ""))
    fill_tree(tree, store)
    loose = self.uncategorized()
    tree.root.add_leaf(Text.assemble(("not categorized", "italic"), (f"  {len(loose)}", "dim") if loose else ""), data=NONE)
    path = self.path if select is None else select
    if path and path != NONE and path not in store.paths:
      path = cat.parent(path) if cat.parent(path) in store.paths else ""
    self.path = path
    # (the new nodes have their lines only once the tree is drawn: the cursor goes there then; until then the tree's
    # own move to its first line must not change what is shown)
    self.refilling = True
    def restore():
      tree.last_line # (builds the tree's lines, which it does only when drawn: a subtab shown again is not yet)
      self.refilling = False
      tree.move_cursor(find_node(tree, self.path) or tree.root)
    self.call_after_refresh(restore)

  refilling = False

  @on(Tree.NodeHighlighted, "#cat-tree")
  def highlighted(self, event):
    # (a late one from the refill, for a line the cursor has left: not a choice)
    if self.refilling or event.node is not self.query_one("#cat-tree", Tree).cursor_node:
      return
    path = event.node.data or ""
    if path != self.path:
      self.path = path
      self.show_category()

  def uncategorized(self):
    """The jobs of the job list (queued, and those finished since …) in no category: {job id or array id: [jobs]}"""
    store = self.store
    out = {}
    for job in self.cluster.jobs.values():
      if not store.category_of(job.jid):
        out.setdefault(cat.array_of(job.jid) if "_" in job.jid else job.jid, []).append(job)
    return dict(sorted(out.items(), key=lambda kv: [int(x) for x in kv[0].split("_") if x.isdigit()], reverse=True))

  # ---- the category shown
  def show_category(self):
    store = self.store
    self.show_notes()
    if self.path == NONE:
      self.loose = self.uncategorized()
      ids = list(self.loose)
      self.show_info(ids)
      self.show_jobs(ids)
      return
    ids = store.job_ids(self.path)
    missing = [j for j in ids if j not in self.asked]
    if missing and not self.fetching and not self.app.offline(self.cluster):
      self.load(missing)
    self.show_info(ids)
    self.show_jobs(ids)

  def load(self, ids):
    """The jobs of the category from Slurm (sacct -j: also old ones), in chunks; an array id gives all its tasks"""
    self.fetching, self.load_started = True, time.monotonic()
    def work():
      found = {}
      for i in range(0, len(ids), 200):
        for job in load_jobs(None, jids=ids[i:i + 200]):
          found.setdefault(cat.array_of(job.jid) if cat.array_of(job.jid) in ids else job.jid, []).append(job)
      return found
    def loaded(found):
      self.fetching = False
      self.asked.update(ids)
      for jid, jobs in (found or {}).items():
        self.known[jid] = jobs
      self.show_category()
    in_background(self, work, loaded, group="categories-load", name="loading the jobs of the categories")

  def jobs_of(self, jid):
    """What Slurm reported for a job id of the store: the job, or the tasks of an array (or one task)"""
    if self.path == NONE:
      return self.loose.get(jid, [])
    jobs = self.known.get(jid)
    if jobs is None and "_" in jid: # a single task of an array
      jobs = [j for j in self.known.get(cat.array_of(jid), []) if j.jid == jid]
    if not jobs and jid not in self.asked: # (not asked yet, or the cluster cannot be reached: those of the job list)
      jobs = [j for j in self.cluster.jobs.values() if j.jid == jid or "_" not in jid and cat.array_of(j.jid) == jid]
    return jobs or []

  def show_info(self, ids):
    store = self.store
    jobs = []
    for jid in ids:
      jobs.extend(self.jobs_of(jid))
    # (a task that is in another category than its array: counted there, not twice)
    jobs = list({j.jid: j for j in jobs if not ("_" in j.jid and j.jid not in ids and store.category_of(j.jid) != store.category_of(cat.array_of(j.jid)))}.values())
    m = cat.summary(jobs)
    out = Text()
    none = self.path == NONE
    title = "not categorized" if none else self.path or "all categories"
    out.append(title + "\n", style="bold")
    subs = [] if none else store.children(self.path)
    if none:
      since = f"those finished since {self.cluster.since:%m-%d %H:%M}" if self.cluster.since else "no finished ones"
      out.append(f"{len(ids)} job{'s' if len(ids) != 1 else ''} of the job list in no category (queued, and {since}: "
                 "older ones are not known here)" + (f", {m['tasks']} with their array tasks" if m["tasks"] > m["jobs"] else "") + "\n")
    else:
      out.append(f"{len(ids)} job{'s' if len(ids) != 1 else ''} put in it" + (f" (with the {len([p for p in store.paths if cat.within(p, self.path) and p != self.path])} categories in it)" if subs else "")
               + (f", {m['tasks']} with their array tasks" if m["tasks"] > m["jobs"] else "") + "\n")
    if self.fetching:
      out.append(f"asking Slurm … {time.monotonic() - self.load_started:.0f} s\n", style="yellow")
    elif self.app.offline(self.cluster) and not none:
      out.append("the cluster cannot be reached: the numbers are of the jobs kept with the job list; the others when it "
                 "is back (r)\n", style="bold yellow")
    unknown = [j for j in ids if j in self.asked and not self.jobs_of(j)]
    states = m["states"]
    if jobs:
      parts = [(f"{states[k]} {k}", style) for k, style in (("running", "green"), ("pending", "yellow"), ("completed", ""),
               ("failed", "red"), ("cancelled", "dim"), ("other", "dim")) if states.get(k)]
      for i, (text, style) in enumerate(parts):
        out.append(("  ·  " if i else ""), style="dim")
        out.append(text, style=style)
      if m["success"] is not None:
        out.append(f"   ({m['success'] * 100:.0f} % of those that ended completed)", style="dim")
      out.append("\n")
      out.append("Node hours ", style="bold")
      out.append(hours_text(m["node_hours"]))
      out.append("   CPU hours ", style="bold")
      out.append(hours_text(m["cpu_hours"]))
      if m["gpu_hours"]:
        out.append("   GPU hours ", style="bold")
        out.append(hours_text(m["gpu_hours"]))
      out.append("   run time ", style="bold")
      out.append(hours_text(m["elapsed_hours"]) + " h\n")
      if m["first_submit"]:
        out.append("From ", style="bold")
        out.append(m["first_submit"].replace("T", " ")[:16])
        out.append(" (first submit)")
        if m["last_end"]:
          out.append(" to ", style="bold")
          out.append(m["last_end"].replace("T", " ")[:16] + " (last end)")
        out.append("\n")
      for label, counter in (("Partitions", m["partitions"]), ("Accounts", m["accounts"])):
        out.append(f"{label} ", style="bold")
        out.append(", ".join(f"{k} {v}" for k, v in counter.most_common(6)) + "\n")
      if len(m["users"]) > 1:
        out.append("Users ", style="bold")
        out.append(", ".join(f"{k} {v}" for k, v in m["users"].most_common(6)) + "\n")
    elif not ids and none:
      out.append("Every job of the job list is in a category.\n", style="dim")
    elif not ids:
      out.append("No jobs in it yet: in the job list, mark jobs (m) and categorize them (c).\n" if store.paths else
                 "No categories yet. In the job list: mark jobs (m), then c puts them in a category (a new one is made); "
                 "or New here.\n", style="dim")
    if unknown:
      out.append(f"{len(unknown)} no longer known to Slurm (accounting kept only so long): {', '.join(unknown[:8])}"
                 + (" …" if len(unknown) > 8 else "") + "\n", style="dim")
    self.query_one("#cat-info", Static).update(out)

  def show_jobs(self, ids):
    table = self.query_one("#cat-jobs", ClickTable)
    store = self.store
    current = None
    if table.row_count:
      try:
        current = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
      except Exception:
        pass
    table.clear()
    for jid in ids:
      jobs = self.jobs_of(jid)
      where = store.jobs[jid]["category"] if jid in store.jobs else ""
      rel = "" if self.path == NONE else where[len(self.path) + 1:] if self.path and where != self.path else "" if self.path else where
      if not jobs:
        state = (Text("not known to Slurm", style="dim") if jid in self.asked else Text("cluster not reachable", style="yellow")
                 if self.app.offline(self.cluster) else Text("asking Slurm …", style="yellow"))
        table.add_row(jid, store.jobs[jid].get("name") or "", category_text(rel), state, "", "", "", "", "", "", key=jid)
        continue
      array = len(jobs) > 1 or "_" in jobs[0].jid and "_" not in jid
      first = jobs[0].rec
      m = cat.summary(jobs)
      ends = [j.rec["End"] for j in jobs if is_time(j.rec.get("End") or "")]
      table.add_row(Text(f"{jid} ({len(jobs)} tasks)", style="bold") if array else jid, first.get("JobName") or "",
                    category_text(rel), self.app.mixed_state(jobs) if array else state_text(jobs[0].state),
                    first.get("NNodes") or "", "" if array else first.get("Elapsed") or "", hours_text(m["node_hours"]),
                    hours_text(m["gpu_hours"]) if m["gpu_hours"] else "",
                    fmt_time(min(j.rec["Submit"] for j in jobs)) if is_time(first.get("Submit") or "") else "",
                    fmt_time(max(ends)) if ends else "", key=jid)
    if current is not None:
      try:
        table.move_cursor(row=table.get_row_index(current))
      except Exception:
        pass

  @on(ClickTable.RowSelected, "#cat-jobs")
  def job_selected(self, event):
    jid = str(event.row_key.value)
    jobs = self.jobs_of(jid)
    if not jobs:
      return
    # (in a subtab of its own: the one that shows it already, if open)
    if len(jobs) > 1 or ("_" in jobs[0].jid and "_" not in jid):
      self.app.open_array_subtab(jid, jobs[0])
    else:
      from .job import JobScreen
      job = jobs[0]
      self.app.open_view(f"job {job.jid}", lambda: JobScreen(job), kind="job", key=f"job {job.jid}")

  def selected_job(self):
    table = self.query_one("#cat-jobs", ClickTable)
    if not table.row_count:
      return None
    return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

  # ---- changes
  def show_notes(self):
    """The notes of the category selected, rendered (none: the box is hidden)"""
    text = self.store.notes_of(self.path) if self.path and self.path != NONE else ""
    box = self.query_one("#cat-notes-box", VerticalScroll)
    box.border_title = f"notes of {cat.leaf(self.path)}" if text else ""
    box.set_class(not text, "-empty")
    self.query_one("#cat-notes-edit", Button).disabled = not self.path or self.path == NONE
    if getattr(self, "notes_shown", None) != text:
      self.notes_shown = text
      self.query_one("#cat-notes", Markdown).update(text)

  @on(Markdown.LinkClicked, "#cat-notes")
  def note_link(self, event):
    event.stop()
    href = event.href or ""
    if href.startswith(("http://", "https://")):
      self.app.open_page(href)

  @on(Button.Pressed, "#cat-notes-edit")
  def action_notes(self, event=None):
    path = self.path
    if not path or path == NONE:
      self.notify("select a category in the tree first", severity="warning")
      return
    def saved(text):
      if text is not None and text.strip() != self.store.notes_of(path):
        change_categories(self.app, self.cluster, ["note", path, text], lambda st, p: self.changed(p), "writing notes")
    self.app.push_screen(NotesScreen(path, self.store.notes_of(path)), saved)

  def changed(self, select=None):
    self.refresh_tree(select)
    self.show_category()
    if self.app.main is not None:
      self.app.call_later(self.app.populate) # (the job list's column)

  @on(Button.Pressed, "#cat-categorize")
  def action_categorize(self, event=None):
    """The job selected (an array: the whole array) in a category"""
    jid = self.selected_job()
    if jid is None:
      return
    jobs = self.jobs_of(jid)
    whole = len(jobs) > 1 or bool(jobs) and "_" in jobs[0].jid and "_" not in jid
    self.app.categorize_jobs([jid], {jid} if whole else (), after=lambda: self.changed())

  @on(Button.Pressed, "#cat-new")
  def action_new(self, event=None):
    base = "" if self.path == NONE else self.path
    def made(path):
      if path:
        change_categories(self.app, self.cluster, ["add", path], lambda st, p: self.changed(p), "making a category")
    self.app.push_screen(NameScreen("New category" + (f" in {base}" if base else ""), base + "/" if base else "",
                                    "a/b: b in the category a (made too, if it is new)", "Make it"), made)

  @on(Button.Pressed, "#cat-rename")
  def action_rename(self, event=None):
    old = self.path
    if not old or old == NONE:
      self.notify("select a category in the tree first", severity="warning")
      return
    def renamed(new):
      if new and new != old:
        change_categories(self.app, self.cluster, ["rename", old, new], lambda st, p: self.changed(p), "renaming a category")
    self.app.push_screen(NameScreen(f"Rename or move {old}", old, "With all it has (jobs, categories). Another path moves it "
                                    "(into a category that exists: they are merged).", "Rename"), renamed)

  @on(Button.Pressed, "#cat-delete")
  def action_delete(self, event=None):
    path = self.path
    if not path or path == NONE:
      self.notify("select a category in the tree first", severity="warning")
      return
    store = self.store
    n = store.count(path)
    up = cat.parent(path)
    inner = len([p for p in store.paths if cat.within(p, path) and p != path])
    def done(yes):
      if yes:
        change_categories(self.app, self.cluster, ["delete", path], lambda st, p: self.changed(p), "deleting a category")
    self.app.push_screen(ConfirmScreen(f"Delete the category {path}?",
                                       (f"The {inner} categories in it go too. " if inner else "")
                                       + (f"Its {n} job{'s' if n != 1 else ''} go to {up}." if n and up else
                                          f"Its {n} job{'s' if n != 1 else ''} are in no category then (the jobs stay as they are)." if n else
                                          "It has no jobs."), "Delete"), done)

  @on(Button.Pressed, "#cat-take-out")
  def action_take_out(self, event=None):
    jid = self.selected_job()
    if jid is None or self.path == NONE:
      return
    change_categories(self.app, self.cluster, ["unassign", [jid]], lambda st, r: self.changed(), "taking a job out")

  @on(Button.Pressed, "#cat-list")
  def action_in_list(self, event=None):
    """The job list, filtered to this category (the jobs it lists: queued, and those that finished since ...)"""
    if not self.path:
      self.notify("select a category in the tree first", severity="warning")
      return
    app = self.app
    app.show_view(None)
    def filtered():
      search = app.main.query_one("#jfilter", Input)
      search.add_class("visible")
      search.value = "cat:-" if self.path == NONE else f"cat:{self.path}"
      app.job_filter = search.value
      app.populate()
    app.call_after_refresh(filtered)

  @on(Button.Pressed, "#cat-reload")
  def action_reload(self, event=None):
    """The categories from the file again (another slurp may have changed them), and the jobs from Slurm"""
    cluster = self.cluster
    if self.app.offline(cluster): # (no connection: the commands would run on this machine, not the cluster)
      self.notify("the cluster cannot be reached: showing what is kept here", severity="warning")
      self.refresh_tree()
      self.show_category()
      return
    def work():
      self.app.load_categories(cluster)
      return True
    def done(_):
      self.known, self.asked = {}, set()
      self.refresh_tree()
      self.show_category()
    in_background(self, work, done, group="categories", name="reading the categories", connection=cluster.connection)

  def action_back(self):
    self.pop_back()
