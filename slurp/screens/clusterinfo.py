"""The cluster info window"""

import argparse
import asyncio
import codecs
import collections
import fcntl
import functools
import json
import logging
import os
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import humanize
from rich.color import Color
from rich.progress_bar import ProgressBar as RichProgressBar
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, MarkdownViewer, OptionList, ProgressBar, RadioButton, RadioSet, Select, Static, Switch, TextArea, Tree
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual.suggester import SuggestFromList
from textual.worker import get_current_worker

import slurm as sl

from ..state import *
from ..remote import *
from ..widgets import *

NODE_FLAGS = {"*": "not responding", "~": "powered off", "#": "powering up", "!": "powering down", "%": "powering down",
              "$": "maintenance", "@": "pending reboot", "^": "rebooting", "-": "planned"}

def cluster_info():
  """Nodes, partitions, reasons, reservations and the queue of the cluster (one round trip on a remote cluster)"""
  cmds = [["sinfo", "-h", "-N", "-o", "%N|%T"],
          ["sinfo", "-h", "-o", "%P|%a|%F|%l|%D"],
          ["sinfo", "-h", "-R", "-o", "%E|%T|%D|%N|%H"],
          ["squeue", "-h", "-a", "-r", "--format=%T|%D|%r"],
          ["scontrol", "show", "reservation", "-o"]]
  res = run_many(cmds)
  info = {"errors": [f"{shlex.join(c[:3])}: {(r.stderr or '').strip()[:200]}" for c, r in zip(cmds, res) if r.returncode
                     and "No reservations" not in (r.stdout + r.stderr)],
          "time": datetime.now(), "failed": all(r.returncode for r in res[:4])}
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

class ClusterInfoScreen(ModalScreen):
  """Health and occupancy of the cluster of a tab"""
  DEFAULT_CSS = """
  ClusterInfoScreen { align: center middle; }
  #ci-dialog { width: 120; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #ci-title { text-style: bold; height: auto; margin-bottom: 1; }
  #ci-dialog.-good { border: thick $success; }
  #ci-dialog.-warn { border: thick $warning; }
  #ci-dialog.-bad { border: thick $error; }
  #ci-body { height: 1fr; }
  #ci-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #ci-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("r", "reload", "Reload")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster = cluster
    self.info = None
    self.loading = False

  def compose(self) -> ComposeResult:
    with Vertical(id="ci-dialog"):
      yield Static(id="ci-title")
      with VerticalScroll(id="ci-body"):
        yield Static(id="ci-info")
      with Horizontal(id="ci-buttons"):
        yield Button("Reload", id="ci-reload", variant="primary")
        if self.cluster.destination:
          yield Button("Reconnect", id="ci-reconnect")
        yield Button("Close", id="ci-close")

  def on_mount(self):
    self.action_reload()

  def title_text(self):
    state = "loading …" if self.loading else f"as of {self.info['time']:%H:%M:%S}" if self.info else ""
    text = Text(f"Cluster {self.cluster.name} · ")
    dialog = self.query_one("#ci-dialog")
    for level in HEALTH_STYLES:
      dialog.remove_class(f"-{level}")
    if self.info is not None:
      level, why = cluster_health(self.info)
      dialog.add_class(f"-{level}")
      text.append(f"● {dict(good='healthy', warn='degraded', bad='unhealthy')[level]}: {why}", style=HEALTH_STYLES[level])
      text.append(" · ")
    text.append(state)
    return text

  def not_connected(self):
    """Why the tab cannot be asked now (None: it can)"""
    c = self.cluster.connection
    if not self.cluster.destination:
      return None if self.cluster.name else "no tab"
    if c is None or not c.ready:
      state = "not connected" if c is None or c.state in ("new", "closed") else \
              "connecting" if c.state == "connecting" else f"connection lost ({c.error or 'the session ended'})"
      return f"No connection to {self.cluster.destination}: {state}. Reconnect logs in again, Reload asks once connected."
    return None

  @on(Button.Pressed, "#ci-reconnect")
  def reconnect_pressed(self):
    self.app.reconnect(self.cluster)
    self.dismiss()

  @on(Button.Pressed, "#ci-reload")
  def action_reload(self):
    if self.loading:
      return
    why = self.not_connected()
    if why:
      self.query_one("#ci-title", Static).update(Text(f"Cluster {self.cluster.name}"))
      self.query_one("#ci-info", Static).update(Text(why, style="bold yellow"))
      return
    self.loading = True
    self.query_one("#ci-title", Static).update(self.title_text())
    def loaded(info):
      self.loading = False
      if info is not None:
        self.info = info
      self.show()
    in_background(self, cluster_info, loaded, group="cluster-info",
                  connection=self.cluster.connection if self.cluster.destination else None)

  def show(self):
    self.query_one("#ci-title", Static).update(self.title_text())
    info = self.info
    if info is None:
      return
    if info["failed"]:
      self.query_one("#ci-info", Static).update(Text("Could not query the cluster:\n" + "\n".join(info["errors"]), style="red"))
      self.shown = Text("failed")
      return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    def section(title):
      grid.add_row("", "")
      grid.add_row(Text(title, style="bold underline"), "")
    s, total = info["states"], info["nodes"]
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
    if info["reservations"]:
      section("Reservations")
      for r in info["reservations"]:
        flags = r.get("Flags", "")
        style = "bold yellow" if "MAINT" in flags else ""
        grid.add_row(Text(r["ReservationName"], style=style),
                     f"{r.get('StartTime', '?').replace('T', ' ')} – {r.get('EndTime', '?').replace('T', ' ')}, "
                     f"{r.get('NodeCnt', '?')} nodes, {r.get('State', '')} {flags}")
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
    if info["partitions"]:
      section("Partitions")
      table = Table(box=None, padding=(0, 1), header_style="bold")
      for col in ("partition", "avail", "allocated", "idle", "other", "total", "time limit"):
        table.add_column(col, justify="right" if col in ("allocated", "idle", "other", "total") else "left")
      for p in info["partitions"]:
        table.add_row(p["name"], Text(p["avail"], style="" if p["avail"] == "up" else "red"), p["alloc"], p["idle"],
                      p["other"], p["total"], p["limit"])
      grid.add_row("", table)
    for err in info["errors"]:
      grid.add_row(Text("error", style="red"), err)
    self.query_one("#ci-info", Static).update(grid)
    self.shown = grid

  @on(Button.Pressed, "#ci-close")
  def action_close(self):
    self.dismiss()
