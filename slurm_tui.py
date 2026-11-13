#! /usr/bin/env python3
"""
Interactive terminal UI to browse queued, running and finished slurm jobs:
click a job for its details, steps and files, click a file to read it (with
tail -f style following), and run commands like nvidia-smi on the nodes of a
running job.
"""

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
from textual.worker import get_current_worker

import slurm as sl

log = logging.getLogger("slurm_tui")

TAIL_BYTES = 4*1024*1024 # files larger than this are shown from their end
PROGRESS_TAIL_BYTES = 64*1024
PROGRESS_TAIL_LINES = 30
PROGRESS_PATTERN = re.compile(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)")
STEP_NAME = "slurm_tui"  # job name of the steps that run commands on the nodes
PRESETS = {
  "nvidia-smi": "nvidia-smi",
  "GPU csv":    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv",
  "free":       "free -h",
  "top":        "top -bn1 | head -30",
  "uptime":     "uptime",
  "ps":         "ps -u $USER -o pid,pcpu,rss,etime,cmd",
}

# ---------------------------------------------------------------------------
# logging: never write to the terminal, collect warnings to show them in the UI

class Collector(logging.Handler):
  def __init__(self):
    super().__init__(logging.WARNING)
    self.messages = []
  def emit(self, record):
    self.messages.append((tab_name(), record.getMessage())) # the tab of the thread that logs it
  def drain(self):
    msgs, self.messages = self.messages, []
    return msgs

collector = Collector()
ssh_connection = None # RemoteSSH of the active tab, None: the local machine
initial_ssh_destinations = []

@dataclass
class ClusterSession:
  """A tab: the local machine or an SSH destination, with its own connection, shell and job list"""
  name: str
  destination: str | None = None
  connection: object | None = None
  terminal: object | None = None       # shell in the pane on the right while this tab is active
  login_terminal: object | None = None # the ssh login (control master) of the connection
  jobs: dict = field(default_factory=dict)
  since: object | None = None          # finished-since time of the loaded list
  summary: str = ""                    # status line of the loaded list
  loaded: bool = False
  loader: object | None = None         # worker loading the job list
  accounts: list | None = None         # Slurm accounts of the user (sacctmgr), None: not asked yet
  scope: str = "me"                    # whose jobs are listed: "me", "all" (all accounts) or an account
  load_started: float = 0.0
  updated: object | None = None        # when the list was loaded
  show_login: bool = False             # the login is shown in the pane (until it is done)
  attempts: int = 0                    # logins since the session was last ready
  ready_generation: int | None = None  # session generation the app has seen ready
  broken_at: float | None = None       # when the session broke (or the last reconnect started)

def is_local_destination(destination):
  return destination.lower() in {"localhost", "127.0.0.1", "::1"}

def setup_logging(logfile):
  for name in ("slurm_tui", "utils"): # "utils" is the logger of slurm.py / helpers.py
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.DEBUG if logfile else logging.WARNING)
    lg.addHandler(collector)
    if logfile:
      fh = logging.FileHandler(logfile)
      fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(funcName)s] %(message)s"))
      lg.addHandler(fh)

# ---------------------------------------------------------------------------
# running commands, locally or on the cluster of a tab

SLURM_ENV = {"SLURM_TIME_FORMAT": "standard"}

class Cancelled(BaseException):
  """The user aborted the work of a worker (a BaseException, so that `except Exception` on the way lets it pass)"""

_thread = threading.local() # per worker thread: the connection it works with (conn) and its abort event (cancel)

def conn():
  """Connection of the cluster the running code works for: the one its worker was started with, else the active tab's"""
  return _thread.conn if hasattr(_thread, "conn") else ssh_connection

def tab_name():
  """Name of the tab the running code works for (shown as the title of its notifications)"""
  c = conn()
  return c.destination if c is not None else "Local"

def check_cancelled():
  event = getattr(_thread, "cancel", None)
  if event is not None and event.is_set():
    raise Cancelled()

CURRENT = object() # start_worker(connection=CURRENT): the connection of the active tab

def start_worker(node, fn, group, exclusive=True, connection=CURRENT, name=""):
  """Run fn() in a thread worker of node. The worker keeps the connection of the tab that was active when it was
  started (or the given one), so switching tabs does not redirect its commands; worker.abort stops the command it
  is waiting for; an error is logged and shown instead of ending the app."""
  bound = ssh_connection if connection is CURRENT else connection
  abort = threading.Event()
  app = node.app
  if exclusive:
    for w in list(app.workers):
      if w.node is node and w.group == group and hasattr(w, "abort"):
        w.abort.set()
  def body():
    _thread.conn, _thread.cancel = bound, abort
    try:
      return fn()
    except Cancelled:
      return None
    except Exception as e:
      log.exception(f"{name or group} failed")
      if not abort.is_set():
        app.call_from_thread(app.notify, f"{name or group} failed: {e}", title=tab_name(), severity="error", timeout=10)
      return None
    finally:
      del _thread.conn, _thread.cancel
  worker = node.run_worker(body, thread=True, group=group, exclusive=exclusive, exit_on_error=False, name=name or group)
  worker.abort = abort
  return worker

def in_background(node, work, done, group="io", name="", connection=CURRENT):
  """Run work() in a worker (remote file access and commands never run on the UI thread), then done(result) on the
  UI thread, if node is still there; result is None if work() failed (the error is shown)"""
  app = node.app
  def body():
    try:
      result = work()
    except Exception as e:
      log.exception(f"{name or group} failed")
      app.call_from_thread(app.notify, f"{name or group} failed: {e}", title=tab_name(), severity="error", timeout=10)
      result = None
    app.call_from_thread(lambda: done(result) if node is app or node.is_attached else None)
  return start_worker(node, body, group, exclusive=False, connection=connection, name=name)

def cluster_work(group, exclusive=True):
  """Decorator for methods that run as start_worker() workers, like @work(thread=True)"""
  def deco(method):
    @functools.wraps(method)
    def start(self, *args, **kwargs):
      return start_worker(self, lambda: method(self, *args, **kwargs), group, exclusive, name=method.__name__)
    return start
  return deco

def run_local(cmd, timeout=60, env=None, cwd=None, text=True):
  """subprocess.run with capture_output that the abort of the worker stops"""
  proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=text,
                          env=env, cwd=cwd or None)
  deadline = time.monotonic() + timeout
  while True:
    try:
      out, err = proc.communicate(timeout=0.2)
      return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
      expired = time.monotonic() > deadline
      try:
        check_cancelled()
      except Cancelled:
        proc.kill()
        proc.communicate()
        raise
      if expired:
        proc.kill()
        proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout)

def run_cmd(cmd, timeout=60, env=None, cwd=None, text=True, retry=True):
  """Run a command on the cluster of the current tab (worker) and return the CompletedProcess. retry=False for
  commands that must not run twice (sbatch, scancel): they are not sent again after a reconnect."""
  c = conn()
  if c is not None:
    return c.run(cmd, timeout=timeout, quiet=True, env=env, cwd=cwd, text=text, retry=retry)
  try:
    return run_local(cmd, timeout, os.environ | (env or {}), cwd, text)
  except (OSError, subprocess.TimeoutExpired) as e:
    return subprocess.CompletedProcess(cmd, 255, "" if text else b"", str(e) if text else str(e).encode())

def run(cmd, timeout=60, quiet=False):
  """Run a command, return its stdout ('' on failure, with a warning unless quiet)"""
  log.debug(f"running command: {shlex.join(cmd)}")
  res = run_cmd(cmd, timeout, SLURM_ENV)
  if res.returncode != 0 and not quiet:
    log.warning(f"{shlex.join(cmd[:3])} ... failed: {(res.stderr or '').strip()[:300]}")
  return res.stdout or ""

def run_many(cmds, timeout=60):
  """Run several commands; on a remote cluster all of them in a single round trip. CompletedProcess list."""
  for cmd in cmds:
    log.debug(f"running command: {shlex.join(cmd)}")
  c = conn()
  if c is not None:
    return c.run_many([(cmd, SLURM_ENV, None) for cmd in cmds], timeout=timeout, quiet=True)
  return [run_cmd(cmd, timeout, SLURM_ENV) for cmd in cmds]

class RemoteDirEntry:
  """Small os.DirEntry-compatible record returned by remote scandir."""
  def __init__(self, directory, info):
    self.name = info["name"]
    self.path = os.path.join(directory, self.name)
    self.info = info
  def is_file(self, follow_symlinks=True):
    return self.info["isfile"]
  def is_dir(self, follow_symlinks=True):
    return self.info["isdir"]
  def is_symlink(self):
    return self.info.get("link", False)
  def stat(self, follow_symlinks=True):
    return type("Stat", (), {"st_size": self.info["size"], "st_mtime": self.info["mtime"]})()

class RemoteScandir(list):
  def __enter__(self): return self
  def __exit__(self, *exc): return False

ui_thread_remote_calls = [] # remote commands run on the UI thread (they block it): must stay empty

# stderr of the ssh process is the terminal of the login pane, which becomes its controlling terminal (/dev/tty, where
# ssh asks for passwords); stdin and stdout are the command channel of the app
CTTY_STDERR = "import fcntl, os, sys, termios; fcntl.ioctl(2, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
PING_INTERVAL = 15 # seconds without an answer from the cluster before a heartbeat is sent
PING_SLOW = 5      # seconds without an answer to the heartbeat: shown as not responding
PING_DEAD = 60     # seconds without an answer to the heartbeat: the session is given up and reconnected

class RemoteSSH:
  """The SSH session of a tab. One ssh process is the login (its prompts appear in the terminal pane) and then the
  command channel of the app: it runs /bin/sh on the cluster, which runs every command of the app in the
  background (any number at a time) and writes the results back framed by a random token. A command costs a round
  trip in the open session; no ssh process is started for it. The process is also the OpenSSH control master, so
  interactive shells (Shell, node shells, GDB) open as channels of the same connection, without a new login.
  A heartbeat notices a session that stops answering; the app then reconnects it."""
  def __init__(self, destination):
    self.destination = destination
    self.socket_dir = tempfile.mkdtemp(prefix=f"slurm-tui-{os.getuid()}-")
    os.chmod(self.socket_dir, 0o700)
    self.control_path = os.path.join(self.socket_dir, "control")
    self.proc = None             # the ssh process of the current session
    self.state = "new"           # new, connecting, ready, broken, closed
    self.generation = 0          # sessions started so far
    self.user, self.groups, self.home = "", [], ""
    self.error = ""              # why the last session ended
    self.connecting_since = 0.0
    self.last_answer = 0.0       # time.monotonic() of the last result from the cluster
    self.ping_sent = None        # time.monotonic() of the heartbeat waiting for its answer
    self.latency = None          # seconds of the last heartbeat
    self.wanted = True           # reconnect when the session breaks (False after the user closed the login)
    self.lock = threading.Lock()       # state and pending results
    self.write_lock = threading.Lock() # the input of the session
    self.pending = {}            # command id -> {"event", "result" or "error"}
    self.next_id = 0
    self.token = ""
    # diagnostics (the connection window)
    self.events = collections.deque(maxlen=200) # (datetime, text)
    self.hostname = ""           # of the login node
    self.bytes_sent = self.bytes_received = 0 # in the command channel, all sessions
    self.commands = 0            # sent, all sessions
    self.started_at = self.ready_at = None # datetime of the start / the login of the current session
    self.exit_code = None        # of the ssh process of the last session that ended
    self.latencies = collections.deque(maxlen=50) # heartbeat round trips, seconds
    self.argv = []
    self.heartbeat_thread = None

  def event(self, text):
    self.events.append((datetime.now(), text))
    log.debug(f"{self.destination}: {text}")

  @property
  def ready(self):
    return self.state == "ready"

  @property
  def master(self):
    return self.proc

  @property
  def identity(self):
    return f"{self.user}@{self.destination}" if self.user else self.destination

  @property
  def connected(self):
    return self.ready

  def responding(self):
    """Seconds the heartbeat waits for its answer (None: it is not waiting)"""
    sent = self.ping_sent
    return None if sent is None else time.monotonic() - sent

  # ---- the session
  def spawn(self, tty):
    """Start the ssh process (called by the TerminalSession of the login with its terminal)"""
    with self.lock:
      self.generation += 1
      generation, self.token = self.generation, os.urandom(8).hex()
      self.state, self.error, self.connecting_since = "connecting", "", time.monotonic()
      self.ping_sent, self.started_at, self.ready_at, self.exit_code = None, datetime.now(), None, None
      replaced, self.pending = self.pending, {}
    for entry in replaced.values(): # waiting for a session that was replaced: sent again in the new one
      entry["error"] = "the session was replaced"
      entry["event"].set()
    if not os.path.isdir(self.socket_dir): # closed before
      self.socket_dir = tempfile.mkdtemp(prefix=f"slurm-tui-{os.getuid()}-")
      self.control_path = os.path.join(self.socket_dir, "control")
    try:
      os.unlink(self.control_path) # left by a broken session: ssh -M would not listen on it
    except OSError:
      pass
    if self.heartbeat_thread is None or not self.heartbeat_thread.is_alive():
      self.heartbeat_thread = threading.Thread(target=self.heartbeat, daemon=True, name=f"heartbeat {self.destination}")
      self.heartbeat_thread.start()
    argv = self.argv = ["ssh", "-M", "-S", self.control_path, "-o", "ControlMaster=yes", "-o", "ControlPersist=no",
                        "-T", "-x", "-a", self.destination, "exec /bin/sh"]
    self.event(f"session {generation}: connecting")
    proc = subprocess.Popen([sys.executable, "-c", CTTY_STDERR, *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=tty, start_new_session=True, env=os.environ | {"TERM": "xterm-256color"})
    self.proc = proc
    # the shell keeps the results in a temporary directory, out() writes one result at a time (mkdir is the lock)
    setup = ('d=$(mktemp -d "${TMPDIR:-/tmp}/slurm_tui.XXXXXX") || exit 1\n'
             "trap 'rm -rf \"$d\"' EXIT\n"
             "trap 'exit 1' HUP INT TERM PIPE\n"
             'out() { until mkdir "$d/l" 2>/dev/null; do sleep 0.01; done; cat "$1"; rm -f "$1"; rmdir "$d/l"; }\n'
             f"printf '\\n%s ready\\n%s\\n%s\\n%s\\n%s\\n' {self.token} \"$(id -un)\" \"$(id -Gn)\" \"$HOME\" \"$(hostname)\"\n")
    try:
      proc.stdin.write(setup.encode())
      proc.stdin.flush()
    except OSError:
      pass
    threading.Thread(target=self.reader, args=(proc, generation, self.token), daemon=True,
                     name=f"session {self.destination}").start()
    return proc

  def reader(self, proc, generation, token):
    """Thread: wait for the shell (what the login scripts print before is dropped), then hand out the results"""
    fd, buf = proc.stdout.fileno(), b""
    def more():
      nonlocal buf
      chunk = os.read(fd, 1 << 16)
      if not chunk:
        raise EOFError("the session ended")
      self.bytes_received += len(chunk)
      buf += chunk
    def line():
      nonlocal buf
      while b"\n" not in buf:
        more()
      head, _, buf = buf.partition(b"\n")
      return head
    def exact(n):
      nonlocal buf
      while len(buf) < n:
        more()
      data, buf = buf[:n], buf[n:]
      return data
    error = "the session ended"
    try:
      marker = f"\n{token} ready\n".encode()
      while marker not in buf:
        buf = buf[-len(marker):] if len(buf) > 1 << 20 else buf
        more()
      buf = buf.partition(marker)[2]
      user, groups, home, hostname = (line().decode(errors="replace").strip() for _ in range(4))
      with self.lock:
        if generation != self.generation or self.state == "closed":
          return
        self.user, self.groups, self.home, self.hostname = user, groups.split(), home, hostname
        self.state, self.last_answer, self.ready_at = "ready", time.monotonic(), datetime.now()
      self.event(f"session {generation}: connected as {user} on {hostname} "
                 f"after {time.monotonic() - self.connecting_since:.1f} s")
      tok = token.encode()
      while True:
        head = line().split()
        if len(head) != 5 or head[0] != tok:
          raise ValueError(f"unexpected output in the session: {b' '.join(head)[:100]!r}")
        cid, code, nout, nerr = map(int, head[1:])
        out, err = exact(nout), exact(nerr)
        with self.lock:
          self.last_answer = time.monotonic()
          entry = self.pending.pop(cid, None)
        if entry is not None:
          entry["result"] = (code, out, err)
          entry["event"].set()
    except (EOFError, OSError, ValueError) as e:
      error = str(e)
    finally:
      with self.lock:
        current = generation == self.generation
        if current:
          if self.state != "closed":
            self.state = "broken"
          self.error = (self.error or error or "the session ended") if self.state == "broken" else ""
          failed, self.pending = self.pending, {}
        else:
          failed = {}
      for entry in failed.values():
        entry["error"] = f"connection to {self.destination} lost: {error}"
        entry["event"].set()
      if current:
        if isinstance(error, str) and error.startswith("unexpected output"):
          try:
            proc.kill()
          except OSError:
            pass
        try:
          self.exit_code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
          proc.kill()
          self.exit_code = proc.wait()
        if self.state != "closed":
          self.event(f"session {generation} ended: {self.error} (ssh exit code {self.exit_code})")
      else:
        try:
          code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
          proc.kill()
          code = proc.wait()
        self.event(f"session {generation} ended: replaced by session {self.generation} (ssh exit code {code})")

  def send(self, text):
    data = text.encode()
    with self.write_lock:
      self.proc.stdin.write(data)
      self.proc.stdin.flush()
      self.bytes_sent += len(data)

  def wrap(self, cid, argv, env=None, cwd=None):
    """Shell code that runs a command in the background and writes its result as one frame"""
    f = f'"$d/{cid}'
    parts = [f"cd {shlex.quote(cwd)} || exit 125"] if cwd else []
    parts += [f"export {k}={shlex.quote(str(v))}" for k, v in (env or {}).items()]
    parts.append(f"exec sh -c 'echo $$ >\"$0\"; exec \"$@\"' {f}.pid\" " + shlex.join(map(str, argv)))
    return (f"{{ ( {'; '.join(parts)} ) >{f}.o\" 2>{f}.e\" </dev/null; r=$?; "
            f"{{ printf '%s %s %s %s %s\\n' {self.token} {cid} \"$r\" $(wc -c <{f}.o\") $(wc -c <{f}.e\"); "
            f"cat {f}.o\" {f}.e\"; }} >{f}.f\"; rm -f {f}.o\" {f}.e\" {f}.pid\"; out {f}.f\"; }} &\n")

  def request(self, requests, timeout=60, retry=True):
    """[(argv, env, cwd)] -> [(exit code, stdout bytes, stderr bytes)], all sent in one go. Waits for the session
    while it (re)connects; with retry, commands lost with a broken session are sent again after the reconnect (only
    for commands that may run twice). Raises TimeoutError, EOFError (session lost) or Cancelled (worker aborted)."""
    if threading.current_thread() is threading.main_thread():
      ui_thread_remote_calls.append(requests[0][0])
      log.warning(f"remote command on the UI thread (blocks it): {shlex.join(map(str, requests[0][0][:4]))}")
    wait_until = time.monotonic() + max(timeout, 300) # for a reconnect (it may need a password)
    while not self.ready:
      check_cancelled()
      if self.state == "closed":
        raise EOFError(f"the connection to {self.destination} is closed")
      if time.monotonic() > wait_until:
        raise TimeoutError(f"not connected to {self.destination}")
      time.sleep(0.1)
    deadline = time.monotonic() + timeout
    entries = []
    with self.lock:
      for argv, env, cwd in requests:
        cid, self.next_id = self.next_id, self.next_id + 1
        entries.append((cid, {"event": threading.Event()}))
        self.pending[cid] = entries[-1][1]
      script = "".join(self.wrap(cid, *r) for (cid, _), r in zip(entries, requests))
      self.commands += len(requests)
    log.debug(f"round trip with {len(requests)} command(s): {shlex.join(map(str, requests[0][0][:4]))} ...")
    try:
      try:
        self.send(script)
      except (OSError, ValueError): # the session broke before: nothing ran, wait for the reconnect and send again
        self.abandon([cid for cid, _ in entries])
        time.sleep(0.1)
        return self.request(requests, max(1, deadline - time.monotonic()), retry)
      for cid, entry in entries:
        while not entry["event"].wait(0.1):
          check_cancelled()
          if time.monotonic() > deadline:
            raise TimeoutError(f"no answer from {self.destination} within {timeout} s")
    except BaseException:
      self.abandon([cid for cid, _ in entries])
      raise
    results = []
    for cid, entry in entries:
      if "error" in entry:
        if retry and self.state != "closed" and time.monotonic() < deadline:
          log.debug(f"{self.destination}: {entry['error']}, sending again after the reconnect")
          return self.request(requests, deadline - time.monotonic(), retry)
        raise EOFError(entry["error"])
      results.append(entry["result"])
    return results

  def abandon(self, cids):
    """Forget commands nobody waits for any more and stop them on the cluster (without blocking the caller)"""
    with self.lock:
      open_ids = [cid for cid in cids if self.pending.pop(cid, None) is not None]
    if open_ids and self.ready:
      script = "".join(f'kill $(cat "$d/{cid}.pid" 2>/dev/null) 2>/dev/null\n' for cid in open_ids)
      threading.Thread(target=lambda: self.send_quietly(script), daemon=True).start()

  def send_quietly(self, text):
    try:
      self.send(text)
    except (OSError, ValueError, AttributeError):
      pass

  def heartbeat(self):
    """Thread: a command when the session has been quiet for a while, to notice when it stops answering"""
    while self.state != "closed":
      time.sleep(1)
      if not self.ready or time.monotonic() - self.last_answer < PING_INTERVAL:
        continue
      self.ping_sent = sent = time.monotonic()
      generation = self.generation
      try:
        self.request([(["true"], None, None)], timeout=PING_DEAD)
        self.latency = time.monotonic() - sent
        self.latencies.append(self.latency)
      except TimeoutError:
        if generation == self.generation and self.ready:
          log.warning(f"session {self.destination}: no answer for {PING_DEAD} s, reconnecting")
          self.event(f"session {generation}: no answer to the heartbeat for {PING_DEAD} s, giving it up")
          self.error = f"no answer for {PING_DEAD} s"
          self.kill_session()
      except (EOFError, OSError):
        pass
      finally:
        self.ping_sent = None

  def kill_session(self):
    proc = self.proc
    if proc is not None and proc.poll() is None:
      try:
        proc.kill()
      except OSError:
        pass

  # ---- commands
  def run_many(self, requests, timeout=60, quiet=False, text=True, retry=True):
    """[(argv, env, cwd)] in one round trip -> [CompletedProcess]"""
    try:
      results = self.request(requests, timeout, retry)
    except (OSError, EOFError, TimeoutError, ValueError) as e:
      msg = f"{shlex.join(map(str, requests[0][0][:3]))} ... failed: {e}"
      if not quiet:
        log.warning(f"SSH {self.destination}: {msg}")
      return [subprocess.CompletedProcess(argv, 255, "" if text else b"", msg if text else msg.encode())
              for argv, _, _ in requests]
    out = []
    for (argv, _, _), (code, stdout, stderr) in zip(requests, results):
      if text:
        stdout, stderr = stdout.decode(errors="replace"), stderr.decode(errors="replace")
      if code and not quiet:
        log.warning(f"{shlex.join(map(str, argv[:3]))} ... failed: {(stderr if text else stderr.decode(errors='replace')).strip()[:300]}")
      out.append(subprocess.CompletedProcess(argv, code, stdout, stderr))
    return out

  def run(self, command, timeout=60, quiet=False, env=None, cwd=None, text=True, retry=True):
    return self.run_many([(list(command), env, cwd)], timeout, quiet, text, retry)[0]

  def command_argv(self, command, interactive=False, cwd=None):
    """ssh command line for a new channel of the connection that runs command (none: the login shell)"""
    remote = [f"cd {shlex.quote(cwd)} || exit"] if cwd else []
    if command:
      remote.append("exec " + shlex.join(list(map(str, command))))
    return ["ssh", "-S", self.control_path, "-tt" if interactive else "-T", self.destination,
            *(["; ".join(remote)] if remote else [])]

  def path_info(self, path):
    source = ("p=$1; [ -e \"$p\" ] || [ -L \"$p\" ] || exit 0; "
              "f=0; d=0; [ ! -f \"$p\" ] || f=1; [ ! -d \"$p\" ] || d=1; "
              "stat -L -c \"$f $d %s %Y\" -- \"$p\"; readlink -f -- \"$p\"")
    result = self.run(["sh", "-c", source, "sh", path], quiet=True)
    lines = (result.stdout or "").splitlines()
    if result.returncode or len(lines) < 2:
      return {}
    try:
      isfile, isdir, size, mtime = lines[0].split()
      return {"exists": True, "isfile": isfile == "1", "isdir": isdir == "1",
              "size": int(size), "mtime": float(mtime), "realpath": lines[1]}
    except (ValueError, TypeError):
      return {}

  def exists(self, path): return bool(self.path_info(path).get("exists"))
  def isfile(self, path): return bool(self.path_info(path).get("isfile"))
  def isdir(self, path): return bool(self.path_info(path).get("isdir"))
  def getsize(self, path):
    info = self.path_info(path)
    if not info.get("exists"):
      raise OSError(f"remote path does not exist: {path}")
    return info["size"]
  def stat(self, path):
    info = self.path_info(path)
    if not info.get("exists"):
      raise OSError(f"remote path does not exist: {path}")
    return type("Stat", (), {"st_size": info["size"], "st_mtime": info["mtime"]})()
  def realpath(self, path): return self.path_info(path).get("realpath", path)

  def expanduser(self, path):
    if self.home and (path == "~" or path.startswith("~/")):
      return self.home + path[1:]
    return path

  def which(self, name):
    source = "command -v \"$1\" 2>/dev/null || true"
    return self.run(["sh", "-c", source, "sh", name], quiet=True).stdout.strip()

  def is_hdf5(self, path):
    source = ("p=$1; for offset in 0 512 1024 2048 4096 8192 16384 32768 65536 "
              "131072 262144 524288 1048576; do "
              "sig=$(od -An -tx1 -j \"$offset\" -N 8 -- \"$p\" 2>/dev/null | tr -d ' \\n'); "
              "[ \"$sig\" = 894844460d0a1a0a ] && exit 0; done; exit 1")
    return self.run(["sh", "-c", source, "sh", path], quiet=True).returncode == 0

  def scandir(self, path):
    result = self.run(["find", "--", path, "-mindepth", "1", "-maxdepth", "1", "-printf", "%f\\t%Y\\t%s\\t%T@\\t%y\\n"], quiet=True)
    if result.returncode and not result.stdout:
      raise OSError((result.stderr or "remote directory listing failed").strip())
    items = []
    for line in (result.stdout or "").splitlines():
      fields = line.split("\t")
      if len(fields) != 5:
        continue
      name, kind, size, mtime, own = fields
      items.append({"name": name, "isfile": kind == "f", "isdir": kind == "d", "link": own == "l",
                    "size": int(size), "mtime": float(mtime)})
    return RemoteScandir(RemoteDirEntry(path, item) for item in items)

  def read(self, path, offset=0, size=None):
    if size is None:
      args = ["cat", "--", path]
    else:
      args = ["dd", f"if={path}", "iflag=skip_bytes,count_bytes", f"skip={int(offset)}", f"count={int(size)}", "status=none"]
    result = self.run(args, timeout=120, quiet=True, text=False)
    if result.returncode:
      error = result.stderr.decode(errors="replace") if result.stderr else "remote file read failed"
      raise OSError(error.strip())
    return result.stdout

  def download(self, path, suffix=""):
    """Copy a (large) file over a channel of the connection, not through the command session"""
    fd, local_path = tempfile.mkstemp(prefix="slurm-tui-remote-", suffix=suffix)
    try:
      with os.fdopen(fd, "wb") as output:
        proc = subprocess.Popen(self.command_argv(["cat", "--", path]), stdout=output, stdin=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        while True:
          try:
            _, err = proc.communicate(timeout=0.2)
            break
          except subprocess.TimeoutExpired:
            try:
              check_cancelled()
            except Cancelled:
              proc.kill()
              proc.communicate()
              raise
      if proc.returncode:
        raise OSError(err.decode(errors="replace").strip() or f"cannot download {path}")
      return local_path
    except BaseException:
      try: os.unlink(local_path)
      except OSError: pass
      raise

  def close(self):
    """End the session: the remote shell exits on the end of its input and removes its temporary directory"""
    if self.state != "closed":
      self.event("closed")
    with self.lock:
      self.state = "closed"
      failed, self.pending = self.pending, {}
    for entry in failed.values():
      entry["error"] = f"the connection to {self.destination} is closed"
      entry["event"].set()
    proc = self.proc
    if proc is not None and proc.poll() is None:
      try:
        proc.stdin.close()
        proc.wait(timeout=1)
      except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
      if proc.poll() is None:
        proc.terminate()
    shutil.rmtree(self.socket_dir, ignore_errors=True)

def fs_exists(path): return conn().exists(path) if conn() else os.path.exists(path)
def fs_isfile(path): return conn().isfile(path) if conn() else os.path.isfile(path)
def fs_isdir(path): return conn().isdir(path) if conn() else os.path.isdir(path)
def fs_getsize(path): return conn().getsize(path) if conn() else os.path.getsize(path)
def fs_stat(path): return conn().stat(path) if conn() else os.stat(path)
def fs_scandir(path): return conn().scandir(path) if conn() else os.scandir(path)
def fs_read(path, offset=0, size=None):
  if conn():
    return conn().read(path, offset, size)
  with open(path, "rb") as f:
    f.seek(offset)
    return f.read() if size is None else f.read(size)

def fs_realpath(path): return conn().realpath(path) if conn() else os.path.realpath(path)

def fs_expanduser(path):
  if conn() and str(path).startswith("~"):
    return conn().expanduser(path)
  return os.path.expanduser(path)

def command_exists(name):
  return bool(conn().which(name)) if conn() else shutil.which(name) is not None

def parsable(out, fields):
  """Rows of '|'-separated output as dicts, the last field may contain '|'; other lines (e.g. messages) are skipped"""
  rows = [line.split("|", len(fields)-1) for line in out.splitlines()]
  return [dict(zip(fields, row)) for row in rows if len(row) == len(fields)]

SACCT_FIELDS = ["JobID", "JobIDRaw", "JobName", "User", "Account", "Partition", "State", "ExitCode", "NNodes", "NTasks",
                "NodeList", "Submit", "Start", "End", "Elapsed", "Timelimit", "WorkDir", "StdOut", "StdErr",
                "MaxRSS", "AveRSS", "TRESUsageInMax", "SubmitLine"]
SQUEUE_FIELDS = ["JobID", "JobName", "User", "Account", "Partition", "State", "Reason", "NNodes", "Elapsed",
                 "Timelimit", "Start", "Submit", "WorkDir", "NodeList"]
SQUEUE_FMT = "%i|%j|%u|%a|%P|%T|%r|%D|%M|%l|%S|%V|%Z|%N"

def sacct_cmd(args):
  return ["sacct", *args, "-P", "-n", f"--format={','.join(SACCT_FIELDS)}"]

def squeue_cmd(args):
  return ["squeue", *args, "-r", "-h", f"--format={SQUEUE_FMT}"]

def sacct(args):
  return parsable(run(sacct_cmd(args)), SACCT_FIELDS)

def squeue(args, quiet=False):
  return parsable(run(squeue_cmd(args), quiet=quiet), SQUEUE_FIELDS)

def is_time(val):
  return bool(val) and val not in ("Unknown", "None", "N/A", "(null)")

def fmt_time(val):
  """2026-11-12T15:14:29 -> 11-12 15:14"""
  try:
    return datetime.fromisoformat(val).strftime("%m-%d %H:%M")
  except (TypeError, ValueError):
    return ""

def job_id_key(jid):
  return tuple(int(n) for n in re.findall(r'\d+', jid)) or (0,)

@dataclass
class JobProgress:
  current: int
  total: int
  line: str

@dataclass
class Job:
  rec: dict                              # sacct record of the allocation (or built from squeue)
  steps: list = field(default_factory=list)
  queued: bool = False                   # listed by squeue
  reason: str = ""                       # pending reason (squeue)
  command: str = ""                      # what the job executes (filled by annotate)
  err: tuple = (None, 0)                 # ("file"|"merged"|"missing"|None, size)
  progress: JobProgress | None = None      # latest n/N progress entry from a running job's output

  @property
  def jid(self):
    return self.rec["JobID"]

  @property
  def state(self):
    return self.rec["State"]

  @property
  def running(self):
    return self.state.startswith("RUNNING")

  @property
  def started(self):
    return is_time(self.rec["Start"]) and self.rec["Start"] <= datetime.now().isoformat()

  def stdout_path(self):
    pat = self.rec["StdOut"]
    return sl.expand_filename(pat, self.rec) if pat and pat != "(null)" else None

  def stderr_path(self):
    """Path of the error file, None if there is none or it is the output file"""
    pat = self.rec["StdErr"]
    if not pat or pat == "(null)":
      return None
    path = sl.expand_filename(pat, self.rec)
    return None if path == self.stdout_path() else path

  def merged(self):
    return self.stdout_path() is not None and self.stderr_path() is None

  def node_hours(self):
    try:
      return int(self.rec["NNodes"] or 0) * sl.hours(self.rec["Elapsed"])
    except ValueError:
      return 0

FAILED_JOB_STATES = {
  "BOOT_FAIL", "CANCELLED", "DEADLINE", "FAILED", "NODE_FAIL",
  "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "TIMEOUT",
}

def progress_for_job(job):
  """Return parsed live progress or a terminal-state bar for completed/failed jobs."""
  state = job.state.split()[0].rstrip("+") if job.state else ""
  if state == "COMPLETED":
    return JobProgress(1, 1, "Job completed successfully")
  if state in FAILED_JOB_STATES:
    return JobProgress(0, 1, f"Job ended with state {state}")
  return job.progress if job.running else None

def record_from_squeue(row):
  rec = dict.fromkeys(SACCT_FIELDS, "")
  rec.update({k: row.get(k, "") for k in SQUEUE_FIELDS if k in rec})
  rec["JobIDRaw"] = row["JobID"]
  rec["NodeList"] = "" if row.get("NodeList") in ("", "(null)") else row["NodeList"]
  return rec

def current_user():
  c = conn()
  return c.user if c else sl.getpass.getuser()

def slurm_accounts():
  """The Slurm accounts the user has associations with (sacctmgr), sorted"""
  out = run(["sacctmgr", "-nP", "show", "associations", f"user={current_user()}", "format=account"])
  return sorted({line.strip() for line in out.splitlines() if line.strip()})

def load_jobs(since, scope="me", accounts=(), jids=None):
  """Queued jobs plus jobs that finished after `since` (datetime or None), or only the given job ids.
  scope: "me" (the user's jobs), "all" (the jobs of all the user's accounts) or the name of one account."""
  accts = ",".join(accounts) if scope == "all" else scope
  by_account = scope != "me" and bool(accts)
  # squeue and the finished jobs together (one round trip on a remote cluster), then the accounting of the queued jobs
  cmds = [squeue_cmd(["-j", ",".join(jids)] if jids else ["-A", accts] if by_account else ["--me"])]
  if since and not jids:
    cmds.append(sacct_cmd([*(["-a", "-A", accts] if by_account else ["-u", current_user()]), "-S", since.isoformat(), "-E", "now"]))
  results = run_many(cmds)
  for cmd, res in zip(cmds, results):
    if res.returncode and not (jids and cmd[0] == "squeue"): # finished jobs are unknown to squeue
      log.warning(f"{shlex.join(cmd[:3])} ... failed: {(res.stderr or '').strip()[:300]}")
  if results[0].returncode and not jids:
    raise RuntimeError(f"squeue failed: {(results[0].stderr or '').strip()[:300]}")
  queued = {row["JobID"]: row for row in parsable(results[0].stdout, SQUEUE_FIELDS)}

  rows = []
  ids = set(queued) | set(jids or [])
  if ids:
    rows += sacct(["-j", ",".join(sorted(ids))])
  if len(results) > 1:
    rows += parsable(results[1].stdout, SACCT_FIELDS)

  jobs, steps = {}, {}
  for row in rows:
    base, dot, _ = row["JobID"].partition(".")
    if dot:
      steps.setdefault(base, {})[row["JobID"]] = row
    elif "[" not in base: # pending array tasks like 123_[4-7]: squeue -r lists them one by one
      jobs.setdefault(base, Job(row))

  for jid, row in queued.items():
    job = jobs.setdefault(jid, Job(record_from_squeue(row)))
    job.queued = True
    job.rec["State"] = row["State"] # squeue is up to date
    job.reason = row["Reason"]
    if row["State"] == "PENDING":
      job.rec["Start"] = row["Start"] # estimated start time

  def step_key(row):
    step = row["JobID"].partition(".")[2]
    return (0, int(step)) if step.isdigit() else (-1, step)
  for jid, job in jobs.items():
    job.steps = sorted(steps.get(jid, {}).values(), key=step_key)
  return list(jobs.values())

def step_commands(job):
  cmds = [s["SubmitLine"] for s in job.steps
          if s["JobID"].partition(".")[2].isdigit() and s["SubmitLine"] and s["JobName"] != STEP_NAME]
  if not cmds and job.rec["SubmitLine"].startswith("srun"):
    cmds = [job.rec["SubmitLine"]]
  return cmds

def command_of(job, cache):
  """srun line of the latest step, the job's own srun line, the srun lines of
  the batch script of a queued job or the sbatch/salloc line"""
  cmds = step_commands(job)
  if not cmds and job.queued:
    cmds = [expand_vars(line, job_vars(job)) for line in sl.srun_lines(batch_script(job.jid, cache))]
  cmds = list(dict.fromkeys(cmds))
  if cmds:
    return cmds[-1] + (f"  [+{len(cmds)-1} more]" if len(cmds) > 1 else "")
  return job.rec["SubmitLine"]

def batch_script(jid, cache=None):
  """Batch script as submitted, only available while the job is known to the controller"""
  key = jid.split("_")[0] # all tasks of an array share the script
  if cache is not None and key in cache:
    return cache[key]
  out = run(["scontrol", "write", "batch_script", jid, "-"]) if jid else ""
  if cache is not None:
    cache[key] = out
  return out

def array_task_id(job):
  """Array task index from a selected Slurm array element ID (for example 12345_7)"""
  for jid in (job.jid, job.rec.get("JobIDRaw", "")):
    match = re.fullmatch(r"\d+_(\d+)", str(jid))
    if match:
      return match.group(1)
  return None

def array_spec_in_args(args):
  for i, arg in enumerate(args[1:], 1):
    if arg in ("-a", "--array"):
      return args[i + 1] if i + 1 < len(args) else None
    if arg.startswith("--array="):
      return arg.split("=", 1)[1]
    if arg.startswith("-a") and arg != "-a":
      return arg[2:].lstrip("=")
  return None

def original_array_spec(job):
  """Array expression from sbatch arguments or a #SBATCH directive in its script"""
  submit_line = job.rec.get("SubmitLine") or ""
  try:
    args = shlex.split(submit_line)
  except ValueError:
    args = []
  if spec := array_spec_in_args(args):
    return spec
  workdir = job.rec.get("WorkDir") or "."
  for path in existing_files([submit_line], workdir):
    text = read_text(path)
    spec = None
    for line in text.splitlines():
      line = line.strip()
      if not line:
        continue
      if line.startswith("#SBATCH"):
        try:
          directive = shlex.split(line[len("#SBATCH"):].strip())
        except ValueError:
          continue
        spec = array_spec_in_args(["sbatch", *directive]) or spec
      elif not line.startswith("#"):
        break
    if spec:
      return spec
  return None

def expand_array_spec(spec, limit=1_000_000):
  """Expand Slurm array lists/ranges such as 1,4-10:2%3 to task IDs"""
  ids = set()
  spec = spec.split("%", 1)[0]
  for part in spec.split(","):
    match = re.fullmatch(r"(\d+)(?:-(\d+)(?::(\d+))?)?", part.strip())
    if not match:
      raise ValueError(f"unsupported Slurm array range: {spec}")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    step = int(match.group(3) or 1)
    if end < start or step < 1:
      raise ValueError(f"invalid Slurm array range: {spec}")
    ids.update(range(start, end + 1, step))
    if len(ids) > limit:
      raise ValueError(f"array has more than {limit} task IDs")
  return sorted(ids)

def compress_array_ids(ids):
  """Compress sorted task IDs into the range-list syntax accepted by sbatch"""
  ids = sorted(set(ids))
  if not ids:
    return ""
  ranges = []
  start = previous = ids[0]
  for value in ids[1:]:
    if value == previous + 1:
      previous = value
      continue
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    start = previous = value
  ranges.append(str(start) if start == previous else f"{start}-{previous}")
  return ",".join(ranges)

def array_ids_not_running_or_completed(task_ids, states):
  def should_retry(task_id):
    state = states.get(str(task_id), "").split()
    return not state or state[0].rstrip("+") not in ("RUNNING", "COMPLETED")
  return [task_id for task_id in task_ids if should_retry(task_id)]

def with_array_spec(args, spec):
  """Replace any existing --array option in an sbatch argv with one specific expression"""
  filtered, skip = [], False
  for arg in args[1:]:
    if skip:
      skip = False
    elif arg in ("-a", "--array"):
      skip = True
    elif arg.startswith("--array=") or (arg.startswith("-a") and arg != "-a"):
      continue
    else:
      filtered.append(arg)
  return [args[0], f"--array={spec}", *filtered]

def restart_args(job):
  """Original sbatch command as argv, or None when the job cannot be resubmitted this way"""
  submit_line = (job.rec.get("SubmitLine") or "").strip()
  try:
    args = shlex.split(submit_line)
  except ValueError:
    return None
  if not args or os.path.basename(args[0]) != "sbatch":
    return None
  task_id = array_task_id(job)
  if task_id is not None:
    # Replace command-line array ranges and add a single-task override for any #SBATCH directive.
    args = with_array_spec(args, task_id)
  return args

def array_task_states(parent_id):
  """Current/accounted states by array task ID; fail if either Slurm query is unavailable"""
  states = {}
  commands = [
    ["sacct", "-X", "-j", str(parent_id), "-n", "-P", "--format=JobIDRaw,State"],
    ["squeue", "-r", "-j", str(parent_id), "-h", "--format=%i|%T"],
  ]
  for cmd in commands:
    result = run_cmd(cmd, timeout=45, env=SLURM_ENV)
    if result.returncode:
      raise RuntimeError(result.stderr.strip() or f"{shlex.join(cmd)} failed")
    for line in result.stdout.splitlines():
      fields = line.split("|", 1)
      if len(fields) != 2:
        continue
      jid, state = fields[0].strip().split(".", 1)[0], fields[1].strip()
      match = re.fullmatch(rf"{re.escape(str(parent_id))}_(\d+)", jid)
      if match:
        states[match.group(1)] = state
  return states

def err_status(job, sizes=None):
  """("file", size) of the error file, or why there is none; sizes: known sizes by path (None: missing)"""
  if not (job.rec["StdOut"] or job.rec["StdErr"]) or not job.started:
    return (None, 0)
  if job.merged():
    return ("merged", 0)
  path = job.stderr_path()
  if not path:
    return (None, 0)
  if sizes is not None:
    return ("file", sizes[path]) if sizes.get(path) is not None else ("missing", 0)
  try:
    return ("file", fs_getsize(path))
  except OSError:
    return ("missing", 0)

def output_progress(path, tail=None):
  """Latest valid n/N entry in the tail of an output file (read here unless given)"""
  if not path:
    return None
  if tail is None:
    try:
      size = fs_getsize(path)
      tail = fs_read(path, max(0, size - PROGRESS_TAIL_BYTES), PROGRESS_TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
      return None
  for line in reversed(tail.splitlines()[-PROGRESS_TAIL_LINES:]):
    for match in reversed(list(PROGRESS_PATTERN.finditer(line))):
      try:
        current, total = map(int, match.groups())
      except ValueError: # extremely long digit sequences
        continue
      if current <= total:
        return JobProgress(current, total, line.strip())
  return None

def annotate(jobs):
  """Command, error file and progress of the jobs. On a remote cluster the sizes of the error files, the tails of
  the output files and the batch scripts are fetched together, in one round trip."""
  if conn() is None:
    cache = {}
    for job in jobs:
      job.command = command_of(job, cache)
      job.err = err_status(job)
      job.progress = output_progress(job.stdout_path()) if job.running else None
    return jobs
  err_paths = sorted({p for job in jobs if err_status(job, {})[0] == "missing" and (p := job.stderr_path())})
  out_paths = sorted({p for job in jobs if job.running and (p := job.stdout_path())})
  scripts = sorted({job.jid.split("_")[0]: job.jid for job in jobs if job.queued and not step_commands(job)}.items())
  cmds = ([["stat", "-L", "-c", "%s", "--", p] for p in err_paths] +
          [["tail", "-c", str(PROGRESS_TAIL_BYTES), "--", p] for p in out_paths] +
          [["scontrol", "write", "batch_script", jid, "-"] for _, jid in scripts])
  results = run_many(cmds) if cmds else []
  sizes = {}
  for p, res in zip(err_paths, results):
    sizes[p] = int(res.stdout) if res.returncode == 0 and res.stdout.strip().isdigit() else None
  results = results[len(err_paths):]
  tails = {p: res.stdout if res.returncode == 0 else None for p, res in zip(out_paths, results)}
  cache = {key: res.stdout for (key, _), res in zip(scripts, results[len(out_paths):])}
  for job in jobs:
    job.command = command_of(job, cache)
    job.err = err_status(job, sizes)
    path = job.stdout_path()
    job.progress = output_progress(path, tails.get(path)) if job.running and tails.get(path) is not None else None
  return jobs

# ---------------------------------------------------------------------------
# job details

@dataclass
class Entry:
  kind: str             # stdout, stderr, script, command, ...
  label: str            # path or description
  path: str = None      # file on disk
  text: str = None      # or content that is not a file
  note: str = ""
  exists: bool = False  # the path exists (checked by load_details)

def job_vars(job):
  """Environment of the job as far as it can be known, to expand $VARS in its batch script"""
  array_id, _, task = job.jid.partition("_")
  raw = job.rec["JobIDRaw"]
  return os.environ | {
    "SLURM_JOB_ID": raw, "SLURM_JOBID": raw, "SLURM_ARRAY_JOB_ID": array_id, "SLURM_ARRAY_TASK_ID": task,
    "SLURM_SUBMIT_DIR": job.rec["WorkDir"], "SLURM_JOB_NAME": job.rec["JobName"], "PWD": job.rec["WorkDir"],
    "SLURM_JOB_NUM_NODES": job.rec["NNodes"], "SLURM_NNODES": job.rec["NNodes"], "SLURM_NTASKS": job.rec["NTasks"],
  }

def expand_vars(string, env):
  return re.sub(r'\$\{(\w+)\}|\$(\w+)', lambda m: env.get(m.group(1) or m.group(2), m.group(0)), string)

def path_candidates(line):
  """Words of a command line that might be file names, including the values of --opt=value"""
  try:
    words = shlex.split(line, comments=True)
  except ValueError:
    words = line.split()
  for word in words[:500]:
    for part in [word] + ([word.split("=", 1)[1]] if "=" in word else []):
      part = re.sub(r'^\d*[<>]+&?', '', part).rstrip(";&|")
      if part and not part.startswith("-"):
        yield part

FS_INFO_SCRIPT = r"""
for p; do
  if [ ! -e "$p" ]; then echo -; continue; fi
  t=o; [ -d "$p" ] && t=d; [ -f "$p" ] && t=f
  s=$(stat -L -c %s -- "$p" 2>/dev/null || echo 0); h=0; b=0
  if [ "$t" = f ] && [ -r "$p" ]; then
    [ "$(od -An -tx1 -N 8 -- "$p" 2>/dev/null | tr -d ' \n')" = 894844460d0a1a0a ] && h=1
    [ "$(head -c 8192 -- "$p" | wc -c)" = "$(head -c 8192 -- "$p" | tr -d '\000' | wc -c)" ] || b=1
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$t" "$s" "$h" "$b" "$(readlink -f -- "$p")"
done
"""

def fs_infos(paths):
  """{path: {"isdir", "isfile", "size", "hdf5", "binary", "realpath"}} of the existing ones among paths;
  on a remote cluster in one round trip"""
  paths = list(dict.fromkeys(paths))
  infos = {}
  if not paths:
    return infos
  if conn() is None:
    for p in paths:
      try:
        st = os.stat(p)
      except OSError:
        continue
      isfile = os.path.isfile(p)
      infos[p] = {"isdir": os.path.isdir(p), "isfile": isfile, "size": st.st_size, "realpath": os.path.realpath(p),
                  "hdf5": isfile and _is_hdf5_local(p), "binary": isfile and is_binary(p)}
    return infos
  res = conn().run(["sh", "-c", FS_INFO_SCRIPT, "sh", *paths], quiet=True)
  for p, line in zip(paths, res.stdout.splitlines()):
    fields = line.split("\t", 4)
    if len(fields) == 5:
      t, size, h, b, real = fields
      infos[p] = {"isdir": t == "d", "isfile": t == "f", "size": int(size) if size.isdigit() else 0,
                  "hdf5": h == "1", "binary": b == "1", "realpath": real or p}
  return infos

def existing_files(lines, workdir, env=None, dirs=False):
  """Existing files (and directories if dirs) named in command lines, relative paths are relative to workdir
  (checked together: one round trip on a remote cluster)"""
  cands = []
  for line in lines:
    if env is not None:
      line = expand_vars(line, env)
    for cand in path_candidates(line):
      path = fs_expanduser(cand)
      path = path if os.path.isabs(path) else os.path.join(workdir or ".", path)
      cands.append(os.path.normpath(path))
  infos = fs_infos(cands)
  return [p for p in dict.fromkeys(cands) if p in infos and (infos[p]["isfile"] or dirs and infos[p]["isdir"] and p != "/")]

PATH_DELIMS = set(" \t\"'`=,;()[]{}<>|")

def word_at(line, col):
  """The word around column col of line, delimited by white space, quotes, '=' and brackets"""
  if not (0 <= col < len(line)) or line[col] in PATH_DELIMS:
    return ""
  start, end = col, col
  while start > 0 and line[start-1] not in PATH_DELIMS:
    start -= 1
  while end < len(line) and line[end] not in PATH_DELIMS:
    end += 1
  return line[start:end]

def resolve_path(word, bases=(), env=None):
  """The existing file or directory a word of a text refers to (None if there is none).
  Relative paths are tried against the base directories in order, 'path:123' as path."""
  word = re.sub(r'^\d*[<>]+&?', '', word.strip()).rstrip(".,;:&|")
  if env:
    word = expand_vars(word, env)
  for cand in dict.fromkeys([word, word.split(":")[0]]):
    if not re.search(r'[\w~]', cand) or cand.startswith("-"):
      continue
    cand = fs_expanduser(cand)
    for path in [cand] if os.path.isabs(cand) else [os.path.join(b, cand) for b in bases if b]:
      if fs_exists(path):
        return os.path.normpath(path)
  return None

def is_binary(path):
  try:
    return b"\0" in fs_read(path, size=8192)
  except OSError:
    return False

def script_lines(text):
  """Lines of a batch script that may name files: commands and #SBATCH options, no other comments"""
  lines = []
  for line in text.splitlines()[1:]:
    stripped = line.strip()
    if stripped.startswith("#SBATCH"):
      lines.append(stripped[len("#SBATCH"):])
    elif stripped and not stripped.startswith("#"):
      lines.append(stripped)
  return lines

def read_text(path, limit=1024*1024):
  try:
    return fs_read(path, size=limit).decode("utf-8", errors="replace")
  except OSError:
    return ""

def sacct_all(job):
  """All sacct fields of the job and its steps as 'key: value' blocks"""
  out = run(["sacct", "-j", job.jid, "-P", "-o", "ALL"])
  lines = out.splitlines()
  if len(lines) < 2:
    return out
  header = lines[0].split("|")
  blocks = []
  for line in lines[1:]:
    vals = line.split("|", len(header)-1)
    width = max(map(len, header))
    blocks.append("\n".join(f"{k:<{width}}  {v}" for k, v in zip(header, vals) if v))
  return "\n\n".join(blocks) + "\n"

def load_details(job):
  """Everything shown in the job window that needs extra queries or file system access"""
  d = {"files": [], "sstat": {}, "nodes": [], "progress": output_progress(job.stdout_path()) if job.running else None}
  workdir = job.rec["WorkDir"]
  files = d["files"]

  def add(entry): # checked and deduplicated at the end, all at once
    files.append(entry)

  for kind, path in [("stdout", job.stdout_path()), ("stderr", job.stderr_path())]:
    if path:
      add(Entry(kind, path, path=path))
  if job.merged():
    files.append(Entry("stderr", "goes to the stdout file"))
  if workdir:
    add(Entry("workdir", workdir, path=workdir))
  for path in [job.stdout_path(), job.stderr_path()]:
    if path:
      add(Entry("output dir", os.path.dirname(path), path=os.path.dirname(path)))

  for entry in likely_job_core_dumps(job):
    add(entry)

  submitted = batch_script(job.jid) if job.queued else ""
  if submitted:
    add(Entry("script", "batch script as submitted", text=submitted, note="scontrol write batch_script"))
  if job.queued:
    add(Entry("scontrol", "scontrol show job", text=run(["scontrol", "show", "job", job.jid])))

  on_disk = ""
  submit_line = job.rec["SubmitLine"]
  if submit_line.startswith("sbatch"):
    for path in existing_files([submit_line], workdir):
      on_disk = read_text(path)
      add(Entry("script", path, path=path, note="on disk now, may differ from the submitted one"))
      break

  cmds = [submit_line] + [s["SubmitLine"] for s in job.steps if s["SubmitLine"] and s["JobName"] != STEP_NAME]
  for path in existing_files(cmds, workdir, dirs=True):
    add(Entry("command", path, path=path))
  env = job_vars(job)
  for path in existing_files(script_lines(submitted or on_disk), workdir, env, dirs=True):
    add(Entry("in script", path, path=path))

  infos = fs_infos([e.path for e in files if e.path])
  checked, seen = [], set()
  for e in files:
    info = infos.get(e.path) if e.path else None
    if e.kind == "output dir" and not (info and info["isdir"]):
      continue
    key = info["realpath"] if info else e.path or (e.kind, e.label)
    if key in seen:
      continue
    seen.add(key)
    checked.append(e)
    e.exists = info is not None
    if info and info["isdir"]:
      e.label = e.path.rstrip("/") + "/"
      e.note = e.note or "directory"
    elif e.path and not info:
      e.note = "does not exist (yet)"
    elif info:
      e.note = (e.note or humanize.naturalsize(info["size"])) + (" HDF5" if info["hdf5"] else " binary" if info["binary"] else "")
  files[:] = checked
  add(Entry("sacct", "all accounting fields of the job and its steps", text=None, note="sacct -o ALL"))

  if job.running:
    if job.rec["NodeList"]:
      d["nodes"] = run(["scontrol", "show", "hostnames", job.rec["NodeList"]]).split()
  return d

CORE_DUMP_NAME = re.compile(r"^core_(.+)_(\d+)$")

def likely_job_core_dumps(job):
  """Find top-level core files in a job's workdir that match its node and run time."""
  rec = job.rec
  workdir = rec.get("WorkDir")
  if not workdir or workdir in ("(null)", "None") or not fs_isdir(workdir):
    return []
  try:
    start = datetime.fromisoformat(rec["Start"].replace("Z", "+00:00")).timestamp()
  except (AttributeError, KeyError, TypeError, ValueError):
    return []
  try:
    end = datetime.fromisoformat(rec["End"].replace("Z", "+00:00")).timestamp()
  except (AttributeError, KeyError, TypeError, ValueError):
    if not job.running:
      return []
    end = time.time()

  # NodeList may be a Slurm hostlist expression. Expand it so core_<node>_<pid>
  # names can be checked against the allocation.
  node_list = rec.get("NodeList", "")
  if node_list in ("(null)", "None", "None assigned"):
    node_list = ""
  nodes = set(run(["scontrol", "show", "hostnames", node_list], timeout=10, quiet=True).split()) if node_list else set()
  if not nodes and node_list and not re.search(r"[\[\]]", node_list):
    nodes = set(node_list.split(","))

  found = []
  try:
    with fs_scandir(workdir) as entries:
      for de in entries:
        match = CORE_DUMP_NAME.fullmatch(de.name)
        if not match:
          continue
        node, pid = match.groups()
        if nodes and node not in nodes:
          continue
        try:
          stat = de.stat(follow_symlinks=True)
          if not de.is_file(follow_symlinks=True) or not start - 300 <= stat.st_mtime <= end + 300:
            continue
          if not is_core_dump(de.path):
            continue
        except OSError:
          continue
        confidence = "likely" if nodes and node in nodes else "possible"
        note = f"{confidence} · {node} · PID {pid} · {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}"
        found.append(Entry("core", de.name, path=de.path, note=note))
  except OSError:
    return []
  return sorted(found, key=lambda entry: entry.label)

def run_on_nodes(job, cmd, node=None):
  """Run a shell command once per node inside the allocation of a running job"""
  args = ["srun", f"--jobid={job.rec['JobIDRaw']}", "--overlap", "--ntasks-per-node=1", f"--job-name={STEP_NAME}",
          "--label", "--quiet"]
  args += ["--nodes=1", "-w", node] if node else [f"--nodes={job.rec['NNodes'] or 1}"]
  args += ["bash", "-c", f'echo "== $(hostname)"; {cmd}']
  log.debug(f"running command: {shlex.join(args)}")
  try:
    res = run_cmd(args, timeout=60)
    out, err = res.stdout, res.stderr
  except OSError as e:
    out, err = "", str(e)
  tasks, other = {}, []
  for line in out.splitlines():
    if (m := re.match(r'\s*(\d+): ?(.*)', line)):
      tasks.setdefault(int(m.group(1)), []).append(m.group(2))
    else:
      other.append(line)
  text = [f"$ {cmd}    ({node or 'all nodes'}, {datetime.now():%H:%M:%S})", ""]
  for task in sorted(tasks):
    text += tasks[task] + [""]
  text += other + ([f"srun: {err.strip()}"] if err.strip() else [])
  return "\n".join(text) + "\n"

def memory_probe_command(job_id):
  """Per-node CPU RSS and GPU process-memory probe, limited to this job's PIDs."""
  return (
    f"pids=$(scontrol listpids {shlex.quote(str(job_id))} 2>/dev/null | "
    "awk -v self=\"$SLURM_STEP_ID\" 'NR > 1 && $3 != self { printf \"%s%s\", sep, $1; sep=\",\" }'); "
    "if [ -n \"$pids\" ]; then "
    "cpu_kib=$(ps -o rss= -p \"$pids\" 2>/dev/null | awk '{sum += $1} END {print sum+0}'); "
    "printf 'CPU_KIB=%s\\n' \"$cpu_kib\"; printf 'JOB_PIDS=%s\\n' \"$pids\"; "
    "else printf 'CPU_UNAVAILABLE=no job process IDs found\\n'; printf 'JOB_PIDS=\\n'; fi; "
    "if command -v nvidia-smi >/dev/null 2>&1; then "
    "if gpu_output=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null); then "
    "printf '%s\\n' \"$gpu_output\" | sed 's/^/GPU=/'; "
    "else printf 'GPU_UNAVAILABLE=nvidia-smi query failed\\n'; fi; "
    "else printf 'GPU_UNAVAILABLE=nvidia-smi not found\\n'; fi"
  )

def current_memory_usage(job):
  """Query current process RSS and per-job GPU usage; called only by the detail button."""
  fields = ["JobID", "MaxRSS", "AveRSS"]
  rows = parsable(run(["sstat", "-a", "-j", job.rec["JobIDRaw"], "-P", "-n", "-o", ",".join(fields)]), fields)
  sstat = {row["JobID"].partition(".")[2]: row for row in rows}
  output = run_on_nodes(job, memory_probe_command(job.rec["JobIDRaw"]))
  nodes = {}
  node = None
  for line in output.splitlines():
    if line.startswith("== "):
      node = line[3:].strip()
      nodes[node] = {"cpu_kib": None, "pids": set(), "gpu_mib": 0, "gpu_error": ""}
    elif node and line.startswith("CPU_KIB="):
      try:
        nodes[node]["cpu_kib"] = int(line.partition("=")[2])
      except ValueError:
        pass
    elif node and line.startswith("CPU_UNAVAILABLE="):
      nodes[node]["cpu_error"] = line.partition("=")[2]
    elif node and line.startswith("JOB_PIDS="):
      nodes[node]["pids"] = {pid for pid in line.partition("=")[2].split(",") if pid.isdigit()}
    elif node and line.startswith("GPU_UNAVAILABLE="):
      nodes[node]["gpu_error"] = line.partition("=")[2]
    elif node and line.startswith("GPU="):
      pid, sep, memory = line.partition("=")[2].partition(",")
      pid = pid.strip()
      match = re.match(r"\s*(\d+)", memory)
      if sep and pid in nodes[node]["pids"] and match:
        nodes[node]["gpu_mib"] += int(match.group(1))

  cpu_values = [node_data["cpu_kib"] for node_data in nodes.values() if node_data["cpu_kib"] is not None]
  cpu_bytes = sum(cpu_values) * 1024 if cpu_values else None
  cpu_source = "ps"
  if cpu_bytes is None:
    step_counts = {step["JobID"].partition(".")[2]: int(step["NTasks"] or 0) for step in job.steps}
    estimates = [sl.sacct_bytes(row.get("AveRSS") or "") * step_counts.get(step, 0)
                 for step, row in sstat.items()]
    estimates = [value for value in estimates if value]
    if estimates:
      cpu_bytes = sum(estimates)
      cpu_source = "sstat estimate"
  cpu_partial = len(cpu_values) < len(nodes)
  gpu_nodes = [node_data for node_data in nodes.values() if not node_data["gpu_error"]]
  gpu_bytes = sum(node_data["gpu_mib"] for node_data in gpu_nodes) * 1024**2 if gpu_nodes else None
  gpu_partial = len(gpu_nodes) < len(nodes)
  return {"cpu_bytes": cpu_bytes, "cpu_source": cpu_source, "cpu_partial": cpu_partial, "gpu_bytes": gpu_bytes,
          "gpu_partial": gpu_partial, "gpu_errors": [d["gpu_error"] for d in nodes.values() if d["gpu_error"]],
          "sstat": sstat}

def peak_job_memory(job):
  """Slurm-accounted CPU and (when configured) GPU peak memory for an ended job."""
  records = [job.rec, *job.steps]
  cpu_peak = max((sl.sacct_bytes(row.get("MaxRSS") or "") for row in records), default=0)
  gpu_peaks = []
  for row in records:
    for tres in (row.get("TRESUsageInMax") or "").split(","):
      key, sep, value = tres.partition("=")
      if sep and key.strip().lower().endswith("gpumem"):
        gpu_peaks.append(sl.sacct_bytes(value.strip()))
  return cpu_peak, max(gpu_peaks, default=0)

# ---------------------------------------------------------------------------
# widgets and screens

STATE_STYLES = {"RUNNING": "green", "COMPLETED": "green", "COMPLETING": "green", "CONFIGURING": "green",
                "PENDING": "blue", "REQUEUED": "blue", "SUSPENDED": "yellow"}

def state_text(state):
  return Text(state, style=STATE_STYLES.get(state.split(" ")[0], "red"))

def err_text(err):
  kind, size = err
  if kind == "file":
    return Text(f"! {humanize.naturalsize(size)}", style="bold red") if size else Text("")
  if kind == "merged":
    return Text("(stdout)", style="dim")
  if kind == "missing":
    return Text("?", style="yellow")
  return Text("")

class ClickTable(DataTable):
  """Row table where a single click selects (opens) a row and remembers the clicked column"""
  clicked_column = None

  async def _on_click(self, event):
    meta = event.style.meta
    row, col = meta.get("row"), meta.get("column")
    if row is None or col is None or row < 0 or col < 0 or self.cursor_type != "row":
      return # header clicks etc.: Textual calls DataTable._on_click next (handlers run along the MRO)
    event.prevent_default() # but not for clicks on rows, they are handled here
    event.stop()
    self.clicked_column = self.ordered_columns[col].key.value
    self.move_cursor(row=row)
    self.post_message(self.RowSelected(self, row, self.coordinate_to_cell_key(Coordinate(row, col)).row_key))

  def take_clicked_column(self):
    col, self.clicked_column = self.clicked_column, None
    return col

def clean(text):
  """Remove terminal escape sequences and lines overwritten with carriage returns"""
  text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07', '', text)
  return re.sub(r'[^\n]*\r(?!\n)', '', text.replace("\r\n", "\n"))

def crumb_title(crumbs):
  """Breadcrumbs for the header, shortened in the middle when they get long"""
  crumbs = list(crumbs)
  if len(crumbs) > 5:
    crumbs = crumbs[:2] + ["…"] + crumbs[-2:]
  return " › ".join(crumbs)

class PathArea(TextArea):
  """Read-only text where a click on the name of an existing file or directory opens it"""
  class PathClicked(Message):
    """A word was clicked; the screen looks it up (in the background) and opens it if it names a file or directory"""
    def __init__(self, word):
      super().__init__()
      self.word = word

  def word_at_cursor(self):
    if self.selected_text.strip():
      return self.selected_text.strip()
    row, col = self.cursor_location
    return self.word_at(row, col)

  def word_at(self, row, col):
    if row >= self.document.line_count:
      return None
    line = self.document.get_line(row)
    return word_at(line, col) or word_at(line, col - 1) # also just behind the end of a word

  def on_click(self, event):
    if event.chain > 1 or self.selected_text: # double clicks and dragging select text
      return
    row, col = self.get_target_document_location(event)
    word = self.word_at(row, col)
    if word:
      self.post_message(self.PathClicked(word))

@dataclass
class FileView:
  """A specialized viewer selected by inspecting a file, before the text fallback."""
  name: str
  matches: object
  open: object

FILE_VIEWS = []

def register_file_view(name, matches, open_view):
  FILE_VIEWS.append(FileView(name, matches, open_view))

def open_path(app, crumbs, path, bases=(), env=None, follow=False):
  """Open a directory or dispatch a file to its first matching specialized view. What the path is (and what the
  view needs) is found out in a worker: on a remote cluster that takes round trips, which must not block the UI.
  A view's open() runs in the worker and returns what to do on the UI thread."""
  def work():
    if fs_isdir(path):
      return lambda: app.push_screen(DirScreen(crumbs, path, bases=bases, env=env))
    if not fs_isfile(path):
      return lambda: app.notify(f"{path} does not exist (yet)", severity="warning")
    for view in FILE_VIEWS:
      if view.matches(path):
        return view.open(app, crumbs, path, bases, env)
    if is_binary(path):
      return lambda: app.notify(f"{path} is a binary file", severity="warning")
    more_bases = list(dict.fromkeys([*bases, os.path.dirname(path)]))
    return lambda: app.push_screen(FileScreen([*crumbs, os.path.basename(path)], path=path, follow=follow,
                                              bases=more_bases, env=env))
  in_background(app, work, lambda show: show() if show else None, group="open", name=f"opening {path}")

def open_resolved(screen, word, crumbs, bases, env, quiet=True):
  """Open the file or directory a word of a text names (looked up in the background)"""
  def opened(path):
    if path:
      open_path(screen.app, crumbs, path, bases, env)
    elif not quiet:
      screen.notify(f"no existing file or directory named {word}", severity="warning")
  in_background(screen, lambda: resolve_path(word, bases, env) or "", opened, group="resolve")

def open_hdf5(app, crumbs, path, bases=(), env=None):
  """Browse an HDF5 file, or without h5py show its structure as printed by h5dump -H (runs in a worker)"""
  try:
    import h5py # noqa: F401
  except ImportError:
    if command_exists("h5dump"):
      cmd = ["h5dump", "-H", path]
      text = run(cmd)
      def show():
        app.push_screen(FileScreen([*crumbs, os.path.basename(path)], text=text, refresher=lambda: run(cmd), bases=bases, env=env))
        app.notify("h5py is not installed: showing the structure printed by h5dump -H", severity="warning")
      return show
    return lambda: app.notify(f"{path} is an HDF5 file: install h5py to browse it", severity="warning")
  return lambda: app.push_screen(H5Screen(crumbs, path, bases=bases, env=env))

def is_markdown(path):
  return os.path.splitext(path)[1].lower() in {".md", ".markdown", ".mdown", ".mdx"}

def open_markdown(app, crumbs, path, bases=(), env=None):
  """(runs in a worker)"""
  try:
    markdown = fs_read(path).decode("utf-8", errors="replace")
  except OSError as e:
    return lambda: app.notify(f"cannot read {path}: {e}", severity="error")
  return lambda: app.push_screen(MarkdownScreen(crumbs, path, markdown, bases=bases, env=env))

class SplitScreen(Screen):
  """Window with its content on the left and, while a shell is open, the terminal pane on the right"""
  SHOW_BACK = False

  def compose(self) -> ComposeResult:
    with Horizontal(id="cluster-tabs"):
      for index, cluster in enumerate(self.app.clusters):
        variant = "primary" if index == self.app.cluster_index else "default"
        yield Button(cluster.name, id=f"cluster-tab-{index}", classes="cluster-tab", variant=variant, compact=True)
        yield Button("✕", id=f"cluster-close-{index}", classes="cluster-close", compact=True, tooltip="close the tab")
      yield Button("+", id="cluster-add", tooltip="Add a local or SSH cluster", compact=True)
    with Horizontal(id="topbar"):
      if self.SHOW_BACK:
        yield Button("←", id="back", compact=True, tooltip="Back")
      yield Header()
    if isinstance(self, MainScreen):
      yield Static("No tabs. The + button above adds a connection (blank for this machine, or an SSH destination).", id="notabs")
    with Horizontal(classes="split"):
      with Vertical(classes="left"):
        yield from self.compose_left()
      yield Splitter()
      yield TerminalPane()
    with Horizontal(id="bottombar"):
      yield Footer()
      yield Static(id="loadstatus")
      yield ConnectionStatus(id="connstatus")

  def on_mount(self):
    self.call_after_refresh(self.app.refresh_status)

  @on(Button.Pressed)
  def back_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "cluster-add":
      event.stop()
      self.app.push_screen(AddClusterScreen(), self.app.add_cluster)
      return
    if event.button.id and event.button.id.startswith("cluster-tab-"):
      event.stop()
      self.app.select_cluster(int(event.button.id.rsplit("-", 1)[1]))
      return
    if event.button.id and event.button.id.startswith("cluster-close-"):
      event.stop()
      self.app.confirm_close_cluster(int(event.button.id.rsplit("-", 1)[1]))
      return
    if event.button.id != "back":
      return
    event.stop()
    self.action_back()

  def compose_left(self):
    return []

  def pop_back(self):
    self.app.pop_screen()
    # pop_screen completes after this key action; queue a full redraw behind it.
    self.app.call_later(self.app.refresh, layout=True)

class MarkdownScreen(SplitScreen):
  """Render a Markdown file with Textual's viewer and table of contents."""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back"), Binding("r", "reload", "Reload")]

  def __init__(self, crumbs, path, markdown, bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.markdown = markdown
    self.bases, self.env = list(bases), env

  def compose_left(self):
    yield MarkdownViewer(self.markdown, id="markdown-viewer", show_table_of_contents=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#markdown-viewer", MarkdownViewer).focus()

  def action_reload(self):
    def read():
      try:
        return fs_read(self.path).decode("utf-8", errors="replace")
      except OSError as e:
        self.app.call_from_thread(self.notify, f"cannot read {self.path}: {e}", title=tab_name(), severity="error")
        return None
    def show(markdown):
      if markdown is not None:
        self.markdown = markdown
        self.query_one("#markdown-viewer", MarkdownViewer).update(markdown)
    in_background(self, read, show, group="reload")

  def action_back(self):
    self.pop_back()

DIR_COLUMNS = [("name", "Name"), ("size", "Size"), ("mtime", "Modified")]
DIR_LIMIT = 20000 # entries listed at most

class DirScreen(SplitScreen):
  """Directory browser: click (or Enter) a subdirectory to enter it, a file to open it"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("backspace,u", "parent", "Parent"),
    Binding("slash", "filter", "Filter"),
    Binding("f", "follow", "Follow"),
    Binding("r", "reload", "Reload"),
    Binding("full_stop", "hidden", "Hidden files"),
  ]

  def __init__(self, crumbs, path, bases=(), env=None):
    super().__init__()
    self.path = os.path.normpath(path)
    self.crumbs = [*crumbs, self.path.rstrip("/") + "/"]
    self.bases, self.env = list(bases), env # passed on to the files opened from here (e.g. the job's WorkDir)
    self.entries = []
    self.pattern = ""
    self.show_hidden = True
    self.following = False
    self.sort_column, self.sort_reverse = "name", False

  def compose_left(self):
    yield Static(id="dstatus")
    yield ClickTable(id="dir", cursor_type="row", zebra_stripes=True)
    yield Input(id="dfilter", placeholder="filter names (Enter: apply, Esc: clear)")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    table = self.query_one("#dir", ClickTable)
    for key, label in DIR_COLUMNS:
      table.add_column(label, key=key)
    self.timer = self.set_interval(2.0, self.action_reload, pause=True)
    self.query_one("#dstatus", Static).update(f"{self.path} · listing …")
    self.action_reload()
    table.focus()

  def scan(self):
    entries = []
    try:
      with fs_scandir(self.path) as it:
        for i, de in enumerate(it):
          if i >= DIR_LIMIT:
            break
          try:
            st = de.stat()
            isdir = de.is_dir()
          except OSError:
            st, isdir = None, False
          entries.append({"name": de.name, "dir": isdir, "size": st.st_size if st and not isdir else None,
                          "mtime": st.st_mtime if st else 0, "link": de.is_symlink()})
    except OSError as e:
      self.app.call_from_thread(self.notify, f"cannot list {self.path}: {e}", title=tab_name(), severity="error")
    return entries

  scanning = False

  def action_reload(self):
    """List the directory in the background (also every 2 s while following, one listing at a time)"""
    if self.scanning:
      return
    self.scanning = True
    def listed(entries):
      self.scanning = False
      if entries is not None:
        self.entries = entries
        self.populate()
    in_background(self, self.scan, listed, group="scan")

  def populate(self):
    table = self.query_one("#dir", ClickTable)
    current = None
    if table.row_count:
      try:
        current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
      except Exception:
        pass
    table.clear()
    if self.path != "/":
      table.add_row(Text("../", style="bold"), "", "", key="..")
    keys = {"name": lambda e: e["name"].lower(), "size": lambda e: e["size"] or 0, "mtime": lambda e: e["mtime"]}
    shown = [e for e in self.entries if (self.show_hidden or not e["name"].startswith("."))
             and self.pattern.lower() in e["name"].lower()]
    shown.sort(key=keys[self.sort_column], reverse=self.sort_reverse)
    shown.sort(key=lambda e: not e["dir"]) # directories first
    for e in shown:
      style = "bold blue" if e["dir"] else ""
      if e["name"].startswith("."):
        style += " dim"
      name = Text(e["name"] + ("/" if e["dir"] else "") + (" @" if e["link"] else ""), style=style.strip())
      size = humanize.naturalsize(e["size"]) if e["size"] is not None else ""
      mtime = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M:%S") if e["mtime"] else ""
      table.add_row(name, size, mtime, key=e["name"])
    if current is not None:
      try:
        table.move_cursor(row=table.get_row_index(current))
      except Exception:
        pass
    status = f"{self.path} · {len(shown)} of {len(self.entries)} entries"
    if len(self.entries) >= DIR_LIMIT:
      status += f" (only the first {DIR_LIMIT} are listed)"
    if self.pattern:
      status += f" · filter '{self.pattern}'"
    status += " · following (f)" if self.following else ""
    self.query_one("#dstatus", Static).update(status)

  def open(self, name):
    path = os.path.dirname(self.path) if name == ".." else os.path.join(self.path, name)
    open_path(self.app, self.crumbs, path, bases=self.bases, env=self.env)

  @on(DataTable.RowSelected, "#dir")
  def selected(self, event):
    event.stop()
    self.query_one("#dir", ClickTable).take_clicked_column()
    if event.row_key.value is not None:
      self.open(event.row_key.value)

  @on(DataTable.HeaderSelected, "#dir")
  def sort_by(self, event):
    event.stop()
    col = event.column_key.value
    if col == self.sort_column:
      self.sort_reverse = not self.sort_reverse
    else:
      self.sort_column, self.sort_reverse = col, col != "name"
    self.populate()

  def action_back(self):
    search = self.query_one("#dfilter", Input)
    if search.has_class("visible"):
      search.remove_class("visible")
      self.pattern = ""
      self.populate()
      self.query_one("#dir").focus()
    else:
      self.pop_back()

  def action_parent(self):
    if self.path != "/":
      self.open("..")

  def action_filter(self):
    search = self.query_one("#dfilter", Input)
    search.add_class("visible")
    search.value = self.pattern
    search.focus()

  @on(Input.Changed, "#dfilter")
  def filter_changed(self, event):
    self.pattern = event.value
    self.populate()

  @on(Input.Submitted, "#dfilter")
  def filter_submitted(self, event):
    event.input.remove_class("visible")
    self.query_one("#dir").focus()

  def action_follow(self):
    self.following = not self.following
    (self.timer.resume if self.following else self.timer.pause)()
    self.populate()

  def action_hidden(self):
    self.show_hidden = not self.show_hidden
    self.populate()

class FileScreen(SplitScreen):
  """Read-only viewer for a file (with tail -f style following) or for text (optionally re-run by a refresher)"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("enter", "open_path", "Open path"),
    Binding("f", "follow", "Follow"),
    Binding("slash", "search", "Search"),
    Binding("n", "find(True)", "Next", show=False),
    Binding("N", "find(False)", "Prev", show=False),
    Binding("g", "top", "Top"),
    Binding("G", "bottom", "Bottom"),
    Binding("r", "reload", "Reload"),
    Binding("w", "watch", "Watch"),
    Binding("A", "load_all", "Load all", show=False),
    Binding("l", "less", "less"),
  ]

  def __init__(self, crumbs, path=None, text=None, refresher=None, follow=False, bases=(), env=None):
    super().__init__()
    self.crumbs, self.path, self.text, self.refresher = crumbs, path, text, refresher
    self.bases, self.env = list(bases), env # to resolve relative paths in the text
    self.following = follow and path is not None
    self.watching = False
    self.full = False
    self.file_pos = 0
    self.truncated = False
    self.term = ""
    self.decoder = None
    self.timer = None

  def compose_left(self):
    yield Static(id="fstatus")
    area = PathArea(id="text", read_only=True, show_line_numbers=True, soft_wrap=False, highlight_cursor_line=False)
    yield area
    yield Input(id="search", placeholder="search (Enter: find, Esc: cancel)")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.load()
    self.timer = self.set_interval(1.0, self.poll, pause=not self.following)
    self.rerun_timer = self.set_interval(5.0, self.rerun, pause=True)
    self.query_one("#text").focus()

  def check_action(self, action, parameters):
    if action == "follow" or action == "load_all":
      return self.path is not None
    if action == "watch":
      return self.refresher is not None
    return True

  @property
  def area(self):
    return self.query_one("#text", TextArea)

  file_size = None # as last read

  def update_status(self):
    parts = []
    if self.path:
      if self.file_size is None:
        parts.append(f"{self.path}  (not readable)" if self.decoder else f"{self.path} · loading …")
      else:
        parts.append(f"{self.path}  {humanize.naturalsize(self.file_size)}")
      if self.truncated:
        parts.append(f"showing the last {humanize.naturalsize(TAIL_BYTES)} (A: load all)")
      parts.append("following" if self.following else "not following (f)")
    if self.refresher:
      parts.append("watching every 5 s" if self.watching else "w: watch, r: run again")
    self.query_one("#fstatus", Static).update(" · ".join(parts))

  def load(self):
    """Show the text, or read the file (its end if it is large) in the background"""
    if self.path is None:
      self.area.load_text(clean(self.text or ""))
      self.update_status()
      return
    full = self.full
    def read():
      try:
        size = fs_getsize(self.path)
        truncated = not full and size > TAIL_BYTES
        offset = size - TAIL_BYTES if truncated else 0
        data = fs_read(self.path, offset, size - offset)
        return size, truncated, offset + len(data), data
      except OSError as e:
        return None, False, 0, f"cannot read {self.path}: {e}\n".encode()
    in_background(self, read, self.file_read, group="load")
    self.update_status()

  def file_read(self, result):
    if result is None:
      return
    area = self.area
    self.file_size, self.truncated, self.file_pos, data = result
    self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    if self.truncated:
      data = data.partition(b"\n")[2] # drop the partial first line
    area.load_text(clean(self.decoder.decode(data)))
    area.history.clear()
    self.action_bottom()
    self.update_status()

  polling = False

  def poll(self):
    """Append what was written to the file since the last poll. Read in a worker: on a remote cluster it takes
    round trips, which must not block the UI."""
    if self.polling:
      return
    self.polling, pos = True, self.file_pos
    def read():
      size, data = None, b""
      try:
        size = fs_getsize(self.path)
        if size > pos:
          data = fs_read(self.path, pos, TAIL_BYTES)
      except OSError:
        pass
      finally:
        self.app.call_from_thread(self.append, pos, size, data)
    start_worker(self, read, "poll", exclusive=False)

  def append(self, pos, size, data):
    self.polling = False
    if size is None or pos != self.file_pos or not self.is_mounted or self.decoder is None: # unreadable, reloaded
      return
    self.file_size = size
    if size < pos: # truncated or replaced
      self.load()
      return
    if not data:
      return
    self.file_pos += len(data)
    area = self.area
    at_end = area.scroll_y >= area.max_scroll_y - 1
    area.insert(clean(self.decoder.decode(data)), area.document.end, maintain_selection_offset=True)
    area.history.clear()
    if at_end:
      area.scroll_end(animate=False)
    self.update_status()

  def action_back(self):
    search = self.query_one("#search", Input)
    if search.has_class("visible"):
      search.remove_class("visible")
      self.area.focus()
    else:
      self.pop_back()

  @on(PathArea.PathClicked)
  def path_clicked(self, event):
    open_resolved(self, event.word, self.crumbs, self.bases, self.env)

  def action_open_path(self):
    word = self.area.word_at_cursor()
    if word:
      open_resolved(self, word, self.crumbs, self.bases, self.env, quiet=False)
    else:
      self.notify("no file or directory name at the cursor", severity="warning")

  def action_follow(self):
    self.following = not self.following
    if self.following:
      self.poll()
      self.action_bottom()
      self.timer.resume()
    else:
      self.timer.pause()
    self.update_status()

  def action_top(self):
    self.area.move_cursor((0, 0))
    self.area.scroll_home(animate=False)

  def action_bottom(self):
    area = self.area
    area.move_cursor((area.document.line_count - 1, 0))
    area.scroll_end(animate=False)

  def action_load_all(self):
    self.full = True
    self.load()

  def action_reload(self):
    if self.refresher:
      self.rerun()
    else:
      self.load()

  def action_watch(self):
    self.watching = not self.watching
    (self.rerun_timer.resume if self.watching else self.rerun_timer.pause)()
    self.update_status()

  @cluster_work("rerun")
  def rerun(self):
    text = self.refresher()
    self.app.call_from_thread(self.show_text, text)

  def show_text(self, text):
    self.text = text
    self.load()

  def action_search(self):
    search = self.query_one("#search", Input)
    search.add_class("visible")
    search.value = self.term
    search.focus()

  @on(Input.Submitted, "#search")
  def search_submitted(self, event):
    event.input.remove_class("visible")
    self.term = event.value
    self.area.focus()
    self.action_find(True)

  def action_find(self, forward):
    if not self.term:
      self.action_search()
      return
    area, term = self.area, self.term.lower()
    doc = area.document
    n = doc.line_count
    sel = area.selection
    row, col = (max(sel.start, sel.end) if forward else min(sel.start, sel.end))
    for i in range(n + 1):
      r = (row + i) % n if forward else (row - i) % n
      line = doc.get_line(r).lower()
      if forward:
        c = line.find(term, col if i == 0 else 0)
      else:
        c = line.rfind(term, 0, col if i == 0 else len(line))
        c = -1 if (i == 0 and c >= col) else c
      if c >= 0:
        area.selection = Selection((r, c), (r, c + len(term)))
        area.scroll_cursor_visible(center=True)
        return
    self.notify(f"'{self.term}' not found", severity="warning")

  def action_less(self):
    path = self.path
    if path is None:
      import tempfile
      with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write(self.text or "")
        path = tmp.name
    try:
      with self.app.suspend():
        args = ["less", "-R", "+F" if self.following else "+G", path]
        subprocess.run(conn().command_argv(args, interactive=True) if conn() else args)
    except SuspendNotSupported:
      self.notify("cannot suspend the terminal here", severity="error")
    finally:
      if path != self.path:
        os.unlink(path)
    if self.path:
      self.poll()

HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
H5_ROWS = 500       # rows of a dataset shown at a time
H5_COLS = 50        # columns (or fields of a compound type) shown at a time
H5_MEMBERS = 5000   # members of a group listed at most

def _is_hdf5_local(path):
  """HDF5 signature at the start of the file or behind a user block (512, 1024, 2048, ... bytes)"""
  try:
    offset = 0
    while offset <= 1 << 20:
      sig = fs_read(path, offset, 8)
      if sig == HDF5_SIGNATURE:
        return True
      if len(sig) < 8:
        return False
      offset = 512 if offset == 0 else 2*offset
  except OSError:
    pass
  return False

def is_hdf5(path):
  return conn().is_hdf5(path) if conn() else _is_hdf5_local(path)

def is_core_dump(path):
  """Whether path is an ELF core file (not merely a file named 'core')."""
  try:
    header = fs_read(path, size=18)
    if len(header) < 18 or header[:4] != b"\x7fELF":
      return False
    byte_order = {1: "little", 2: "big"}.get(header[5])
    return byte_order is not None and int.from_bytes(header[16:18], byte_order) == 4 # ET_CORE
  except OSError:
    return False

def open_h5(path):
  """Open read-only without file locking, so files that a running job writes can be read too"""
  import h5py
  try:
    return h5py.File(path, "r", locking=False)
  except TypeError: # h5py < 3.5
    return h5py.File(path, "r")

def parse_selection(text, shape):
  """'[3, :, 10:20]' -> tuple with an int or a slice for every axis ('...' and missing trailing axes: all)"""
  text = text.strip()
  if text.startswith("[") and text.endswith("]"):
    text = text[1:-1]
  parts = [p.strip() for p in text.split(",")] if text.strip() else []
  if parts.count("...") > 1:
    raise ValueError("only one ... allowed")
  if "..." in parts:
    i = parts.index("...")
    parts[i:i + 1] = [":"] * max(0, len(shape) - len(parts) + 1)
  if len(parts) > len(shape):
    raise ValueError(f"the dataset has {len(shape)} dimensions, not {len(parts)}")
  parts += [":"] * (len(shape) - len(parts))
  sel = []
  for p, n in zip(parts, shape):
    items = p.split(":")
    try:
      nums = [int(x) if x.strip() else None for x in items]
    except ValueError:
      nums = None
    if nums is None or len(nums) > 3 or (len(nums) == 1 and nums[0] is None):
      raise ValueError(f"'{p}' is not an index or a slice like 10:20 or ::2")
    if len(nums) == 1:
      if not -n <= nums[0] < n:
        raise ValueError(f"index {nums[0]} is out of range for size {n}")
      sel.append(nums[0] % n)
    else:
      nums += [None] * (3 - len(nums))
      if nums[2] is not None and nums[2] < 1:
        raise ValueError(f"only positive steps are supported: '{p}'")
      sel.append(slice(*nums))
  return tuple(sel)

def window(sel, shape, row0=0, col0=0):
  """Restrict the (at most two) sliced axes of a selection to the rows and columns shown.
  Returns the new selection and for each sliced axis (indices shown, number of indices selected)."""
  sel = list(sel)
  axes = [k for k, s in enumerate(sel) if isinstance(s, slice)]
  if len(axes) > 2:
    raise ValueError(f"{len(axes)} dimensions selected: fix all but two with an index, e.g. [0, :, :]")
  shown = []
  for k, start, limit in zip(axes, (row0, col0), (H5_ROWS, H5_COLS)):
    full = range(*sel[k].indices(shape[k]))
    part = full[start:start + limit]
    sel[k] = slice(part[0], part[-1] + 1, part.step) if len(part) else slice(0, 0)
    shown.append((part, len(full)))
  return tuple(sel), shown

def h5_value(v, width=300):
  """Short text for a value of an attribute or a cell"""
  import numpy as np
  if isinstance(v, bytes):
    s = v.decode("utf-8", errors="replace")
  elif isinstance(v, np.ndarray):
    if v.dtype.kind == "S":
      v = np.char.decode(v, "utf-8", errors="replace")
    s = np.array2string(v, threshold=20, edgeitems=3, max_line_width=10**6)
  else:
    s = str(v)
  s = s.replace("\n", " ")
  return s if len(s) <= width else s[:width - 1] + "…"

def h5_link_target(path, filename):
  """File name of an external link, relative to the file that has the link"""
  return os.path.normpath(os.path.join(os.path.dirname(path), fs_expanduser(filename)))

class H5Screen(SplitScreen):
  """HDF5 browser: tree of groups and datasets, info and attributes of the selected one, and a table of its data"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("s", "selection", "Selection"),
    Binding("left_square_bracket", "page(-1, 0)", "Rows ↑", key_display="["),
    Binding("right_square_bracket", "page(1, 0)", "Rows ↓", key_display="]"),
    Binding("left_curly_bracket", "page(0, -1)", "Cols ←", key_display="{"),
    Binding("right_curly_bracket", "page(0, 1)", "Cols →", key_display="}"),
    Binding("r", "reload", "Reload"),
  ]

  def __init__(self, crumbs, path, start="/", bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.start = start
    self.bases, self.env = list(bases), env
    self.file = None
    self.local_path = None
    self.current = None # data of the selected tree node
    self.dataset_selections = {} # dataset path -> selection text typed for it
    self.row0 = self.col0 = 0

  def compose_left(self):
    yield Static(id="hstatus")
    with Horizontal(id="hbody"):
      yield Tree(os.path.basename(self.path), id="htree", data={"path": "/", "kind": "group"})
      with Vertical(id="hright"):
        with VerticalScroll(id="hinfo"):
          yield Static(id="hinfotext")
        yield Input(id="hslice", placeholder="selection, e.g. [0, :, 10:20] or [..., 3] (Enter: apply)")
        yield Static(id="hpage")
        yield DataTable(id="hdata", zebra_stripes=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#htree", Tree).show_root = True
    self.action_reload(first=True)
    self.query_one("#htree").focus()

  def on_unmount(self):
    self.gone = True
    self.close_file()

  gone = False

  def close_file(self):
    if self.file is not None:
      try:
        self.file.close()
      except Exception:
        pass
      self.file = None
    if self.local_path:
      try: os.unlink(self.local_path)
      except OSError: pass
      self.local_path = None

  # --------------------------------------------------------------- tree
  def action_reload(self, first=False):
    """(Re)open the file and rebuild the tree with the same groups expanded and the same item selected"""
    tree = self.query_one("#htree", Tree)
    expanded = set()
    if not first:
      stack = [tree.root]
      while stack:
        node = stack.pop()
        if node.is_expanded and node.data and node.data["kind"] == "group":
          expanded.add(node.data["path"])
        stack.extend(node.children)
    target = self.start if first else (self.current or {}).get("path", "/")
    self.query_one("#hstatus", Static).update(f"{self.path} · {'downloading' if conn() else 'opening'} …")
    def fetch():
      """the size, and on a remote cluster a local copy (h5py reads local files)"""
      try:
        size = fs_getsize(self.path)
        local = conn().download(self.path, suffix=os.path.splitext(self.path)[1]) if conn() else None
        if local and self.gone: # the window was closed meanwhile
          os.unlink(local)
          local = None
        return size, local, ""
      except OSError as e:
        return None, None, str(e)
    in_background(self, fetch, lambda result: self.fetched(result, expanded, target), group="fetch")

  def fetched(self, result, expanded, target):
    size, local_path, error = result or (None, None, "failed")
    tree = self.query_one("#htree", Tree)
    self.close_file()
    self.local_path = local_path
    tree.clear()
    tree.root.data = {"path": "/", "kind": "group"}
    try:
      if error:
        raise OSError(error)
      self.file = open_h5(local_path or self.path)
    except Exception as e:
      self.query_one("#hstatus", Static).update(f"{self.path} · cannot open: {e}")
      self.show_message(f"cannot open {self.path}:\n{e}")
      return
    size = humanize.naturalsize(size)
    self.query_one("#hstatus", Static).update(f"{self.path}  {size} · r: reload (e.g. while a job writes it)")
    self.load_children(tree.root)
    tree.root.expand()
    for p in sorted(expanded, key=lambda p: p.count("/")):
      node = self.find_node(p)
      if node is not None:
        self.load_children(node)
        node.expand()
    self.goto(target)

  def find_node(self, path, expand=False):
    """Tree node of a path; with expand, the groups on the way are expanded"""
    node = self.query_one("#htree", Tree).root
    for name in [p for p in path.split("/") if p]:
      self.load_children(node)
      if expand:
        node.expand()
      child = next((c for c in node.children if c.data and c.data.get("name") == name), None)
      if child is None:
        return node if expand else None
      node = child
    return node

  def goto(self, path):
    node = self.find_node(path or "/", expand=True)
    tree = self.query_one("#htree", Tree)
    self.call_after_refresh(lambda: tree.move_cursor(node))
    self.show(node.data)

  def load_children(self, node):
    import h5py
    data = node.data
    if not data or data["kind"] != "group" or data.get("loaded") or self.file is None:
      return
    data["loaded"] = True
    try:
      group = self.file[data["path"]]
      names = list(group)
    except Exception as e:
      node.add_leaf(Text(f"cannot list: {e}", style="red"), data={"path": data["path"], "kind": "error", "error": str(e)})
      return
    for name in names[:H5_MEMBERS]:
      path = data["path"].rstrip("/") + "/" + name
      item = {"path": path, "name": name}
      try:
        link = group.get(name, getlink=True)
      except Exception:
        link = None
      if isinstance(link, h5py.ExternalLink):
        item.update(kind="external", file=link.filename, target=link.path)
        node.add_leaf(Text.assemble((name, "bold magenta"), (f"  ⇢ {link.filename}:{link.path}", "dim")), data=item)
        continue
      soft = f"  → {link.path}" if isinstance(link, h5py.SoftLink) else ""
      try:
        obj = group[name]
      except Exception as e:
        item.update(kind="error", error=f"broken link{soft}: {e}")
        node.add_leaf(Text.assemble((name, "red"), (soft or "  (cannot open)", "dim")), data=item)
        continue
      if isinstance(obj, h5py.Group):
        item["kind"] = "group"
        node.add(Text.assemble((name + "/", "bold blue"), (soft, "dim")), data=item)
      elif isinstance(obj, h5py.Dataset):
        item["kind"] = "dataset"
        shape = "scalar" if obj.shape == () else "empty" if obj.shape is None else "×".join(map(str, obj.shape))
        node.add_leaf(Text.assemble(name, (soft, "dim"), (f"  {shape} {obj.dtype}", "dim")), data=item)
      else:
        item["kind"] = "datatype"
        node.add_leaf(Text.assemble(name, (soft, "dim"), ("  type", "dim")), data=item)
    if len(names) > H5_MEMBERS:
      node.add_leaf(Text(f"… {len(names) - H5_MEMBERS} more members not listed", style="dim"), data=None)

  @on(Tree.NodeExpanded, "#htree")
  def expanded(self, event):
    event.stop()
    self.load_children(event.node)

  @on(Tree.NodeHighlighted, "#htree")
  def highlighted(self, event):
    event.stop()
    if event.node.data and event.node.data is not self.current:
      self.show(event.node.data)

  @on(Tree.NodeSelected, "#htree")
  def selected(self, event):
    event.stop()
    data = event.node.data
    if data and data["kind"] == "external":
      target = h5_link_target(self.path, data["file"])
      def checked(ok):
        if ok:
          self.app.push_screen(H5Screen(self.crumbs, target, start=data["target"], bases=self.bases, env=self.env))
        elif ok is not None:
          self.notify(f"{target} of the external link does not exist or is no HDF5 file", severity="warning")
      in_background(self, lambda: fs_isfile(target) and is_hdf5(target), checked, group="link")
    elif data and data["kind"] == "dataset":
      self.query_one("#hdata").focus()

  # --------------------------------------------------------------- info and data
  def show_message(self, text):
    self.query_one("#hinfotext", Static).update(text)
    self.show_table(None)

  def show(self, data):
    self.current, self.row0, self.col0 = data, 0, 0
    if not data:
      return
    kind = data["kind"]
    if kind == "error":
      return self.show_message(Text(f"{data['path']}\n{data['error']}", style="red"))
    if kind == "external":
      target = h5_link_target(self.path, data["file"])
      return self.show_message(Text.assemble((data["path"], "bold"), "\nexternal link to ", (f"{data['target']}", "bold"),
                                             " in ", (target, "bold"), "\nEnter or click: open it"))
    try:
      obj = self.file[data["path"]]
      text = self.describe(obj, kind)
    except Exception as e:
      return self.show_message(Text(f"{data['path']}\ncannot read: {e}", style="red"))
    self.query_one("#hinfotext", Static).update(text)
    if kind == "dataset" and obj.shape:
      inp = self.query_one("#hslice", Input)
      inp.value = self.dataset_selections.get(data["path"], "")
    self.show_data()

  def describe(self, obj, kind):
    t = Text()
    t.append(obj.name, style="bold")
    if kind == "group":
      t.append(f"\ngroup with {len(obj)} members")
    elif kind == "dataset":
      shape = "scalar" if obj.shape == () else "empty (no dataspace)" if obj.shape is None else " × ".join(map(str, obj.shape))
      t.append(f"\ndataset  {shape}  {obj.dtype}")
      if obj.dtype.names:
        t.append("\nfields: " + ", ".join(f"{n} {obj.dtype.fields[n][0]}" for n in obj.dtype.names))
      layout = f"chunks {obj.chunks}" if obj.chunks else "contiguous"
      filters = [f for f in (obj.compression and f"{obj.compression}" + (f"({obj.compression_opts})" if obj.compression_opts is not None else ""),
                             obj.shuffle and "shuffle", obj.fletcher32 and "fletcher32", obj.scaleoffset and "scaleoffset") if f]
      t.append(f"\n{layout}" + (", " + ", ".join(filters) if filters else ""))
      try:
        stored = obj.id.get_storage_size()
        t.append(f"\n{humanize.naturalsize(obj.nbytes)} of data, {humanize.naturalsize(stored)} on disk")
        if stored and obj.nbytes:
          t.append(f" (ratio {obj.nbytes / stored:.2g})")
      except Exception:
        pass
      if obj.fillvalue is not None and obj.shape:
        t.append(f"\nfill value {h5_value(obj.fillvalue)}")
      try:
        dims = [", ".join(d.keys()) or (d.label if d.label else "") for d in obj.dims]
        if any(dims):
          t.append("\ndimensions: " + " × ".join(d or "-" for d in dims))
      except Exception:
        pass
      if obj.shape == ():
        t.append("\nvalue: ", style="bold")
        t.append(self.read_scalar(obj))
    else:
      t.append(f"\nnamed datatype {obj.dtype}")
    if obj.file.filename != self.file.filename:
      t.append(f"\nin file {obj.file.filename}")
    try:
      attrs = list(obj.attrs)
    except Exception as e:
      t.append(f"\ncannot list the attributes: {e}", style="red")
      return t
    t.append(f"\n\n{len(attrs)} attribute{'s' if len(attrs) != 1 else ''}" + (":" if attrs else ""), style="bold" if attrs else "dim")
    for name in attrs:
      t.append(f"\n  {name}", style="bold")
      try:
        t.append(f" = {h5_value(obj.attrs[name])}")
      except Exception as e:
        t.append(f" (cannot read: {e})", style="red")
    return t

  def read_scalar(self, obj):
    import h5py
    try:
      ds = obj.asstr() if h5py.check_string_dtype(obj.dtype) else obj
      return h5_value(ds[()], width=2000)
    except Exception as e:
      return f"cannot read: {e}"

  def show_table(self, rows, columns=(), status=""):
    table = self.query_one("#hdata", DataTable)
    table.clear(columns=True)
    sliced = rows is not None and self.current and self.current["kind"] == "dataset"
    self.query_one("#hslice", Input).display = bool(sliced and self.file[self.current["path"]].shape)
    self.query_one("#hpage", Static).update(status)
    self.query_one("#hpage", Static).display = bool(status)
    table.display = rows is not None
    if rows is None:
      return
    for c in columns:
      table.add_column(c)
    for label, cells in rows:
      table.add_row(*cells, label=label)

  def show_data(self):
    """Table with the rows and columns of the current page of the selected dataset"""
    import h5py
    data = self.current
    if not data or data["kind"] != "dataset":
      return self.show_table(None)
    ds = self.file[data["path"]]
    if not ds.shape: # scalar or empty: shown in the info
      return self.show_table(None)
    text = self.query_one("#hslice", Input).value
    try:
      sel = parse_selection(text, ds.shape)
      sel, shown = window(sel, ds.shape, self.row0, self.col0)
    except ValueError as e:
      return self.show_table([], status=Text(str(e), style="red"))
    if any(not len(part) for part, _ in shown):
      return self.show_table([], status="nothing selected")
    try:
      src = ds.asstr() if h5py.check_string_dtype(ds.dtype) else ds
      values = src[sel]
    except Exception as e:
      return self.show_table([], status=Text(f"cannot read: {e}", style="red"))
    names = ds.dtype.names
    status = []
    if len(shown) == 0: # every axis fixed by an index: a single value
      return self.show_table([("", [h5_value(values)])], ["value"])
    rows_part, nrows = shown[0]
    status.append(f"rows {rows_part[0]}–{rows_part[-1]} ({len(rows_part)} of {nrows}" + (", [ ]: page" if len(rows_part) < nrows else "") + ")")
    if len(shown) == 2:
      cols_part, ncols = shown[1]
      columns = [str(i) for i in cols_part]
      status.append(f"columns {cols_part[0]}–{cols_part[-1]} ({len(cols_part)} of {ncols}" + (", { }: page" if len(cols_part) < ncols else "") + ")")
      rows = [(str(i), [h5_value(v, 60) for v in row]) for i, row in zip(rows_part, values)]
    elif names: # one axis of a compound type: a column per field
      fields = names[self.col0:self.col0 + H5_COLS]
      columns = list(fields)
      if len(names) > H5_COLS:
        status.append(f"fields {self.col0}–{self.col0 + len(fields) - 1} of {len(names)}, {{ }}: page")
      rows = [(str(i), [h5_value(v[f], 60) for f in fields]) for i, v in zip(rows_part, values)]
    else:
      columns = ["value"]
      rows = [(str(i), [h5_value(v, 200)]) for i, v in zip(rows_part, values)]
    self.show_table(rows, columns, " · ".join(status))

  @on(Input.Submitted, "#hslice")
  def selection_submitted(self, event):
    event.stop()
    if self.current:
      self.dataset_selections[self.current["path"]] = event.value
    self.row0 = self.col0 = 0
    self.show_data()
    self.query_one("#hdata").focus()

  def action_selection(self):
    inp = self.query_one("#hslice", Input)
    if inp.display:
      inp.focus()

  def action_page(self, rows, cols):
    if not self.current or self.current["kind"] != "dataset":
      return
    self.row0 = max(0, self.row0 + rows * H5_ROWS)
    self.col0 = max(0, self.col0 + cols * H5_COLS)
    try:
      ds = self.file[self.current["path"]]
      sel = parse_selection(self.query_one("#hslice", Input).value, ds.shape)
      _, shown = window(sel, ds.shape)
      if shown: # do not page past the end
        self.row0 = min(self.row0, max(0, (shown[0][1] - 1) // H5_ROWS * H5_ROWS))
        ncols = shown[1][1] if len(shown) == 2 else len(ds.dtype.names or ())
        self.col0 = min(self.col0, max(0, (ncols - 1) // H5_COLS * H5_COLS))
    except Exception:
      pass
    self.show_data()

  def action_back(self):
    if self.focused is self.query_one("#hslice"):
      self.query_one("#htree").focus()
    else:
      self.pop_back()


class GdbCommandScreen(ModalScreen[list[str] | None]):
  """Edit the command that will be run interactively in the terminal pane."""
  DEFAULT_CSS = """
  GdbCommandScreen { align: center middle; }
  #gdb-dialog { width: 100; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #gdb-title { text-style: bold; margin-bottom: 1; }
  #gdb-command { width: 1fr; }
  #gdb-help { height: auto; margin-top: 1; color: $text-muted; }
  #gdb-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #gdb-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, default_args):
    super().__init__()
    self.default_command = shlex.join(default_args)

  def compose(self) -> ComposeResult:
    with Vertical(id="gdb-dialog"):
      yield Static("Open core dump with GDB", id="gdb-title")
      yield Label("Command (editable)", id="gdb-command-label")
      yield Input(self.default_command, id="gdb-command")
      yield Static("Arguments are parsed as a command line; shell operators and expansion are not evaluated.", id="gdb-help")
      with Horizontal(id="gdb-buttons"):
        yield Button("Cancel", id="gdb-cancel")
        yield Button("Run in terminal", id="gdb-run", variant="primary")

  def on_mount(self):
    command = self.query_one("#gdb-command", Input)
    command.focus()
    command.cursor_position = len(command.value)

  @on(Button.Pressed, "#gdb-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  @on(Button.Pressed, "#gdb-run")
  @on(Input.Submitted, "#gdb-command")
  def run_pressed(self, event):
    event.stop()
    command = self.query_one("#gdb-command", Input).value
    try:
      args = shlex.split(command)
    except ValueError as e:
      self.notify(f"Invalid command: {e}", severity="error")
      return
    if not args:
      self.notify("Enter a command to run", severity="warning")
      return
    self.dismiss(args)

  def action_cancel(self):
    self.dismiss(None)


def inspect_core_file(path):
  """Read process notes from a core file through GDB without starting an interactive session."""
  gdb = conn().which("gdb") if conn() else shutil.which("gdb")
  if not gdb:
    return {}, "gdb was not found on PATH"
  try:
    args = [gdb, "--batch", "--nx", f"--core={path}", "-ex", "info proc"]
    result = run_cmd(args, timeout=20)
  except OSError as e:
    return {}, f"could not inspect core metadata: {e}"
  output = (result.stdout or "") + "\n" + (result.stderr or "")
  info = {}
  for line in output.splitlines():
    key, sep, value = line.partition("=")
    key = key.strip()
    if sep and key in ("exe", "cwd", "cmdline"):
      value = value.strip()
      try:
        parsed = shlex.split(value)
        value = parsed[0] if len(parsed) == 1 else value
      except ValueError:
        value = value.strip("\"'")
      info[key] = value
  error = ""
  if not info:
    error = (result.stderr or result.stdout or "GDB could not read process details").strip()[:240]
  return info, error


class CoreScreen(SplitScreen):
  """Overview for an ELF core dump, with an editable GDB command launcher."""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back")]

  def __init__(self, crumbs, path, bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.bases, self.env = list(bases), env
    self.process_info = {}
    self.metadata_error = "Reading process details with GDB …"
    self.stat = None        # (size, mtime) or the error, read in the background
    self.exe_exists = False

  def compose_left(self) -> ComposeResult:
    with VerticalScroll(id="core-overview"):
      yield Static(id="core-info")
      yield Button("Open with GDB", id="open-gdb", variant="primary")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.show_info()
    self.inspect_metadata()

  def show_info(self):
    if isinstance(self.stat, tuple):
      size = humanize.naturalsize(self.stat[0])
      modified = datetime.fromtimestamp(self.stat[1]).strftime("%Y-%m-%d %H:%M:%S")
    else:
      size, modified = ("…", "…") if self.stat is None else ("unavailable", self.stat)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Type", "ELF core dump")
    grid.add_row("File", self.path)
    grid.add_row("Size", size)
    grid.add_row("Modified", modified)
    for key, label in (("exe", "Executable"), ("cmdline", "Command line"), ("cwd", "Working directory")):
      if self.process_info.get(key):
        grid.add_row(label, self.process_info[key])
    if self.metadata_error:
      grid.add_row("Process details", self.metadata_error)
    self.query_one("#core-info", Static).update(grid)

  @cluster_work("core-metadata")
  def inspect_metadata(self):
    try:
      st = fs_stat(self.path)
      stat = (st.st_size, st.st_mtime)
    except OSError as e:
      stat = str(e)
    info, error = inspect_core_file(self.path)
    exe_exists = bool(info.get("exe")) and fs_isfile(info["exe"])
    self.app.call_from_thread(self.metadata_loaded, info, error, stat, exe_exists)

  def metadata_loaded(self, info, error, stat, exe_exists):
    if not self.is_mounted:
      return
    self.process_info, self.metadata_error, self.stat, self.exe_exists = info, error, stat, exe_exists
    self.show_info()

  def gdb_args(self):
    executable = self.process_info.get("exe")
    if executable and self.exe_exists:
      return ["gdb", executable, self.path]
    return ["gdb", f"--core={self.path}"]

  @on(Button.Pressed, "#open-gdb")
  def open_gdb(self, event):
    event.stop()
    title = f"GDB · {os.path.basename(self.path)}"
    self.app.push_screen(GdbCommandScreen(self.gdb_args()),
                         lambda args: self.app.open_shell(args, title, focus=True) if args is not None else None)

  def action_back(self):
    self.pop_back()


def open_core_view(app, crumbs, path, bases=(), env=None):
  """(runs in a worker)"""
  return lambda: app.push_screen(CoreScreen(crumbs, path, bases=bases, env=env))


register_file_view("HDF5", is_hdf5, open_hdf5)
register_file_view("Markdown", is_markdown, open_markdown)
register_file_view("ELF core dump", is_core_dump, open_core_view)


class ConfirmScreen(ModalScreen[bool]):
  """Confirmation dialog for an action that changes a Slurm job"""
  DEFAULT_CSS = """
  ConfirmScreen { align: center middle; }
  #confirm-dialog { width: 72; max-width: 90%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #confirm-title { text-style: bold; margin-bottom: 1; }
  #confirm-message { height: auto; margin-bottom: 1; }
  #confirm-buttons { height: auto; align-horizontal: right; }
  #confirm-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "No")]

  def __init__(self, title, message, confirm_label, variant="error"):
    super().__init__()
    self.dialog_title, self.dialog_message = title, message
    self.confirm_label, self.variant = confirm_label, variant

  def compose(self) -> ComposeResult:
    with Vertical(id="confirm-dialog"):
      yield Static(self.dialog_title, id="confirm-title")
      yield Static(self.dialog_message, id="confirm-message", markup=False)
      with Horizontal(id="confirm-buttons"):
        yield Button("No", id="confirm-no")
        yield Button(self.confirm_label, id="confirm-yes", variant=self.variant)

  @on(Button.Pressed, "#confirm-no")
  def no(self, event):
    event.stop()
    self.dismiss(False)

  @on(Button.Pressed, "#confirm-yes")
  def yes(self, event):
    event.stop()
    self.dismiss(True)

  def action_cancel(self):
    self.dismiss(False)


def bash_editor(text, id, read_only=False):
  """Multi-line command editor with bash syntax highlighting"""
  editor = TextArea.code_editor(text, id=id, read_only=read_only, soft_wrap=True, show_line_numbers=False)
  if "bash" in editor.available_languages:
    editor.language = "bash"
  return editor

def command_args(text):
  """argv of a command typed in an editor: lines may end with a backslash, the lines are joined"""
  return shlex.split(text.replace("\\\n", " ").replace("\n", " "))

INTERACTIVE_JOB = """sbatch --job-name=interactive \\
  --nodes=1 --ntasks=1 \\
  --time=01:00:00 \\
  --output=/dev/null \\
  --wrap='sleep infinity'"""

class CommandScreen(ModalScreen[list[str] | None]):
  """Confirm (and edit) a command before it is run: returns its argv, or None"""
  DEFAULT_CSS = """
  CommandScreen { align: center middle; }
  #command-dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #command-title { text-style: bold; margin-bottom: 1; }
  #command-text { height: auto; min-height: 3; max-height: 16; }
  #command-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #command-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)] # also while the editor has the focus

  def __init__(self, title, explanation, command, submit_label="Submit", first_word=None):
    super().__init__()
    self.dialog_title, self.explanation, self.command = title, explanation, command
    self.submit_label, self.first_word = submit_label, first_word

  def compose(self) -> ComposeResult:
    with Vertical(id="command-dialog"):
      yield Static(self.dialog_title, id="command-title")
      yield Static(self.explanation, markup=False)
      yield bash_editor(self.command, "command-text")
      with Horizontal(id="command-buttons"):
        yield Button("Cancel", id="command-cancel")
        yield Button(self.submit_label, id="command-submit", variant="primary")

  def on_mount(self):
    self.query_one("#command-text").focus()

  @on(Button.Pressed, "#command-submit")
  def submit(self, event):
    event.stop()
    try:
      args = command_args(self.query_one("#command-text", TextArea).text)
    except ValueError as e:
      self.notify(f"invalid command: {e}", severity="error")
      return
    if not args or (self.first_word and os.path.basename(args[0]) != self.first_word):
      self.notify(f"the command must start with {self.first_word}", severity="error")
      return
    self.dismiss(args)

  @on(Button.Pressed, "#command-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  def action_cancel(self):
    self.dismiss(None)

class RestartScreen(ModalScreen[list[str] | None]):
  """Choose the command to submit when restarting a job"""
  DEFAULT_CSS = """
  RestartScreen { align: center middle; }
  #restart-dialog { width: 100; max-width: 95%; max-height: 90%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #restart-title { text-style: bold; margin-bottom: 1; }
  #restart-options { max-height: 10; }
  #restart-command-label { margin-top: 1; }
  #restart-command { width: 1fr; height: auto; min-height: 3; max-height: 12; }
  #restart-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #restart-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)] # also while the editor has the focus

  def __init__(self, jid, default_args, retry_args=None, retry_label="Retry unfinished array tasks unavailable"):
    super().__init__()
    self.jid = jid
    self.default_args = list(default_args)
    self.retry_args = list(retry_args) if retry_args else None
    self.retry_label = retry_label
    self.default_command = shlex.join(self.default_args)
    self.retry_command = shlex.join(self.retry_args) if self.retry_args else self.default_command
    self.custom_command = self.default_command

  def compose(self) -> ComposeResult:
    with Vertical(id="restart-dialog"):
      yield Static(f"Restart job {self.jid}", id="restart-title")
      with RadioSet(id="restart-options"):
        yield RadioButton("Use the current restart command", value=True)
        yield RadioButton(self.retry_label, disabled=self.retry_args is None)
        yield RadioButton("Custom command")
      yield Label("Command to submit (editable in Custom mode)", id="restart-command-label")
      yield bash_editor(self.default_command, "restart-command", read_only=True)
      with Horizontal(id="restart-buttons"):
        yield Button("Cancel", id="restart-cancel")
        yield Button("Submit job", id="restart-submit", variant="primary")

  def on_mount(self):
    self.query_one("#restart-options", RadioSet).focus()

  @on(RadioSet.Changed, "#restart-options")
  def option_changed(self, event):
    command = self.query_one("#restart-command", TextArea)
    if event.index == 2:
      command.read_only = False
      command.load_text(self.custom_command)
      command.focus()
    else:
      command.read_only = True
      command.load_text(self.default_command if event.index == 0 else self.retry_command)

  @on(TextArea.Changed, "#restart-command")
  def custom_command_changed(self, event):
    if self.query_one("#restart-options", RadioSet).pressed_index == 2:
      self.custom_command = event.text_area.text

  @on(Button.Pressed, "#restart-cancel")
  def cancel(self, event):
    event.stop()
    self.dismiss(None)

  @on(Button.Pressed, "#restart-submit")
  def submit(self, event):
    event.stop()
    index = self.query_one("#restart-options", RadioSet).pressed_index
    if index == 0:
      args = self.default_args
    elif index == 1 and self.retry_args is not None:
      args = self.retry_args
    elif index == 2:
      try:
        args = command_args(self.query_one("#restart-command", TextArea).text)
      except ValueError as e:
        self.notify(f"Invalid command: {e}", severity="error")
        return
      if not args or os.path.basename(args[0]) != "sbatch":
        self.notify("The restart command must start with sbatch", severity="error")
        return
    else:
      self.notify("Select a restart command", severity="warning")
      return
    self.dismiss(list(args))

  def action_cancel(self):
    self.dismiss(None)


class JobScreen(SplitScreen):
  """Details, files and steps of one job, commands on its nodes"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape,backspace", "back", "Back"),
    Binding("r", "reload", "Reload"),
    Binding("e", "open('stderr')", "Error file"),
    Binding("o", "open('stdout')", "Output file"),
    Binding("s", "shell", "Shell on node"),
  ]

  def __init__(self, job):
    super().__init__()
    self.job = job
    self.details = {"files": [], "sstat": {}, "nodes": []}

  def compose_left(self):
    with Horizontal(id="job-actions"):
      yield Button("Cancel job", id="cancel-job", variant="error")
      yield Button("Restart job", id="restart-job", variant="primary")
    with VerticalScroll(id="details"):
      yield Static(id="info")
      with Horizontal(id="memory-controls"):
        yield Button("Query current memory", id="memory-query", variant="primary")
        yield Label("CPU:")
        yield Static(id="memory-cpu", markup=False)
        yield Label("GPU:")
        yield Static(id="memory-gpu", markup=False)
      with Vertical(id="job-progress"):
        yield ProgressBar(total=1, show_eta=False, id="progress-bar")
        yield Static(id="progress-line", markup=False)
      yield Label("Files", classes="section")
      yield OptionList(Option("loading ...", disabled=True), id="files")
      yield Label("Steps", classes="section")
      yield ClickTable(id="steps", cursor_type="row", zebra_stripes=True)
      with Vertical(id="nodes"):
        yield Label("Run on the nodes of the job", classes="section")
        with Horizontal(id="nodebar"):
          yield Input(id="cmd", placeholder="command, e.g. nvidia-smi")
          yield Select([], id="node", prompt="all nodes", allow_blank=True)
          yield Button("Run", id="run", variant="primary")
          yield Button("Shell", id="shell", tooltip="interactive shell on the node, opens on the right")
        with Horizontal(id="presets"):
          for i, name in enumerate(PRESETS):
            yield Button(name, id=f"preset{i}", classes="preset")

  def on_mount(self):
    self.sub_title = f"Jobs › {self.job.jid}"
    table = self.query_one("#steps", ClickTable)
    for key, label in [("step", "Step"), ("name", "Name"), ("state", "State"), ("exit", "Exit"), ("elapsed", "Elapsed"),
                       ("tasks", "Tasks"), ("nodes", "Nodes"), ("maxrss", "MaxRSS"), ("total", "~Total RSS"), ("cmd", "Command")]:
      table.add_column(label, key=key)
    self.show_job()
    self.query_one("#files").focus()
    self.load()

  def show_job(self):
    job, rec = self.job, self.job.rec
    self.query_one("#cancel-job", Button).disabled = not (job.queued or job.running)
    self.query_one("#restart-job", Button).disabled = restart_args(job) is None
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    state = state_text(job.state)
    if job.reason and job.reason not in ("None", ""):
      state.append(f"  ({job.reason})", style="dim")
    elif not job.queued and rec["ExitCode"]:
      state.append(f"  exit code {rec['ExitCode']}")
    rows = [
      ("Job", f"{job.jid}  {rec['JobName']}"),
      ("State", state),
      ("User / account", f"{rec['User']} / {rec['Account']}   partition {rec['Partition']}"),
      ("Nodes", f"{rec['NNodes']}  {rec['NodeList'] if rec['NodeList'] not in ('None assigned', '') else ''}"),
      ("Tasks", str(rec["NTasks"] or max((int(s["NTasks"] or 0) for s in job.steps), default=0) or "-")),
      ("Submit", rec["Submit"].replace("T", " ")),
      ("Start", ("~ " if job.state == "PENDING" else "") + rec["Start"].replace("T", " ") if is_time(rec["Start"]) else "-"),
      ("End", rec["End"].replace("T", " ") if is_time(rec["End"]) else "-"),
      ("Elapsed / limit", f"{rec['Elapsed']} / {rec['Timelimit']}"),
      ("Node hours", f"{job.node_hours():.2f}"),
      ("WorkDir", rec["WorkDir"]),
      ("Submitted with", rec["SubmitLine"]),
      ("Command", job.command),
    ]
    state_name = job.state.split()[0].rstrip("+") if job.state else ""
    if not job.queued and state_name in (FAILED_JOB_STATES | {"COMPLETED"}):
      peak_cpu, peak_gpu = peak_job_memory(job)
      rows.append(("Peak CPU memory (Slurm DB)", humanize.naturalsize(peak_cpu) if peak_cpu else "not reported"))
      if peak_gpu:
        rows.append(("Peak GPU memory (Slurm DB)", humanize.naturalsize(peak_gpu)))
    for key, val in rows:
      grid.add_row(key, val)
    self.query_one("#info", Static).update(grid)
    self.query_one("#memory-controls", Horizontal).display = job.running
    self.query_one("#memory-query", Button).disabled = not job.running
    progress = progress_for_job(job)
    progress_view = self.query_one("#job-progress", Vertical)
    if progress is not None:
      progress_view.display = True
      self.query_one("#progress-bar", ProgressBar).update(total=max(progress.total, 1), progress=progress.current)
      self.query_one("#progress-line", Static).update(progress.line)
    else:
      progress_view.display = False
    self.query_one("#nodes").display = job.running
    self.show_steps()

  def show_steps(self):
    table = self.query_one("#steps", ClickTable)
    table.clear()
    sstat = self.details["sstat"]
    for s in self.job.steps:
      step = s["JobID"].partition(".")[2]
      live = sstat.get(step, {})
      maxrss = sl.sacct_bytes(live.get("MaxRSS") or s["MaxRSS"])
      ntasks = int(s["NTasks"] or 0)
      total = sl.sacct_bytes(live.get("AveRSS") or s["AveRSS"]) * ntasks
      table.add_row(step, s["JobName"], state_text(s["State"]), s["ExitCode"], s["Elapsed"], s["NTasks"], s["NNodes"],
                    humanize.naturalsize(maxrss) if maxrss else "", humanize.naturalsize(total) if total else "",
                    s["SubmitLine"], key=s["JobID"])
    if not self.job.steps:
      table.add_row("", "no steps", *[""]*8)

  def show_files(self):
    files = self.query_one("#files", OptionList)
    files.clear_options()
    for i, e in enumerate(self.details["files"]):
      prompt = Text()
      prompt.append(f"{e.kind:<10} ", style="bold")
      openable = (e.path and e.exists) or e.text is not None or e.kind == "sacct"
      prompt.append(e.label, style="" if openable else "dim")
      if e.note:
        prompt.append(f"  {e.note}", style="dim")
      if e.kind == "stderr" and e.path and self.job.err[0] == "file" and self.job.err[1]:
        prompt.append("  !", style="bold red")
      files.add_option(Option(prompt, id=str(i), disabled=not openable))
    if self.details["nodes"]:
      self.query_one("#node", Select).set_options([(n, n) for n in self.details["nodes"]])

  @cluster_work("details")
  def load(self, refresh_job=False):
    job = self.job
    if refresh_job:
      jobs = annotate(load_jobs(None, jids=[job.jid]))
      job = next((j for j in jobs if j.jid == job.jid), job)
    details = load_details(job)
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.loaded, job, details)

  def loaded(self, job, details):
    self.job, self.details = job, details
    self.job.progress = details.get("progress")
    self.show_job()
    self.show_files()
    self.app.show_warnings()

  @on(Button.Pressed, "#memory-query")
  def memory_query_pressed(self, event):
    event.stop()
    if not self.job.running:
      return
    self.query_one("#memory-query", Button).disabled = True
    self.query_one("#memory-cpu", Static).update("querying …")
    self.query_one("#memory-gpu", Static).update("querying …")
    self.query_current_memory()

  @cluster_work("current-memory")
  def query_current_memory(self):
    try:
      result = current_memory_usage(self.job)
    except Exception as e:
      result = {"error": str(e)}
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.memory_loaded, result)

  def memory_loaded(self, result):
    if not self.is_mounted:
      return
    if "error" in result:
      self.query_one("#memory-cpu", Static).update(f"unavailable ({result['error']})")
      self.query_one("#memory-gpu", Static).update("unavailable")
      self.query_one("#memory-query", Button).disabled = not self.job.running
      self.app.show_warnings()
      return
    self.details["sstat"] = result["sstat"]
    cpu = result["cpu_bytes"]
    cpu_text = f"{humanize.naturalsize(cpu)} RSS" if cpu is not None else "unavailable"
    if cpu is not None and result["cpu_source"] != "ps":
      cpu_text += f" ({result['cpu_source']})"
    if cpu is not None and result["cpu_partial"]:
      cpu_text += " (partial)"
    gpu = result["gpu_bytes"]
    if gpu is None:
      errors = result["gpu_errors"]
      gpu_text = f"unavailable ({errors[0]})" if errors else "unavailable"
    else:
      gpu_text = humanize.naturalsize(gpu)
      if result["gpu_partial"]:
        gpu_text += " (partial)"
    self.query_one("#memory-cpu", Static).update(cpu_text)
    self.query_one("#memory-gpu", Static).update(gpu_text)
    self.query_one("#memory-query", Button).disabled = not self.job.running
    self.show_steps()
    self.app.show_warnings()

  def action_back(self):
    self.pop_back()

  @on(Button.Pressed, "#cancel-job")
  def confirm_cancel(self, event):
    event.stop()
    jid = self.job.rec["JobIDRaw"]
    self.app.push_screen(
      ConfirmScreen("Cancel job?", f"Cancel Slurm job {jid}? This cannot be undone.", "Cancel job"),
      lambda confirmed: self.run_job_action("cancel", ["scancel", str(jid)]) if confirmed else None,
    )

  @on(Button.Pressed, "#restart-job")
  def confirm_restart(self, event):
    event.stop()
    args = restart_args(self.job)
    if args is None:
      self.notify("The original sbatch command is not available for this job", severity="warning")
      return
    self.prepare_restart_options(args)

  @cluster_work("restart-options")
  def prepare_restart_options(self, default_args):
    task_id = array_task_id(self.job)
    retry_args, retry_label = None, "Retry array IDs not running/completed (not an array task)"
    if task_id is not None:
      parent_id = self.job.jid.partition("_")[0]
      try:
        spec = original_array_spec(self.job)
        if not spec:
          raise ValueError("the original array range is unavailable")
        all_ids = expand_array_spec(spec)
        states = array_task_states(parent_id)
        retry_ids = array_ids_not_running_or_completed(all_ids, states)
        if retry_ids:
          retry_spec = compress_array_ids(retry_ids)
          retry_args = with_array_spec(default_args, retry_spec)
          retry_label = f"Retry IDs not running/completed (--array={retry_spec})"
        else:
          retry_label = "Retry IDs not running/completed (none found)"
      except Exception as e:
        retry_label = f"Retry IDs not running/completed (unavailable: {str(e)[:80]})"
    self.app.call_from_thread(self.show_restart_options, default_args, retry_args, retry_label)

  def show_restart_options(self, default_args, retry_args, retry_label):
    if not self.is_mounted:
      return
    cwd = self.job.rec.get("WorkDir")
    self.app.push_screen(
      RestartScreen(self.job.jid, default_args, retry_args, retry_label),
      lambda args: self.run_job_action("restart", args, cwd=cwd) if args is not None else None,
    )

  @cluster_work("job-action")
  def run_job_action(self, action, args, cwd=None):
    cwd = cwd if cwd and cwd not in ("(null)", "None") else None
    try:
      result = run_cmd(args, cwd=cwd, timeout=60, env=SLURM_ENV, retry=False)
      self.app.call_from_thread(self.job_action_finished, action, result.returncode, result.stdout, result.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
      self.app.call_from_thread(self.job_action_finished, action, -1, "", str(e))

  def job_action_finished(self, action, returncode, stdout, stderr):
    output = (stdout or stderr).strip()
    if returncode == 0:
      if action == "cancel":
        self.notify(f"Cancellation requested for job {self.job.jid}")
        if self.is_mounted:
          self.load(refresh_job=True)
      else:
        self.notify(f"Restarted {self.job.jid}: {output or 'new job submitted'}")
    else:
      self.notify(f"Could not {action} job {self.job.jid}: {output or 'Slurm command failed'}", severity="error")

  def check_action(self, action, parameters):
    if action == "shell":
      return self.job.running
    return True

  @on(Button.Pressed, "#shell")
  def action_shell(self):
    if not self.job.running:
      self.notify("the job is not running", severity="error")
      return
    node = self.query_one("#node", Select).value
    node = (self.details["nodes"] or [None])[0] if node is Select.NULL else node
    if not node:
      self.notify("the nodes of the job are not known yet, try again in a moment", severity="warning")
      return
    args = ["srun", f"--jobid={self.job.rec['JobIDRaw']}", "--overlap", "--pty", "--nodes=1", "--ntasks=1", "-w", node,
            f"--job-name={STEP_NAME}", "bash", "-l"]
    self.app.open_shell(args, f"shell on {node} · job {self.job.jid}")

  def action_reload(self):
    self.load(refresh_job=True)

  def action_open(self, kind):
    for e in self.details["files"]:
      if e.kind == kind and e.path:
        self.open_entry(e)
        return
    self.notify(f"no {kind} file", severity="warning")

  def context(self):
    """Base directories and variables to resolve paths in texts of this job"""
    return [self.job.rec["WorkDir"]], job_vars(self.job)

  def open_entry(self, e):
    bases, env = self.context()
    crumbs = ["Jobs", self.job.jid]
    if e.kind == "sacct":
      job = self.job
      in_background(self, lambda: sacct_all(job), lambda text: self.app.push_screen(
        FileScreen([*crumbs, e.label], text=text, refresher=lambda: sacct_all(job), bases=bases, env=env)) if text is not None else None,
        group="sacct")
    elif e.path:
      open_path(self.app, crumbs, e.path, bases, env, follow=self.job.running and e.kind in ("stdout", "stderr"))
    elif e.text is not None:
      self.app.push_screen(FileScreen([*crumbs, e.label], text=e.text, bases=bases, env=env))

  @on(OptionList.OptionSelected, "#files")
  def file_selected(self, event):
    self.open_entry(self.details["files"][int(event.option_id)])

  @on(DataTable.RowSelected, "#steps")
  def step_selected(self, event):
    event.stop()
    self.query_one("#steps", ClickTable).take_clicked_column()
    step = next((s for s in self.job.steps if s["JobID"] == event.row_key.value), None)
    if step:
      width = max(map(len, step))
      text = "\n".join(f"{k:<{width}}  {v}" for k, v in step.items() if v) + "\n"
      bases, env = self.context()
      self.app.push_screen(FileScreen(["Jobs", self.job.jid, f"step {step['JobID']}"], text=text, bases=bases, env=env))

  @on(Button.Pressed, "#run")
  @on(Input.Submitted, "#cmd")
  def run_pressed(self, event):
    self.run_command(self.query_one("#cmd", Input).value.strip())

  @on(Button.Pressed, ".preset")
  def preset_pressed(self, event):
    cmd = list(PRESETS.values())[int(event.button.id.removeprefix("preset"))]
    self.query_one("#cmd", Input).value = cmd
    self.run_command(cmd)

  def run_command(self, cmd):
    if not cmd:
      self.notify("enter a command first", severity="warning")
      return
    if not self.job.running:
      self.notify("the job is not running", severity="error")
      return
    node = self.query_one("#node", Select).value
    node = None if node is Select.NULL else node
    self.notify(f"running '{cmd}' on {node or 'all nodes'} ...")
    start_worker(self, lambda: self.command_done(cmd, node, run_on_nodes(self.job, cmd, node)), "nodes", exclusive=False)

  def command_done(self, cmd, node, text):
    job = self.job
    crumbs = ["Jobs", job.jid, f"{cmd} @ {node or 'all nodes'}"]
    bases, env = self.context()
    self.app.call_from_thread(self.app.push_screen, FileScreen(crumbs, text=text, refresher=lambda: run_on_nodes(job, cmd, node),
                                                               bases=bases, env=env))

# ---------------------------------------------------------------------------
# terminal window: a command (srun --pty bash) in a pseudo terminal, emulated with pyte

# make the pseudo terminal the controlling terminal of the command (for Ctrl-C, job control), then run it
CTTY = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
ALT_SCREEN_MODES = (47, 1047, 1049)
DECCKM = 1 << 5 # private mode 1 (application cursor keys), pyte stores private modes shifted by 5 bits

TERM_KEYS = {
  "enter": "\r", "tab": "\t", "shift+tab": "\x1b[Z", "backspace": "\x7f", "ctrl+h": "\x08", "escape": "\x1b",
  "insert": "\x1b[2~", "delete": "\x1b[3~", "pageup": "\x1b[5~", "pagedown": "\x1b[6~", "home": "\x1b[H", "end": "\x1b[F",
  "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS", "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~",
  "f8": "\x1b[19~", "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}
TERM_ARROWS = {"up": "A", "down": "B", "right": "C", "left": "D"}

def vt_color(name):
  """pyte color (name or rrggbb) -> rich color"""
  if not name or name == "default":
    return None
  if re.fullmatch(r'[0-9a-fA-F]{6}', name):
    return Color.parse("#" + name)
  bright = name.startswith("bright")
  base = {"brown": "yellow"}.get(name[6:] if bright else name, name[6:] if bright else name)
  try:
    return Color.parse(("bright_" if bright else "") + base)
  except Exception:
    return None

@functools.lru_cache(maxsize=4096)
def vt_style(fg, bg, bold, italics, underscore, strikethrough, reverse):
  return Style(color=vt_color(fg), bgcolor=vt_color(bg), bold=bold, italic=italics, underline=underscore,
               strike=strikethrough, reverse=reverse)

def make_vt_screen(cols, rows, reply):
  """pyte screen that answers terminal queries through reply and supports the alternate screen of vim, less, htop"""
  import pyte

  class VTScreen(pyte.Screen):
    saved = None

    def write_process_input(self, data):
      reply(data.encode())

    def set_mode(self, *modes, **kwargs):
      if kwargs.get("private") and any(m in ALT_SCREEN_MODES for m in modes):
        self.saved = ({y: dict(line) for y, line in self.buffer.items()}, (self.cursor.x, self.cursor.y))
        self.erase_in_display(2)
        modes = tuple(m for m in modes if m not in ALT_SCREEN_MODES)
        if not modes:
          return
      super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
      if kwargs.get("private") and any(m in ALT_SCREEN_MODES for m in modes):
        if self.saved:
          buffer, (x, y) = self.saved
          self.saved = None
          self.buffer.clear()
          for row, line in buffer.items():
            if row < self.lines:
              self.buffer[row].update(line)
          self.cursor.x, self.cursor.y = min(x, self.columns - 1), min(y, self.lines - 1)
          self.dirty.update(range(self.lines))
        modes = tuple(m for m in modes if m not in ALT_SCREEN_MODES)
        if not modes:
          return
      super().reset_mode(*modes, **kwargs)

  return VTScreen(cols, rows), pyte

SWITCH_KEY = "ctrl+right_square_bracket" # Ctrl-]: the one key that does not go to the shell

class TerminalSession:
  """A command (srun --pty bash) in a pseudo terminal, emulated with pyte. It belongs to the app and is shown in
  the terminal pane of every window, so it keeps running while you navigate on the left."""
  def __init__(self, app, args, title, via=None, login=None):
    self.app, self.args, self.title = app, args, title
    self.via = via     # RemoteSSH: args run on its cluster (no args: the login shell there)
    self.login = login # RemoteSSH: args are the ssh login (control master) of this connection
    self.vt = self.stream = self.proc = self.fd = None
    self.size = None
    self.done, self.code, self.error = False, None, ""

  def ensure(self, cols, rows):
    """Start with, or resize to, the size of the pane"""
    cols, rows = max(cols, 2), max(rows, 2)
    if self.vt is None and not self.done:
      self.start(cols, rows)
    elif self.vt is not None and (cols, rows) != self.size:
      self.size = (cols, rows)
      self.vt.resize(rows, cols)
      self.set_winsize(cols, rows)

  def start(self, cols, rows):
    self.size = (cols, rows)
    try:
      self.vt, pyte = make_vt_screen(cols, rows, self.write)
    except ImportError:
      self.done, self.code, self.error = True, -1, "the shell needs the python package pyte: pip install pyte"
      self.app.shell_changed()
      return
    self.stream = pyte.ByteStream(self.vt)
    master, slave = os.openpty()
    self.fd = master
    self.set_winsize(cols, rows)
    log.debug(f"running command: {shlex.join(self.args)}")
    try:
      if self.login:
        self.proc = self.login.spawn(slave)
      else:
        command = self.via.command_argv(self.args, interactive=True) if self.via else self.args
        self.proc = subprocess.Popen([sys.executable, "-c", CTTY, *command], stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, env=os.environ | {"TERM": "xterm-256color"}, close_fds=True)
    except OSError as e:
      os.close(slave)
      self.stream.feed(f"cannot start {self.args[0]}: {e}\r\n".encode())
      self.finish()
      return
    os.close(slave)
    os.set_blocking(master, False)
    asyncio.get_running_loop().add_reader(master, self.read_output)
    self.app.shell_changed()

  def set_winsize(self, cols, rows):
    if self.fd is not None:
      try:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
      except OSError:
        pass

  def read_output(self):
    try:
      data = os.read(self.fd, 65536)
    except BlockingIOError:
      return
    except OSError: # EIO: the command closed the terminal
      data = b""
    if not data:
      self.finish()
      return
    self.stream.feed(data)
    self.app.refresh_terminal()

  def close_fd(self):
    if self.fd is not None:
      try:
        asyncio.get_running_loop().remove_reader(self.fd)
      except Exception:
        pass
      os.close(self.fd)
      self.fd = None

  def finish(self):
    if self.done:
      return
    self.done = True
    self.close_fd()
    self.code = -1
    if self.proc:
      try:
        self.code = self.proc.wait(timeout=3)
      except subprocess.TimeoutExpired:
        self.proc.kill()
        self.code = self.proc.wait()
    if self.proc and self.app.shell is self and not self.login: # a login stays, so that its errors can be read
      self.app.close_shell()
    else:
      self.app.shell_changed()

  def terminate(self):
    """End the command (the pane is closed or the app quits)"""
    self.close_fd()
    if self.proc and self.proc.poll() is None:
      self.proc.terminate()
      try:
        self.proc.wait(timeout=3)
      except subprocess.TimeoutExpired:
        self.proc.kill()
    self.done = True

  def write(self, data):
    if self.fd is not None and not self.done:
      try:
        os.write(self.fd, data)
      except OSError:
        pass

class TerminalView(Widget, can_focus=True):
  """Shows the shell of the app; while it has the focus all keys except Ctrl-] go to the shell"""
  DEFAULT_CSS = "TerminalView { height: 1fr; }"

  @property
  def session(self):
    return self.app.shell

  def attach(self):
    """The pane was shown or got a new session: start/resize the session with the size of this view"""
    if self.session and self.size.width > 0 and self.size.height > 0:
      self.session.ensure(self.size.width, self.size.height)
    self.refresh()

  def on_resize(self, event):
    if self.session and self.display:
      self.session.ensure(event.size.width, event.size.height)
    self.refresh()

  def on_focus(self):
    self.refresh()

  def on_blur(self):
    self.refresh()

  async def on_key(self, event):
    event.stop()
    event.prevent_default()
    key = event.key
    if key == "ctrl+x":
      self.app.close_shell()
      return
    if key == SWITCH_KEY:
      self.app.action_switch_side()
      return
    s = self.session
    if s is None or s.vt is None and not s.done:
      return
    if s.done:
      if key == "enter":
        self.app.restart_shell()
      return
    if key in TERM_ARROWS:
      data = ("\x1bO" if DECCKM in s.vt.mode else "\x1b[") + TERM_ARROWS[key]
    elif key.startswith(("ctrl+", "shift+", "alt+")) and key.split("+")[-1] in TERM_ARROWS:
      mod = {"shift": 2, "alt": 3, "ctrl": 5}[key.split("+")[0]]
      data = f"\x1b[1;{mod}{TERM_ARROWS[key.split('+')[-1]]}"
    elif key in TERM_KEYS:
      data = TERM_KEYS[key]
    elif event.character:
      data = event.character
    elif key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
      data = chr(ord(key[5]) & 0x1f)
    elif key.startswith("alt+") and len(key) == 5:
      data = "\x1b" + key[4]
    else:
      return
    s.write(data.encode())

  def on_paste(self, event):
    s = self.session
    if s is None or s.vt is None:
      return
    text = event.text.replace("\r\n", "\r").replace("\n", "\r")
    if (2004 << 5) in s.vt.mode: # bracketed paste
      text = f"\x1b[200~{text}\x1b[201~"
    s.write(text.encode())

  def render_line(self, y):
    width = self.size.width
    s = self.session
    vt = s.vt if s else None
    base = self.rich_style
    if vt is None or y >= vt.lines:
      if s and s.error and y == 0:
        return Strip([Segment(s.error, base + Style(color="red"))]).adjust_cell_length(width, base)
      return Strip.blank(width, base)
    line = vt.buffer[y]
    cursor_x = vt.cursor.x if (y == vt.cursor.y and self.has_focus and not vt.cursor.hidden and not s.done) else -1
    segments, text, style = [], "", None
    for x in range(min(vt.columns, width)):
      ch = line[x]
      if ch.data == "": # second cell of a wide character
        continue
      st = base + vt_style(ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.strikethrough, ch.reverse != (x == cursor_x))
      if st != style:
        if text:
          segments.append(Segment(text, style))
        text, style = "", st
      text += ch.data
    if text:
      segments.append(Segment(text, style))
    return Strip(segments).adjust_cell_length(width, base)

class TerminalPane(Vertical):
  """Right side of every window: title bar with a close button and the shell"""
  def compose(self) -> ComposeResult:
    with Horizontal(classes="tbar"):
      yield Static(classes="ttitle")
      yield Button("✕", classes="tclose", compact=True, tooltip="close the shell (Ctrl-X)")
    yield TerminalView()

  def on_mount(self):
    self.watch(self.app, "shell_version", self.sync)
    self.watch(self.app, "pane_width", self.set_width)

  def sync(self, _=None):
    s = self.app.shell
    self.display = s is not None
    for splitter in self.parent.query(Splitter):
      splitter.display = s is not None
    if s is None:
      return
    if s.done:
      status = f"ended (exit code {s.code}) · Enter: new shell · ✕ or Ctrl-X: close"
    else:
      status = "Ctrl-] switches between the sides"
    self.query_one(".ttitle", Static).update(f"{s.title} · {status}")
    self.call_after_refresh(self.query_one(TerminalView).attach)

  def set_width(self, width):
    self.styles.width = width if width else "50%"

  @on(Button.Pressed, ".tclose")
  def close_pressed(self, event):
    event.stop()
    self.app.close_shell()

class Splitter(Widget):
  """Vertical bar between the window and the terminal pane, drag it with the mouse to resize"""
  def __init__(self):
    super().__init__()
    self.dragging = False

  def render(self):
    return ""

  def on_mouse_down(self, event):
    self.dragging = True
    self.capture_mouse()
    self.add_class("-dragging")
    event.stop()

  def on_mouse_move(self, event):
    if self.dragging:
      total = self.screen.size.width
      self.app.pane_width = max(20, min(total - event.screen_x - 1, total - 20))

  def on_mouse_up(self, event):
    if self.dragging:
      self.dragging = False
      self.release_mouse()
      self.remove_class("-dragging")

COLUMNS = [("jobid", "Job ID"), ("name", "Name"), ("user", "User"), ("state", "State"), ("progress", "Progress"),
           ("nodes", "Nodes"), ("time", "Elapsed / Limit"),
           ("start", "Start"), ("end", "End"), ("err", "Err"), ("cmd", "Command")]

SORT_KEYS = {
  "jobid": lambda j: job_id_key(j.jid),
  "name":  lambda j: j.rec["JobName"],
  "user":  lambda j: j.rec["User"].casefold(),
  "state": lambda j: j.state,
  "progress": lambda j: (p.current / p.total if (p := progress_for_job(j)) and p.total else 0),
  "nodes": lambda j: int(j.rec["NNodes"] or 0),
  "time":  lambda j: j.node_hours() / max(int(j.rec["NNodes"] or 1), 1),
  "start": lambda j: j.rec["Start"] if is_time(j.rec["Start"]) else "",
  "end":   lambda j: j.rec["End"] if is_time(j.rec["End"]) else "",
  "err":   lambda j: (j.err[0] == "file", j.err[1]),
  "cmd":   lambda j: j.command,
}

class MainScreen(SplitScreen):
  """The job table"""
  BINDINGS = [
    Binding("slash", "filter", "Filter jobs"),
    Binding("escape", "clear_filter", "Clear filter", show=False),
  ]

  def compose_left(self):
    with Horizontal(id="toolbar"):
      yield Button("Reload", id="reload", variant="primary")
      yield Button("Shell", id="cluster-shell", tooltip="shell on the cluster of this tab, in the pane on the right (t)")
      yield Button("Interactive job", id="interactive-job", tooltip="submit a job that holds nodes for shells (i)")
      yield Button("Cluster info", id="cluster-info", tooltip="health and occupancy of the cluster (C)")
      yield Label("finished since")
      yield Input("1d", id="since", placeholder="off")
      yield Select([("my jobs", "me"), ("all my accounts", "all")], value="me", allow_blank=False, id="scope",
                   tooltip="whose jobs are listed: yours, or those of one or all of your Slurm accounts")
    yield Static(id="status")
    yield ClickTable(id="jobs", cursor_type="row", zebra_stripes=True)
    yield Input(id="jfilter", placeholder="filter jobs by name, command, ID, state, user, account, path…")

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

  def on_mount(self):
    self.app.setup_job_columns()
    table = self.query_one("#jobs", ClickTable)
    table.focus()
    self.app.activate_cluster(self.app.cluster_index)

class AddClusterScreen(ModalScreen[str | None]):
  DEFAULT_CSS = """
  AddClusterScreen { align: center middle; }
  #cluster-dialog { width: 72; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #cluster-destination { width: 1fr; }
  #cluster-actions { height: auto; align-horizontal: right; margin-top: 1; }
  #cluster-actions Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def compose(self) -> ComposeResult:
    with Vertical(id="cluster-dialog"):
      yield Label("SSH destination (blank for local machine)")
      yield Input(placeholder="user@host or ~/.ssh/config alias", id="cluster-destination")
      with Horizontal(id="cluster-actions"):
        yield Button("Cancel", id="cluster-cancel")
        yield Button("Add", id="cluster-confirm", variant="primary")

  def on_mount(self):
    self.query_one("#cluster-destination", Input).focus()

  @on(Button.Pressed, "#cluster-cancel")
  def cancel_pressed(self):
    self.dismiss(None)

  @on(Button.Pressed, "#cluster-confirm")
  @on(Input.Submitted, "#cluster-destination")
  def add_pressed(self):
    self.dismiss(self.query_one("#cluster-destination", Input).value.strip())

  def action_cancel(self):
    self.dismiss(None)

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

  def on_mount(self):
    self.query_one("#conn-title", Static).update(
      Text(f"Connection of the tab {self.cluster.name}" + (f" · ssh {self.cluster.destination}" if self.cluster.destination else "")))
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
    delay = SlurmTUI.RECONNECT_DELAYS[min(cluster.attempts, len(SlurmTUI.RECONNECT_DELAYS) - 1)]
    left = max(0, delay - (now - (cluster.broken_at or now)))
    text = Text(f"failed: {c.error or 'the session ended'}" + (f" (ssh exit code {c.exit_code})" if c.exit_code is not None else ""),
                style="bold red")
    if c.wanted:
      text.append(f" · next attempt in {left:.0f} s" if left >= 1 else " · reconnecting", style="red")
    return text

  def show(self):
    if not self.is_mounted:
      return
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

class ConnectionStatus(Static):
  """State of the SSH session in the footer; a click opens the connection window"""
  def on_click(self, event):
    event.stop()
    self.app.action_connection()

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

def nodes_text(n):
  return f"{n} node" if n == 1 else f"{n} nodes"

class ClusterInfoScreen(ModalScreen):
  """Health and occupancy of the cluster of a tab"""
  DEFAULT_CSS = """
  ClusterInfoScreen { align: center middle; }
  #ci-dialog { width: 120; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #ci-title { text-style: bold; height: 1; margin-bottom: 1; }
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
    return Text(f"Cluster {self.cluster.name} · {state}")

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

class SlurmTUI(App):
  TITLE = "slurm_tui"
  CSS = """
  #toolbar { height: auto; padding: 0 1; }
  #toolbar > * { margin-right: 1; }
  #toolbar Label { padding-top: 1; }
  #scope { width: 28; }
  #notabs { display: none; padding: 2 4; color: $text-muted; }
  MainScreen.-no-tabs #notabs { display: block; }
  MainScreen.-no-tabs .split, MainScreen.-no-tabs #loadstatus, MainScreen.-no-tabs #connstatus { display: none; }
  #us { width: auto; height: auto; padding: 0; border: none; }
  #since { width: 12; }
  #topbar { height: 1; }
  #cluster-tabs { height: 1; padding: 0 1; background: $panel; }
  #cluster-tabs Button { height: 1; min-height: 1; padding: 0 1; margin-right: 1; }
  #cluster-tabs Button.cluster-tab { margin-right: 0; }
  #cluster-tabs Button.cluster-close { width: 3; min-width: 3; padding: 0; margin-right: 2; color: $text-muted; }
  #cluster-tabs Button.cluster-close:hover { color: $error; }
  #topbar Header { dock: none; width: 1fr; height: 1; }
  #bottombar { dock: bottom; height: 1; }
  #bottombar Footer { dock: none; width: 1fr; }
  #loadstatus { width: auto; height: 1; padding: 0 1; background: $footer-background; color: $warning; }
  #connstatus { width: auto; height: 1; padding: 0 1; background: $footer-background; }
  #connstatus:hover { background: $accent; }
  #back { width: 3; min-width: 3; height: 1; padding: 0; }
  #status { height: 1; padding: 0 1; background: $panel; }
  #jobs { height: 1fr; }
  #details { height: 1fr; padding: 0 1; }
  #memory-controls { height: auto; margin: 1 0; }
  #memory-controls > * { margin-right: 1; }
  #memory-controls Label { width: auto; height: 1; content-align: center middle; padding: 0; }
  #memory-cpu, #memory-gpu { width: auto; height: 1; }
  #job-progress { height: auto; margin-bottom: 1; padding: 0 1; }
  #progress-bar { width: 40; }
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
    Binding("e", "open('stderr')", "Error file"),
    Binding("o", "open('stdout')", "Output file"),
    Binding("t", "cluster_shell", "Shell"),
    Binding("d", "login_dir", "Home dir"),
    Binding("i", "interactive_job", "Interactive job"),
    Binding("C", "cluster_info", "Cluster info"),
    Binding("a", "abort_load", "Abort loading"),
    Binding("c", "connection", "Connection"),
    Binding(SWITCH_KEY, "switch_side", "Shell ⇄", key_display="^]"),
    Binding("ctrl+x", "close_shell", "Close shell"),
  ]
  shell_version = reactive(0) # bumped when the shell is opened, closed or ends: the panes update
  pane_width = reactive(0)    # width of the terminal pane in columns, 0: half of the window

  def __init__(self):
    super().__init__()
    self.clusters = [ClusterSession("Local")]
    for destination in initial_ssh_destinations:
      if is_local_destination(destination):
        continue
      self.clusters.append(ClusterSession(destination, destination))
    self.cluster_index = 0
    if initial_ssh_destinations and not is_local_destination(initial_ssh_destinations[0]):
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
    global ssh_connection
    if not 0 <= index < len(self.clusters):
      return
    cluster = self.clusters[index]
    if self.needs_login(cluster):
      cluster.attempts = 0
      self.login(cluster)
    self.cluster_index = index
    ssh_connection = cluster.connection
    self.show_tab_shell()
    self.update_scope_select()
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
    want = login or cluster.terminal
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
    if self.shell is not None and self.shell is not self.cluster.login_terminal:
      self.cluster.terminal = self.shell
    for _ in range(len(self.screen_stack) - 1):
      self.pop_screen()
    self.activate_cluster(index)

  def confirm_close_cluster(self, index):
    if not 0 <= index < len(self.clusters):
      return
    cluster = self.clusters[index]
    what = ["its job list"]
    if cluster.destination:
      what.insert(0, "its SSH session")
    if cluster.terminal is not None and not cluster.terminal.done:
      what.append("its shell")
    self.push_screen(ConfirmScreen(f"Close the tab {cluster.name}?", f"This ends {', '.join(what[:-1])} and {what[-1]}."
                                   if len(what) > 1 else f"This ends {what[0]}.", "Close tab"),
                     lambda ok: self.close_cluster(cluster) if ok else None)

  def close_cluster(self, cluster):
    """Close a tab: its loads, shells and SSH session end"""
    global ssh_connection
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
    self.clusters.pop(index)
    if current:
      for _ in range(len(self.screen_stack) - 1):
        self.pop_screen()
      if self.clusters:
        self.activate_cluster(min(index, len(self.clusters) - 1))
      else: # no tab left: only the + button
        self.cluster_index, ssh_connection = 0, None
        self.populate()
    elif index < self.cluster_index:
      self.cluster_index -= 1
    self.screen_stack[0].set_class(not self.clusters, "-no-tabs")
    self.refresh_status()

  def add_cluster(self, destination):
    if destination is None:
      return
    self.screen_stack[0].remove_class("-no-tabs")
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
    global ssh_connection
    if cluster.connection is not None:
      cluster.connection.event("reconnect requested")
    if cluster.login_terminal is not None:
      cluster.login_terminal.terminate()
    cluster.attempts, cluster.broken_at = 0, None
    self.login(cluster, show=True)
    if cluster is self.cluster:
      ssh_connection = cluster.connection
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
      return Text(f"● SSH {c.identity}{latency}", style="green")
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
    for screen in self.screen_stack:
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
    cluster = self.cluster
    loading, connection = self.load_text(cluster), self.connection_text(cluster)
    for screen in self.screen_stack:
      if not isinstance(screen, SplitScreen) or not screen.is_mounted:
        continue
      for widget in screen.query("#loadstatus"):
        widget.update(loading)
      for widget in screen.query("#connstatus"):
        widget.update(connection)
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
                       if (key != "user" or all_accounts) and (key != "progress" or show_progress)]
    column_keys = [key for key, _ in visible_columns]
    if column_keys == self.job_column_keys:
      return
    if not all_accounts and self.sort_column == "user":
      self.sort_column, self.sort_reverse = "jobid", True
    if not show_progress and self.sort_column == "progress":
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

  def open_shell(self, args, title, focus=False, via=CURRENT):
    """Shell in the pane on the right. It runs on the cluster of the active tab, over its SSH connection."""
    via = self.ssh if via is CURRENT else via
    if via is not None and not via.ready:
      self.notify("log in first", severity="warning")
      return
    if self.shell and not self.shell.done:
      self.notify("a shell is already open on the right, close it first (✕)", severity="warning")
      self.focus_terminal()
      return
    if self.shell:
      self.shell.terminate()
    if not focus and not isinstance(self.focused, TerminalView):
      self.screen.left_focus = self.focused
    self.shell = TerminalSession(self, args, title, via=via)
    self.cluster.terminal = self.shell
    self.shell_changed()
    self.call_after_refresh(self.focus_terminal if focus else self.focus_left)

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
          global ssh_connection
          ssh_connection = cluster.connection
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
    if action in ("open", "reload"): # keys of the job table, not of the other windows
      return self.screen is self.screen_stack[0]
    if action in ("switch_side", "close_shell"):
      return self.shell is not None
    if action == "abort_load":
      return self.cluster.loader is not None
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
    value = self.query_one("#since", Input).value.strip()
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
      values.extend((rec["NNodes"], f"{rec['Elapsed']} / {rec['Timelimit']}",
                     ("~" + start) if start and job.state == "PENDING" else start, fmt_time(rec["End"]), err_text(job.err),
                     job.command[:150]))
      table.add_row(*values, key=job.jid)
    if current in {job.jid for job in visible_jobs}:
      table.move_cursor(row=table.get_row_index(current))
    if self.screen is self.screen_stack[0] and (self.focused is None or not isinstance(self.focused, (Input, TerminalView, TextArea))):
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
    self.push_screen(CommandScreen(f"Interactive job on {cluster.name}", explanation, INTERACTIVE_JOB, "Submit", "sbatch"),
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

  @on(Input.Submitted, "#since")
  def reload_requested(self, event):
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
    if col == self.sort_column:
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
      self.push_screen(JobScreen(job))

  def open_file(self, job, path):
    open_path(self, ["Jobs", job.jid], path, [job.rec["WorkDir"]], job_vars(job), follow=job.running)

  def action_open(self, kind):
    job = self.current_job()
    path = job and (job.stderr_path() if kind == "stderr" else job.stdout_path())
    if not path:
      self.notify(f"no {kind} file" + (" (stderr goes to the output file)" if job and kind == "stderr" and job.merged() else ""),
                  severity="warning")
      return
    self.open_file(job, path)

REPORTS_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1015l\x1b[?1006l\x1b[?1004l" # mouse and focus reporting off

def main():
  parser = argparse.ArgumentParser(description=__doc__)
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
    command = [sys.executable, os.path.abspath(__file__)]
    if args.log:
      command += ["--log", args.log]
    if args.ssh:
      command += ["--ssh", *args.ssh]
    Server(shlex.join(command), host=args.host, port=args.port, title="Slurm TUI").serve()
    return

  setup_logging(args.log)
  global initial_ssh_destinations
  initial_ssh_destinations = args.ssh
  app = SlurmTUI()
  try:
    app.run()
  finally:
    app.shutdown()

if __name__ == "__main__":
  main()
