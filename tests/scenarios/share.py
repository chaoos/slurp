"""Lab test of slurp/share.py with real clients: the lab sshd is the relay (an own OpenSSH relay), a real ssh in a
pseudo terminal is the user's terminal, a real sshfs mounts the drive. Usage: SLURP_TEST_WORK=<lab dir> python tests/scenarios/share.py (the lab sshd runs)"""
import asyncio, os, pty, re, select, shutil, subprocess, sys, time, fcntl, struct, termios
HERE = os.environ.get("SLURP_TEST_WORK") or sys.exit("SLURP_TEST_WORK: the lab directory (see tests/lab)"); LAB = os.path.join(HERE, "sshlab"); S = os.path.join(HERE, "share")
os.environ["PATH"] = os.path.join(LAB, "bin") + ":" + os.environ["PATH"]
for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
  os.environ[k] = os.path.join(S, k.lower())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from slurp import share as sh

results = []
def check(name, cond, info=""):
  results.append(bool(cond)); print(f"{'PASS' if cond else 'FAIL'}  {name}  {info}", flush=True)

events = []
mgr = sh.ShareManager(lambda kind, link, session, msg: events.append((kind, link.id, msg)))

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

KNOWN = os.path.join(S, "client_known_hosts")
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

async def main():
  if os.environ.get("LAZY_TASKS") != "1": # as in the real app: Textual runs the loop with eager tasks
    asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
  shutil.rmtree(S + "/xdg_config_home", ignore_errors=True); shutil.rmtree(S + "/xdg_state_home", ignore_errors=True)
  if os.path.exists(KNOWN): os.unlink(KNOWN)
  check("asyncssh is there", sh.available() is None, sh.available())
  check("relay settings", sh.parse_relay("ssh-j.com") == ("ssh-j", "ssh-j.com") and sh.parse_relay("me@relay.example.org") == ("openssh", "me@relay.example.org")
        and sh.is_public("ssh-j.com") and not sh.is_public("lab") and sh.parse_relay("sshj://my.relay") == ("ssh-j", "my.relay"))

  # ---- a shell, once, through the lab as own relay
  link = mgr.create(sh.Link("shell", "shell (lab test)", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"], relay="lab-relay"))
  ok = await until(lambda: link.ready(), 20)
  check("the relay tunnel is up, the relay picked a port", ok and link.relay_port > 0 and link.port > 0, (link.relay_state, link.relay_error, link.relay_port))
  cmd = sh.client_commands(link)[0][1]
  check("the command for the user's terminal", cmd.startswith(f"ssh -J lab-relay -p {link.relay_port} -o HostKeyAlias=slurp-") and cmd.endswith("slurp@localhost"), cmd)
  t = Term(ssh_argv(link), client_env(link.password))
  ok = await t.read_until(r"\$ ", 20)
  check("ssh through the relay with the password: a shell", ok and link.logins == 1 and any(e[0] == "login" for e in events), t.out[-300:])
  known = open(KNOWN).read() if os.path.exists(KNOWN) else ""
  check("the client remembers slurp's host key under the alias (the fingerprint shown is that key)", sh.host_alias() in known, known[:80])
  t.send("echo $((6*7)); stty size\r")
  ok = await t.read_until(r"42\r?\n.*30 100", 10)
  check("commands run; the terminal has the client's size", ok, t.out[-200:])
  t.resize(120, 40); await asyncio.sleep(0.3)
  t.send("stty size\r")
  ok = await t.read_until(r"40 120", 10)
  check("resizing the client's window resizes the shell", ok, t.out[-120:])
  s = link.sessions[0] if link.sessions else None
  check("the session: kind, terminal, size, client, bytes", s and s.kind == "shell" and s.term and s.size == (120, 40) and "OpenSSH" in s.client
        and s.bytes_in > 0 and s.bytes_out > 0, s)
  check("used once: the link takes no more logins (the session goes on)", not link.open() and link.server is None and link.why_closed() == "used")
  t2 = Term(ssh_argv(link), client_env(link.password))
  code = await asyncio.get_running_loop().run_in_executor(None, t2.wait, 20)
  check("a second login is refused", code not in (0, None), (code, t2.out[-200:]))
  t.send("exit 3\r")
  code = await asyncio.get_running_loop().run_in_executor(None, t.wait, 10)
  ok = await until(lambda: s.ended is not None, 5)
  check("exit ends the session, with its exit code", ok and code == 3, code)
  ok = await until(lambda: link.relay_state == "closed", 10)
  check("nothing left: the relay tunnel closes", ok, link.relay_state)

  # ---- wrong passwords lock the link; disconnecting; revoking
  link = mgr.create(sh.Link("shell", "shell 2", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"], relay="lab-relay", uses=None))
  await until(lambda: link.ready(), 20)
  for i in range(sh.MAX_FAILURES):
    t = Term(ssh_argv(link) + ["-o", "NumberOfPasswordPrompts=1"], client_env("wrong"))
    await asyncio.get_running_loop().run_in_executor(None, t.wait, 15)
  check("wrong passwords: counted, then the link is locked", link.locked and link.failures == sh.MAX_FAILURES and not link.open()
        and sum(e[0] == "failure" for e in events if e[1] == link.id) == sh.MAX_FAILURES, (link.failures, link.locked))
  t = Term(ssh_argv(link), client_env(link.password))
  code = await asyncio.get_running_loop().run_in_executor(None, t.wait, 20)
  check("locked: even the right password is refused", code not in (0, None) and link.logins == 0, code)
  link = mgr.create(sh.Link("shell", "shell 3", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"], relay="lab-relay", uses=None))
  await until(lambda: link.ready(), 20)
  t = Term(ssh_argv(link), client_env(link.password))
  await t.read_until(r"\$ ", 20)
  mgr.disconnect(link.sessions[0])
  code = await asyncio.get_running_loop().run_in_executor(None, t.wait, 10)
  check("Disconnect ends the client's session", code is not None and link.sessions[0].ended is not None, code)
  check("a reusable link still takes logins after that", link.open())
  mgr.revoke(link)
  check("Revoke: no more logins", not link.open() and link.why_closed() == "revoked")

  # ---- expiry
  link = mgr.create(sh.Link("shell", "shell 4", "lab", lambda: ["bash"], relay="lab-relay", expiry=2))
  await until(lambda: link.ready(), 20)
  await asyncio.sleep(2.2); mgr.tick()
  check("expired: no more logins, the tunnel closes", not link.open() and link.why_closed() == "expired" and await until(lambda: link.relay_state == "closed", 10))

  # ---- key login
  pub = open(os.path.join(S, "clientkey.pub")).read().strip()
  link = mgr.create(sh.Link("shell", "shell key", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"], relay="lab-relay", auth="key", public_key=pub))
  await until(lambda: link.ready(), 20)
  t = Term(ssh_argv(link) + ["-o", "IdentitiesOnly=yes", "-i", os.path.join(S, "otherkey"), "-o", "PasswordAuthentication=no"], client_env("x"))
  code = await asyncio.get_running_loop().run_in_executor(None, t.wait, 20)
  check("key login: another key is refused", code not in (0, None) and link.logins == 0, code)
  t = Term(ssh_argv(link) + ["-o", "IdentitiesOnly=yes", "-i", os.path.join(S, "clientkey")], client_env("x"))
  ok = await t.read_until(r"\$ ", 20)
  check("key login: the pasted key gets in; the session shows its fingerprint", ok and link.sessions and link.sessions[0].key.startswith("SHA256:"), t.out[-200:])
  t.send("exit\r"); await asyncio.get_running_loop().run_in_executor(None, t.wait, 10)

  # ---- a drive: sshfs, many logins
  d = os.path.join(S, "home"); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
  open(os.path.join(d, "hello.txt"), "w").write("hi from the cluster\n")
  sftp = next(p for p in sh.SFTP_SERVERS if os.path.exists(p))
  link = mgr.create(sh.Link("drive", "drive (lab test)", "lab", lambda: [sftp, "-d", d], relay="lab-relay", uses=None, path=d))
  await until(lambda: link.ready(), 20)
  cmds = dict((title, text) for title, text, _ in sh.client_commands(link))
  sshfs = cmds["Linux, macOS (sshfs): one command"]
  check("the sshfs command", f"sshfs -o ProxyJump=lab-relay,HostKeyAlias=slurp-" in sshfs and f"port={link.relay_port}" in sshfs and f"slurp@localhost:{d}" in sshfs, sshfs)
  mnt = os.path.join(S, "mnt"); os.makedirs(mnt, exist_ok=True)
  subprocess.run(["fusermount", "-u", mnt], capture_output=True)
  opts = re.search(r"-o (\S+)", sshfs).group(1) + f",UserKnownHostsFile={KNOWN},StrictHostKeyChecking=accept-new,password_stdin"
  r = await blocking(mount, opts, f"slurp@localhost:{d}", mnt, link.password)
  ok = r.returncode == 0 and await blocking(os.path.exists, os.path.join(mnt, "hello.txt"))
  check("sshfs mounts the drive (the password on stdin)", ok, r.stderr[-300:])
  if ok:
    check("files are read through the drive", await blocking(read, os.path.join(mnt, "hello.txt")) == "hi from the cluster\n")
    await blocking(write, os.path.join(mnt, "new.txt"), "written locally\n")
    check("and written", open(os.path.join(d, "new.txt")).read() == "written locally\n")
    s = link.live_sessions[0] if link.live_sessions else None
    check("the drive session is listed, with bytes", s and s.kind == "drive" and s.bytes_in > 0 and s.bytes_out > 0, s)
    check("the session knows the folder sshfs mounted", s and s.path == d, s and s.path)
    ok = await until(lambda: (mgr.tick(), s.latency is not None)[1], 10)
    check("a heartbeat to the client: its round trip; the session and the link are healthy", ok and sh.session_health(s)[0] == "good"
          and "ms" in sh.session_health(s)[1] and sh.link_health(link) == ("good", "healthy"), (s.latency, sh.session_health(s), sh.link_health(link)))
    os.makedirs(os.path.join(d, "sub"), exist_ok=True)
    mnt2 = os.path.join(S, "mnt2")
    subprocess.run(["fusermount", "-u", mnt2], capture_output=True) # (one left by a run that stopped)
    os.makedirs(mnt2, exist_ok=True)
    r2 = await blocking(mount, opts, "slurp@localhost:sub", mnt2, link.password)
    ok = r2.returncode == 0 and await until(lambda: len(link.live_sessions) == 2, 10)
    second = next((x for x in link.live_sessions if x is not s), None)
    check("a second mount of another folder (relative to the login directory): its own session and path",
          ok and second is not None and second.path == os.path.join(d, "sub"), (r2.stderr[-200:], second and second.path))
    for _ in range(5): # (busy right after the mount: again)
      if (await blocking(subprocess.run, ["fusermount", "-u", mnt2], capture_output=True)).returncode == 0:
        break
      await asyncio.sleep(1)
    check("unmounting it ends its session (the first one stays)", second is None or await until(lambda: second.ended is not None, 20) and s.ended is None)
    await blocking(subprocess.run, ["fusermount", "-u", mnt], capture_output=True)
    check("unmounting ends the session", await until(lambda: not link.live_sessions, 10))
  check("a drive link takes more logins (mounts reconnect)", link.open() and link.logins >= 1)
  # a drive refuses a shell, a shell link refuses sftp
  t = Term(ssh_argv(link), client_env(link.password))
  ok = await t.read_until("drive \\(sftp\\) only", 20)
  check("a drive link gives no shell", ok, t.out[-200:])
  mgr.revoke(link)
  # read only
  link = mgr.create(sh.Link("drive", "drive ro", "lab", lambda: [sftp, "-R", "-d", d], relay="lab-relay", uses=None, path=d, read_only=True))
  await until(lambda: link.ready(), 20)
  opts = re.search(r"-o (\S+)", dict((a, b) for a, b, _ in sh.client_commands(link))["Linux, macOS (sshfs): one command"]).group(1) + f",UserKnownHostsFile={KNOWN},password_stdin"
  r = await blocking(mount, opts, f"slurp@localhost:{d}", mnt, link.password)
  if r.returncode == 0:
    try:
      await blocking(write, os.path.join(mnt, "nope.txt"), "x"); wrote = True
    except OSError:
      wrote = False
    check("read only: reading works, writing is refused", not wrote and await blocking(os.path.exists, os.path.join(mnt, "hello.txt")))
    await blocking(subprocess.run, ["fusermount", "-u", mnt], capture_output=True)
  else:
    check("read only mount", False, r.stderr[-300:])
  mgr.shutdown()
  log = open(os.path.join(sh.state_dir(), "links.log")).read()
  check("the log of links has links and sessions, no password", "created" in log and "session" in log and "locked" in log
        and all(l.password not in log for l in mgr.links), log.count("\n"))
  print(f"\n{sum(results)}/{len(results)} passed")

asyncio.run(main())
sys.exit(0 if results and all(results) else 1)
