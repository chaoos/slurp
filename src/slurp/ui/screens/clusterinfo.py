"""The state of a tab's cluster (Health, a subtab; the overview uses its health): health, reservations,
occupancy, partitions, node topology"""

import collections
import re
import shlex
import time
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from .. import icons


from ...state import log
from ...cluster.remote import in_background, run_many
from ...checks.engine import mem_text

NODE_FLAGS = {"*": "not responding", "~": "powered off", "#": "powering up", "!": "powering down", "%": "powering down",
              "$": "maintenance", "@": "pending reboot", "^": "rebooting", "-": "planned"}

def cluster_info():
  """Nodes, partitions, reasons, reservations and the queue of the cluster (one round trip on a remote cluster)"""
  cmds = [["sinfo", "-h", "-N", "-o", "%N|%T"],
          ["sinfo", "-h", "-o", "%P|%a|%F|%l|%D"],
          ["sinfo", "-h", "-R", "-o", "%E|%T|%D|%N|%H"],
          ["squeue", "-h", "-a", "-r", "--format=%T|%D|%r"],
          ["scontrol", "show", "reservation", "-o"],
          ["sinfo", "--version"]]
  res = run_many(cmds)
  try:
    from ...checks.engine import gather_facts
    facts = gather_facts()
  except Exception as e:
    log.debug(f"facts for the node topology: {e!r}")
    facts = None
  info = {"facts": facts, "errors": [f"{shlex.join(c[:3])}: {(r.stderr or '').strip()[:200]}" for c, r in zip(cmds[:5], res[:5]) if r.returncode
                     and "No reservations" not in (r.stdout + r.stderr)],
          "time": datetime.now(), "failed": all(r.returncode for r in res[:4]),
          "slurm": res[5].stdout.strip().removeprefix("slurm ") if len(res) > 5 and not res[5].returncode else ""}
  nodes = {}
  for line in res[0].stdout.splitlines():
    name, _, state = line.partition("|")
    if name and state:
      nodes[name] = state.strip()
  states, flags = collections.Counter(), collections.Counter()
  for state in nodes.values():
    base = state.rstrip("".join(NODE_FLAGS)).split("+")[0].lower()
    states[base] += 1
    for ch, label in NODE_FLAGS.items():
      if state.endswith(ch) or f"+{ch}" in state:
        flags[label] += 1
    for extra in state.lower().split("+")[1:]:
      flags[extra.rstrip("".join(NODE_FLAGS))] += 1
  info["nodes"], info["states"], info["flags"] = len(nodes), states, flags
  info["partitions"] = []
  for line in res[1].stdout.splitlines():
    f = line.split("|")
    if len(f) == 5:
      counts = (f[2].split("/") + ["0"] * 4)[:4]
      info["partitions"].append({"name": f[0], "avail": f[1], "alloc": counts[0], "idle": counts[1], "other": counts[2],
                                 "total": counts[3], "limit": f[3]})
  info["reasons"] = [dict(zip(["reason", "state", "count", "nodes", "since"], line.split("|", 4)))
                     for line in res[2].stdout.splitlines() if line.count("|") >= 4]
  jobs, job_nodes, pending_reasons = collections.Counter(), collections.Counter(), collections.Counter()
  for line in res[3].stdout.splitlines():
    f = line.split("|")
    if len(f) == 3:
      jobs[f[0]] += 1
      job_nodes[f[0]] += int(f[1]) if f[1].isdigit() else 0
      if f[0] == "PENDING":
        pending_reasons[f[2]] += 1
  info["jobs"], info["job_nodes"], info["pending_reasons"] = jobs, job_nodes, pending_reasons
  info["reservations"] = []
  for line in res[4].stdout.splitlines():
    fields = dict(re.findall(r"(\w+)=(\S*)", line))
    if "ReservationName" in fields:
      info["reservations"].append(fields)
  return info

HEALTH_STYLES = {"good": "bold green", "warn": "bold yellow", "bad": "bold red"}

def cluster_health(info):
  """("good" | "warn" | "bad", why) of the cluster from cluster_info()"""
  if info is None or info.get("failed"):
    return "bad", "no answer from Slurm"
  states, total = info["states"], info["nodes"] or 0
  broken = sum(n for s, n in states.items() if s in ("down", "fail", "failing"))
  impaired = broken + sum(n for s, n in states.items() if s in ("drained", "draining", "maint", "unknown", "inval"))
  impaired += info["flags"].get("not responding", 0)
  down_parts = [p["name"] for p in info["partitions"] if p["avail"] != "up"]
  now, soon = datetime.now(), []
  for r in info["reservations"]:
    if "MAINT" not in r.get("Flags", ""):
      continue
    try:
      start, end = datetime.fromisoformat(r.get("StartTime", "")), datetime.fromisoformat(r.get("EndTime", ""))
    except ValueError:
      continue
    if start <= now <= end or 0 <= (start - now).total_seconds() <= 86400:
      soon.append(r["ReservationName"] + (" active" if start <= now else f" at {start:%m-%d %H:%M}"))
  if not total or broken >= max(1, total // 10) or down_parts:
    why = [f"{broken} of {total} nodes down"] if total else ["no nodes"]
    return "bad", ", ".join(why + ([f"partition {', '.join(down_parts)} not up"] if down_parts else []))
  if impaired or soon:
    return "warn", ", ".join(([f"{impaired} of {total} nodes not usable"] if impaired else []) + [f"maintenance {s}" for s in soon])
  return "good", f"all {total} nodes usable"

def nodes_text(n):
  return f"{n} node" if n == 1 else f"{n} nodes"

SECTIONS = ("health", "reservations", "occupancy", "partitions", "topology")

def info_grid(info, parts=SECTIONS):
  """The sections of cluster_info() asked for, as one grid (a title row each)"""
  grid = Table.grid(padding=(0, 2))
  grid.add_column(style="bold", no_wrap=True)
  grid.add_column()
  def section(title):
    if grid.row_count:
      grid.add_row("", "")
    grid.add_row(Text(title, style="bold underline"), "")
  s, total = info["states"], info["nodes"]
  if "health" in parts:
    unhealthy = {k: v for k, v in s.items() if k in ("down", "drained", "draining", "fail", "failing", "maint", "unknown",
                                                    "inval", "future", "reboot_requested", "planned", "power_down")}
    section("Health")
    if unhealthy or info["flags"]:
      for state, n in sorted(unhealthy.items(), key=lambda kv: -kv[1]):
        grid.add_row(state, Text(nodes_text(n), style="red" if state in ("down", "fail", "failing") else "yellow"))
      for flag, n in info["flags"].most_common():
        grid.add_row(flag, Text(nodes_text(n), style="yellow"))
    else:
      grid.add_row("", Text("all nodes are up", style="green"))
    for r in info["reasons"][:25]:
      grid.add_row(Text(r["state"], style="yellow"), Text(f"{nodes_text(int(r['count']) if r['count'].isdigit() else r['count'])} ({r['nodes']}): {r['reason']}  since {r['since'].replace('T', ' ')}"))
  if "reservations" in parts and info["reservations"]:
    section("Reservations")
    for r in info["reservations"]:
      flags = r.get("Flags", "")
      style = "bold yellow" if "MAINT" in flags else ""
      grid.add_row(Text(r["ReservationName"], style=style),
                   f"{r.get('StartTime', '?').replace('T', ' ')} – {r.get('EndTime', '?').replace('T', ' ')}, "
                   f"{r.get('NodeCnt', '?')} nodes, {r.get('State', '')} {flags}")
  if "occupancy" in parts:
    section("Occupancy")
    busy = s.get("allocated", 0) + s.get("mixed", 0) + s.get("completing", 0)
    grid.add_row("Nodes", f"{total} in total")
    if total:
      grid.add_row("in use", f"{busy} ({busy / total:.0%}): {s.get('allocated', 0)} allocated, {s.get('mixed', 0)} partly (mixed)"
                   + (f", {s.get('completing', 0)} completing" if s.get("completing") else ""))
      grid.add_row("free", Text(f"{s.get('idle', 0)} idle", style="green"))
      if s.get("reserved") or s.get("planned"):
        grid.add_row("reserved", f"{s.get('reserved', 0) + s.get('planned', 0)}")
    jobs, job_nodes = info["jobs"], info["job_nodes"]
    grid.add_row("Jobs", f"{jobs.get('RUNNING', 0)} running on {job_nodes.get('RUNNING', 0)} nodes, "
                 f"{jobs.get('PENDING', 0)} pending asking for {job_nodes.get('PENDING', 0)} nodes"
                 + "".join(f", {n} {st.lower()}" for st, n in jobs.items() if st not in ("RUNNING", "PENDING")))
    if info["pending_reasons"]:
      grid.add_row("pending because", ", ".join(f"{reason} {n}" for reason, n in info["pending_reasons"].most_common(6)))
  if "partitions" in parts and info["partitions"]:
    section("Partitions")
    table = Table(box=None, padding=(0, 1), header_style="bold")
    for col in ("partition", "avail", "allocated", "idle", "other", "total", "time limit"):
      table.add_column(col, justify="right" if col in ("allocated", "idle", "other", "total") else "left")
    for p in info["partitions"]:
      table.add_row(p["name"], Text(p["avail"], style="" if p["avail"] == "up" else "red"), p["alloc"], p["idle"],
                    p["other"], p["total"], p["limit"])
    grid.add_row("", table)
  facts = info.get("facts")
  if "topology" in parts and facts is not None and facts.partitions:
    section("Node topology")
    table = Table(box=None, padding=(0, 1), header_style="bold")
    for col in ("partition", "CPUs", "sockets × cores × threads", "NUMA domains", "GPUs", "memory", "whole nodes"):
      table.add_column(col, justify="left" if col == "partition" else "right")
    domains = facts.site.get("numa_domains")
    for p in sorted(facts.partitions.values(), key=lambda p: (not p.default, p.name)):
      table.add_row(p.name, str(p.cpus), f"{p.sockets or '?'} × {p.cores or '?'} × {p.threads}",
                    str(domains or p.sockets or "?"), str(p.gpus or "-"), mem_text(p.mem_mb) if p.mem_mb else "?",
                    "yes" if p.exclusive else "no")
    grid.add_row("", table)
    grid.add_row("", Text("NUMA placement: the guides (F1)", style="dim"))
  for err in info["errors"]:
    grid.add_row(Text("error", style="red"), err)
  return grid


class ClusterInfoScreen(ModalScreen):
  """Health: everything about the cluster of a tab (a window): health with the reasons, reservations, occupancy,
  partitions, node topology; the title (and the border's colour) says healthy, degraded or unhealthy and why"""
  DEFAULT_CSS = """
  ClusterInfoScreen { align: center middle; }
  #ci-dialog { width: 120; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #ci-dialog.-good { border: thick $success; }
  #ci-dialog.-warn { border: thick $warning; }
  #ci-dialog.-bad { border: thick $error; }
  #ci-title { height: auto; margin-bottom: 1; }
  #ci-body { height: 1fr; }
  #ci-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #ci-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("r", "reload", "Reload")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster, self.info, self.fetching = cluster, None, False

  def compose(self):
    with Vertical(id="ci-dialog"):
      yield Static(id="ci-title")
      with VerticalScroll(id="ci-body"):
        yield Static(id="ci-info")
      with Horizontal(id="ci-buttons"):
        yield Button(icons.label("reload", "Reload"), id="ci-reload", variant="primary", compact=True)
        yield Button("Close", id="ci-close", compact=True)

  def on_mount(self):
    cached = (self.cluster.overview or {}).get("info") if isinstance(self.cluster.overview, dict) else None
    if cached:
      self.info = cached
      self.show()
    self.action_reload()

  @on(Button.Pressed, "#ci-close")
  def action_close(self):
    self.dismiss()

  def title_text(self):
    text = Text(icons.label("health", f"Health of {self.cluster.name}"), style="bold")
    dialog = self.query_one("#ci-dialog")
    for level in HEALTH_STYLES:
      dialog.remove_class(f"-{level}")
    if self.info is not None and not self.info["failed"]:
      level, why = cluster_health(self.info)
      dialog.add_class(f"-{level}")
      text.append(" · ")
      text.append(f"● {dict(good='healthy', warn='degraded', bad='unhealthy')[level]}: {why}", style=HEALTH_STYLES[level])
    if self.fetching:
      text.append(" · loading …", style="yellow")
    elif self.info:
      text.append(f" · as of {self.info['time']:%H:%M:%S}", style="dim")
    return text

  @on(Button.Pressed, "#ci-reload")
  def action_reload(self, event=None):
    if event is not None:
      event.stop()
    if self.fetching:
      return
    c = self.cluster.connection
    if self.cluster.destination and (c is None or not c.ready):
      self.query_one("#ci-title", Static).update(self.title_text())
      self.query_one("#ci-info", Static).update(Text(f"Not connected to {self.cluster.destination}: Connect in the overview or "
                                                     "the job list, then Reload.", style="bold yellow"))
      return
    self.fetching = True
    self.query_one("#ci-title", Static).update(self.title_text())
    def loaded(info):
      self.fetching = False
      if info is not None:
        self.info = info
        if isinstance(self.cluster.overview, dict):
          self.cluster.overview["info"], self.cluster.overview["info_at"] = info, time.time()
      self.show()
    in_background(self, cluster_info, loaded, group="cluster-info",
                  connection=self.cluster.connection if self.cluster.destination else None)

  def show(self):
    self.query_one("#ci-title", Static).update(self.title_text())
    if self.info is None:
      return
    if self.info["failed"]:
      self.shown = Text("Could not query the cluster:\n" + "\n".join(self.info["errors"]), style="red")
    else:
      self.shown = info_grid(self.info)
    self.query_one("#ci-info", Static).update(self.shown)
