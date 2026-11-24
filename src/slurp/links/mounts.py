"""The login directory of a tab as a drive on the machine slurp runs on: sshfs through the tab's SSH session (its
control socket, so no login), run by slurp in the foreground (-f): slurp knows its state and its messages, and it
ends with slurp (the kernel ends it when slurp ends; sshfs then unmounts).

States: "unmounted", "mounting", "mounted", "broken" (mounted, but the files do not answer), "unmounting",
"failed" (sshfs ended with an error), "foreign" (something else is mounted at the path), "reconnecting" (sshfs of a
mounted drive ended, e.g. the session of the tab broke and sshfs gave up: slurp mounts it again when the session
is back)."""

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from ..state import log
from ..config import load_config, update_config

def available():
  """None if drives can be mounted here, else why not"""
  if not shutil.which("sshfs"):
    return "needs sshfs on this machine (e.g. apt install sshfs, brew install macfuse sshfs)"
  if sys.platform.startswith("linux") and not os.path.exists("/dev/fuse"):
    return "needs FUSE on this machine (/dev/fuse is missing)"
  return None

def default_path(cluster):
  name = re.sub(r"[^\w.-]", "_", cluster.destination or cluster.name)
  return os.path.join(os.path.expanduser("~"), "mnt", name)

def saved(cluster):
  """(path, read_only) chosen for the tab before, or the defaults"""
  entry = (load_config().get("drive_mounts") or {}).get(cluster.destination or "")
  if isinstance(entry, dict) and entry.get("path"):
    return entry["path"], bool(entry.get("read_only"))
  return default_path(cluster), False

def saved_remote(cluster):
  """The directory of the cluster chosen for the tab's drive before ("": the login directory)"""
  entry = (load_config().get("drive_mounts") or {}).get(cluster.destination or "")
  return str(entry.get("remote") or "") if isinstance(entry, dict) else ""

def save(cluster, path, read_only, remote=""):
  mounts = dict(load_config().get("drive_mounts") or {})
  mounts[cluster.destination or ""] = {"path": path, "read_only": read_only, **({"remote": remote} if remote else {})}
  update_config(drive_mounts=mounts)

def mount_table():
  """{mount point: (source, fs type)} of this machine"""
  table = {}
  try:
    with open("/proc/self/mounts") as f:
      for line in f:
        parts = line.split()
        if len(parts) >= 3:
          unescape = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
          table[unescape(parts[1])] = (unescape(parts[0]), parts[2])
  except OSError: # (no /proc: macOS) mount prints "source on point (type, ...)"
    try:
      out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
      for line in out.splitlines():
        m = re.match(r"(.+?) on (.+?) \((\w+)", line)
        if m:
          table[m.group(2)] = (m.group(1), m.group(3))
    except (OSError, subprocess.TimeoutExpired):
      pass
  return table

def unmount_argv(path, lazy=False):
  if shutil.which("fusermount3") or shutil.which("fusermount"):
    return [shutil.which("fusermount3") or "fusermount", "-u", *(["-z"] if lazy else []), path]
  return ["umount", *(["-f"] if lazy else []), path] # (macOS)

class LocalDrive:
  """The drive of one tab on this machine"""
  _state = "unmounted"

  @property
  def state(self):
    return self._state

  @state.setter
  def state(self, value):
    if value != self._state:
      log.debug(f"drive of {self.cluster.name}: {self._state} -> {value}")
    self._state = value

  def __init__(self, cluster, events=lambda *a: None):
    self.cluster, self.events = cluster, events
    self.path, self.read_only = saved(cluster)
    self.remote = saved_remote(cluster) # the directory of the cluster it shows ("": the login directory)
    self.proc = None            # the sshfs process (asyncio)
    self.state = "unmounted"
    self.error = ""
    self.messages = []          # last lines sshfs wrote
    self.started = self.mounted_at = None
    self.source = ""            # dest:home
    self.server = ""            # the sftp-server it runs ("" the subsystem)
    self.healthy = None         # the last check: the files answer (None: not checked yet)
    self.checking = False
    self.check_started = 0.0
    self.foreign = None         # (source, type) of something else mounted at the path
    self.wanted = False         # the user mounted it (and did not unmount it): bring it back when sshfs ends
    self.attempts = 0           # mounts again since it was lost
    self.retry_at = 0.0
    self.broken_since = None    # when it stopped answering
    self.link = None            # the control socket path sshfs uses: a symlink to the tab's current socket

  # ---- mount, unmount
  async def mount(self, path, read_only, sftp_server="", remote=None):
    """Mount (the tab must be connected) the directory remote of the cluster (None: the one chosen before; "": the
    login directory) at path; returns an error text or ""."""
    c = self.cluster.connection
    if self.proc is not None and self.proc.returncode is None:
      return "already mounted"
    if not self.cluster.destination:
      return "the local tab: its files are already here"
    if c is None or not c.ready:
      return f"{self.cluster.name}: not connected"
    missing = available()
    if missing:
      return missing
    path = os.path.abspath(os.path.expanduser(path))
    loop = asyncio.get_running_loop()
    table = await loop.run_in_executor(None, mount_table)
    if path in table and table[path][0].startswith("slurp:"): # left by an sshfs of slurp that ended: clear it
      await loop.run_in_executor(None, lambda: subprocess.run(unmount_argv(path, lazy=True), capture_output=True, timeout=30))
      table = await loop.run_in_executor(None, mount_table)
    if path in table:
      return f"something is mounted at {path} already ({table[path][0]}, {table[path][1]})"
    try:
      os.makedirs(path, exist_ok=True)
      if os.listdir(path):
        return f"{path} is not empty: choose an empty or new directory"
    except OSError as e:
      return f"cannot use {path}: {e}"
    self.path, self.read_only, self.server = path, read_only, sftp_server
    if remote is not None:
      self.remote = "" if remote == c.home else remote
    self.wanted = True
    save(self.cluster, path, read_only, self.remote)
    home = self.remote or c.home or "."
    self.source = f"{self.cluster.destination}:{home}"
    self.update_link()
    ssh = f"ssh -S {self.link} -o ControlMaster=no -o BatchMode=yes"
    options = ["reconnect", "ServerAliveInterval=15", f"fsname=slurp:{self.source}", "subtype=slurp"]
    if read_only:
      options.append("ro")
    if sftp_server:
      options.append(f"sftp_server={sftp_server}{' -R' if read_only else ''}")
    argv = ["sshfs", "-f", "-o", f"ssh_command={ssh}", "-o", ",".join(options), f"{self.cluster.destination}:{home}", path]
    log.debug(f"drive of {self.cluster.name}: {shlex.join(argv)}")
    from .share import die_with_parent
    self.state, self.error, self.messages, self.healthy = "mounting", "", [], None
    self.started, self.mounted_at = time.time(), None
    try:
      self.proc = await asyncio.create_subprocess_exec(*argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.PIPE, start_new_session=True,
                                                       preexec_fn=die_with_parent if sys.platform.startswith("linux") else None)
    except OSError as e:
      self.state, self.error = "failed", f"cannot run sshfs: {e}"
      return self.error
    asyncio.ensure_future(self._watch(self.proc))
    self.events("mounting", self, "")
    return ""

  async def _watch(self, proc):
    while True:
      line = await proc.stderr.readline()
      if not line:
        break
      text = line.decode(errors="replace").strip()
      if text:
        self.messages = (self.messages + [text])[-5:]
    code = await proc.wait()
    if proc is not self.proc:
      return
    self.proc = None
    was = self.state
    lost = self.wanted and (self.mounted_at is not None or self.attempts) and was != "unmounting"
    if lost: # (it was mounted: mount it again when the session of the tab is there)
      self.state, self.error, self.retry_at = "reconnecting", " | ".join(self.messages[-2:]), time.time() + 2
    elif self.state == "unmounting" or code == 0:
      self.state, self.error = "unmounted", ""
    else:
      self.state = "failed"
      self.error = " | ".join(self.messages[-2:]) or f"sshfs ended with {code}"
    self.mounted_at = None
    log.debug(f"drive of {self.cluster.name}: sshfs ended ({code}), {self.error}")
    self.events("lost" if lost else "ended", self, self.error if was != "unmounting" else "")

  async def unmount(self, lazy=False):
    """Unmount (lazy: also when files are in use, it goes when they are closed); returns an error text or ""."""
    self.wanted = False
    if self.state == "reconnecting":
      self.state = "unmounted"
      return ""
    if self.state not in ("mounted", "mounting", "broken", "foreign"):
      return ""
    path = self.path
    previous, self.state = self.state, "unmounting"
    def run():
      return subprocess.run(unmount_argv(path, lazy), capture_output=True, text=True, timeout=30)
    try:
      r = await asyncio.get_running_loop().run_in_executor(None, run)
    except (OSError, subprocess.TimeoutExpired) as e:
      self.state = previous
      return f"unmounting failed: {e}"
    if r.returncode:
      self.state = previous
      msg = (r.stderr or r.stdout).strip()
      return f"unmounting failed: {msg}" + (" (files are in use: close them, or Force unmount)" if "busy" in msg.lower() else "")
    if self.proc is None: # (not ours, or sshfs gone already)
      self.state = "unmounted"
    return ""

  async def remount(self, sftp_server=None):
    err = await self.unmount()
    if err:
      return err
    for _ in range(100): # sshfs ends after the unmount
      if self.proc is None:
        break
      await asyncio.sleep(0.05)
    self.state = "unmounted"
    return await self.mount(self.path, self.read_only, self.server if sftp_server is None else sftp_server)

  def stop(self):
    """slurp quits: end sshfs (it unmounts on SIGTERM)"""
    self.wanted = False
    if self.proc is not None and self.proc.returncode is None:
      self.state = "unmounting"
      try:
        self.proc.terminate()
      except ProcessLookupError:
        pass

  async def stopped(self, timeout=5):
    """stop(), then wait until sshfs is gone (see share.ended)"""
    proc = self.proc
    self.stop()
    from .share import ended
    await ended([proc], timeout)

  def update_link(self):
    """sshfs reaches the cluster through a symlink to the control socket of the tab's session: a reconnect may give
    the socket a new path, the symlink follows it, so sshfs's own reconnect works"""
    c = self.cluster.connection
    if c is None or not c.control_path:
      return
    if self.link is None:
      directory = tempfile.mkdtemp(prefix=f"slurp-drive-{os.getuid()}-")
      os.chmod(directory, 0o700)
      self.link = os.path.join(directory, "control")
    try:
      if os.path.islink(self.link) and os.readlink(self.link) == c.control_path:
        return
      tmp = self.link + ".new"
      if os.path.lexists(tmp):
        os.unlink(tmp)
      os.symlink(c.control_path, tmp)
      os.replace(tmp, self.link)
    except OSError as e:
      log.warning(f"drive of {self.cluster.name}: cannot update {self.link}: {e}")

  # ---- state
  def poll(self):
    """Update the state (cheap; the health of a mounted drive is checked in a thread, it may hang)"""
    ours = self.proc is not None and self.proc.returncode is None
    if self.state == "reconnecting":
      c = self.cluster.connection
      if c is not None and c.ready and time.time() >= self.retry_at:
        self.attempts += 1
        self.retry_at = time.time() + 10 # (the next attempt, if this one fails)
        self.events("remount", self, "")
      return self.state
    table = mount_table()
    entry = table.get(self.path)
    self.foreign = entry if entry and not ours else None
    if not ours:
      if self.state not in ("failed", "unmounting"):
        self.state = "foreign" if entry else "unmounted"
      elif self.state == "unmounting" and not entry:
        self.state = "unmounted"
      return self.state
    if self.state == "unmounting":
      return self.state
    self.update_link()
    if entry is None:
      self.state = "mounting"
      return self.state
    if self.mounted_at is None:
      self.mounted_at = time.time()
      self.events("mounted", self, "again" if self.attempts else "")
      self.attempts = 0
    self.check_health()
    self.state = "broken" if self.healthy is False else "mounted"
    if self.state != "broken":
      self.broken_since = None
    else:
      self.broken_since = self.broken_since or time.time()
      c = self.cluster.connection
      if c is not None and c.ready and time.time() - self.broken_since > 30 and time.time() - (c.ready_at.timestamp() if c.ready_at else 0) > 30:
        # (the session is there, sshfs still does not answer: mount it again)
        log.debug(f"drive of {self.cluster.name}: not answering for {time.time() - self.broken_since:.0f} s, mounting again")
        self.broken_since = None
        self.events("stuck", self, "")
    return self.state

  def check_health(self):
    """Do the files answer? (stat of the mount point in a thread; a dead connection makes it hang or fail)"""
    if self.checking:
      if time.time() - self.check_started > 5:
        self.healthy = False # (still hanging)
      return
    if time.time() - self.check_started < 5: # (every 5 s: each check asks the cluster)
      return
    self.checking, self.check_started = True, time.time()
    path = self.path
    def check():
      try:
        os.statvfs(path)
        os.listdir(path)
        self.healthy = True
      except OSError:
        self.healthy = False
      finally:
        self.checking = False
    threading.Thread(target=check, daemon=True).start()
