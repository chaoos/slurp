"""The overview of a tab, its first subtab: the cluster at a glance (health, occupancy, your jobs, your quotas, the
partitions and the system), each part loaded in the background"""

import collections
import time

import humanize
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from ...cluster.remote import in_background
from ...cluster.storage import quota_info
from ..widgets import open_clicked_url, url_text
from .base import SplitScreen
from .clusterinfo import cluster_health, cluster_info, HEALTH_STYLES, info_grid, nodes_text

STALE = 300 # seconds: an overview older than this is loaded again when it is shown

def usage_bar(used, limit, width=12):
  """(bar, percent) of used of limit, coloured by how full it is"""
  if not limit:
    return Text(""), Text("")
  share = used / limit
  style = "red" if share >= 0.9 else "yellow" if share >= 0.75 else "green"
  filled = min(width, round(share * width))
  return Text("█" * filled + "░" * (width - filled), style=style), Text(f"{share:.0%}", style=style)

class OverviewScreen(SplitScreen):
  """The first subtab of every tab: what the cluster is and how it is doing, your jobs and quotas there"""
  DEFAULT_CSS = """
  #ov-bar { height: auto; padding: 0 1; }
  #ov-bar > * { margin-right: 1; }
  #ov-title { height: auto; padding: 0 1; margin-bottom: 1; }
  #ov-body { height: 1fr; padding: 0 1; }
  #ov-connect { display: none; height: 1fr; align: center middle; }
  #ov-connect Static { width: 100%; text-align: center; margin-bottom: 1; color: $text-muted; }
  #ov-connect Center { height: auto; }
  """
  BINDINGS = [Binding("r", "reload_overview", "Reload"), Binding("j", "jobs", "Jobs")]

  def compose_left(self):
    with Horizontal(id="ov-bar"):
      yield Button("☰ Menu", id="menu", compact=True,
                   tooltip="cluster info, quota, shell, drive, interactive job, categories, web, options, … (F10)")
      yield Button("Reload", id="ov-reload", variant="primary", compact=True, tooltip="ask the cluster again (r)")
      yield Button("Jobs", id="ov-jobs", compact=True, tooltip="the job list (j)")
    yield Static(id="ov-title")
    with VerticalScroll(id="ov-body"):
      yield Static(id="ov-info")
    with Vertical(id="ov-connect"):
      yield Static(id="ov-connect-note")
      with Center():
        yield Button("Connect", id="ov-connect-button", variant="primary", compact=True, tooltip="log in to the cluster of this tab")

  def on_mount(self):
    super().on_mount()
    self.set_interval(1, self.tick)
    self.call_after_refresh(self.refresh_overview)

  def on_screen_resume(self):
    self.refresh_overview()

  def tick(self):
    """While shown: load once the tab can be asked (after a login), show what came"""
    if self.app.screen is self:
      self.refresh_overview()

  @property
  def cluster(self):
    return self.app.cluster

  def data(self, cluster=None):
    cluster = cluster or self.cluster
    if not isinstance(cluster.overview, dict):
      cluster.overview = {}
    return cluster.overview

  def reachable(self, cluster):
    """The tab's cluster can be asked now (this machine, or a session that is ready)"""
    return not cluster.destination or (cluster.connection is not None and cluster.connection.ready)

  def refresh_overview(self, force=False):
    """Load what is missing or stale (unless loading), then show it"""
    if not self.is_mounted or not self.app.clusters:
      return
    cluster = self.cluster
    d = self.data(cluster)
    if self.reachable(cluster) and not cluster.no_slurm:
      now = time.time()
      for part, work in (("info", cluster_info), ("quota", quota_info)):
        if part in d.get("loading", set()):
          continue
        if force or part not in d or now - d.get(f"{part}_at", 0) > STALE:
          self.load(cluster, part, work)
    self.show()

  def load(self, cluster, part, work):
    d = self.data(cluster)
    d.setdefault("loading", set()).add(part)
    def done(result):
      d["loading"].discard(part)
      if result is not None:
        d[part], d[f"{part}_at"] = result, time.time()
      else:
        d.setdefault(part, None)
        d[f"{part}_at"] = time.time() # (failed: not again at once; Reload asks)
      if self.app.clusters and self.cluster is cluster:
        self.show()
    in_background(self.app, work, done, group=f"overview-{part}-{id(cluster)}", name=f"the {part} of the overview",
                  connection=cluster.connection if cluster.destination else None)

  def action_reload_overview(self):
    self.refresh_overview(force=True)

  @on(Button.Pressed, "#ov-reload")
  def reload_pressed(self, event):
    event.stop()
    self.refresh_overview(force=True)

  @on(Button.Pressed, "#ov-jobs")
  def jobs_pressed(self, event):
    event.stop()
    self.action_jobs()

  def action_jobs(self):
    self.app.show_view(None)

  @on(Button.Pressed, "#ov-connect-button")
  def connect_pressed(self, event):
    event.stop()
    self.app.connect_tab()
    self.show()

  def on_click(self, event):
    if open_clicked_url(self.app, event): # (the status page and the docs of the site)
      event.stop()

  # ---- what it shows
  def show(self):
    if not self.is_mounted:
      return
    if not self.app.clusters:
      self.query_one("#ov-title", Static).update("")
      self.query_one("#ov-info", Static).update("")
      return
    cluster = self.cluster
    d = self.data(cluster)
    state = self.app.connect_state(cluster)
    connect = self.query_one("#ov-connect")
    connect.display = state is not None
    self.query_one("#ov-body").display = state is None
    if state is not None:
      self.query_one("#ov-connect-note", Static).update(
        Text.assemble((f"Connecting to {cluster.destination} …", "bold"), "\nlog in on the right") if state == "connecting" else
        Text.assemble((f"Not connected to {cluster.destination}", "bold"), "\nthe overview shows the cluster once you are logged in"))
      self.query_one("#ov-connect-button").display = state == "idle"
    self.query_one("#ov-title", Static).update(self.title_text(cluster, d))
    if state is None:
      self.shown = self.body(cluster, d)
      self.query_one("#ov-info", Static).update(self.shown)

  def title_text(self, cluster, d):
    info = d.get("info")
    text = Text(cluster.name, style="bold")
    if info:
      level, why = cluster_health(info)
      text.append(" · ")
      text.append(f"● {dict(good='healthy', warn='degraded', bad='unhealthy')[level]}: {why}", style=HEALTH_STYLES[level])
    c = cluster.connection
    if cluster.destination and c is not None and c.ready:
      text.append(f" · {c.identity}" + (f" ({c.latency * 1000:.0f} ms)" if c.latency is not None else ""), style="dim")
    elif not cluster.destination:
      text.append(" · this machine", style="dim")
    if info and info.get("slurm"):
      text.append(f" · Slurm {info['slurm']}", style="dim")
    loading = d.get("loading") or set()
    if loading:
      text.append(f" · {self.app.spinner()} loading {' and '.join(sorted(loading))} …", style="yellow")
    elif info:
      text.append(f" · as of {info['time']:%H:%M:%S}", style="dim")
    return text

  def body(self, cluster, d):
    parts = []
    def heading(title):
      parts.append(Text(("\n" if parts else "") + title, style="bold underline"))
    if cluster.no_slurm:
      parts.append(Text(f"No Slurm on {cluster.destination or 'this machine'}: squeue is not found there. Add a tab for a "
                        "cluster with +, or start slurp with slurp --ssh user@login-node.", style="yellow"))
      return Group(*parts)
    heading("Your jobs")
    parts.append(self.jobs_text(cluster))
    info = d.get("info")
    if "info" not in d:
      heading("The cluster")
      parts.append(Text("loading …", style="dim"))
    elif info is None:
      heading("The cluster")
      parts.append(Text("could not be asked (Reload asks again)", style="red"))
    elif info["failed"]:
      heading("The cluster")
      parts.append(Text("Could not query the cluster:\n" + "\n".join(info["errors"]), style="red"))
    else:
      parts.append(Text(""))
      parts.append(info_grid(info, ("health", "reservations", "occupancy")))
    heading("Your quotas")
    parts.append(self.quota_text(d))
    if info and not info["failed"]:
      parts.append(Text(""))
      parts.append(info_grid(info, ("partitions", "topology")))
    site = self.site_text(cluster)
    if site is not None:
      heading("The site")
      parts.append(site)
    return Group(*parts)

  def jobs_text(self, cluster):
    if not cluster.loaded:
      return Text("not loaded yet: the job list (j) loads them", style="dim")
    jobs = list(cluster.jobs.values())
    queued = [j for j in jobs if j.queued]
    running = [j for j in queued if j.running]
    pending = [j for j in queued if not j.running]
    finished = [j for j in jobs if not j.queued]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    nodes = sum(int(j.rec.get("NNodes") or 0) for j in running if str(j.rec.get("NNodes", "")).isdigit())
    grid.add_row("running", Text(f"{len(running)}" + (f" on {nodes_text(nodes)}" if nodes else ""), style="green" if running else ""))
    reasons = collections.Counter(j.reason for j in pending if j.reason and j.reason != "None")
    grid.add_row("pending", f"{len(pending)}" + (": " + ", ".join(f"{r} {n}" for r, n in reasons.most_common(4)) if reasons else ""))
    if cluster.since:
      states = collections.Counter(j.state.split()[0] if j.state else "?" for j in finished)
      bad = sum(n for s, n in states.items() if s in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE"))
      hours = sum(j.node_hours() for j in finished)
      grid.add_row(f"finished since {cluster.since:%m-%d %H:%M}",
                   Text.assemble(f"{len(finished)}: " if finished else "none",
                                 ", ".join(f"{n} {s.lower()}" for s, n in states.most_common()),
                                 (f" · {hours:.1f} node hours" if finished else ""), style="red" if bad else ""))
    problems = sum(1 for j in jobs if j.checks and any(f.severity == "error" for f in j.checks))
    if problems:
      grid.add_row("with problems", Text(f"{problems} (the job list, !: only these)", style="yellow"))
    grid.add_row("", Text("the job list: j, or Jobs above", style="dim"))
    return grid

  def quota_text(self, d):
    if "quota" not in d:
      return Text("loading …", style="dim")
    info = d["quota"]
    if info is None:
      return Text("could not be asked (Reload asks again)", style="red")
    if not info["entries"]:
      return Text("\n".join(info["errors"]) or "no quotas found", style="dim")
    table = Table(box=None, padding=(0, 1), header_style="bold")
    for col, justify in (("filesystem", "left"), ("of", "left"), ("space", "right"), ("", "left"), ("", "right"),
                         ("files", "right"), ("", "left"), ("", "right")):
      table.add_column(col, justify=justify)
    gb = lambda kb: humanize.naturalsize(kb * 1024, binary=True)
    for e in info["entries"]:
      kb_limit, files_limit = e["kb_hard"] or e["kb_soft"], e["files_hard"] or e["files_soft"]
      table.add_row(Text(e["filesystem"], style="bold red" if e["over"] else ""), f"{e['kind']} {e['name']}",
                    f"{gb(e['kb'])} of {gb(kb_limit)}" if kb_limit else gb(e["kb"]), *usage_bar(e["kb"], kb_limit),
                    f"{e['files']:,} of {files_limit:,}" if files_limit else f"{e['files']:,}", *usage_bar(e["files"], files_limit))
    return Group(table, Text("all of them, and the oldest files in scratch: the menu (Q, X)", style="dim"))

  def site_text(self, cluster):
    """The site's pack: its name, status page and docs, the scratch purge (None if no pack matches)"""
    from ...checks.rulepacks import load_packs
    import fnmatch
    names = [n.lower() for n in (cluster.name, cluster.destination or "") if n]
    names += [n.split("@")[-1] for n in names if "@" in n]
    pack = next((p for p in load_packs() if any(fnmatch.fnmatch(n, pat.lower()) for pat in p.get("match", []) for n in names)), None)
    if pack is None:
      return None
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    grid.add_row("site", str(pack.get("name", "")))
    for key, label in (("status", "status page"), ("docs", "documentation")):
      url = str(pack.get(key) or "")
      if url.startswith(("https://", "http://")):
        grid.add_row(label, url_text(url))
    if pack.get("scratch_purge_days"):
      grid.add_row("scratch", f"files unused for {pack['scratch_purge_days']} days are deleted (the menu: old files in scratch, X)")
    return grid
