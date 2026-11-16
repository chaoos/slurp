"""What the link tests share (test_links_*.py): the lab as the relay of the links, the user's terminal (a real ssh in a
pseudo terminal, the password from askpass), sshfs. configure() sets the paths for a lab; the env fixture of the
tests (links_env) puts the lab's ssh first on PATH and the link settings in the lab directory."""
import asyncio
import fcntl
import os
import pty
import re
import select
import socket
import struct
import subprocess
import termios
import time

from slurp.links import share as sh

HERE = LAB = S = KNOWN = None

def configure(work):
  global HERE, LAB, S, KNOWN
  HERE, LAB, S = work, os.path.join(work, "sshlab"), os.path.join(work, "share")
  KNOWN = os.path.join(S, "client_known_hosts")

def free_port():
  s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

async def blocking(fn, *a, **k):
  """run fn in a thread (the server lives on this event loop: a blocking call here would stall it)"""
  import functools
  return await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *a, **k))
def read(p): return open(p).read()
def mount(opts, remote, mnt, password):
  """sshfs (it stays in the background with the output it inherits: not to pipes, which would never close)"""
  errf = os.path.join(S, "sshfs.err")
  with open(errf, "w") as err:
    r = subprocess.run(["sshfs", "-o", opts, remote, mnt], input=(password + "\n").encode(), stdout=subprocess.DEVNULL, stderr=err, timeout=30)
  r.stderr = open(errf).read()
  return r
def write(p, text): open(p, "w").write(text)

async def until(cond, t=20):
  t0 = time.time()
  while time.time() - t0 < t:
    if cond(): return True
    await asyncio.sleep(0.05)
  return False

def client_env(password):
  pw = os.path.join(S, "pw"); open(pw, "w").write(password)
  return os.environ | {"SSH_ASKPASS": os.path.join(S, "askpass.sh"), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0",
                       "SHARE_PW_FILE": pw}

def ssh_argv(link):
  """the shell command of the link, as the user would paste it (plus test settings for known hosts)"""
  import shlex
  if link.kind == "shell":
    argv = shlex.split(sh.client_commands(link)[0][1])
  else: # (a drive has no ssh command: the same login, by hand)
    host, port = link.target()
    argv = ["ssh", "-J", link.jump(), "-p", str(port), "-o", f"HostKeyAlias={sh.host_alias()}", f"slurp@{host}"]
  return [argv[0], "-tt", "-o", f"UserKnownHostsFile={KNOWN}", "-o", "StrictHostKeyChecking=accept-new", *argv[1:]]

class Term:
  """ssh in a pseudo terminal"""
  def __init__(self, argv, env, cols=100, rows=30):
    self.pid, self.fd = pty.fork()
    if self.pid == 0:
      os.execvpe(argv[0], argv, env)
    self.resize(cols, rows)
    self.out = b""
  def resize(self, cols, rows):
    fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
  def send(self, text):
    os.write(self.fd, text.encode())
  async def read_until(self, pattern, t=15):
    t0 = time.time()
    while time.time() - t0 < t:
      r, _, _ = select.select([self.fd], [], [], 0)
      if r:
        try:
          chunk = os.read(self.fd, 65536)
        except OSError:
          break
        if not chunk: break
        self.out += chunk
        if re.search(pattern, self.out.decode(errors="replace")): return True
      else:
        await asyncio.sleep(0.05)
    return False
  def wait(self, t=10):
    t0 = time.time()
    while time.time() - t0 < t:
      pid, status = os.waitpid(self.pid, os.WNOHANG)
      if pid: return os.waitstatus_to_exitcode(status)
      time.sleep(0.05)
    return None
