"""The job list"""


from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.widgets import (
  Button, Checkbox, Input, Label, Select, Static
)


from ...cluster.jobs import array_task_id, is_time, job_id_key, pace_eta, progress_for_job
from ..widgets import ClickTable
from .base import SplitScreen
from ...checks.engine import counts as checks_counts

COLUMNS = [("jobid", "Job ID"), ("name", "Name"), ("category", "Category"), ("user", "User"), ("state", "State"), ("progress", "Progress"),
           ("eta", "Done: same pace"), ("eta_now", "Done: current pace"),
           ("nodes", "Nodes"), ("time", "Elapsed / Limit"),
           ("start", "Start"), ("end", "End"), ("checks", "Checks"), ("err", "Err"), ("cmd", "Command")]

SINCE_CHOICES = [("only queued", "off"), ("1 hour", "1h"), ("6 hours", "6h"), ("12 hours", "12h"), ("1 day", "1d"),
                 ("2 days", "2d"), ("1 week", "1w"), ("2 weeks", "2w"), ("1 month", "1mo"), ("custom …", "custom")]

SORT_KEYS = {
  "arrayid": lambda j: (int(t) if (t := array_task_id(j)) is not None else -1, job_id_key(j.jid)), # array index first
  "jobid": lambda j: job_id_key(j.jid),
  "name":  lambda j: j.rec["JobName"],
  "category": lambda j: (not j.category, j.category.casefold()), # (those in none last)
  "user":  lambda j: j.rec["User"].casefold(),
  "state": lambda j: j.state,
  "progress": lambda j: (p.current / p.total if (p := progress_for_job(j)) and p.total else 0),
  "eta": lambda j: pace_eta(j.progress, "same") or float("inf"),
  "eta_now": lambda j: pace_eta(j.progress, "current") or float("inf"),
  "nodes": lambda j: int(j.rec["NNodes"] or 0),
  "checks": lambda j: tuple(-v for v in checks_counts(j.checks).values()),
  "time":  lambda j: j.node_hours() / max(int(j.rec["NNodes"] or 1), 1),
  "start": lambda j: j.rec["Start"] if is_time(j.rec["Start"]) else "",
  "end":   lambda j: j.rec["End"] if is_time(j.rec["End"]) else "",
  "err":   lambda j: (j.err[0] == "file", j.err[1]),
  "cmd":   lambda j: j.command,
}

class MainScreen(SplitScreen):
  """The job table"""
  SHOW_NOTABS = True
  BINDINGS = [
    Binding("slash", "filter", "Filter jobs"),
    Binding("escape", "clear_filter", "Clear filter", show=False),
    Binding("space", "app.toggle_group", "Open/close array", show=False),
    Binding("g", "app.toggle_collapse", "Group arrays"),
    Binding("exclamation_mark", "app.toggle_problems", "Only problems"),
    Binding("n", "app.new_script", "New job script"),
    Binding("m", "app.mark", "Mark"),
    Binding("M", "app.mark_all", "Mark all / none", show=False),
    Binding("c", "app.categorize", "Categorize"),
    Binding("K", "app.categories", "Categories", show=False),
  ]

  def compose_left(self):
    with Horizontal(id="toolbar"):
      yield Button("☰ Menu", id="menu", compact=True,
                   tooltip="cluster info, quota, shell, drive, interactive job, categories, web, options, … (F10)")
      yield Button("Reload", id="reload", variant="primary", compact=True)
      yield Label("finished since")
      yield Select(SINCE_CHOICES, value="1d", allow_blank=False, id="since-choice",
                   tooltip="also list the jobs that finished within this time", compact=True)
      yield Input("1d", id="since", placeholder="e.g. 3d12h", tooltip="s, m, h, d, w, mo, y, combined (1d12h); Enter: load", compact=True)
      yield Select([("my jobs", "me"), ("all my accounts", "all")], value="me", allow_blank=False, id="scope",
                   tooltip="whose jobs are listed: yours, or those of one or all of your Slurm accounts", compact=True)
      yield Checkbox("group arrays", value=True, id="collapse-arrays", compact=True,
                     tooltip="big arrays as one row (on) or every task individually (off) (g)")
    yield Static(id="status")
    yield ClickTable(id="jobs", cursor_type="row", zebra_stripes=True)
    yield Static(id="no-slurm")
    with Vertical(id="connect-panel"):
      yield Static(id="connect-note")
      with Center():
        yield Button("Connect", id="connect", variant="primary", compact=True, tooltip="log in to the cluster of this tab")
    yield Input(id="jfilter", placeholder="filter jobs by name, command, ID, state, user, account, path, category… (cat:a/b: only that category)")

  def action_filter(self):
    search = self.query_one("#jfilter", Input)
    search.add_class("visible")
    search.value = self.app.job_filter
    search.focus()

  def action_clear_filter(self):
    search = self.query_one("#jfilter", Input)
    if search.has_class("visible"):
      search.value = ""
      search.remove_class("visible")
      self.app.job_filter = ""
      self.app.populate()
      self.query_one("#jobs", ClickTable).focus()

  def on_screen_resume(self):
    """Back to the job list: draw it again if results came while another window was shown (the checks)"""
    cluster = self.app.cluster if self.app.clusters else None
    if cluster is not None and getattr(cluster, "stale_list", False) and cluster.loader is None:
      cluster.stale_list = False
      self.call_after_refresh(self.app.populate)

  def on_mount(self):
    self.app.main = self
    # the job list is the default mode; Textual does not register that one, so it could not be switched back to
    self.app._modes.setdefault(self.app.DEFAULT_MODE, lambda: self)
    from .overview import OverviewScreen
    self.app._modes.setdefault("overview", OverviewScreen) # (the first subtab of every tab: one screen for all of them)
    self.app.setup_job_columns()
    table = self.query_one("#jobs", ClickTable)
    table.focus()
    if self.app.clusters:
      self.app.activate_cluster(self.app.cluster_index)
    else:
      self.set_class(True, "-no-tabs")
