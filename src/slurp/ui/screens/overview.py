"""The overview of a tab, shown when none of its subtabs is open (at the start, and after the last one closed): in its
middle, what the cluster is, its health and the SSH session, and the ways on: the job list, the quota, the health.
Not connected: Connect, with the state of the connection."""

import time

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.widgets import Button, Static

from ...cluster.remote import in_background
from .. import icons
from ..widgets import open_clicked_url, url_text
from .base import SplitScreen
from .clusterinfo import cluster_health, cluster_info, HEALTH_STYLES

STALE = 300 # seconds: the cluster is asked again when the overview is shown after this long

class OverviewScreen(SplitScreen):
  """What a tab shows while none of its subtabs is open"""
  DEFAULT_CSS = """
  #ov-middle { height: 1fr; align: center middle; }
  #ov-card { width: 72; max-width: 95%; height: auto; border: round $panel-lighten-2; padding: 1 2; }
  #ov-card Static { width: 100%; text-align: center; }
  #ov-name { text-style: bold; }
  #ov-about { color: $text-muted; margin-bottom: 1; }
  #ov-links { width: auto; height: auto; }
  #ov-card Center { width: 100%; height: auto; margin-top: 1; }
  #ov-links Button { margin: 0 1; }
  #ov-card #ov-no-slurm { width: auto; margin: 0 1; }
  #ov-site { color: $text-muted; margin-top: 1; }
  """
  BINDINGS = [Binding("r", "reload_overview", "Reload"), Binding("j", "jobs", "Jobs")]

  def compose_left(self):
    with Vertical(id="ov-middle"):
      with Vertical(id="ov-card"):
        yield Static(id="ov-name")
        yield Static(id="ov-about")
        yield Static(id="ov-health")
        yield Static(id="ov-ssh")
        yield Static(id="ov-transfers")
        with Center():
          with Horizontal(id="ov-links"):
            yield Button(icons.label("jobs", "Jobs"), id="ov-jobs", variant="primary", compact=True, tooltip="the job list (j)")
            yield Static(id="ov-no-slurm")
            yield Button(icons.label("quota", "Quota"), id="ov-quota", compact=True, tooltip="your quotas on the filesystems (Q)")
            yield Button(icons.label("health", "Health"), id="ov-info", compact=True,
                         tooltip="health with the reasons, occupancy, partitions, node topology (C)")
        with Center(id="ov-connect-row"):
          yield Button(icons.label("connect", "Connect"), id="ov-connect", variant="primary", compact=True, tooltip="log in to the cluster of this tab")
        with Center(id="ov-disconnect-row"):
          yield Button(icons.label("disconnect", "Disconnect"), id="ov-disconnect", compact=True, tooltip="end the SSH session of this tab")
        with Center(id="ov-config-row"):
          yield Button(icons.label("options", "SSH settings…"), id="ov-config", compact=True,
                       tooltip="how this tab logs in: its destination or full SSH settings, what ~/.ssh/config says, Check")
        yield Static(id="ov-site")

  def on_mount(self):
    super().on_mount()
    self.set_interval(1, self.tick)
    self.call_after_refresh(self.refresh_overview)

  def on_screen_resume(self):
    self.refresh_overview()

  def tick(self):
    if self.app.screen is self:
      self.refresh_overview()

  @property
  def cluster(self):
    return self.app.cluster

  def data(self, cluster):
    if not isinstance(cluster.overview, dict):
      cluster.overview = {}
    return cluster.overview

  def refresh_overview(self, force=False):
    """Ask the cluster (in the background) if it can be asked and the last answer is old, then show"""
    if not self.is_mounted or not self.app.clusters:
      return
    cluster = self.cluster
    d = self.data(cluster)
    reachable = not cluster.destination or (cluster.connection is not None and cluster.connection.ready)
    if reachable and not cluster.no_slurm and not d.get("loading") and (force or "info" not in d or time.time() - d.get("info_at", 0) > STALE):
      self.load(cluster)
    self.show()

  def load(self, cluster):
    d = self.data(cluster)
    d["loading"] = True
    def done(info):
      d["loading"] = False
      d["info"], d["info_at"] = info, time.time()
      if self.app.clusters and self.cluster is cluster:
        self.show()
    in_background(self.app, cluster_info, done, group=f"overview-{id(cluster)}", name="the overview of the cluster",
                  connection=cluster.connection if cluster.destination else None)

  def action_reload_overview(self):
    self.refresh_overview(force=True)

  def action_jobs(self):
    if not self.cluster.no_slurm:
      self.app.show_view(None)

  @on(Button.Pressed, "#ov-jobs")
  def jobs_pressed(self, event):
    event.stop()
    self.action_jobs()

  @on(Button.Pressed, "#ov-quota")
  def quota_pressed(self, event):
    event.stop()
    self.app.action_quota()

  @on(Button.Pressed, "#ov-info")
  def info_pressed(self, event):
    event.stop()
    self.app.action_cluster_info()

  @on(Button.Pressed, "#ov-connect")
  def connect_pressed(self, event):
    event.stop()
    self.app.connect_tab()
    self.show()

  @on(Button.Pressed, "#ov-config")
  def config_pressed(self, event):
    event.stop()
    self.app.edit_ssh_settings(self.cluster)

  @on(Button.Pressed, "#ov-disconnect")
  def disconnect_pressed(self, event):
    event.stop()
    if self.cluster.destination:
      self.app.disconnect(self.cluster)
    self.show()

  def on_click(self, event):
    if open_clicked_url(self.app, event): # (the site's status page, its docs)
      event.stop()
      return
    if getattr(event.widget, "id", None) == "ov-transfers" and self.app.transfers.summary_for(self.cluster): # (its transfers)
      event.stop()
      self.app.show_transfers()

  # ---- what it shows
  def show(self):
    if not self.is_mounted:
      return
    q = lambda sel: self.query_one(sel)
    cluster = self.cluster if self.app.clusters else None
    d = self.data(cluster) if cluster else {}
    info = d.get("info")
    state = self.app.connect_state(cluster) if cluster else None
    c = cluster.connection if cluster and cluster.destination else None
    live = cluster is not None and (not cluster.destination or (c is not None and c.ready)) # (its session answers)
    connected = cluster is not None and state is None and live
    lines = {"#ov-name": Text(icons.label("cluster" if cluster.destination else "local", cluster.name), style="bold") if cluster else Text(""),
             "#ov-about": self.about(cluster, info) if cluster else Text(""),
             "#ov-health": self.health(cluster, d, info) if connected else Text(""),
             "#ov-ssh": self.ssh(cluster, state, live) if cluster else Text(""),
             "#ov-transfers": Text(self.app.transfers.summary_for(cluster), style="cyan") if cluster else Text(""),
             "#ov-site": self.site(cluster) if cluster else Text("")}
    for sel, text in lines.items():
      q(sel).update(text)
      q(sel).display = bool(text.plain.strip()) # (no empty lines)
    q("#ov-links").display = connected
    q("#ov-connect-row").display = state == "idle"
    q("#ov-disconnect-row").display = c is not None and c.state not in ("new", "closed") and state != "idle"
    button = q("#ov-disconnect")
    label = icons.label("disconnect", "Disconnect" if live else "Abort") # (not connected yet: the login is given up)
    if str(button.label) != label:
      button.label = label
      button.tooltip = ("end the SSH session of this tab" if live else
                        "give up the login (the tab stays not connected; Connect tries again)")
      # (its width follows the label: Abort → Disconnect; a hidden row would keep the narrow one)
      button.refresh(layout=True)
      q("#ov-disconnect-row").refresh(layout=True)
    q("#ov-config-row").display = bool(cluster and cluster.destination)
    if connected:
      jobs = q("#ov-jobs")
      jobs.display = not cluster.no_slurm
      q("#ov-no-slurm").display = cluster.no_slurm
      q("#ov-no-slurm").update(Text("No Slurm available", style="yellow"))
      label = self.jobs_label(cluster)
      if str(jobs.label) != label:
        jobs.label = label

  def about(self, cluster, info):
    """A line about the cluster: the site, Slurm's version, nodes and partitions"""
    words = []
    pack = self.pack(cluster)
    if pack.get("name"):
      words.append(str(pack["name"]))
    words.append(f"SSH {cluster.destination}" if cluster.destination else "this machine")
    if info and not info.get("failed"):
      if info.get("slurm"):
        words.append(f"Slurm {info['slurm']}")
      parts = [p["name"].rstrip("*") for p in info.get("partitions", [])]
      if info.get("nodes"):
        words.append(f"{info['nodes']} nodes" + (f" in {len(parts)} partition{'s' if len(parts) != 1 else ''}"
                                                 f" ({', '.join(parts[:4])}{', …' if len(parts) > 4 else ''})" if parts else ""))
    return Text(" · ".join(words))

  def health(self, cluster, d, info):
    if cluster.no_slurm:
      return Text("")
    if info is None:
      if d.get("loading") or "info" not in d:
        return Text(f"{self.app.spinner()} asking the cluster …", style="yellow")
      return Text("the cluster did not answer (r asks again)", style="red")
    if info.get("failed"):
      return Text("Slurm did not answer: " + "; ".join(info["errors"])[:120], style="red")
    level, why = cluster_health(info)
    return Text(f"● {dict(good='healthy', warn='degraded', bad='unhealthy')[level]}: {why}", style=HEALTH_STYLES[level])

  def ssh(self, cluster, state, live=True):
    """The SSH session: its state (as the footer has it); not connected: what happened, and Connect; while it
    connects: what ssh printed last (a changed host key: said plainly)"""
    if not cluster.destination:
      return Text("")
    if state == "idle":
      c = cluster.connection
      why = ("never connected" if c is None or c.state == "new" else "the session was closed" if c.state == "closed"
             else f"disconnected{': ' + c.error if c.error else ''}")
      return Text.assemble((f"Not connected to {cluster.destination}", "bold"), f"\n{why}")
    if state == "connecting":
      return Text.assemble((f"Connecting to {cluster.destination} …", "bold yellow"), "\nlog in on the right")
    text = self.app.connection_text(cluster)
    if not live:
      said = self.ssh_said(cluster)
      if any("REMOTE HOST IDENTIFICATION HAS CHANGED" in line or "Host key verification failed" in line
             or ("Host key for" in line and "has changed" in line) for line in said):
        text.append(f"\nThe host key of {cluster.destination} changed: ssh does not trust it. Ask the site whether that is "
                    "expected before you remove the old key (ssh-keygen -R <host>) and log in again.", style="bold red")
      elif said:
        text.append("\nssh: " + " | ".join(said[-2:])[:160], style="dim")
    return text

  def ssh_said(self, cluster):
    """The last lines of the login's terminal (what ssh printed)"""
    t = cluster.login_terminal
    if t is None or t.vt is None:
      return []
    return [line.strip() for line in t.vt.display if line.strip()]

  def jobs_label(self, cluster):
    if not cluster.loaded:
      return icons.label("jobs", "Jobs")
    queued = [j for j in cluster.jobs.values() if j.queued]
    running = sum(j.running for j in queued)
    return icons.label("jobs", f"Jobs · {running} running, {len(queued) - running} pending")

  def pack(self, cluster):
    import fnmatch
    from ...checks.rulepacks import load_packs
    names = [n.lower() for n in (cluster.name, cluster.destination or "") if n]
    names += [n.split("@")[-1] for n in names if "@" in n]
    return next((p for p in load_packs() if any(fnmatch.fnmatch(n, pat.lower()) for pat in p.get("match", []) for n in names)), {})

  def site(self, cluster):
    """The site's status page and documentation (a click opens them), if a rule pack knows the site"""
    pack = self.pack(cluster)
    text = Text()
    for key, label in (("status", "status"), ("docs", "docs")):
      url = str(pack.get(key) or "")
      if url.startswith(("https://", "http://")):
        if text:
          text.append(" · ")
        text.append(icons.label(key, label) + " ")
        text.append_text(url_text(url, url.split("://", 1)[1].rstrip("/")))
    return text
