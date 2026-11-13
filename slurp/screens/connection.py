"""The connection window of a tab"""

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

def process_tree(pid):
  """pid and its descendants (e.g. the ssh -W of a ProxyJump, which holds the TCP connection)"""
  children = {}
  for entry in os.listdir("/proc"):
    if entry.isdigit():
      try:
        with open(f"/proc/{entry}/stat") as f:
          ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(entry))
      except (OSError, ValueError, IndexError):
        pass
  pids, todo = [], [pid]
  while todo:
    p = todo.pop()
    pids.append(p)
    todo += children.get(p, [])
  return pids

def tcp_connections(pids):
  """TCP sockets of the processes, from ss: [{"local", "peer", "pid", "bytes_sent", "bytes_received", "rtt", ...}]"""
  try:
    out = subprocess.run(["ss", "-tinpH"], capture_output=True, text=True, timeout=5).stdout
  except (OSError, subprocess.TimeoutExpired):
    return None
  conns, header = [], None
  for line in out.splitlines():
    if not line[:1].isspace():
      header = line
      continue
    if header is None:
      continue
    pid = next((p for p in pids if f"pid={p}," in header), None)
    if pid is None:
      continue
    fields = header.split()
    info = {"local": fields[3] if len(fields) > 4 else "?", "peer": fields[4] if len(fields) > 4 else "?", "pid": pid,
            "state": fields[0] if fields else "?"}
    for key, pattern in [("bytes_sent", r"bytes_sent:(\d+)"), ("bytes_received", r"bytes_received:(\d+)"),
                         ("rtt", r"\brtt:([\d.]+)"), ("retrans", r"retrans:\d+/(\d+)"), ("lastrcv", r"lastrcv:(\d+)"),
                         ("lastsnd", r"lastsnd:(\d+)")]:
      m = re.search(pattern, line)
      info[key] = float(m.group(1)) if m else None
    conns.append(info)
  return conns

def ssh_config(destination):
  """What OpenSSH makes of a destination (ssh -G: no connection is made)"""
  try:
    out = subprocess.run(["ssh", "-G", destination], capture_output=True, text=True, timeout=5).stdout
  except (OSError, subprocess.TimeoutExpired):
    return {}
  conf = {}
  for line in out.splitlines():
    key, _, value = line.partition(" ")
    conf.setdefault(key, value)
  return conf

def ago(seconds):
  if seconds is None:
    return "-"
  seconds = int(seconds)
  if seconds < 60:
    return f"{seconds} s"
  if seconds < 3600:
    return f"{seconds // 60} min {seconds % 60} s"
  return f"{seconds // 3600} h {seconds // 60 % 60} min"

class ConnectionScreen(ModalScreen):
  """The SSH session of a tab: state, the course of a (re)connect, traffic and diagnostics; reconnect/disconnect"""
  DEFAULT_CSS = """
  ConnectionScreen { align: center middle; }
  #conn-dialog { width: 110; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #conn-title { text-style: bold; height: 1; margin-bottom: 1; }
  #conn-dialog.-good { border: thick $success; }
  #conn-dialog.-warn { border: thick $warning; }
  #conn-dialog.-bad { border: thick $error; }
  #conn-body { height: 1fr; }
  #conn-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #conn-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("R", "reconnect", "Reconnect")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster = cluster
    self.config, self.tcp, self.tcp_time = None, None, 0.0

  def compose(self) -> ComposeResult:
    with Vertical(id="conn-dialog"):
      yield Static(id="conn-title")
      with VerticalScroll(id="conn-body"):
        yield Static(id="conn-info")
      with Horizontal(id="conn-buttons"):
        if self.cluster.destination:
          yield Button("Reconnect", id="conn-reconnect", variant="primary", tooltip="end the session and log in again (R)")
          yield Button("Disconnect", id="conn-disconnect", variant="error")
        yield Button("Close", id="conn-close")

  def show_title(self):
    text = Text(f"Connection of the tab {self.cluster.name}" + (f" · ssh {self.cluster.destination}" if self.cluster.destination else ""))
    level, words = connection_health(self.cluster.connection if self.cluster.destination else None)
    text.append(f" · ● {words}", style=HEALTH_STYLES[level])
    self.query_one("#conn-title", Static).update(text)
    dialog = self.query_one("#conn-dialog")
    for other in HEALTH_STYLES:
      dialog.set_class(other == level, f"-{other}")

  def on_mount(self):
    self.show_title()
    if self.cluster.destination:
      dest = self.cluster.destination
      in_background(self, lambda: ssh_config(dest), self.got_config, group="ssh-config", connection=None)
    self.set_interval(0.5, self.show)
    self.show()

  def got_config(self, config):
    self.config = config or {}
    self.show()

  def poll_tcp(self):
    """the TCP connection of the ssh process, from ss (every 2 s, in the background)"""
    c = self.cluster.connection
    proc = c.proc if c else None
    if proc is None or proc.poll() is not None or time.monotonic() - self.tcp_time < 2:
      return
    self.tcp_time = time.monotonic()
    pid = proc.pid
    def got(conns):
      self.tcp = conns
    in_background(self, lambda: tcp_connections(process_tree(pid)), got, group="tcp", connection=None)

  def status(self):
    """The course of the current (re)connect, in words"""
    cluster, c = self.cluster, self.cluster.connection
    if c is None or c.state == "new":
      return Text("not connected", style="dim")
    now = time.monotonic()
    if c.state == "ready":
      waiting = c.responding()
      if waiting is not None and waiting > PING_SLOW:
        return Text(f"connected, but no answer to the heartbeat for {waiting:.0f} s "
                    f"(given up after {PING_DEAD} s)", style="bold red")
      return Text(f"connected since {c.ready_at:%Y-%m-%d %H:%M:%S} ({ago((datetime.now() - c.ready_at).total_seconds())})",
                  style="bold green")
    if c.state == "connecting":
      t = cluster.login_terminal
      printed = t is not None and t.vt is not None and any(line.strip() for line in t.vt.display)
      text = Text(f"connecting for {now - c.connecting_since:.0f} s (attempt {max(cluster.attempts, 1)})", style="bold yellow")
      if printed:
        text.append(" · the login may wait for input: answer it in the pane on the right (close this window)",
                    style="yellow")
      return text
    if c.state == "closed":
      return Text("disconnected (closed) · Reconnect logs in again", style="bold")
    delay = self.app.RECONNECT_DELAYS[min(cluster.attempts, len(self.app.RECONNECT_DELAYS) - 1)]
    left = max(0, delay - (now - (cluster.broken_at or now)))
    text = Text(f"failed: {c.error or 'the session ended'}" + (f" (ssh exit code {c.exit_code})" if c.exit_code is not None else ""),
                style="bold red")
    if c.wanted:
      text.append(f" · next attempt in {left:.0f} s" if left >= 1 else " · reconnecting", style="red")
    return text

  def show(self):
    if not self.is_mounted:
      return
    self.show_title()
    cluster, c = self.cluster, self.cluster.connection
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    def section(title):
      grid.add_row("", "")
      grid.add_row(Text(title, style="bold underline"), "")
    if not cluster.destination:
      grid.add_row("Machine", f"local ({os.uname().nodename}), commands run directly, no SSH")
      self.query_one("#conn-info", Static).update(grid)
      return
    self.poll_tcp()
    grid.add_row("Status", self.status())
    if c is not None:
      grid.add_row("Logged in as", f"{c.user or '-'} on {c.hostname or '-'}" + (f" (home {c.home})" if c.home else ""))
      if c.groups:
        grid.add_row("Groups", ", ".join(c.groups))
      grid.add_row("Session", f"number {c.generation}" + (f", started {c.started_at:%H:%M:%S}" if c.started_at else "")
                   + (f", reconnect attempts {cluster.attempts}" if cluster.attempts > 1 else ""))
    conf = self.config
    if conf is None:
      grid.add_row("Destination", f"{cluster.destination} …")
    elif conf:
      target = f"{conf.get('user', '?')}@{conf.get('hostname', '?')}:{conf.get('port', '?')}"
      grid.add_row("Destination", f"{cluster.destination} → {target}")
      for key, label in [("proxyjump", "Proxy jump"), ("identityfile", "Identity file"),
                         ("serveraliveinterval", "Server alive"), ("connecttimeout", "Connect timeout")]:
        value = conf.get(key)
        if value and value != "none":
          if key == "serveraliveinterval":
            value = f"every {value} s, gives up after {conf.get('serveralivecountmax', '?')} missed"
          grid.add_row(label, value)
    if c is not None:
      section("Command channel")
      now = time.monotonic()
      grid.add_row("Traffic", f"{humanize.naturalsize(c.bytes_sent)} sent, {humanize.naturalsize(c.bytes_received)} received, "
                   f"{c.commands} commands (all sessions)")
      grid.add_row("Waiting", f"{len(c.pending)} commands" + (f", last answer {ago(now - c.last_answer)} ago" if c.last_answer else ""))
      if c.latencies:
        grid.add_row("Heartbeat", f"round trip {c.latencies[-1] * 1000:.0f} ms (average {sum(c.latencies) / len(c.latencies) * 1000:.0f} ms, "
                     f"max {max(c.latencies) * 1000:.0f} ms of {len(c.latencies)}), every {PING_INTERVAL} s when idle")
      else:
        grid.add_row("Heartbeat", f"none yet (sent after {PING_INTERVAL} s without an answer from the cluster)")
      section("SSH process")
      proc = c.proc
      if proc is None:
        grid.add_row("Process", "-")
      else:
        code = proc.poll()
        grid.add_row("Process", f"pid {proc.pid}, " + ("running" if code is None else f"ended with exit code {code}"))
      grid.add_row("Control socket", c.control_path)
      if c.argv:
        grid.add_row("Command", shlex.join(c.argv))
      if self.tcp is None:
        grid.add_row("TCP", "…" if proc is not None and proc.poll() is None else "-")
      elif not self.tcp:
        grid.add_row("TCP", "no TCP connection of the ssh process found")
      for conn in self.tcp or []:
        details = [f"{conn['local']} → {conn['peer']} ({conn['state']})"]
        if conn["bytes_sent"] is not None:
          details.append(f"{humanize.naturalsize(conn['bytes_sent'])} sent, {humanize.naturalsize(conn['bytes_received'] or 0)} received "
                         "(the whole connection, shells included)")
        if conn["rtt"] is not None:
          details.append(f"rtt {conn['rtt']:.1f} ms" + (f", {conn['retrans']:.0f} retransmissions" if conn["retrans"] else "")
                         + (f", last data received {conn['lastrcv'] / 1000:.1f} s ago" if conn["lastrcv"] is not None else ""))
        grid.add_row("TCP", "\n".join(details))
      t = cluster.login_terminal
      lines = [line.rstrip() for line in t.vt.display if line.strip()] if t is not None and t.vt is not None else []
      if lines:
        section("Login output")
        for line in lines[-10:]:
          grid.add_row("", Text(line))
      section("Events")
      for when, text in list(c.events)[-25:][::-1]:
        style = "red" if any(w in text for w in ("ended", "no answer", "failed")) else "green" if "connected as" in text else ""
        grid.add_row(f"{when:%H:%M:%S}", Text(text, style=style))
    self.query_one("#conn-info", Static).update(grid)
    self.shown = grid

  @on(Button.Pressed, "#conn-reconnect")
  def action_reconnect(self):
    """Log in again: the window closes and the login comes up in the pane, with the focus"""
    if self.cluster.destination:
      self.app.reconnect(self.cluster)
      self.dismiss()

  @on(Button.Pressed, "#conn-disconnect")
  def disconnect_pressed(self):
    self.app.disconnect(self.cluster)
    self.show()

  @on(Button.Pressed, "#conn-close")
  def action_close(self):
    self.dismiss()
