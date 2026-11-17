"""The app: tabs and their SSH sessions, subtabs, the job list, and main()"""

import argparse
import asyncio
import math
import os
import re
import threading
import time
from datetime import datetime

from rich.progress_bar import ProgressBar as RichProgressBar
from rich.text import Text
from textual import events, on
from textual.app import App, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
  Button, Checkbox, DataTable, Input, Select, Static, TextArea
)
from textual.worker import get_current_worker

from ..cluster import slurm as sl
from ..cluster import sshsetup

from ..state import ClusterSession, collector, is_local_destination, log, View
from ..cluster.remote import (
  command_exists, connection_health, CURRENT, fs_isdir, fs_isfile, HEALTH_STYLES, in_background, RemoteSSH, run, run_cmd, SLURM_ENV, start_worker
)
from ..cluster.jobs import (
  annotate, array_task_id, compress_array_ids, duration_text, fmt_time, is_time, load_jobs, pace_eta, progress_for_job, slurm_accounts
)
from ..cluster.details import current_memory_usage, MEMORY, record_memory
from ..cluster.paths import job_vars
from . import icons
from .widgets import ClickTable, err_text, SPINNING, STATE_STYLES, state_text, url_text
from .terminal import SWITCH_KEY, TerminalSession, TerminalView
from .screens.dialogs import ConfirmScreen, InteractiveJobScreen
from .screens.base import SplitScreen
from .screens.files import DirScreen, home_of, open_path, OpenDirScreen
from .screens import hdf5, core # noqa: F401 (they register the views of HDF5 files and core dumps, in this order)
from .screens.shell import TerminalHelpScreen, TerminalScreen
from .screens.job import JobScreen
from .screens.main import COLUMNS, MainScreen, SINCE_CHOICES, SORT_KEYS
from .screens.connection import ConnectionScreen
from .screens.array import ArrayScreen
from .screens.storage import QuotaScreen, ScratchScreen
from ..cluster import remote
from ..config import load_config, update_config
from ..links.share import ShareManager, SFTP_SERVERS, MAX_FAILURES, available as share_available
from .screens.links import ShellChoiceScreen, LinkSetupScreen, LinkScreen, DriveScreen
from .screens.script import ScriptScreen # (registers the view of batch scripts)
from ..links import mounts
from ..checks.engine import check_jobs, counts as checks_counts, SEVERITY_ICON, SEVERITY_STYLE, ERROR, WARNING

LOGIN_CHECK_EVERY = 300 # seconds between the checks of your processes on the login nodes (B09)

def job_has_problems(job):
  c = checks_counts(job.checks)
  return bool(c[ERROR] or c[WARNING])

def category_cell(path):
  """A job's category in the job list: its own name bold, the categories it is in dimmed"""
  if not path:
    return ""
  up, _, leaf = path.rpartition("/")
  return Text.assemble((up + "/", "dim") if up else "", leaf)

def checks_cell(findings):
  """✖1 ⚠2 in the colours of the severities ('' without errors and warnings)"""
  c = checks_counts(findings)
  parts = []
  for sev in (ERROR, WARNING):
    if c[sev]:
      if parts:
        parts.append(" ")
      parts.append((f"{SEVERITY_ICON[sev]}{c[sev]}", SEVERITY_STYLE[sev]))
  return Text.assemble(*parts) if parts else Text("")

def new_session(cluster):
  """The SSH session of a tab (not started): its destination, with its full SSH settings if it has them"""
  return RemoteSSH(cluster.destination, sshsetup.ssh_args(cluster.ssh), cluster.pre_login)


class Slurp(App):
  TITLE = "slurp"
  ENABLE_COMMAND_PALETTE = False # (Textual's palette and its ⭘ in the header: the menu of the job list has what slurp does)
  CSS = """
  Button { min-width: 6; } /* (all buttons are compact: one line, as wide as their label) */
  /* a plain button has a box of its own, as a coloured one (Textual's is the colour of the window: only its word showed) */
  Button.-style-default.-textual-compact { background: $surface-lighten-1; }
  Button.-style-default.-textual-compact:hover { background: $surface-lighten-2; }
  Button.-style-default.-textual-compact.-primary { background: $primary; }
  Button.-style-default.-textual-compact.-primary:hover { background: $primary-darken-2; }
  Button.-style-default.-textual-compact.-success { background: $success; }
  Button.-style-default.-textual-compact.-success:hover { background: $success-darken-2; }
  Button.-style-default.-textual-compact.-warning { background: $warning; }
  Button.-style-default.-textual-compact.-warning:hover { background: $warning-darken-2; }
  Button.-style-default.-textual-compact.-error { background: $error; }
  Button.-style-default.-textual-compact.-error:hover { background: $error-darken-2; }
  /* (marks, not buttons to look at: the ✕ of tabs, the ? of windows) */
  Button.-style-default.-textual-compact.cluster-close, Button.-style-default.-textual-compact.view-close,
  Button.-style-default.-textual-compact.help-corner, #view-help, #about { background: transparent; }
  Button.-style-default.-textual-compact.cluster-close:hover, Button.-style-default.-textual-compact.view-close:hover,
  Button.-style-default.-textual-compact.help-corner:hover, #view-help:hover, #about:hover { background: $surface-lighten-2; }
  HeaderIcon { display: none; }
  #toolbar { height: auto; padding: 0 1; }
  #toolbar > * { margin-right: 1; }
  #reload { width: auto; min-width: 10; } /* (as wide as its label, never wrapped; Reload and Abort one width: the row does not move) */
  #scratch-search { width: 8; }
  #scope { width: 20; }
  #collapse-arrays { width: auto; }
  #scratch-bar, #scratch-actions { height: auto; padding: 0 1; }
  #scratch-bar Label { padding: 0 1 0 0; width: auto; }
  #scratch-bar Input, #scratch-bar Select { margin-right: 1; }
  #scratch-dir { width: 1fr; }
  #scratch-by { width: 24; }
  #scratch-days { width: 8; }
  #scratch-actions Button { margin-right: 1; }
  #scratch-exclude { width: 1fr; margin-right: 1; }
  #scratch-status { height: auto; padding: 0 1; }
  #scratch-files { height: 1fr; }
  #notabs { display: none; padding: 2 4; color: $text-muted; }
  MainScreen.-no-tabs #notabs { display: block; }
  MainScreen.-no-tabs .split, MainScreen.-no-tabs #loadstatus, MainScreen.-no-tabs #connstatus { display: none; }
  #us { width: auto; height: auto; padding: 0; border: none; }
  #since-choice { width: 15; }
  #since { width: 12; display: none; }
  #since.-custom { display: block; }
  #topbar { height: 1; background: $panel; }
  #topbar #menu { height: 1; min-height: 1; padding: 0 1; text-style: bold; }
  #cluster-tabs { height: 1; padding: 0 1; background: $panel; }
  #cluster-tabs Button { height: 1; min-height: 1; padding: 0 1; margin-right: 1; }
  #cluster-tabs Button.cluster-tab { margin-right: 0; }
  #cluster-tabs #about { dock: right; margin-right: 0; text-style: bold; }
  #view-tabs { height: 1; padding: 0 1; background: $boost; }
  #view-tabs Button { height: 1; min-height: 1; padding: 0 1; margin-right: 0; }
  #view-tabs Button.view-close { width: 3; min-width: 3; padding: 0; margin-right: 1; color: $text-muted; }
  #view-tabs Button.view-close:hover { color: $error; }
  #shell-bar { height: 1; background: $panel; }
  #shell-status { width: 1fr; height: 1; padding: 0 1; background: $panel; }
  #shell-help { height: 1; min-height: 1; padding: 0 1; }
  TerminalScreen TerminalView { border: round $panel-lighten-3; }
  TerminalScreen TerminalView:focus { border: round $accent; }
  MainScreen.-no-tabs #view-tabs { display: none; }
  #cluster-tabs Button.cluster-close { width: 3; min-width: 3; padding: 0; margin-right: 2; color: $text-muted; }
  #cluster-tabs Button.cluster-close:hover { color: $error; }
  #topbar Header { dock: none; width: 1fr; height: 1; }
  #view-help { width: 3; min-width: 3; height: 1; text-style: bold; }
  #web-timer { width: auto; height: 1; padding: 0 1; display: none; color: $text-muted; background: transparent; }
  #update { width: auto; height: 1; padding: 0 1; display: none; color: $success; text-style: bold; background: transparent; }
  #refresh-timer { width: auto; height: 1; padding: 0 1; display: none; color: $text-muted; background: transparent; }
  #refresh-timer.-off { color: $text-disabled; text-style: italic; }
  #refresh-timer:hover { background: $surface-lighten-2; }
  #update:hover { background: $surface-lighten-2; }
  #web-timer:hover { background: $surface-lighten-2; }
  #web-timer.-ending { color: $warning; text-style: bold; }
  /* code in Markdown (rule catalogue, guides, help, .md files): a framed block, the same in every theme (Textual's
     ANSI themes make it transparent and without margins, so it looked like stray indented text) */
  MarkdownFence, MarkdownFence:ansi { margin: 1 0 1 1; padding: 0; background: $boost; border-left: outer $accent; }
  MarkdownFence > Label, MarkdownFence:ansi > Label { padding: 0 1; }
  .help-corner { dock: right; width: 3; min-width: 3; height: 1; text-style: bold; }
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
  #connect-panel { display: none; layout: horizontal; height: auto; padding: 0 1; }
  #connect-panel.-center { layout: vertical; height: 1fr; align: center middle; }
  #connect-panel Static { width: auto; color: $text-muted; margin-right: 2; }
  #connect-panel.-center Static { width: 100%; text-align: center; margin: 0 0 1 0; }
  #connect-panel Center { width: auto; height: auto; }
  #connect-panel.-center Center { width: 100%; }
  #no-slurm { display: none; height: 1fr; content-align: center middle; text-align: center; color: $text-muted; padding: 0 4; }
  #details { height: 1fr; padding: 0 1; }
  #checks-bar { height: auto; margin-top: 1; }
  #checks-bar Label { margin-right: 2; }
  #checks-bar Button { margin-right: 1; }
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
  #memory-watch { height: auto; margin-bottom: 1; }
  #memory-watch-bar { height: auto; }
  #memory-every { width: 22; margin-right: 1; }
  #memory-watch-status { width: 1fr; }
  .memory-plot { height: 6; }
  .memory-plot-label { width: 5; padding-top: 2; }
  .memory-plot TimePlot { width: 1fr; height: 6; }
  .memory-plot-text { width: 22; padding: 1 0 0 1; }
  #core-overview { height: 1fr; padding: 1; }
  #core-info { height: auto; }
  #open-gdb { margin-top: 1; }
  #job-actions { height: auto; margin: 1 0; }
  #job-actions Button { margin-right: 1; }
  #job-notes-box { height: auto; border: round $panel; border-title-color: $text-muted; padding: 0 1; margin: 1 0 0 0; }
  #job-notes-box.-empty { display: none; }
  #job-notes { margin: 0; padding: 0; }
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
  #search, #goto, #dsearch, #jfilter { dock: bottom; display: none; }
  #search.visible, #goto.visible, #dsearch.visible, #jfilter.visible { display: block; }
  #dsearch { height: 1; background: $panel; }
  #dfilter { width: 1fr; height: 1; border: none; padding: 0 1; }
  #drecursive { width: auto; margin-left: 1; }
  #dstatus { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
  #dbar { height: 1; background: $panel; }
  #dpath { width: 1fr; height: 1; border: none; padding: 0 1; background: $boost; }
  #dpath:focus { background: $surface; }
  #dplaces { width: auto; height: 1; }
  #dplaces Button { margin-left: 1; min-width: 6; }
  #dir { height: 1fr; }
  #hstatus, #hpage { height: 1; padding: 0 1; background: $panel; }
  #hbody { height: 1fr; }
  #htree { width: 40%; height: 1fr; border-right: vkey $panel-lighten-2; }
  #hright { width: 1fr; height: 1fr; }
  #hinfo { height: auto; max-height: 45%; padding: 0 1; }
  #hmarkdown { height: 1fr; display: none; padding: 0 1; border-top: solid $panel-lighten-2; }
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
  .tclose, .tinfo { width: 3; min-width: 3; height: 1; }
  """
  BINDINGS = [
    Binding("f10", "menu", "Menu", priority=False),
    Binding("q", "quit", "Quit"),
    Binding("r", "reload", "Reload"),
    # (in the menu, with their keys: not in the footer)
    Binding("t", "cluster_shell", "Shell", show=False),
    Binding("d", "login_dir", "Open a directory", show=False),
    Binding("F", "cluster_drive", "Map a directory", show=False),
    Binding("W", "web_access", "Web", show=False),
    Binding("i", "interactive_job", "Interactive job", show=False),
    Binding("C", "cluster_info", "Health", show=False),
    Binding("Q", "quota", "Quota", show=False),
    Binding("X", "scratch", "Old scratch files", show=False),
    Binding("a", "abort_load", "Abort loading"),
    Binding("ctrl+w", "close_view", "Close subtab"),
    Binding(SWITCH_KEY, "switch_side", "Shell ⇄", key_display="^]"),
    Binding("ctrl+x", "close_shell", "Close shell"),
    Binding("question_mark", "view_help", "Help", key_display="?"),
    Binding("f1", "guides", "Guides"),
    Binding("f2", "rule_catalogue", "Rules", show=False),
    Binding("f3", "options", "Options"),
  ]
  shell_version = reactive(0) # bumped when the shell is opened, closed or ends: the panes update
  pane_width = reactive(0)    # width of the terminal pane in columns, 0: half of the window

  def __init__(self):
    super().__init__()
    icons.set_style(None) # (the set of icons stored in the config, read again)
    self.main = None # the job list (MainScreen sets it when it is mounted; the timer of tick() may run before)
    self.link_manager = ShareManager(self.link_event) # shells and drives for the user's own terminal
    # the tabs of the last run (from the config; a local tab the first time)
    saved = load_config()
    self.clusters = []
    self.tab_history = [] # the tabs shown, the last one most recently (closing the tab shown goes back)
    for tab in saved.get("tabs", [{"destination": None}]):
      destination = tab.get("destination") or None
      cluster = ClusterSession(tab.get("name") or destination or "Local", destination)
      cluster.ssh = sshsetup.clean(tab.get("ssh")) if destination else None
      cluster.pre_login = str(tab.get("pre_login") or "") if destination else ""
      cluster.since_value = tab.get("since") or "1d"
      cluster.scope = tab.get("scope") or "me"
      cluster.collapse_arrays = tab.get("collapse_arrays", True)
      self.clusters.append(cluster)
    self.cluster_index = min(max(int(saved.get("active", 0) or 0), 0), max(len(self.clusters) - 1, 0))
    from ..cluster import jobcache
    for cluster in self.clusters: # (the list of the last load at once: until the cluster answers, or if it does not)
      jobcache.load(cluster)
    self.shell = None # TerminalSession shown on the right of every window
    self.sort_column, self.sort_reverse = "jobid", True
    self.job_filter = ""
    self.problems_only = False # the job list shows only jobs with errors or warnings of the checks
    self.job_column_keys = None
    self.spinner_frame = 0

  def get_default_screen(self):
    return MainScreen()

  new_version = None # a newer slurp on PyPI (update.py), asked at the start

  # ---- automatic refreshing of the job list, the job window and the array window (AUTO_REFRESH), counted down in
  # the title bar; a click there switches it off and on (kept in the config)
  @property
  def auto_refresh(self):
    return load_config().get("auto_refresh", True) is not False

  def refresh_seconds(self):
    """Seconds between the automatic refreshes: "refresh_seconds" of the config (60; at least 10);
    SLURP_REFRESH_SECONDS overrides it (the tests: nothing refreshes in the middle of a scenario)"""
    try:
      return max(10, int(os.environ.get("SLURP_REFRESH_SECONDS") or load_config().get("refresh_seconds") or 60))
    except (TypeError, ValueError):
      return 60

  def toggle_auto_refresh(self):
    on = not self.auto_refresh
    update_config(auto_refresh=on)
    self.notify(f"automatic refreshing {'on: every ' + str(self.refresh_seconds()) + ' s' if on else 'off (r reloads)'}", timeout=4)
    self.show_refresh_timer()

  def show_refresh_timer(self):
    """Each second: the countdown of the view shown (if it refreshes by itself), and its refresh when it is due"""
    screen = self.screen
    timers = list(screen.query("#refresh-timer").results(Button))
    if not getattr(screen, "AUTO_REFRESH", False):
      for timer in timers:
        timer.display = False
      return
    on, every = self.auto_refresh, self.refresh_seconds()
    # (the job list counts from its last load, a property; the job and array windows from their last reload)
    settable = not isinstance(getattr(type(screen), "last_refresh", None), property)
    last = screen.last_refresh or 0.0
    if not last and settable: # (shown, not reloaded yet: from now)
      screen.last_refresh = last = time.time()
    left = math.ceil(last + every - time.time()) if last else every # (3, 2, 1, then the refresh)
    if on and last and left <= 0:
      if settable:
        screen.last_refresh = time.time()
      screen.auto_reload()
      left = every if settable else 0
    text = f"⟳ {max(left, 0) // 60}:{max(left, 0) % 60:02d}" if on else "⟳ off"
    for timer in timers:
      timer.display = True
      if str(timer.label) != text:
        timer.label = text
      timer.set_class(not on, "-off")

  def on_mount(self):
    self.set_interval(0.25, self.tick)
    from .. import update
    if update.enabled():
      in_background(self, update.latest_version, self.version_found, group="update", connection=None)
    self.view_kind = "web" if self.is_web else "terminal" # (under the session holder: of the active view)
    self.theme_applied = None
    self.apply_theme()
    self.watch(self, "theme", self.theme_changed, init=False)
    from ..links import holder
    if holder.under_holder(): # the theme follows the active view: the terminal's or the browser's
      import threading
      threading.Thread(target=holder.subscribe, daemon=True, name="active view",
                       args=(lambda kind, label, port, host: self.call_from_thread(self.active_view_changed, kind, port, host),)).start()

  def theme_key(self, kind=None):
    """The config key of the theme: "theme" in a terminal, "theme_web" in a browser (slurp --serve, or the web view
    being the active one)"""
    return "theme_web" if (kind or self.view_kind) == "web" else "theme"

  def configured_theme(self, kind=None):
    """The theme stored for a kind of view (a browser without its own: the terminal's)"""
    config = load_config()
    theme = config.get(self.theme_key(kind)) or (config.get("theme") if self.theme_key(kind) == "theme_web" else None)
    return theme if theme in self.available_themes else None

  def apply_theme(self):
    theme = self.configured_theme()
    if theme and theme != self.theme:
      self.theme_applied = theme # (switched to, not chosen: not stored again)
      self.theme = theme

  active_view_port = 0 # the active view is a browser: the port it connects from (see holder.View.port)
  active_view_host = "" # ... through Cloudflare: the address it opened (a Cloudflare link's public host)

  def active_view_changed(self, kind, port=0, host=""):
    self.active_view_port = port if kind == "web" else 0
    self.active_view_host = host if kind == "web" else ""
    if kind and kind != self.view_kind:
      self.view_kind = kind
      self.apply_theme()

  def theme_changed(self, theme):
    """A theme chosen (in Options) is the default from now on, for this kind of view"""
    if theme == self.theme_applied:
      self.theme_applied = None
      return
    update_config(**{self.theme_key(): theme})

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
      cluster.connection = new_session(cluster)
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

  def activate_cluster(self, index, connect=True):
    """Show the tab: its job list as loaded before (no reload), its shell, or its login while not logged in. connect:
    log in if it is not (a tab just added with +); else a tab without a session shows Connect (the start, a switch)"""
    if not 0 <= index < len(self.clusters):
      return
    cluster = self.clusters[index]
    if connect and self.needs_login(cluster):
      cluster.attempts = 0
      self.login(cluster)
    elif cluster.destination and cluster.connection is None:
      cluster.connection = new_session(cluster) # (not started: the tab's commands wait for Connect, never run here)
    self.cluster_index = index
    if cluster in self.tab_history:
      self.tab_history.remove(cluster)
    self.tab_history.append(cluster) # (closing the tab shown goes back to the one shown before)
    remote.ssh_connection = cluster.connection
    if not cluster.opened:
      cluster.opened = True
      cluster.jobs_open = self.start_page() == "_default"
      cluster.current_view = self.home_mode(cluster)
    mode = cluster.current_view
    if not (mode == "_default" and cluster.jobs_open or any(v.mode == mode for v in cluster.views)):
      mode = cluster.current_view = self.home_mode(cluster)
    if self.current_mode != mode:
      self.switch_mode(mode)
    self.show_tab_shell()
    self.update_scope_select()
    self.update_since_widgets()
    self.query_one("#collapse-arrays", Checkbox).value = cluster.collapse_arrays # (no reload: only the rows change)
    self.populate()
    self.refresh_status()
    if not cluster.loaded and cluster.loader is None and (not cluster.destination or cluster.connection.ready):
      self.action_reload()

  def connect_tab(self):
    """Connect (the job list of a tab without a session): log in, the login in the pane"""
    cluster = self.cluster
    if not self.needs_login(cluster):
      return
    cluster.attempts, cluster.broken_at = 0, None
    self.login(cluster, show=True)
    remote.ssh_connection = cluster.connection
    self.show_tab_shell()
    self.refresh_status()

  @on(Button.Pressed, "#connect")
  def connect_pressed(self, event):
    event.stop()
    self.connect_tab()

  def connect_state(self, cluster):
    """A remote tab without a session the user wants: "idle" (Connect), "connecting" (after Connect), else None"""
    c = cluster.connection
    if not cluster.destination or c is None or c.ready:
      return None
    if c.state == "connecting":
      return "connecting" if cluster.show_login else None
    return "idle" if self.needs_login(cluster) and (c.state in ("new", "closed") or not c.wanted) else None

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
    """A click on a tab: show it (one without a session shows Connect); on the active tab again: log in"""
    if index == self.cluster_index:
      if self.needs_login(self.cluster):
        self.connect_tab()
      return
    self.activate_cluster(index, connect=False)
    self.save_tabs()

  # ---- subtabs: the job list (the default mode) and the views of the active tab, each a mode with its own windows
  next_view_id = 0

  def find_view(self, key):
    return next((v for v in self.cluster.views if key and v.key == key), None)

  def open_view(self, title, make_screen, kind="window", key="", session=None, icon=""):
    """A new subtab of the active tab (or the one that shows key already); icon: the concept of its icon"""
    if not self.clusters:
      return
    view = self.find_view(key)
    if view is None:
      type(self).next_view_id += 1
      view = View(type(self).next_view_id, title, kind, key, session, icon)
      self.add_mode(view.mode, make_screen)
      self.cluster.views.append(view)
    self.show_view(view)

  new_subtab = None # (while in_new_subtab runs: the key of the subtab a window opens in)

  def in_new_subtab(self, show, key=""):
    """Run show (that opens a window): the window opens in a subtab of its own, also from within a subtab (every
    file and directory view, see open_path); a subtab that shows key already is shown instead"""
    view = self.find_view(key)
    if view is not None:
      self.show_view(view)
      return
    self.new_subtab = key
    try:
      show()
    finally:
      self.new_subtab = None

  def open_window(self, screen, title=""):
    """A window: from the job list it opens as a subtab, within a subtab it opens on top of it (← goes back); in a
    subtab of its own in in_new_subtab"""
    new = self.new_subtab is not None
    if (new or self.current_mode in ("_default", "overview")) and not isinstance(self.screen, ModalScreen) and self.clusters:
      crumbs = getattr(screen, "crumbs", None)
      self.open_view(title or (crumbs[-1] if crumbs else "") or type(screen).__name__, lambda: screen,
                     key=self.new_subtab or "", icon=getattr(screen, "icon_name", ""))
    else:
      self.push_screen(screen)

  def start_page(self):
    """What a tab shows the first time: "overview" (no subtab open, the default) or "_default" (its job list open)"""
    return "_default" if load_config().get("start_page") == "jobs" else "overview"

  def home_mode(self, cluster, views=True):
    """What a tab shows when the subtab shown goes away: the one shown before it that is still open (views), else its
    job list if open, else its last subtab (views), else the overview"""
    if views:
      for mode in reversed(cluster.view_history):
        if (mode == "_default" and cluster.jobs_open) or any(v.mode == mode for v in cluster.views) \
            or (mode == "overview" and not cluster.jobs_open):
          return mode
    if cluster.jobs_open:
      return "_default"
    return cluster.views[-1].mode if views and cluster.views else "overview"

  def show_home(self, views=True):
    """The job list (if open), the last subtab (views: when one closed; not for Back), or the overview of the tab"""
    if not self.clusters:
      return
    mode = self.cluster.current_view = self.home_mode(self.cluster, views)
    if self.current_mode != mode:
      self.switch_mode(mode)
      if mode == "_default":
        self.call_after_refresh(self.focus_jobs)
    self.call_after_refresh(self.refresh_status)

  def close_jobs(self):
    """Close the job list's subtab (its list stays loaded): the last subtab, or the overview"""
    cluster = self.cluster
    cluster.jobs_open = False
    if self.current_mode == "_default" or cluster.current_view == "_default":
      self.show_home()
    self.refresh_status()

  def switch_mode(self, mode):
    """(every subtab switch: remembered, so that closing one goes back to the one shown before)"""
    if self.clusters:
      history = self.cluster.view_history
      if mode in history:
        history.remove(mode)
      history.append(mode)
      del history[:-50]
    return super().switch_mode(mode)

  def show_view(self, view):
    """Show a subtab of the active tab (None: the job list, opened if it is closed)"""
    mode = view.mode if view is not None else "_default"
    if view is None:
      self.cluster.jobs_open = True
    self.cluster.current_view = mode
    if self.current_mode != mode:
      self.switch_mode(mode)
      if view is None:
        self.call_after_refresh(self.focus_jobs)
    elif isinstance(self.screen, TerminalScreen): # (its subtab clicked again: the keys go to the shell, not the button)
      self.call_after_refresh(self.screen.query_one(TerminalView).focus)
    self.call_after_refresh(self.refresh_status)

  def close_view(self, view, cluster=None):
    """Close a subtab: its windows go away, its shell ends"""
    cluster = cluster or self.cluster
    if view not in cluster.views:
      return
    if view.session is not None:
      view.session.terminate()
    cluster.views.remove(view)
    if cluster.current_view == view.mode:
      cluster.current_view = self.home_mode(cluster)
    if self.current_mode == view.mode:
      self.show_home()
    def remove():
      try:
        self.remove_mode(view.mode)
      except Exception as e:
        log.debug(f"removing the subtab {view.title}: {e}")
    self.call_after_refresh(remove)
    self.refresh_status()

  def action_close_view(self):
    if self.current_mode == "_default":
      self.close_jobs()
      return
    view = next((v for v in self.cluster.views if v.mode == self.current_mode), None)
    if view is not None:
      self.request_close_view(view)

  def unsaved_scripts(self, mode=None):
    """Script views with unsaved edits (of one subtab, or of all)"""
    stacks = getattr(self, "_screen_stacks", {}) or {}
    screens = [s for m, stack in stacks.items() if mode is None or m == mode for s in stack]
    return [s for s in screens if isinstance(s, ScriptScreen) and s.unsaved()]

  def request_close_view(self, view):
    """Close a subtab, asking first if a script in it has unsaved edits"""
    unsaved = self.unsaved_scripts(view.mode)
    if not unsaved:
      self.close_view(view)
      return
    names = ", ".join(os.path.basename(s.path) for s in unsaved)
    self.push_screen(ConfirmScreen("Unsaved changes", f"{names} has unsaved changes. Close the subtab and lose them?",
                                   "Close"), lambda ok: self.close_view(view) if ok else None)

  def refresh_view_tabs(self, screen):
    bar = screen.query("#view-tabs").first(Horizontal) if screen.query("#view-tabs") else None
    jobs = bar.query("#view-jobs") if bar is not None else None
    if not jobs:
      return
    jobs.first().variant = "primary" if self.current_mode == "_default" else "default"
    for b in (*jobs, *bar.query("#view-close-jobs")):
      if b.display != self.cluster.jobs_open:
        b.display = self.cluster.jobs_open
    wanted = {str(v.id) for v in self.cluster.views}
    have = set()
    for b in bar.query("Button.view-tab, Button.view-close"): # remove only the buttons of closed subtabs (ids stay unique)
      if b.id in ("view-jobs", "view-close-jobs"):
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
        bar.mount(Button(icons.label(v.icon, v.title[:40]), id=f"view-tab-{v.id}", classes="view-tab", compact=True, tooltip=v.title),
                  Button("✕", id=f"view-close-{v.id}", classes="view-close", compact=True, tooltip="close the subtab"))
    for v in self.cluster.views:
      for b in bar.query(f"#view-tab-{v.id}"):
        b.variant = "primary" if self.current_mode == v.mode else "default"
        label = icons.label(v.icon, v.title[:40] + (" (ended)" if v.session is not None and v.session.done else ""))
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
    if cluster.drive is not None:
      cluster.drive.stop() # (its session ends)
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
    if cluster in self.tab_history:
      self.tab_history.remove(cluster)
    if current:
      if self.clusters:
        before = next((c for c in reversed(self.tab_history) if c in self.clusters), None) # (the tab shown before)
        self.activate_cluster(self.clusters.index(before) if before is not None else min(index, len(self.clusters) - 1),
                              connect=False)
      else: # no tab left: only the + button
        self.cluster_index = 0
        remote.ssh_connection = None
        self.populate()
    elif index < self.cluster_index:
      self.cluster_index -= 1
    self.main.set_class(not self.clusters, "-no-tabs")
    self.refresh_status()
    self.save_tabs()

  def save_tabs(self):
    """The open tabs (and which one is shown, and their choices) in the config, for the next start"""
    update_config(tabs=[{"destination": c.destination, "since": c.since_value, "scope": c.scope,
                         "collapse_arrays": c.collapse_arrays, **({"ssh": c.ssh, "name": c.name} if c.ssh else {}),
                         **({"pre_login": c.pre_login} if c.pre_login else {})}
                        for c in self.clusters],
                  active=self.cluster_index)

  def add_cluster(self, destination):
    """A new tab: a destination (a Host of ~/.ssh/config, user@host; empty: this machine), or {"ssh": full settings}
    (the add dialog's); it logs in right away"""
    if destination is None:
      return
    settings = sshsetup.clean(destination.get("ssh")) if isinstance(destination, dict) else None
    pre_login = str(destination.get("pre_login") or "").strip() if isinstance(destination, dict) else ""
    if isinstance(destination, dict):
      destination = destination.get("destination") or ""
    self.main.remove_class("-no-tabs")
    if settings:
      cluster = ClusterSession(sshsetup.label(settings), settings["host"])
      cluster.ssh = settings
      self.clusters.append(cluster)
    elif not destination or is_local_destination(destination):
      self.clusters.append(ClusterSession("Local" if not destination else destination))
    else:
      self.clusters.append(ClusterSession(destination, destination))
    if self.clusters[-1].destination:
      self.clusters[-1].pre_login = pre_login
    from ..cluster import jobcache
    jobcache.load(self.clusters[-1])
    self.refresh_cluster_tabs()
    self.activate_cluster(len(self.clusters) - 1) # (logs in right away: the user just asked for it)
    self.save_tabs()

  def edit_ssh_settings(self, cluster):
    """The SSH settings of a tab, to see and change: its destination, or its full settings (the add dialog)"""
    from .screens.dialogs import AddClusterScreen
    def saved(result):
      if result is None or cluster not in self.clusters:
        return
      settings = sshsetup.clean(result.get("ssh")) if isinstance(result, dict) else None
      pre_login = str(result.get("pre_login") or "").strip() if isinstance(result, dict) else ""
      if isinstance(result, dict) and not settings:
        result = result.get("destination") or ""
      destination = settings["host"] if settings else str(result or "").strip()
      if not destination:
        self.notify("a tab of a cluster needs a destination (this machine: add a tab with an empty one)", severity="warning")
        return
      changed = (destination, settings, pre_login) != (cluster.destination, cluster.ssh, cluster.pre_login)
      cluster.destination, cluster.ssh, cluster.pre_login = destination, settings, pre_login
      cluster.name = sshsetup.label(settings) if settings else destination
      c = cluster.connection
      if c is not None: # (the session takes them at its next login)
        c.destination, c.ssh_options, c.pre_login = destination, sshsetup.ssh_args(settings), pre_login
      self.refresh_cluster_tabs()
      self.save_tabs()
      self.refresh_status()
      if changed and c is not None and c.ready:
        self.notify(f"{cluster.name}: the new SSH settings apply at the next login (Reconnect, R)", timeout=8)
      elif changed:
        self.notify(f"{cluster.name}: SSH settings saved", timeout=4)
    self.push_screen(AddClusterScreen(cluster), saved)

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
    """End the session of a tab; it stays disconnected until Connect or Reconnect is pressed"""
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
    try:
      self.screen
    except ScreenStackError: # (no window: the app is closing down)
      return
    self.spinner_frame += 1
    now = time.monotonic()
    self.link_manager.tick()
    if self.spinner_frame % 4 == 0: # (each second) the countdown of a web link that ends, and its warning
      self.show_web_timer()
      self.show_refresh_timer()
      for button in self.screen.query("#update").results(Button): # (a newer slurp: in the title bar)
        button.display = bool(self.new_version)
        if self.new_version and str(button.label) != f"⬆ slurp {self.new_version}":
          button.label = f"⬆ slurp {self.new_version}"
    if self.spinner_frame % 4 == 0: # (each second) the drives on this machine
      for c in self.clusters:
        if c.drive is not None:
          old = c.drive.state
          if c.drive.poll() != old:
            self.refresh_status()
    if self.spinner_frame % (4 * LOGIN_CHECK_EVERY) == 4 * 30: # (every 5 minutes, first after 30 s) B09
      self.check_login_nodes()
    if self.spinner_frame % 8 == 0 and (self.web_port is not None or self.web_links()): # (every 2 s) the web view
      self.stop_web_if_unused()
      self.report_web_links()
    if self.screen is self.main and (getattr(self.cluster, "checking", False) or any(not job.annotated for job in self.cluster.jobs.values())):
      table = self.main.query_one("#jobs") # (turn the spinners of the cells still coming)
      clear = getattr(table, "_clear_caches", None)
      if clear is not None:
        clear()
        table.refresh()
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
    self.run_memory_watches()

  def run_memory_watches(self):
    """Query the memory of the watched jobs that are due (in the background, on the cluster of their tab)"""
    now = time.monotonic()
    for (tab, jid), w in list(MEMORY.items()):
      if not w["every"] or w["busy"] or now < w["due"] or w["job"] is None:
        continue
      cluster = next((c for c in self.clusters if (c.destination or "Local") == tab), None)
      if cluster is None or cluster.destination and not (cluster.connection and cluster.connection.ready):
        continue
      w["busy"] = True
      def measure(w=w):
        try:
          result = current_memory_usage(w["job"])
        except Exception as e:
          result = {"error": str(e)}
        self.call_from_thread(self.memory_measured, w, result)
      start_worker(self, measure, f"memory-{tab}-{jid}", exclusive=False,
                   connection=cluster.connection if cluster.destination else None, name=f"memory of job {jid}")

  def memory_measured(self, w, result):
    w["busy"] = False
    w["due"] = time.monotonic() + (w["every"] or 0)
    if not record_memory(w, result) and w["misses"] >= 2:
      w["every"] = 0 # the job has probably ended
      w["error"] = f"stopped: {w['error']}"

  def spinner(self):
    return "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self.spinner_frame % 10]

  def open_page(self, url):
    """A web page (a site's status page, …): in the browser when slurp is shown in one, else with this machine's"""
    if self.on_client():
      self.open_on_client(url, lambda ok, why: None if ok else self.notify(f"{url}: {why}", severity="warning"))
      return
    try:
      self.open_url(url)
    except Exception as e:
      log.debug(f"opening {url}: {e!r}")
    self.notify(f"opening {url} with this machine's browser (none? in a terminal that knows links, Ctrl+click on it "
                "opens it there; or copy it)", timeout=10)

  def status_hint(self, cluster):
    """" · check status.cscs.ch" (a link) for a tab whose site has a status page (rule packs), else None"""
    from ..checks.rulepacks import status_page
    url = status_page(cluster.name, cluster.destination or "")
    if not url:
      return None
    return Text.assemble((" · check ", "red"), url_text(url, url.split("://", 1)[1].rstrip("/")))

  def connection_text(self, cluster):
    """State of the SSH session of a tab, for the footer"""
    text = self.connection_state_text(cluster)
    c = cluster.connection
    failing = c is not None and (c.state not in ("new", "closed", "ready", "connecting") or c.state == "connecting" and cluster.attempts > 1)
    hint = self.status_hint(cluster) if failing and cluster.destination else None
    return text.append_text(hint) if hint is not None else text

  def connection_state_text(self, cluster):
    if not cluster.destination:
      return Text("local", style="dim")
    c = cluster.connection
    if c is None or c.state == "new":
      return Text(f"SSH {cluster.destination}: not connected", style="dim")
    if c.state == "closed":
      return Text(f"SSH {cluster.destination}: closed · Connect in the job list", style="yellow")
    if c.ready:
      waiting = c.responding()
      if waiting is not None and waiting > remote.PING_SLOW:
        return Text(f"{self.spinner()} SSH {c.identity}: no answer for {waiting:.0f} s", style="bold red")
      latency = f" · {c.latency * 1000:.0f} ms" if c.latency is not None else ""
      level, words = connection_health(c)
      return Text(f"● SSH {c.identity}{latency}" + ("" if level == "good" else f" · {words}"), style=HEALTH_STYLES[level])
    if c.state == "connecting":
      what = "log in on the right" if self.shell is cluster.login_terminal else "connecting"
      attempt = f" (attempt {cluster.attempts})" if cluster.attempts > 1 else ""
      return Text(f"{self.spinner()} SSH {cluster.destination}: {what}{attempt}", style="yellow")
    # broken
    reason = f" ({c.error[:60] + '…' if len(c.error) > 60 else c.error})" if c.error else "" # (short: the status page follows)
    if not c.wanted:
      return Text(f"SSH {cluster.destination}: disconnected{reason} · Connect in the job list", style="red")
    delay = self.RECONNECT_DELAYS[min(cluster.attempts, len(self.RECONNECT_DELAYS) - 1)]
    left = max(0, delay - (time.monotonic() - (cluster.broken_at or time.monotonic())))
    return Text(f"{self.spinner()} SSH {cluster.destination}: connection lost{reason}, reconnecting"
                + (f" in {left:.0f} s" if left >= 1 else ""), style="bold red")

  def load_text(self, cluster):
    if cluster.loader is None:
      return Text("")
    waiting = " (waiting for the connection)" if cluster.connection and not cluster.connection.ready else ""
    what = "reading the files of the jobs" if cluster.listed else "loading jobs"
    return Text(f"{self.spinner()} {what} {time.monotonic() - cluster.load_started:.0f} s{waiting} · a: abort",
                style="yellow")

  def tab_label(self, cluster):
    """Its icon (this machine or a cluster over SSH), its name, for a cluster a dot coloured by the health of its
    session (● connected, ◌ connecting or not answering, ○ not connected: by its shape too, also on the colour of the
    active tab), and ⟳ while its list loads"""
    c = cluster.connection
    mark = icons.icon("cluster" if cluster.destination else "local")
    label = Text(f"{mark} {cluster.name}" if mark else cluster.name)
    if cluster.destination:
      level = connection_health(c)[0] if c is not None and c.state != "new" else None
      dot = "●" if c and c.ready and (c.responding() or 0) <= remote.PING_SLOW else "○" if c is None or c.state in ("new", "closed") else "◌"
      label.append(" " + dot, style=HEALTH_STYLES[level] if level else "dim")
    if cluster.loader is not None:
      label.append(" ⟳")
    return label

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
          if str(button.label) != str(label) or getattr(button, "shown_label", None) != label.markup:
            button.label, button.shown_label = label, label.markup
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
    links = self.links_text()
    if links is not None:
      connection = Text.assemble(connection, links)
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
        self.show_no_slurm(cluster)
        stale = self.stale_text(cluster)
        screen.query_one("#status", Static).update(Text.assemble(stale, cluster.summary) if stale is not None else Text(cluster.summary))
        button = screen.query_one("#reload", Button)
        label, variant = ((icons.label("cancel_job", "Abort"), "error") if cluster.loader is not None
                          else (icons.label("reload", "Reload"), "primary"))
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
      # the width again from the label: the rows added next widen it as needed (DataTable never narrows a column, so
      # one long cell would keep it wide after the list changed)
      column.content_width = len(label)
    table.refresh()

  def setup_job_columns(self, show_progress=None):
    table = self.query_one("#jobs", ClickTable)
    all_accounts = self.cluster.scope != "me"
    if show_progress is None:
      show_progress = any(progress_for_job(job) is not None or job.running and not job.annotated for job in self.jobs.values())
    store = self.cluster.categories
    with_categories = bool(store and (store.paths or store.jobs))
    visible_columns = [(key, label) for key, label in COLUMNS
                       if (key != "user" or all_accounts) and (key not in ("progress", "eta_now") or show_progress)
                       and (key != "category" or with_categories)]
    column_keys = [key for key, _ in visible_columns]
    if column_keys == self.job_column_keys:
      return
    if not all_accounts and self.sort_column == "user":
      self.sort_column, self.sort_reverse = "jobid", True
    if not show_progress and self.sort_column in ("progress", "eta_now"):
      self.sort_column, self.sort_reverse = "jobid", True
    if not with_categories and self.sort_column == "category":
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
    self.open_view(title, lambda: TerminalScreen(session), kind="shell", session=session, icon="shell")

  def action_cluster_shell(self):
    """A shell on the cluster of the tab: in slurp, or from the user's own terminal (asked every time)"""
    cluster = self.cluster
    name = cluster.connection.identity if cluster.destination and cluster.connection else cluster.destination or "this machine"
    def chosen(where):
      if where == "slurp":
        if cluster.destination:
          self.open_shell([], f"shell on {name}", focus=True)
        else:
          self.open_shell([os.environ.get("SHELL") or "bash", "-l"], "local shell", focus=True)
      elif where == "own":
        self.open_link_setup("shell", f"shell on {name}", cluster, self.link_argv(cluster))
    self.push_screen(ShellChoiceScreen(f"Shell on {name}"), chosen)

  # ---- links: shells and drives for the user's own terminal (links/share.py)
  def link_argv(self, cluster, args=None, interactive=True):
    """() -> the argv a session of a link runs on the cluster of the tab (over its session at that time)"""
    def argv():
      if not cluster.destination:
        return list(args) if args else [os.environ.get("SHELL") or "bash", "-l"]
      c = cluster.connection
      if c is None or not c.ready:
        raise RuntimeError(f"the connection to {cluster.name} is not ready")
      return c.command_argv(list(args or []), interactive=interactive)
    return argv

  def open_link_setup(self, kind, title, cluster, argv, path="", read_only_argv=None, dirs=None):
    if cluster.destination and (cluster.connection is None or not cluster.connection.ready):
      self.notify(f"{cluster.name}: log in first", severity="warning")
      return
    def created(link):
      if link is not None:
        if dirs:
          link.dirs = list(dirs)
        if kind == "drive": # (its name on the user's machine: user@cluster:/path)
          import getpass
          import socket
          user = (cluster.connection.user if cluster.destination and cluster.connection else "") or getpass.getuser()
          where = cluster.name.split("@")[-1] if cluster.destination else socket.gethostname().split(".")[0]
          link.origin = f"{user}@{where}"
        self.push_screen(LinkScreen(link))
        self.refresh_status()
    self.push_screen(LinkSetupScreen(kind, title, cluster.name, argv, path=path, read_only_argv=read_only_argv), created)

  def action_cluster_drive(self):
    """The login directory of the tab as a drive for the user's own machine (SFTP)"""
    cluster = self.cluster
    if cluster.destination and (cluster.connection is None or not cluster.connection.ready):
      self.notify(f"{cluster.name}: log in first", severity="warning")
      return
    home = cluster.connection.home if cluster.destination else os.path.expanduser("~")
    probe = "for p; do [ -x \"$p\" ] && echo \"$p\" && exit 0; done; exit 0"
    def found(result):
      out, dirs = result or ("", [])
      path = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
      cluster.sftp_server = path
      local_reason = ("this is the local tab: its files are already here" if not cluster.destination else mounts.available())
      def chosen(where):
        if where == "link":
          link_setup(path, dirs)
      self.push_screen(DriveScreen(cluster, home, local_reason), chosen)
    def link_setup(path, dirs):
      if not path and not cluster.destination:
        self.notify("no sftp-server on this machine: a drive is not possible", severity="error")
        return
      def make(read_only):
        if path:
          return self.link_argv(cluster, [path, *(["-R"] if read_only else []), "-d", home], interactive=False)
        def subsystem(): # (the cluster's standard SFTP: no read only, no start directory)
          c = cluster.connection
          if c is None or not c.ready:
            raise RuntimeError(f"the connection to {cluster.name} is not ready")
          return c.subsystem_argv("sftp")
        return subsystem
      self.open_link_setup("drive", f"{cluster.name} {home}", cluster, make(False), path=home,
                           read_only_argv=make(True) if path else None, dirs=dirs)
    def probe_all(): # the sftp-server, and the folders the helper offers ($HOME, $SCRATCH, ...)
      from ..cluster.storage import drive_dirs
      out = run(["sh", "-c", probe, "sh", *SFTP_SERVERS])
      try:
        dirs = drive_dirs(home)
      except Exception:
        dirs = [("HOME", home)] if home else []
      return out, dirs
    in_background(self, probe_all, found, group="sftp-server", connection=cluster.connection)

  def link_event(self, kind, link, session, message):
    """Logins, wrong passwords and the state of the links, as notifications (they must not go unnoticed)"""
    what = f"link {link.id} ({link.title})"
    if kind == "login":
      self.notify(f"{session.kind} opened through {what}" + (f", key {session.key}" if session.key else ""),
                  title=link.tab, timeout=8)
    elif kind == "failure":
      self.notify(f"{what}: {message}", title=link.tab, severity="warning", timeout=8)
    elif kind == "locked":
      self.notify(f"{what} locked after {MAX_FAILURES} wrong passwords", title=link.tab, severity="error", timeout=15)
    elif kind == "ended" and session is not None and session.kind:
      self.notify(f"{session.kind} through {what} ended", title=link.tab, timeout=4)
    elif kind == "relay":
      if link.relay_state == "broken" and not getattr(link, "warned", False):
        link.warned = True
        self.notify(f"{what}: relay lost: {message}", title=link.tab, severity="warning", timeout=10)
      elif link.relay_state == "ready":
        link.warned = False
    elif kind == "closed" and message != "revoked":
      self.notify(f"{what} takes no more logins: {message}", title=link.tab, timeout=5)
    self.refresh_status()

  # ---- the login directory as a drive on this machine (links/mounts.py)
  def drive_of(self, cluster):
    if cluster.drive is None:
      cluster.drive = mounts.LocalDrive(cluster, self.drive_event)
    return cluster.drive

  def drive_task(self, cluster, coro, done=None, quiet=False):
    """Run a mount or unmount on the event loop; its error as a notification (quiet: only logged)"""
    async def run():
      try:
        err = await coro
      except Exception as e:
        log.exception("drive action failed")
        err = str(e)
      if err and quiet:
        log.debug(f"drive of {cluster.name}: {err}")
      elif err:
        self.notify(f"drive of {cluster.name}: {err}", title=cluster.name, severity="error", timeout=10)
      elif done:
        done()
      self.refresh_status()
    asyncio.ensure_future(run())

  def mount_drive(self, cluster, path, read_only, remote=None):
    """remote: the directory of the cluster (None: as before; "": the login directory)"""
    self.drive_task(cluster, self.drive_of(cluster).mount(path, read_only, getattr(cluster, "sftp_server", ""), remote))

  def unmount_drive(self, cluster, lazy=False):
    self.drive_task(cluster, self.drive_of(cluster).unmount(lazy))

  def remount_drive(self, cluster):
    self.drive_task(cluster, self.drive_of(cluster).remount())

  def drive_event(self, kind, drive, message):
    if kind == "stuck":
      self.notify(f"drive at {drive.path} did not answer for 30 s: mounting it again", title=drive.cluster.name,
                  severity="warning", timeout=8)
      self.drive_task(drive.cluster, drive.remount())
    elif kind == "remount": # (lost before; the session of the tab is there again)
      self.drive_task(drive.cluster, drive.mount(drive.path, drive.read_only, drive.server), quiet=True)
    elif kind == "lost":
      self.notify(f"drive at {drive.path} lost its connection: it is mounted again when the session of the tab is back",
                  title=drive.cluster.name, severity="warning", timeout=8)
    elif kind == "mounted":
      self.notify(f"drive {'mounted again' if message else 'mounted'} at {drive.path}", title=drive.cluster.name, timeout=5)
    elif kind == "ended" and message:
      self.notify(f"drive at {drive.path} ended: {message}", title=drive.cluster.name, severity="error", timeout=10)
    elif kind == "ended":
      self.notify(f"drive at {drive.path} unmounted", title=drive.cluster.name, timeout=4)
    self.refresh_status()

  def links_text(self):
    """For the footer: open links and sessions (they must stay visible)"""
    active, live = self.link_manager.active(), self.link_manager.live_sessions()
    drives = [c.drive for c in self.clusters if c.drive is not None and c.drive.state in ("mounting", "mounted", "broken", "reconnecting")]
    if not active and not drives:
      return None
    text = Text()
    if active:
      text.append(f" · ⇄ {len(active)} link{'s' if len(active) != 1 else ''}" + (f", {len(live)} session{'s' if len(live) != 1 else ''} open" if live else ""),
                  style="bold magenta")
    for d in drives:
      text.append(f" · ⛁ {d.path}" + {"mounting": " …", "broken": " (not answering)", "reconnecting": " (reconnecting)"}.get(d.state, ""),
                  style="bold red" if d.state in ("broken", "reconnecting") else "bold magenta")
    return text

  @on(Button.Pressed, "#menu")
  def menu_pressed(self, event):
    event.stop()
    self.action_menu()

  def action_job_list(self):
    """The job list of the tab (opened if it is closed; without Slurm it says so)"""
    if self.clusters:
      self.show_view(None)

  def action_menu(self):
    """The menu, under its button in the header (every window has it): what slurp does for the tab"""
    from .screens.menu import MenuScreen
    if not self.screen.query("#menu"): # (a dialog, the menu itself)
      return
    button = self.screen.query_one("#menu", Button)
    at = (button.region.x, button.region.y + 1)
    def chosen(action):
      if action:
        self.call_later(self.run_action, action)
    self.push_screen(MenuScreen(at), chosen)

  # ---- remote web access: this slurp in a browser, through a web link (see links/holder.py)
  web_port = None # the web view of the session holder, while one runs
  web_setup_open = False # the Web dialog is open: its web view must not stop before the link exists

  def action_web_access(self):
    from ..links import holder
    if not holder.under_holder():
      self.notify("remote web access needs slurp started as usual (slurp): this one runs without the session "
                  "holder (--no-holder, --serve, or not in a terminal)", severity="warning", timeout=10)
      return
    if share_available() is not None:
      self.notify(f"remote web access {share_available()}", severity="warning", timeout=10)
      return
    def start():
      return holder.control("web_start")
    def started(reply):
      if not reply or not reply.get("port"):
        self.notify(f"the web view did not start: {(reply or {}).get('error', 'no answer from the session holder')}",
                    severity="error")
        return
      self.web_port = reply["port"]
      host = os.uname().nodename
      def created(link):
        self.web_setup_open = False
        if link is not None:
          self.push_screen(LinkScreen(link))
          self.refresh_status()
        else:
          self.stop_web_if_unused()
      self.web_setup_open = True
      self.push_screen(LinkSetupScreen("web", f"this slurp on {host}", self.cluster.name, lambda: [],
                                       web=(reply["port"], reply["token"])), created)
    in_background(self, start, started, group="web")

  def format_title(self, title, sub_title):
    """The header: the logo's mark (in the theme's colours), then the title as Textual shows it"""
    from textual.content import Content
    from . import logo
    from .. import VERSION_TEXT
    return Content.assemble(logo.mark(), " ", App.format_title(self, title, sub_title), (f"  {VERSION_TEXT}", "dim"))

  shared_selection = ""

  def on_text_selected(self, event):
    """Text marked with the mouse (Textual's selection): to the browser, if it is the active view"""
    self.share_selection(self.screen.get_selected_text() or "", always=True)

  @on(TextArea.SelectionChanged)
  def text_area_selection(self, event):
    self.share_selection(event.text_area.selected_text or "")

  def share_selection(self, text, always=False):
    """The text marked, to the browser that is the active view (slurp in your browser): its Ctrl+C copies it itself,
    in the key's event (holder.COPY_SCRIPT). A browser lets a page write the clipboard only there; the OSC 52 of
    Textual's copy arrives later and is dropped. always: also the same text again (the page forgets it on a click)"""
    from ..links import holder
    if not (text or self.shared_selection) or text == self.shared_selection and not always:
      return
    if not (holder.under_holder() and self.view_kind == "web"):
      return
    self.shared_selection = text
    in_background(self, lambda: holder.control("selection", text=text), lambda _: None, group="selection", connection=None)

  def on_client(self):
    """The active view is a browser: slurp can open a URL (or a download) on the user's computer through it"""
    from ..links import holder
    return self.is_web or (holder.under_holder() and self.view_kind == "web")

  def open_on_client(self, url, done=lambda ok, why: None):
    """Opens the URL in the active browser (a slurp:// link: the slurp helper there); done(ok, why) on the UI thread"""
    from ..links import holder
    if self.is_web:
      self.open_url(url, new_tab=url.startswith(("http://", "https://"))) # (a web page beside slurp's; a slurp:// link has no page)
      done(True, "")
      return
    if not self.on_client():
      done(False, "the active view is a terminal: slurp cannot open anything on its computer")
      return
    in_background(self, lambda: holder.control("open_url", url=url),
                  lambda reply: done(bool(reply and reply.get("ok")), (reply or {}).get("error", "no answer from the session holder")),
                  group="client")

  def deliver_to_client(self, name, data, done=lambda ok, why: None):
    """The browser that is active downloads the file (bytes)"""
    import base64
    import io
    from ..links import holder
    if self.is_web:
      self.deliver_binary(io.BytesIO(data), save_filename=name, mime_type="application/octet-stream")
      done(True, "")
      return
    if not self.on_client():
      done(False, "the active view is a terminal: a download needs slurp in your browser (Remote web access)")
      return
    in_background(self, lambda: holder.control("deliver", name=name, data=base64.b64encode(data).decode()),
                  lambda reply: done(bool(reply and reply.get("ok")), (reply or {}).get("error", "no answer from the session holder")),
                  group="client")

  def this_view(self, session):
    """The session is the web link's tunnel of the browser this slurp is on now"""
    return bool(self.active_view_port) and self.active_view_port in getattr(session, "web_ports", ())

  def this_view_link(self, link):
    """The browser this slurp is on now came through the link (an SSH web link: one of its sessions; Cloudflare: its
    address)"""
    if link.cloudflare:
      return bool(self.active_view_host) and self.active_view_host == link.public_host
    return any(self.this_view(s) for s in link.live_sessions)

  def web_links(self):
    return [l for l in self.link_manager.links if l.kind == "web"]

  WEB_WARN = 5 * 60 # seconds before a web link ends: the user is warned (and can extend it)
  web_warned = None # {(link id, what, deadline)} warned about already

  def web_deadline(self):
    """(seconds left, link, what) of the web link that ends first, None if none does: "expiry" (it takes no more
    logins; it can be extended) or "relay" (its relay ends the tunnel: a new address, e.g. pinggy's free hour). Only
    links that are open or have sessions."""
    now, first = time.time(), None
    for link in self.web_links():
      if link.finished or not (link.open() or link.live_sessions):
        continue
      for when, what in ((link.expires if link.open() else None, "expiry"),
                         (link.relay_until if link.relay_state == "ready" else None, "relay")):
        if when and when > now and (first is None or when - now < first[0]):
          first = (when - now, link, what)
    return first

  def show_web_timer(self):
    """The countdown in the title bar (left of ?), and the warning a few minutes before"""
    deadline = self.web_deadline()
    for timer in self.screen.query("#web-timer").results(Button):
      timer.display = deadline is not None
      if deadline is not None:
        left, link, what = deadline
        text = f"⏱ {int(left) // 3600}:{int(left) % 3600 // 60:02d}:{int(left) % 60:02d}" if left >= 3600 else \
               f"⏱ {int(left) // 60}:{int(left) % 60:02d}"
        if timer.label != text:
          timer.label = text
        timer.set_class(left <= self.WEB_WARN, "-ending")
    if deadline is None:
      return
    left, link, what = deadline
    key = (link.id, what, round(link.expires if what == "expiry" else link.relay_until))
    self.web_warned = self.web_warned or set()
    if left <= self.WEB_WARN and key not in self.web_warned:
      self.web_warned.add(key)
      from .screens.links import WebEndingScreen
      self.push_screen(WebEndingScreen(link, what, left))

  def version_found(self, version):
    """A newer slurp: said once at the start, and the title bar has it (⬆) until slurp quits; skipped versions not"""
    from .. import update, __version__
    if not update.newer(version, __version__) or version == load_config().get("update_skip"):
      return
    self.new_version = version
    self.notify(f"slurp {version} is available (this is {__version__}): ⬆ in the title bar says how to update", timeout=12)

  def show_update(self):
    from .screens.dialogs import UpdateScreen
    if self.new_version:
      self.push_screen(UpdateScreen(self.new_version))

  def show_web_deadline(self):
    """The web link that ends first, in its window"""
    deadline = self.web_deadline()
    if deadline is not None:
      self.push_screen(LinkScreen(deadline[1]))

  def stop_web_if_unused(self):
    """No web link takes logins or has a session any more: the web view stops (the port closes)"""
    from ..links import holder
    if self.web_port is None or self.web_setup_open or any(l.open() or l.live_sessions for l in self.web_links()):
      return
    self.web_port = None
    in_background(self, lambda: holder.control("web_stop"), lambda _: None, group="web")

  def report_web_links(self):
    """What the lock screens of the other views say about the web links (sent when it changes)"""
    from ..links import holder
    if not holder.under_holder():
      return
    lines = []
    for l in self.web_links():
      if l.open() or l.live_sessions:
        lines.append((f"web link {l.id} via Cloudflare ({l.public_host or 'starting'})" if l.cloudflare else
                      f"web link {l.id} via {l.relay_host}: {l.logins} logins, {len(l.live_sessions)} open")
                     + (f", until {datetime.fromtimestamp(l.expires):%H:%M}" if l.expires else ""))
    text = "\n".join(lines)
    if text != getattr(self, "web_info_sent", ""):
      self.web_info_sent = text
      in_background(self, lambda: holder.control("info", text=text), lambda _: None, group="web-info")

  def action_login_dir(self):
    """Open a directory of the active cluster (home, $SCRATCH, $PROJECT, … or a path, asked in a dialog) in a new
    subtab: each time another one"""
    cluster = self.cluster
    if cluster.destination and (cluster.connection is None or not cluster.connection.ready):
      self.notify(f"{cluster.name}: log in first", severity="warning")
      return
    home = home_of(cluster)
    if not home:
      self.notify(f"{cluster.name}: the login directory is not available yet", severity="warning")
      return
    def chosen(path):
      if not path or cluster is not self.cluster:
        return
      def work():
        if fs_isdir(path):
          type(self).next_dir_view += 1
          key = f"dir {self.next_dir_view}" # (a key of its own: never an open subtab instead)
          return lambda: self.in_new_subtab(lambda: self.open_window(DirScreen([cluster.name], path, bases=[home])), key=key)
        return lambda: self.notify(f"{path} is not a directory" if fs_isfile(path) else f"{path} does not exist",
                                   severity="warning")
      in_background(self, work, lambda show: show() if show else None, group="open", name=f"opening {path}")
    self.push_screen(OpenDirScreen(cluster), chosen)

  next_dir_view = 0

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
      chain = [w for w in self.screen.focus_chain if not w.has_class("tclose") and not w.has_class("tinfo") and w.id != "shell-help" and not isinstance(w, TerminalView)]
      jobs = self.main.query_one("#jobs", ClickTable) if self.main is not None and self.screen is self.main else None
      widget = jobs if jobs is not None and jobs in chain else chain[0] if chain else None # (on the job list: the list)
    if widget is not None:
      widget.focus()

  def action_switch_side(self):
    if isinstance(self.focused, TerminalView):
      self.focus_left()
    else:
      self.focus_terminal()

  async def action_quit(self):
    live = self.link_manager.live_sessions()
    drives = [c.drive for c in self.clusters if c.drive is not None and (c.drive.proc is not None or c.drive.state == "reconnecting")]
    unsaved = self.unsaved_scripts()
    from ..links import holder
    status = holder.control("status", timeout=1) if holder.under_holder() and not getattr(self, "quit_confirmed", False) else None
    views = (status or {}).get("views", [])
    if (live or drives or unsaved or len(views) > 1) and not getattr(self, "quit_confirmed", False):
      def done(yes):
        if yes:
          self.quit_confirmed = True
          self.call_later(self.action_quit) # (run_action is async: called without await, it did nothing)
      parts = []
      if live:
        parts.append(f"{len(live)} session{'s' if len(live) != 1 else ''} through links (shells and drives on your own "
                     "machine) are open: they end, a drive mounted through one fails until it is unmounted.")
      for d in drives:
        parts.append(f"The drive at {d.path} is unmounted.")
      if unsaved:
        parts.append(f"Unsaved changes in {', '.join(os.path.basename(s.path) for s in unsaved)} are lost.")
      if len(views) > 1:
        parts.append(f"{len(views)} views show this slurp ({', '.join(v['label'] for v in views)}): they all close.")
      self.push_screen(ConfirmScreen("Quit slurp", " ".join(parts), "Quit"), done)
      return
    self.link_manager.shutdown()
    for c in self.clusters:
      if c.drive is not None:
        c.drive.stop()
    await self.real_quit()

  async def real_quit(self):
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
    if not self.clusters and action not in ("quit", "command_palette", "help_quit", "view_help", "about", "guides", "rule_catalogue", "options"):
      return False # no tab: only + (and quit)
    if action == "reload": # a key of the job table, not of the other windows
      return self.screen is self.main
    if action in ("switch_side", "close_shell"):
      return self.shell is not None
    if action == "abort_load":
      return self.cluster.loader is not None
    if action == "close_view":
      return self.current_mode != "overview"
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
      except argparse.ArgumentTypeError as e:
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
      if not cluster.slurm_found: # (no Slurm there: the job list says so, instead of an error per command)
        cluster.slurm_found = command_exists("squeue")
        cluster.no_slurm = not cluster.slurm_found
        if cluster.no_slurm:
          log.info(f"{cluster.name}: no squeue there, no Slurm")
          return
      if accounts is None:
        accounts = slurm_accounts()
      jobs = load_jobs(since, scope, accounts)
      self.load_categories(cluster)
      # the list is shown now; the command, error file and progress of each job (files on the cluster) follow. Until
      # then a job keeps what the last load found, and a new one shows spinners
      known = cluster.jobs
      for job in jobs:
        old = known.get(job.jid)
        if old is not None and old.annotated: # (not one of a load replaced before its files were read)
          job.command, job.err = old.command, old.err
          job.progress = old.progress if job.running else None
          job.checks = old.checks
        else:
          job.annotated = False
      self.call_from_thread(self.jobs_listed, cluster, worker, jobs, since, accounts)
      annotate(jobs)
    finally:
      self.call_from_thread(self.jobs_loaded, cluster, worker, jobs, since, accounts)

  def load_categories(self, cluster):
    """(in a worker of the tab) its categories, read with every load: another slurp may have changed them"""
    from ..cluster import categories
    try:
      if cluster.slurm_cluster is None:
        cluster.slurm_cluster = categories.cluster_name()
      self.write_pending_categories(cluster)
      cluster.categories = categories.load(cluster.slurm_cluster)
    except (OSError, ValueError) as e:
      log.warning(f"categories of {cluster.name}: {e}")
      if cluster.categories is None:
        cluster.categories = categories.Store(cluster.slurm_cluster or "default")
      if not getattr(cluster, "told_categories", False):
        cluster.told_categories = True
        self.call_from_thread(self.notify, f"the categories cannot be read: {e}", title=cluster.name, severity="warning", timeout=12)

  def write_pending_categories(self, cluster):
    """(in a worker of the tab) the changes to the categories made while the cluster could not be reached: on the
    cluster now, in their order (one that no longer fits, e.g. a rename of a category deleted meanwhile: skipped)"""
    from ..cluster import categories
    ops = list(cluster.pending_categories)
    if not ops:
      return
    def replay(store):
      skipped = []
      for op in ops:
        try:
          categories.apply_op(store, op)
        except (ValueError, TypeError, AttributeError) as e:
          skipped.append(f"{op[0]}: {e}")
      return skipped
    store, skipped = categories.change(cluster.slurm_cluster, replay) # (OSError, ValueError: tried again next load)
    cluster.pending_categories = [p for p in cluster.pending_categories if p not in ops]
    cluster.categories = store
    from ..cluster import jobcache
    jobcache.save_pending(cluster)
    done = len(ops) - len(skipped)
    text = (f"{done} change{'s' if done != 1 else ''} to the categories made while the cluster could not be reached: "
            "written to it now" + (f"; skipped (no longer possible): {'; '.join(skipped)}" if skipped else ""))
    self.call_from_thread(self.notify, text, title=cluster.name, severity="warning" if skipped else "information", timeout=12)

  def offline(self, cluster):
    """An SSH tab whose cluster cannot be reached now (not logged in, lost, closed)"""
    return bool(cluster.destination) and (cluster.connection is None or not cluster.connection.ready)

  def stale_text(self, cluster):
    """The job list may be outdated: kept from an earlier load, or the cluster cannot be reached (None: it is current)"""
    if not cluster.loaded or cluster.updated is None:
      return None
    when = f"{cluster.updated:%m-%d %H:%M}" if cluster.updated.date() != datetime.now().date() else f"{cluster.updated:%H:%M}"
    c = cluster.connection
    if self.offline(cluster):
      why = ("the cluster has not answered yet" if c is not None and c.state in ("new", "connecting") else
             "not logged in" if c is None or c.state in ("new", "closed") else "the cluster cannot be reached")
      return Text(f"⚠ the list from {when} ({why}): it may be outdated · ", style="bold yellow")
    if cluster.from_cache:
      return Text(f"⚠ the list kept from {when}" + (": loading the current one · " if cluster.loader is not None else
                  ": it may be outdated, the current one did not load (r: reload) · "), style="bold yellow")
    return None

  def jobs_listed(self, cluster, worker, jobs, since, accounts):
    """The jobs from Slurm, before their files are read"""
    if accounts is not None and cluster.accounts is None:
      cluster.accounts = accounts
      if cluster is self.cluster:
        self.update_scope_select()
    if cluster.loader is not worker:
      return
    cluster.jobs = {job.jid: job for job in jobs}
    cluster.since, cluster.loaded, cluster.updated = since, True, datetime.now()
    cluster.listed, cluster.from_cache = True, False
    if cluster is self.cluster:
      self.populate()
    self.refresh_status()

  def jobs_loaded(self, cluster, worker, jobs, since, accounts=None):
    if accounts is not None and cluster.accounts is None:
      cluster.accounts = accounts
      if cluster is self.cluster:
        self.update_scope_select()
    if cluster.loader is not worker: # aborted or replaced by a newer reload
      return
    cluster.loader = None
    cluster.listed = False
    if cluster.no_slurm and cluster is self.cluster:
      self.populate()
    if jobs is not None:
      for job in jobs:
        job.annotated = True # (also when reading the files failed: what is known stays)
      cluster.jobs = {job.jid: job for job in jobs}
      cluster.since, cluster.loaded, cluster.updated = since, True, datetime.now()
      cluster.from_cache = False
      if cluster is self.cluster:
        self.populate()
      self.check_jobs_later(cluster, jobs)
      from ..cluster import jobcache
      threading.Thread(target=jobcache.save, args=(cluster, jobs), daemon=True, name="keeping the job list").start()
    self.show_warnings()
    self.refresh_status()

  def check_login_nodes(self):
    """B09: your processes on the login nodes that use a lot of CPU for long (said once per process)"""
    from ..checks.engine import login_hogs
    if "B09" in set((load_config().get("checks_muted") or [])):
      return
    for cluster in self.clusters:
      if cluster.destination and cluster.connection is not None and cluster.connection.ready:
        def done(hogs, cluster=cluster):
          told = cluster.__dict__.setdefault("told_hogs", set())
          for pid, cpu, minutes, comm in hogs or []:
            if pid not in told:
              told.add(pid)
              self.notify(f"B09: {comm} (pid {pid}) uses {cpu:.0f}% CPU for {minutes} min on the login node: "
                          "run heavy work as a job (i: interactive job). F2: the rule catalogue",
                          title=cluster.name, severity="warning", timeout=15)
        in_background(self, login_hogs, done, group=f"login-{cluster.name}", connection=cluster.connection)

  def action_guides(self, anchor=""):
    """The guides, with the numbers of the active tab's cluster"""
    from .screens.checks import GuidesScreen
    from ..checks.engine import gather_facts
    from ..checks.guides import guides_markdown
    cluster = self.cluster
    def facts():
      try:
        return gather_facts()
      except Exception as e:
        log.debug(f"facts for the guides: {e!r}")
        return None
    in_background(self, facts, lambda f: self.open_doc_tab("guides", "Guides", lambda: GuidesScreen(guides_markdown(f, cluster.name), anchor),
                                                           GuidesScreen, lambda screen: screen.jump(anchor) if anchor else None),
                  group="guides")

  def action_new_script(self):
    """New job script: a template with the cluster's numbers, written to a file and opened in the script view"""
    from .screens.wizard import NewScriptScreen
    cluster = self.cluster
    if cluster.destination and not (cluster.connection and cluster.connection.ready):
      self.notify("log in first", severity="warning")
      return
    home = cluster.connection.home if cluster.connection else os.path.expanduser("~")
    def created(path):
      if path:
        open_path(self, ["Jobs"], path)
    self.push_screen(NewScriptScreen(home or "."), created)

  def action_about(self):
    """? slurp: slurp in general, its version, the machine it runs on"""
    from .screens.help import AboutScreen, about_facts, about_markdown, about_text
    if isinstance(self.screen, AboutScreen):
      return
    in_background(self, lambda: about_facts(self),
                  lambda facts: self.push_screen(AboutScreen(about_markdown(facts), about_text(facts))) if facts else None,
                  group="about", connection=None)

  def action_view_help(self):
    """? : the help of the window shown (a shell: its terminal help)"""
    from .screens.help import ViewHelpScreen, help_markdown
    screen = self.screen
    if isinstance(screen, ViewHelpScreen):
      return
    if isinstance(screen, TerminalScreen):
      self.show_terminal_help(screen.session, then=lambda: screen.query_one(TerminalView).focus())
      return
    self.push_screen(ViewHelpScreen(help_markdown(screen)))

  def show_terminal_help(self, session, pane=False, then=None):
    """The help of a shell (pane: one in the side pane), then() when it closes (the widgets of the shells ask for it
    here: the screens build on them)"""
    self.push_screen(TerminalHelpScreen(session, pane=pane), lambda _: then and then())

  def push_screen(self, screen, *args, **kwargs):
    """Dialogs get a ? in their top right corner (the windows have theirs in the top bar)"""
    result = super().push_screen(screen, *args, **kwargs)
    if isinstance(screen, ModalScreen):
      from .screens.help import add_help_button
      self.call_after_refresh(add_help_button, screen)
    return result

  @on(Button.Pressed, ".help-corner")
  def help_corner_pressed(self, event):
    event.stop()
    self.action_view_help()

  def check_jobs_later(self, cluster, jobs):
    """The light checks of the listed jobs, after the load (their first facts about the cluster cost a round trip that
    must not hold up the list): the Checks column fills in when they are done"""
    def work():
      check_jobs(jobs)
      return True
    def done(ok):
      cluster.checking = False # (on the UI thread, with the redraw: no moment with results but without them shown)
      if cluster is self.cluster and cluster.loader is None and self.screen is self.main:
        self.populate() # (also when the checks failed: no spinners left)
      else:
        cluster.stale_list = True # (drawn again when the job list is shown)
    cluster.checking = True
    in_background(self, work, done, group=f"checks-{cluster.name}", name="checking the jobs",
                  connection=cluster.connection if cluster.destination else None)

  def action_options(self):
    """Options: the settings in config.json"""
    from .screens.options import OptionsScreen
    def saved(changes):
      if changes is None:
        return
      self.apply_theme() # (the theme of the kind of view that is active now)
      same = (changes.get("icons") or "nerd") == icons.style()
      self.notify("options saved" + ("" if same else " · the icons change at the next start"), timeout=3 if same else 6)
      if "checks_muted" in changes:
        for screen in self.screen_stack:
          recheck = getattr(screen, "recheck", None)
          if recheck:
            recheck()
    self.push_screen(OptionsScreen(), saved)

  def action_toggle_problems(self):
    self.problems_only = not self.problems_only
    self.populate()
    self.refresh_status()

  def action_rule_catalogue(self, rule_id=""):
    """The rule catalogue, at a rule (shown in the catalogue window if one is open)"""
    from .screens.checks import RuleCatalogueScreen
    self.open_doc_tab("rules", "Rule catalogue", lambda: RuleCatalogueScreen(rule_id), RuleCatalogueScreen,
                      lambda screen: screen.show_rule(rule_id))

  def open_doc_tab(self, key, title, make_screen, cls, show):
    """A document (rule catalogue, guides) in a subtab of its own: one per tab, shown again (and moved to what is
    asked) when it is open already"""
    if not self.clusters:
      self.push_screen(make_screen())
      return
    view = self.find_view(key)
    if view is None:
      self.open_view(title, make_screen, key=key, icon=getattr(cls, "icon_name", ""))
      return
    self.show_view(view)
    def moved():
      screen = next((s for s in reversed(self.screen_stack) if isinstance(s, cls)), None)
      if screen is not None:
        show(screen)
    self.call_after_refresh(moved)

  def action_abort_load(self):
    cluster = self.cluster
    if cluster.loader is None:
      return
    cluster.loader.abort.set()
    cluster.loader.cancel()
    cluster.loader = None
    if cluster.listed: # the new list is shown, only its files were not read: no spinners turning for ever
      cluster.listed = False
      for job in cluster.jobs.values():
        job.annotated = True
      self.populate()
      self.notify("reading the files of the jobs aborted: commands, error files and progress of the new jobs are missing", timeout=5)
    else:
      self.notify("loading aborted: the list is the one loaded before", timeout=4)
    self.refresh_status()

  COLLAPSE_MIN = 10 # tasks of one array in one state from which they are shown as one row

  def collapse(self, ordered, cluster):
    """The rows of the job list: jobs, and for each array with many tasks a row (array id, tasks) where its first
    task is (followed by the tasks if the row was opened)"""
    groups = {}
    for job in ordered:
      if array_task_id(job) is not None:
        groups.setdefault(job.jid.partition("_")[0], []).append(job)
    big = {key: jobs for key, jobs in groups.items() if len(jobs) >= self.COLLAPSE_MIN}
    member = {job.jid: key for key, jobs in big.items() for job in jobs}
    done = set()
    for job in ordered:
      key = member.get(job.jid)
      if key is None:
        yield job
      elif key not in done:
        done.add(key)
        yield (key, big[key])
        if key in cluster.expanded:
          yield from big[key]

  STATE_CODES = {"RUNNING": "R", "PENDING": "PD", "COMPLETED": "CD", "COMPLETING": "CG", "CONFIGURING": "CF",
                 "FAILED": "F", "CANCELLED": "CA", "TIMEOUT": "TO", "OUT_OF_MEMORY": "OOM", "NODE_FAIL": "NF",
                 "BOOT_FAIL": "BF", "DEADLINE": "DL", "PREEMPTED": "PR", "SUSPENDED": "S", "REQUEUED": "RQ"}

  def mixed_state(self, tasks):
    """How many tasks are in which state, with the short codes of Slurm in the colours of the states:
    30R 10PD 100CD 5F (running, pending, completed, failed first, then the others)"""
    counts = {}
    for j in tasks:
      state = j.state.split()[0].rstrip("+") if j.state else "?"
      counts[state] = counts.get(state, 0) + 1
    order = ["RUNNING", "PENDING", "COMPLETED"]
    parts = []
    for state in sorted(counts, key=lambda s: (order.index(s) if s in order else len(order), s)):
      if parts:
        parts.append(" ")
      style = {"RUNNING": "bold green", "COMPLETED": "green", "PENDING": "blue", "CANCELLED": "yellow"}.get(state)
      parts.append((f"{counts[state]}{self.STATE_CODES.get(state, state[:3])}", style or STATE_STYLES.get(state, "red")))
    return Text.assemble(*parts)

  def job_id_cell(self, job, under_its_array=False):
    """The id of a job in the job list, after the icon of an array (a task of one) or room for it; the tasks under
    the row of their array further in"""
    mark = icons.icon("array")
    room = " " * (len(mark) + 1) if mark else ""
    if under_its_array:
      return room + "  " + job.jid
    return (f"{mark} " if array_task_id(job) is not None and mark else room) + job.jid

  def group_row(self, key, tasks, cluster):
    """Cells of the row of the tasks of an array"""
    array_id = key
    indices = sorted(int(t) for j in tasks if (t := array_task_id(j)) is not None)
    ids = compress_array_ids(indices)
    if len(ids) > 15: # interleaved states give long lists (1,3,5,...): they would make the column very wide
      ids = f"{indices[0]}…{indices[-1]}"
    opened = key in cluster.expanded
    first = tasks[0].rec
    values = [Text(icons.label("array", f"{array_id}_[{ids}] {'▾' if opened else '▸'} {len(tasks)} tasks"), style="bold"), first["JobName"]]
    if "group|" + key in cluster.marked:
      values[0] = Text.assemble(("● ", "bold magenta"), values[0])
    if "category" in self.job_column_keys:
      cats = {j.category for j in tasks}
      values.append(category_cell(cats.pop()) if len(cats) == 1 else Text(f"{len(cats)} categories", style="dim"))
    if cluster.scope != "me":
      users = {j.rec["User"] for j in tasks}
      values.append(first["User"] if len(users) == 1 else f"{len(users)} users")
    values.append(self.mixed_state(tasks))
    if "progress" in self.job_column_keys:
      progress = [p for j in tasks if (p := progress_for_job(j))]
      total, done = sum(p.total for p in progress), sum(p.current for p in progress)
      values.append(RichProgressBar(total=max(total, 1), completed=done, width=8) if progress else "")
      values.append("") # (no estimate for the array as a whole)
    starts = [j.rec["Start"] for j in tasks if is_time(j.rec["Start"])]
    ends = [j.rec["End"] for j in tasks if is_time(j.rec["End"])]
    errors = sum(j.err[0] == "file" and j.err[1] > 0 for j in tasks)
    values.extend((str(sum(int(j.rec["NNodes"] or 0) for j in tasks)), "",
                   fmt_time(min(starts)) if starts else "", fmt_time(max(ends)) if ends else "",
                   checks_cell([f for j in tasks for f in (j.checks or [])]),
                   Text(f"! {errors}", style="bold red") if errors else "", tasks[0].command[:150]))
    return values

  def focus_jobs(self):
    """The job list takes the focus unless something to type into has it: an unfocused table is drawn in other colours
    (e.g. after a tab switch, a reload, or back from a tab without Slurm, whose hidden table gave the focus away)"""
    if self.main is None or self.screen is not self.main:
      return
    table = self.main.query_one("#jobs", ClickTable)
    if table.display and not isinstance(self.focused, (Input, TerminalView, TextArea)):
      table.focus()

  def show_connect(self, cluster):
    """A remote tab without a session: Connect, in the middle of the job list, or above the jobs it lists from before.
    True if it takes the place of the job list"""
    if self.main is None or not self.main.is_mounted:
      return False
    panel = self.main.query_one("#connect-panel")
    state = self.connect_state(cluster)
    center = state is not None and not cluster.jobs
    if state == "connecting" and not center:
      state = None # (the bar goes; the login is in the pane)
    panel.display = state is not None
    panel.set_class(center, "-center")
    if state is not None:
      where = cluster.destination
      text = (Text.assemble((f"Connecting to {where} …", "bold"), "\nlog in on the right") if state == "connecting" else
              Text.assemble((f"Not connected to {where}", "bold"), "\nthe jobs are listed once you are logged in" if center else
                            " · the jobs below are from before"))
      label = panel.query_one("#connect-note", Static)
      if str(label.render()) != str(text):
        label.update(text)
      button = panel.query_one("#connect", Button)
      button.display = state == "idle"
    return center

  def show_no_slurm(self, cluster):
    """Instead of the job list, in its middle: no Slurm on the machine of the tab (what to do); or Connect"""
    table, note = self.main.query_one("#jobs", ClickTable), self.main.query_one("#no-slurm", Static)
    connect = self.show_connect(cluster)
    shown = not connect and not cluster.no_slurm
    if table.display != shown:
      table.display = shown
      if shown: # (the focus went elsewhere while it was hidden)
        self.call_after_refresh(self.focus_jobs)
    note.display = cluster.no_slurm and not connect
    if cluster.no_slurm and not connect:
      where = cluster.destination or "this machine"
      note.update(Text.assemble(
        (f"No Slurm on {where}\n\n", "bold"),
        f"squeue is not found {'there' if cluster.destination else 'here'}, so there are no jobs to list.\n\n",
        "Add a tab for a cluster with ", ("+", "bold"), " above (", ("user@login-node", "bold"), " or a ~/.ssh/config alias).\n",
        ("r", "bold"), " looks again (e.g. after module load slurm); the shell, the drive and the files work without Slurm.",
        style="", justify="center"))

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
    store = cluster.categories
    for job in all_jobs:
      job.category = store.category_of(job.jid) if store else ""
    self.setup_job_columns()
    pattern = self.job_filter.casefold()
    from ..cluster.categories import within
    only = self.job_filter.strip()[4:].strip().strip("/") if pattern.startswith("cat:") else None # (cat:a/b: that category)
    visible_jobs = [job for job in all_jobs if (not self.problems_only or job_has_problems(job)) and (
      (not job.category if only == "-" else within(job.category, only) and (job.category or not only)) if only is not None else
      not pattern or pattern in " ".join(str(value or "") for value in (
      job.jid, job.rec.get("JobName"), job.state, job.rec.get("User"), job.rec.get("Account"),
      job.rec.get("Partition"), job.rec.get("NodeList"), job.rec.get("WorkDir"), job.command,
      job.rec.get("SubmitLine"), job.rec.get("ExitCode"), job.category,
    )).casefold())]
    cluster.marked &= {job.jid for job in all_jobs} | {"group|" + job.jid.partition("_")[0] for job in all_jobs} # (gone ones)
    table.clear()
    self.mark_sort_column(table)
    ordered = sorted(visible_jobs, key=SORT_KEYS[self.sort_column], reverse=self.sort_reverse)
    try:
      items = list(self.collapse(ordered, cluster)) if cluster.collapse_arrays else ordered
      rows = {item[0]: self.group_row(*item, cluster) for item in items if isinstance(item, tuple)}
    except Exception: # the list must show in any case: then without collapsed arrays
      log.exception("collapsing arrays in the job list failed")
      items, rows = ordered, {}
    for item in items:
      if isinstance(item, tuple): # the row of a collapsed (or opened) array
        table.add_row(*rows[item[0]], key="group|" + item[0])
        continue
      job = item
      rec = job.rec
      state = job.state if job.queued and job.state != "PENDING" else \
              f"{job.state} ({job.reason})" if job.queued else f"{job.state} ({rec['ExitCode']})"
      start = fmt_time(rec["Start"])
      in_open_array = rows and job.jid.partition("_")[0] in rows and array_task_id(job) is not None # under its row
      values = [self.job_id_cell(job, in_open_array), rec["JobName"]]
      if job.jid in cluster.marked:
        values[0] = Text.assemble(("● ", "bold magenta"), (values[0], "bold"))
      if "category" in self.job_column_keys:
        values.append(category_cell(job.category))
      if cluster.scope != "me":
        values.append(rec["User"])
      values.append(state_text(state))
      if "progress" in self.job_column_keys:
        progress = progress_for_job(job)
        values.append(RichProgressBar(total=max(progress.total, 1), completed=progress.current, width=8) if progress
                      else SPINNING if job.running and not job.annotated else "")
        going = job.running and job.progress and job.progress.current < job.progress.total
        if job.running and not job.annotated:
          values.append(SPINNING)
        else:
          eta = pace_eta(job.progress, "current") if going else None
          values.append(Text(duration_text(eta - time.time()) if eta and eta > time.time() else "overdue" if eta
                             else "-" if going else "", style="" if not eta or eta > time.time() else "yellow"))
      values.extend((rec["NNodes"], f"{rec['Elapsed']} / {rec['Timelimit']}",
                     ("~" + start) if start and job.state == "PENDING" else start, fmt_time(rec["End"]),
                     checks_cell(job.checks) if job.checks is not None else SPINNING if getattr(cluster, "checking", False) else "",
                     err_text(job.err) if job.annotated else SPINNING, job.command[:150] if job.annotated else SPINNING))
      table.add_row(*values, key=job.jid)
    if current is not None:
      try:
        table.move_cursor(row=table.get_row_index(current)) # a job or the row of an array
      except Exception:
        pass
    self.show_no_slurm(cluster) # (first: a table hidden on a tab without Slurm cannot take the focus)
    self.focus_jobs()
    if cluster.no_slurm:
      cluster.summary = f"no Slurm on {cluster.destination or 'this machine'}"
      self.refresh_status()
      return
    if not cluster.loaded:
      cluster.summary = "no jobs loaded yet" if not cluster.destination or (cluster.connection and cluster.connection.ready) \
                        else "press Connect to load the jobs"
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
    if self.problems_only:
      status += f" · only the {len(visible_jobs)} jobs with problems (!: all)"
    if errors:
      status += f" · {errors} with output in their error file"
    if cluster.marked:
      status += f" · {len(cluster.marked)} marked (c: categorize, M: none)"
    cluster.summary = status + f" · updated {cluster.updated:%H:%M:%S}"
    self.refresh_status()

  @on(Button.Pressed, "#reload")
  def reload_pressed(self, event):
    if self.cluster.loader is not None:
      self.action_abort_load()
    else:
      self.action_reload()

  def action_quota(self):
    cluster = self.cluster
    if self.clusters and not isinstance(self.screen, QuotaScreen): # (a window)
      self.push_screen(QuotaScreen(cluster))

  def action_scratch(self):
    cluster = self.cluster
    if cluster.destination and not (cluster.connection and cluster.connection.ready):
      self.notify("log in first", severity="warning")
      return
    self.open_view("old files in scratch", lambda: ScratchScreen(cluster), key="scratch", icon="scratch")

  def action_cluster_info(self):
    """Health, reservations, occupancy, partitions and node topology of the tab's cluster: a window"""
    from .screens.clusterinfo import ClusterInfoScreen
    cluster = self.cluster
    if self.clusters and not isinstance(self.screen, ClusterInfoScreen):
      self.push_screen(ClusterInfoScreen(cluster))

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

  @property
  def since_value(self):
    """How long ago finished jobs of the active tab are listed (e.g. 1d, 3d12h, 2w, 1mo), "off": none"""
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
      except argparse.ArgumentTypeError as e:
        self.notify(str(e), severity="error")
        return
    self.since_value = value or "off"
    self.save_tabs()
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
    self.save_tabs()
    self.action_reload()

  @on(Select.Changed, "#scope")
  def scope_changed(self, event):
    if self.setting_scope or event.value == self.cluster.scope or event.value is Select.NULL:
      return
    self.cluster.scope = event.value
    self.save_tabs()
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
    if str(event.row_key.value).startswith("group|"): # the row of an array
      key = event.row_key.value.partition("|")[2]
      if self.query_one("#jobs", ClickTable).take_clicked_column() == "jobid": # on ▸: open or close the row
        self.toggle_group(key)
      else: # Enter or a click elsewhere: the window of the array
        self.open_array(key)
      return
    job = self.jobs.get(event.row_key.value)
    col = self.query_one("#jobs", ClickTable).take_clicked_column()
    if not job:
      return
    if col == "err" and job.stderr_path() and job.err[0] == "file":
      self.open_file(job, job.stderr_path())
    else:
      self.open_view(f"job {job.jid}", lambda: JobScreen(job), kind="job", key=f"job {job.jid}")

  @on(Checkbox.Changed, "#collapse-arrays")
  def collapse_changed(self, event):
    if event.value != self.cluster.collapse_arrays:
      self.cluster.collapse_arrays = event.value
      self.populate()
      self.save_tabs()

  # ---- categories: m marks rows, c puts the marked ones (or the row under the cursor) in a category, K the window
  def cursor_key(self):
    table = self.query_one("#jobs", ClickTable)
    if not table.row_count:
      return None
    return str(table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value)

  def action_mark(self):
    """m: mark the row under the cursor (again: unmark it), then the next row"""
    key = self.cursor_key()
    if key is None:
      return
    self.cluster.marked.symmetric_difference_update({key})
    table = self.query_one("#jobs", ClickTable)
    row = table.cursor_row
    self.populate()
    table.move_cursor(row=min(row + 1, table.row_count - 1))

  def action_mark_all(self):
    """M: mark every row shown; if some are marked: none"""
    table = self.query_one("#jobs", ClickTable)
    if self.cluster.marked:
      self.cluster.marked.clear()
    else:
      self.cluster.marked.update(str(k.value) for k in table.rows)
    self.populate()

  def category_targets(self):
    """The jobs c works on: the marked rows (an array's row: the array), else the row under the cursor; as job ids"""
    keys = sorted(self.cluster.marked) or ([k] if (k := self.cursor_key()) else [])
    jids = [k.partition("|")[2] if k.startswith("group|") else k for k in keys]
    return list(dict.fromkeys(jids))

  def action_categorize(self):
    """c in the job list: the marked rows (else the row under the cursor) in a category"""
    jids = self.category_targets()
    if not jids:
      self.notify("no job here to categorize", severity="warning")
      return
    self.categorize_jobs(jids, {k.partition("|")[2] for k in self.cluster.marked if k.startswith("group|")})

  def categorize_jobs(self, jids, arrays=(), after=None):
    """Ask for the category of the jobs (ids; arrays: those that are whole arrays), then put them in it"""
    from .screens.categories import CategorizeScreen, change_categories
    cluster = self.cluster
    if cluster.categories is None:
      self.notify("the categories are not read yet (they come with the job list)", severity="warning")
      return
    names = {}
    for jid in jids:
      job = cluster.jobs.get(jid) or next((j for j in cluster.jobs.values() if j.jid.partition("_")[0] == jid), None)
      if job is not None:
        names[jid] = job.rec.get("JobName") or ""
    shown = ", ".join(f"{j} (array)" if j in arrays else j for j in jids[:6]) + (" …" if len(jids) > 6 else "")
    describe = f"{'job' if len(jids) == 1 else f'{len(jids)} jobs'}: {shown}"
    def answered(result):
      if not result:
        return
      what, path = result
      def done(store, _):
        cluster.marked.clear()
        self.populate()
        if after is not None:
          after()
        n = f"{len(jids)} job{'s' if len(jids) != 1 else ''}"
        self.notify(f"{n} put in {path}" if what == "assign" else f"{n} taken out of their category", title=cluster.name)
      op = ["assign", list(jids), path, names] if what == "assign" else ["unassign", list(jids)]
      change_categories(self, cluster, op, done, "categorizing jobs")
    self.push_screen(CategorizeScreen(cluster.categories, jids, describe), answered)

  def action_categories(self):
    from .screens.categories import CategoriesScreen
    if self.cluster.categories is None:
      self.notify("the categories are read with the job list: load it first (r)", severity="warning")
      return
    self.open_view("categories", CategoriesScreen, key="categories", icon="categories")

  def open_category(self, path):
    """The categories window at one category, in a subtab of its own (one per category: shown again if it is open)"""
    from .screens.categories import CategoriesScreen
    if self.cluster.categories is None:
      return
    self.open_view(f"category {path}", lambda: CategoriesScreen(path), key=f"category {path}", icon="categories")


  def action_toggle_collapse(self):
    self.query_one("#collapse-arrays", Checkbox).value = not self.cluster.collapse_arrays

  def toggle_group(self, key):
    self.cluster.expanded.symmetric_difference_update({key})
    self.populate()

  def action_toggle_group(self):
    """Space on the row of an array (or on one of its tasks when it is open): open or close it"""
    table = self.query_one("#jobs", ClickTable)
    if not table.row_count:
      return
    key = str(table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value)
    if key.startswith("group|"):
      self.toggle_group(key.partition("|")[2])
    elif (job := self.jobs.get(key)) and array_task_id(job) is not None:
      group = job.jid.partition("_")[0]
      if group in self.cluster.expanded:
        self.toggle_group(group)

  def open_array(self, array_id, job=None):
    """The window of an array job (a subtab from the job list, else on top of the window it is opened from)"""
    job = job or next((j for j in self.jobs.values() if j.jid.partition("_")[0] == str(array_id)), None)
    if self.current_mode in ("_default", "overview"):
      self.open_view(f"array {array_id}", lambda: ArrayScreen(array_id, job), kind="job", key=f"array {array_id}",
                     icon="array")
    else:
      self.push_screen(ArrayScreen(array_id, job))

  def open_array_subtab(self, array_id, job=None):
    """The window of an array job in a subtab of its own, from wherever (the one open already: shown)"""
    job = job or next((j for j in self.jobs.values() if j.jid.partition("_")[0] == str(array_id)), None)
    self.in_new_subtab(lambda: self.open_window(ArrayScreen(array_id, job)), key=f"array {array_id}")

  def open_file(self, job, path):
    """A log of a job (its output or error file): at its end, followed while the job runs"""
    open_path(self, ["Jobs", job.jid], path, [job.rec["WorkDir"]], job_vars(job), follow=job.running, at_end=True)


REPORTS_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1015l\x1b[?1006l\x1b[?1004l" # mouse and focus reporting off
