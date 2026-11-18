"""A tab as one end of a transfer: commands there (the command channel of its SSH session, or this machine) and
channels with their own input and output (a new channel of its SSH connection, no new login; here: a process)"""

import os
import shlex
import subprocess

from ..cluster import remote
from ..state import log

STATE_ROOT = '"${XDG_CACHE_HOME:-$HOME/.cache}/slurp/transfers"' # (on the cluster: the folders of the runs)

class NotConnected(Exception):
  """The tab of an end is not connected (or not open)"""

class Endpoint:
  def __init__(self, cluster):
    self.cluster = cluster
    self.name = cluster.name
    self.local = not cluster.destination
    self.connection = None if self.local else cluster.connection
    self._root = None

  def __repr__(self):
    return f"Endpoint({self.name})"

  @property
  def ready(self):
    return self.local or (self.connection is not None and self.connection.ready)

  def require(self):
    if not self.ready:
      raise NotConnected(f"{self.name} is not connected")

  def run(self, argv, timeout=60, retry=True, text=True):
    """A command there -> CompletedProcess (waits for the session while it reconnects; 255 when it cannot run)"""
    if self.local:
      try:
        return remote.run_local(list(argv), timeout, None, None, text)
      except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(argv, 255, "" if text else b"", str(e) if text else str(e).encode())
    if self.connection is None:
      raise NotConnected(f"{self.name} is not connected")
    return self.connection.run(list(argv), timeout=timeout, quiet=True, text=text, retry=retry)

  def sh(self, script, *args, timeout=60, retry=True):
    """A POSIX shell script there, with its arguments ($1, …)"""
    return self.run(["/bin/sh", "-c", script, "sh", *map(str, args)], timeout=timeout, retry=retry)

  def channel_argv(self, script, *args):
    """The command line of a process here that runs script there with its own stdin, stdout and stderr (a script for
    a channel must be one line: the login shell of the cluster parses it first)"""
    if "\n" in script:
      raise ValueError("a channel's script must be one line")
    command = ["/bin/sh", "-c", script, "sh", *map(str, args)]
    if self.local:
      return command
    self.require()
    return self.connection.command_argv(command)

  def popen(self, script, *args):
    argv = self.channel_argv(script, *args)
    log.debug(f"channel on {self.name}: {shlex.join(argv)[:300]}")
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)

  @property
  def home(self):
    if self.local:
      return os.path.expanduser("~")
    return self.connection.home if self.connection is not None else ""

  def state_root(self):
    """The folder of the runs of transfers there (created)"""
    if self._root is None:
      result = self.sh(f'r={STATE_ROOT}; mkdir -p "$r" && chmod 700 "$r" 2>/dev/null; printf "%s" "$r"')
      if result.returncode or not (result.stdout or "").strip():
        raise OSError(f"cannot create the folder of transfers on {self.name}: {(result.stderr or '').strip()[:200]}")
      self._root = result.stdout.strip().splitlines()[-1]
    return self._root

  def kill(self, pid, signal="TERM"):
    if self.local:
      try:
        os.kill(int(pid), {"TERM": 15, "INT": 2, "KILL": 9}[signal])
        return True
      except (OSError, ValueError):
        return False
    return self.run(["kill", f"-{signal}", str(pid)], timeout=30, retry=False).returncode == 0
