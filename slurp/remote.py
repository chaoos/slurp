"""Running commands and reading files: locally or on the cluster of a tab, over its SSH session, in workers"""

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

from . import slurm as sl

from .state import *

ssh_connection = None # RemoteSSH of the active tab, None: the local machine

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
    tab = tab_name()
    def show():
      if not (node is app or node.is_attached):
        return
      try:
        done(result)
      except Exception as e: # showing a result must not end the app
        log.exception(f"showing the result of {name or group} failed")
        app.notify(f"showing {name or group} failed: {e}", title=tab, severity="error", timeout=10)
    app.call_from_thread(show)
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
    self.socket_dir = tempfile.mkdtemp(prefix=f"slurp-{os.getuid()}-")
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
      self.socket_dir = tempfile.mkdtemp(prefix=f"slurp-{os.getuid()}-")
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
    setup = ('d=$(mktemp -d "${TMPDIR:-/tmp}/slurp.XXXXXX") || exit 1\n'
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

  def subsystem_argv(self, name):
    """ssh command line for a new channel of the connection that runs a subsystem of the cluster's sshd (sftp)"""
    return ["ssh", "-S", self.control_path, "-T", "-s", self.destination, name]

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

HEALTH_STYLES = {"good": "bold green", "warn": "bold yellow", "bad": "bold red"}
SLOW_ROUND_TRIP = 1.0 # seconds: a heartbeat round trip longer than this is slow

def connection_health(connection):
  """("good" | "warn" | "bad", in words) of an SSH connection (None: the local machine)"""
  c = connection
  if c is None:
    return "good", "local"
  if c.ready:
    waiting = c.responding()
    if waiting is not None and waiting > PING_SLOW:
      return "bad", f"no answer for {waiting:.0f} s"
    if waiting is not None and waiting > SLOW_ROUND_TRIP or c.latency is not None and c.latency > SLOW_ROUND_TRIP:
      return "warn", "slow"
    return "good", "healthy"
  if c.state in ("connecting", "new"):
    return "warn", "connecting" if c.state == "connecting" else "not connected"
  return "bad", "disconnected" if c.state == "closed" or not c.wanted else "connection lost"

class RemoteFile:
  """A read-only file on the cluster as a Python file object (for h5py): reads fetch only the byte ranges asked for,
  over the session of the connection, in blocks of RemoteFile.BLOCK with a cache of the last ones. No local copy.
  Use it in workers only: a read is a round trip."""
  BLOCK = 1 << 20
  CACHED = 64 # blocks kept

  def __init__(self, connection, path, size=None):
    self.connection, self.path, self.pos = connection, path, 0
    self.size = size if size is not None else connection.getsize(path)
    self.blocks = collections.OrderedDict()
    self.lock = threading.Lock()
    self.round_trips = self.bytes_read = 0

  def fetch(self, first, last):
    """Blocks first..last (inclusive) into the cache, those not there in one read"""
    missing = [b for b in range(first, last + 1) if b not in self.blocks]
    if not missing:
      return
    lo, hi = missing[0], missing[-1]
    data = self.connection.read(self.path, lo * self.BLOCK, (hi - lo + 1) * self.BLOCK)
    self.round_trips += 1
    self.bytes_read += len(data)
    for b in range(lo, hi + 1):
      self.blocks[b] = data[(b - lo) * self.BLOCK:(b - lo + 1) * self.BLOCK]
      self.blocks.move_to_end(b)
    while len(self.blocks) > self.CACHED:
      self.blocks.popitem(last=False)

  def read(self, n=-1):
    with self.lock:
      if n is None or n < 0:
        n = self.size - self.pos
      n = max(0, min(n, self.size - self.pos))
      if n == 0:
        return b""
      first, last = self.pos // self.BLOCK, (self.pos + n - 1) // self.BLOCK
      self.fetch(first, last)
      out = b"".join(self.blocks.get(b, b"") for b in range(first, last + 1))
      start = self.pos - first * self.BLOCK
      data = out[start:start + n]
      self.pos += len(data)
      return data

  def readinto(self, buffer):
    data = self.read(len(buffer))
    buffer[:len(data)] = data
    return len(data)

  def seek(self, offset, whence=0):
    self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
    return self.pos

  def tell(self):
    return self.pos

  def seekable(self):
    return True

  def readable(self):
    return True

  def writable(self):
    return False

  def flush(self):
    pass

  def close(self):
    self.blocks.clear()

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

def fs_write(path, text, exclusive=False):
  """Write a (small) text file; exclusive: fail if it exists. Raises OSError."""
  if conn() is None:
    with open(path, "x" if exclusive else "w") as f:
      f.write(text)
    return
  import base64
  script = ('set -C; ' if exclusive else '') + 'printf "%s" "$2" | base64 -d > "$1"'
  res = conn().run(["sh", "-c", script, "sh", path, base64.b64encode(text.encode()).decode()], quiet=True, retry=False)
  if res.returncode:
    raise OSError((res.stderr or "").strip() or f"cannot write {path}")

def fs_expanduser(path):
  if conn() and str(path).startswith("~"):
    return conn().expanduser(path)
  return os.path.expanduser(path)

def command_exists(name):
  return bool(conn().which(name)) if conn() else shutil.which(name) is not None
