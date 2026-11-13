"""The app: tabs and their SSH sessions, subtabs, the job list, and main()"""

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

from .state import *
from .remote import *
from .jobs import *
from .details import *
from .widgets import *
from .terminal import *
from .screens.dialogs import *
from .screens.base import *
from .screens.files import *
from .screens.hdf5 import *
from .screens.core import *
from .screens.shell import *
from .screens.job import *
from .screens.main import *
from .screens.connection import *
from .screens.clusterinfo import *
from .storage import *
from .screens.storage import *
from . import remote, state

class Slurp(App):
  TITLE = "slurp"
  CSS = """
  #toolbar { height: auto; padding: 0 1; }
  #toolbar > * { margin-right: 1; }
  #toolbar Label { padding-top: 1; }
  #scope { width: 28; }
  #scratch-bar, #scratch-actions { height: auto; padding: 0 1; }
  #scratch-bar Label { padding: 1 1 0 0; width: auto; }
  #scratch-dir { width: 1fr; }
  #scratch-by { width: 24; }
  #scratch-days { width: 8; }
  #scratch-actions Button { margin-right: 1; }
  #scratch-status { height: auto; padding: 0 1; }
  #scratch-files { height: 1fr; }
  #notabs { display: none; padding: 2 4; color: $text-muted; }
  MainScreen.-no-tabs #notabs { display: block; }
  MainScreen.-no-tabs .split, MainScreen.-no-tabs #loadstatus, MainScreen.-no-tabs #connstatus { display: none; }
  #us { width: auto; height: auto; padding: 0; border: none; }
  #since-choice { width: 18; }
  #since { width: 12; display: none; }
  #since.-custom { display: block; }
  #topbar { height: 1; }
  #cluster-tabs { height: 1; padding: 0 1; background: $panel; }
  #cluster-tabs Button { height: 1; min-height: 1; padding: 0 1; margin-right: 1; }
  #cluster-tabs Button.cluster-tab { margin-right: 0; }
  #view-tabs { height: 1; padding: 0 1; background: $boost; }
  #view-tabs Button { height: 1; min-height: 1; padding: 0 1; margin-right: 0; }
  #view-tabs Button.view-close { width: 3; min-width: 3; padding: 0; margin-right: 1; color: $text-muted; }
  #view-tabs Button.view-close:hover { color: $error; }
  #shell-status { height: 1; padding: 0 1; background: $panel; }
  TerminalScreen TerminalView { border: round $panel-lighten-3; }
  TerminalScreen TerminalView:focus { border: round $accent; }
  MainScreen.-no-tabs #view-tabs { display: none; }
  #cluster-tabs Button.cluster-close { width: 3; min-width: 3; padding: 0; margin-right: 2; color: $text-muted; }
  #cluster-tabs Button.cluster-close:hover { color: $error; }
  #topbar Header { dock: none; width: 1fr; height: 1; }
  #bottombar { dock: bottom; height: 1; }
  #bottombar Footer { dock: none; width: 1fr; }
  #loadstatus { width: auto; height: 1; padding: 0 1; background: $footer-background; color: $warning; }
  #connstatus { width: auto; height: 1; padding: 0 1; background: $footer-background; }
  #connstatus.-good { background: $success 25%; }
  #connstatus.-warn { background: $warning 30%; }
  #connstatus.-bad { background: $error 40%; }
  #connstatus:hover { background: $accent; }
  #back { width: 3; min-width: 3; height: 1; padding: 0; }
  #status { height: 1; padding: 0 1; background: $panel; }
  #jobs { height: 1fr; }
  #details { height: 1fr; padding: 0 1; }
  #memory-controls { height: auto; margin: 1 0; }
  #memory-controls > * { margin-right: 1; }
  #memory-controls Label { width: auto; height: 1; content-align: center middle; padding: 0; }
  #memory-cpu { width: auto; height: 1; }
  #memory-gpu { width: auto; height: auto; }
  #job-progress { height: auto; margin-bottom: 1; padding: 0 1; }
  #progress-bar { width: 40; }
  #progress-top { height: auto; }
  #progress-top Button { margin-left: 2; }
  #progress-line { height: auto; }
  #core-overview { height: 1fr; padding: 1; }
  #core-info { height: auto; }
  #open-gdb { margin-top: 1; }
  #job-actions { height: auto; margin: 1 0; }
  #job-actions Button { margin-right: 1; }
  .section { margin-top: 1; text-style: bold; color: $accent; }
  #files { height: auto; max-height: 16; }
  #steps { height: auto; max-height: 14; }
  #nodes { height: auto; }
  #nodebar, #presets { height: auto; }
  #cmd { width: 1fr; }
  #node { width: 28; }
  #presets Button { margin-right: 1; min-width: 8; }
  #fstatus { height: 1; padding: 0 1; background: $panel; }
  #text { height: 1fr; }
  #search, #dfilter, #jfilter { dock: bottom; display: none; }
  #search.visible, #dfilter.visible, #jfilter.visible { display: block; }
  #dstatus { height: 1; padding: 0 1; background: $panel; }
  #dir { height: 1fr; }
  #hstatus, #hpage { height: 1; padding: 0 1; background: $panel; }
  #hbody { height: 1fr; }
  #htree { width: 40%; height: 1fr; border-right: vkey $panel-lighten-2; }
  #hright { width: 1fr; height: 1fr; }
  #hinfo { height: auto; max-height: 45%; padding: 0 1; }
  #hdata { height: 1fr; }
  .split { height: 1fr; }
  .left { width: 1fr; height: 1fr; }
  TerminalPane { width: 50%; height: 1fr; display: none; border: round $panel-lighten-3; }
  TerminalPane:focus-within { border: round $accent; }
  Splitter { width: 1; height: 1fr; display: none; background: $panel-lighten-2; }
  Splitter:hover, Splitter.-dragging { background: $accent; }
  .tbar { height: 1; background: $panel; }
  TerminalPane:focus-within .tbar { background: $accent; color: $text; }
  .ttitle { width: 1fr; height: 1; padding: 0 1; }
  .tclose { width: 3; min-width: 3; height: 1; }
  """
  BINDINGS = [
    Binding("q", "quit", "Quit"),
    Binding("r", "reload", "Reload"),
    Binding("t", "cluster_shell", "Shell"),
    Binding("d", "login_dir", "Home dir"),
    Binding("i", "interactive_job", "Interactive job"),
    Binding("C", "cluster_info", "Cluster info"),
    Binding("Q", "quota", "Quota"),
    Binding("X", "scratch", "Old scratch files"),
    Binding("a", "abort_load", "Abort loading"),
    Binding("c", "connection", "Connection"),
    Binding("ctrl+w", "close_view", "Close subtab"),
    Binding(SWITCH_KEY, "switch_side", "Shell ⇄", key_display="^]"),
    Binding("ctrl+x", "close_shell", "Close shell"),
  ]
  shell_version = reactive(0) # bumped when the shell is opened, closed or ends: the panes update
  pane_width = reactive(0)    # width of the terminal pane in columns, 0: half of the window

  def __init__(self):
    super().__init__()
    self.clusters = [ClusterSession("Local")]
    for destination in state.initial_ssh_destinations:
      if is_local_destination(destination):
        continue
      self.clusters.append(ClusterSession(destination, destination))
    self.cluster_index = 0
    if state.initial_ssh_destinations and not is_local_destination(state.initial_ssh_destinations[0]):
      self.cluster_index = 1
    self.shell = None # TerminalSession shown on the right of every window
    self.sort_column, self.sort_reverse = "jobid", True
    self.job_filter = ""
    self.job_column_keys = None
    self.spinner_frame = 0

  def get_default_screen(self):
    return MainScreen()

  def on_mount(self):
    self.set_interval(0.25, self.tick)

  def on_unmount(self):
    self.shutdown()

  def shutdown(self):
    """End the shells, logins and remote shells of all tabs"""
    for cluster in self.clusters:
      for view in cluster.views:
        if view.session is not None:
          view.session.terminate()
      for terminal in (cluster.terminal, cluster.login_terminal):
        if terminal:
          terminal.terminate()
      if self.shell and self.shell in (cluster.terminal, cluster.login_terminal):
        self.shell = None
      if cluster.connection:
        cluster.connection.close()
    if self.shell:
      self.shell.terminate()

  # ---- tabs: the local machine and SSH destinations, each with its own connection, shell and job list
  @property
  def cluster(self):
    """The active tab (an empty stand-in after the last tab was closed)"""
    return self.clusters[self.cluster_index] if self.clusters else self.no_cluster

  no_cluster = ClusterSession("")

  @property
  def jobs(self):
    return self.cluster.jobs

  @property
  def ssh(self):
    return self.cluster.connection

  def login(self, cluster, show=True):
    """Start the SSH session of a tab. Its prompts appear in the terminal pane: right away if show (the user selected
    the tab), else only if the login takes more than a moment (an automatic reconnect that needs a password)."""
    if cluster.connection is None:
      cluster.connection = RemoteSSH(cluster.destination)
    c = cluster.connection
    c.wanted = True
    if cluster.login_terminal and not cluster.login_terminal.done:
      cluster.login_terminal.terminate()
    cluster.login_terminal = TerminalSession(self, [], f"SSH login: {cluster.destination}", login=c)
    cluster.login_terminal.ensure(100, 30) # starts it now, also while it is not shown
    cluster.show_login = show
    cluster.attempts += 1
    cluster.ready_generation = None

  def needs_login(self, cluster):
    """A remote tab without a live session (none yet, closed, or broken with the login process gone)"""
    if not cluster.destination:
      return False
    c = cluster.connection
    if c is None or c.state in ("new", "closed"):
      return True
    return c.state == "broken" and (cluster.login_terminal is None or cluster.login_terminal.done)

  def activate_cluster(self, index):
    """Show the tab: its job list as loaded before (no reload), its shell, or its login while not logged in"""
    if not 0 <= index < len(self.clusters):
      return
    cluster = self.clusters[index]
    if self.needs_login(cluster):
      cluster.attempts = 0
      self.login(cluster)
    self.cluster_index = index
    remote.ssh_connection = cluster.connection
    if self.current_mode != cluster.current_view:
      self.switch_mode(cluster.current_view if any(v.mode == cluster.current_view for v in cluster.views) else "_default")
    self.show_tab_shell()
    self.update_scope_select()
    self.update_since_widgets()
    self.populate()
    self.refresh_status()
    if not cluster.loaded and cluster.loader is None and (not cluster.destination or cluster.connection.ready):
      self.action_reload()

  def login_visible(self, cluster):
    """The login is shown while it may need the user: one the user started, or an automatic reconnect that printed
    something (e.g. a password prompt) and takes more than a moment; and a failed login the user started, so that
    its messages can be read (Enter there logs in again)"""
    t, c = cluster.login_terminal, cluster.connection
    if t is None or c is None or c.ready:
      return False
    if t.done:
      return cluster.show_login
    if c.state != "connecting":
      return False
    printed = t.vt is not None and any(line.strip() for line in t.vt.display)
    return cluster.show_login or printed and time.monotonic() - c.connecting_since > 1

  def show_tab_shell(self):
    """The pane shows the login of the active tab while it needs the user, else the shell of the tab"""
    cluster = self.cluster
    login = cluster.login_terminal if self.login_visible(cluster) else None
    want = login
    if want is self.shell:
      return
    self.shell = want
    self.shell_changed()
    if login is not None:
      cluster.show_login = True # stays until the login is done
      self.call_after_refresh(self.focus_terminal)
    elif isinstance(self.focused, TerminalView) or self.focused is None:
      self.call_after_refresh(self.focus_left)

  def select_cluster(self, index):
    if index == self.cluster_index:
      cluster = self.cluster
      if self.needs_login(cluster):
        self.activate_cluster(index)
      return
    self.activate_cluster(index)

  # ---- subtabs: the job list (the default mode) and the views of the active tab, each a mode with its own windows
  next_view_id = 0

  def find_view(self, key):
    return next((v for v in self.cluster.views if key and v.key == key), None)

  def open_view(self, title, make_screen, kind="window", key="", session=None):
    """A new subtab of the active tab (or the one that shows key already)"""
    if not self.clusters:
      return
    view = self.find_view(key)
    if view is None:
      type(self).next_view_id += 1
      view = View(type(self).next_view_id, title, kind, key, session)
      self.add_mode(view.mode, make_screen)
      self.cluster.views.append(view)
    self.show_view(view)

  def open_window(self, screen, title=""):
    """A window: from the job list it opens as a subtab, within a subtab it opens on top of it (← goes back)"""
    if self.current_mode == "_default" and not isinstance(self.screen, ModalScreen):
      crumbs = getattr(screen, "crumbs", None)
      self.open_view(title or (crumbs[-1] if crumbs else "") or type(screen).__name__, lambda: screen)
    else:
      self.push_screen(screen)

  def show_view(self, view):
    """Show a subtab of the active tab (None: the job list)"""
    mode = view.mode if view is not None else "_default"
    self.cluster.current_view = mode
    if self.current_mode != mode:
      self.switch_mode(mode)
    self.call_after_refresh(self.refresh_status)

  def close_view(self, view, cluster=None):
    """Close a subtab: its windows go away, its shell ends"""
    cluster = cluster or self.cluster
    if view not in cluster.views:
      return
    if view.session is not None:
      view.session.terminate()
    cluster.views.remove(view)
    if self.current_mode == view.mode:
      self.show_view(None)
    if cluster.current_view == view.mode:
      cluster.current_view = "_default"
    def remove():
      try:
        self.remove_mode(view.mode)
      except Exception as e:
        log.debug(f"removing the subtab {view.title}: {e}")
    self.call_after_refresh(remove)
    self.refresh_status()

  def action_close_view(self):
    view = next((v for v in self.cluster.views if v.mode == self.current_mode), None)
    if view is not None:
      self.close_view(view)

  def refresh_view_tabs(self, screen):
    bar = screen.query("#view-tabs").first(Horizontal) if screen.query("#view-tabs") else None
    jobs = bar.query("#view-jobs") if bar is not None else None
    if not jobs:
      return
    jobs.first().variant = "primary" if self.current_mode == "_default" else "default"
    wanted = {str(v.id) for v in self.cluster.views}
    have = set()
    for b in bar.query("Button.view-tab, Button.view-close"): # remove only the buttons of closed subtabs (ids stay unique)
      if b.id == "view-jobs":
        continue
      vid = b.id.rsplit("-", 1)[1]
      if vid not in wanted or getattr(b, "removing", False):
        if not getattr(b, "removing", False):
          b.removing = True
          b.remove()
      else:
        have.add(vid)
    for v in self.cluster.views:
      if str(v.id) not in have:
        bar.mount(Button(v.title[:40], id=f"view-tab-{v.id}", classes="view-tab", compact=True, tooltip=v.title),
                  Button("✕", id=f"view-close-{v.id}", classes="view-close", compact=True, tooltip="close the subtab"))
    for v in self.cluster.views:
      for b in bar.query(f"#view-tab-{v.id}"):
        b.variant = "primary" if self.current_mode == v.mode else "default"
        label = ("▸ " if v.kind == "shell" else "") + v.title[:40] + (" (ended)" if v.session is not None and v.session.done else "")
        if str(b.label) != label:
          b.label = label

  def confirm_close_cluster(self, index):
    if not 0 <= index < len(self.clusters):
      return
    cluster = self.clusters[index]
    what = ["its job list"]
    if cluster.destination:
      what.insert(0, "its SSH session")
    shells = sum(v.session is not None and not v.session.done for v in cluster.views)
    if shells:
      what.append(f"its {shells} shell{'s' if shells > 1 else ''}")
    if len(cluster.views) > shells:
      what.append(f"its other subtabs ({len(cluster.views) - shells})")
    self.push_screen(ConfirmScreen(f"Close the tab {cluster.name}?", f"This ends {', '.join(what[:-1])} and {what[-1]}."
                                   if len(what) > 1 else f"This ends {what[0]}.", "Close tab"),
                     lambda ok: self.close_cluster(cluster) if ok else None)

  def close_cluster(self, cluster):
    """Close a tab: its loads, shells and SSH session end"""
    if cluster not in self.clusters:
      return
    index = self.clusters.index(cluster)
    current = cluster is self.cluster
    if cluster.loader is not None:
      cluster.loader.abort.set()
      cluster.loader.cancel()
      cluster.loader = None
    for terminal in (cluster.terminal, cluster.login_terminal):
      if terminal is not None:
        terminal.terminate()
    if cluster.connection is not None:
      cluster.connection.wanted = False
      cluster.connection.close()
    if self.shell is not None and self.shell in (cluster.terminal, cluster.login_terminal):
      self.shell = None
      self.shell_changed()
    if current and self.current_mode != "_default":
      self.switch_mode("_default")
    for view in list(cluster.views):
      self.close_view(view, cluster)
    self.clusters.pop(index)
    if current:
      if self.clusters:
        self.activate_cluster(min(index, len(self.clusters) - 1))
      else: # no tab left: only the + button
        self.cluster_index = 0
        remote.ssh_connection = None
        self.populate()
    elif index < self.cluster_index:
      self.cluster_index -= 1
    self.main.set_class(not self.clusters, "-no-tabs")
    self.refresh_status()

  def add_cluster(self, destination):
    if destination is None:
      return
    self.main.remove_class("-no-tabs")
    if not destination or is_local_destination(destination):
      self.clusters.append(ClusterSession("Local" if not destination else destination))
    else:
      self.clusters.append(ClusterSession(destination, destination))
    self.refresh_cluster_tabs()
    self.select_cluster(len(self.clusters) - 1)

  RECONNECT_DELAYS = (0, 5, 15, 30, 60) # seconds before the 1st, 2nd, ... attempt to reconnect a broken session

  def action_connection(self):
    if not isinstance(self.screen, ConnectionScreen):
      self.push_screen(ConnectionScreen(self.cluster), lambda _: self.connection_window_closed())

  def connection_window_closed(self):
    """A login started in the window waits in the pane: give it the keys"""
    if self.shell is not None and self.shell is self.cluster.login_terminal and not self.shell.done:
      self.call_after_refresh(self.focus_terminal)

  def reconnect(self, cluster):
    """End the session of a tab (if any) and log in again"""
    if cluster.connection is not None:
      cluster.connection.event("reconnect requested")
    if cluster.login_terminal is not None:
      cluster.login_terminal.terminate()
    cluster.attempts, cluster.broken_at = 0, None
    self.login(cluster, show=True)
    if cluster is self.cluster:
      remote.ssh_connection = cluster.connection
      self.show_tab_shell()
    self.refresh_status()

  def disconnect(self, cluster):
    """End the session of a tab; it stays disconnected until the tab is selected or Reconnect is pressed"""
    c = cluster.connection
    if c is None:
      return
    c.wanted = False
    c.close()
    if cluster.login_terminal is not None:
      cluster.login_terminal.terminate()
    cluster.show_login = False
    if self.shell is cluster.login_terminal:
      self.shell = cluster.terminal
      self.shell_changed()
    self.refresh_status()

  def tick(self):
    """Every 0.25 s (only reads state, never waits): notice logins and broken sessions, reconnect, update the status"""
    self.spinner_frame += 1
    now = time.monotonic()
    for cluster in self.clusters:
      c = cluster.connection
      if c is None or not cluster.destination:
        continue
      t = cluster.login_terminal
      if t is not None and t.done and not getattr(t, "recorded", False):
        t.recorded = True
        lines = [line.strip() for line in t.vt.display if line.strip()] if t.vt is not None else []
        if lines and c.state != "closed":
          c.event("ssh said: " + " | ".join(lines[-3:]))
      if c.ready and cluster.ready_generation != c.generation: # (re)connected
        cluster.ready_generation, cluster.attempts, cluster.show_login = c.generation, 0, False
        cluster.broken_at = None
        self.notify(f"connected to {c.identity}", timeout=3)
        if not cluster.loaded and cluster.loader is None:
          self.reload_cluster(cluster)
      elif c.state == "broken" and c.wanted and (cluster.login_terminal is None or cluster.login_terminal.done):
        if cluster.broken_at is None:
          cluster.broken_at = now
          log.warning(f"session {c.destination} broke: {c.error}")
        delay = self.RECONNECT_DELAYS[min(cluster.attempts, len(self.RECONNECT_DELAYS) - 1)]
        if now - cluster.broken_at >= delay:
          cluster.broken_at = now
          self.login(cluster, show=False)
    self.show_tab_shell()
    self.refresh_status()

  def spinner(self):
    return "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self.spinner_frame % 10]

  def connection_text(self, cluster):
    """State of the SSH session of a tab, for the footer"""
    if not cluster.destination:
      return Text("local", style="dim")
    c = cluster.connection
    if c is None or c.state == "new":
      return Text(f"SSH {cluster.destination}: not connected", style="dim")
    if c.state == "closed":
      return Text(f"SSH {cluster.destination}: closed · select the tab to log in", style="yellow")
    if c.ready:
      waiting = c.responding()
      if waiting is not None and waiting > PING_SLOW:
        return Text(f"{self.spinner()} SSH {c.identity}: no answer for {waiting:.0f} s", style="bold red")
      latency = f" · {c.latency * 1000:.0f} ms" if c.latency is not None else ""
      level, words = connection_health(c)
      return Text(f"● SSH {c.identity}{latency}" + ("" if level == "good" else f" · {words}"), style=HEALTH_STYLES[level])
    if c.state == "connecting":
      what = "log in on the right" if self.shell is cluster.login_terminal else "connecting"
      attempt = f" (attempt {cluster.attempts})" if cluster.attempts > 1 else ""
      return Text(f"{self.spinner()} SSH {cluster.destination}: {what}{attempt}", style="yellow")
    # broken
    reason = f" ({c.error})" if c.error else ""
    if not c.wanted:
      return Text(f"SSH {cluster.destination}: disconnected{reason} · select the tab to log in", style="red")
    delay = self.RECONNECT_DELAYS[min(cluster.attempts, len(self.RECONNECT_DELAYS) - 1)]
    left = max(0, delay - (time.monotonic() - (cluster.broken_at or time.monotonic())))
    return Text(f"{self.spinner()} SSH {cluster.destination}: connection lost{reason}, reconnecting"
                + (f" in {left:.0f} s" if left >= 1 else ""), style="bold red")

  def load_text(self, cluster):
    if cluster.loader is None:
      return Text("")
    waiting = " (waiting for the connection)" if cluster.connection and not cluster.connection.ready else ""
    return Text(f"{self.spinner()} loading jobs {time.monotonic() - cluster.load_started:.0f} s{waiting} · a: abort",
                style="yellow")

  def tab_label(self, cluster):
    c = cluster.connection
    mark = "" if not cluster.destination else " ●" if c and c.ready and (c.responding() or 0) <= PING_SLOW else \
           " ○" if c is None or c.state in ("new", "closed") else " ◌"
    return cluster.name + mark + (" ⟳" if cluster.loader is not None else "")

  def refresh_cluster_tabs(self):
    for screen in dict.fromkeys([self.main, *self.screen_stack]):
      if not isinstance(screen, SplitScreen) or not screen.is_mounted:
        continue
      tabs = screen.query("#cluster-tabs").first(Horizontal) if screen.query("#cluster-tabs") else None
      add = tabs.query("#cluster-add") if tabs is not None else None
      if not add: # a window that is being built or closed
        continue
      existing = {button.id: button for button in tabs.query("Button.cluster-tab")}
      for index, cluster in enumerate(self.clusters):
        variant = "primary" if index == self.cluster_index else "default"
        label = self.tab_label(cluster)
        button_id = f"cluster-tab-{index}"
        if button_id in existing:
          button = existing[button_id]
          if button.variant != variant:
            button.variant = variant
          if str(button.label) != label:
            button.label = label
        else:
          tabs.mount(Button(label, id=button_id, classes="cluster-tab", variant=variant, compact=True),
                     Button("✕", id=f"cluster-close-{index}", classes="cluster-close", compact=True, tooltip="close the tab"),
                     before=add.first())
      for button in tabs.query("Button.cluster-tab, Button.cluster-close"): # of closed tabs
        if int(button.id.rsplit("-", 1)[1]) >= len(self.clusters):
          button.remove()

  def refresh_status(self):
    """Tabs, the footer (state of the session of the active tab and of a load) and the status line of the job table"""
    self.refresh_cluster_tabs()
    for screen in dict.fromkeys([self.main, *self.screen_stack]):
      if isinstance(screen, SplitScreen) and screen.is_mounted:
        self.refresh_view_tabs(screen)
    cluster = self.cluster
    loading, connection = self.load_text(cluster), self.connection_text(cluster)
    for screen in dict.fromkeys([self.main, *self.screen_stack]):
      if not isinstance(screen, SplitScreen) or not screen.is_mounted:
        continue
      for widget in screen.query("#loadstatus"):
        widget.update(loading)
      level = connection_health(cluster.connection)[0] if cluster.destination else "good"
      for widget in screen.query("#connstatus"):
        widget.update(connection)
        for other in HEALTH_STYLES:
          widget.set_class(other == level and bool(cluster.destination), f"-{other}")
      if isinstance(screen, MainScreen) and screen.query("#reload"):
        screen.query_one("#status", Static).update(Text(cluster.summary))
        button = screen.query_one("#reload", Button)
        label, variant = ("Abort", "error") if cluster.loader is not None else ("Reload", "primary")
        if str(button.label) != label:
          button.label, button.variant = label, variant

  def mark_sort_column(self, table):
    """▲ (ascending) or ▼ (descending) after the label of the column the list is sorted by"""
    labels = dict(COLUMNS)
    for key, column in table.columns.items():
      label = labels.get(key.value, str(column.label))
      if key.value == self.sort_column:
        label += " ▼" if self.sort_reverse else " ▲"
      elif key.value == "jobid" and self.sort_column == "arrayid": # by array index: hollow marks
        label += " ▽ array" if self.sort_reverse else " △ array"
      if str(column.label) != label:
        column.label = Text(label)
        column.content_width = max(column.content_width, len(label))
    table.refresh()

  def setup_job_columns(self, show_progress=None):
    table = self.query_one("#jobs", ClickTable)
    all_accounts = self.cluster.scope != "me"
    if show_progress is None:
      show_progress = any(progress_for_job(job) is not None for job in self.jobs.values())
    visible_columns = [(key, label) for key, label in COLUMNS
                       if (key != "user" or all_accounts) and (key not in ("progress", "eta") or show_progress)]
    column_keys = [key for key, _ in visible_columns]
    if column_keys == self.job_column_keys:
      return
    if not all_accounts and self.sort_column == "user":
      self.sort_column, self.sort_reverse = "jobid", True
    if not show_progress and self.sort_column in ("progress", "eta"):
      self.sort_column, self.sort_reverse = "jobid", True
    table.clear(columns=True)
    for key, label in visible_columns:
      if key == "progress":
        table.add_column(label, key=key, width=10)
      else:
        table.add_column(label, key=key)
    self.job_column_keys = column_keys

  # ---- the shell on the right
  def shell_changed(self):
    self.shell_version += 1

  def refresh_terminal(self):
    for view in self.screen.query(TerminalView):
      view.refresh()

  def open_shell(self, args, title, focus=True, via=CURRENT):
    """A shell in a subtab of its own. It runs on the cluster of the active tab, over its SSH connection."""
    via = self.ssh if via is CURRENT else via
    if via is not None and not via.ready:
      self.notify("log in first", severity="warning")
      return
    same = sum(v.kind == "shell" and v.title.split(" (")[0] == title for v in self.cluster.views)
    title = f"{title} ({same + 1})" if same else title
    session = TerminalSession(self, args, title, via=via)
    self.open_view(title, lambda: TerminalScreen(session), kind="shell", session=session)

  def action_cluster_shell(self):
    cluster = self.cluster
    if cluster.destination:
      self.open_shell([], f"shell on {cluster.connection.identity if cluster.connection else cluster.destination}", focus=True)
    else:
      self.open_shell([os.environ.get("SHELL") or "bash", "-l"], "local shell", focus=True)

  def action_login_dir(self):
    """Browse the login directory of the active cluster."""
    cluster = self.cluster
    if cluster.destination and (cluster.connection is None or not cluster.connection.ready):
      self.notify(f"{cluster.name}: log in first", severity="warning")
      return
    path = cluster.connection.home if cluster.connection else os.path.expanduser("~")
    if not path:
      self.notify(f"{cluster.name}: login directory is not available yet", severity="warning")
      return
    open_path(self, [cluster.name], path, [path])

  def restart_shell(self):
    if not self.shell:
      return
    if self.shell.login is not None: # the login ended (e.g. a wrong password): log in again
      cluster = next((c for c in self.clusters if c.login_terminal is self.shell), None)
      if cluster is not None:
        self.login(cluster)
        if cluster is self.cluster:
          remote.ssh_connection = cluster.connection
          self.shell = cluster.login_terminal
          self.shell_changed()
          self.call_after_refresh(self.focus_terminal)
      return
    args, title, via = self.shell.args, self.shell.title, self.shell.via
    self.shell.terminate()
    self.shell = None
    self.open_shell(args, title, focus=True, via=via)

  def action_close_shell(self):
    self.close_shell()

  def close_shell(self):
    if not self.shell:
      return
    cluster = self.cluster
    if self.shell is cluster.login_terminal: # closing the login gives up the connection
      self.disconnect(cluster)
      cluster.login_terminal = None
      self.shell = None
      self.shell_changed()
      self.call_after_refresh(self.focus_left)
      self.refresh_status()
      return
    if cluster.terminal is self.shell:
      cluster.terminal = None
    self.shell.terminate()
    self.shell = None
    self.shell_changed()
    self.call_after_refresh(self.focus_left)
    self.refresh_status()

  def focus_terminal(self):
    views = self.screen.query(TerminalView)
    if views and self.shell:
      if not isinstance(self.focused, TerminalView):
        self.screen.left_focus = self.focused
      views.first().focus()

  def focus_left(self):
    widget = getattr(self.screen, "left_focus", None)
    if widget is None or not widget.is_attached or widget.screen is not self.screen or not widget.focusable:
      chain = [w for w in self.screen.focus_chain if not w.has_class("tclose") and not isinstance(w, TerminalView)]
      widget = chain[0] if chain else None
    if widget is not None:
      widget.focus()

  def action_switch_side(self):
    if isinstance(self.focused, TerminalView):
      self.focus_left()
    else:
      self.focus_terminal()

  async def action_quit(self):
    # Stop mouse and focus reports right away, and clear the screen while still on the alternate screen:
    # terminals that switch back to the previous content are not affected, in terminals (or GNU screen / tmux
    # setups) without an alternate screen the last frame of the app would otherwise stay on the screen.
    if self._driver is not None:
      self._driver.write(REPORTS_OFF + "\x1b[0m\x1b[2J\x1b[H")
      self._driver.flush()
    self.exit()

  async def on_event(self, event):
    if isinstance(event, events.Key) and event is not getattr(self, "_logged_key", None):
      self._logged_key = event # the app sees a key twice: on its way to the focused widget and bubbling back
      # with --log: when do keys arrive (e.g. a lone Esc held back by the terminal until the next key)
      log.debug(f"key {event.key!r} in {type(self.screen).__name__} ({len(self.screen_stack)} windows open)")
    await super().on_event(event)

  def check_action(self, action, parameters):
    if not self.clusters and action not in ("quit", "command_palette", "help_quit"):
      return False # no tab: only + (and quit)
    if action == "reload": # a key of the job table, not of the other windows
      return self.screen is self.main
    if action in ("switch_side", "close_shell"):
      return self.shell is not None
    if action == "abort_load":
      return self.cluster.loader is not None
    if action == "close_view":
      return self.current_mode != "_default"
    if action in ("quit", "command_palette", "help_quit") and isinstance(self.focused, TerminalView):
      return False # Ctrl-Q, Ctrl-P, Ctrl-C go to the shell
    return True

  def show_warnings(self):
    for tab, msg in dict.fromkeys(collector.drain()):
      self.notify(msg, title=tab, severity="warning", timeout=8)

  def notify(self, message, *, title="", severity="information", timeout=None, markup=True):
    """Every notification names its tab (the active one unless the caller says which)"""
    kwargs = {} if timeout is None else {"timeout": timeout}
    super().notify(message, title=title or self.cluster.name, severity=severity, markup=markup, **kwargs)

  def action_reload(self):
    self.reload_cluster(self.cluster)

  def reload_cluster(self, cluster):
    """Load the job list of a tab in the background; the list loaded before stays until the new one is there"""
    value = self.since_value
    if value.lower() in ("", "0", "off", "no", "none"):
      since = None
    else:
      try:
        since = sl.parse_duration(value)
      except sl.argparse.ArgumentTypeError as e:
        self.notify(str(e), severity="error")
        return
    if cluster.destination and cluster.connection is None:
      self.notify(f"not connected to {cluster.destination}", severity="warning")
      return
    scope = cluster.scope
    if cluster.loader is not None:
      cluster.loader.abort.set()
    cluster.load_started = time.monotonic()
    cluster.loader = start_worker(self, lambda: self.load_worker(cluster, since, scope), f"jobs-{id(cluster)}",
                                  connection=cluster.connection, name=f"loading the jobs of {cluster.name}")
    self.refresh_status()

  def load_worker(self, cluster, since, scope):
    worker = get_current_worker()
    jobs, accounts = None, cluster.accounts
    try:
      if accounts is None:
        accounts = slurm_accounts()
      jobs = annotate(load_jobs(since, scope, accounts))
    finally:
      self.call_from_thread(self.jobs_loaded, cluster, worker, jobs, since, accounts)

  def jobs_loaded(self, cluster, worker, jobs, since, accounts=None):
    if accounts is not None and cluster.accounts is None:
      cluster.accounts = accounts
      if cluster is self.cluster:
        self.update_scope_select()
    if cluster.loader is not worker: # aborted or replaced by a newer reload
      return
    cluster.loader = None
    if jobs is not None:
      cluster.jobs = {job.jid: job for job in jobs}
      cluster.since, cluster.loaded, cluster.updated = since, True, datetime.now()
      if cluster is self.cluster:
        self.populate()
    self.show_warnings()
    self.refresh_status()

  def action_abort_load(self):
    cluster = self.cluster
    if cluster.loader is None:
      return
    cluster.loader.abort.set()
    cluster.loader.cancel()
    cluster.loader = None
    self.notify("loading aborted: the list is the one loaded before", timeout=4)
    self.refresh_status()

  def populate(self, jobs=None):
    table = self.query_one("#jobs", ClickTable)
    cluster = self.cluster
    current = None
    if table.row_count:
      try:
        current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
      except Exception:
        pass
    all_jobs = list(cluster.jobs.values())
    self.setup_job_columns()
    pattern = self.job_filter.casefold()
    visible_jobs = [job for job in all_jobs if not pattern or pattern in " ".join(str(value or "") for value in (
      job.jid, job.rec.get("JobName"), job.state, job.rec.get("User"), job.rec.get("Account"),
      job.rec.get("Partition"), job.rec.get("NodeList"), job.rec.get("WorkDir"), job.command,
      job.rec.get("SubmitLine"), job.rec.get("ExitCode"),
    )).casefold()]
    table.clear()
    self.mark_sort_column(table)
    for job in sorted(visible_jobs, key=SORT_KEYS[self.sort_column], reverse=self.sort_reverse):
      rec = job.rec
      state = job.state if job.queued and job.state != "PENDING" else \
              f"{job.state} ({job.reason})" if job.queued else f"{job.state} ({rec['ExitCode']})"
      start = fmt_time(rec["Start"])
      values = [job.jid, rec["JobName"]]
      if cluster.scope != "me":
        values.append(rec["User"])
      values.append(state_text(state))
      if "progress" in self.job_column_keys:
        progress = progress_for_job(job)
        values.append(RichProgressBar(total=max(progress.total, 1), completed=progress.current, width=8) if progress else "")
        eta = best_eta(job.progress) if job.running and job.progress and job.progress.current < job.progress.total else None
        values.append(Text(duration_text(eta - time.time()) if eta and eta > time.time() else "overdue" if eta else "",
                           style="" if not eta or eta > time.time() else "yellow"))
      values.extend((rec["NNodes"], f"{rec['Elapsed']} / {rec['Timelimit']}",
                     ("~" + start) if start and job.state == "PENDING" else start, fmt_time(rec["End"]), err_text(job.err),
                     job.command[:150]))
      table.add_row(*values, key=job.jid)
    if current in {job.jid for job in visible_jobs}:
      table.move_cursor(row=table.get_row_index(current))
    if self.screen is self.main and (self.focused is None or not isinstance(self.focused, (Input, TerminalView, TextArea))):
      table.focus() # an unfocused table is drawn in other colours (e.g. after a tab switch or a reload)
    if not cluster.loaded:
      cluster.summary = "no jobs loaded yet" if not cluster.destination or (cluster.connection and cluster.connection.ready) \
                        else "log in to load the jobs"
      self.refresh_status()
      return
    queued = [j for j in all_jobs if j.queued]
    running = sum(j.running for j in queued)
    finished = [j for j in all_jobs if not j.queued]
    errors = sum(j.err[0] == "file" and j.err[1] > 0 for j in all_jobs)
    status = f"{len(queued)} queued ({running} running, {len(queued)-running} pending)"
    if cluster.since:
      status += f" · {len(finished)} finished since {cluster.since:%m-%d %H:%M}, {sum(j.node_hours() for j in finished):.1f} node hours"
    if pattern:
      status += f" · {len(visible_jobs)} of {len(all_jobs)} jobs match '{self.job_filter}'"
    if errors:
      status += f" · {errors} with output in their error file"
    cluster.summary = status + f" · updated {cluster.updated:%H:%M:%S}"
    self.refresh_status()

  @on(Button.Pressed, "#reload")
  def reload_pressed(self, event):
    if self.cluster.loader is not None:
      self.action_abort_load()
    else:
      self.action_reload()

  @on(Button.Pressed, "#quota")
  def quota_pressed(self, event):
    self.action_quota()

  def action_quota(self):
    if not isinstance(self.screen, QuotaScreen):
      self.push_screen(QuotaScreen(self.cluster))

  @on(Button.Pressed, "#scratch")
  def scratch_pressed(self, event):
    self.action_scratch()

  def action_scratch(self):
    cluster = self.cluster
    if cluster.destination and not (cluster.connection and cluster.connection.ready):
      self.notify("log in first", severity="warning")
      return
    self.open_view("old files in scratch", lambda: ScratchScreen(cluster), key="scratch")

  @on(Button.Pressed, "#cluster-info")
  def cluster_info_pressed(self, event):
    self.action_cluster_info()

  def action_cluster_info(self):
    cluster = self.cluster
    if not isinstance(self.screen, ClusterInfoScreen):
      self.push_screen(ClusterInfoScreen(cluster))

  @on(Button.Pressed, "#interactive-job")
  def interactive_job_pressed(self, event):
    self.action_interactive_job()

  def action_interactive_job(self):
    """Submit a job that only holds its nodes; once it runs, its job window opens shells on them"""
    cluster = self.cluster
    if cluster.destination and not (cluster.connection and cluster.connection.ready):
      self.notify("log in first", severity="warning")
      return
    explanation = ("The job runs 'sleep infinity' and so holds its nodes until its time limit or until it is cancelled. "
                   "Once it runs, open it in the job list: Shell gives a shell on its node. Change the options as needed "
                   "(partition, account, nodes, time, GPUs).")
    self.push_screen(InteractiveJobScreen(f"Interactive job on {cluster.name}", explanation, cluster.accounts,
                                          cluster.connection if cluster.destination else None),
                     lambda args: self.submit_job(cluster, args) if args else None)

  def submit_job(self, cluster, args):
    home = cluster.connection.home if cluster.connection else os.path.expanduser("~")
    def submit():
      return run_cmd(args, timeout=60, env=SLURM_ENV, cwd=home or None, retry=False) # never twice
    def submitted(res):
      if res is None:
        return
      if res.returncode:
        self.notify(f"sbatch failed: {(res.stderr or res.stdout).strip()[:300]}", title=cluster.name, severity="error", timeout=12)
        return
      jid = (re.findall(r"\d+", res.stdout) or ["?"])[-1]
      self.notify(f"submitted interactive job {jid}", title=cluster.name, timeout=6)
      if cluster is self.cluster:
        self.reload_cluster(cluster)
    in_background(self, submit, submitted, group="submit", connection=cluster.connection)

  @on(Button.Pressed, "#cluster-shell")
  def cluster_shell_pressed(self, event):
    self.action_cluster_shell()

  @property
  def since_value(self):
    """How long ago finished jobs of the active tab are listed (as in slurm.py -c), "off": none"""
    return self.cluster.since_value

  @since_value.setter
  def since_value(self, value):
    self.cluster.since_value = value

  setting_since = False

  def update_since_widgets(self):
    """The dropdown (and custom field) show the choice of the active tab"""
    value = self.since_value
    preset = value in [v for _, v in SINCE_CHOICES if v != "custom"]
    select, custom = self.query_one("#since-choice", Select), self.query_one("#since", Input)
    self.setting_since = True
    try:
      select.value = value if preset else "custom"
      custom.set_class(not preset, "-custom")
      if not preset:
        custom.value = value
    finally:
      self.call_after_refresh(lambda: setattr(self, "setting_since", False))

  @on(Input.Submitted, "#since")
  def since_typed(self, event):
    value = event.value.strip()
    if value.lower() not in ("", "0", "off", "no", "none"):
      try:
        sl.parse_duration(value)
      except sl.argparse.ArgumentTypeError as e:
        self.notify(str(e), severity="error")
        return
    self.since_value = value or "off"
    self.action_reload()

  @on(Select.Changed, "#since-choice")
  def since_chosen(self, event):
    if self.setting_since:
      return
    custom = self.query_one("#since", Input)
    custom.set_class(event.value == "custom", "-custom")
    if event.value == "custom":
      custom.value = self.since_value if self.since_value != "off" else ""
      custom.focus()
      return
    if event.value is Select.NULL or event.value == self.since_value:
      return
    self.since_value = event.value
    self.action_reload()

  @on(Select.Changed, "#scope")
  def scope_changed(self, event):
    if self.setting_scope or event.value == self.cluster.scope or event.value is Select.NULL:
      return
    self.cluster.scope = event.value
    self.setup_job_columns()
    self.action_reload()

  setting_scope = False

  def update_scope_select(self):
    """The accounts of the active tab in the dropdown, and its choice"""
    select = self.query_one("#scope", Select)
    cluster = self.cluster
    options = [("my jobs", "me"), *((f"account {a}", a) for a in cluster.accounts or ()), ("all my accounts", "all")]
    if cluster.scope not in [v for _, v in options]:
      options.insert(-1, (f"account {cluster.scope}", cluster.scope))
    self.setting_scope = True
    try:
      select.set_options(options)
      select.value = cluster.scope
    finally:
      self.call_after_refresh(lambda: setattr(self, "setting_scope", False))

  @on(Input.Changed, "#jfilter")
  def job_filter_changed(self, event):
    self.job_filter = event.value
    self.populate(list(self.jobs.values()))

  @on(Input.Submitted, "#jfilter")
  def job_filter_submitted(self, event):
    event.input.remove_class("visible")
    self.query_one("#jobs", ClickTable).focus()

  @on(DataTable.HeaderSelected, "#jobs")
  def sort_by(self, event):
    col = event.column_key.value
    if col == "jobid": # four orders: job ID descending, ascending, array index ascending, descending
      orders = [("jobid", True), ("jobid", False), ("arrayid", False), ("arrayid", True)]
      current = (self.sort_column, self.sort_reverse)
      self.sort_column, self.sort_reverse = orders[(orders.index(current) + 1) % 4] if current in orders else orders[0]
    elif col == self.sort_column:
      self.sort_reverse = not self.sort_reverse
    else:
      self.sort_column, self.sort_reverse = col, col in ("jobid", "start", "end", "err")
    self.populate(list(self.jobs.values()))

  def current_job(self):
    table = self.query_one("#jobs", ClickTable)
    if not table.row_count:
      return None
    key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
    return self.jobs.get(key)

  @on(DataTable.RowSelected, "#jobs")
  def job_selected(self, event):
    job = self.jobs.get(event.row_key.value)
    col = self.query_one("#jobs", ClickTable).take_clicked_column()
    if not job:
      return
    if col == "err" and job.stderr_path() and job.err[0] == "file":
      self.open_file(job, job.stderr_path())
    else:
      self.open_view(f"job {job.jid}", lambda: JobScreen(job), kind="job", key=f"job {job.jid}")

  def open_file(self, job, path):
    open_path(self, ["Jobs", job.jid], path, [job.rec["WorkDir"]], job_vars(job), follow=job.running)


REPORTS_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1015l\x1b[?1006l\x1b[?1004l" # mouse and focus reporting off

def main():
  parser = argparse.ArgumentParser(prog="slurp", description="Interactive terminal UI for Slurm jobs, locally or on "
                                   "clusters over SSH (one tab per cluster, given with --ssh or added with +).")
  parser.add_argument("--log", metavar="FILE", help="write a debug log (all slurm commands that are run) to FILE")
  parser.add_argument("--ssh", metavar="HOST", nargs="*", default=[], help="add SSH cluster destinations (user@host or ~/.ssh/config aliases)")
  parser.add_argument("--serve", action="store_true", help="serve the app in a browser with textual-serve")
  parser.add_argument("--host", default="127.0.0.1", help="web server bind address for --serve (default: 127.0.0.1)")
  parser.add_argument("--port", type=int, default=8000, help="web server port for --serve (default: 8000)")
  args = parser.parse_args()

  if args.serve:
    try:
      from textual_serve.server import Server
    except ImportError:
      parser.error("--serve requires textual-serve; install it with: pip install textual-serve")
    command = [sys.executable, os.path.dirname(os.path.abspath(__file__))] # the package directory runs __main__.py
    if args.log:
      command += ["--log", args.log]
    if args.ssh:
      command += ["--ssh", *args.ssh]
    Server(shlex.join(command), host=args.host, port=args.port, title="slurp").serve()
    return

  setup_logging(args.log)
  state.initial_ssh_destinations = args.ssh
  app = Slurp()
  try:
    app.run()
  finally:
    app.shutdown()

if __name__ == "__main__":
  main()
