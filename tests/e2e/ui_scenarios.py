"""slurp's UI against the lab: the scenarios t_* (Textual's run_test of the app, logged in to the lab cluster over
SSH), and UISession, which runs them in one logged-in app, one at a time, each in that app's task (test_ui_*.py: a
session per test module).

A scenario uses the module's globals HERE (the lab directory), LAB (its sshlab/) and OUT (screenshots and the log),
set by configure() before a session starts, and check() (a soft check of the current test)."""
import urllib.parse
import asyncio
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
import collections
import importlib
import pkgutil
import slurp
from slurp.checks import engine as checks, environment
from slurp.cluster import remote
from slurp.ui.screens import dialogs
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import Markdown, RadioButton, TextArea
from slurp.ui.app import Slurp
from slurp.config import config_path, load_config, update_config
from slurp.cluster.jobs import array_overview, ERR_SIZES, JobProgress, progress_state, set_progress_file
from slurp.cluster.remote import HEALTH_STYLES
from slurp.ui.screens.array import ArrayScreen, epoch
from slurp.ui.screens.cancel import CancelScreen
from slurp.ui.screens.categories import CategoriesScreen, CategorizeScreen, NONE, find_node
from slurp.ui.screens.checks import FixPreviewScreen, GuidesScreen, RuleCatalogueScreen
from slurp.ui.screens.clusterinfo import cluster_health, ClusterInfoScreen
from slurp.ui.screens.overview import OverviewScreen
from slurp.ui.screens.connection import ConnectionScreen
from slurp.ui.screens.dialogs import CommandScreen, ConfirmScreen, CopyScreen, RestartScreen, RunArrayScreen
from slurp.ui.screens.files import DirScreen, FilePickScreen, FileScreen, open_path, OpenDirScreen
from slurp.ui.screens.help import AboutScreen, VIEW_HELP, ViewHelpScreen
from slurp.ui.screens.job import JobScreen
from slurp.ui.screens.links import OTHER_PLACE, DriveScreen, link_state, LinkScreen, LinkSetupScreen, ShellChoiceScreen
from slurp.ui.screens.main import MainScreen
from slurp.ui.screens.options import OptionsScreen
from slurp.ui.screens.script import ScriptScreen
from slurp.ui.screens.shell import screen_text, TerminalHelpScreen, TerminalScreen
from slurp.ui.screens.storage import QuotaScreen, ScratchScreen
from slurp.ui.screens.wizard import NewScriptScreen
from slurp.links.share import client_commands, host_fingerprint, is_public, Link, link_health, parse_relay, Session
from slurp.state import setup_logging
from slurp.ui.terminal import TerminalView
from slurp.ui.widgets import ClickTable, ConnectionStatus, Spinning
import slurp.cluster.categories as _categories

HERE = LAB = OUT = CATEGORIES_FILE = None
CURRENT = None # (the check of the test that runs a scenario)

def check(name, cond, info=""):
  return CURRENT(name, cond, info)

def configure(work, home, out):
  """The lab and this machine for a session of the app (the environment is restored by UISession.close)"""
  global HERE, LAB, OUT, CATEGORIES_FILE
  HERE, LAB, OUT = work, os.path.join(work, "sshlab"), out
  os.makedirs(OUT, exist_ok=True); os.makedirs(home, exist_ok=True)
  os.environ["HOME"] = home # (~/mnt/lab and the like: not the user's home)
  os.environ["PATH"] = os.path.join(LAB, "bin") + ":" + os.environ["PATH"] # (ssh with the lab's config)
  os.environ.pop("SSH_AUTH_SOCK", None); os.environ.pop("DISPLAY", None)
  for var, sub in (("XDG_CACHE_HOME", "xdgcache"), ("XDG_STATE_HOME", "xdgstate"), ("XDG_CONFIG_HOME", "xdgtest"), ("XDG_DATA_HOME", "xdgdata")):
    os.environ[var] = os.path.join(home, sub) # (never the user's: empty for every session)
  # the lab's "cluster" is this machine: never the real ~/.config/slurp/categories.json
  CATEGORIES_FILE = _categories.FILE = os.path.join(home, "xdgcat", "categories.json")
  # the lab as it is built (a scenario that raised may have left it slow or with arrays)
  open(os.path.join(LAB, "delay"), "w").write("0")
  for name in ("arrays", "stat_delay"):
    if os.path.exists(os.path.join(LAB, name)): os.unlink(os.path.join(LAB, name))

def term(app): s = app.shell; return "\n".join(s.vt.display) if s and s.vt else ""
def check_term(screen):
  """the text of the terminal of a check window (SshCheckScreen), its history and screen"""
  s = getattr(screen, "session", None)
  if not s or not s.vt:
    return ""
  history = ["".join(ch.data for ch in row) for row in (s.vt.history or [])] # (a list of pyte chars per line)
  return "\n".join(history + list(s.vt.display))
def sterm(app):
  """the screen of the shell subtab shown"""
  scr = app.screen
  s = scr.session if isinstance(scr, TerminalScreen) else None
  return "\n".join(s.vt.display) if s and s.vt else ""
async def until(pilot, cond, t=20):
  t0 = time.time()
  while time.time() - t0 < t:
    await pilot.pause(0.05)
    try:
      if cond(): return True
    except Exception: pass
  return False
def notes(app): return [(n.title, n.message) for n in app._notifications]
async def menu(app, pilot, action):
  """☰ Menu of the job list (a click on it), then its item for action (as Enter on it)"""
  from slurp.ui.screens.menu import MenuScreen
  from textual.widgets import OptionList
  await pilot.click("#menu")
  await until(pilot, lambda: isinstance(app.screen, MenuScreen), 5)
  items = app.screen.query_one(OptionList)
  items.highlighted = items.get_option_index(action); await pilot.pause(0.1)
  await pilot.press("enter")
def note_ids(app): return {n.identity for n in app._notifications}
def new_notes(app, seen):
  """the notifications that came after note_ids gave seen (by identity: counting them would not do, Textual drops
  the ones that expired from the list meanwhile)"""
  return [(n.title, n.message) for n in app._notifications if n.identity not in seen]
def slurp_modules():
  """every module of the slurp package"""
  return [importlib.import_module(m.name) for m in pkgutil.walk_packages(slurp.__path__, "slurp.") if not m.name.endswith("__main__")]
class UISession:
  """slurp's app under run_test in an event loop of its own, logged in to the lab. Its task runs the scenarios given to
  run(), one at a time, in its own context (as a single run through all of them did); between them the loop waits."""
  def __init__(self, work, home, out, size=(170, 45), icons="none"):
    self.args = (work, home, out)
    self.size = size # (columns, rows of the terminal)
    self.icons = icons # (none: the labels are their words, for the checks; the icons have tests of their own)
    self.broken = None

  def start(self):
    """Log in (raises if that fails, after cleaning up)"""
    self.closed = False
    self.saved_env, self.saved_file = dict(os.environ), _categories.FILE
    self.saved_modules = {name: dict(vars(m)) for name, m in sys.modules.items() if name == "slurp" or name.startswith("slurp.")}
    self.loop = None
    try:
      return self._start()
    except BaseException:
      self.close()
      raise

  def _start(self):
    configure(*self.args)
    setup_logging(os.path.join(OUT, "slurp.log"))
    self.loop = asyncio.new_event_loop()
    self.requests = asyncio.Queue()
    self.ready = self.loop.create_future()
    self.task = self.loop.create_task(self.main())
    self.loop.run_until_complete(asyncio.wait_for(asyncio.shield(self.ready), 120))
    return self

  async def main(self):
    update_config(icons=self.icons,
                  start_page="jobs", # (the scenarios start on the job list; the overview has its own)
                  tabs=[{"destination": None}, {"destination": "lab"}], active=1) # (a local tab, and the lab shown)
    app = self.app = Slurp()
    try:
      async with app.run_test(size=self.size) as pilot:
        self.pilot = pilot
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory) # (as App.run does; run_test does not)
        await until(pilot, lambda: app.main is not None and app.cluster.connection is not None, 30)
        app.connect_tab() # (no login at the start: Connect)
        await until(pilot, lambda: "passphrase" in term(app), 30); await pilot.pause(0.3)
        await pilot.press(*"pw123", "enter")
        if not await until(pilot, lambda: app.ssh.ready and app.cluster.loaded, 30):
          raise RuntimeError(f"no login to the lab: {term(app)[-500:]}")
        self.ready.set_result(True)
        while (item := await self.requests.get()) is not None:
          scenario, done = item
          try:
            await scenario(app, pilot)
          except BaseException as e: # (to the test; the app may be left half way: the next test gets a new session)
            self.broken = f"{scenario.__name__} raised {e!r}"
            if not done.done(): done.set_exception(e)
            if isinstance(e, asyncio.CancelledError): raise
          else:
            if not done.done(): done.set_result(None)
        await pilot.press("q")
    except BaseException as e:
      if not self.ready.done(): self.ready.set_exception(e)
      raise

  def run(self, scenario, checks, timeout=900):
    """Run one scenario in the app (its checks go to checks); a scenario that does not end in time breaks the session"""
    global CURRENT
    if self.broken:
      raise RuntimeError(f"the session broke in an earlier test: {self.broken}")
    CURRENT = checks
    done = self.loop.create_future()
    self.requests.put_nowait((scenario, done))
    try:
      self.loop.run_until_complete(asyncio.wait_for(asyncio.shield(done), timeout))
    except TimeoutError:
      self.broken = f"{scenario.__name__} did not end in {timeout} s"
      raise
    finally:
      CURRENT = None

  def close(self):
    """End the app, put back the environment and the names of slurp's modules (a scenario that raised may have left
    a patch); twice is harmless"""
    if self.closed:
      return
    self.closed = True
    try:
      if self.loop is None or self.loop.is_closed():
        return
      if not self.task.done():
        if self.broken:
          self.task.cancel()
        else:
          self.requests.put_nowait(None)
        self.loop.run_until_complete(asyncio.wait({self.task}, timeout=60))
      for task in asyncio.all_tasks(self.loop):
        task.cancel()
      self.loop.run_until_complete(asyncio.sleep(0.1))
      self.loop.close()
    finally:
      os.environ.clear(); os.environ.update(self.saved_env)
      _categories.FILE = self.saved_file
      for name in ("slurp", "asyncssh"): # (the log file of this session)
        lg = logging.getLogger(name)
        for handler in [h for h in lg.handlers if isinstance(h, logging.FileHandler)]:
          handler.close(); lg.removeHandler(handler)
      for name, saved in self.saved_modules.items():
        module = sys.modules.get(name)
        if module is None:
          continue
        for key in [k for k in vars(module) if k not in saved]:
          delattr(module, key)
        for key, value in saved.items():
          if vars(module).get(key, object()) is not value:
            setattr(module, key, value)

async def t_titles(app, pilot):
  app.select_cluster(0)
  await until(pilot, lambda: app.cluster.loader is None and (app.cluster.no_slurm or app.cluster.loaded), 15)
  if app.cluster.no_slurm: # (this machine has no Slurm, as in the CI)
    note = app.main.query_one("#no-slurm")
    check("no Slurm on this machine: the job list says so, instead of errors", note.display and not app.main.query_one("#jobs").display
          and "No Slurm on this machine" in str(note.render()) and not any(t == "Local" and "squeue" in m for t, m in notes(app)), notes(app)[-3:])
  app.notify("y"); await pilot.pause(0.2)
  check("a notification on the local tab is titled Local", ("Local", "y") in notes(app), notes(app)[-2:])
  app.select_cluster(1); await pilot.pause(0.3)
  app.notify("x"); await pilot.pause(0.2)
  check("a notification on the lab tab is titled lab", ("lab", "x") in notes(app), notes(app)[-2:])

async def t_close(app, pilot):
  app.add_cluster(""); await pilot.pause(0.5)
  check("a third tab", len(app.clusters) == 3 and app.cluster_index == 2)
  await pilot.click("#cluster-close-2"); await pilot.pause(0.3)
  check("✕ asks for confirmation", isinstance(app.screen, ConfirmScreen))
  await pilot.click("#confirm-no"); await pilot.pause(0.3)
  check("No keeps the tab", len(app.clusters) == 3)
  await pilot.click("#cluster-close-2"); await pilot.pause(0.3)
  await pilot.click("#confirm-yes"); await pilot.pause(0.5)
  buttons = [b.id for b in app.screen.query("#cluster-tabs Button")]
  check("Yes closes it, the next tab is shown", len(app.clusters) == 2 and app.cluster_index == 1 and "cluster-tab-2" not in buttons, buttons)
  check("the remaining SSH tab still works", app.ssh.ready and app.screen.query_one("#jobs").row_count == 40, (app.ssh.state, app.screen.query_one("#jobs").row_count, len(app.jobs), type(app.screen).__name__))
  app.select_cluster(0); await pilot.pause(0.3)
  ssh = app.clusters[1].connection
  await pilot.click("#cluster-close-1"); await pilot.pause(0.3)
  await pilot.click("#confirm-yes"); await pilot.pause(1)
  check("closing another tab ends its SSH session", len(app.clusters) == 1 and ssh.state == "closed" and ssh.proc.poll() is not None
        and app.cluster_index == 0)
  await pilot.click("#cluster-close-0"); await pilot.pause(0.3)
  await pilot.click("#confirm-yes"); await pilot.pause(0.5)
  main = app.main
  check("the last tab can be closed: only + (and ? slurp) and a hint remain", not app.clusters and main.has_class("-no-tabs")
        and main.query_one("#notabs").display and [b.id for b in main.query("#cluster-tabs Button")] == ["cluster-add", "about"],
        [b.id for b in main.query("#cluster-tabs Button")])
  await pilot.press("r", "t", "c", "i", "C", "d"); await pilot.pause(0.3)
  check("keys do nothing without a tab", isinstance(app.screen, MainScreen) and app.shell is None)
  app.add_cluster("lab"); await pilot.pause(0.5)
  ok = await until(pilot, lambda: "passphrase" in term(app), 15); await pilot.pause(0.3)
  await pilot.press(*"pw123", "enter")
  ok = ok and await until(pilot, lambda: app.ssh.ready and app.cluster.loaded, 30)
  check("+ adds a tab again, the UI is back", ok and not main.has_class("-no-tabs") and main.query_one("#jobs").row_count == 40)

def row_styles(app):
  """background and foreground of the first cell of rows 0..3 as rendered"""
  t = app.main.query_one("#jobs")
  out = []
  for y in range(1, 5):
    strip = t.render_line(y)
    out.append(tuple(sorted({(str(sg.style.bgcolor and sg.style.bgcolor.triplet)[-30:], str(sg.style.color and sg.style.color.triplet)[-30:]) for sg in strip if sg.text.strip()})))
  return out

async def t_zebra(app, pilot):
  app.screen.query_one("#jobs").focus(); await pilot.pause(0.2)
  before = row_styles(app)
  app.add_cluster(""); await pilot.pause(0.5)
  app.select_cluster(1); await pilot.pause(0.5)
  after = row_styles(app)
  check("switching tabs", before == after, f"{before} -> {after}")
  t = app.screen.query_one("#jobs"); t.focus(); await pilot.press("enter")
  await until(pilot, lambda: isinstance(app.screen, JobScreen))
  app.select_cluster(2); await pilot.pause(0.5)
  app.select_cluster(1); await pilot.pause(0.5)
  app.show_view(None); await pilot.pause(0.5)
  after = row_styles(app)
  check("rows look the same after switching tabs", before == after, f"{before} -> {after}")
  await pilot.press("r"); await until(pilot, lambda: app.cluster.loader is None)
  await pilot.pause(0.3)
  check("rows look the same after a reload", row_styles(app) == before, row_styles(app))
  app.screen.query_one("#reload").focus(); await pilot.press("enter")
  await until(pilot, lambda: app.cluster.loader is None); await pilot.pause(0.3)
  check("also after a reload with the button, the table has the focus", isinstance(app.focused, ClickTable) and row_styles(app) == before)
  t = app.main.query_one("#jobs")
  for y in (2, 3):
    print("NONE-FG", y, [sg.text for sg in t.render_line(y) if sg.text.strip() and sg.style.color is None][:12])
  app.save_screenshot(filename="zebra.svg", path=OUT)
  app.close_cluster(app.clusters[2]); await pilot.pause(0.3)

async def t_reconnect(app, pilot):
  app.select_cluster(1); await pilot.pause(0.3)
  app.screen.query_one("#jobs").focus()
  await pilot.click("#connstatus"); await pilot.pause(0.5)
  await pilot.click("#conn-reconnect")
  ok = await until(pilot, lambda: "passphrase" in term(app), 15)
  check("Reconnect: the window closes, the login is in the pane and has the focus", ok and not isinstance(app.screen, ConnectionScreen)
        and app.shell is app.cluster.login_terminal and isinstance(app.focused, TerminalView), type(app.focused).__name__)
  await pilot.press(*"pw123", "enter")
  check("typed straight into it: logged in again", await until(pilot, lambda: app.ssh.ready and app.shell is None, 20))

async def t_connect_button(app, pilot):
  """Switching to a tab without a session does not log in: Connect (above the jobs it lists from before); Connect logs in"""
  app.select_cluster(1); await pilot.pause(0.3)
  cluster = app.cluster
  app.disconnect(cluster); await pilot.pause(0.5)
  app.select_cluster(0); await pilot.pause(0.5)
  app.select_cluster(1); await pilot.pause(0.8)
  panel, table = app.main.query_one("#connect-panel"), app.main.query_one("#jobs")
  check("switching to the disconnected tab does not log in", not cluster.connection.ready and cluster.connection.state != "connecting"
        and app.shell is not cluster.login_terminal, (cluster.connection.state, app.shell))
  note = str(app.main.query_one("#connect-note").render())
  check("…its job list shows Connect, as a bar above the jobs from before", panel.display and not panel.has_class("-center")
        and table.display and "Not connected to lab" in note and app.main.query_one("#connect").display, note)
  check("…and the status line says where to connect", "Connect in the job list" in str(app.connection_text(cluster)), app.connection_text(cluster))
  await pilot.click("#connect")
  ok = await until(pilot, lambda: "passphrase" in term(app), 15)
  check("Connect: the login is in the pane", ok and app.shell is cluster.login_terminal)
  await pilot.press(*"pw123", "enter")
  ok = await until(pilot, lambda: app.ssh.ready, 20); await pilot.pause(0.5)
  check("logged in: Connect is gone, the job list has the focus", ok and not panel.display and table.display
        and isinstance(app.focused, ClickTable), type(app.focused).__name__)
  # the overview (no subtab open): Disconnect while connected, then Connect
  app.close_jobs(); await pilot.pause(0.5)
  ov = app.screen
  check("the overview of a connected tab: Disconnect, no Connect", isinstance(ov, OverviewScreen)
        and ov.query_one("#ov-disconnect-row").display and not ov.query_one("#ov-connect-row").display, type(ov).__name__)
  await pilot.click("#ov-disconnect"); await pilot.pause(0.5)
  check("Disconnect ends the session: Connect instead", not cluster.connection.ready and ov.query_one("#ov-connect-row").display
        and not ov.query_one("#ov-disconnect-row").display, cluster.connection.state)
  await pilot.click("#ov-connect")
  ok = await until(pilot, lambda: "passphrase" in term(app), 15)
  check("Connect in the overview: the login is in the pane", ok and app.shell is cluster.login_terminal)
  await pilot.press(*"pw123", "enter")
  ok = await until(pilot, lambda: app.ssh.ready, 20); await pilot.pause(0.5)
  check("logged in again: Disconnect in the overview", ok and ov.query_one("#ov-disconnect-row").display)
  button = ov.query_one("#ov-disconnect")
  check("…its whole label shown (as wide as its text, after Abort while it logged in)",
        button.size.width >= len(str(button.label)), (str(button.label), button.size))
  app.show_view(None); await pilot.pause(0.4) # (the job list again, for the scenarios after this one)

async def t_restart(app, pilot):
  got = []
  app.push_screen(RestartScreen("9001", ["sbatch", "job.sh"]), got.append); await pilot.pause(0.3)
  ed = app.screen.query_one("#restart-command")
  check("restart command in a bash editor", isinstance(ed, TextArea) and ed.language == "bash" and ed.read_only and ed.text == "sbatch job.sh")
  app.screen.query_one("#restart-options").focus()
  list(app.screen.query(RadioButton))[2].value = True; await pilot.pause(0.3)
  check("Custom makes it editable", not ed.read_only, app.screen.query_one("#restart-options").pressed_index)
  ed.load_text("sbatch \\\n  --time=1:00:00 \\\n  job.sh")
  await pilot.pause(0.1)
  await pilot.click("#restart-submit"); await pilot.pause(0.3)
  check("a multi-line command is submitted as one", got and got[0]["args"] == ["sbatch", "--time=1:00:00", "job.sh"], got)

async def t_interactive(app, pilot):
  calls = os.path.join(LAB, "sbatch_calls")
  if os.path.exists(calls): os.unlink(calls)
  app.select_cluster(1); await pilot.pause(0.3)
  await menu(app, pilot, "interactive_job"); await pilot.pause(0.3)
  ed = app.screen.query_one("#command-text") if isinstance(app.screen, CommandScreen) else None
  check("the button opens the dialog with the command in a bash editor", ed is not None and ed.language == "bash" and "sleep infinity" in ed.text)
  ed.load_text(ed.text.replace("01:00:00", "00:30:00")); await pilot.pause(0.1)
  await pilot.click("#command-submit")
  ok = await until(pilot, lambda: os.path.exists(calls), 10)
  call = open(calls).read() if ok else ""
  check("the edited command is submitted in the login directory", "'--time=00:30:00'" in call and "'--wrap=sleep infinity'" in call
        and "cwd=" + app.ssh.home in call, call)
  check("the job id is shown", await until(pilot, lambda: any("submitted interactive job 9100" in m for _, m in notes(app)), 5))
  await pilot.press("i"); await pilot.pause(0.3)
  await pilot.press("escape"); await pilot.pause(0.3)
  check("i opens it too, Esc cancels", not isinstance(app.screen, CommandScreen) and open(calls).read().count("sbatch") <= 1)

def info_text(app):
  """what the Health window shows (as text), "" if it is not shown"""
  from rich.console import Console
  scr = app.screen
  if not isinstance(scr, ClusterInfoScreen) or not hasattr(scr, "shown"): return ""
  c = Console(width=200, record=True, file=open(os.devnull, "w")); c.print(scr.shown); return c.export_text()

def card_text(app):
  """the lines of the overview's card that are shown, "" if the overview is not shown"""
  scr = app.screen
  if not isinstance(scr, OverviewScreen): return ""
  return "\n".join(str(w.render()) for w in scr.query("#ov-card Static") if w.display)

async def t_cluster_info(app, pilot):
  """Health, a window: the cluster's health, occupancy, partitions, node topology; the overview, shown when no
  subtab is open: the cluster in a few lines, its health, the SSH session, Jobs, Quota and Health"""
  app.select_cluster(1); await pilot.pause(0.3)
  await menu(app, pilot, "cluster_info")
  ok = await until(pilot, lambda: "Occupancy" in info_text(app) and not app.screen.fetching, 30)
  t = info_text(app)
  check("Health in the menu: a window, no subtab", isinstance(app.screen, ClusterInfoScreen) and "health" not in vtabs(app), vtabs(app))
  check("cluster info: health", ok and "down" in t and "drained" in t and "not responding" in t and "maintenance" in t
        and "bad gpu" in t and "maint1" in t)
  check("cluster info: occupancy", "20 in total" in t and "13 (65%)" in t and "5 idle" in t and "5 running on 10 nodes" in t
        and "3 pending" in t and "Priority 3" in t)
  check("cluster info: partitions, node topology", "normal*" in t and "debug" in t and "Node topology" in t)
  title = str(app.screen.query_one("#ci-title").render())
  check("health in the title, coloured: 1 of 20 down is degraded (yellow)", "● degraded" in title and "3 of 20 nodes not usable" in title
        and app.screen.query_one("#ci-dialog").has_class("-warn"), title)
  info = dict(app.screen.info); info["states"] = dict(info["states"], down=5)
  check("5 of 20 down: unhealthy (red)", cluster_health(info)[0] == "bad")
  info["states"] = {"allocated": 20}; info["flags"] = collections.Counter(); info["reservations"] = []
  check("all up: healthy (green)", cluster_health(info) == ("good", "all 20 nodes usable"))
  app.save_screenshot(filename="cluster_info.svg", path=OUT)
  first = app.screen.info["time"]; await pilot.pause(1.1)
  await pilot.press("r"); await until(pilot, lambda: app.screen.info["time"] != first, 10)
  check("its own reload", app.screen.info["time"] != first)
  await pilot.press("escape"); await pilot.pause(0.4)
  check("Esc closes it: the job list", app.screen is app.main)
  app.close_jobs(); await pilot.pause(0.5)
  check("closing the job list too: nothing open, the overview", isinstance(app.screen, OverviewScreen) and not app.cluster.jobs_open)
  ok = await until(pilot, lambda: "degraded" in card_text(app) and "Slurm 23.11.4" in card_text(app), 20)
  t = card_text(app)
  check("the overview: what it is (Slurm, nodes, partitions), its health, the SSH session", ok and "lab" in t and "20 nodes in 2 partitions" in t
        and "● degraded: 3 of 20 nodes not usable" in t and "SSH" in t, t)
  jobs = str(app.screen.query_one("#ov-jobs").label)
  check("…Jobs (with how many run and wait), Quota, Health", "running" in jobs and "pending" in jobs
        and app.screen.query_one("#ov-quota").display and app.screen.query_one("#ov-info").display, jobs)
  check("…a few lines, not a report", app.screen.query_one("#ov-card").region.height <= 15) # (with SSH settings…: 15)
  app.save_screenshot(filename="overview.svg", path=OUT)
  await pilot.click("#ov-jobs"); await pilot.pause(0.4)
  check("Jobs: the job list, as a subtab again", app.screen is app.main and app.cluster.jobs_open and vtabs(app) == ["Jobs"], vtabs(app))

def slurm_calls():
  f = os.path.join(LAB, "slurm_calls")
  return open(f).read().splitlines() if os.path.exists(f) else []

async def t_accounts(app, pilot):
  sel = app.screen.query_one("#scope")
  opts = [v for _, v in sel._options]
  check("dropdown: my jobs, the Slurm accounts (sacctmgr), all my accounts", opts == ["me", "acct1", "proj2", "all"], opts)
  open(os.path.join(LAB, "slurm_calls"), "w").close()
  sel.value = "proj2"
  await until(pilot, lambda: app.cluster.loader is None and any("squeue" in c for c in slurm_calls())); await pilot.pause(0.3)
  calls = slurm_calls()
  check("one account: squeue/sacct -A proj2, User column", any(c.startswith("squeue -A proj2") for c in calls)
        and any("sacct -a -A proj2" in c for c in calls) and "user" in app.job_column_keys, calls)
  open(os.path.join(LAB, "slurm_calls"), "w").close()
  sel.value = "all"
  await until(pilot, lambda: app.cluster.loader is None and any("squeue" in c for c in slurm_calls())); await pilot.pause(0.3)
  check("all my accounts: -A acct1,proj2", any(c.startswith("squeue -A acct1,proj2") for c in slurm_calls()), slurm_calls())
  check("no id -Gn call", not any(c.startswith("id") for c in slurm_calls()))
  sel.value = "me"
  await until(pilot, lambda: app.cluster.loader is None); await pilot.pause(0.3)
  check("my jobs again: no User column", "user" not in app.job_column_keys)

async def t_sort(app, pilot):
  t = app.screen.query_one("#jobs")
  def header():
    return "".join(sg.text for sg in t.render_line(0))
  check("sorted by Job ID descending: ▼", "Job ID ▼" in header(), header()[:80])
  await pilot.click("#jobs", offset=(t.columns[next(k for k in t.columns if k.value == "name")].get_render_width(t) // 2 + 12, 0))
  await pilot.pause(0.3)
  check("click on Name: ▲ there, none on Job ID", "Name ▲" in header() and "Job ID ▼" not in header() and app.sort_column == "name", header()[:80])
  app.sort_column, app.sort_reverse = "name", False
  app.screen.query_one("#jobs")
  app.sort_by(type("E", (), {"column_key": type("K", (), {"value": "name"})()})()); await pilot.pause(0.3)
  check("again: ▼", "Name ▼" in header(), header()[:80])
  app.sort_by(type("E", (), {"column_key": type("K", (), {"value": "jobid"})()})()); await pilot.pause(0.3)

async def t_ci_noconn(app, pilot):
  """The overview of a tab without a session: Connect in its middle, with what happened; a tab without Slurm says so"""
  app.disconnect(app.cluster); await pilot.pause(0.5)
  app.close_jobs(); await pilot.pause(0.5)
  scr = app.screen
  t = card_text(app)
  check("the overview without connection: what happened, Connect in the middle", isinstance(scr, OverviewScreen)
        and "Not connected to lab" in t and scr.query_one("#ov-connect").display and not scr.query_one("#ov-links").display, t)
  await pilot.click("#ov-connect")
  ok = await until(pilot, lambda: "passphrase" in term(app), 15); await pilot.pause(0.3)
  check("Connect: the login is in front", ok and app.shell is app.cluster.login_terminal)
  await pilot.press(*"pw123", "enter")
  await until(pilot, lambda: app.ssh.ready, 20)
  ok = await until(pilot, lambda: "degraded" in card_text(app), 20)
  check("…logged in: the overview shows the cluster", ok, card_text(app))
  app.select_cluster(0); await pilot.pause(0.3)
  app.close_jobs()
  ok = await until(pilot, lambda: app.cluster.no_slurm and isinstance(app.screen, OverviewScreen) and app.screen.query_one("#ov-no-slurm").display, 10)
  check("a tab without Slurm (local): No Slurm available, no Jobs, no made-up health", ok and not app.screen.query_one("#ov-jobs").display
        and "nodes" not in card_text(app), card_text(app))
  app.show_view(None); await pilot.pause(0.2)
  app.select_cluster(1); await pilot.pause(0.3)
  app.show_view(None); await pilot.pause(0.3)

async def t_border(app, pilot):
  await pilot.press("t"); await pilot.pause(0.3); await pilot.press("enter") # (the choice: In slurp)
  ok = await until(pilot, lambda: isinstance(app.screen, TerminalScreen) and "(cluster)" in sterm(app), 15)
  await pilot.press(*"echo cols=$(tput cols) rows=$(tput lines)", "enter"); await pilot.pause(1)
  view = app.screen.query_one(TerminalView)
  check("the shell (a subtab) has a border and fits inside it", ok and view.styles.border_top[0] == "round"
        and f"cols={view.size.width} rows={view.size.height}" in sterm(app), f"{view.size} {sterm(app)[-200:]!r}")
  app.save_screenshot(filename="border.svg", path=OUT)
  app.action_close_view(); await pilot.pause(0.5)

async def t_term_help(app, pilot):
  await pilot.press("t"); await pilot.pause(0.3); await pilot.press("enter") # (In slurp)
  ok = await until(pilot, lambda: isinstance(app.screen, TerminalScreen) and "(cluster)" in sterm(app), 15)
  await pilot.press(*"echo HELPMARK", "enter"); await until(pilot, lambda: sterm(app).count("HELPMARK") >= 2, 5)
  scr = app.screen
  check("the shell subtab has a Help button", ok and scr.query("#shell-help"))
  await pilot.click("#shell-help"); await pilot.pause(0.5)
  help = app.screen
  md = "\n".join(m._markdown for m in help.query(Markdown)) if isinstance(help, TerminalHelpScreen) else ""
  check("Help opens the terminal help", isinstance(help, TerminalHelpScreen) and "Copying out" in md and "Pasting" in md
        and "login node" in md and "Ctrl+X" not in md and "subtab bar" in md, md[:200])
  app.save_screenshot(filename="term_help.svg", path=OUT)
  check("the screen text has the output", "HELPMARK" in screen_text(scr.session))
  got = []
  real = dialogs.copy_text; dialogs.copy_text = lambda app_, text, what="": got.append(text)
  try:
    await pilot.click("#thelp-copy"); await pilot.pause(0.4)
  finally:
    dialogs.copy_text = real
  check("Copy the screen copies the visible screen and closes", got and "HELPMARK" in got[0] and app.screen is scr, got[:1])
  check("the shell has the focus again", isinstance(app.focused, TerminalView))
  await pilot.click("#shell-help"); await pilot.pause(0.4); await pilot.press("escape"); await pilot.pause(0.3)
  check("Esc closes the help, keys go to the shell again", app.screen is scr and isinstance(app.focused, TerminalView))
  app.action_close_view(); await pilot.pause(0.5)

async def t_pane_help(app, pilot):
  """the ⓘ of the side pane, on a login (Reconnect puts the login of the tab in the pane)"""
  app.select_cluster(1); await pilot.pause(0.3)
  app.screen.query_one("#jobs").focus()
  await pilot.click("#connstatus"); await pilot.pause(0.5)
  await pilot.click("#conn-reconnect")
  ok = await until(pilot, lambda: "passphrase" in term(app), 15)
  check("a login in the pane to test (Reconnect)", ok and app.shell is not None)
  await pilot.click(".tinfo"); await pilot.pause(0.5)
  help = app.screen
  md = "\n".join(m._markdown for m in help.query(Markdown)) if isinstance(help, TerminalHelpScreen) else ""
  check("ⓘ of the pane opens the help, for the login", "login of the tab" in md and "Ctrl+X" in md and "gives up" in md, md[:150])
  app.save_screenshot(filename="pane_help.svg", path=OUT)
  await pilot.press("escape"); await pilot.pause(0.3)
  check("Esc: back, the pane has the focus", isinstance(app.focused, TerminalView))
  await pilot.press(*"pw123", "enter")
  check("the login in the pane goes on: logged in again", await until(pilot, lambda: app.ssh.ready and app.shell is None, 20))

def fv_ids(screen, sel):
  v = screen.query_one(sel)
  return [f.rule.id for f in v.findings]

async def t_checks_ui(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  ok = await until(pilot, lambda: app.cluster.jobs.get("9005") and app.cluster.jobs["9005"].checks is not None
                   and not getattr(app.cluster, "checking", False), 30); await pilot.pause(0.3)
  check("the checks of the list follow the load", ok)
  t = app.screen.query_one("#jobs"); keys = [k.value for k in t.columns]
  check("the job list has a Checks column", "checks" in keys, keys)
  cell = str(t.get_row("9005")[keys.index("checks")]) if "checks" in keys else ""
  check("job 9005 (a bad script) shows its errors in the list", "✖" in cell, (cell, [(f.rule.id, f.evidence) for f in (app.cluster.jobs["9005"].checks or [])]))
  await pilot.press("exclamation_mark"); await pilot.pause(0.3)
  rows = [k.value for k in t.rows]
  check("! shows only jobs with problems", "9005" in rows and len(rows) < len(app.cluster.jobs), rows[:8])
  await pilot.press("exclamation_mark"); await pilot.pause(0.3)
  check("! again shows all", len(list(t.rows)) > 8)
  t.focus(); t.move_cursor(row=t.get_row_index("9005")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.query_one("#job-checks").findings, 20)
  js = app.screen
  ids = fv_ids(js, "#job-checks") if ok else []
  check("the job window checks the submitted script: R01, R02, P01, S03, B05", all(i in ids for i in ("R01", "R02", "P01", "S03", "B05")),
        [(f.rule.id, f.evidence) for f in js.query_one("#job-checks").findings] if ok else "no findings")
  g01 = [f for f in js.query_one("#job-checks").findings if f.rule.id == "G01"] if ok else []
  check("G01 from the submit environment: modules and the uenv the script does not set up", g01 and g01[0].severity == "warning"
        and "cuda/12.4" in g01[0].evidence and "uenv" in g01[0].evidence, [(f.severity, f.evidence) for f in g01])
  check("the findings are collapsible entries, the most severe first", ok and len(js.query("#job-checks Collapsible")) == len(ids)
        and js.query_one("#job-checks").findings[0].severity == "error")
  app.save_screenshot(filename="checks_job.svg", path=OUT)
  first = js.query("#job-checks Collapsible").first(); first.collapsed = False; await pilot.pause(0.3)
  body = str(first.query_one(".fv-body").render())
  check("an entry opens: why it matters, how to fix, the catalogue link", "Why it matters" in body and "How to fix" in body and "rule catalogue" in body, body[:200])
  rid = js.query_one("#job-checks").findings[0].rule.id
  app.action_rule_catalogue(rid)
  ok = await until(pilot, lambda: isinstance(app.screen, RuleCatalogueScreen), 10)
  await pilot.pause(1.0)
  cat = app.screen
  viewer = cat.query_one("#doc")
  check(f"the rule link opens the catalogue at {rid}", ok and cat.rule_id == rid and viewer.document.scroll_y > 0 or viewer.scroll_y > 0,
        (cat.rule_id, viewer.scroll_y, viewer.document.scroll_y, cat.tries))
  app.save_screenshot(filename="checks_catalogue.svg", path=OUT)
  check("the catalogue is a subtab of its own", "Rule catalogue" in vtabs(app) and app.current_mode != "_default", vtabs(app))
  app.show_view(next(v for v in app.cluster.views if v.title == "job 9005")); await pilot.pause(0.3)
  app.action_rule_catalogue("B05"); await pilot.pause(1.0)
  check("another rule link: the same subtab, at the other rule", isinstance(app.screen, RuleCatalogueScreen) and app.screen is cat
        and cat.rule_id == "B05" and vtabs(app).count("Rule catalogue") == 1, vtabs(app))
  await pilot.press("escape"); await pilot.pause(0.3)
  check("Esc: the job list, the catalogue stays a subtab", app.screen is app.main and "Rule catalogue" in vtabs(app))
  # the script view
  wd = os.path.join(LAB, "jobs", "9001"); path = os.path.join(wd, "bad_job.sh")
  open(path, "w").write("#!/bin/bash\n#SBATCH --nodes=2\n#SBATCH --mem=8\n#SBATCH --output=missing_dir/%j.out\n"
                        "#SBATCH --cpus-per-task=8\nmodule load gcc\ncd nowhere\nsrun /bin/ls\n#SBATCH --time=01:00:00\n")
  open_path(app, ["Jobs"], path)
  ok = await until(pilot, lambda: isinstance(app.screen, ScriptScreen) and app.screen.findings, 20)
  sc = app.screen
  ids = [f.rule.id for f in sc.findings] if ok else []
  check("a batch script opens in the script view, checked: R01 P01 P02 S03 N04", all(i in ids for i in ("R01", "P01", "P02", "S03", "N04")),
        [(f.rule.id, f.line, f.evidence) for f in sc.findings] if ok else "not opened")
  sc.go_to_line(3); await pilot.pause(0.2)
  area = sc.query_one("#script-text")
  check("Go to line selects the line", area.selection.start[0] == 2 and "--mem=8" in area.selected_text, area.selected_text)
  await pilot.click("#sc-explain"); await pilot.pause(0.3)
  check("Explain annotates the #SBATCH lines", "⟵" in area.text and "8 MB per node" in area.text and "IGNORED" in area.text, area.text[:400])
  await pilot.click("#sc-explain"); await pilot.pause(0.3)
  app.save_screenshot(filename="checks_script.svg", path=OUT)
  await pilot.click("#sc-test")
  ok = await until(pilot, lambda: any("would start at" in n.message for n in app._notifications), 10)
  check("Test: sbatch --test-only, when it would start", ok, notes(app)[-2:])
  calls = os.path.join(LAB, "sbatch_calls"); n0 = len(open(calls).read().splitlines()) if os.path.exists(calls) else 0
  await pilot.click("#sc-submit"); ok = await until(pilot, lambda: isinstance(app.screen, dialogs.SubmitScriptScreen), 5)
  dlg = app.screen
  msg = " ".join(str(w.render()) for w in dlg.query("Static")) if ok else ""
  check("Submit asks, with the errors found, and the sbatch command to edit", ok and "errors" in msg
        and dlg.command() == ["sbatch", dlg.path], (msg[:200], ok and dlg.command()))
  arr = dlg.query_one("#submit-array"); arr.value = "1-4%2"; await pilot.pause(0.2)
  note = str(dlg.query_one("#submit-array-note").render())
  check("an array: the range writes --array into the command, and says how many tasks", dlg.command() == ["sbatch", "--array=1-4%2", dlg.path]
        and "4 tasks" in note and "at most 2" in note and "first 1, last 4" in note, (dlg.command(), note))
  arr.value = "1-10:4"; await pilot.pause(0.2)
  note = str(dlg.query_one("#submit-array-note").render())
  check("a step that misses the end: the first and last id, and a warning", "3 tasks" in note and "ids 1, 5, 9" in note
        and "the last id is 9, not 10" in note, note)
  arr.value = "1-4%2"; await pilot.pause(0.2)
  arr.value = "1-x"; await pilot.pause(0.2)
  check("a range that is none: said so, the command keeps the last good one", "not an array range" in str(dlg.query_one("#submit-array-note").render())
        and "--array=1-4%2" in dlg.command())
  editor = dlg.query_one("#submit-command")
  editor.load_text(f"sbatch --array=1-4%2 --job-name=edited {dlg.path}"); await pilot.pause(0.1)
  arr.value = "0-9:3"; await pilot.pause(0.2)
  check("an edited command: the range changes only its --array", dlg.command() == ["sbatch", "--array=0-9:3", "--job-name=edited", dlg.path],
        dlg.command())
  await pilot.click("#submit-submit")
  ok = await until(pilot, lambda: os.path.exists(calls) and len(open(calls).read().splitlines()) > n0, 10)
  last = open(calls).read().splitlines()[-1] if os.path.exists(calls) else ""
  check("Submit runs that command: the script, the array, the edit", ok and "bad_job.sh" in last and "--array=0-9:3" in last
        and "--job-name=edited" in last, last)
  await until(pilot, lambda: isinstance(app.screen, ScriptScreen), 5)
  check("one Check button (no separate Check environment)", not sc.query("#sc-env") and sc.query("#sc-check"))
  real_replay = environment.replay_environment
  def slow_replay(ctx):
    time.sleep(1.5); return real_replay(ctx)
  environment.replay_environment = slow_replay
  try:
    await pilot.click("#sc-check")
    ok1 = await until(pilot, lambda: sc.ctx is not None and sc.ctx.replay is None and "checking the environment" in str(sc.query_one("#sc-findings").query_one(".fv-summary").render()), 10)
    check("Check shows the quick results at once, saying the environment check follows", ok1)
    await pilot.pause(0.4) # (the entries of the quick results drawn: what a person could click)
    first = sc.query("#sc-findings Collapsible").first(); first.collapsed = False; key = first.finding_key
    ok2 = await until(pilot, lambda: sc.ctx is not None and sc.ctx.replay is not None, 20); await pilot.pause(0.5)
    still = [c for c in sc.query("#sc-findings Collapsible") if getattr(c, "finding_key", None) == key]
    check("an entry opened meanwhile stays open when the environment results come", ok2 and still and not still[0].collapsed)
  finally:
    environment.replay_environment = real_replay
  ok = await until(pilot, lambda: sc.ctx is not None and sc.ctx.replay is not None and (sc.ctx.replay or {}).get("module_errors"), 30)
  ids = [f.rule.id for f in sc.findings]
  # (the lab is a uenv cluster without a module system, its sbatch has --uenv: E08 instead of the module rule E04)
  check("then the environment: the setup replayed (the module load fails here; a uenv cluster: E08, not E04), ls checked with ldd",
        ok and "E08" in ids and "E04" not in ids and sc.ctx.facts.stacks == {"uenv"} and "/bin/ls" in (sc.ctx.replay or {}).get("libs", {}) and "checking the environment" not in str(sc.query_one("#sc-findings").query_one(".fv-summary").render()),
        (ids, sc.ctx.replay if sc.ctx else None))
  open(path, "a").write("#comment\n")
  ok = await until(pilot, lambda: "#comment" in sc.text, 10)
  check("a change of the file is checked again by itself", ok)
  await pilot.press("escape"); await pilot.pause(0.3)

async def t_checks_more(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  # the guides, and a rule link in them
  await pilot.press("f1")
  ok = await until(pilot, lambda: isinstance(app.screen, GuidesScreen), 15)
  g = app.screen
  check("F1 opens the guides, with the cluster's numbers", ok and "NUMA placement" in g.markdown and "normal" in g.markdown
        and "128 cores" in g.markdown, g.markdown[:200] if ok else "")
  doc = g.query_one("#doc").document
  doc.post_message(Markdown.LinkClicked(doc, "rule:P01")); await pilot.pause(1.0)
  check("a rule link in the guides opens the rule in the catalogue", isinstance(app.screen, RuleCatalogueScreen) and app.screen.rule_id == "P01")
  cat = app.screen
  d = cat.query_one("#doc").document
  d.post_message(Markdown.LinkClicked(d, "rule:B05")); await pilot.pause(0.8)
  check("a rule link inside the catalogue jumps within it", app.screen is cat and cat.rule_id == "B05")
  await pilot.press("escape"); await pilot.pause(0.2); await pilot.press("escape"); await pilot.pause(0.3)
  # the wizard
  app.screen.query_one("#jobs").focus()
  await pilot.press("n")
  ok = await until(pilot, lambda: isinstance(app.screen, NewScriptScreen) and app.screen.facts is not None, 15)
  w = app.screen
  w.query_one("#ns-kind").value = "gpu"; await pilot.pause(0.5)
  pv = w.query_one("#ns-preview").text
  check("New job script: a GPU template with the partition's numbers", ok and "--gpus-per-task=1" in pv and "--ntasks-per-node=4" in pv
        and "--cpus-per-task=32" in pv, pv[:500])
  path = os.path.join(LAB, "jobs", "9002", f"wiz{int(time.time())}.sh")
  w.query_one("#ns-path").value = path; await pilot.pause(0.6)

  await pilot.click("#ns-create")
  ok = await until(pilot, lambda: isinstance(app.screen, ScriptScreen) and app.screen.path == path and app.screen.ctx is not None, 20)
  sc = app.screen
  bad = [(f.rule.id, f.evidence) for f in sc.findings if f.severity != "info" and f.rule.id not in ("P03", "E04")] if ok else "not opened"  # (the lab has no module command)
  check("Create writes it (and logs/), opens it in the script view, without errors or warnings",
        ok and os.path.isdir(os.path.join(os.path.dirname(path), "logs")) and not bad, bad)
  await pilot.press("escape"); await pilot.pause(0.3)
  # fixed copy
  bad_path = os.path.join(LAB, "jobs", "9002", "tofix.sh")
  open(bad_path, "w").write("#!/bin/bash\r\n#SBATCH --nodes=1\r\n#SBATCH --cpus-per-task=4\r\n#SBATCH --output=o.txt\r\nmodule load gcc/1\r\nsrun ./x\r\n#SBATCH --time=01:00:00\r\n")
  for f in os.listdir(os.path.dirname(bad_path)):
    if f.startswith("tofix.fixed"): os.remove(os.path.join(os.path.dirname(bad_path), f))
  open_path(app, ["Jobs"], bad_path)
  ok = await until(pilot, lambda: isinstance(app.screen, ScriptScreen) and app.screen.path == bad_path and app.screen.findings, 20)
  sc = app.screen
  await pilot.click("#sc-fix")
  ok = await until(pilot, lambda: isinstance(app.screen, FixPreviewScreen), 5)
  done = app.screen.done if ok else []
  diff = app.screen.diff if ok else ""
  check("Fixed copy shows what it fixes, as a diff", ok and any("S01" in d for d in done) and any("S03" in d for d in done)
        and "+#SBATCH --time" in diff, (done, diff[:300]))
  await pilot.click("#fx-write")
  fixed = os.path.join(os.path.dirname(bad_path), "tofix.fixed.sh")
  ok = await until(pilot, lambda: isinstance(app.screen, ScriptScreen) and app.screen.path == fixed and app.screen.findings is not None and app.screen.ctx, 20)
  ids = [f.rule.id for f in app.screen.findings] if ok else []
  check("Write: the fixed copy opens, checked: S01 S03 N04 gone", ok and os.path.exists(fixed) and not {"S01", "S03", "N04"} & set(ids)
        and "\r" not in open(fixed, newline="").read(), ids)
  check("the original is unchanged", "\r" in open(bad_path, newline="").read())
  # mute in this script / everywhere
  sc2 = app.screen
  f = next((x for x in sc2.findings if x.rule.id == "E01"), None)
  if f:
    sc2.mute_in_script("E01")
    ok = await until(pilot, lambda: "# slurp: ignore E01" in sc2.text and not any(x.rule.id == "E01" for x in sc2.findings), 10)
    check("Mute in this script adds # slurp: ignore and the finding goes", ok, sc2.text[:120])
  await until(pilot, lambda: sc2.ctx is not None and sc2.ctx.replay is not None, 20) # (both phases of the check)
  await pilot.pause(0.3)
  rid = sc2.findings[0].rule.id if sc2.findings else None
  if rid:
    view = sc2.query_one("#sc-findings")
    view.query(".fv-mute").first().press(); await pilot.pause(0.2)
    ok = await until(pilot, lambda: not any(x.rule.id == rid for x in sc2.findings) and sc2.ctx is not None, 10)
    check(f"Mute everywhere: {rid} goes into the config and is not reported", ok and rid in load_config().get("checks_muted", []))
    update_config(checks_muted=[])
  for _ in range(4):
    if app.screen is app.main: break
    await pilot.press("escape"); await pilot.pause(0.3)
  # what changed (a failed job)
  t = app.screen.query_one("#jobs"); failed = next((j for j in app.cluster.jobs.values() if j.state.startswith("FAILED") and "_" not in j.jid), None)
  check("there is a failed job in the list", failed is not None, (app.cluster.name, type(app.screen).__name__, sorted({j.state for j in app.cluster.jobs.values()})))
  if failed is None: return
  t.focus(); t.move_cursor(row=t.get_row_index(failed.jid)); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.query_one("#checks-changed").display, 10)
  js = app.screen
  ids = [f.rule.id for f in js.query_one("#job-checks").findings] if await until(pilot, lambda: js.query_one("#job-checks").findings, 15) else []
  check("a failed job: U03 explains why", "U03" in ids, ids)
  await pilot.click("#checks-changed")
  ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and "What changed" in app.screen.query_one("#text").text, 15)
  check("What changed compares with the last run that worked", ok, app.screen.query_one("#text").text[:300] if ok else type(app.screen))
  await pilot.press("escape"); await pilot.pause(0.2); await pilot.press("escape"); await pilot.pause(0.3)
  # node topology in Health
  await pilot.press("C")
  ok = await until(pilot, lambda: "Node topology" in info_text(app) and not app.screen.fetching, 30)
  info = app.screen.info if isinstance(app.screen, ClusterInfoScreen) else {}
  check("Health shows the node topology", ok and info.get("facts") and info["facts"].partitions["normal"].cores == 64)
  await pilot.press("escape"); await pilot.pause(0.3)
  # B09: heavy processes on the login node (said once)
  real = checks.login_hogs
  checks.login_hogs = lambda: [(4242, 99.0, 20, "heavy_sim")]
  try:
    app.check_login_nodes(); await until(pilot, lambda: any("heavy_sim" in m for _, m in notes(app)), 10)
    n1 = sum("heavy_sim" in m for _, m in notes(app))
    app.check_login_nodes(); await pilot.pause(1.0)
    n2 = sum("heavy_sim" in m for _, m in notes(app))
    check("B09: heavy work on the login node is said, once", n1 == 1 and n2 == 1, (n1, n2))
  finally:
    checks.login_hogs = real

async def t_view_help(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  import inspect
  classes = {n for m in slurp_modules() for n, o in vars(m).items() if inspect.isclass(o) and issubclass(o, Screen)}
  missing = sorted(n for n in classes if n.endswith("Screen") and n not in VIEW_HELP and n not in ("SplitScreen", "DocScreen", "ViewHelpScreen", "TerminalHelpScreen", "ModalScreen", "Screen"))
  check("every window and dialog has a help text", not missing, missing)
  check("the job list has a ? in its top bar", bool(app.screen.query("#view-help")))
  await pilot.click("#view-help")
  ok = await until(pilot, lambda: isinstance(app.screen, ViewHelpScreen), 5)
  md = app.screen.markdown if ok else ""
  check("? opens the help of the job list: text, its keys, the keys of everywhere", "# Job list" in md and "Only problems" in md
        and "New job script" in md and "## Everywhere" in md and "Interactive job" in md and "Focus Next" not in md, md[:300])
  app.save_screenshot(filename="help_jobs.svg", path=OUT)
  await pilot.press("escape"); await pilot.pause(0.3)
  check("Esc closes it", app.screen is app.main)
  t = app.screen.query_one("#jobs"); t.focus(); await pilot.press("question_mark")
  ok = await until(pilot, lambda: isinstance(app.screen, ViewHelpScreen), 5)
  check("the ? key opens it too", ok)
  await pilot.press("question_mark"); await pilot.pause(0.3)
  check("? again closes it", app.screen is app.main)
  t.focus(); t.move_cursor(row=t.get_row_index("9001")); await pilot.press("enter")
  await until(pilot, lambda: isinstance(app.screen, JobScreen), 10)
  await pilot.click("#view-help"); ok = await until(pilot, lambda: isinstance(app.screen, ViewHelpScreen), 5)
  check("the job window's ? explains the job window", ok and "# Job window" in app.screen.markdown and "Shell on node" in app.screen.markdown)
  await pilot.press("escape"); await pilot.pause(0.2); await pilot.press("escape"); await pilot.pause(0.3)
  await pilot.press("f3")
  ok = await until(pilot, lambda: isinstance(app.screen, OptionsScreen) and app.screen.query(".help-corner"), 10)
  check("a dialog (Options) has a ? in its corner", ok)
  if ok:
    app.screen.query_one(".help-corner").press()
    ok = await until(pilot, lambda: isinstance(app.screen, ViewHelpScreen), 5)
    check("its ? explains it", ok and "# Options" in app.screen.markdown, app.screen.markdown[:100] if ok else "")
    app.save_screenshot(filename="help_ci.svg", path=OUT)
    await pilot.press("escape"); await pilot.pause(0.2)
  await pilot.press("escape"); await pilot.pause(0.3)
  await pilot.press("t"); await pilot.pause(0.3); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, TerminalScreen), 15)
  await pilot.click("#view-help"); ok = await until(pilot, lambda: isinstance(app.screen, TerminalHelpScreen), 5)
  check("a shell's ? opens its terminal help", ok)
  await pilot.press("escape"); await pilot.pause(0.3)
  app.action_close_view(); await pilot.pause(0.5)

async def t_scrollback(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  update_config(terminal_scrollback=10000)
  await pilot.press("t"); await pilot.pause(0.3); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, TerminalScreen) and "(cluster)" in sterm(app), 15)
  scr = app.screen; view = scr.query_one(TerminalView); vt = scr.session.vt
  await pilot.press(*"seq 1 300", "enter")
  ok = await until(pilot, lambda: "\n300" in sterm(app) or sterm(app).splitlines()[-2:] and any(l.strip() == "300" for l in sterm(app).splitlines()), 10)
  check("lines that scroll off the screen go into the history", ok and len(vt.history) >= 250, len(vt.history))
  for _ in range(10): view.wheel(-1)
  await pilot.pause(0.2)
  top = view.render_line(0).text; second = view.render_line(1).text.strip()
  check("the wheel scrolls back through the history, a bar says how far", view.scroll == 30 and "lines back" in top
        and second.isdigit() and int(second) < 250, (view.scroll, top[-60:], second))
  # select by dragging (real mouse events, 1 cell border)
  got = []
  real = dialogs.copy_text; dialogs.copy_text = lambda app_, text, what="": got.append(text)
  try:
    await pilot.mouse_down(view, offset=(1, 3)); await pilot.hover(view, offset=(4, 5)); await pilot.mouse_up(view, offset=(4, 5))
    await pilot.pause(0.3)
    check("dragging selects (in the history too), without copying yet; the top row says how to copy",
          not got and view.selection and "right-click on it copies" in view.render_line(0).text, (got, view.render_line(0).text[-60:]))
    check("the selection is drawn reversed", view.selection and view.in_selection(view.row_at(2), 1))
    await pilot.click(view, offset=(3, 4), button=3); await pilot.pause(0.3)
    lines = got[0].splitlines() if got else []
    check("a right-click on the selection copies it", len(lines) == 3 and all(l.strip().isdigit() for l in lines)
          and int(lines[1]) == int(lines[0]) + 1 and view.selection is None, got)
    got.clear()
    await pilot.mouse_down(view, offset=(1, 3)); await pilot.hover(view, offset=(4, 5)); await pilot.mouse_up(view, offset=(4, 5))
    await pilot.click(view, offset=(10, 12), button=3); await pilot.pause(0.3)
    check("a right-click elsewhere copies nothing and removes the selection", not got and view.selection is None, got)
    await pilot.mouse_down(view, offset=(1, 3)); await pilot.hover(view, offset=(4, 5)); await pilot.mouse_up(view, offset=(4, 5))
  finally:
    dialogs.copy_text = real
  await pilot.press("shift+pagedown"); await pilot.press("shift+pagedown"); await pilot.pause(0.2)
  check("Shift+PgDn scrolls down to the bottom", view.scroll == 0, view.scroll)
  await pilot.press("shift+pageup"); await pilot.pause(0.2)
  check("Shift+PgUp scrolls a page back", view.scroll > 0, view.scroll)
  await pilot.press("e"); await pilot.pause(0.3)
  check("typing goes back to the bottom and ends the selection", view.scroll == 0 and view.selection is None)
  await pilot.press("backspace", *"clear", "enter")
  ok = await until(pilot, lambda: len(vt.history) == 0, 5)
  check("clear empties the history", ok, len(vt.history))
  app.action_close_view(); await pilot.pause(0.5)
  update_config(terminal_scrollback=50)
  await pilot.press("t"); await pilot.pause(0.3); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, TerminalScreen) and "(cluster)" in sterm(app), 15)
  check("the history length comes from the config", ok and app.screen.session.vt.history.maxlen == 50)
  app.action_close_view(); await pilot.pause(0.5)
  update_config(terminal_scrollback=10000)

async def t_this_view(app, pilot):
  """slurp in a browser through a web link: its session is marked "this view"; disconnecting it, or revoking the
  link, warns that this ends the very view"""
  await until(pilot, lambda: app.cluster.loader is None, 30)
  mgr = app.link_manager
  link = Link("web", "this slurp on lab", app.cluster.name, lambda: [], relay="ssh-j.com", uses=None, web_port=1, web_token="t")
  link.relay_state = "ready"
  mine = Session(link, "s1", kind="web", client="SSH-2.0-OpenSSH_9.6", web_ports={40001, 40002})
  other = Session(link, "s2", kind="web", client="SSH-2.0-PuTTY", web_ports={40003})
  link.sessions += [mine, other]; mgr.links.append(link)
  kind = app.view_kind
  try:
    app.active_view_changed(kind, 0)
    for _ in range(3):
      await pilot.click("#connstatus")
      if await until(pilot, lambda: isinstance(app.screen, ConnectionScreen), 3):
        break
    cs = app.screen
    table = cs.query_one("#conn-links")
    cell = lambda sid: str(table.get_row(f"session|{link.id}|{sid}")[0])
    ok = await until(pilot, lambda: f"session|{link.id}|s1" in [k.value for k in table.rows], 5)
    check("a terminal view: no session is marked", ok and "this view" not in cell("s1") + cell("s2"), (cell("s1"), cell("s2")))
    app.active_view_changed("web", 40002); cs.show_links(); await pilot.pause(0.3)
    check("the browser of a web session is the active view: its session is marked, only it", "◀ this view" in cell("s1")
          and "this view" not in cell("s2"), (cell("s1"), cell("s2")))
    app.save_screenshot(filename="this_view.svg", path=OUT)
    table.focus(); table.move_cursor(row=table.get_row_index(f"session|{link.id}|s2")); await pilot.press("enter"); await pilot.pause(0.4)
    text = str(app.screen.dialog_message) if isinstance(app.screen, ConfirmScreen) else ""
    check("disconnecting another session: no warning", text and "this very view" not in text, text)
    await pilot.press("escape"); await pilot.pause(0.3)
    table.move_cursor(row=table.get_row_index(f"session|{link.id}|s1")); await pilot.press("enter"); await pilot.pause(0.4)
    text = str(app.screen.dialog_message) if isinstance(app.screen, ConfirmScreen) else ""
    check("disconnecting the session of this view warns that it ends this very view", "ends this very view" in text, text)
    app.save_screenshot(filename="this_view_disconnect.svg", path=OUT)
    await pilot.press("escape"); await pilot.pause(0.3)
    check("No: nothing is disconnected", mine.ended is None and isinstance(app.screen, ConnectionScreen))
    table.move_cursor(row=table.get_row_index(f"link|{link.id}")); await pilot.press("enter"); await pilot.pause(0.5)
    lv = app.screen
    state = lambda: rendered(lv.query_one("#lv-state").content) if isinstance(lv, LinkScreen) else ""
    await until(pilot, lambda: "Sessions" in state(), 3)
    state = state()
    check("the link's window says one of its sessions is this view", "one is this view" in state, (type(lv).__name__, state[-300:]))
    await pilot.click("#lv-revoke"); await pilot.pause(0.4)
    text = str(app.screen.dialog_message) if isinstance(app.screen, ConfirmScreen) else ""
    check("revoking the link of this view warns too", "revoking it ends this very view" in text, text)
    await pilot.press("escape"); await pilot.pause(0.2)
    app.active_view_changed("terminal", 0); lv.refresh_view(); await pilot.pause(0.2)
    await pilot.click("#lv-revoke"); await pilot.pause(0.4)
    text = str(app.screen.dialog_message) if isinstance(app.screen, ConfirmScreen) else ""
    check("from a terminal: revoking it does not warn", text and "this very view" not in text, text)
    await pilot.press("escape"); await pilot.pause(0.2); await pilot.press("escape"); await pilot.pause(0.2); await pilot.press("escape"); await pilot.pause(0.3)
  finally:
    app.active_view_changed(kind, 0)
    mgr.links.remove(link)

async def t_web_links_everywhere(app, pilot):
  """A web link is this whole slurp, not one tab: the connection window of every tab lists it; a shell link only its own"""
  mgr = app.link_manager
  web = Link("web", "this slurp on lab", app.clusters[1].name, lambda: [], relay="ssh-j.com", uses=None, web_port=1, web_token="t")
  shell = Link("shell", "shell (lab)", app.clusters[1].name, lambda: [], relay="ssh-j.com")
  for link in (web, shell):
    link.relay_state = "ready"
    mgr.links.append(link)
  try:
    rows = {}
    for i in (0, 1):
      app.push_screen(ConnectionScreen(app.clusters[i])); await pilot.pause(0.5)
      cs = app.screen
      if isinstance(cs, ConnectionScreen):
        cs.show_links(); await pilot.pause(0.2)
        table = cs.query_one("#conn-links")
        rows[i] = [k.value for k in table.rows] if table.display else []
      await pilot.press("escape"); await pilot.pause(0.3)
    check("the lab tab's window lists both its links", rows.get(1) and f"link|{web.id}" in rows[1] and f"link|{shell.id}" in rows[1], rows)
    check("another tab's window lists the web link, not the shell link of lab", rows.get(0) is not None and f"link|{web.id}" in rows[0]
          and f"link|{shell.id}" not in rows[0], rows)
  finally:
    for link in (web, shell):
      mgr.links.remove(link)

async def t_cloudflare_ask(app, pilot):
  """No cloudflared here: Create link asks before downloading it (No: nothing happens); Download: the link's window
  shows downloading cloudflared (with the MB), then installing cloudflared, then the tunnel"""
  import shutil
  import slurp.links.cloudflare as cf
  await until(pilot, lambda: app.cluster.loader is None, 30)
  shutil.rmtree(cf.data_dir(), ignore_errors=True)
  fake = os.path.join(OUT, "fake-cloudflared-dl")
  open(fake, "w").write("""#!/bin/sh
[ "$1" = "--version" ] && { echo "cloudflared version 2099.1.0 (fake)"; exit 0; }
echo "2099-01-01T00:00:00Z INF |  https://fake-download-words.trycloudflare.com  |" >&2
sleep 0.3
echo "2099-01-01T00:00:01Z INF Registered tunnel connection connIndex=0 location=zrh" >&2
exec sleep 300
""")
  os.chmod(fake, 0o755)
  calls = []
  def download(progress=lambda d, t: None, installing=lambda: None):
    calls.append("download")
    for mb in (4, 20, 40):
      progress(mb << 20, 40 << 20); time.sleep(0.6)
    installing(); time.sleep(1.2)
    os.makedirs(cf.data_dir(), exist_ok=True)
    path = os.path.join(cf.data_dir(), "cloudflared"); shutil.copy(fake, path)
    return path
  saved = (cf.download, cf.resolves, shutil.which)
  before = load_config().get("link_relay")
  update_config(cloudflared=None, link_relay_web="cloudflare")
  cf.download, cf.resolves = download, (lambda host: True)
  cf.shutil.which = lambda name: None if name == "cloudflared" else saved[2](name) # (none on PATH either)
  link = None
  try:
    check("no cloudflared here", cf.binary() is None)
    app.cf_ask = None
    app.push_screen(LinkSetupScreen("web", "this slurp on lab", app.cluster.name, lambda: [], web=(1, "tok-dl")),
                    lambda l: setattr(app, "cf_ask", l)); await pilot.pause(0.5)
    await pilot.click("#setup-create"); await pilot.pause(0.5)
    ask = app.screen
    text = str(ask.dialog_message) if isinstance(ask, ConfirmScreen) else ""
    check("Create link asks first: what cloudflared is, how big, from where, to where", "Download cloudflared?" in getattr(ask, "dialog_title", "")
          and "40 MB" in text and "github.com/cloudflare/cloudflared" in text and cf.data_dir() in text, text)
    app.save_screenshot(filename="cloudflare_ask.svg", path=OUT)
    await pilot.click("#confirm-no"); await pilot.pause(0.4)
    check("No: nothing downloaded, no link, the setup panel stays", not calls and app.cf_ask is None and isinstance(app.screen, LinkSetupScreen))
    await pilot.click("#setup-create"); await pilot.pause(0.5); await pilot.click("#confirm-yes")
    ok = await until(pilot, lambda: app.cf_ask is not None, 5)
    link = app.cf_ask
    seen = []
    def watch():
      text = str(link_state(link)[0])
      if not seen or seen[-1] != text: seen.append(text)
      return link.ready()
    ok = ok and await until(pilot, watch, 20)
    check("Download: the link's states under Relay: downloading cloudflared (MB), installing cloudflared, connecting, ready",
          ok and any(t.startswith("downloading cloudflared: ") and "of 40 MB" in t for t in seen) and any(t.startswith("installing cloudflared") for t in seen)
          and seen.index(next(t for t in seen if t.startswith("installing"))) > seen.index(next(t for t in seen if t.startswith("downloading")))
          and seen[-1].startswith("ready, at https://fake-download-words"), seen)
    check("its health meanwhile: downloading cloudflared (degraded, as connecting)", link_health(Link("web", "x", "t", lambda: [], relay="cloudflare",
          relay_state="downloading")) == ("warn", "downloading cloudflared"))
    check("then cloudflared is there: the next link does not ask", calls == ["download"] and cf.binary() == os.path.join(cf.data_dir(), "cloudflared"))
  finally:
    cf.download, cf.resolves, cf.shutil.which = saved
    update_config(link_relay_web=None, link_relay=before)
    if link is not None:
      app.link_manager.revoke(link)
      if link in app.link_manager.links: app.link_manager.links.remove(link)
    shutil.rmtree(cf.data_dir(), ignore_errors=True)
    while not isinstance(app.screen, MainScreen) and len(app.screen_stack) > 1:
      await pilot.press("escape"); await pilot.pause(0.2)

async def t_web_cloudflare(app, pilot):
  """A web link through Cloudflare: the choice (web links only), its panel (no logins, no password), the quick tunnel
  (a fake cloudflared), the link window (the https address, no password, no helper), its own setting; Revoke ends it"""
  import slurp.links.cloudflare as cf
  await until(pilot, lambda: app.cluster.loader is None, 30)
  fake = os.path.join(OUT, "fake-cloudflared")
  marker = os.path.join(OUT, "fake-cloudflared.args")
  open(fake, "w").write(f"""#!/bin/sh
[ "$1" = "--version" ] && {{ echo "cloudflared version 2099.1.0 (fake)"; exit 0; }}
echo "$@" > {marker}
echo "2099-01-01T00:00:00Z INF |  https://fake-words-here.trycloudflare.com  |" >&2
sleep 0.5
echo "2099-01-01T00:00:01Z INF Registered tunnel connection connIndex=0 location=zrh" >&2
exec sleep 300
""")
  os.chmod(fake, 0o755)
  before = load_config().get("link_relay")
  update_config(cloudflared=fake, link_relay="lab-relay", link_relay_web=None)
  resolves, kind = cf.resolves, app.view_kind
  cf.resolves = lambda host: True # (the fake name is in no DNS)
  try:
    check("parse_relay: cloudflare; a public relay; straight (no jump)", parse_relay("cloudflare") == ("cloudflare", "trycloudflare.com")
          and is_public("cloudflare") and Link("web", "x", "t", lambda: [], relay="cloudflare").jump() == "")
    try:
      app.link_manager.create(Link("shell", "x", app.cluster.name, lambda: [], relay="cloudflare")); refused = False
    except ValueError:
      refused = True
    check("a shell (or drive) link through Cloudflare is refused", refused)
    app.push_screen(LinkSetupScreen("shell", "a shell", app.cluster.name, lambda: [])); await pilot.pause(0.5)
    check("the shell's panel has no Cloudflare choice", not app.screen.query("#setup-relay-cloudflare"))
    await pilot.press("escape"); await pilot.pause(0.3)
    app.push_screen(LinkSetupScreen("web", "this slurp on lab", app.cluster.name, lambda: [], web=(1, "tok-cf")),
                    lambda link: setattr(app, "cf_link", link)); await pilot.pause(0.5)
    scr = app.screen
    check("the web panel offers Cloudflare; the relay of shells (lab-relay) is chosen until the web links have their own",
          scr.query("#setup-relay-cloudflare") and scr.chosen() == "custom", scr.chosen())
    scr.query_one("#setup-relay-cloudflare").value = True; await pilot.pause(0.4)
    hidden = [r for r in scr.query(".setup-ssh-only") if r.display]
    warning = rendered(scr.query_one("#setup-warning").content)
    intro = rendered(scr.query_one("#setup-web-intro").content)
    check("Cloudflare chosen: no logins or password rows, its explanation, the intro says: open the address",
          not hidden and "quick tunnel" in warning and "Cloudflare ends the HTTPS" in warning and "https:// address" in intro, (len(hidden), warning[:200], intro[:120]))
    app.save_screenshot(filename="cloudflare_setup.svg", path=OUT)
    await pilot.click("#setup-create")
    ok = await until(pilot, lambda: getattr(app, "cf_link", None) is not None and app.cf_link.ready(), 15)
    link = getattr(app, "cf_link", None)
    check("Create: the quick tunnel is ready at the address cloudflared gave", ok and link.public_host == "fake-words-here.trycloudflare.com",
          (link and link.relay_state, link and link.relay_error))
    args = open(marker).read() if os.path.exists(marker) else ""
    check("cloudflared runs the quick tunnel to the web view, HTTP/2, its own config", "tunnel --config" in args and "--protocol http2" in args
          and "--url http://127.0.0.1:1" in args, args)
    check("the setting: the web links' own relay (cloudflare), the shells' stays", load_config().get("link_relay_web") == "cloudflare"
          and load_config().get("link_relay") == "lab-relay")
    check("its address: https, with the token", link.url() == "https://fake-words-here.trycloudflare.com/?token=tok-cf", link.url())
    lv = LinkScreen(link); app.push_screen(lv); await pilot.pause(0.6)
    await until(pilot, lambda: lv.query(".lv-copy"), 5)
    state = rendered(lv.query_one("#lv-state").content)
    texts = " ".join(rendered(w.content) for w in lv.query(".lv-command-text"))
    check("the link window: the address; no password, host key, helper or ssh command", link.url() in state and "Password" not in state
          and "Host key" not in state and not lv.query("#lv-helper") and not lv.query("#lv-password") and link.url() in texts and "ssh" not in texts,
          (state, texts))
    await pilot.pause(0.8); app.save_screenshot(filename="cloudflare_link.svg", path=OUT)
    app.active_view_changed("web", 0, "fake-words-here.trycloudflare.com"); lv.refresh_view(); await pilot.pause(0.3)
    state = rendered(lv.query_one("#lv-state").content)
    check("the browser in use came through this address: the link window says it is this view", "this view" in state, state)
    await pilot.click("#lv-revoke"); await pilot.pause(0.4)
    text = str(app.screen.dialog_message) if isinstance(app.screen, ConfirmScreen) else ""
    check("and Revoke warns that it ends this very view", "revoking it ends this very view" in text, text)
    await pilot.click("#confirm-yes"); await pilot.pause(0.5)
    ok = await until(pilot, lambda: (app.link_manager.tick(), link.relay_state == "closed")[1], 10)
    check("Revoke: cloudflared ends, the link is closed", ok and link.tunnel is None, link.relay_state)
    await pilot.press("escape"); await pilot.pause(0.3)
  finally:
    app.active_view_changed(kind, 0, "")
    cf.resolves = resolves
    update_config(cloudflared=None, link_relay_web=None, link_relay=before)
    if getattr(app, "cf_link", None) in app.link_manager.links:
      app.link_manager.revoke(app.cf_link); app.link_manager.links.remove(app.cf_link)

async def t_about(app, pilot):
  """? slurp at the right of the tabs: slurp in general, its version, the machine it runs on; Copy details; in the menu"""
  import socket
  import slurp
  await until(pilot, lambda: app.cluster.loader is None, 30)
  app.show_view(None); await pilot.pause(0.3)
  button = app.screen.query("#about")
  region = button.first().region if button else None
  check("the job list has ? slurp at the right of the tabs (under the header)", button and region.y == 1
        and region.right >= app.size.width - 2, region)
  await pilot.click("#about")
  ok = await until(pilot, lambda: isinstance(app.screen, AboutScreen), 10)
  md = app.screen.markdown if ok else ""
  check("it opens About slurp: the version, how slurp works", ok and f"About slurp {slurp.VERSION_TEXT}" in md and "The job list" in md, md[:200])
  check("the machine slurp runs on: host, system, CPUs, memory, user", all(x in md for x in (socket.getfqdn(), "kernel", "| CPUs |", "GiB", "uid"))
        and "This machine (slurp runs here)" in md, md[-1500:])
  check("this slurp: process, how it runs, the active tab, Python and libraries", all(x in md for x in ("pid ", "| runs |", "| active tab |", "textual ", "Python")), md[-900:])
  check("Copy details: the same as text", app.screen.query("#vh-copy") and slurp.__version__ in app.screen.details and socket.getfqdn() in app.screen.details)
  await pilot.pause(0.5); app.save_screenshot(filename="about.svg", path=OUT)
  await pilot.press("escape"); await pilot.pause(0.3)
  from slurp.ui.screens.menu import ITEMS
  check("the menu has About slurp", ("about", "About slurp", "", "about") in ITEMS)

async def t_links_health(app, pilot):
  """The links list: a health column for links and sessions (as for the SSH login), a drive without its path but one
  row per folder its clients mounted, with their reconnects"""
  await until(pilot, lambda: app.cluster.loader is None, 30)
  mgr = app.link_manager
  now, mono = time.time(), time.monotonic()
  drive = Link("drive", f"{app.cluster.name} /home/u", app.cluster.name, lambda: [], relay="ssh-j.com", uses=None, path="/home/u")
  drive.relay_state = "ready"
  def sess(sid, path, started, ended=None, latency=0.03, ping=None):
    x = Session(drive, sid, kind="drive", client="SSH-2.0-OpenSSH_for_Windows_9.5", path=path, started=started, ended=ended, latency=latency)
    x.ping_sent = ping
    return x
  drive.sessions += [sess("a1", "/scratch/u", now - 400, now - 300), sess("a2", "/scratch/u", now - 290, now - 100),
                     sess("a3", "/scratch/u", now - 90), sess("b1", "/home/u", now - 1000, latency=2.0)]
  shell = Link("shell", "a shell", app.cluster.name, lambda: [], relay="ssh-j.com")
  shell.relay_state, shell.relay_error = "broken", "connection reset"
  shaky = Link("shell", "another shell", app.cluster.name, lambda: [], relay="ssh-j.com")
  shaky.relay_state, shaky.relay_losses = "ready", [mono - 30, mono - 200]
  silent = Session(shaky, "c1", kind="shell", client="SSH-2.0-PuTTY", latency=0.05)
  silent.ping_sent = mono - 12
  shaky.sessions.append(silent)
  done = Link("shell", "a closed shell", app.cluster.name, lambda: [], relay="ssh-j.com")
  done.relay_state, done.revoked = "closed", True
  for l in (drive, shell, shaky, done):
    mgr.links.append(l)
  try:
    for _ in range(3):
      await pilot.click("#connstatus")
      if await until(pilot, lambda: isinstance(app.screen, ConnectionScreen), 3):
        break
    cs = app.screen
    table = cs.query_one("#conn-links")
    await until(pilot, lambda: f"link|{drive.id}" in [k.value for k in table.rows], 5)
    keys = [k.value for k in table.rows]
    row = lambda key: [str(c) for c in table.get_row(key)]
    check("a Health column", [c.label.plain for c in table.ordered_columns][:3] == ["Link", "Health", "What"],
          [c.label.plain for c in table.ordered_columns])
    main = row(f"link|{drive.id}")
    check("the drive's own row: healthy, no path", "healthy" in main[1] and main[2] == f"drive: {app.cluster.name}" and "/home/u" not in main[2], main)
    mine = [k for k in keys if k.startswith(f"session|{drive.id}|")]
    check("one row per folder (its newest session), not per reconnect", mine == [f"session|{drive.id}|a3", f"session|{drive.id}|b1"], mine)
    a, b = row(mine[0]), row(mine[1])
    check("the folder in each row, with its reconnects", a[2].startswith("/scratch/u · open") and "reconnected 2×" in a[2]
          and b[2].startswith("/home/u · open") and "reconnected" not in b[2], (a[2], b[2]))
    check("reconnects lately: unstable (degraded)", "unstable: reconnected 2× in 10 min" in a[1], a[1])
    check("a slow heartbeat: slow", "slow 2000 ms" in b[1], b[1])
    check("a lost relay: relay lost", "relay lost" in row(f"link|{shell.id}")[1], row(f"link|{shell.id}")[1])
    check("a relay that reconnected lately: unstable", "unstable: relay reconnected 2×" in row(f"link|{shaky.id}")[1], row(f"link|{shaky.id}")[1])
    columns = [c.label.plain for c in table.ordered_columns]
    check("Started and Valid columns: when each link was made, how long it still takes logins", columns[3:5] == ["Started", "Valid"]
          and " ago)" in row(f"link|{drive.id}")[3] and row(f"link|{done.id}")[4].startswith("no more logins"), (columns, row(f"link|{drive.id}")))
    check("a closed link: closed, and the way it went (as ready, via ssh-j.com)", row(f"link|{done.id}")[5] == "closed, was via ssh-j.com"
          and "closed" in row(f"link|{done.id}")[1], row(f"link|{done.id}"))
    check("a session without an answer for 12 s: no answer", re.search(r"no answer for 1[2-9] s", row(f"session|{shaky.id}|c1")[1]), row(f"session|{shaky.id}|c1")[1])
    styles = {key: table.get_row(key)[1].style for key in (f"link|{drive.id}", mine[0], f"link|{shell.id}")}
    check("the colours of the SSH login: green, yellow, red", [str(v) for v in styles.values()] == [HEALTH_STYLES[x] for x in ("good", "warn", "bad")], styles)
    app.save_screenshot(filename="links_health.svg", path=OUT)
    await pilot.press("escape"); await pilot.pause(0.3)
  finally:
    for l in (drive, shell, shaky, done):
      mgr.links.remove(l)

async def t_categories(app, pilot):
  """Categories: mark jobs (m), put them in a new category (c), the Category column, cat: in the filter, the job
  window; the Categories window: the tree, the numbers (node and GPU hours), its jobs, rename, take out, delete; the
  file on the cluster, per Slurm cluster; a broken file is not overwritten"""
  import json
  await until(pilot, lambda: app.cluster.loader is None and app.cluster.categories is not None, 30)
  app.show_view(None); await pilot.pause(0.3)
  cluster = app.cluster
  table = app.main.query_one("#jobs")
  cols = lambda: [c.key.value for c in table.ordered_columns]
  check("no categories yet: no Category column; the Slurm cluster's name is known", "category" not in cols() and cluster.slurm_cluster == "lab", (cols(), cluster.slurm_cluster))
  table.focus(); table.move_cursor(row=table.get_row_index("9001")); await pilot.pause(0.1)
  await pilot.press("m", "m"); await pilot.pause(0.3)
  first = str(table.get_row("9001")[0])
  check("m marks the row and moves on (down: the list is newest first): two marked, shown with ●, said in the status line", cluster.marked == {"9001", "9000"} and first.startswith("● ")
        and "2 marked" in cluster.summary, (cluster.marked, first, cluster.summary[-80:]))
  await pilot.press("c"); await pilot.pause(0.5)
  cz = app.screen
  check("c: the dialog, for the 2 marked jobs", isinstance(cz, CategorizeScreen) and "2 jobs: 9000, 9001" in str(cz.query_one("#cz-title").render()))
  cz.query_one("#cz-path").value = " proj X / production "
  await pilot.press("enter")
  ok = await until(pilot, lambda: cluster.categories.category_of("9001") == "proj X/production", 10)
  check("Enter: they are in the new category; the marks are gone", ok and cluster.categories.category_of("9000") == "proj X/production" and not cluster.marked)
  data = json.load(open(CATEGORIES_FILE)) if os.path.exists(CATEGORIES_FILE) else {}
  check("kept on the cluster, under its Slurm name, with the job's name", data.get("clusters", {}).get("lab", {}).get("jobs", {}).get("9001", {}).get("name") == "run01"
        and "proj X" in data["clusters"]["lab"]["categories"], data)
  await pilot.pause(0.3)
  check("the Category column shows it", "category" in cols() and "proj X/production" in str(table.get_row("9001")[cols().index("category")]), cols())
  table.move_cursor(row=table.get_row_index("9003")); await pilot.press("c"); await pilot.pause(0.5)
  app.screen.query_one("#cz-path").value = "proj X/tests"; await pilot.click("#cz-ok")
  ok = await until(pilot, lambda: cluster.categories.category_of("9003") == "proj X/tests", 10)
  check("c without marks: the job under the cursor", ok)
  await pilot.press("slash"); await pilot.pause(0.2)
  app.main.query_one("#jfilter").value = "cat:proj X"; await pilot.pause(0.5)
  rows = [str(k.value) for k in table.rows]
  check("the filter cat:proj X: the category with the ones in it", sorted(rows) == ["9000", "9001", "9003"], rows)
  app.main.query_one("#jfilter").value = "cat:proj X/tests"; await pilot.pause(0.5)
  check("cat:proj X/tests: only that one", [str(k.value) for k in table.rows] == ["9003"])
  app.main.query_one("#jfilter").value = "tests"; await pilot.pause(0.5)
  check("plain text finds the category too", "9003" in [str(k.value) for k in table.rows])
  await pilot.press("escape"); await pilot.pause(0.3)
  # the job window
  table.focus(); table.move_cursor(row=table.get_row_index("9001")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen), 10); await pilot.pause(0.5)
  info = rendered(app.screen.query_one("#info").content) if ok and app.screen.query("#info") else ""
  check("the job window shows its category", "proj X/production" in info, info[:300])
  # notes of the job, in Markdown
  js = app.screen
  check("no notes: no box", js.query_one("#job-notes-box").has_class("-empty"))
  await pilot.press("o"); await pilot.pause(0.4)
  ok = type(app.screen).__name__ == "NotesScreen"
  if ok:
    app.screen.query_one("#nt-text").text = "## Run 1\n\nthe **baseline**\n"
    await pilot.press("ctrl+s")
  ok = ok and await until(pilot, lambda: cluster.categories.job_notes_of("9001").startswith("## Run 1"), 10); await pilot.pause(0.5)
  shown = " ".join(str(b.render()) for b in js.query_one("#job-notes").query("MarkdownBlock"))
  check("Notes… (o) in the job window: kept on the cluster, rendered", ok and not js.query_one("#job-notes-box").has_class("-empty")
        and "Run 1" in shown and "**" not in shown
        and json.load(open(CATEGORIES_FILE))["clusters"]["lab"]["job_notes"]["9001"].startswith("## Run 1"), (ok, shown[:200]))
  # paths in the notes that exist (and web addresses): links that open
  wd = cluster.jobs["9001"].rec["WorkDir"]
  await pilot.press("o"); await pilot.pause(0.4)
  if type(app.screen).__name__ == "NotesScreen":
    app.screen.query_one("#nt-text").text = f"the log: {wd}/slurm-9001.out:3, not /no/such/file, see https://slurm.schedmd.com\n"
    await pilot.press("ctrl+s")
  md = js.query_one("#job-notes")
  ok = await until(pilot, lambda: f"](<{wd}/slurm-9001.out:3>)" in getattr(md, "notes_markdown", ""), 15)
  check("an existing path in the notes: a link; one that does not exist: not; a web address: a link", ok
        and "/no/such/file](" not in md.notes_markdown and "](<https://slurm.schedmd.com>)" in md.notes_markdown, getattr(md, "notes_markdown", ""))
  views = len(cluster.views)
  md.post_message(Markdown.LinkClicked(md, f"{wd}/slurm-9001.out:3"))
  ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and (app.screen.path or "").endswith("slurm-9001.out"), 10)
  check("a click on it: the file opens, in a subtab", ok and len(cluster.views) == views + 1, type(app.screen).__name__)
  for v in list(cluster.views)[views:]: app.close_view(v)
  await pilot.pause(0.3)
  # a click on it: the categories window there, in a subtab of its own
  grid = app.screen.query_one("#info").content if ok else None
  cell = next((c for c in getattr(grid, "columns", [None, None])[1]._cells if "proj X/production" in str(c)), None) if grid else None
  clicks = [s.style.meta.get("@click") for s in (cell.spans if cell is not None else []) if s.style and not isinstance(s.style, str)]
  check("the category is a link", "screen.open_category" in clicks, clicks)
  views = len(cluster.views)
  await app.screen.run_action("open_category"); await pilot.pause(0.6)
  check("a click on it: the categories window at it, in a new subtab", isinstance(app.screen, CategoriesScreen)
        and app.screen.path == "proj X/production" and len(cluster.views) == views + 1
        and cluster.views[-1].key == "category proj X/production", (type(app.screen).__name__, getattr(app.screen, "path", None)))
  app.close_view(cluster.views[-1])
  app.show_view(None)
  await pilot.pause(0.4)
  # the window
  await menu(app, pilot, "categories")
  ok = await until(pilot, lambda: isinstance(app.screen, CategoriesScreen), 10)
  cs = app.screen
  tree = cs.query_one("#cat-tree")
  labels = lambda: [str(n.label) for n in tree.root.children] + [str(n.label) for c in tree.root.children for n in c.children]
  check("Categories (the toolbar button): the tree with counts", ok and labels()[:1] + labels()[2:] == ["proj X  3", "production  2", "tests  1"] and labels()[1].startswith("not categorized  "), labels() if ok else None)
  ok = await until(pilot, lambda: not cs.fetching and "Node hours" in rendered(cs.query_one("#cat-info").render()), 20)
  info = rendered(cs.query_one("#cat-info").render())
  check("all categories: 3 jobs, node hours (2 nodes × 30 min each), GPU hours (8 GPUs)", ok and "3 jobs put in it" in info and "Node hours 3.0" in info
        and "GPU hours 12.0" in info and "running" in info, info)
  node = next(n for c in tree.root.children for n in c.children if n.data == "proj X/tests")
  tree.move_cursor(node); await pilot.pause(0.6)
  jt = cs.query_one("#cat-jobs")
  check("a category selected: its jobs only", [str(k.value) for k in jt.rows] == ["9003"] and "proj X/tests" in rendered(cs.query_one("#cat-info").render()))
  app.save_screenshot(filename="categories.svg", path=OUT)
  # a job of it: its window in a subtab of its own; the categories stay in theirs
  views = len(cluster.views)
  jt.focus(); jt.move_cursor(row=0); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.job.jid == "9003", 10)
  check("a job in the categories window: its window in a new subtab", ok and len(cluster.views) == views + 1
        and cluster.views[-1].key == "job 9003", [v.key for v in cluster.views])
  app.close_view(cluster.views[-1]); app.show_view(app.find_view("categories")); await pilot.pause(0.4)
  check("closed: back to the categories", app.screen is cs, type(app.screen).__name__)
  check("and still at the category chosen", cs.path == "proj X/tests" and tree.cursor_node.data == "proj X/tests", cs.path)
  tree.focus()
  await pilot.press("e"); await pilot.pause(0.4)
  app.screen.query_one("#nm-path").value = "proj X/checks"; await pilot.press("enter")
  ok = await until(pilot, lambda: cluster.categories.category_of("9003") == "proj X/checks", 10); await pilot.pause(0.4)
  check("rename (e): the category and its job, the tree follows", ok and "proj X/tests" not in cluster.categories.paths and cs.path == "proj X/checks", cs.path)
  # notes, in Markdown
  box = cs.query_one("#cat-notes-box")
  check("no notes: no box", not box.display or box.has_class("-empty"))
  await pilot.press("o"); await pilot.pause(0.4)
  ok = type(app.screen).__name__ == "NotesScreen"
  if ok:
    app.screen.query_one("#nt-text").text = "# Checks\n\nruns with **-O0**\n\n- one\n- two\n"
    await pilot.press("ctrl+s")
  ok = ok and await until(pilot, lambda: cluster.categories.notes_of("proj X/checks").startswith("# Checks"), 10); await pilot.pause(0.5)
  md = cs.query_one("#cat-notes")
  shown = " ".join(str(b.render()) if hasattr(b, "render") else "" for b in md.query("MarkdownBlock"))
  check("Notes… (o): kept with the categories (the file), rendered", ok and not box.has_class("-empty") and "Checks" in shown
        and "**" not in shown and json.load(open(CATEGORIES_FILE))["clusters"]["lab"]["notes"]["proj X/checks"].startswith("# Checks"),
        (ok, shown[:200]))
  tree.move_cursor(tree.root); await pilot.pause(0.4)
  check("another category: not its notes", box.has_class("-empty"))
  tree.move_cursor(find_node(tree, "proj X/checks")); await pilot.pause(0.4)
  jt.focus(); await pilot.press("x")
  ok = await until(pilot, lambda: cluster.categories.category_of("9003") == "", 10)
  check("take a job out (x)", ok)
  # not categorized: the jobs of the job list in no category
  loose_node = lambda: next((n for n in tree.root.children if n.data == NONE), None)
  n_loose = len({j.jid.partition("_")[0] for j in cluster.jobs.values() if not cluster.categories.category_of(j.jid)})
  check("the tree has 'not categorized' with the jobs of the job list in none", loose_node() is not None
        and str(loose_node().label) == f"not categorized  {n_loose}", (str(loose_node().label) if loose_node() else None, n_loose))
  tree.move_cursor(loose_node()); await pilot.pause(0.5)
  rows = [str(k.value) for k in jt.rows]
  info = rendered(cs.query_one("#cat-info").render())
  check("selected: those jobs (9003 again, not 9000/9001), and what they are", "9003" in rows and "9001" not in rows and "9000" not in rows
        and len(rows) == n_loose and "of the job list in no category" in info, (len(rows), info[:200]))
  jt.focus(); jt.move_cursor(row=jt.get_row_index("9003")); await pilot.press("c"); await pilot.pause(0.5)
  app.screen.query_one("#cz-path").value = "proj X/checks"; await pilot.press("enter")
  ok = await until(pilot, lambda: cluster.categories.category_of("9003") == "proj X/checks", 10); await pilot.pause(0.4)
  check("c there: the job into a category; it leaves 'not categorized'", ok and "9003" not in [str(k.value) for k in jt.rows]
        and str(loose_node().label) == f"not categorized  {n_loose - 1}", (str(loose_node().label), cs.path == NONE, "9003" in [str(k.value) for k in jt.rows], type(app.screen).__name__, app.focused))
  await pilot.press("l"); await pilot.pause(0.8)
  shown = [str(k.value) for k in table.rows] if app.screen is app.main else []
  check("In the job list (l): cat:- lists the jobs in no category", app.main.query_one("#jfilter").value == "cat:-" and shown
        and not {"9000", "9001", "9003"} & set(shown), (app.main.query_one("#jfilter").value, shown[:5], type(app.screen).__name__, app.focused))
  await pilot.press("escape"); await pilot.pause(0.3)
  await menu(app, pilot, "categories"); await until(pilot, lambda: isinstance(app.screen, CategoriesScreen), 10); await pilot.pause(0.3)
  cs = app.screen; tree = cs.query_one("#cat-tree"); jt = cs.query_one("#cat-jobs")
  node = next(n for n in tree.root.children if n.data == "proj X")
  tree.move_cursor(node); await pilot.pause(0.4)
  await pilot.press("delete"); await pilot.pause(0.4)
  text = str(app.screen.dialog_message) if isinstance(app.screen, ConfirmScreen) else ""
  check("delete asks, saying what goes", "categories in it go too" in text and "no category then" in text, text)
  await pilot.click("#confirm-yes")
  ok = await until(pilot, lambda: not cluster.categories.paths, 10)
  check("delete: no categories, no jobs in them", ok and not cluster.categories.jobs)
  await pilot.press("escape"); await pilot.pause(0.4); app.show_view(None); await pilot.pause(0.5)
  check("no categories: the column goes again", "category" not in cols(), cols())
  # a broken file: read with a warning, never overwritten
  open(CATEGORIES_FILE, "w").write("{ broken")
  await pilot.press("r"); await until(pilot, lambda: cluster.loader is None, 30); await pilot.pause(0.5)
  check("a broken file: a warning", any("categories cannot be read" in m for _, m in notes(app)), notes(app)[-3:])
  table.focus(); table.move_cursor(row=table.get_row_index("9001")); await pilot.press("c"); await pilot.pause(0.5)
  app.screen.query_one("#cz-path").value = "x"; await pilot.press("enter"); await pilot.pause(2)
  check("and it is not overwritten", open(CATEGORIES_FILE).read() == "{ broken" and any("not valid JSON" in m for _, m in notes(app)), notes(app)[-2:])
  os.unlink(CATEGORIES_FILE)
  await pilot.press("r"); await until(pilot, lambda: cluster.loader is None, 30)
  cluster.marked.clear()

async def t_status_page(app, pilot):
  """A failing login to a cluster whose site has a status page (rule pack: status): the footer says to check it, a
  click on it opens it (and a click beside it the connection window); the connection window says so with a link"""
  import types
  from textual.screen import ModalScreen
  fake = types.SimpleNamespace(state="broken", ready=False, wanted=True, error="Connection timed out", exit_code=255,
                               identity="daint", latency=None, responding=lambda: None)
  tab = types.SimpleNamespace(name="daint", destination="daint-alps", connection=fake, attempts=2, broken_at=time.monotonic(),
                              login_terminal=None)
  text = app.connection_text(tab)
  check("the footer of a failing login to daint: check status.cscs.ch", "connection lost (Connection timed out)" in text.plain
        and text.plain.endswith("· check status.cscs.ch"), text.plain)
  jureca = types.SimpleNamespace(**dict(vars(tab), name="jureca", destination="gruber3@jureca.fz-juelich.de"))
  check("JSC (jureca): check status.jsc.fz-juelich.de", app.connection_text(jureca).plain.endswith("· check status.jsc.fz-juelich.de"), app.connection_text(jureca).plain)
  other = types.SimpleNamespace(**dict(vars(tab), name="lab", destination="lab"))
  check("a site without a status page: nothing added", "check" not in app.connection_text(other).plain, app.connection_text(other).plain)
  fake.state, fake.ready = "ready", True
  check("a working login: nothing added", "check" not in app.connection_text(tab).plain)
  fake.state, fake.ready = "broken", False
  opened, windows = [], []
  saved = app.open_page, app.action_connection
  app.open_page, app.action_connection = opened.append, (lambda: windows.append(1))
  from textual.containers import Vertical
  class Footer(ModalScreen):
    def compose(self):
      with Vertical(): # (a dialog gets its ? button in its first container)
        yield ConnectionStatus(text, id="fake-status")
  try:
    app.push_screen(Footer()); await pilot.pause(0.3)
    x = text.plain.index("status.cscs.ch")
    await pilot.click("#fake-status", offset=(x + 2, 0)); await pilot.pause(0.2)
    await pilot.click("#fake-status", offset=(2, 0)); await pilot.pause(0.2)
    check("a click on it opens the status page; beside it: the connection window", opened == ["https://status.cscs.ch"] and windows == [1], (opened, windows))
    await pilot.press("escape"); app.pop_screen() if isinstance(app.screen, Footer) else None; await pilot.pause(0.2)
  finally:
    app.open_page, app.action_connection = saved
  cs = ConnectionScreen(tab)
  site = cs.site_status().plain
  check("the connection window: the cluster may be down, its status page as a link", "may be down or in maintenance" in site
        and "https://status.cscs.ch" in site and any(getattr(sp.style, "meta", {}).get("open_url") == "https://status.cscs.ch" for sp in cs.site_status().spans), site)
  cs2 = ConnectionScreen(other)
  check("…and without a known page: still the advice", "may be down" in cs2.site_status().plain and "http" not in cs2.site_status().plain)

async def t_offline(app, pilot):
  """The job list is kept: a tab whose cluster cannot be reached shows the last list at once, marked as possibly
  outdated; categorizing there is kept here and written to the cluster with the next load"""
  import json
  import types
  from slurp.cluster import jobcache
  await until(pilot, lambda: app.cluster.loader is None, 30)
  lab = app.cluster
  kept = jobcache.path_of(lab)
  ok = await until(pilot, lambda: os.path.exists(kept), 10)
  check("after a load the list is kept here (the cache, not the user's)", ok and kept.startswith(HERE) and len(json.load(open(kept))["jobs"]) == len(lab.jobs),
        kept)
  shutil.copy(kept, jobcache.path_of(types.SimpleNamespace(destination="labdown")))
  before = open(CATEGORIES_FILE).read() if os.path.exists(CATEGORIES_FILE) else None
  app.add_cluster("labdown"); await pilot.pause(1.0)
  down = app.cluster
  table = app.main.query_one("#jobs")
  check("a tab whose cluster cannot be reached: the kept list at once", down.destination == "labdown" and down.from_cache
        and len(down.jobs) == len(lab.jobs) and table.row_count > 0, (down.destination, down.from_cache, len(down.jobs), table.row_count))
  ok = await until(pilot, lambda: "may be outdated" in str(app.main.query_one("#status").render()), 10)
  status = str(app.main.query_one("#status").render())
  check("the status line: the list from when, why, it may be outdated", ok and status.startswith("⚠ the list from ") and "may be outdated" in status, status[:160])
  app.save_screenshot(filename="offline.svg", path=OUT)
  table.focus(); table.move_cursor(row=table.get_row_index("9001")); await pilot.press("c"); await pilot.pause(0.5)
  cz = app.screen
  if isinstance(cz, CategorizeScreen):
    cz.query_one("#cz-path").value = "offline/test"; await pilot.press("enter"); await pilot.pause(0.5)
  check("categorize while it cannot be reached: done here, the change kept for later", down.categories is not None
        and down.categories.category_of("9001") == "offline/test" and down.pending_categories == [["assign", ["9001"], "offline/test", {"9001": "run01"}]]
        and any("kept here" in m for _, m in notes(app)), (down.pending_categories, notes(app)[-1:]))
  after = open(CATEGORIES_FILE).read() if os.path.exists(CATEGORIES_FILE) else None
  check("…and the cluster's file is not touched (nor this machine's in its place)", after == before
        and not os.path.exists(os.path.join(os.environ["XDG_CONFIG_HOME"], "slurp", "categories.json")))
  await until(pilot, lambda: json.load(open(jobcache.path_of(down))).get("pending"), 5)
  check("the kept change is in the kept list (a restart keeps it)", json.load(open(jobcache.path_of(down)))["pending"] == down.pending_categories)
  table.focus(); await pilot.press("K"); ok = await until(pilot, lambda: isinstance(app.screen, CategoriesScreen), 10); await pilot.pause(0.6)
  info = rendered(app.screen.query_one("#cat-info").render()) if ok else ""
  check("the Categories window (K; the login pane narrows the toolbar): says the cluster cannot be reached, numbers from the kept jobs", "cannot be reached" in info and "Node hours" in info, (info[:300], type(app.screen).__name__, notes(app)[-2:]))
  await pilot.press("escape"); await pilot.pause(0.3); app.show_view(None); await pilot.pause(0.3)
  pending = list(down.pending_categories)
  app.confirm_close_cluster(app.clusters.index(down)); await pilot.pause(0.4); await pilot.click("#confirm-yes"); await pilot.pause(0.8)
  check("the tab closed, back on lab", app.cluster is lab and down not in app.clusters)
  # the next load of a cluster that is back writes the kept changes (here: given to the lab tab, the same cluster)
  lab.pending_categories = pending
  await pilot.press("r"); ok = await until(pilot, lambda: lab.loader is None and not lab.pending_categories, 30); await pilot.pause(0.3)
  data = json.load(open(CATEGORIES_FILE)) if os.path.exists(CATEGORIES_FILE) else {}
  check("the next load: the kept change on the cluster, said so", ok and data.get("clusters", {}).get("lab", {}).get("jobs", {}).get("9001", {}).get("category") == "offline/test"
        and any("written to it now" in m for _, m in notes(app)), (data, notes(app)[-1:]))
  os.unlink(jobcache.path_of(types.SimpleNamespace(destination="labdown")))
  if os.path.exists(CATEGORIES_FILE):
    os.unlink(CATEGORIES_FILE)
  await pilot.press("r"); await until(pilot, lambda: lab.loader is None, 30)

async def t_links_scroll(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  mgr = app.link_manager
  long = "drive: lab " + "/very/long/path" * 12
  links = []
  for i in range(2):
    l = Link("drive", long + str(i), app.cluster.name, lambda: [], relay="ssh-j.com", uses=None, path="/x")
    l.relay_state = "ready"
    l.sessions.append(Session(l, f"s{i}", kind="drive", client="SSH-2.0-OpenSSH_9.6 " + "x" * 80, term="", bytes_in=1000))
    links.append(l); mgr.links.append(l)
  try:
    await pilot.click("#connstatus"); await pilot.pause(0.8)
    cs = app.screen
    table = cs.query_one("#conn-links")
    mine = lambda: [k.value for k in table.rows if any(l.id in k.value for l in links)]
    ok = await until(pilot, lambda: len(mine()) == 4, 5)
    check("the links table is wider than the window (scrolls sideways)", ok and table.max_scroll_x > 0, (len(mine()), table.max_scroll_x))
    table.scroll_to(x=table.max_scroll_x, animate=False); await pilot.pause(0.3)
    x = table.scroll_x
    for n in range(3): # traffic changes, refreshed every second
      links[0].sessions[0].bytes_in += 50000 * (n + 1); cs.show_links(); await pilot.pause(0.4)
    check("updated values keep the table scrolled where it was", x > 0 and table.scroll_x == x and "51.0 kB" not in str(table.get_row(f"session|{links[0].id}|s0")),
          (x, table.scroll_x))
    links[1].sessions.append(Session(links[1], "s9", kind="drive", client="new")); cs.show_links(); await pilot.pause(0.5)
    check("a new row keeps the table scrolled where it was too", len(mine()) == 5 and table.scroll_x == x, (len(mine()), table.scroll_x, x))
    await pilot.press("escape"); await pilot.pause(0.3)
  finally:
    for l in links:
      mgr.links.remove(l)

async def t_script_edit(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  path = os.path.join(LAB, "jobs", "9003", "edit_me.sh")
  open(path, "w").write("#!/bin/bash -l\n#SBATCH --nodes=1\n#SBATCH --time=01:00:00\nset -eo pipefail\nsrun /bin/true\n")
  open_path(app, ["Jobs"], path)
  ok = await until(pilot, lambda: isinstance(app.screen, ScriptScreen) and app.screen.ctx is not None, 20)
  sc = app.screen; area = sc.query_one("#script-text")
  await pilot.click("#sc-edit"); await pilot.pause(0.3)
  check("Edit makes the script editable in place, with the focus", ok and sc.editing and not area.read_only and app.focused is area
        and sc.query_one("#sc-save").display and not sc.query_one("#sc-edit").display)
  area.cursor_location = (2, 0)
  await pilot.press(*"#SBATCH --mem=8", "enter"); await pilot.pause(sc.LIVE_DELAY + 0.6)
  ids = [f.rule.id for f in sc.findings]
  status = str(sc.query_one("#script-status").render())
  check("typing is checked as you type (R01 for --mem=8), the status says modified", "R01" in ids and "modified" in status, (ids, status))
  check("the file is not written yet", "--mem=8" not in open(path).read())
  area.cursor_location = (2, 0)
  await pilot.press(*"#SBATCH --chdir=/nonexistent/run1", "enter")
  ok = await until(pilot, lambda: any(f.rule.id == "P02" and f.severity == "error" and "/nonexistent/run1" in f.evidence for f in sc.findings), 10)
  check("while typing, the paths are checked on the cluster too: a --chdir that does not exist is an error (P02)", ok,
        [(f.rule.id, f.severity, f.evidence[:60]) for f in sc.findings][:6])
  for _ in range(len("#SBATCH --chdir=/nonexistent/run1") + 1):
    await pilot.press("backspace")
  ok = await until(pilot, lambda: "--chdir" not in area.text and not any(f.rule.id == "P02" for f in sc.findings), 10)
  check("and the error goes when the line goes", ok)
  area.cursor_location = (2, 0)
  await pilot.press(*"#SBATCH --chdir=/nonexistent/zz", "enter")
  await pilot.click("#sc-check")
  ok = await until(pilot, lambda: sc.ctx is not None and sc.ctx.replay is not None and "/nonexistent/zz" in (sc.ctx.script.text if sc.ctx.script else ""), 20)
  summary = str(sc.query_one("#sc-findings").query_one(".fv-summary").render())
  check("Check while editing checks the edited, unsaved text (with paths and environment), not the file",
        ok and any(f.rule.id == "P02" and "/nonexistent/zz" in f.evidence for f in sc.findings) and "unsaved" in summary
        and "/nonexistent/zz" not in open(path).read() and sc.unsaved(), ([(f.rule.id, f.evidence[:50]) for f in sc.findings][:4], summary[-80:]))
  for _ in range(len("#SBATCH --chdir=/nonexistent/zz") + 1):
    await pilot.press("backspace")
  await until(pilot, lambda: "/nonexistent/zz" not in area.text, 5)
  await pilot.press("ctrl+s")
  ok = await until(pilot, lambda: "--mem=8" in open(path).read() and "modified" not in str(sc.query_one("#script-status").render()), 10)
  check("Ctrl+S saves the file; still editing", ok and sc.editing and open(path).read().splitlines()[2] == "#SBATCH --mem=8", open(path).read()[:120])
  ok = await until(pilot, lambda: sc.ctx is not None and sc.ctx.probes is not None and any(f.rule.id == "R01" for f in sc.findings), 15)
  check("after saving, the full check runs (with the files on the cluster)", ok)
  before = area.text
  await pilot.press("ctrl+z"); await pilot.pause(sc.LIVE_DELAY + 0.5)
  check("Ctrl+Z undoes (the text differs from the file again: modified)", area.text != before
        and "modified" in str(sc.query_one("#script-status").render()), area.text[:80])
  await pilot.press("escape"); ok = await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), 5)
  check("Esc with unsaved changes asks", ok and "Leave without saving" in str(app.screen.query_one("#confirm-message").render()))
  await pilot.click("#confirm-yes"); await pilot.pause(0.5)
  check("Discard: not editing, the text is the file's again", not sc.editing and area.read_only and "--mem=8" in area.text, area.text[:100])
  await pilot.click("#sc-edit"); await pilot.pause(0.3)
  area.cursor_location = (0, 0); await pilot.press(*"# x", "enter"); await pilot.pause(0.2)
  open(path, "a").write("# changed elsewhere\n"); os.utime(path, (time.time() + 5, time.time() + 5))
  await pilot.pause(3.5)
  await pilot.press("ctrl+s"); ok = await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), 8)
  check("Save asks before overwriting a file changed on disk meanwhile", ok and "Overwrite" in str(app.screen.query_one("#confirm-yes").label))
  await pilot.click("#confirm-yes")
  ok = await until(pilot, lambda: open(path).read().startswith("# x"), 8)
  check("Overwrite writes the edited version", ok and "changed elsewhere" not in open(path).read())
  await pilot.press(*"y"); await pilot.pause(0.3)
  view = next(v for v in app.cluster.views if v.mode == app.current_mode)
  app.action_close_view(); ok = await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), 5)
  check("closing the subtab with unsaved changes asks", ok and "unsaved" in str(app.screen.query_one("#confirm-message").render()))
  await pilot.click("#confirm-no"); await pilot.pause(0.3)
  check("No: the subtab stays", view in app.cluster.views and isinstance(app.screen, ScriptScreen))
  check("Reload is there while editing too", sc.query_one("#sc-reload").display)
  await pilot.press("ctrl+r"); ok = await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), 5)
  check("Reload (Ctrl+R) with unsaved changes asks", ok and "Reload" in str(app.screen.query_one("#confirm-yes").label))
  await pilot.click("#confirm-no"); await pilot.pause(0.3)
  edited = area.text
  check("No: the edits stay", sc.unsaved() and edited != open(path).read(), area.text[:20])
  open(path, "a").write("# reloaded\n")
  await pilot.press("ctrl+r"); await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), 5)
  await pilot.click("#confirm-yes")
  ok = await until(pilot, lambda: "# reloaded" in area.text, 8)
  check("Reload: the file in the editor, still editing, nothing unsaved", ok and sc.editing and not area.read_only
        and not sc.unsaved() and area.text != edited and area.text == open(path).read(), area.text[:60])
  await pilot.press("escape"); await pilot.pause(0.3)
  check("Esc without changes just stops editing", not sc.editing)
  open(path, "a").write("# again\n")
  await pilot.click("#sc-reload")
  ok = await until(pilot, lambda: "# again" in area.text, 8)
  check("Reload while not editing: the file again, checked", ok and not sc.editing and area.read_only)
  app.action_close_view(); await pilot.pause(0.5)

async def t_chdir(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  path = os.path.join(LAB, "jobs", "9004", "chdir_job.sh")
  open(path, "w").write("#!/bin/bash -l\n#SBATCH --time=01:00:00\nsrun /bin/true\n")
  open_path(app, ["Jobs"], path)
  ok = await until(pilot, lambda: isinstance(app.screen, ScriptScreen) and app.screen.ctx is not None, 20)
  sc = app.screen
  for value, shown in (("/nonexistent/run1", "/nonexistent/run1"), ("run_missing", os.path.join(os.path.dirname(path), "run_missing"))):
    open(path, "w").write(f"#!/bin/bash -l\n#SBATCH --chdir={value}\n#SBATCH --time=01:00:00\nsrun /bin/true\n")
    await pilot.click("#sc-check")
    ok = await until(pilot, lambda: sc.ctx is not None and any(f.rule.id == "P02" and shown in f.evidence for f in sc.findings), 15)
    p02 = [f for f in sc.findings if f.rule.id == "P02"]
    check(f"Check again: --chdir={value} that does not exist is an error (P02), in the list and the status", ok and p02[0].severity == "error"
          and shown in p02[0].evidence and "✖" in str(sc.query_one("#script-status").render()), [(f.rule.id, f.severity, f.evidence) for f in sc.findings][:5])
  await pilot.press("escape"); await pilot.pause(0.3)

async def t_web_access(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  import slurp.links.holder as hm
  calls = []
  views = [{"label": "the terminal on host (pts/1)", "kind": "terminal"}]
  def fake(cmd, timeout=5, **kw):
    calls.append((cmd, kw))
    if cmd == "web_start": return {"port": 45678, "token": "tk42"}
    if cmd == "status": return {"views": views, "web": 45678}
    return {"ok": True}
  real = (hm.under_holder, hm.control)
  hm.under_holder = lambda: False
  try:
    await pilot.press("W"); await pilot.pause(0.5)
    check("without the session holder, Web says why", any("session holder" in m for _, m in notes(app)), notes(app)[-1:])
    hm.under_holder, hm.control = (lambda: True), fake
    await pilot.press("W")
    ok = await until(pilot, lambda: isinstance(app.screen, LinkSetupScreen), 10)
    scr = app.screen
    check("Web: the web view starts, then the link setup for remote web access", ok and scr.kind == "web" and ("web_start", {}) in calls
          and "Remote web access" in str(scr.query_one("#setup-title").render()) and scr.query_one("#setup-uses").value == 0)
    await pilot.pause(3) # (the dialog stays open a while: its web view must not stop meanwhile)
    check("while the Web dialog is open, its web view keeps running", not any(c == "web_stop" for c, _ in calls) and app.web_port == 45678,
          [c for c, _ in calls])
    steps = str(scr.query_one(".setup-row").render())
    check("the Web dialog says how it goes: the command, the password, keep the window open, the URL, Take over",
          "password" in steps and "Keep that window open" in steps and "Take over" in steps, steps[:200])
    scr.query_one("#setup-relay-custom").value = True; scr.query_one("#setup-relay").value = "lab-relay"; await pilot.pause(0.2)
    await pilot.click("#setup-create")
    ok = await until(pilot, lambda: isinstance(app.screen, LinkScreen), 10)
    ls = app.screen; link = ls.link
    ok = ok and await until(pilot, lambda: link.ready(), 20)
    await pilot.pause(1.2)
    texts = [str(w.render()) for w in ls.query(".lv-command-text")]
    check("the link window: the tunnel to the web view's port, then the URL with the token", ok and link.kind == "web" and link.web_port == 45678
          and any("-L 8000:127.0.0.1:45678" in t for t in texts) and any("token=tk42" in t for t in texts), texts[:2])
    await pilot.press("escape"); await pilot.pause(0.3)
    ok = await until(pilot, lambda: any(c == "info" and "web link" in kw.get("text", "") for c, kw in calls), 6)
    check("the other views' lock screens hear about the web link", ok)
    views.append({"label": "Firefox (web)", "kind": "web"})
    await pilot.press("q"); ok = await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), 5)
    msg = str(app.screen.query_one("#confirm-message").render()) if ok else ""
    check("quit asks when other views show this slurp, naming them", "2 views" in msg and "Firefox (web)" in msg, msg)
    await pilot.click("#confirm-no"); await pilot.pause(0.3)
    app.link_manager.revoke(link)
    ok = await until(pilot, lambda: any(c == "web_stop" for c, _ in calls), 10)
    check("revoked: no web link left, the web view stops (the port closes)", ok and app.web_port is None)
  finally:
    hm.under_holder, hm.control = real
    for l in [l for l in app.link_manager.links if l.kind == "web"]: # (the tests after this one count the links)
      app.link_manager.revoke(l); app.link_manager.links.remove(l)
    cfg = load_config(); cfg.pop("link_relay", None)
    update_config(link_relay="ssh-j.com") # (the relay chosen here is a setting: back to the default)

async def t_options(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  before = dict(load_config())
  update_config(drive_mounts={"lab": {"path": "/tmp/x", "read_only": True}}, progress_files={"1": "a"})
  try:
    await pilot.press("f3")
    ok = await until(pilot, lambda: isinstance(app.screen, OptionsScreen), 5)
    op = app.screen
    check("F3 opens Options, with the config's values", ok and op.query_one("#op-relay").value == (before.get("link_relay") or "ssh-j.com")
          and "config.json" in str(op.query_one("#op-path").render()))
    op.query_one("#op-theme").value = "nord"; op.query_one("#op-theme-web").value = "gruvbox"
    op.query_one("#op-scrollback").value = "500"; op.query_one("#op-copy").value = "terminal"
    op.query_one("#op-muted").select("R07")
    check("Progress: the defaults (64 KiB, 600 lines, 6 values), no warning", [op.query_one(f"#op-{k}").value for k in
          ("progress_tail_kib", "progress_tail_lines", "progress_pace_points")] == ["64", "600", "6"] and not op.query_one("#op-progress-warning").display)
    op.query_one("#op-progress_tail_kib").value = "4096"; await pilot.pause(0.2)
    warning = lambda: str(op.query_one("#op-progress-warning").render())
    check("much more: a warning that it gets slower, at once", op.query_one("#op-progress-warning").display and "4096 KiB is a lot" in warning()
          and "slower" in warning(), warning())
    op.query_one("#op-progress_tail_lines").value = "5"; await pilot.pause(0.2)
    check("out of range: said", "20 to 200000 lines" in warning(), warning())
    await pilot.click("#op-save"); await pilot.pause(0.3)
    check("…and not saved", isinstance(app.screen, OptionsScreen))
    op.query_one("#op-progress_tail_lines").value = "2000"; op.query_one("#op-progress_pace_points").value = "10"
    for b in op.query(".op-forget"):
      if b.name in ("drive:lab", "progress_files"):
        b.press()
    await pilot.pause(0.3)
    await pilot.click("#op-save")
    ok = await until(pilot, lambda: not isinstance(app.screen, OptionsScreen), 5)
    cfg = load_config()
    check("Save writes config.json: themes, scrollback, copy, muted rules, the forgotten entries",
          ok and cfg.get("theme") == "nord" and cfg.get("theme_web") == "gruvbox" and cfg.get("terminal_scrollback") == 500
          and cfg.get("copy") == "terminal" and "R07" in cfg.get("checks_muted", []) and cfg.get("drive_mounts") == {}
          and cfg.get("progress_files") == {}, {k: cfg.get(k) for k in ("theme", "theme_web", "terminal_scrollback", "copy", "checks_muted", "drive_mounts")})
    from slurp.cluster.jobs import progress_scan
    check("the progress settings: kept, and used from now on", cfg.get("progress_tail_kib") == 4096 and cfg.get("progress_tail_lines") == 2000
          and progress_scan() == {"bytes": 4096 * 1024, "lines": 2000, "pace": 10}, (progress_scan(), cfg.get("progress_tail_kib")))
    check("the terminal's theme applies at once", app.theme == "nord", app.theme)
    app.active_view_changed("web"); await pilot.pause(0.3)
    check("a browser becomes the active view: its theme", app.theme == "gruvbox", app.theme)
    app.theme = "dracula"; await pilot.pause(0.3)
    check("a theme chosen while the browser is active is the browser's", load_config().get("theme_web") == "dracula"
          and load_config().get("theme") == "nord")
    app.active_view_changed("terminal"); await pilot.pause(0.3)
    check("the terminal active again: the terminal's theme", app.theme == "nord" and load_config().get("theme_web") == "dracula", app.theme)
    await pilot.press("f3"); await until(pilot, lambda: isinstance(app.screen, OptionsScreen), 5)
    op = app.screen
    check("Options shows the saved values again", op.query_one("#op-theme-web").value == "dracula" and op.query_one("#op-scrollback").value == "500"
          and "R07" in op.query_one("#op-muted").selected)
    await pilot.press("escape"); await pilot.pause(0.3)
  finally:
    import json
    cfg = load_config()
    for k in ("theme", "theme_web", "terminal_scrollback", "copy", "checks_muted", "drive_mounts", "progress_files", "link_relay",
              "progress_tail_kib", "progress_tail_lines", "progress_pace_points"):
      if k in before: cfg[k] = before[k]
      else: cfg.pop(k, None)
    open(config_path(), "w").write(json.dumps(cfg))
    app.active_view_changed("terminal")
    if before.get("theme"): app.theme = before["theme"]

async def t_copy_views(app, pilot):
  await until(pilot, lambda: app.cluster.loader is None, 30)
  import json
  import importlib
  dialogs = importlib.import_module("slurp.ui.screens.dialogs")
  before = dict(load_config())
  used = []
  real_tool = dialogs.local_clipboard
  try:
    update_config(copy="terminal"); cfg = load_config(); cfg.pop("copy_web", None); open(config_path(), "w").write(json.dumps(cfg))
    dialogs.local_clipboard = lambda text: used.append(text) or None
    seen = note_ids(app)
    dialogs.copy_text(app, "abc", "the test text"); await pilot.pause(0.3)
    check("terminal view, \"copy\": \"terminal\": copied without asking", not isinstance(app.screen, CopyScreen)
          and any("through the terminal" in m for _, m in new_notes(app, seen)) and used == ["abc"])
    app.active_view_changed("web"); used.clear()
    dialogs.copy_text(app, "def", "the test text")
    ok = await until(pilot, lambda: isinstance(app.screen, CopyScreen), 5)
    check("a browser active: it has its own setting (asks), and this machine's clipboard tool is not used", ok and used == [])
    await pilot.click("#copy-arrived")
    await until(pilot, lambda: load_config().get("copy_web") == "terminal" and not isinstance(app.screen, CopyScreen), 5)
    cfg = load_config()
    check("It arrived, in a browser: stored as copy_web, the terminal's stays", cfg.get("copy_web") == "terminal" and cfg.get("copy") == "terminal", cfg)
    update_config(copy=None)
    seen = note_ids(app)
    dialogs.copy_text(app, "ghi", "the test text")
    await until(pilot, lambda: any("through the terminal" in m for _, m in new_notes(app, seen)), 5)
    check("browser: copied without asking now", not isinstance(app.screen, CopyScreen) and any("through the terminal" in m for _, m in new_notes(app, seen)))
    app.active_view_changed("terminal")
    dialogs.copy_text(app, "jkl", "the test text")
    ok = await until(pilot, lambda: isinstance(app.screen, CopyScreen), 5)
    check("back in the terminal (its setting cleared): it asks again", ok)
    await pilot.click("#copy-close"); await pilot.pause(0.3)
    await pilot.press("f3"); await until(pilot, lambda: isinstance(app.screen, OptionsScreen), 5)
    op = app.screen
    check("Options: one copy setting for each kind of view", op.query_one("#op-copy").value == "" and op.query_one("#op-copy-web").value == "terminal")
    await pilot.press("escape"); await pilot.pause(0.3)
  finally:
    dialogs.local_clipboard = real_tool
    app.active_view_changed("terminal")
    cfg = load_config()
    for k in ("copy", "copy_web"):
      if k in before: cfg[k] = before[k]
      else: cfg.pop(k, None)
    open(config_path(), "w").write(json.dumps(cfg))

async def t_array(app, pilot):
  got = []
  states = {"1": "COMPLETED", "2": "RUNNING", "3": "PENDING", "5": "FAILED"}
  app.push_screen(RunArrayScreen("700", ["sbatch", "--array=3", "job.sh"], states, list(range(0, 10))), got.append)
  await pilot.pause(0.3)
  scr = app.screen
  w = lambda: str(scr.query_one("#array-warning").render())
  ed = scr.query_one("#array-command")
  check("Run array: the original range, a warning for running/queued/completed tasks", "completed (1)" in w() and "running (2)" in w()
        and "pending (3)" in w() and ed.text == "sbatch --array=0-9 job.sh" and ed.language == "bash", (w(), ed.text))
  scr.query_one("#array-start").value = "4"; scr.query_one("#array-step").value = "2"; await pilot.pause(0.2)
  check("4-9 step 2: no overlap, the command follows", "none of them" in w() and ed.text == "sbatch --array=4-9:2 job.sh", (w(), ed.text))
  check("and which ids: 4, 6, 8, the last 8 not 9", "ids 4, 6, 8 (first 4, last 8)" in w() and "the last id is 8, not 9" in w(), w())
  scr.query_one("#array-stop").value = "2"; await pilot.pause(0.2)
  check("stop < start: an error", "must be numbers" in w())
  scr.query_one("#array-stop").value = "9"; await pilot.pause(0.2)
  await pilot.click("#array-submit"); await pilot.pause(0.3)
  check("submit gives the command", got == [["sbatch", "--array=4-9:2", "job.sh"]], got)
  t = app.screen.query_one("#jobs"); t.focus(); await pilot.press("enter")
  await until(pilot, lambda: isinstance(app.screen, JobScreen))
  await pilot.pause(0.5)
  check("no Run array button for a job that is not an array", not app.screen.query_one("#run-array").display)
  await pilot.press("escape"); await pilot.pause(0.3)

async def t_ij_fields(app, pilot):
  calls = os.path.join(LAB, "sbatch_calls")
  if os.path.exists(calls): os.unlink(calls)
  await pilot.press("i")
  scr = app.screen
  ok = await until(pilot, lambda: len(scr.query_one("#ij-partition")._options) > 1, 10)
  ed = scr.query_one("#command-text")
  check("interactive job: fields, partitions from sinfo, accounts listed", ok and [v for _, v in scr.query_one("#ij-account-list")._options][1:] == ["acct1", "proj2"],
        [v for _, v in scr.query_one("#ij-partition")._options])
  scr.query_one("#ij-account-list").value = "proj2"
  scr.query_one("#ij-partition").value = "debug"
  scr.query_one("#ij-time").value = "00:30:00"
  scr.query_one("#ij-gpus").value = "4"
  await pilot.pause(0.3)
  check("the fields write the command", all(x in ed.text for x in ("--account=proj2", "--partition=debug", "--time=00:30:00", "--gpus-per-node=4")), ed.text)
  scr.query_one("#ij-account").value = "myown"; await pilot.pause(0.2)
  check("an own account string", "--account=myown" in ed.text)
  ed.load_text(ed.text + " --mem=10G"); await pilot.pause(0.1)
  await pilot.click("#command-submit")
  ok = await until(pilot, lambda: os.path.exists(calls), 10)
  call = open(calls).read() if ok else ""
  check("the edited command is submitted", "'--account=myown'" in call and "'--mem=10G'" in call and "'--gpus-per-node=4'" in call, call)

async def t_progress(app, pilot):
  JobScreen.PROGRESS_EVERY = 2 # (10 s in use)
  from datetime import datetime, timedelta
  t = app.screen.query_one("#jobs")
  labels = [str(c.label) for c in t.columns.values()]
  check("job list: only the estimate on current pace", "eta_now" in app.job_column_keys and "eta" not in app.job_column_keys
        and "Done: current pace" in labels and "Done: same pace" not in labels, labels)
  t.focus(); t.move_cursor(row=t.get_row_index("9001")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen) and "on same pace (since the start" in str(app.screen.query_one("#progress-line").render()), 10)
  line = lambda: str(app.screen.query_one("#progress-line").render())
  check("job window: too few updates for the current pace: the estimate since the start, the file", ok and "done in" in line()
        and "slurm-9001.out" in line() and "on current pace" not in line(), line())
  titles = [str(l.render()) for l in app.screen.query("Label.section")]
  check("the progress has its own title, before Files, like the other sections", any(t.endswith("Progress") for t in titles)
        and [t.split()[-1] for t in titles].index("Progress") < [t.split()[-1] for t in titles].index("Files")
        and app.screen.query_one("#job-progress Label.section") is not None, titles)
  check("the diagnostics have their own title too, above the query and the plot (a running job)",
        any(t.endswith("Diagnostics") for t in titles) and app.screen.query_one("#diagnostics-title").display, titles)
  app.save_screenshot(filename="job_progress.svg", path=OUT)
  wd = app.jobs["9001"].rec["WorkDir"]
  now = datetime.now().replace(microsecond=0)
  with open(os.path.join(wd, "progress.log"), "w") as f:
    for k, minutes in ((10, 40), (20, 30), (30, 20), (40, 10)):
      f.write(f"{(now - timedelta(minutes=minutes)).isoformat()} INFO iteration {k}/100\n")
  js = app.screen
  await pilot.click("#progress-file")
  ok = await until(pilot, lambda: isinstance(app.screen, FilePickScreen) and app.screen.entries, 10)
  ds = app.screen
  check("Progress file… opens a window with the directory of the output", ok and ds.path == wd and len(app.screen_stack) >= 2
        and app.screen_stack[-2] is js, (ds.path if ok else None, wd))
  dt = ds.query_one("#fp-list"); dt.focus()
  dt.move_cursor(row=dt.get_row_index("slurm-9001.out")); await pilot.pause(0.5)
  pv = lambda: str(ds.query_one("#fp-preview").render())
  ok = await until(pilot, lambda: "progress found" in pv() or "no n/N" in pv(), 10)
  check("the file under the cursor: its end, and whether it has progress", ok and "progress found" in pv() and "step" in pv(), pv())
  ds.query_one("#fp-filter").value = "progress"; await pilot.pause(0.3)
  rows = [str(k.value) for k in dt.rows]
  check("the filter: only the names with it", "progress.log" in rows and "slurm-9001.out" not in rows, rows)
  ds.query_one("#fp-filter").value = "slurm-9*.out"; await pilot.pause(0.3)
  rows = [str(k.value) for k in dt.rows]
  check("a pattern: slurm-9*.out", "slurm-9001.out" in rows and "progress.log" not in rows
        and all(r == ".." or r.startswith("slurm-9") and r.endswith(".out") for r in rows), rows)
  ds.query_one("#fp-filter").value = "progress"; await pilot.pause(0.3)
  dt.focus(); dt.move_cursor(row=dt.get_row_index("progress.log")); await pilot.pause(0.3)
  ok = await until(pilot, lambda: "40/100" in pv(), 10)
  check("progress.log: 40/100 found, with timestamps", ok and "with timestamps" in pv(), pv())
  await pilot.press("enter")
  ok = await until(pilot, lambda: app.screen is js and "40/100" in line(), 10)
  ok2 = await until(pilot, lambda: any(e.kind == "progress" for e in js.details["files"]), 10)
  entry = next((e for e in js.details["files"] if e.kind == "progress"), None)
  check("the chosen progress file is in the file list as progress", ok2 and entry.path.endswith("progress.log") and entry.exists,
        [(e.kind, e.label) for e in js.details["files"]][:5])
  check("the chosen file is used, with its timestamps: rate 1/min, done in ~50 min", ok and "timestamps in the file" in line()
        and "on current pace (last 4 updates): 1/min" in line() and ("~49 min" in line() or "~50 min" in line()) and "chosen for this job" in line(), repr(line()))
  check("…only the current pace then (not the average since the start)", "on same pace" not in line(), repr(line()))
  with open(os.path.join(wd, "progress.log"), "a") as f:
    f.write(f"{datetime.now().replace(microsecond=0).isoformat()} INFO iteration 55/100\n")
  ok3 = await until(pilot, lambda: "55/100" in line(), 3 * JobScreen.PROGRESS_EVERY + 5)
  age = str(js.query_one("#progress-age").render())
  check("the progress is read again by itself; when is shown, counting up", ok3 and f"updated every {JobScreen.PROGRESS_EVERY} s, last" in age
        and "s ago" in age, age)
  await pilot.press("escape"); await pilot.pause(0.3)
  await pilot.press("r"); await until(pilot, lambda: app.cluster.loader is None); await pilot.pause(0.3)
  check("the job list uses the chosen file too", app.jobs["9001"].progress.current == 55, app.jobs["9001"].progress)
  t = app.screen.query_one("#jobs"); keys = [k.value for k in t.columns]; row = t.get_row("9001")
  cur = str(row[keys.index("eta_now")]).strip()
  check("job list: done on current pace", cur not in ("", "-"), cur)
  t = app.screen.query_one("#jobs"); t.focus(); t.move_cursor(row=t.get_row_index("9001")); await pilot.press("enter")
  await until(pilot, lambda: isinstance(app.screen, JobScreen) and "55/100" in line(), 10)
  await pilot.click("#progress-stdout")
  ok = await until(pilot, lambda: "slurm-9001.out" in line(), 10)
  check("Use stdout goes back to the output file", ok, line())
  await pilot.press("escape"); await pilot.pause(0.3)

def vtabs(app):
  """the subtabs shown (the job list's only while it is open), besides the overview (always there, the first)"""
  return [str(b.label) for b in app.screen.query("#view-tabs Button.view-tab") if b.display and b.id != "view-overview"]

async def t_subtabs(app, pilot):
  t = app.screen.query_one("#jobs"); t.focus(); t.move_cursor(row=t.get_row_index("9001")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.details["files"], 10)
  check("a job opens as a subtab", ok and app.current_mode != "_default" and vtabs(app) == ["Jobs", "job 9001"], vtabs(app))
  js = app.screen
  before = js.details
  await pilot.click("#reload-job"); await pilot.pause(0.05)
  button = js.query_one("#reload-job")
  busy = button.disabled and "Reloading" in str(button.label)
  ok = await until(pilot, lambda: js.details is not before and not button.disabled, 15)
  check("Reload: the job and its files from the cluster again; the button says so meanwhile, then is back",
        ok and busy and "Reloading" not in str(button.label) and js.job.jid == "9001", (busy, str(button.label)))
  out = next(e for e in js.details["files"] if e.kind == "stdout"); js.open_entry(out)
  ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and len(vtabs(app)) == 3, 10)
  file_view = app.cluster.views[-1]
  check("a file of the job: a subtab of its own, shown; the job's stays", ok and vtabs(app)[:2] == ["Jobs", "job 9001"]
        and "slurm-9001.out" in vtabs(app)[2] and app.current_mode == file_view.mode, vtabs(app))
  app.show_view(app.cluster.views[0]); await pilot.pause(0.3)
  check("the job's subtab: still the job window", app.screen is js)
  js.open_entry(out); await pilot.pause(0.5)
  check("the file again: its subtab, not another", app.current_mode == file_view.mode and len(app.cluster.views) == 2, vtabs(app))
  app.close_view(file_view); await pilot.pause(0.3)
  app.show_view(app.cluster.views[0]); await pilot.pause(0.3)
  check("its subtab closed: the job's stays", app.screen is js and vtabs(app) == ["Jobs", "job 9001"], vtabs(app))
  await pilot.press("escape"); await pilot.pause(0.3)
  check("Esc at the job window: the job list, the subtab stays", app.screen is app.main and vtabs(app) == ["Jobs", "job 9001"])
  t.focus(); t.move_cursor(row=t.get_row_index("9001")); await pilot.press("enter"); await pilot.pause(0.5)
  check("the same job again: the same subtab", app.screen is js and len(app.cluster.views) == 1)
  await pilot.click("#view-jobs"); await pilot.pause(0.3)
  t.focus(); t.move_cursor(row=t.get_row_index("9002"))
  await pilot.press("o", "e"); await pilot.pause(0.5)
  check("o and e are no shortcuts any more", "o" not in app.active_bindings and "e" not in app.active_bindings and app.screen is app.main
        and len(app.cluster.views) == 1)
  app.open_file(app.jobs["9002"], app.jobs["9002"].stdout_path())
  ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and len(vtabs(app)) == 3, 10)
  check("output file from the job list: a file subtab", ok and len(app.cluster.views) == 2 and "slurm-9002.out" in vtabs(app)[-1], vtabs(app))
  await pilot.press("t"); await pilot.pause(0.3); await pilot.press("enter"); ok = await until(pilot, lambda: "(cluster)" in sterm(app), 15)
  await pilot.click("#view-jobs"); await pilot.pause(0.3)
  await pilot.press("t"); await pilot.pause(0.3); await pilot.press("enter"); ok = ok and await until(pilot, lambda: "(cluster)" in sterm(app), 15)
  shells = [v for v in app.cluster.views if v.kind == "shell"]
  check("two shells, each a subtab", ok and len(shells) == 2 and shells[0].session is not shells[1].session, vtabs(app))
  await pilot.press(*"echo second-$((1+1))", "enter"); await until(pilot, lambda: "second-2" in sterm(app), 5)
  await pilot.click(f"#view-tab-{shells[0].id}"); await pilot.pause(0.5)
  check("clicking a shell subtab shows it, with the focus", app.screen.session is shells[0].session and isinstance(app.focused, TerminalView)
        and "second-2" not in sterm(app))
  await pilot.press(*"echo first-$((0+1))", "enter")
  check("typing goes to that shell", await until(pilot, lambda: "first-1" in sterm(app), 5))
  app.select_cluster(0); await pilot.pause(0.5)
  check("another tab: its job list", app.screen is app.main and vtabs(app) == ["Jobs"], vtabs(app))
  app.select_cluster(1); await pilot.pause(0.5)
  check("back: the subtab shown before", isinstance(app.screen, TerminalScreen) and app.screen.session is shells[0].session)
  proc = shells[0].session.proc
  await pilot.click(f"#view-close-{shells[0].id}"); await pilot.pause(0.8)
  check("✕ closes a shell subtab and ends the shell; the subtab shown before it comes back (the other shell)",
        shells[0] not in app.cluster.views and proc.poll() is not None and isinstance(app.screen, TerminalScreen)
        and app.screen.session is shells[1].session, vtabs(app))
  await pilot.click(f"#view-tab-{shells[1].id}"); await pilot.pause(0.3)
  await pilot.press("ctrl+w"); await pilot.pause(0.5)
  check("Ctrl-W in a shell goes to the shell (no close)", shells[1] in app.cluster.views)
  await pilot.click("#view-jobs"); await pilot.pause(0.3)
  for v in list(app.cluster.views):
    app.close_view(v)
  await pilot.pause(0.5)
  check("all subtabs closed", vtabs(app) == ["Jobs"] and app.screen is app.main)

async def t_array_sort(app, pilot):
  import copy
  t = app.main.query_one("#jobs")
  base = next(iter(app.jobs.values()))
  saved = dict(app.cluster.jobs)
  app.cluster.jobs.clear()
  for jid in ("500_3", "500_1", "400_2", "600"):
    j = copy.deepcopy(base); j.rec["JobID"] = jid; app.cluster.jobs[jid] = j
  app.sort_column, app.sort_reverse = "jobid", True
  app.populate()
  order = lambda: [str(t.get_row_at(i)[0]) for i in range(t.row_count)]
  header = lambda: "".join(sg.text for sg in t.render_line(0))
  click = lambda: app.sort_by(type("E", (), {"column_key": type("K", (), {"value": "jobid"})()})())
  seen = [(order(), header().split("Name")[0].strip())]
  for _ in range(4):
    click(); await pilot.pause(0.2)
    seen.append((order(), header().split("Name")[0].strip()))
  check("Job ID: job id desc, asc, array asc, array desc, then again desc", [o for o, _ in seen] == [
    ["600", "500_3", "500_1", "400_2"], ["400_2", "500_1", "500_3", "600"], ["600", "500_1", "400_2", "500_3"],
    ["500_3", "400_2", "500_1", "600"], ["600", "500_3", "500_1", "400_2"]], seen)
  check("marks: ▼ ▲ for the job id, △ ▽ for the array index", [h for _, h in seen] == ["Job ID ▼", "Job ID ▲", "Job ID △ array",
        "Job ID ▽ array", "Job ID ▼"], [h for _, h in seen])
  app.cluster.jobs.clear(); app.cluster.jobs.update(saved); app.populate()

async def t_conn_health(app, pilot):
  app.show_view(None); await pilot.pause(0.3); app.main.query_one("#jobs").focus()
  await pilot.press("c"); await pilot.pause(0.3)
  check("c no longer opens the connection window (the footer is clicked; c categorizes)", not isinstance(app.screen, ConnectionScreen)
        and isinstance(app.screen, CategorizeScreen), type(app.screen).__name__)
  if isinstance(app.screen, CategorizeScreen):
    await pilot.press("escape"); await pilot.pause(0.3)
  import signal
  app.show_view(None); await pilot.pause(0.3)
  cs = app.main.query_one("#connstatus")
  check("footer: healthy connection is green", cs.has_class("-good") and not cs.has_class("-bad"), str(cs.render()))
  await pilot.click("#connstatus"); await pilot.pause(0.5)
  title = str(app.screen.query_one("#conn-title").render())
  check("connection window: ● healthy in the title, green border", "● healthy" in title and app.screen.query_one("#conn-dialog").has_class("-good"), title)
  await pilot.press("escape"); await pilot.pause(0.3)
  remote.PING_INTERVAL, remote.PING_SLOW = 1, 2
  os.kill(app.ssh.proc.pid, signal.SIGSTOP)
  ok = await until(pilot, lambda: cs.has_class("-bad"), 15)
  check("frozen: footer red with 'no answer'", ok and "no answer" in str(cs.render()), str(cs.render()))
  await pilot.click("#connstatus"); await pilot.pause(0.5)
  title = str(app.screen.query_one("#conn-title").render())
  check("connection window: red with no answer", "no answer" in title and app.screen.query_one("#conn-dialog").has_class("-bad"), title)
  await pilot.press("escape")
  os.kill(app.ssh.proc.pid, signal.SIGCONT)
  remote.PING_INTERVAL, remote.PING_SLOW = 15, 5
  await until(pilot, lambda: cs.has_class("-good"), 15)

async def t_restart_env(app, pilot):
  calls = os.path.join(LAB, "sbatch_calls")
  if os.path.exists(calls): os.unlink(calls)
  wd = app.jobs["9010"].rec["WorkDir"]
  open(os.path.join(wd, "job10.sh"), "w").write("#!/bin/bash\necho changed on disk\n")
  app.show_view(None); await pilot.pause(0.3)
  t = app.main.query_one("#jobs"); t.focus(); t.move_cursor(row=t.get_row_index("9010")); await pilot.press("enter")
  await until(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.details["files"], 10)
  await pilot.click("#restart-job")
  ok = await until(pilot, lambda: isinstance(app.screen, RestartScreen), 10)
  scr = app.screen
  env_cb, script_cb = scr.query_one("#restart-same-env"), scr.query_one("#restart-same-script")
  label = str(env_cb.label)
  check("restart: same environment offered, with uenv and modules, SLURM_* left out", ok and env_cb.value and "uenv /capstor/uenv" in label
        and "3 modules: cuda/12.4" in label and "SLURM_JOB_ID" not in scr.original["env"], label)
  check("restart: same batch script offered", script_cb.value and not script_cb.disabled, str(script_cb.label))
  await pilot.pause(0.5); await pilot.click("#restart-submit")
  ok = await until(pilot, lambda: os.path.exists(calls), 10)
  call = open(calls).read() if ok else ""
  check("submitted with the original environment (env -i) and the original script", "UENV=/capstor/uenv/prgenv-gnu.squashfs" in call
        and "MY=42" in call and "SLURM_JOB_ID=None" in call and "FUNC=True" in call and "SCRIPT='#!/bin/bash" in call and "changed on disk" not in call
        and ".slurp-restart-9010.sh" in call, call)
  for v in list(app.cluster.views):
    app.close_view(v)
  await pilot.pause(0.5)

def rendered(renderable, width=220):
  from rich.console import Console
  c = Console(width=width, record=True, file=open(os.devnull, "w")); c.print(renderable); return c.export_text()

async def t_quota(app, pilot):
  app.show_view(None); await pilot.pause(0.3)
  lab_quota_delay = os.path.join(LAB, "delay"); open(lab_quota_delay, "w").write("0")
  await pilot.press("Q"); await pilot.pause(0.3)
  check("quota opens as a window, no subtab", isinstance(app.screen, QuotaScreen) and "quota" not in [v.title for v in app.cluster.views])
  check("…not covered while it loads", not app.screen.query("LoadingIndicator"))
  ok = await until(pilot, lambda: hasattr(app.screen, "shown"), 20)
  t = rendered(app.screen.shown) if ok else ""
  check("quota: filesystems of $SCRATCH etc. with space and files, limits and soft limits", ok and "/sshlab/scratch" in t
        and "50.0 GiB" in t and "100.0 GiB (soft 80.0 GiB)" in t and "600,000" in t and "1,000,000" in t and "50%" in t and "60%" in t
        and f"user {getpass.getuser()}" in t, t[:1500])
  check("quota: groups without usage left out, the site's quota output shown", "group " not in t and "site quota: see the table" in t)
  await pilot.press("escape"); await pilot.pause(0.3)
  check("Esc closes it: the job list", app.screen is app.main)

async def t_scratch(app, pilot):
  import time as _t
  d = os.path.join(LAB, "scratch")
  import shutil as _sh; _sh.rmtree(d, ignore_errors=True); os.makedirs(d)
  now = _t.time()
  for name, days in (("old1.dat", 90), ("old2.dat", 45), ("new.dat", 2)):
    p = os.path.join(d, name); open(p, "w").write("x" * 1000); os.utime(p, (now - days * 86400, now - days * 86400))
  app.show_view(None); await pilot.pause(0.3)
  await pilot.press("X")
  ok = await until(pilot, lambda: isinstance(app.screen, ScratchScreen) and app.screen.result is not None, 30)
  scr = app.screen
  status = str(scr.query_one("#scratch-status").render())
  paths = [str(scr.query_one("#scratch-files").get_row_at(i)[5]) for i in range(scr.query_one("#scratch-files").row_count)]
  check("scratch: a subtab, $SCRATCH found, files listed oldest access first, 2 older than 30 days",
        ok and scr.query_one("#scratch-dir").value == d and [os.path.basename(p) for p in paths] == ["old1.dat", "old2.dat", "new.dat"]
        and "3 files" in status and "2 not used for more than 30 days" in status, (paths, status))
  scr.query_one("#scratch-files").focus(); await pilot.press("space")
  await pilot.click("#touch-selected"); await pilot.pause(0.3)
  check("touch asks first and warns about site policies", isinstance(app.screen, ConfirmScreen) and "policy" in str(app.screen.dialog_message))
  await pilot.click("#confirm-yes")
  ok = await until(pilot, lambda: os.stat(os.path.join(d, "old1.dat")).st_atime > now - 60, 15)
  check("touch selected: only that file got a new access time, its modification time stays", ok
        and os.stat(os.path.join(d, "old2.dat")).st_atime < now - 40 * 86400 and os.stat(os.path.join(d, "old1.dat")).st_mtime < now - 80 * 86400)
  await until(pilot, lambda: scr.result is not None and os.path.basename(str(scr.query_one("#scratch-files").get_row_at(0)[5])) == "old2.dat", 20)
  await pilot.click("#touch-all"); await pilot.pause(0.3); await pilot.click("#confirm-yes")
  ok = await until(pilot, lambda: os.stat(os.path.join(d, "old2.dat")).st_atime > now - 60, 20)
  check("touch all my files there", ok)
  app.action_close_view(); await pilot.pause(0.3)

def sacct_since():
  """days back of the -S of the last sacct call with a start time"""
  from datetime import datetime as _d
  calls = [c for c in slurm_calls() if c.startswith("sacct") and " -S " in c]
  if not calls: return None
  start = calls[-1].split(" -S ")[1].split()[0]
  return round((_d.now() - _d.fromisoformat(start)).total_seconds() / 86400, 2)

async def t_since(app, pilot):
  app.show_view(None); await pilot.pause(0.3)
  main = app.main
  sel, custom = main.query_one("#since-choice"), main.query_one("#since")
  check("finished since: a dropdown, 1 day chosen, no custom field", sel.value == "1d" and not custom.display)
  open(os.path.join(LAB, "slurm_calls"), "w").close()
  sel.value = "1w"
  await until(pilot, lambda: sacct_since() is not None and app.cluster.loader is None, 15)
  check("1 week: sacct -S a week ago", sacct_since() == 7.0, sacct_since())
  sel.value = "custom"; await pilot.pause(0.3)
  check("custom: a field to type in, with the focus", custom.display and app.focused is custom)
  open(os.path.join(LAB, "slurm_calls"), "w").close()
  custom.value = "3d12h"; await pilot.press("enter")
  await until(pilot, lambda: sacct_since() is not None and app.cluster.loader is None, 15)
  check("custom 3d12h: sacct -S 3.5 days ago", sacct_since() == 3.5, sacct_since())
  custom.value = "nonsense"; await pilot.press("enter"); await pilot.pause(0.3)
  check("an invalid duration is refused", app.since_value == "3d12h" if hasattr(app, "since_value") else app.cluster.since_value == "3d12h")
  app.add_cluster(""); await pilot.pause(0.5)
  check("a new tab has its own choice (1 day)", sel.value == "1d" and not custom.display and app.cluster.since_value == "1d")
  sel.value = "2w"; await pilot.pause(0.5)
  app.select_cluster(1); await pilot.pause(0.5)
  check("back on the first tab: its custom 3d12h", sel.value == "custom" and custom.display and custom.value == "3d12h"
        and app.cluster.since_value == "3d12h", (sel.value, custom.value))
  app.select_cluster(2); await pilot.pause(0.5)
  check("the other tab kept 2 weeks", sel.value == "2w" and not custom.display)
  app.close_cluster(app.clusters[2]); await pilot.pause(0.5)
  sel.value = "1d"; await until(pilot, lambda: app.cluster.loader is None, 15)

async def open_job(app, pilot, jid):
  app.show_view(None); await pilot.pause(0.3)
  t = app.main.query_one("#jobs"); t.focus(); t.move_cursor(row=t.get_row_index(jid)); await pilot.press("enter")
  await until(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.details["files"], 10)
  return app.screen

async def t_log_positions(app, pilot):
  """Where a text file opens depends on where from: a job's logs at their end (followed while it runs), anything from
  the directory browser at its top and not followed"""
  def opened(name):
    return isinstance(app.screen, FileScreen) and (app.screen.path or "").endswith(name) and app.screen.file_size is not None
  def at_end(fs):
    return fs.area.cursor_location[0] >= fs.area.document.line_count - 2
  for jid, running in (("9001", True), ("9008", False)):
    js = await open_job(app, pilot, jid)
    out = next(e for e in js.details["files"] if e.kind == "stdout"); js.open_entry(out)
    ok = await until(pilot, lambda: opened(f"slurm-{jid}.out"), 10); await pilot.pause(0.3)
    fs = app.screen
    check(f"job window, output of a {'running' if running else 'ended'} job: at its end, {'followed' if running else 'not followed'}",
          ok and at_end(fs) and fs.following == running, (fs.area.cursor_location, fs.following))
    for v in list(app.cluster.views): app.close_view(v)
    await pilot.pause(0.3)
  wd = app.jobs["9001"].rec["WorkDir"]
  open_path(app, ["Local"], wd)
  await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.entries, 10)
  ds = app.screen; dt = ds.query_one("#dir"); dt.focus(); dt.move_cursor(row=dt.get_row_index("slurm-9001.out")); await pilot.press("enter")
  ok = await until(pilot, lambda: opened("slurm-9001.out"), 10); await pilot.pause(0.3)
  fs = app.screen
  check("the same log of the running job from the directory browser: at its top, not followed",
        ok and fs.area.cursor_location == (0, 0) and not fs.following, (fs.area.cursor_location, fs.following))
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)
  app.show_view(None); await pilot.pause(0.3)
  job = next(j for j in app.jobs.values() if not j.running and j.stderr_path() and j.err[0] == "file" and j.err[1] > 0)
  app.open_file(job, job.stderr_path())
  ok = await until(pilot, lambda: opened(os.path.basename(job.stderr_path())), 10); await pilot.pause(0.3)
  fs = app.screen
  check("the error file from the job list (Err): at its end; the job ended: not followed", ok and at_end(fs) and not fs.following,
        (fs.area.cursor_location, fs.following))
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)

async def t_large_log(app, pilot):
  """A large log on the cluster, like less: read in parts over SSH, never as a whole; G its end with the line
  numbers counted there (wc -l), : a line, / a match far away (grep there)"""
  home = app.cluster.connection.home
  path = os.path.join(home, "big-run.log")
  n = 50000
  with open(path, "w") as f: # (the lab's cluster is this machine)
    for i in range(1, n + 1):
      f.write(f"line {i:06d} " + ("needle " if i == 31234 else "x" * 7) + "." * 81 + "\n")
  try:
    open_path(app, ["Local"], path)
    ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and app.screen.file_size is not None and not app.screen.reading, 15)
    fs = app.screen; a = fs.area
    def numbers():
      row = a.cursor_location[0]
      if row and not a.document.get_line(row):
        row -= 1
      text = a.document.get_line(row)
      return (a.line_number_start + row if a.show_line_numbers else None), (int(text[5:11]) if text.startswith("line ") else None)
    check("a 5 MB log: its start, one part of it", ok and fs.start_pos == 0 and fs.file_pos < fs.file_size and numbers() == (1, 1), (fs.chunks, numbers()))
    await pilot.press("G")
    ok = await until(pilot, lambda: fs.file_pos == fs.file_size and fs.first_line is not None and not fs.reading, 20); await pilot.pause(0.3)
    a.move_cursor((a.document.line_count - 2, 0))
    check("G: its end, numbered as in the file (counted on the cluster)", ok and numbers() == (n, n), numbers())
    await pilot.press("colon"); await pilot.pause(0.1)
    fs.query_one("#goto").value = "12000"; await pilot.press("enter")
    ok = await until(pilot, lambda: numbers() == (12000, 12000) and not fs.reading, 20)
    check(": 12000: that line", ok, numbers())
    await pilot.press("slash"); fs.query_one("#search").value = "needle"; await pilot.press("enter")
    ok = await until(pilot, lambda: numbers() == (31234, 31234) and a.selected_text == "needle" and not fs.reading, 20)
    check("/ a match beyond the part shown: grep on the cluster, shown, selected, numbered", ok, (numbers(), a.selected_text))
  finally:
    os.unlink(path)
    for v in list(app.cluster.views): app.close_view(v)
    await pilot.pause(0.3)

async def t_cancel_reason(app, pilot):
  """Cancel a job: the dialog asks why (a list, or in your words), proposes the jobs that look like it for a
  duplicate, shows the guide for a dependency; the reason is kept with the job, shown in its window and in the
  categories window"""
  calls = os.path.join(LAB, "scancel_calls")
  if os.path.exists(calls): os.unlink(calls)
  cluster = app.cluster
  await until(pilot, lambda: cluster.loader is None and cluster.categories is not None, 30)
  # (another job like 9001: the same name and directory, submitted again)
  twin = cluster.jobs["9002"]
  saved = dict(twin.rec)
  twin.rec.update(JobName=cluster.jobs["9001"].rec["JobName"], WorkDir=cluster.jobs["9001"].rec["WorkDir"])
  try:
    js = await open_job(app, pilot, "9001")
    js.query_one("#cancel-job").press()
    ok = await until(pilot, lambda: isinstance(app.screen, CancelScreen), 5)
    dlg = app.screen
    check("Cancel job: asks why, with the command", ok and "scancel 9001" in dlg.dialog_message
          and len(dlg.query("RadioButton")) >= 12, dlg.dialog_message if ok else type(app.screen).__name__)
    choose = lambda key: setattr(next(b for b in dlg.query("RadioButton") if b.name == key), "value", True)
    choose("dependency"); await pilot.pause(0.2)
    hint = str(dlg.query_one("#cr-hint").render())
    check("a dependency: says how Slurm does it, a button to the guide", "--dependency=afterok" in hint and dlg.query_one("#cr-guide").display, hint)
    choose("other"); await pilot.pause(0.2)
    await pilot.click("#cr-ok"); await pilot.pause(0.2)
    check("another reason: it must be said", isinstance(app.screen, CancelScreen) and "say the reason" in str(dlg.query_one("#cr-error").render()))
    choose("duplicate"); await pilot.pause(0.2)
    box = dlg.query_one("#cr-related-list")
    options = lambda: [box.get_option_at_index(i).id for i in range(box.option_count)]
    first = str(box.get_option_at_index(0).prompt) if box.option_count else ""
    check("submitted twice: the job that looks the same first (★), then the other jobs of the job list", dlg.query_one("#cr-related").display
          and options()[:1] == ["9002"] and first.startswith("★") and len(options()) > 3 and "9001" not in options(), (options()[:5], first))
    search = dlg.query_one("#cr-related-search")
    search.value = "run05"; await pilot.pause(0.2)
    check("searchable: by name", options() == ["9005"], options())
    search.value = "acct1 fail"; await pilot.pause(0.2)
    failed = [j.jid for j in cluster.jobs.values() if j.state.startswith("FAILED") and "_" not in j.jid and j.jid != "9001"]
    check("…by every word typed (account, state)", sorted(options()) == sorted(failed), (options(), failed))
    search.value = "9002"; await pilot.pause(0.2)
    search.focus(); await pilot.press("enter"); await pilot.pause(0.2)
    check("Enter chooses the first match", dlg.related_jid == "9002" and "chosen: 9002" in str(dlg.query_one("#cr-related-chosen").render()),
          str(dlg.query_one("#cr-related-chosen").render()))
    dlg.query_one("#cr-details").value = "pressed submit twice"
    await pilot.click("#cr-ok")
    ok = await until(pilot, lambda: os.path.exists(calls), 10)
    check("cancelled: scancel 9001", ok and open(calls).read().strip() == "9001")
    ok = await until(pilot, lambda: (cluster.categories.cancel_reason_of("9001") or {}).get("related") == "9002", 10)
    entry = cluster.categories.cancel_reason_of("9001") or {}
    check("the reason kept with the job (on the cluster)", ok and entry.get("reason") == "duplicate" and entry.get("text") == "pressed submit twice"
          and json.load(open(CATEGORIES_FILE))["clusters"]["lab"]["cancelled"]["9001"]["related"] == "9002", entry)
    await until(pilot, lambda: "Cancelled because" in rendered(js.query_one("#info").content), 10)
    info = rendered(js.query_one("#info").content)
    check("the job window: why, the details, the other job", "Cancelled because" in info and "Submitted twice by mistake: pressed submit twice" in info
          and "the same as job 9002" in info, info[:600])
    views = len(cluster.views)
    await app.run_action("open_job('9002')"); await pilot.pause(0.5)
    check("the other job, a link: its window in a subtab", isinstance(app.screen, JobScreen) and app.screen.job.jid == "9002" and len(cluster.views) == views + 1)
    # in the categories window
    from slurp.ui.screens.categories import change_categories
    done = []
    change_categories(app, cluster, ["assign", ["9001"], "reasons test"], lambda st, r: done.append(1))
    await until(pilot, lambda: done, 10)
    await menu(app, pilot, "categories")
    ok = await until(pilot, lambda: isinstance(app.screen, CategoriesScreen), 10); cs = app.screen
    tree = cs.query_one("#cat-tree"); tree.move_cursor(find_node(tree, "reasons test")); await pilot.pause(0.6)
    jt = cs.query_one("#cat-jobs")
    row = [str(c) for c in jt.get_row("9001")] if ok and "9001" in [str(k.value) for k in jt.rows] else []
    check("the categories window: a column with why", any("Submitted twice by mistake" in c for c in row), row)
  finally:
    twin.rec.clear(); twin.rec.update(saved)
    from slurp.ui.screens.categories import change_categories
    for op in (["delete", "reasons test"], ["cancel_reason", "9001", None]): # (one after the other)
      undone = []
      change_categories(app, cluster, op, lambda st, r: undone.append(1))
      await until(pilot, lambda: undone, 10)
    for v in list(app.cluster.views): app.close_view(v)
    await pilot.pause(0.3)

async def t_restore_subtabs(app, pilot):
  """The subtabs kept from the last run of the lab's tab, opened again once it is connected and loaded: a job, an
  array, a file at its end, the categories, the rule catalogue (in their order), then the one shown before"""
  cluster = app.cluster
  for v in list(cluster.views): app.close_view(v)
  await pilot.pause(0.3)
  wd = cluster.jobs["9001"].rec["WorkDir"]
  cluster.pending_views = {"views": [{"type": "job", "jid": "9001"}, {"type": "path", "path": f"{wd}/slurm-9001.out", "crumbs": ["Jobs", "9001"],
                                      "follow": False, "at_end": True},
                                     {"type": "categories", "key": "categories", "path": ""}, {"type": "rules", "rule": "B05"},
                                     {"type": "path", "path": "/no/such/file", "crumbs": ["Jobs"]}],
                           "shown": 1, "jobs_open": True}
  from slurp.ui.restore import placeholders
  placeholders(app, cluster, cluster.pending_views) # (as at the start: there at once)
  app.show_overview(); await pilot.pause(0.3)
  check("at once: placeholders, in their order", [v.title for v in cluster.views] == ["job 9001", "slurm-9001.out", "categories", "Rule catalogue", "file"]
        and all(v.pending for v in cluster.views), [v.title for v in cluster.views])
  app.restore_views(cluster)
  ok = await until(pilot, lambda: cluster.pending_views is None and not cluster.restoring, 30); await pilot.pause(0.5)
  check("the kept subtabs open again, in their order (one that is gone: left out)", ok
        and [v.title for v in cluster.views] == ["job 9001", "slurm-9001.out", "categories", "Rule catalogue"], [v.title for v in cluster.views])
  check("the one shown before is shown: the log, at its end", isinstance(app.screen, FileScreen) and app.screen.path.endswith("slurm-9001.out")
        and app.screen.area.cursor_location[0] >= app.screen.area.document.line_count - 2, type(app.screen).__name__)
  from slurp.ui.restore import tab_views
  kept = tab_views(app, cluster)
  check("and they are kept again as they are", [s["type"] for s in kept["views"]] == ["job", "path", "categories", "rules"] and kept["shown"] == 1, kept)
  for v in list(cluster.views): app.close_view(v)
  await pilot.pause(0.3)
  app.show_view(None); await pilot.pause(0.3)

async def t_auto_refresh(app, pilot):
  """The job window and the array window refresh by themselves every refresh_seconds (here 2), counted down in the
  title bar"""
  real = app.refresh_seconds
  app.refresh_seconds = lambda: 2
  try:
    js = await open_job(app, pilot, "9001")
    timer = js.query_one("#refresh-timer")
    ok = await until(pilot, lambda: timer.display and str(timer.label).startswith("⟳ 0:0"), 5)
    check("the job window: the countdown", ok, str(timer.label))
    before = js.details
    ok = await until(pilot, lambda: js.details is not before, 15)
    check("…it reloads by itself (the job and its files again)", ok)
    for v in list(app.cluster.views): app.close_view(v)
    await pilot.pause(0.3)
    app.show_view(None); await pilot.pause(0.3)
    app.open_array("9700") # (an array of the lab)
    ok = await until(pilot, lambda: isinstance(app.screen, ArrayScreen) and not app.screen.fetching, 15)
    arr = app.screen
    first = arr.last_refresh
    timer = arr.query_one("#refresh-timer")
    ok = ok and await until(pilot, lambda: timer.display and arr.last_refresh > first, 15)
    check("the array window: the countdown, and it reloads by itself", ok, (str(timer.label), first, arr.last_refresh))
  finally:
    app.refresh_seconds = real
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)

async def t_shell_button(app, pilot):
  """Shell… is in the job's button bar (it was hidden at the end of the window, and opened the categorize dialog):
  it asks on which node of the job (radio buttons) and where the shell opens"""
  js = await open_job(app, pilot, "9001")
  nodes = js.details["nodes"]
  button = js.query_one("#shell")
  check("Shell… in the button bar of a running job, with Cancel job, Restart job, …",
        button.display and button.parent.id == "job-actions" and not js.query("#details #shell"), button.parent.id)
  js.query_one("#details").scroll_end(animate=False); await pilot.pause(0.3)
  await pilot.click("#shell")
  ok = await until(pilot, lambda: isinstance(app.screen, (ShellChoiceScreen, CategorizeScreen)), 5)
  scr = app.screen
  radios = [str(b.label) for b in scr.query("#choice-nodes RadioButton")] if ok else []
  check("it asks on which node (the job's, as radio buttons, the first chosen), not Categorize",
        ok and isinstance(scr, ShellChoiceScreen) and radios == nodes and scr.node == nodes[0], (type(scr).__name__, radios, nodes))
  opened = []
  real = app.open_shell
  app.open_shell = lambda args, title, **kw: opened.append((args, title))
  try:
    buttons = list(scr.query("#choice-nodes RadioButton"))
    buttons[-1].value = True; await pilot.pause(0.2)
    await pilot.click("#choice-slurp"); await pilot.pause(0.3)
  finally:
    app.open_shell = real
  args = opened[0][0] if opened else []
  check("In slurp: the shell on the node chosen (srun --overlap --pty -w <node> in the job)",
        args[:2] == ["srun", "--jobid=9001"] and "--overlap" in args and args[args.index("-w") + 1] == nodes[-1], (args, opened))
  await pilot.press("c")
  ok = await until(pilot, lambda: isinstance(app.screen, CategorizeScreen), 5)
  check("c still categorizes the job", ok, type(app.screen).__name__)
  await pilot.press("escape"); await pilot.pause(0.3)
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)
  ended = await open_job(app, pilot, "9008")
  check("a job that ended: no Shell…", not ended.query_one("#shell").display)
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)

async def t_ended_progress(app, pilot):
  """A job that ended keeps its progress file in the job window: where it got to, and how it ended"""
  js = await open_job(app, pilot, "9009") # (FAILED; its output ends with step 28/100)
  line = lambda: str(js.query_one("#progress-line").render())
  ok = await until(pilot, lambda: "the job ended" in line(), 10)
  check("an ended job: its progress file's last line, and that it ended there (FAILED, exit code)",
        ok and "step 28/100 done" in line() and "the job ended FAILED (exit code 1:0) here" in line()
        and "slurm-9009.out" in line() and js.query_one("#job-progress").display, (line(), repr(js.job.progress)[:300], js.job.state))
  check("an ended job without measurements: no Diagnostics (nothing under it)", not js.query_one("#diagnostics-title").display)
  bar = js.query_one("#progress-bar")
  check("…the bar where it got to (not 0 of 1)", bar.total == 100 and bar.progress == 28, (bar.total, bar.progress))
  check("…and another file can be chosen for it", js.query_one("#progress-file").display)
  from slurp.ui.screens.help import ViewHelpScreen
  js.query_one("#progress-help").press()
  ok = await until(pilot, lambda: isinstance(app.screen, ViewHelpScreen), 5)
  doc = app.screen.markdown if ok else ""
  check("? next to Progress file…: what the progress is, what it needs, how much is read, how the paces are made",
        ok and "What it needs" in doc and "the last 64 KiB" in doc and "the last 600 lines" in doc and "On current pace" in doc
        and "last 6 different progress values" in doc and "On same pace" in doc and "flush=True" in doc, doc[:200])
  await pilot.press("escape"); await pilot.pause(0.3)
  check("…Esc: back to the job", app.screen is js)
  wd = js.job.rec["WorkDir"]
  other = os.path.join(wd, "train.log"); open(other, "w").write("epoch 3\nvalidation 62.5% done\n")
  set_progress_file("9009", other); js.load()
  ok = await until(pilot, lambda: "62.5%" in line(), 10)
  check("a file chosen for an ended job: its progress (M% too)", ok and "(chosen for this job)" in line() and js.query_one("#progress-stdout").display, line())
  set_progress_file("9009", None); js.load(); await pilot.pause(0.5)
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)

async def t_memory(app, pilot):
  """Query runtime diagnostics: on which node (all, or one), then the job's memory, CPU, GPU, I/O, disks and
  processes there, measured by the probe as a step of the job; the memory row shows its memory"""
  from slurp.ui.screens.diagnostics import DiagnosticsScreen, NodeChoiceScreen
  calls = os.path.join(LAB, "srun_calls")
  if os.path.exists(calls): os.unlink(calls)
  js = await open_job(app, pilot, "9001")
  js.query_one("#diagnostics").scroll_visible(); await pilot.pause(0.3)
  await pilot.click("#diagnostics")
  ok = await until(pilot, lambda: isinstance(app.screen, NodeChoiceScreen), 5)
  radios = [str(b.label) for b in app.screen.query("RadioButton")] if ok else []
  check("it asks on which node: all of them (chosen) or one of the job's", ok and radios[0].startswith("All nodes")
        and radios[1:] == js.details["nodes"] and app.screen.node == "", radios)
  await pilot.click("#nc-ok")
  ok = await until(pilot, lambda: isinstance(app.screen, DiagnosticsScreen) and "Memory" in str(app.screen.query_one("#diag-text").render()), 30)
  ds = app.screen
  job_view = next(v for v in app.cluster.views if v.key == "job 9001")
  diag_view = next((v for v in app.cluster.views if v.key == "diagnostics 9001 *"), None)
  check("in a subtab of its own (the job's stays), named after the job", ok and diag_view is not None
        and app.current_mode == diag_view.mode and diag_view.title == "diagnostics 9001" and job_view in app.cluster.views,
        [(v.key, v.title) for v in app.cluster.views])
  text = str(ds.query_one("#diag-text").render()) if ok else ""
  call = open(calls).read() if os.path.exists(calls) else ""
  check("the probe runs through srun as a step of the job, the script on stdin, measuring (full), on all nodes",
        ok and "--input=all" in call and "bash -s -- 9001 full" in call and "--overlap" in call, (call[-300:], text[:200]))
  check("the diagnostics: memory (the processes, the node as free), CPU (the node, its load), local disks, the processes",
        "processes: " in text and "node (as free): used " in text and "CPU" in text and "load " in text
        and "Local disks" in text and "Busiest processes of the job" in text and "iB" in text, text)
  status = str(ds.query_one("#diag-status").render())
  check("…measured when, on all nodes", "measured at" in status and "on all " in status, status)
  ds.query_one("#diag-text").update(""); await pilot.press("r")
  ok = await until(pilot, lambda: "Memory" in str(ds.query_one("#diag-text").render()), 30)
  check("r: again", ok)
  app.show_view(job_view); await pilot.pause(0.3)
  ds.query_one("#diag-text").update("")
  await pilot.click("#diagnostics"); await until(pilot, lambda: isinstance(app.screen, NodeChoiceScreen), 5)
  await pilot.click("#nc-ok")
  ok = await until(pilot, lambda: app.screen is ds and "Memory" in str(ds.query_one("#diag-text").render()), 30)
  check("the same diagnostics again: its subtab, measured again (not another)", ok
        and sum(v.key.startswith("diagnostics 9001") for v in app.cluster.views) == 1)
  app.show_view(job_view); await pilot.pause(0.3)
  cpu = str(js.query_one("#memory-cpu").render())
  check("the memory row shows its memory (all nodes: the job's PSS here)", app.screen is js and "(the job's processes, PSS" in cpu
        and "unavailable" not in cpu, cpu)
  node = js.details["nodes"][0]
  await pilot.click("#diagnostics"); await until(pilot, lambda: isinstance(app.screen, NodeChoiceScreen), 5)
  app.screen.query("RadioButton")[1].value = True; await pilot.pause(0.2)
  await pilot.click("#nc-ok")
  ok = await until(pilot, lambda: isinstance(app.screen, DiagnosticsScreen) and "Memory" in str(app.screen.query_one("#diag-text").render()), 30)
  call = open(calls).read().splitlines()[-1] if os.path.exists(calls) else ""
  check("one node: srun -w that node, in a subtab of its own too", ok and f"-w {node}" in call
        and node in str(app.screen.query_one("#diag-status").render())
        and any(v.key == f"diagnostics 9001 {node}" for v in app.cluster.views), call)
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)

async def t_memory_watch(app, pilot):
  import time as _t
  os.environ["XDG_DATA_HOME"] = os.path.join(HERE, "xdg")
  import shutil as _sh; _sh.rmtree(os.environ["XDG_DATA_HOME"], ignore_errors=True)
  js = await open_job(app, pilot, "9003")
  w = js.memory
  js.query_one("#memory-every").value = 300
  status = lambda: str(js.query_one("#memory-watch-status").render())
  ok = await until(pilot, lambda: len(w["samples"]) == 1 and "1 measurement" in status(), 30)
  check("every 5 min: the first measurement at once, the next counted down", ok and "1 measurement" in status() and "next in 4:" in status(), status())
  path = w["file"]
  rows = open(path).read().splitlines() if os.path.exists(path) else []
  check("measurements written to a file (CSV)", rows[:1] == ["time,cpu_job_bytes,gpu_job_bytes,gpu_used_bytes,gpu_total_bytes"] and len(rows) == 2
        and path.startswith(os.environ["XDG_DATA_HOME"]), rows)
  await pilot.click("#view-jobs"); await pilot.pause(0.3)
  w["due"] = 0 # due now: measured while the job window is not shown
  ok = await until(pilot, lambda: len(w["samples"]) == 2, 30)
  check("measured also while another subtab is shown", ok)
  app.show_view(next(v for v in app.cluster.views if v.key == "job 9003")); await pilot.pause(1.5)
  plot = js.query_one("#spark-cpu")
  check("the plot has the measurements (CPU and GPU over time)", [v for _, v in plot.data] == [s[1] for s in w["samples"]]
        and "now" in str(js.query_one("#spark-cpu-text").render()), plot.data)
  # a rising series: the bars grow from left to right, from zero
  t0 = _t.time()
  plot.set_data([(t0 + i * 60, v) for i, v in enumerate([1e9, 2e9, 3e9, 4e9])]); await pilot.pause(0.3)
  lines = ["".join(sg.text for sg in plot.render_line(y)) for y in range(plot.size.height)]
  bottom = lines[plot.size.height - 2][plot.LABEL:]
  tops = lines[0][plot.LABEL:]
  check("bars from zero: a rising series is low on the left, full on the right; max and times labelled",
        bottom.strip() and tops.rstrip().endswith("█") and tops[0] == " " and "3.73 GiB" in lines[0] and ":" in lines[-1], lines)
  from slurp.ui.screens.diagnostics import DiagnosticsScreen, NodeChoiceScreen
  b = js.query_one("#diagnostics")
  b.scroll_visible(); await pilot.pause(0.3)
  await pilot.click("#diagnostics"); await until(pilot, lambda: isinstance(app.screen, NodeChoiceScreen), 5)
  await pilot.click("#nc-ok")
  ok = await until(pilot, lambda: len(w["samples"]) == 3, 30)
  check("diagnostics of all nodes are kept as a measurement too", ok and len(open(path).read().splitlines()) == 4)
  await until(pilot, lambda: isinstance(app.screen, DiagnosticsScreen), 5)
  js.query_one("#memory-every").value = 0
  for v in list(app.cluster.views): app.close_view(v)
  await pilot.pause(0.3)

async def t_scratch_exclude(app, pilot):
  import time as _t
  import shutil as _sh
  d = os.path.join(LAB, "scratch")
  _sh.rmtree(d, ignore_errors=True); os.makedirs(os.path.join(d, "keep"))
  now = _t.time()
  for name, days in (("old.dat", 90), ("near.dat", 27), ("new.dat", 2), ("keep/kept.dat", 80)):
    p = os.path.join(d, name); open(p, "w").write("x"); os.utime(p, (now - days * 86400, now - days * 86400))
  app.show_view(None); await pilot.pause(0.3)
  await pilot.press("X")
  ok = await until(pilot, lambda: isinstance(app.screen, ScratchScreen) and app.screen.result is not None, 30)
  scr = app.screen
  table = scr.query_one("#scratch-files")
  rows = lambda: [(os.path.relpath(str(table.get_row_at(i)[5]), d), table.get_row_at(i)[5].style) for i in range(table.row_count)]
  check("old files red, those close to the limit (27 of 30 days) yellow", ok and dict(rows()).get("near.dat") == "yellow"
        and dict(rows()).get("old.dat") == "red" and dict(rows()).get("new.dat") == "", rows())
  scr.query_one("#scratch-exclude").value = "/keep/"; scr.query_one("#scratch-exclude").focus(); await pilot.press("enter"); await pilot.pause(0.3)
  status = str(scr.query_one("#scratch-status").render())
  check("the exclude regex leaves matching files out of the list", "keep/kept.dat" not in dict(rows()) and len(rows()) == 3
        and "1 of them excluded" in status, (rows(), status))
  await pilot.click("#touch-all"); await pilot.pause(0.3)
  check("the confirmation names the exclusion", "except those matching /keep/" in str(app.screen.dialog_message))
  await pilot.click("#confirm-yes")
  ok = await until(pilot, lambda: os.stat(os.path.join(d, "old.dat")).st_atime > now - 60, 20)
  check("touch all: the excluded file is left alone", ok and os.stat(os.path.join(d, "keep/kept.dat")).st_atime < now - 70 * 86400)
  app.action_close_view(); await pilot.pause(0.3)

def arrays(on):
  f = os.path.join(LAB, "arrays")
  if on: open(f, "w").close()
  elif os.path.exists(f): os.unlink(f)

async def reload(app, pilot):
  app.show_view(None); await pilot.pause(0.2)
  app.action_reload(); await until(pilot, lambda: app.cluster.loader is None, 60); await pilot.pause(0.3)

async def t_array_progress(app, pilot):
  wd = os.path.join(LAB, "jobs", "9500")
  arrays(True)
  os.makedirs(wd, exist_ok=True)
  for name in os.listdir(wd):
    if name.startswith("train_"): os.unlink(os.path.join(wd, name))
  for task, stamp, k in ((1, "2026-11-13T10-01-07", 3), (2, "2026-11-13T10-02-41", 4), (3, "2026-11-13T09-59-13", 5)):
    name = f"train_task{task:02d}_{stamp}.log" if task == 3 else f"train_task{task}_{stamp}.log"
    open(os.path.join(wd, name), "w").write(f"epoch {k}/10\n")
  open(os.path.join(wd, "train_task2_2026-11-12T08-00-00.log"), "w").write("epoch 1/10\n") # an older run of task 2
  os.utime(os.path.join(wd, "train_task2_2026-11-12T08-00-00.log"), (1e9, 1e9))
  await reload(app, pilot)
  js = await open_job(app, pilot, "9500_1")
  js.choose_progress_file(type("E", (), {"stop": lambda self: None})())
  await until(pilot, lambda: isinstance(app.screen, FilePickScreen) and app.screen.entries, 10)
  dt = app.screen.query_one("#fp-list"); dt.focus(); dt.move_cursor(row=dt.get_row_index("train_task1_2026-11-13T10-01-07.log")); await pilot.press("enter")
  ok = await until(pilot, lambda: any("array 9500" in m for _, m in notes(app)), 15)
  files = {j: os.path.basename(progress_state(j)["file"] or "") for j in ("9500_2", "9500_3", "9500_4")}
  check("an array task's progress file: the files of the other running tasks guessed (datetime differs, zero padding)",
        ok and files["9500_2"] == "train_task2_2026-11-13T10-02-41.log" and files["9500_3"] == "train_task03_2026-11-13T09-59-13.log"
        and files["9500_4"] == "", (files, [m for _, m in notes(app)][-1:]))
  check("the message counts them: 2 of 3 running found, 2 not running", any("found for 2 of 3 other running tasks (2 not running)" in m for _, m in notes(app)),
        [m for _, m in notes(app)][-1:])
  await reload(app, pilot)
  check("the job list shows their progress", app.jobs["9500_2"].progress and app.jobs["9500_2"].progress.current == 4
        and app.jobs["9500_3"].progress.current == 5, (app.jobs["9500_2"].progress, app.jobs["9500_3"].progress))
  open(os.path.join(wd, "train_task4_2026-11-13T11-00-00.log"), "w").write("epoch 7/10\n")
  await reload(app, pilot)
  check("a file that appears later is found at the next reload", app.jobs["9500_4"].progress and app.jobs["9500_4"].progress.current == 7)
  for v in list(app.cluster.views): app.close_view(v)
  arrays(False)
  await reload(app, pilot)

async def t_collapse(app, pilot):
  arrays(True)
  await reload(app, pilot)
  t = app.main.query_one("#jobs")
  keys = lambda: [str(t.coordinate_to_cell_key(Coordinate(i, 0)).row_key.value) for i in range(t.row_count)]
  groups = [k for k in keys() if k.startswith("group|")]
  cell = lambda key, c: t.get_row_at(t.get_row_index(key))[c]
  check("a big array is one row; a small one (6 tasks) is not collapsed", sorted(groups) == ["group|9600", "group|9700"]
        and "9500_1" in keys() and not any(k.startswith("9600_") for k in keys()), groups)
  state = cell("group|9600", 2)
  check("the row: which tasks, how many, and the mix of states (Slurm's codes, coloured)", str(cell("group|9600", 0)) == "9600_[1-150] ▸ 150 tasks"
        and str(state) == "30R 10PD 100CD 10F" and [str(sp.style) for sp in state.spans][:4] == ["bold green", "blue", "green", "red"],
        (str(cell("group|9600", 0)), str(state), [str(sp.style) for sp in state.spans]))
  rows_before = t.row_count
  t.focus(); t.move_cursor(row=t.get_row_index("group|9600")); await pilot.press("space"); await pilot.pause(0.3)
  check("Space opens it: every task below the row", t.row_count == rows_before + 150 and "9600_17" in keys()
        and str(cell("group|9600", 0)).endswith("▾ 150 tasks") and isinstance(app.screen, MainScreen), t.row_count)
  check("the tasks of an opened array are indented, other jobs not", str(cell("9600_17", 0)) == "  9600_17"
        and str(cell("9500_1", 0)) == "9500_1" and str(cell("9001", 0)) == "9001", (str(cell("9600_17", 0)), str(cell("9500_1", 0))))
  await reload(app, pilot)
  check("it stays open after a reload", "9600_17" in keys())
  t.move_cursor(row=t.get_row_index("group|9600")); await pilot.press("space"); await pilot.pause(0.3)
  check("Space again closes it", t.row_count == rows_before and "9600_17" not in keys())
  cb = app.main.query_one("#collapse-arrays")
  cb.value = False; await pilot.pause(0.3)
  check("group arrays off: every task individually", not any(k.startswith("group|") for k in keys()) and "9600_77" in keys()
        and t.row_count == rows_before + 160, t.row_count)
  t.focus(); await pilot.press("g"); await pilot.pause(0.3)
  check("g switches back", cb.value and "group|9600" in keys() and t.row_count == rows_before)
  t.move_cursor(row=t.get_row_index("9500_1")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen), 10)
  check("other rows open their job as before", ok and app.screen.job.jid == "9500_1")
  for v in list(app.cluster.views): app.close_view(v)
  arrays(False)
  await reload(app, pilot)

async def t_array_window(app, pilot):
  calls = os.path.join(LAB, "scancel_calls")
  if os.path.exists(calls): os.unlink(calls)
  arrays(True)
  await reload(app, pilot)
  t = app.main.query_one("#jobs")
  t.focus(); t.move_cursor(row=t.get_row_index("group|9600")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, ArrayScreen) and app.screen.groups, 20)
  scr = app.screen
  groups = {g: len(ids) for g, ids in scr.groups.items()} if ok else {}
  check("Enter on a collapsed array: its window (a subtab) with every task by state and exit code", ok and groups == {
        "RUNNING": 30, "COMPLETED (exit 0:0)": 100, "FAILED (exit 1:0)": 5, "FAILED (exit 2:0)": 5, "PENDING (Priority)": 10}
        and "array 9600" in [v.title for v in app.cluster.views], groups)
  texts = [str(w.render()) for w in scr.query(".arr-ids")]
  check("the ids comma separated, and as ranges", any(x.startswith("1, 2, 3") and "= 1-30" in x for x in texts), texts[:1])
  info = rendered(scr.info_table)
  check("information about the array: tasks, counts, run times, nodes", "150: 1-150" in info and "100 completed" in info
        and "median" in info and "Running on" in info, info[:400])
  check("queue wait and run times: min, avg, max", re.search(r"Queue wait \(started\) +min 5 min · avg 5 min .*\(140 tasks\)", info) and "Run time (ended)" in info
        and "Run time (completed)" in info and "Running for (running)" in info and "Waiting so far (pending)" in info, info)
  m = re.search(r"Node hours \(total\) +([0-9.]+) +over (\d+) tasks that ran, ([0-9.]+) of them by running", info)
  check("the array's total node hours, with the running part", m and float(m.group(1)) > float(m.group(3)) > 0 and int(m.group(2)) == 140, m and m.groups())
  import slurp.ui.screens.dialogs as dlg
  writes = []
  real_write = app._driver.write
  app._driver.write = lambda data: (writes.append(data), real_write(data))[1]
  button = next(b for b in scr.query(".arr-head Button") if b.parent.group == "FAILED (exit 2:0)")
  button.scroll_visible(); await pilot.pause(0.2); await pilot.click(button); await pilot.pause(0.5)
  import base64
  osc = "\x1b]52;c;" + base64.b64encode(b"132,134,136,138,140").decode() + "\x07"
  check("Copy ids without a clipboard tool: sent to the terminal (OSC 52), a window with ways to get it by hand",
        osc in writes and isinstance(app.screen, CopyScreen) and app.screen.query_one("#copy-text").text == "132,134,136,138,140"
        and app.screen.query_one("#copy-plain").display, [w for w in writes if "]52" in w])
  os.environ["TMUX"] = "/tmp/x,1,0"
  check("in tmux the OSC 52 is wrapped for passthrough", dlg.osc52("a").startswith("\x1bPtmux;\x1b\x1b]52;c;"))
  os.environ.pop("TMUX")
  await pilot.click("#copy-plain"); await pilot.pause(0.3)
  check("Plain text without a real terminal: a notice, no crash", app.is_running and any("cannot leave" in m for _, m in notes(app)))
  await pilot.click("#copy-save"); await pilot.pause(0.3)
  check("Save to a file", open(dlg.copied_file()).read() == "132,134,136,138,140\n" and any(dlg.copied_file() in m for _, m in notes(app)))
  await pilot.click("#copy-arrived"); await pilot.pause(0.3)
  check("It arrived: stored, the window closes", not isinstance(app.screen, CopyScreen) and load_config().get("copy") == "terminal")
  button.scroll_visible(); await pilot.pause(0.2); await pilot.click(button); await pilot.pause(0.5)
  check("after that: only a notice", not isinstance(app.screen, CopyScreen) and any("through the terminal" in m for _, m in notes(app)))
  update_config(copy=None)
  clip = os.path.join(HERE, "clipbin")
  os.environ["PATH"] = clip + ":" + os.environ["PATH"]; os.environ["SSH_CONNECTION"] = "1.2.3.4 5 6.7.8.9 22"
  if os.path.exists(os.path.join(clip, "clipboard.txt")): os.unlink(os.path.join(clip, "clipboard.txt"))
  button.scroll_visible(); await pilot.pause(0.2); await pilot.click(button); await pilot.pause(0.5)
  check("over SSH, pbcopy of the remote machine is not used", isinstance(app.screen, CopyScreen)
        and not os.path.exists(os.path.join(clip, "clipboard.txt")))
  await pilot.press("escape"); await pilot.pause(0.3)
  os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]; os.environ.pop("SSH_CONNECTION")
  clip = os.path.join(HERE, "clipbin")
  os.environ["PATH"] = clip + ":" + os.environ["PATH"]; os.environ["DISPLAY"] = ":99"
  await pilot.click("#arr-copy-all"); await pilot.pause(0.5)
  got = open(os.path.join(clip, "clipboard.txt")).read() if os.path.exists(os.path.join(clip, "clipboard.txt")) else ""
  check("with a clipboard tool (xclip): the ids are on the clipboard, no window", got.startswith("1,2,3") and got.count(",") == 149
        and not isinstance(app.screen, CopyScreen) and any("(xclip)" in m for _, m in notes(app)), got[:30])
  os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]; os.environ.pop("DISPLAY")
  scr.query_one("#arr-tabs").active = "arr-tab-tasks"; await pilot.pause(0.3)
  tt = scr.query_one("#arr-tasks")
  row = lambda key: [str(c) for i, c in enumerate(tt.get_row(key)) if i != 2] # (without the progress bar)
  check("Tasks: every task in a list, with state, exit code, queue wait, times, node", tt.row_count == 150
        and row("17")[:3] == ["9600_17", "RUNNING", "0:0"] and row("132")[1:3] == ["FAILED", "2:0"] and row("17")[4] == "5 min"
        and row("145")[1] == "PENDING (Priority)", (row("17"), row("132"), row("145")))
  bar = lambda key: tt.get_row(key)[2]
  check("Tasks: a progress bar for each, as in the job list (completed full, failed empty, pending none)",
        bar("100").completed == bar("100").total and bar("132").completed == 0 and bar("145") == "", (bar("100"), bar("132"), bar("145")))
  task17 = app.cluster.jobs["9600_17"]
  saved = task17.progress
  task17.progress = JobProgress(37, 100, "37/100")
  scr.refresh_task_progress()
  check("a running task: its n/N progress from the job list, refreshed", bar("17").completed == 37 and bar("17").total == 100, bar("17"))
  task17.progress = saved
  # sorting by the headers
  order = lambda: [r.value for r in tt.rows]
  labels = lambda: [str(c.label) for c in tt.columns.values()]
  check("Tasks: sorted by task at first (▲)", order()[:3] == ["1", "2", "3"] and labels()[0] == "Task ▲", (order()[:3], labels()[:2]))
  await pilot.click(tt, offset=(2, 0)); await pilot.pause(0.3)
  check("clicking the Task header reverses it (▼)", order()[:3] == ["150", "149", "148"] and labels()[0] == "Task ▼", (order()[:3], labels()[:1]))
  def header_x(name):
    x = 0
    for c in tt.columns.values():
      if str(c.label).startswith(name): return x + 2
      x += c.get_render_width(tt)
  await pilot.click(tt, offset=(header_x("Start"), 0)); await pilot.pause(0.3)
  starts = [epoch(scr.overview["tasks"][int(k)].get("start")) or 0 for k in order()]
  check("clicking Start sorts by start time, latest first (▼), by value", starts == sorted(starts, reverse=True)
        and any(l == "Start ▼" for l in labels()) and labels()[0] == "Task", labels())
  await pilot.click(tt, offset=(header_x("Start"), 0)); await pilot.pause(0.3)
  starts = [epoch(scr.overview["tasks"][int(k)].get("start")) or 0 for k in order()]
  check("…again: earliest first (▲)", starts == sorted(starts) and any(l == "Start ▲" for l in labels()))
  await pilot.click(tt, offset=(header_x("State"), 0)); await pilot.pause(0.3)
  states = [scr.overview["tasks"][int(k)]["state"] for k in order()]
  check("clicking State groups the tasks by state", states == sorted(states), states[:3])
  await pilot.click(tt, offset=(2, 0)); await pilot.pause(0.3)
  async def back_to_array():
    for v in [v for v in app.cluster.views if v.key.startswith("job ")]: app.close_view(v)
    app.show_view(app.find_view("array 9600")); await pilot.pause(0.3)
  views = len(app.cluster.views)
  tt.focus(); tt.move_cursor(row=tt.get_row_index("17")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen), 10)
  check("a task opens its job window, in a subtab of its own", ok and app.screen.job.jid == "9600_17" and len(app.cluster.views) == views + 1
        and app.cluster.views[-1].key == "job 9600_17", [v.key for v in app.cluster.views])
  await back_to_array()
  check("back to the array window (its subtab)", app.screen is scr)
  gone = app.cluster.jobs.pop("9600_120") # a task that is not in the job list: loaded from Slurm
  tt.focus(); tt.move_cursor(row=tt.get_row_index("120")); await pilot.press("enter")
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen), 15)
  check("a task that is not in the job list is loaded first, in a subtab too", ok and app.screen.job.jid == "9600_120" and len(app.cluster.views) == views + 1)
  app.cluster.jobs["9600_120"] = gone
  await back_to_array()
  scr.query_one("#arr-tabs").active = "arr-tab-states"; await pilot.pause(0.3)
  # an id in By state opens the job window of its task
  group = next(g for g in scr.query(".arr-group") if g.query_one(".arr-head").group == "FAILED (exit 2:0)")
  ids = group.query_one(".arr-ids")
  ids.scroll_visible(); await pilot.pause(0.3)
  text = str(ids.render()).split("\n")[0]
  await pilot.click(ids, offset=(text.index("134") + 1, 0))
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen), 10)
  check("By state: clicking an id opens the job window of that task, in a subtab", ok and app.screen.job.jid == "9600_134"
        and app.cluster.views[-1].key == "job 9600_134", text)
  await back_to_array()
  check("back to the array window", app.screen is scr)
  await pilot.click("#arr-cancel-pending"); await pilot.pause(0.3)
  msg = str(app.screen.dialog_message) if isinstance(app.screen, CancelScreen) else ""
  check("cancelling the pending tasks asks first, with what and the command", "10 tasks (141-150)" in msg and "scancel --state=PENDING 9600" in msg, msg)
  dlg = app.screen
  if isinstance(dlg, CancelScreen):
    next(b for b in dlg.query("RadioButton") if b.name == "allocation").value = True; await pilot.pause(0.1)
    dlg.query_one("#cr-details").value = "array too small"
    await pilot.click("#cr-ok")
  ok = await until(pilot, lambda: os.path.exists(calls), 10)
  check("and runs scancel --state=PENDING", ok and open(calls).read().strip() == "--state=PENDING 9600")
  ok = await until(pilot, lambda: (app.cluster.categories.cancel_reason_of("9600") or {}).get("reason") == "allocation", 10)
  entry = app.cluster.categories.cancel_reason_of("9600") or {}
  check("the reason is kept with the array: what, details, which tasks", ok and entry.get("text") == "array too small" and entry.get("what") == "the pending tasks", entry)
  await until(pilot, lambda: "Cancelled because" in rendered(scr.info_table), 10)
  check("the array window shows it", "Wrong resources asked for" in rendered(scr.info_table), rendered(scr.info_table)[:300])
  for _ in range(3): # (the window may still be settling after the dialog before: click again)
    await pilot.click("#arr-cancel")
    if await until(pilot, lambda: isinstance(app.screen, CancelScreen), 3):
      break
  msg = str(app.screen.dialog_message) if isinstance(app.screen, CancelScreen) else ""
  check("cancelling the whole array asks first", "running tasks included" in msg and "scancel 9600" in msg, msg)
  await pilot.click("#cr-back"); await pilot.pause(0.3)
  check("Do not cancel: nothing cancelled", open(calls).read().count("\n") == 1)
  js = await open_job(app, pilot, "9500_1")
  b = js.query_one("#array-window")
  check("the job window of an array task has an Array button", b.display)
  jinfo = rendered(js.query_one("#info").content)
  check("the job window: total node hours, so far for a running job", re.search(r"Node hours \(total\) +[0-9.]+ +\d+ nodes? × [0-9.]+ h +so far", jinfo), jinfo)
  views = len(app.cluster.views)
  b.scroll_visible(); await pilot.pause(0.2); await pilot.click("#array-window")
  ok = await until(pilot, lambda: isinstance(app.screen, ArrayScreen) and app.screen.groups, 20)
  check("it opens the window of its array, in a subtab of its own", ok and app.screen.array_id == "9500" and len(app.cluster.views) == views + 1
        and {g: len(i) for g, i in app.screen.groups.items()} == {"RUNNING": 4, "COMPLETED (exit 0:0)": 1, "PENDING (Priority)": 1},
        app.screen.groups if ok else None)
  for v in list(app.cluster.views)[views:]: app.close_view(v)
  await pilot.pause(0.3)
  check("closed: back to the job", app.screen is js, type(app.screen).__name__)
  # the Array line of its info: a link to the array, in a subtab of its own
  grid = js.query_one("#info").content
  cell = next((c for c in grid.columns[1]._cells if "(task 1)" in str(c)), None)
  clicks = [s.style.meta.get("@click") for s in (cell.spans if cell is not None else []) if s.style and not isinstance(s.style, str)]
  check("the job window of an array task: an Array line, a link", str(cell).startswith("9500  (task 1)") and "screen.open_array_window" in clicks, (str(cell), clicks))
  views = len(app.cluster.views)
  lines = rendered(js.query_one("#info").content).split("\n")
  y = next(i for i, line in enumerate(lines) if "9500  (task 1)" in line)
  x = lines[y].index("9500  (task 1)") + 2
  js.query_one("#info").scroll_visible(); await pilot.pause(0.2)

  await pilot.click("#info", offset=(x, y)); await pilot.pause(0.5)

  ok = await until(pilot, lambda: isinstance(app.screen, ArrayScreen) and app.screen.groups, 20)
  check("a click on it: the array window in a new subtab", ok and app.screen.array_id == "9500" and len(app.cluster.views) == views + 1
        and app.cluster.views[-1].key == "array 9500", [v.key for v in app.cluster.views])
  await js.run_action("open_array_window"); await pilot.pause(0.3)
  check("again: that subtab, not another", len(app.cluster.views) == views + 1)
  # from the array window: the job window of a task (its own subtab), its Array line: the array's subtab again
  arr = app.screen
  ids = arr.query(".arr-ids").first()
  await pilot.click(ids, offset=(0, 0))
  ok = await until(pilot, lambda: isinstance(app.screen, JobScreen), 10)
  js3 = app.screen; views = len(app.cluster.views)
  await until(pilot, lambda: "(task " in rendered(js3.query_one("#info").content) and js3.query_one("#info").region.height > 3, 10)
  js3.query_one("#info").scroll_visible(); await pilot.pause(0.3)
  lines = rendered(js3.query_one("#info").content).split("\n")
  y = next((i for i, line in enumerate(lines) if "(task " in line), 0)
  await pilot.click("#info", offset=(lines[y].index("9500") + 1, y))
  ok = ok and await until(pilot, lambda: app.screen is arr, 20)
  check("a task opened from the array window, a click on its Array line: the array's subtab (the job window stays in its own)",
        ok and len(app.cluster.views) == views and js3.is_attached, ([v.key for v in app.cluster.views], type(app.screen).__name__))
  js2 = await open_job(app, pilot, "9001")
  check("no Array line for other jobs", "(task " not in rendered(js2.query_one("#info").content))
  check("no Array button for other jobs", not js2.query_one("#array-window").display)
  for v in list(app.cluster.views): app.close_view(v)
  arrays(False)
  await reload(app, pilot)

async def t_array_done(app, pilot):
  arrays(True)
  await reload(app, pilot)
  app.open_array("9700")
  ok = await until(pilot, lambda: isinstance(app.screen, ArrayScreen) and app.screen.groups, 20)
  info = rendered(app.screen.info_table) if ok else ""
  check("an array with no queued task: no squeue error, its tasks listed", ok and "Invalid job id" not in info and "error" not in info
        and {g: len(i) for g, i in app.screen.groups.items()} == {"COMPLETED (exit 0:0)": 12}, info[-300:])
  scr = app.screen
  started = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
  scr.overview = {"errors": [], "tasks": {
    1: {"state": "RUNNING", "exit": "0:0", "reason": "", "submit": started, "start": started, "end": "Unknown", "elapsed": "00:10:00", "nodes": "1", "node": "nid1"},
    2: {"state": "PENDING", "exit": "", "reason": "Priority", "submit": started, "start": "", "end": "", "elapsed": "", "nodes": "1", "node": ""}}}
  scr.show_groups(); scr.show_tasks(); scr.show_info(); await pilot.pause(0.3)
  info = rendered(scr.info_table)
  check("running and pending tasks, none completed yet: no crash, no estimate", app.is_running and "no estimate until a task has completed" in info, info[-400:])
  real = scr.fill_info
  scr.fill_info = lambda: 1 / 0
  scr.show_info(); await pilot.pause(0.3)
  check("a mistake in the information is shown there, the app keeps running", app.is_running and "ZeroDivisionError" in rendered(scr.info_table))
  scr.fill_info = real
  for v in list(app.cluster.views): app.close_view(v)
  arrays(False)
  await reload(app, pilot)

async def t_array_reload(app, pilot):
  arrays(True)
  await reload(app, pilot)
  app.open_array("9600")
  ok = await until(pilot, lambda: isinstance(app.screen, ArrayScreen) and app.screen.groups, 20)
  scr = app.screen
  check("array window loads", ok)
  for i in range(3):
    await pilot.click("#arr-reload"); await pilot.pause(0.05)
  await pilot.press("r"); await pilot.pause(0.05); await pilot.press("r")
  ok = await until(pilot, lambda: not scr.fetching and scr.groups and len(scr.query(".arr-group")) == 5, 20)
  check("reloading (several times quickly) does not crash, the window is filled again", ok and app.is_running,
        (scr.fetching, len(scr.query(".arr-group"))))
  open(os.path.join(LAB, "delay"), "w").write("3") # a slow Slurm: the seconds are counted, the old content stays
  scr.action_reload(); await pilot.pause(2.2)
  info = rendered(scr.info_table)
  check("while Slurm is asked: the seconds so far, the tasks of before stay", re.search(r"asking Slurm … [123] s", info) and len(scr.query(".arr-group")) == 5, info[-200:])
  ok = await until(pilot, lambda: not scr.fetching and len(scr.query(".arr-group")) == 5 and "as of" in rendered(scr.info_table), 30)
  open(os.path.join(LAB, "delay"), "w").write("0")
  check("then the new content", ok, (scr.fetching, len(scr.query(".arr-group")), rendered(scr.info_table)[-150:]))
  orig = array_overview
  import slurp.ui.screens.array as arrmod
  arrmod.array_overview = lambda array_id: (_ for _ in ()).throw(RuntimeError("sacct timed out"))
  scr.action_reload()
  ok = await until(pilot, lambda: not scr.fetching, 10)
  check("a failed load says so in the window (and keeps the tasks)", ok and "loading failed" in rendered(scr.info_table)
        and len(scr.query(".arr-group")) == 5 and app.is_running)
  arrmod.array_overview = orig
  # an error while a result is shown does not end the app
  bad = scr.show_groups
  scr.show_groups = lambda: 1 / 0
  scr.action_reload()
  ok = await until(pilot, lambda: not scr.fetching and any("showing loading array 9600 failed" in m for _, m in notes(app)), 15)
  check("an error while showing the result: a message, the app goes on", ok and app.is_running, [m for _, m in notes(app)][-2:])
  scr.show_groups = bad
  # switched away while it loads: the result must still arrive
  await pilot.click("#arr-reload"); await pilot.pause(0.02)
  app.show_view(None); await pilot.pause(1.5)
  app.show_view(next(v for v in app.cluster.views if v.key == "array 9600")); await pilot.pause(0.3)
  ok = await until(pilot, lambda: not scr.fetching and scr.groups, 10)
  check("a load finishes also when another subtab was shown meanwhile", ok, scr.fetching)
  for v in list(app.cluster.views): app.close_view(v)
  arrays(False)
  await reload(app, pilot)

async def t_lazy(app, pilot):
  delay = os.path.join(LAB, "stat_delay")
  t = app.main.query_one("#jobs")
  def cell(jid, key):
    keys = [k.value for k in t.columns]
    return t.get_row(jid)[keys.index(key)]
  import slurp.cluster.jobs as sj
  real_run_many, seen = sj.run_many, []
  sj.run_many = lambda cmds, *a, **k: (seen.extend(cmds), real_run_many(cmds, *a, **k))[1]
  try:
    await reload(app, pilot)
  finally:
    sj.run_many = real_run_many
  running = [j for j in app.cluster.jobs.values() if j.running]
  progress_cmds = [c for c in seen if c[0] == "sh" and "grep -E" in c[2]]
  check("the progress of all running jobs in one command (only their n/N lines come back)", len(running) >= 5
        and len(progress_cmds) == 1 and not any(c[0] == "tail" for c in seen)
        and sum(j.progress is not None for j in running) >= 5, (len(running), [c[:2] for c in seen][:6]))
  known_cmd = str(cell("9001", "cmd"))
  open(delay, "w").write("4")
  try:
    app.cluster.jobs = {}; ERR_SIZES.clear() # as at the first load: nothing known
    app.populate(); app.show_view(None); await pilot.pause(0.2)
    t0 = time.time(); app.action_reload()
    ok = await until(pilot, lambda: t.row_count >= 40, 10)
    shown = time.time() - t0
    loading = app.cluster.loader is not None
    check("the list shows before the files of the jobs are read", ok and loading and shown < 3.5, f"{shown:.1f} s, loading {loading}")
    check("cells still coming: spinners (error file, command, progress of a running job)",
          all(isinstance(cell("9001", k), Spinning) for k in ("err", "cmd", "progress", "eta_now"))
          and isinstance(cell("9020", "err"), Spinning) and not isinstance(cell("9005", "progress"), Spinning),
          [type(cell("9001", k)).__name__ for k in ("err", "cmd", "progress")])
    import re as _re
    def frames(): # the row of 9001 as the table draws it
      y = t.get_row_index("9001") + 1 - t.scroll_offset.y
      return "".join(_re.findall("[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", t.render_line(y).text))
    f1 = frames(); await pilot.pause(0.4); f2 = frames()
    check("the spinners in the cells turn", f1 and f2 and f1 != f2, (f1, f2))
    status = str(app.load_text(app.cluster))
    check("the status says the files are being read", "reading the files of the jobs" in status, status)
    await until(pilot, lambda: app.cluster.loader is None, 20); await pilot.pause(0.3)
    check("then the values replace the spinners", str(cell("9001", "cmd")) == known_cmd and "!" in str(cell("9000", "err"))
          and not any(isinstance(cell("9001", k), Spinning) for k in ("err", "cmd", "progress")), (str(cell("9000", "err")), str(cell("9001", "cmd"))))
    app.action_reload()
    await until(pilot, lambda: app.cluster.listed, 10)
    check("a reload keeps the values of the last load meanwhile, no spinners", app.cluster.loader is not None
          and str(cell("9001", "cmd")) == known_cmd and not isinstance(cell("9001", "err"), Spinning))
    await until(pilot, lambda: app.cluster.loader is None, 20)
    app.cluster.jobs = {}; ERR_SIZES.clear(); app.populate()
    app.action_reload()
    await until(pilot, lambda: app.cluster.listed, 10)
    app.action_abort_load(); await pilot.pause(0.5)
    check("aborting while the files are read: the list stays, no spinners left", t.row_count >= 40 and app.cluster.loader is None
          and not any(isinstance(cell("9001", k), Spinning) for k in ("err", "cmd", "progress"))
          and any("reading the files of the jobs aborted" in m for _, m in notes(app)), (t.row_count, app.cluster.loader, [type(cell("9001", k)).__name__ for k in ("err", "cmd", "progress")], notes(app)[-3:]))
  finally:
    os.unlink(delay)
  await reload(app, pilot)

class LinkClient:
  """the user's terminal: a real ssh in a pseudo terminal, the password from askpass"""
  def __init__(self, argv, password):
    import pty
    import struct
    import fcntl
    import termios
    pw = os.path.join(HERE, "share", "pw"); open(pw, "w").write(password)
    env = os.environ | {"SSH_ASKPASS": os.path.join(HERE, "share", "askpass.sh"), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0",
                        "SHARE_PW_FILE": pw, "TERM": "xterm-256color"}
    self.pid, self.fd = pty.fork()
    if self.pid == 0:
      os.execvpe(argv[0], argv, env)
    fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    self.out = b""
  def send(self, text): os.write(self.fd, text.encode())
  async def read_until(self, pilot, pattern, t=20):
    import select
    t0 = time.time()
    while time.time() - t0 < t:
      if select.select([self.fd], [], [], 0)[0]:
        try: chunk = os.read(self.fd, 65536)
        except OSError: break
        if not chunk: break
        self.out += chunk
        if re.search(pattern, self.out.decode(errors="replace")): return True
      else:
        await pilot.pause(0.05)
    return False
  async def wait(self, pilot, t=15):
    t0 = time.time()
    while time.time() - t0 < t:
      pid, status = os.waitpid(self.pid, os.WNOHANG)
      if pid: return os.waitstatus_to_exitcode(status)
      await pilot.pause(0.05)
    return None

def link_client_argv(link, known):
  import shlex
  argv = shlex.split(client_commands(link)[0][1])
  return [argv[0], "-tt", "-o", f"UserKnownHostsFile={known}", "-o", "StrictHostKeyChecking=accept-new", *argv[1:]]

async def t_links(app, pilot):
  known = os.path.join(HERE, "share", "ui_known_hosts")
  if os.path.exists(known): os.unlink(known)
  app.show_view(None); await pilot.pause(0.2)
  # Shell asks where, every time
  await menu(app, pilot, "cluster_shell"); await pilot.pause(0.4)
  check("Shell asks: in slurp or from your own terminal", isinstance(app.screen, ShellChoiceScreen)
        and not app.screen.query_one("#choice-own").disabled)
  await pilot.click("#choice-own"); await pilot.pause(0.5)
  scr = app.screen
  warning = str(scr.query_one("#setup-warning").render()) if isinstance(scr, LinkSetupScreen) else ""
  check("From your own terminal: the setup panel, with the public relay and its warning", isinstance(scr, LinkSetupScreen)
        and scr.chosen() == "sshj" and scr.query_one("#setup-relay-sshj").value and "PUBLIC relay" in warning and "third party" in warning, warning[:120])
  check("the relays to choose from: direct, ssh-j.com, serveo.net, pinggy.io, sshreach.me, custom; each with a line on what it is",
        [b.id for b in scr.query("#setup-relays RadioButton")] == [f"setup-relay-{r}" for r in ("direct", "sshj", "serveo", "pinggy", "sshreach", "custom")]
        and all(len(str(b.label)) > 30 for b in scr.query("#setup-relays RadioButton")))
  shown = lambda: [r.display for r in scr.query(".setup-for-direct, .setup-for-sshreach, .setup-for-custom")]
  check("ssh-j.com: no fields of the others", shown() == [False] * 4, shown())
  for rid, intro in (("serveo", "serveo.net: a PUBLIC relay"), ("pinggy", "Anyone on the internet can reach"), ("sshreach", "with an account"),
                     ("direct", "Direct, no relay"), ("custom", "a plain OpenSSH jump host")):
    scr.query_one(f"#setup-relay-{rid}").value = True; await pilot.pause(0.2)
    detail = str(scr.query_one("#setup-warning").render())
    check(f"choosing {rid}: its detailed explanation" + (", its fields" if rid in ("direct", "sshreach", "custom") else ""), intro in detail
          and all(r.display == (f"setup-for-{rid}" in r.classes) for r in scr.query(".setup-for-direct, .setup-for-sshreach, .setup-for-custom")),
          (detail[:150], shown()))
  scr.query_one("#setup-relay-sshreach").value = True; await pilot.pause(0.1)
  scr.query_one("#setup-sshreach").value = "u1@fw1.sshreach.me:10248"; await pilot.pause(0.2)
  check("sshreach.me with user@server:port: valid", "with an account" in str(scr.query_one("#setup-warning").render())
        and scr.relay() == "sshreach://u1@fw1.sshreach.me:10248")
  scr.query_one("#setup-relay-sshj").value = True; await pilot.pause(0.2)
  check("defaults of a shell: once, 1 hour, password", scr.query_one("#setup-uses").value == 1
        and scr.query_one("#setup-expiry").value == 3600 and scr.query_one("#setup-auth-password").value)
  app.save_screenshot(filename="links_setup.svg", path=OUT)
  scr.query_one("#setup-relay-custom").value = True
  relay = scr.query_one("#setup-relay"); relay.value = "lab-relay"; await pilot.pause(0.3)
  warning = str(scr.query_one("#setup-warning").render())
  check("an own relay: no public warning, a note", "PUBLIC" not in warning and "Relay lab-relay: a plain OpenSSH jump host" in warning, warning[:100])
  await pilot.click("#setup-create")
  ok = await until(pilot, lambda: isinstance(app.screen, LinkScreen), 5)
  view = app.screen
  link = view.link if ok else None
  ok = ok and await until(pilot, lambda: link.ready() and view.query(".lv-copy"), 20)
  check("Create: the link window; when the relay is ready, the commands with copy buttons", ok and len(view.query(".lv-copy")) >= 4,
        (link.relay_state, link.relay_error) if link else None)
  app.save_screenshot(filename="links_view.svg", path=OUT)
  check("the relay is remembered as a setting", load_config().get("link_relay") == "lab-relay")
  state = rendered(view.query_one("#lv-state").content) if ok else ""
  check("the window shows relay, logins, password, host key", "ready, via lab-relay" in state and "takes logins: once" in state
        and link.password in state and host_fingerprint() in state, state)
  client = LinkClient(link_client_argv(link, known), link.password)
  ok = await client.read_until(pilot, r"\(cluster\)", 30)
  check("the user's ssh gets the login shell of the cluster (over the tab's session)", ok, client.out[-200:])
  client.send("echo $((6*7))\r")
  ok = await client.read_until(pilot, r"42\r\n", 10)
  check("and runs commands there", ok, client.out[-120:])
  await pilot.pause(0.5)
  check("the login is announced", any("shell opened through link" in m for _, m in notes(app)))
  footer = str(app.main.query_one("#connstatus").render())
  check("the footer shows the open link and session", "⇄ 1 link, 1 session open" in footer, footer)
  await pilot.press("escape"); await pilot.pause(0.3)
  await pilot.click("#connstatus"); await pilot.pause(0.8)
  cs = app.screen
  table = cs.query_one("#conn-links") if isinstance(cs, ConnectionScreen) else None
  keys = [r.value for r in table.rows] if table else []
  check("the connection window lists the link and its session", f"link|{link.id}" in keys and any(k.startswith(f"session|{link.id}|") for k in keys), keys)
  session_key = next(k for k in keys if k.startswith("session|"))
  table.focus(); table.move_cursor(row=table.get_row_index(session_key)); await pilot.press("enter"); await pilot.pause(0.4)
  check("clicking a session asks to disconnect it", isinstance(app.screen, ConfirmScreen) and "Disconnect" in app.screen.dialog_title)
  await pilot.click("#confirm-yes")
  code = await client.wait(pilot, 10)
  check("Disconnect ends the user's ssh", code is not None and not link.live_sessions, code)
  table.move_cursor(row=table.get_row_index(f"link|{link.id}")); await pilot.press("enter"); await pilot.pause(0.5)
  check("clicking the link opens its window", isinstance(app.screen, LinkScreen) and app.screen.link is link)
  check("a used one-time link takes no more logins", not link.open() and link.why_closed() == "used")
  await pilot.press("escape"); await pilot.pause(0.2); await pilot.press("escape"); await pilot.pause(0.3)

  # a node of a running job: only a shell
  js = await open_job(app, pilot, "9001")
  node = js.details["nodes"][0] if js.details.get("nodes") else None
  check("job window: the Shell button", js.query_one("#shell").display and node)
  calls = os.path.join(LAB, "srun_calls")
  if os.path.exists(calls): os.unlink(calls)
  await pilot.press("s"); await pilot.pause(0.4)
  check("a node shell asks too, with the node chosen", isinstance(app.screen, ShellChoiceScreen) and app.screen.node == node)
  await pilot.click("#choice-own"); await pilot.pause(0.5)
  scr = app.screen
  check("its setup panel: a shell (no drive options)", isinstance(scr, LinkSetupScreen) and scr.kind == "shell" and not scr.query("#setup-ro")
        and scr.query_one("#setup-relay").value == "lab-relay")
  await pilot.click("#setup-create")
  await until(pilot, lambda: isinstance(app.screen, LinkScreen) and app.screen.link.ready(), 20)
  link = app.screen.link
  client = LinkClient(link_client_argv(link, known), link.password)
  await pilot.pause(2)
  client.send("echo $((7*6)); exit\r")
  ok = await client.read_until(pilot, r"42", 30)
  srun = open(calls).read() if os.path.exists(calls) else ""
  check("the user's ssh gets a shell on the node (srun --overlap --pty in the job)", ok and "--jobid=9001 --overlap --pty" in srun and f"-w {node}" in srun, (client.out[-200:], srun))
  await client.wait(pilot, 10)
  await pilot.press("escape"); await pilot.pause(0.3)
  check("the job window has no drive", not js.query("#cluster-drive"))
  await pilot.press("escape"); await pilot.pause(0.3)

  # the login directory as a drive (read only, so the test never writes into it)
  app.show_view(None); await pilot.pause(0.3)
  await menu(app, pilot, "cluster_drive")
  await until(pilot, lambda: isinstance(app.screen, DriveScreen), 10)
  for _ in range(3): # (the drive window may still be settling: click again)
    await pilot.click("#drive-link") # (the drive on your own machine)
    ok = await until(pilot, lambda: isinstance(app.screen, LinkSetupScreen), 5)
    if ok or not isinstance(app.screen, DriveScreen):
      break
  scr = app.screen
  home = app.cluster.connection.home
  check("Drive: its setup panel, the login directory, read only possible, many logins by default", ok and scr.kind == "drive"
        and scr.path == home and not scr.query_one("#setup-ro").disabled and scr.query_one("#setup-uses").value == 0, (home, type(scr).__name__, [type(s).__name__ for s in app.screen_stack], getattr(scr, "path", None)))
  scr.query_one("#setup-ro").value = True; await pilot.pause(0.2)
  await pilot.click("#setup-create")
  await until(pilot, lambda: isinstance(app.screen, LinkScreen) and app.screen.link.ready(), 20)
  link = app.screen.link
  from slurp.links.windows import winhelper as wh
  check("drive link: the folders for the Windows helper, $HOME (the login directory) first, in its URL",
        link.dirs[:1] == [("HOME", home)] and all(p.startswith("/") for _, p in link.dirs)
        and urllib.parse.quote(f"HOME={home}", safe="") in wh.link_url(link), link.dirs)
  user = app.cluster.connection.user
  check("drive link: its name on Windows comes from user@cluster (n), the cluster's user and the tab's host",
        link.origin == f"{user}@lab" and f"n={urllib.parse.quote(link.origin, safe='')}" in wh.link_url(link), link.origin)
  titles = [t for t, _, _ in client_commands(link)]
  check("drive commands: sshfs, SSHFS-Win (two steps), PuTTY/WinSCP, ssh config", any("sshfs" in t for t in titles)
        and sum("SSHFS-Win" in t for t in titles) == 2 and any("WinSCP" in t for t in titles) and any("config" in t for t in titles), titles)
  sshfs = dict((t, c) for t, c, _ in client_commands(link))["Linux, macOS (sshfs): one command"]
  mnt = os.path.join(HERE, "share", "uimnt"); os.makedirs(mnt, exist_ok=True)
  subprocess.run(["fusermount", "-u", mnt], capture_output=True)
  opts = re.search(r"-o (\S+)", sshfs).group(1) + f",UserKnownHostsFile={known},StrictHostKeyChecking=accept-new,password_stdin"
  def mount():
    errf = os.path.join(HERE, "share", "uisshfs.err")
    with open(errf, "w") as err:
      r = subprocess.run(["sshfs", "-o", opts, f"slurp@localhost:{home}", mnt], input=(link.password + "\n").encode(),
                         stdout=subprocess.DEVNULL, stderr=err, timeout=30)
    return r.returncode, open(errf).read()
  loop = asyncio.get_running_loop()
  code, err = await loop.run_in_executor(None, mount)
  listing = await loop.run_in_executor(None, lambda: sorted(os.listdir(mnt)) if code == 0 else [])
  check("sshfs mounts the login directory of the cluster", code == 0 and listing == sorted(os.listdir(home)), (code, err[-200:], listing[:5]))
  def try_write():
    try:
      open(os.path.join(mnt, ".slurp-ro-test"), "w").write("x"); return True
    except OSError:
      return False
  wrote = await loop.run_in_executor(None, try_write)
  check("read only: writing is refused", code == 0 and not wrote and not os.path.exists(os.path.join(home, ".slurp-ro-test")))
  check("the drive session is listed", any(s.kind == "drive" for s in link.live_sessions))
  # quitting with an open session asks first
  await pilot.press("escape"); await pilot.pause(0.3)
  await app.action_quit(); await pilot.pause(0.4)
  check("quitting with open sessions asks first", isinstance(app.screen, ConfirmScreen) and "Quit slurp" in app.screen.dialog_title)
  await pilot.click("#confirm-no"); await pilot.pause(0.3)
  await loop.run_in_executor(None, lambda: subprocess.run(["fusermount", "-u", mnt], capture_output=True))
  await until(pilot, lambda: not link.live_sessions, 10)
  app.link_manager.revoke(link)
  for l in app.link_manager.links: app.link_manager.revoke(l)
  await until(pilot, lambda: all(l.relay_state == "closed" for l in app.link_manager.links), 15)
  check("revoked links close their relay tunnels", all(l.relay_state == "closed" for l in app.link_manager.links),
        [l.relay_state for l in app.link_manager.links])
  update_config(link_relay=None)

async def t_drive(app, pilot):
  app.show_view(None); await pilot.pause(0.2)
  cluster = app.cluster
  home = cluster.connection.home
  mnt = os.path.join(HERE, "share", "drivemnt"); subprocess.run(["fusermount", "-u", mnt], capture_output=True)
  shutil.rmtree(mnt, ignore_errors=True)
  await pilot.press("F")
  ok = await until(pilot, lambda: isinstance(app.screen, DriveScreen), 10)
  scr = app.screen
  check("Drive asks where: this machine (path, read only) or your own machine (a link)", ok and scr.query_one("#drive-path").value == os.path.expanduser("~/mnt/lab")
        and not scr.query_one("#drive-mount").disabled and not scr.query_one("#drive-link").disabled, scr.query_one("#drive-path").value if ok else None)
  scr.query_one("#drive-path").value = mnt
  scr.query_one("#drive-ro").value = True; await pilot.pause(0.2)
  button = scr.query_one("#drive-mount")
  check("Mount is there and can be pressed", button.display and not button.disabled)
  button.press() # (not a click: a notification over the button can take a click at that place)
  ok = await until(pilot, lambda: cluster.drive is not None and cluster.drive.state == "mounted", 20) # (the drive is made by the press's handler)
  drive = cluster.drive
  listing = await asyncio.get_running_loop().run_in_executor(None, lambda: sorted(os.listdir(mnt)) if ok else [])
  check("Mount here: sshfs through the tab's session, the login directory at the path", ok and listing == sorted(os.listdir(home)),
        (drive.state if drive else None, drive.error if drive else None, drive.messages if drive else None))
  check("the path and read only are remembered for the tab", (load_config().get("drive_mounts") or {}).get("lab") == {"path": mnt, "read_only": True})
  await until(pilot, lambda: "mounted at" in str(scr.query_one("#drive-state").render()), 5) # (the dialog refreshes on a timer)
  state = str(scr.query_one("#drive-state").render())
  check("the dialog shows the state, Unmount instead of Mount", "mounted at" in state and scr.query_one("#drive-unmount").display
        and not scr.query_one("#drive-mount").display, state)
  def write():
    try: open(os.path.join(mnt, ".slurp-drive-test"), "w").write("x"); return True
    except OSError: return False
  check("read only: writing is refused", not await asyncio.get_running_loop().run_in_executor(None, write))
  footer = str(app.main.query_one("#connstatus").render())
  check("the footer shows the mounted drive", f"⛁ {mnt}" in footer, footer)
  await pilot.press("escape"); await pilot.pause(0.3)
  await pilot.click("#connstatus"); await pilot.pause(0.8)
  cs = app.screen
  info = rendered(cs.query_one("#conn-drive").content) if isinstance(cs, ConnectionScreen) else ""
  check("the connection window: the drive, its state and details", "mounted at" in info and f"lab:{home}" in info and "sshfs" in info, info)
  buttons = {b: cs.query_one(f"#conn-drive-{b}").display for b in ("mount", "unmount", "remount", "force")}
  check("…with Unmount, Remount, Force unmount (no Mount)", buttons == {"mount": False, "unmount": True, "remount": True, "force": True}, buttons)
  pid = drive.proc.pid
  await pilot.click("#conn-drive-remount")
  ok = await until(pilot, lambda: drive.state == "mounted" and drive.proc is not None and drive.proc.pid != pid, 20)
  check("Remount: unmounted and mounted again (a new sshfs)", ok, drive.state)
  # the tab's session breaks: the drive stops answering; once the session is back, sshfs reconnects
  app.disconnect(cluster); await pilot.pause(0.5)
  ok = await until(pilot, lambda: drive.state in ("broken", "failed"), 20)
  check("the session of the tab ends: the drive is shown as not answering", ok, drive.state)
  while not isinstance(app.screen, MainScreen) and len(app.screen_stack) > 1:
    await pilot.press("escape"); await pilot.pause(0.2)
  app.screen.query_one("#jobs").focus()
  await pilot.click("#connstatus"); await pilot.pause(0.5)
  await pilot.click("#conn-reconnect")
  await until(pilot, lambda: "passphrase" in term(app), 15)
  await pilot.press(*"pw123", "enter")
  await until(pilot, lambda: cluster.connection and cluster.connection.ready, 30)
  ok = await until(pilot, lambda: drive.state == "mounted", 40)
  listing = await asyncio.get_running_loop().run_in_executor(None, lambda: sorted(os.listdir(mnt)) if ok else [])
  check("…and answers again after the reconnect (sshfs reconnects through the new session)", ok and listing == sorted(os.listdir(home)), drive.state)
  import signal as _signal
  pid = drive.proc.pid
  os.kill(pid, _signal.SIGKILL) # sshfs dies (the mount point is left dead)
  ok = await until(pilot, lambda: drive.state == "reconnecting", 10)
  check("sshfs dies: the drive is lost, slurp says it mounts it again", ok and any("lost its connection" in m for _, m in notes(app)), drive.state)
  ok = await until(pilot, lambda: drive.state == "mounted" and drive.proc is not None and drive.proc.pid != pid, 30)
  listing = await asyncio.get_running_loop().run_in_executor(None, lambda: sorted(os.listdir(mnt)) if ok else [])
  check("…the dead mount point is cleared and it is mounted again", ok and listing == sorted(os.listdir(home)), drive.state)
  app.show_view(None); await pilot.pause(0.3)
  while not isinstance(app.screen, MainScreen) and len(app.screen_stack) > 1:
    await pilot.press("escape"); await pilot.pause(0.2)
  await app.action_quit(); await pilot.pause(0.4)
  check("quitting with a mounted drive asks first, saying it is unmounted", isinstance(app.screen, ConfirmScreen)
        and f"The drive at {mnt} is unmounted" in app.screen.dialog_message, getattr(app.screen, "dialog_message", ""))
  await pilot.click("#confirm-no"); await pilot.pause(0.3)
  await pilot.click("#connstatus"); await pilot.pause(0.8)
  await pilot.click("#conn-drive-unmount")
  ok = await until(pilot, lambda: drive.state == "unmounted" and drive.proc is None, 15)
  check("Unmount: unmounted, sshfs ended", ok and not os.path.ismount(mnt), drive.state)
  await pilot.pause(0.8)
  cs = app.screen
  check("…then Mount… is offered again", isinstance(cs, ConnectionScreen) and cs.query_one("#conn-drive-mount").display)
  await pilot.press("escape"); await pilot.pause(0.3)
  # a path that is not empty is refused
  busy = os.path.join(HERE, "share", "drivebusy"); os.makedirs(busy, exist_ok=True); open(os.path.join(busy, "x"), "w").write("x")
  app.mount_drive(cluster, busy, False); await pilot.pause(1)
  check("a directory that is not empty is refused", drive.state != "mounted" and any("not empty" in m for _, m in notes(app)))
  # another directory of the cluster: chosen from the places, or a path typed in
  sub = os.path.join(home, "drive-sub"); os.makedirs(sub, exist_ok=True); open(os.path.join(sub, "only-here.txt"), "w").write("x")
  while not isinstance(app.screen, MainScreen) and len(app.screen_stack) > 1:
    await pilot.press("escape"); await pilot.pause(0.2)
  app.screen.query_one("#jobs").focus(); await pilot.press("F")
  ok = await until(pilot, lambda: isinstance(app.screen, DriveScreen) and app.screen.places, 30)
  scr = app.screen
  options = [label for label, _ in scr.places]
  check("Directory: the places of the cluster, home first and chosen", ok and options[0] == "Home"
        and scr.query_one("#drive-place").value == home and scr.query_one("#drive-remote").value == home, (options, scr.query_one("#drive-place").value))
  if len(scr.places) > 1:
    label, path = scr.places[1]
    scr.query_one("#drive-place").value = path; await pilot.pause(0.2)
    check(f"choosing {label}: its path in the field", scr.query_one("#drive-remote").value == path)
  scr.query_one("#drive-remote").value = "drive-sub"; await pilot.pause(0.2)
  check("a path typed in: the list says Other", scr.query_one("#drive-place").value == OTHER_PLACE)
  scr.query_one("#drive-path").value = mnt; scr.query_one("#drive-ro").value = True
  scr.query_one("#drive-mount").press()
  ok = await until(pilot, lambda: drive.state == "mounted", 20)
  listing = await asyncio.get_running_loop().run_in_executor(None, lambda: sorted(os.listdir(mnt)) if ok else [])
  check("Mount here: that directory (relative: in home) at the path, not the login directory", ok and listing == ["only-here.txt"]
        and drive.source == f"lab:{sub}", (drive.state, listing, drive.source))
  check("…remembered for the tab", (load_config().get("drive_mounts") or {}).get("lab", {}).get("remote") == sub)
  await until(pilot, lambda: scr.query_one("#drive-remote").disabled, 5)
  check("while mounted the directory is fixed (Unmount first)", scr.query_one("#drive-remote").disabled and scr.query_one("#drive-place").disabled)
  scr.query_one("#drive-unmount").press()
  await until(pilot, lambda: drive.state == "unmounted", 15)
  scr.query_one("#drive-remote").value = "no-such-dir"; await pilot.pause(0.6)
  scr.query_one("#drive-mount").press(); await pilot.pause(1.5)
  check("a directory that does not exist on the cluster: said so, nothing mounted", drive.state != "mounted"
        and any("no directory on lab" in m for _, m in notes(app)))
  await pilot.press("escape"); await pilot.pause(0.3)
  update_config(drive_mounts=None)


async def t_dir_permissions(app, pilot):
  """Open a directory… over SSH: the places of the cluster; the directory view lists permissions, owners, ACLs and
  what the user cannot do, found on the cluster (one find, getfacl)"""
  home = app.cluster.connection.home
  d = os.path.join(home, "perms"); shutil.rmtree(d, ignore_errors=True); os.makedirs(os.path.join(d, "closed"))
  for name in ("mine", "secret", "readonly", "shared"):
    with open(os.path.join(d, name), "w") as f: f.write("x")
  os.chmod(os.path.join(d, "secret"), 0); os.chmod(os.path.join(d, "readonly"), 0o444); os.chmod(os.path.join(d, "closed"), 0)
  acl = shutil.which("setfacl") and subprocess.run(["setfacl", "-m", "u:root:r--", os.path.join(d, "shared")]).returncode == 0
  try:
    app.action_login_dir()
    ok = await until(pilot, lambda: isinstance(app.screen, OpenDirScreen) and app.cluster.places is not None, 30)
    check("the dialog: the places of the cluster, home first", ok and app.screen.places[0] == ("Home", home), app.screen.places)
    path = app.screen.query_one("#opendir-path"); path.value = "perms"; path.focus(); await pilot.press("enter")
    ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.path == d and app.screen.entries, 10)
    table = app.screen.query_one("#dir")
    row = lambda name: [str(c) for c in table.get_row(name)]
    check("a relative path: in home, in a subtab", ok and app.screen.query_one("#dpath").value == d)
    if os.geteuid() != 0:
      check("over SSH: permissions as ls -l, what the user cannot do", ok and row("readonly")[3] == "-r--r--r--"
            and row("readonly")[5] == "no write" and row("secret")[5] == "no read no write"
            and row("closed")[5] == "no read no write" and row("mine")[5] == "", [row(n) for n in ("readonly", "secret", "mine")])
    if acl:
      check("over SSH: the extended ACL, + after the permissions", row("shared")[3].endswith("+") and "u:root:r--" in row("shared")[6],
            row("shared"))
    # the search in the subdirectories, on the cluster (batches of find over SSH)
    os.makedirs(os.path.join(d, "deep", "er", "est"), exist_ok=True)
    with open(os.path.join(d, "deep", "er", "est", "needle.dat"), "w") as f: f.write("x")
    ds = app.screen
    await pilot.press("slash", *"needle"); await pilot.press("ctrl+r")
    ok = await until(pilot, lambda: ds.searching is None and ds.results, 20)
    check("over SSH: the search in the subdirectories finds deep/er/est/needle.dat (and not into closed/)",
          ok and [e["name"] for e in ds.results] == ["deep/er/est/needle.dat"], [e["name"] for e in ds.results or []])
    await pilot.press("escape"); await pilot.pause(0.3)
  finally:
    os.chmod(os.path.join(d, "closed"), 0o700); os.chmod(os.path.join(d, "secret"), 0o600)


async def t_ssh_setup(app, pilot):
  """+: a destination of ~/.ssh/config, or the full SSH settings; Check says precisely what works and what not (against
  the lab's sshd); the tab is added checked or not and logs in with those settings; the connection window checks too"""
  import re as _re
  from slurp.cluster import sshsetup
  from slurp.ui.screens.dialogs import AddClusterScreen
  loop = asyncio.get_running_loop()
  port = _re.search(r"Port (\d+)", open(os.path.join(LAB, "ssh_config")).read())[1]
  user, relay, userkey = getpass.getuser(), os.path.join(HERE, "share", "relaykey"), os.path.join(LAB, "userkey")
  known = [f"UserKnownHostsFile={os.path.join(LAB, 'known_hosts')}"]
  run = lambda dest, settings: loop.run_in_executor(None, lambda: sshsetup.check(dest, settings, timeout=20))
  r = await run("lab", None)
  check("check of a Host of ssh_config with a key that has a passphrase: works, the login asks for it",
        r.verdict == "warn" and "passphrase" in r.summary and "debug1:" in r.log, (r.verdict, r.summary, [f.text for f in r.findings]))
  full = {"host": "127.0.0.1", "port": port, "user": user, "identities": [relay], "options": known}
  r = await run("", full)
  check("full settings, a key without passphrase: the login works", r.verdict == "ok" and "the login works" in r.summary
        and any(f.text.startswith("port " + port + " answers: SSH-") for f in r.findings), (r.verdict, r.summary, [f.text for f in r.findings]))
  r = await run("", {"host": "127.0.0.1", "port": port, "user": user, "identities": [userkey],
                     "options": [f"UserKnownHostsFile={os.path.join(HERE, 'no-known-hosts')}"]})
  texts = [f.text for f in r.findings]
  check("an unknown host key: said (with its fingerprint), and the rest is checked (nothing written)", r.verdict == "warn"
        and any("not known yet" in t and "SHA256:" in t for t in texts) and "passphrase" in r.summary
        and not os.path.exists(os.path.join(HERE, "no-known-hosts")), (r.summary, texts))
  r = await run("", {**full, "user": "nobody-here"})
  check("a user the server does not take: the keys are not accepted", r.verdict == "error" and "does not accept your keys" in r.summary, r.summary)
  r = await run("", {**full, "port": "1"})
  check("a wrong port: refused", r.verdict == "error" and any("refuses connections on port 1" in t.text for t in r.findings), r.summary)
  # the dialog: the full settings, Check, Add
  while not isinstance(app.screen, MainScreen) and len(app.screen_stack) > 1:
    await pilot.press("escape"); await pilot.pause(0.2)
  await pilot.click("#cluster-add")
  ok = await until(pilot, lambda: isinstance(app.screen, AddClusterScreen), 5)
  dlg = app.screen
  check("+: the destination first (a Host of ~/.ssh/config, encouraged), the full settings folded", ok and app.focused is dlg.query_one("#cluster-destination")
        and dlg.query_one("#cluster-full").collapsed and "~/.ssh/config" in str(dlg.query_one("#cluster-hint").render()))
  dlg.query_one("#cluster-full").collapsed = False; await pilot.pause(0.2)
  for key, value in (("host", "127.0.0.1"), ("port", port), ("user", user), ("identities", relay)):
    dlg.query_one(f"#ssh-{key}").value = value
  dlg.query_one("#ssh-options").load_text("\n".join(known + ["ServerAliveInterval=5"]))
  from slurp.ui.screens.dialogs import SshCheckScreen
  dlg.query_one("#cluster-check-button").press()
  ok = await until(pilot, lambda: isinstance(app.screen, SshCheckScreen) and "works" in check_term(app.screen), 30)
  text = check_term(app.screen) if ok else ""
  check("Check…: a window with a terminal, as the login goes: what needs no login, the login, the verdict, ssh's log",
        ok and "1. what needs no login" in text and f"port {port} answers" in text and "slurp-check: logged in" in text
        and "✔ works: the login works" in text and "ssh -v" in text, text[-600:])
  await until(pilot, lambda: app.screen.session.done, 10); await pilot.pause(0.5)
  check("…done: the keys back to the window (Close has the focus), Esc closes", app.focused is app.screen.query_one("#sc-close"))
  await pilot.press("escape"); await pilot.pause(0.3)
  check("…back in the dialog, its buttons there", app.screen is dlg and dlg.query_one("#cluster-confirm").region.height > 0)
  n = len(app.clusters)
  calls = os.path.join(LAB, "ssh_calls.log")
  before = len(open(calls).read().splitlines()) if os.path.exists(calls) else 0
  dlg.query_one("#cluster-confirm").press()
  ok = await until(pilot, lambda: len(app.clusters) == n + 1 and app.cluster.connection is not None and app.cluster.connection.ready, 30)
  cluster = app.cluster
  new = open(calls).read().splitlines()[before:] if os.path.exists(calls) else []
  master = next((l for l in new if " -M " in l), "")
  check("Add: a tab named user@host:port, it logs in with the settings (-l, -p, -i, IdentitiesOnly, the options)",
        ok and cluster.name == f"{user}@127.0.0.1:{port}" and f"-p {port}" in master and f"-i {relay}" in master
        and "IdentitiesOnly=yes" in master and "ServerAliveInterval=5" in master and master.rstrip().endswith("127.0.0.1 exec /bin/sh"),
        (cluster.name, master))
  tabs = load_config().get("tabs") or []
  check("…kept for the next start (its settings and name)", any(t.get("ssh", {}).get("port") == port and t.get("name") == cluster.name for t in tabs), tabs)
  app.action_connection(); await pilot.pause(0.8)
  cs = app.screen
  info = rendered(cs.query_one("#conn-info").content) if isinstance(cs, ConnectionScreen) else ""
  check("the connection window shows the SSH settings", f"-p {port}" in info and "SSH settings" in info, info[:400])
  cs.query_one("#conn-check-button").press()
  ok = await until(pilot, lambda: isinstance(app.screen, SshCheckScreen) and "works" in check_term(app.screen), 30)
  check("…and Check… there: the same check in a terminal", ok and "1. what needs no login" in check_term(app.screen))
  await until(pilot, lambda: app.screen.session.done, 10); await pilot.pause(0.5)
  await pilot.press("escape"); await pilot.pause(0.3)
  await pilot.press("escape"); await pilot.pause(0.3)
  # a tab can be added unchecked, also one that does not work
  await pilot.click("#cluster-add"); await until(pilot, lambda: isinstance(app.screen, AddClusterScreen), 5)
  app.screen.query_one("#cluster-destination").value = "no-such-host.invalid"
  app.screen.query_one("#cluster-confirm").press()
  ok = await until(pilot, lambda: len(app.clusters) == n + 2, 10)
  check("Add without Check (even one that does not work): the tab is there", ok and app.clusters[-1].destination == "no-such-host.invalid")
  for c in list(app.clusters[n:]):
    app.close_cluster(c); await pilot.pause(0.3)


async def t_ssh_edit(app, pilot):
  """The overview's SSH settings…: the tab's destination (and what ~/.ssh/config and ssh -G make of it); changed to
  full settings and saved, the next login (Reconnect) uses them; and back"""
  import re as _re
  from slurp.ui.screens.dialogs import AddClusterScreen
  from slurp.ui.screens.overview import OverviewScreen
  cluster = app.cluster
  port = _re.search(r"Port (\d+)", open(os.path.join(LAB, "ssh_config")).read())[1]
  relay = os.path.join(HERE, "share", "relaykey")
  app.show_home(); await pilot.pause(0.3)
  for v in list(cluster.views): app.close_view(v)
  cluster.jobs_open = False; app.show_home(); await pilot.pause(0.5)
  ok = await until(pilot, lambda: isinstance(app.screen, OverviewScreen) and app.screen.query_one("#ov-config-row").display, 10)
  check("the overview of an SSH tab: SSH settings…", ok)
  app.screen.query_one("#ov-config").press()
  ok = await until(pilot, lambda: isinstance(app.screen, AddClusterScreen) and "What ssh makes of it all" in str(app.screen.query_one("#cluster-config").render()), 15)
  dlg = app.screen
  config = str(dlg.query_one("#cluster-config").render()) if ok else ""
  check("its settings: the destination filled in, what ssh makes of it (user@host port), Save",
        ok and dlg.query_one("#cluster-destination").value == "lab" and f"@127.0.0.1 port {port}" in config
        and str(dlg.query_one("#cluster-confirm").label) == "Save", config)
  dlg.query_one("#cluster-full").collapsed = False; await pilot.pause(0.2)
  for key, value in (("host", "127.0.0.1"), ("port", port), ("user", getpass.getuser()), ("identities", relay), ("name", "lab-full")):
    dlg.query_one(f"#ssh-{key}").value = value
  dlg.query_one("#ssh-options").load_text(f"UserKnownHostsFile={os.path.join(LAB, 'known_hosts')}")
  dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.5)
  check("Save: the tab takes them (its name, its settings), kept; the session at its next login", cluster.name == "lab-full"
        and cluster.ssh and cluster.ssh["port"] == port and "-p" in cluster.connection.ssh_options
        and any(t.get("name") == "lab-full" for t in load_config().get("tabs") or []), (cluster.name, cluster.ssh))
  calls = os.path.join(LAB, "ssh_calls.log")
  before = len(open(calls).read().splitlines())
  app.reconnect(cluster)
  if not cluster.connection.ready:
    app.connect_tab()
  ok = await until(pilot, lambda: cluster.connection and cluster.connection.ready and len(open(calls).read().splitlines()) > before, 30)
  master = next((l for l in open(calls).read().splitlines()[before:] if " -M " in l), "")
  check("Reconnect: the login with the full settings", ok and f"-p {port}" in master and f"-i {relay}" in master, master)
  # back to the Host of ssh_config
  app.edit_ssh_settings(cluster); await until(pilot, lambda: isinstance(app.screen, AddClusterScreen), 5)
  dlg = app.screen
  for key in ("host", "port", "user", "identities", "name"):
    dlg.query_one(f"#ssh-{key}").value = ""
  dlg.query_one("#ssh-options").load_text("")
  dlg.query_one("#cluster-destination").value = "lab"
  dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.5)
  check("back to a Host of ~/.ssh/config: no full settings", cluster.name == "lab" and cluster.destination == "lab" and not cluster.ssh
        and cluster.connection.ssh_options == [], (cluster.name, cluster.ssh))
  app.disconnect(cluster); await pilot.pause(0.5); app.connect_tab()
  ok = await until(pilot, lambda: "passphrase" in term(app), 15)
  await pilot.press(*"pw123", "enter")
  ok = await until(pilot, lambda: cluster.connection.ready, 30)
  check("…and it logs in as before", ok)


async def t_pre_login(app, pilot):
  """Run before login: a command in the login pane before every login (e.g. one that signs a key), the user answers
  it there; when it ends, the SSH login as usual"""
  from slurp.ui.screens.dialogs import AddClusterScreen
  cluster = app.cluster
  signed = os.path.join(HERE, "signed-key")
  if os.path.exists(signed): os.unlink(signed)
  command = f"printf 'one-time code: '; read code; echo \"signed with $code\" > {signed}; exit 3"
  app.edit_ssh_settings(cluster); await until(pilot, lambda: isinstance(app.screen, AddClusterScreen), 5)
  dlg = app.screen
  dlg.query_one("#ssh-pre-login").value = command
  dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.5)
  check("saved with the tab", cluster.pre_login == command and cluster.connection.pre_login == command
        and any(t.get("pre_login") == command for t in load_config().get("tabs") or []), cluster.pre_login)
  app.disconnect(cluster); await pilot.pause(0.5)
  app.connect_tab()
  ok = await until(pilot, lambda: "one-time code:" in term(app), 15)
  check("the login pane runs the command first, and it asks there", ok and "(run before the login)" in term(app), term(app)[-300:])
  await pilot.press(*"42", "enter")
  ok = await until(pilot, lambda: "passphrase" in term(app), 15)
  check("it ends (said, with its exit code), then the SSH login as usual", ok and "ended with 3; the SSH login follows" in term(app)
        and open(signed).read().strip() == "signed with 42", term(app)[-400:])
  await pilot.press(*"pw123", "enter")
  ok = await until(pilot, lambda: cluster.connection.ready, 30)
  check("…and it logs in", ok)
  app.action_connection(); await pilot.pause(0.8)
  info = rendered(app.screen.query_one("#conn-info").content) if isinstance(app.screen, ConnectionScreen) else ""
  check("the connection window says what runs before the login", "Before the login" in info and "one-time code" in info, info[:300])
  from slurp.ui.screens.dialogs import SshCheckScreen
  app.screen.query_one("#conn-check-button").press()
  ok = await until(pilot, lambda: isinstance(app.screen, SshCheckScreen) and "one-time code:" in check_term(app.screen), 30)
  check("Check…: the command before the login runs in its terminal and asks", ok, check_term(app.screen)[-300:] if ok else "")
  await pilot.press(*"77", "enter")
  ok = await until(pilot, lambda: "passphrase" in check_term(app.screen), 20)
  check("…then the login asks for the passphrase there", ok and open(signed).read().strip() == "signed with 77", check_term(app.screen)[-400:])
  await pilot.press(*"pw123", "enter")
  ok = await until(pilot, lambda: "works" in check_term(app.screen), 30)
  check("…the answer taken: it works", ok and "slurp-check: logged in" in check_term(app.screen), check_term(app.screen)[-400:])
  await until(pilot, lambda: app.screen.session.done, 10); await pilot.pause(0.5)
  await pilot.press("escape"); await pilot.pause(0.3)
  await pilot.press("escape"); await pilot.pause(0.3)
  # only when the login fails without it (the lab's key needs its passphrase: a login that asks nothing fails); what
  # it exports is there for the login
  os.unlink(signed)
  command2 = f"printf 'one-time code: '; read code; echo \"signed with $code\" > {signed}; export SLURP_SIGNED=yes"
  app.edit_ssh_settings(cluster); await until(pilot, lambda: isinstance(app.screen, AddClusterScreen), 5)
  app.screen.query_one("#ssh-pre-login").value = command2
  app.screen.query_one("#ssh-pre-login-fallback").value = True
  app.screen.query_one("#cluster-confirm").press(); await pilot.pause(0.5)
  check("the box: saved with the tab", cluster.pre_login_fallback and cluster.connection.pre_login_fallback
        and any(t.get("pre_login_fallback") for t in load_config().get("tabs") or []))
  app.disconnect(cluster); await pilot.pause(0.5)
  calls = os.path.join(LAB, "ssh_calls.log")
  before = len(open(calls).read().splitlines())
  app.connect_tab()
  ok = await until(pilot, lambda: "one-time code:" in term(app), 20)
  masters = [l for l in open(calls).read().splitlines()[before:] if " -M " in l]
  check("first a login without it that asks nothing (it failed), then the command", ok and masters and "BatchMode=yes" in masters[0]
        and "BatchMode=yes" not in cluster.connection.argv and not os.path.exists(signed), (masters, term(app)[-300:]))
  await pilot.press(*"43", "enter")
  ok = await until(pilot, lambda: "passphrase" in term(app), 15)
  check("…what it exported is there for the login", ok and "it set SLURP_SIGNED for the login" in term(app), term(app)[-400:])
  await pilot.press(*"pw123", "enter")
  ok = await until(pilot, lambda: cluster.connection.ready, 30)
  check("…and it logs in", ok and open(signed).read().strip() == "signed with 43")
  check("logged in: the next login tries without it first again", not cluster.connection.pre_login_due)
  app.edit_ssh_settings(cluster); await until(pilot, lambda: isinstance(app.screen, AddClusterScreen), 5)
  check("the dialog shows the box", app.screen.query_one("#ssh-pre-login-fallback").value)
  app.screen.query_one("#ssh-pre-login").value = ""
  app.screen.query_one("#cluster-confirm").press(); await pilot.pause(0.5)
  check("emptied: no command any more", cluster.pre_login == "" and cluster.connection.pre_login == "" and not cluster.pre_login_fallback)


# ---- transfers: the lab's "cluster" is this machine, its login scripts print text (the junk) on every channel
async def t_transfer_s3(app, pilot):
  """S3 with the lab's tab: rclone on the cluster (the lab is this machine, with an S3 server in the test): refused
  without the user's agreement to send the keys; detached up into the bucket, then as a transfer job down from it; the
  keys file is gone after each run; a run again copies nothing"""
  import glob
  import urllib.request
  from moto.server import ThreadedMotoServer
  import boto3
  from slurp.transfer import endpoints as E, model as M, secrets as S
  server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
  server.start()
  try:
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    urllib.request.urlopen(urllib.request.Request(url + "/moto-api/reset", method="POST")).read()
    client = boto3.client("s3", endpoint_url=url, aws_access_key_id="AK", aws_secret_access_key="SK", region_name="us-east-1")
    client.create_bucket(Bucket="lab")
    store = app.transfers.endpoint_store
    store.add(E.EndpointDef("s3lab", "lab bucket", "s3", {"provider": "Other", "endpoint": url, "region": "us-east-1", "bucket": "lab"},
                            ["access_key", "secret_key"]))
    S.put(S.user("s3lab", "access_key"), "AK")
    S.put(S.user("s3lab", "secret_key"), "SK")
    lab = next(c for c in app.clusters if c.destination)
    ref = M.tab_ref(lab)
    src, dst = xfer_tree("s3")
    check("a tab and S3: rclone on the cluster", E.rclone_routes(store, {"endpoint": E.tab_id(lab)}, {"endpoint": "s3lab"}) == ["rclone-cluster"])
    up = M.Transfer(M.new_id(), "up to S3", {"endpoint": E.tab_id(lab), "tab": ref, "paths": [src]},
                    {"endpoint": "s3lab", "name": "lab bucket", "path": "/up"}, route="rclone-cluster", runs_on="detached")
    app.transfers.add(up)
    plan = app.transfers.rclone_check(up, lambda p: None)
    check("the check: rclone on the lab, the bucket written", plan["routes"]["rclone-cluster"]["ok"] and "on lab" in plan["steps"]["rclone"]["text"]
          and "can be written" in plan["steps"]["rclone"]["text"], plan)
    up.plan = plan
    app.transfers.run_now(up)
    check("without the agreement to send the keys: refused", up.state == "failed" and "keys" in up.message, up.message)
    up.secrets_to = {lab.destination: ["s3lab"]}
    app.transfers.run_now(up)
    check("detached: done", await until(pilot, lambda: up.state == "done", 90), (up.state, up.message, up.log[-4:]))
    found = sorted(o["Key"] for o in client.list_objects_v2(Bucket="lab").get("Contents", []))
    check("in the bucket", found == ["up/src/b.txt", "up/src/sub/a.bin"], found)
    folder = up.runs[-1]["folder"]
    check("the keys file is gone", folder and not os.path.exists(os.path.join(folder, "keys")), folder)
    check("no key in the run's folder", not any("SK" in open(f, errors="replace").read().replace("SKIP", "")
                                                for f in glob.glob(os.path.join(folder, "*")) if os.path.isfile(f)))
    check("its progress was rclone's (bytes)", up.runs[-1]["bytes"] >= 400_000, up.runs[-1])
    down = M.Transfer(M.new_id(), "down from S3", {"endpoint": "s3lab", "name": "lab bucket", "paths": ["/up/src"]},
                      {"endpoint": E.tab_id(lab), "tab": ref, "path": dst}, route="rclone-cluster", runs_on="job",
                      options={"time": "1:00:00"}, secrets_to={lab.destination: ["s3lab"]})
    down.plan = {"routes": {"rclone-cluster": {"ok": True}}}
    app.transfers.add(down)
    app.transfers.run_now(down)
    check("as a transfer job: done", await until(pilot, lambda: down.state == "done", 120), (down.state, down.message, down.log[-4:]))
    check("the files", same_tree(src, os.path.join(dst, "src")))
    check("…its keys gone too", not os.path.exists(os.path.join(down.runs[-1]["folder"], "keys")))
    app.transfers.run_now(down)
    check("a run again: done", await until(pilot, lambda: down.state == "done" and len(down.runs) == 2, 120), (down.state, down.message))
    for t in (up, down):
      app.transfers.delete(t)
  finally:
    server.stop()


async def t_transfer_webdav(app, pilot):
  """WebDAV with the lab's tab: rclone on the cluster (the lab: this machine), the password sent for the run (obscured
  in the keys file, deleted when the run starts): detached up into the WebDAV folder; no password left"""
  import glob
  import socket
  import subprocess
  from slurp.transfer import endpoints as E, model as M, secrets as S
  with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
  root = os.path.join(HERE, "davroot")
  os.makedirs(root, exist_ok=True)
  server = subprocess.Popen(["rclone", "--config", "", "serve", "webdav", root, "--addr", f"127.0.0.1:{port}", "--user", "dav",
                             "--pass", "pw789"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  try:
    def listening():
      try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
      except OSError:
        return False
    await until(pilot, listening, 10)
    store = app.transfers.endpoint_store
    store.add(E.EndpointDef("davlab", "lab dav", "webdav", {"url": f"http://127.0.0.1:{port}", "user": "dav"}, ["password"]))
    S.put(S.user("davlab", "password"), "pw789")
    lab = next(c for c in app.clusters if c.destination)
    ref = M.tab_ref(lab)
    src, _ = xfer_tree("dav")
    t = M.Transfer(M.new_id(), "up to WebDAV", {"endpoint": E.tab_id(lab), "tab": ref, "paths": [src]},
                   {"endpoint": "davlab", "name": "lab dav", "path": "/up"}, route="rclone-cluster", runs_on="detached",
                   secrets_to={lab.destination: ["davlab"]})
    app.transfers.add(t)
    plan = app.transfers.rclone_check(t, lambda p: None)
    check("WebDAV: the check passed (the folder made at the run)", plan["routes"]["rclone-cluster"]["ok"] and "is made at the run" in plan["steps"]["rclone"]["text"], plan)
    t.plan = plan
    app.transfers.run_now(t)
    check("WebDAV: detached on the cluster: done", await until(pilot, lambda: t.state == "done", 90), (t.state, t.message, t.log[-4:]))
    check("WebDAV: the files there", same_tree(src, os.path.join(root, "up", "src")))
    folder = t.runs[-1]["folder"]
    check("WebDAV: no password left in the run's folder", not os.path.exists(os.path.join(folder, "keys"))
          and not any("pw789" in open(f, errors="replace").read() for f in glob.glob(os.path.join(folder, "*")) if os.path.isfile(f)))
    app.transfers.delete(t)
  finally:
    server.kill()


def xfer_tree(name, big=0):
  """A source tree in the lab (the cluster) and an empty destination folder -> (source, destination)"""
  src, dst = os.path.join(HERE, "xfer", name, "src"), os.path.join(HERE, "xfer", name, "dst")
  os.makedirs(os.path.join(src, "sub"), exist_ok=True)
  os.makedirs(dst, exist_ok=True)
  with open(os.path.join(src, "sub", "a.bin"), "wb") as f:
    f.write(os.urandom(400_000))
  with open(os.path.join(src, "b.txt"), "w") as f:
    f.write("hello")
  if big:
    with open(os.path.join(src, "big.bin"), "wb") as f:
      f.write(os.urandom(big))
  return src, dst

def same_tree(a, b):
  for root, _, files in os.walk(a):
    for name in files:
      p = os.path.join(root, name)
      q = os.path.join(b, os.path.relpath(p, a))
      if not os.path.exists(q) or open(p, "rb").read() != open(q, "rb").read():
        return False
  return True

async def t_transfer_wizard(app, pilot):
  """lab → this machine with the wizard: the check over SSH, the relay chosen, Go, done; Run now copies nothing"""
  from slurp.ui.screens.transfers import TransferScreen, TransferWizard
  src, dst = xfer_tree("wizard")
  lab = app.cluster
  await pilot.press("T"); await pilot.pause(0.4)
  w = app.screen
  check("T: the wizard, from the tab shown", isinstance(w, TransferWizard) and w.src is lab)
  w.query_one("#tw-src-path").value = src
  await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.2)
  check("what the source holds, counted on the cluster", await until(pilot, lambda: "2 files" in str(w.query_one("#tw-src-paths").render()), 30),
        str(w.query_one("#tw-src-paths").render()))
  w.query_one("#tw-next").press(); await pilot.pause(0.3)
  w.query_one("#tw-dst-tab").value = "local"; await pilot.pause(0.3) # (this machine)
  w.query_one("#tw-dst-path").value = dst; await pilot.pause(0.2)
  w.query_one("#tw-next").press(); await pilot.pause(0.3) # (the options: as they are)
  w.query_one("#tw-next").press() # (the route)
  check("the check over SSH ends", await until(pilot, lambda: w.plan.get("done"), 90), w.plan.get("steps"))
  steps = str(w.query_one("#tw-check-steps").render())
  check("rsync on both, what remains", "rsync 3" in steps and "in 2 files to copy" in steps, steps)
  await pilot.pause(0.6)
  relay_label = str(w.query_one(".tw-r-relay").label)
  check("the relay passed, with its speed", relay_label.startswith("✓ relay") and "/s" in relay_label, relay_label)
  check("a direct route needs a login by itself: not from this machine", not w.plan["routes"]["direct-pull"]["ok"])
  await pilot.pause(0.6)
  await pilot.click(w.query_one(".tw-r-relay")); await pilot.pause(0.4)
  check("the relay: in slurp", w.chosen_route() == "relay" and w.chosen_runs_on() == "slurp", (w.chosen_route(), w.chosen_runs_on()))
  w.query_one("#tw-next").press(); await pilot.pause(0.3) # (the summary)
  w.query_one("#tw-go").press() # (Add)
  await pilot.pause(0.5)
  t = app.transfers.transfers[-1]
  check("Add: added, not started", t.state == "new" and not t.runs, (t.state, t.runs))
  app.transfers.run_now(t)
  check("Run now: done through the relay", await until(pilot, lambda: t.state == "done", 90), (t.state, t.message, t.log[-4:]))
  check("the files are on this machine", same_tree(src, os.path.join(dst, "src")))
  check("its subtab", isinstance(app.screen, TransferScreen) and app.in_transfers)
  app.transfers.start(t)
  check("Run now: done again", await until(pilot, lambda: t.state == "done" and len(t.runs) == 2, 60), (t.state, t.message))
  check("…and it copied nothing (idempotent)", (t.progress.get("stats") or {}).get("transferred") == 0, t.progress.get("stats"))
  app.leave_transfers(); await pilot.pause(0.3)

async def t_transfer_job(app, pilot):
  """A transfer job on the lab: its progress from progress.json, Pause (scancel, after the warning), Continue (a new job), done"""
  from slurp.transfer import model as M
  from slurp.ui.screens.transfers import TransferScreen
  src, dst = xfer_tree("job", big=30_000_000)
  lab = next(c for c in app.clusters if c.destination)
  ref = M.tab_ref(lab)
  t = M.Transfer(M.new_id(), "job run", {"tab": ref, "paths": [src]}, {"tab": ref, "path": dst}, route="same-fs", runs_on="job",
                 options={"partition": "normal", "time": "01:00:00", "bwlimit": "1"}, plan={"remaining_bytes": 30_400_005})
  app.transfers.add(t)
  tested = []
  app.transfers.test_job(t, "same-fs", tested.append)
  check("its job script tested on the cluster (sbatch --test-only)", await until(pilot, lambda: tested, 30)
        and tested[0][0] is True and "to start at" in tested[0][1], tested)
  app.transfers.run_now(t)
  app.open_transfer(t); await pilot.pause(0.3)
  check("submitted: a transfer job", await until(pilot, lambda: t.run.get("job"), 30) and t.state in ("queued", "running"), (t.state, t.message))
  check("its progress from the cluster", await until(pilot, lambda: t.state == "running" and t.progress.get("bytes", 0) > 1_000_000, 40),
        (t.state, t.progress))
  calls = open(os.path.join(LAB, "sbatch_calls")).read()
  check("sbatch with the job's script", f"XFER={t.run['job']}" in calls)
  first = t.run["job"]
  screen = app.screen
  check("the details", isinstance(screen, TransferScreen))
  # Its job, while it runs (just submitted: not in the job list as it was loaded): its job window, in the lab's tab
  check("…not in the job list loaded before", first not in lab.jobs, list(lab.jobs)[:5])
  await pilot.click("#xd-job")
  from slurp.ui.screens.job import JobScreen
  check("Its job: the job window of its transfer job", await until(
    pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.job.jid == first and not app.in_transfers, 20),
    (type(app.screen).__name__, getattr(getattr(app.screen, "job", None), "jid", None)))
  app.open_transfer(t); await pilot.pause(0.5)
  screen = app.screen
  check("back in its details", isinstance(screen, TransferScreen) and t.state == "running", (type(screen).__name__, t.state))
  await until(pilot, lambda: screen.query_one("#xd-pause").display and screen.query_one("#xd-pause").region.width > 0, 10)
  await pilot.pause(0.3) # (laid out: the click lands on Pause)
  await pilot.click("#xd-pause"); await pilot.pause(0.4)
  check("Pause: it warns (the job is cancelled)", isinstance(app.screen, ConfirmScreen) and "cancels Slurm job" in str(app.screen.dialog_message))
  await pilot.click("#confirm-yes")
  check("paused", await until(pilot, lambda: t.state == "paused", 40), (t.state, t.message))
  check("scancel of its job", first in open(os.path.join(LAB, "scancel_calls")).read())
  t.options["bwlimit"] = ""
  await pilot.click("#xd-resume")
  check("Continue: a new job, done", await until(pilot, lambda: t.state == "done", 60), (t.state, t.message, t.log[-4:]))
  check("another job", t.runs[-1]["job"] and t.runs[-1]["job"] != first, t.runs)
  check("the history: paused, done", [r["result"] for r in t.runs] == ["paused", "done"], t.runs)
  check("the files are whole", same_tree(src, os.path.join(dst, "src")))
  folder = os.path.join(LAB, "cache", "slurp", "transfers", t.id)
  check("its folder on the cluster: progress.json, result.json, the log", all(os.path.exists(os.path.join(folder, f)) for f in ("progress.json", "result.json", "log")))
  app.leave_transfers(); await pilot.pause(0.3)

async def t_transfer_detached(app, pilot):
  """Detached on the lab's login node: followed in its folder there, done"""
  from slurp.transfer import model as M
  src, dst = xfer_tree("detached")
  lab = next(c for c in app.clusters if c.destination)
  ref = M.tab_ref(lab)
  t = M.Transfer(M.new_id(), "detached run", {"tab": ref, "paths": [src]}, {"tab": ref, "path": dst}, route="same-fs", runs_on="detached")
  app.transfers.add(t)
  app.transfers.run_now(t)
  check("done", await until(pilot, lambda: t.state == "done", 60), (t.state, t.message, t.log[-4:]))
  check("the files", same_tree(src, os.path.join(dst, "src")))
  ov_line = app.transfers.summary_for(lab)
  check("nothing under way: no line in the overview", ov_line == "", ov_line)
  app.open_transfer(t); await pilot.pause(0.5)
  app.screen.action_open_end(0)
  check("its source, a click: the folder on the cluster, in the cluster's tab", await until(
    pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.path == src and app.cluster is lab and not app.in_transfers, 20),
    (type(app.screen).__name__, getattr(app.screen, "path", "")))
  app.leave_transfers(); await pilot.pause(0.2)


async def t_host_key_changed(app, pilot):
  """The host key of the cluster changed: the reconnect fails, and while slurp tries again the overview does not look
  connected: no Jobs, no health asked, Abort instead of Disconnect, and it says the host key changed"""
  lab = next(c for c in app.clusters if c.destination)
  known = os.path.join(LAB, "known_hosts")
  saved = open(known).read()
  other = open(os.path.join(HERE, "share", "otherkey.pub")).read().split()[:2]
  try:
    open(known, "w").write(saved.split()[0] + " " + " ".join(other) + "\n") # (another key for the host)
    app.activate_cluster(app.clusters.index(lab), connect=False)
    app.show_overview(); await pilot.pause(0.3)
    lab.connection.kill_session() # (the session breaks: slurp reconnects by itself, and fails)
    ov = app.screen
    said = lambda: str(ov.query_one("#ov-ssh").render())
    check("the reconnect fails on the changed host key: said", await until(pilot, lambda: "host key" in said() and "changed" in said(), 60), said())
    check("…not shown as connected: no Jobs, no health", not ov.query_one("#ov-links").display and not ov.query_one("#ov-health").display)
    button = ov.query_one("#ov-disconnect")
    check("…Abort instead of Disconnect", ov.query_one("#ov-disconnect-row").display and "Abort" in str(button.label), str(button.label))
    button.press(); await pilot.pause(0.5)
    check("Abort: not connected, Connect", ov.query_one("#ov-connect-row").display and not lab.connection.wanted)
  finally:
    open(known, "w").write(saved)
  app.connect_tab()
  await until(pilot, lambda: "passphrase" in term(app), 30); await pilot.pause(0.3)
  await pilot.press(*"pw123", "enter")
  check("the right key back: connected again", await until(pilot, lambda: lab.connection.ready, 30))
  app.show_overview()
  check("…Disconnect again", await until(pilot, lambda: isinstance(app.screen, OverviewScreen)
                                          and "Disconnect" in str(app.screen.query_one("#ov-disconnect").label), 10))
