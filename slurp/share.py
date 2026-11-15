"""Shells and drives of the cluster for the user's own terminal and file manager ("links").

A link is a one-time ticket: slurp runs a small SSH server (asyncssh) on 127.0.0.1 for it and makes that port
reachable through a relay, which both sides reach from the outside, so it works whatever lies between the user's
machine and the one slurp runs on (hops, NAT). A relay is just an SSH host both ends reach (the client jumps through
it, ssh -J; the session stays encrypted end to end); ssh-j.com is the default (public, no accounts), any OpenSSH
server works as well. The client logs in with a random password (or a public key) and gets
- a shell: the same command as a shell inside slurp, run in a pseudo terminal and passed through byte by byte
  (on the login node over the tab's SSH session, or on a node of a running job), or
- a drive: the SFTP server of the cluster (sftp-server over the tab's SSH session), passed through byte by byte, for
  sshfs, SSHFS-Win, WinSCP, file managers.

Relays (the setting "link_relay"):
- "ssh-j" (ssh-j.com, the default, public; or sshj://host, a relay of the same kind): no account on either end, a
  link is a random name there. slurp runs  ssh <name>@ssh-j.com -N -R slurp:22:127.0.0.1:<port>  and the client
  ssh -J <name>@ssh-j.com <user>@slurp ; <name> is random, links of other names are not reachable
- "openssh" (any OpenSSH server both ends can log in to, e.g. me@relay.example.org): slurp runs  ssh <relay> -N -R 0:127.0.0.1:<port> , the relay picks a free port P
  and the client runs  ssh -J <relay> -p P <user>@localhost
- "serveo" (serveo.net, public, no account): the same as ssh-j, the random name is the alias there: slurp runs
  ssh <name>@serveo.net -N -R <name>:22:127.0.0.1:<port>  (keyboard-interactive without prompts: anonymous) and the
  client  ssh -J <name>@serveo.net <user>@<name>
- "pinggy" (free.pinggy.io, public, no account): a public TCP port: slurp runs
  ssh -p 443 -T -R 0:127.0.0.1:<port> tcp@free.pinggy.io , which prints tcp://<host>:<P> ; the client runs
  ssh -p P <user>@<host>  (no jump; anyone on the internet reaches the port; a free tunnel lasts 60 minutes, then it
  comes back at another address)
- "sshreach" (sshreach.me, an account): sshreach://<client user>@<forwarding server>:<P> from its web interface:
  slurp runs  ssh <client user>@<server> -N -R P:127.0.0.1:<port>  (with the key registered there) and the client
  ssh -p P <user>@<server>
- "direct" (no relay: "direct", or direct://host[:port]): slurp's server listens on every interface of this machine
  (at that port, or a free one) and the client runs  ssh -p <port> <user>@<host> ; for a machine the client reaches
  itself (the same network, a VPN), without a third party; host is this machine's name unless given
The session stays SSH encrypted from the client to slurp; the relay only passes bytes along.
- "cloudflare" (web links only, no SSH at all): a Cloudflare quick tunnel (cloudflared, see cloudflare.py) gives the
  web view a random public HTTPS address; the browser opens it with the web view's token, nothing on the user's side.
  Cloudflare ends the HTTPS there: it sees what passes, unlike the SSH relays.

Everything here runs on the event loop of the app; the app gets events (logins, failures, ...) by a callback."""

import asyncio
import hashlib
import os
import re
import secrets
import shlex
import signal
import struct
import subprocess
import sys
import time
import fcntl
import termios
from dataclasses import dataclass, field
from datetime import datetime

from .state import log
from .terminal import CTTY

try:
  import asyncssh
except ImportError: # the feature is then unavailable, slurp works without it
  asyncssh = None

PUBLIC_RELAY = "ssh-j.com"
SERVEO = "serveo.net"
PINGGY = "free.pinggy.io"
DIRECT = "direct" # (a "relay" setting: no relay, the client connects to this machine itself)
CLOUDFLARE = "cloudflare" # (web links only: a Cloudflare quick tunnel to the web view, an HTTPS address; see cloudflare.py)
STRAIGHT = ("direct", "pinggy", "sshreach", "cloudflare") # (relay types the client connects to without a jump: a host and a port)
DEFAULT_RELAY = PUBLIC_RELAY
MAX_FAILURES = 5          # wrong passwords (or keys) until a link is locked
LINK_USER = "slurp"       # the user name in the commands (any name is accepted)
SFTP_SERVERS = ("/usr/lib/ssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/lib/openssh/sftp-server",
                "/usr/lib64/ssh/sftp-server", "/usr/libexec/sftp-server")
EXPIRY_CHOICES = [("15 minutes", 900), ("1 hour", 3600), ("8 hours", 8 * 3600), ("until slurp quits", None)]
DEFAULT_EXPIRY = 3600

def available():
  """None if links can be made here, else why not"""
  return None if asyncssh is not None else "needs the Python package asyncssh (pip install asyncssh)"

def config_dir():
  return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "slurp")

def state_dir():
  return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "slurp")

def audit(message):
  """A line in the log of links and sessions (never a password)"""
  try:
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), "links.log"), "a") as f:
      f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
  except OSError as e:
    log.warning(f"cannot write the log of links: {e}")

_host_key = None
def host_key():
  """The host key of slurp's SSH server, kept in the config directory (so clients that accepted it once are not
  asked again)"""
  global _host_key
  if _host_key is None:
    path = os.path.join(config_dir(), "link_host_key")
    try:
      _host_key = asyncssh.read_private_key(path)
    except (OSError, ValueError, asyncssh.KeyImportError):
      _host_key = asyncssh.generate_private_key("ssh-ed25519", comment="slurp links")
      os.makedirs(os.path.dirname(path), exist_ok=True)
      fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
      with os.fdopen(fd, "wb") as f:
        f.write(_host_key.export_private_key())
  return _host_key

def host_fingerprint():
  return host_key().get_fingerprint("sha256")

def host_alias():
  """The name under which clients remember slurp's host key (the same for every link)"""
  return "slurp-" + hashlib.sha256(host_fingerprint().encode()).hexdigest()[:8]

def parse_relay(relay):
  """("ssh-j", host), ("serveo", host), ("pinggy", host), ("sshreach", "user@server:port"), ("openssh", destination)
  or ("direct", "host[:port]" or "") for a relay setting ("ssh-j.com", "sshj://host", "serveo.net", "pinggy",
  "sshreach://user@server:port", "user@host", "direct", "direct://host:port" ...)"""
  relay = (relay or DEFAULT_RELAY).strip()
  if relay == DIRECT or relay.startswith(DIRECT + "://"):
    return DIRECT, relay.removeprefix(DIRECT).removeprefix("://").strip("/")
  if relay in (CLOUDFLARE, "trycloudflare.com"):
    return CLOUDFLARE, "trycloudflare.com"
  if relay in ("serveo", SERVEO):
    return "serveo", SERVEO
  if relay in ("pinggy", "pinggy.io", PINGGY):
    return "pinggy", PINGGY
  if relay.startswith("sshreach://"):
    return "sshreach", relay.removeprefix("sshreach://").strip("/")
  if relay.startswith("sshj://"):
    return "ssh-j", relay.removeprefix("sshj://")
  if relay == PUBLIC_RELAY or relay.endswith("@" + PUBLIC_RELAY):
    return "ssh-j", PUBLIC_RELAY
  return "openssh", relay

def direct_address(relay):
  """(host, port) of a direct link: the host the client connects to (this machine's name unless the setting has one)
  and the port to listen on (0: a free one)"""
  rest = parse_relay(relay)[1]
  m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9.-]*)(?::(\d{1,5}))?", rest)
  if rest and not m:
    raise ValueError(f"direct://{rest}: give a host name or address, and a port if you like (direct://host:port)")
  host, port = (m[1], int(m[2] or 0)) if m else ("", 0)
  if not 0 <= port <= 65535:
    raise ValueError(f"direct://{rest}: the port is not valid")
  if not host:
    import socket
    host = socket.getfqdn()
  return host, port

def sshreach_address(relay):
  """(user, server, port) of an sshreach.me relay (what its web interface shows for the client); ValueError if not
  valid"""
  rest = parse_relay(relay)[1]
  m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]{0,63})@([A-Za-z0-9][A-Za-z0-9.-]{0,252}):(\d{1,5})", rest)
  if not m or not 1 <= int(m[3]) <= 65535:
    raise ValueError("sshreach.me: give the client's user, its forwarding server and port, as user@fw1.sshreach.me:10248")
  return m[1], m[2], int(m[3])

def check_relay(relay):
  """ValueError if a relay setting cannot work (direct, sshreach and own relays: their form)"""
  kind, rest = parse_relay(relay)
  if kind == DIRECT:
    direct_address(relay)
  elif kind == "sshreach":
    sshreach_address(relay)
  elif kind == "openssh" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9.-]*)?", rest):
    raise ValueError(f"{rest}: give the relay as host or user@host (an SSH server you log in to with a key)")
  elif kind == "ssh-j" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", rest):
    raise ValueError(f"sshj://{rest}: not a host name")

def direct_relay(host, port=0):
  """The relay setting of a direct link to host (the name the client knows this machine by) and port (0: a free one);
  ValueError if they are not valid"""
  host, port = (host or "").strip(), str(port or "").strip()
  if port and not port.isdigit():
    raise ValueError(f"the port {port} is not a number")
  relay = f"{DIRECT}://{host}" + (f":{int(port)}" if port and int(port) else "") if host else DIRECT
  if not host and port and int(port):
    raise ValueError("give the host too (the name your machine knows this one by)")
  direct_address(relay)
  return relay

def is_public(relay):
  """Run by a third party (a public relay, or a service with an account)"""
  kind, host = parse_relay(relay)
  return (kind, host) in (("ssh-j", PUBLIC_RELAY), ("serveo", SERVEO), ("pinggy", PINGGY)) or kind in ("sshreach", CLOUDFLARE)

def die_with_parent():
  """In the child, before exec: the kernel ends it when slurp ends, even when slurp crashes or is killed (Linux;
  elsewhere nothing). A relay tunnel must never outlive slurp."""
  try:
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM) # PR_SET_PDEATHSIG
  except Exception:
    pass

def make_password():
  """20 characters without ones that are easy to confuse"""
  alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
  return "".join(secrets.choice(alphabet) for _ in range(20))

@dataclass(eq=False)
class Session:
  link: "Link"
  id: str
  kind: str = ""                 # "shell" or "drive"
  started: float = field(default_factory=time.time)
  client: str = ""               # the client's SSH version
  username: str = ""
  key: str = ""                  # fingerprint of the key it logged in with
  term: str = ""
  size: tuple = (0, 0)
  bytes_in: int = 0
  bytes_out: int = 0
  ended: float | None = None
  conn: object = None
  proc: object = None            # subprocess.Popen (shell) or asyncio process (drive)
  web_ports: set = field(default_factory=set) # a web session: the local ports of its forwarded connections to the
                                              # web view (the holder knows the port of each browser: which one it is)
  path: str = ""                 # a drive: the folder the client mounted (its first SFTP request names it)
  latency: float | None = None   # the round trip of the last heartbeat, seconds
  ping_sent: float | None = None # (monotonic) a heartbeat waits for its answer since then
  last_heard: float = float("-inf") # (monotonic) the client was last heard from: data or a heartbeat answer

  def responding(self):
    """Seconds the heartbeat waits for its answer (None: it is not waiting)"""
    return None if self.ping_sent is None else time.monotonic() - self.ping_sent

  def close(self):
    """Disconnect: end what it runs and the SSH connection"""
    proc = self.proc
    if proc is not None and getattr(proc, "returncode", None) is None:
      try:
        os.killpg(proc.pid, signal.SIGHUP) if isinstance(proc, subprocess.Popen) else proc.terminate()
      except (OSError, ProcessLookupError):
        pass
    if self.conn is not None:
      self.conn.close()

@dataclass(eq=False)
class Link:
  kind: str                      # "shell", "drive" or "web" (the slurp running here, in a browser)
  title: str                     # what it opens ("shell on daint", "drive: daint /users/x")
  tab: str                       # the tab it belongs to
  argv: object                   # () -> argv of the command a session runs (a shell: in a pty; a drive: sftp)
  relay: str = DEFAULT_RELAY
  uses: int | None = 1           # logins allowed (None: any number until it expires)
  expiry: int | None = DEFAULT_EXPIRY # seconds until it takes no new logins (None: until slurp quits)
  auth: str = "password"         # or "key"
  public_key: str = ""           # the authorized key (auth "key")
  path: str = ""                 # a drive: its directory (the login directory)
  dirs: list = field(default_factory=list) # a drive: [(name, path)] of $HOME, $SCRATCH, ...: the helper offers them
  read_only: bool = False
  web_port: int = 0              # a web link: the port of the web view on 127.0.0.1 (the only place it forwards to)
  web_token: str = ""            # a web link: the token of the web view's URL
  id: str = field(default_factory=lambda: secrets.token_hex(4))
  name: str = field(default_factory=lambda: secrets.token_hex(12)) # the random name at an ssh-j relay
  password: str = field(default_factory=make_password)
  created: float = field(default_factory=time.time)
  logins: int = 0
  failures: int = 0
  revoked: bool = False
  locked: bool = False
  port: int = 0                  # of slurp's server for it, on 127.0.0.1
  relay_port: int = 0            # openssh relay: the port at the relay; pinggy: the public port
  public_host: str = ""          # pinggy, cloudflare: the public host it gave
  relay_note: str = ""           # what happens while it starts (cloudflare: downloading cloudflared)
  relay_state: str = "starting"  # starting, connecting, ready, broken, closed
  relay_losses: list = field(default_factory=list) # (monotonic) when the relay tunnel was lost (then it reconnects)
  relay_error: str = ""
  sessions: list = field(default_factory=list)
  server: object = None
  tunnel: object = None          # asyncio process of the relay tunnel
  task: object = None

  @property
  def expires(self):
    return None if self.expiry is None else self.created + self.expiry

  def expired(self, now=None):
    return self.expires is not None and (now or time.time()) >= self.expires

  def open(self, now=None):
    """Takes new logins"""
    return not (self.revoked or self.locked or self.expired(now)) and (self.uses is None or self.logins < self.uses)

  def why_closed(self):
    return ("revoked" if self.revoked else "locked after wrong passwords" if self.locked else "expired" if self.expired()
            else "used" if self.uses is not None and self.logins >= self.uses else "")

  @property
  def live_sessions(self):
    return [s for s in self.sessions if s.ended is None]

  @property
  def finished(self):
    """Nothing left: no new logins and no session"""
    return not self.open() and not self.live_sessions

  @property
  def relay_type(self):
    return parse_relay(self.relay)[0]

  @property
  def relay_host(self):
    """The relay (its host), or for a direct link the host the client connects to"""
    kind = self.relay_type
    if kind == DIRECT:
      return direct_address(self.relay)[0]
    if kind == "sshreach":
      return sshreach_address(self.relay)[1]
    return parse_relay(self.relay)[1]

  @property
  def direct(self):
    """No relay: slurp's server listens on this machine's interfaces"""
    return self.relay_type == DIRECT

  @property
  def straight(self):
    """The client connects to a host and port without a jump (direct, or a public TCP port of pinggy, sshreach)"""
    return self.relay_type in STRAIGHT

  @property
  def client_kind(self):
    """What the client does, for the Windows helper and the drive window: "ssh-j", "serveo", "openssh" (a jump to
    the target), "direct" (to slurp's machine), "tcp" (to a public TCP port of a relay)"""
    return {"pinggy": "tcp", "sshreach": "tcp"}.get(self.relay_type, self.relay_type)

  def jump(self):
    """The -J argument of the client ("" without a jump)"""
    if self.straight:
      return ""
    return f"{self.name}@{self.relay_host}" if self.relay_type in ("ssh-j", "serveo") else self.relay_host

  def target(self):
    """(host, port) the client connects to (behind the relay)"""
    kind = self.relay_type
    if kind == DIRECT:
      return self.relay_host, self.port
    if kind in ("pinggy", CLOUDFLARE):
      return self.public_host, self.relay_port
    if kind == "sshreach":
      return self.relay_host, sshreach_address(self.relay)[2]
    return ("slurp", 22) if kind == "ssh-j" else (self.name, 22) if kind == "serveo" else ("localhost", self.relay_port)

  def ready(self):
    return self.relay_state == "ready" and bool(self.target()[1]) and bool(self.target()[0])

  @property
  def cloudflare(self):
    """A web link through a Cloudflare quick tunnel: no SSH at all, the browser opens url()"""
    return self.relay_type == CLOUDFLARE

  def url(self):
    """The address to open in the browser (a web link)"""
    if self.cloudflare:
      return f"https://{self.public_host}/?token={self.web_token}"
    return f"http://localhost:8000/?token={self.web_token}"

UNSTABLE = 600 # seconds: reconnects within this long make a link (or a drive) unstable

def session_health(session):
  """("good" | "warn" | "bad", in words) of a session: its heartbeat, as for the SSH login of a tab (None: ended)"""
  from .remote import PING_SLOW, SLOW_ROUND_TRIP
  if session.ended is not None:
    return None, "ended"
  waiting, latency = session.responding(), session.latency
  ms = f" {latency * 1000:.0f} ms" if latency is not None else ""
  if waiting is not None and waiting > PING_SLOW:
    return "bad", f"no answer for {waiting:.0f} s"
  if waiting is not None and waiting > SLOW_ROUND_TRIP or latency is not None and latency > SLOW_ROUND_TRIP:
    return "warn", "slow" + ms
  return "good", "healthy" + ms

def link_health(link):
  """("good" | "warn" | "bad", in words) of a link: its relay tunnel, and how often it was lost lately (None: closed)"""
  state = link.relay_state
  recent = [t for t in link.relay_losses if time.monotonic() - t < UNSTABLE]
  if state == "closed":
    return None, "closed"
  if state == "broken":
    return "bad", "relay lost"
  if state in ("starting", "connecting"):
    return ("bad", "relay lost, reconnecting") if recent else ("warn", "connecting")
  if recent:
    return "warn", f"unstable: relay reconnected {len(recent)}× in {UNSTABLE // 60} min"
  return "good", "listening" if link.direct else "healthy"

def drive_path(data, home):
  """The folder an SFTP client works in, from the start of what it sends (None: not named yet): the path of its first
  request that has one (sshfs: LSTAT of the folder it mounts; others: REALPATH of "."), relative to home"""
  pos = 0
  while len(data) - pos >= 5:
    size = struct.unpack(">I", data[pos:pos + 4])[0]
    if len(data) - pos < 4 + size:
      return None
    kind = data[pos + 4]
    if kind in (3, 7, 9, 11, 13, 14, 15, 16, 17, 19) and size >= 9: # (OPEN, LSTAT, SETSTAT, OPENDIR, ..., STAT, READLINK)
      length = struct.unpack(">I", data[pos + 9:pos + 13])[0]
      path = data[pos + 13:pos + 13 + length].decode(errors="replace")
      if not path.startswith("/"):
        path = os.path.normpath(os.path.join(home or "~", path))
      return path
    pos += 4 + size
  return None

def client_commands(link):
  """What the user runs on their own machine: [(title, text, note)]. The commands are complete only when the relay
  is ready (an own relay picks its port then)."""
  host, port = link.target()
  user = LINK_USER
  alias = host_alias()
  p = [] if port == 22 else ["-p", str(port)]
  ssh_opts = [*([] if link.straight else ["-J", link.jump()]), *p, "-o", f"HostKeyAlias={alias}"]
  out = []
  if link.cloudflare:
    return [("Open in your browser (anywhere: nothing to install, no password)", link.url(),
             "the address is the key: whoever has it gets this slurp, until you revoke the link or it expires. Each "
             "new tunnel (also after a lost one) has a new address: copy it again then")]
  if link.kind == "web":
    local = 8000
    url = f"http://localhost:{local}/?token={link.web_token}"
    out.append(("1. The tunnel: Linux, macOS, Windows (OpenSSH)",
                shlex.join(["ssh", "-L", f"{local}:127.0.0.1:{link.web_port}", *ssh_opts, f"{user}@{host}"]),
                f"paste it into a terminal, cmd or PowerShell on your machine. It asks for the password (below), then "
                f"says that the tunnel is open and shows the URL: keep that window open while you use the web view "
                f"(Ctrl-C there closes it). If port {local} is taken on your machine, use another one, here and in the URL"))
    out.append(("2. Then open in your browser", url,
                "the same slurp as here. One view is active at a time: as long as this one is, the browser shows a lock "
                "screen with Take over; a click on it (and Yes) makes the browser the active view"))
    out.append(("PuTTY", f"{'host ' + host + ', port ' + str(port) if link.straight else 'the proxy settings as for a shell link'}, and Connection > SSH > Tunnels: source port {local}, "
                f"destination 127.0.0.1:{link.web_port}, Local, Add; Connection > SSH: Don't start a shell", "then the URL"))
    return out
  if link.kind == "shell":
    out.append(("Linux, macOS, Windows (OpenSSH)", shlex.join(["ssh", *ssh_opts, f"{user}@{host}"]),
                "works in a terminal, cmd or PowerShell; asks for the password"))
  else:
    mount = "~/" + re.sub(r"[^\w.-]", "_", link.tab or "cluster")
    jump = ",".join([*([] if link.straight else [f"ProxyJump={link.jump()}"]), f"HostKeyAlias={alias}", "reconnect", "ServerAliveInterval=15",
                     *([f"port={port}"] if port != 22 else [])])
    out.append(("Linux, macOS (sshfs): one command", f"mkdir -p {mount} && sshfs -o {jump} {user}@{host}:{shlex.quote(link.path)} {mount}",
                f"asks for the password, then the drive is {mount}; unmount: fusermount -u {mount} (Linux), umount {mount} (macOS)"))
    if link.auth == "password":
      from . import windrive
      helper = windrive.command(link)
      where = "PowerShell or cmd" if len(helper) <= windrive.CMD_LIMIT else "PowerShell (too long for cmd)"
      out.append((f"Windows: map the drive with a small window (paste into {where})", helper,
                  "opens a window: pick the drive letter, paste the password, Map drive; it shows each step (relay, "
                  "host key check, login, mapped), reconnects, and unmaps when closed. Needs SSHFS-Win and WinFsp (it "
                  "offers to install them). Too long for the Run dialog (Win+R): paste it into a terminal window"))
    local = 2222
    if link.straight: # (no tunnel: SSHFS-Win connects to that host and port itself)
      out.append(("Windows by hand (SSHFS-Win): map the drive", f"net use X: \\\\sshfs\\{user}@{host}!{port}",
                  "or in Explorer: Map network drive, folder \\\\sshfs\\" + f"{user}@{host}!{port}; "
                  f"Windows asks for the user ({user}) and the password of this link; the drive is your login directory"))
    else:
      forward = ["-L", f"{local}:{host}:{port}", link.jump()]
      out.append(("Windows by hand (SSHFS-Win), step 1: the tunnel, in a terminal (it asks nothing and stays open)",
                  shlex.join(["ssh", "-N", *forward]),
                  f"it connects to the relay and then only waits, carrying port {local} of your computer to slurp: no "
                  "password here, no output; leave it open while the drive is used, close it (Ctrl-C) after unmapping"))
      out.append(("Windows by hand (SSHFS-Win), step 2: map the drive, while step 1 runs",
                  f"net use X: \\\\sshfs\\{user}@localhost!{local}",
                  "or in Explorer: Map network drive, folder \\\\sshfs\\" + f"{user}@localhost!{local}; "
                  f"Windows asks for the user ({user}) and the password of this link; the drive is your login directory"))
  if link.straight:
    putty = f"Session: host {host}, port {port}; Connection > Data: auto-login user {user}"
  elif link.relay_type in ("ssh-j", "serveo"):
    putty = (f"Session: host {host}, port {port}; Connection > Proxy: type SSH, proxy host {link.relay_host}, port 22, "
             f"user {link.name}; Connection > Data: auto-login user {user}")
  else:
    putty = (f"Session: host localhost, port {port}; Connection > Proxy: type SSH, proxy host and user of "
             f"{link.relay_host}; Connection > Data: auto-login user {user}")
  out.append(("PuTTY / WinSCP (jump host settings)" if link.kind == "drive" else "PuTTY", putty,
              "WinSCP: the same as Advanced > Connection > Tunnel" if link.kind == "drive" else "PuTTY 0.77 or newer"))
  lines = [f"Host {alias}-{link.id}", f"  HostName {host}", f"  User {user}",
           *([] if link.straight else [f"  ProxyJump {link.jump()}"]), f"  HostKeyAlias {alias}"] + ([f"  Port {port}"] if port != 22 else [])
  out.append(("~/.ssh/config entry", "\n".join(lines),
              f"then: ssh {alias}-{link.id}" + (f", sshfs {alias}-{link.id}:{link.path} <dir>, sftp://{alias}-{link.id}/ in a file manager"
                                                 if link.kind == "drive" else "")))
  return out

class ShareManager:
  """The links of the app and their sessions. events(kind, link, session, message) is called on the event loop for
  "login", "failure", "locked", "ended", "relay", "closed"."""

  def __init__(self, events=lambda *a: None):
    self.links = []
    self.events = events

  # ---- links
  def create(self, link):
    """Start a link: its server and its relay tunnel (in the background)"""
    if link.cloudflare:
      if link.kind != "web":
        raise ValueError("Cloudflare carries the web view only (shells and drives need an SSH relay)")
      link.uses, link.auth = None, "token" # (no logins to count: the address with its token opens it)
    elif asyncssh is None:
      raise RuntimeError(available())
    else:
      host_key() # (created now, not in a login)
    self.links.append(link)
    audit(f"link {link.id} created: {link.kind} '{link.title}' (tab {link.tab}), relay {link.relay}, "
          f"{'once' if link.uses == 1 else f'{link.uses} logins' if link.uses else 'any number of logins'}, "
          f"expires {'never' if link.expiry is None else datetime.fromtimestamp(link.expires).isoformat(timespec='seconds')}, "
          f"{link.auth} login" + (", read only" if link.read_only else ""))
    link.task = asyncio.ensure_future(self._run(link))
    return link

  async def _run(self, link):
    if link.cloudflare:
      await self._cloudflare(link)
      return
    try:
      where, port = ("127.0.0.1", 0)
      if link.direct: # (every interface; only that one if the setting names an address: direct://192.168.1.5)
        import ipaddress
        host, port = direct_address(link.relay)
        try:
          where = str(ipaddress.ip_address(host))
        except ValueError:
          where = ""
      link.server = await asyncssh.create_server(
        lambda: _Server(self, link), where, port, server_host_keys=[host_key()],
        encoding=None, agent_forwarding=False, x11_forwarding=False, allow_scp=False, login_timeout=60,
        server_version="slurp")
      link.port = link.server.sockets[0].getsockname()[1]
    except Exception as e:
      link.relay_state, link.relay_error = "broken", f"cannot start the server: {e}"
      log.exception("starting the server of a link failed")
      self.events("relay", link, None, link.relay_error)
      return
    if link.direct:
      await self._direct(link)
    else:
      await self._tunnel(link)

  async def _direct(self, link):
    """A direct link: ready as soon as its server listens; no tunnel to keep up"""
    link.relay_state, link.relay_error = "ready", ""
    audit(f"link {link.id}: listening directly, for {link.relay_host} port {link.port}")
    self.events("relay", link, None, "")
    while not link.finished:
      await asyncio.sleep(1)
    link.relay_state = "closed"
    self.events("relay", link, None, "")

  async def _tunnel(self, link):
    """Keep the tunnel to the relay up while the link takes logins or has sessions (it carries them)"""
    delay = 2
    while not link.finished:
      link.relay_state = "connecting"
      self.events("relay", link, None, "")
      kind = link.relay_type
      known = ["-o", f"UserKnownHostsFile={os.path.join(config_dir(), 'relay_known_hosts')}", "-o", "StrictHostKeyChecking=accept-new"]
      anonymous = ["-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no"] # (no keys of the user to a public relay)
      session, batch, extra = ["-N"], "yes", []
      if kind == "ssh-j":
        forward, dest, extra = f"slurp:22:127.0.0.1:{link.port}", f"{link.name}@{link.relay_host}", known
      elif kind == "serveo": # (anonymous: keyboard-interactive with no prompts; BatchMode would refuse it)
        forward, dest, batch = f"{link.name}:22:127.0.0.1:{link.port}", f"{link.name}@{link.relay_host}", "no"
        extra = [*known, *anonymous, "-o", "PreferredAuthentications=keyboard-interactive"]
      elif kind == "pinggy": # (it prints the public address in the session: no -N, its input kept open)
        forward, dest, session = f"0:127.0.0.1:{link.port}", f"tcp@{link.relay_host}", []
        extra = [*known, *anonymous, "-p", "443"]
      elif kind == "sshreach":
        user, server, rport = sshreach_address(link.relay)
        forward, dest, extra = f"{rport}:127.0.0.1:{link.port}", f"{user}@{server}", known
      else:
        forward, dest = f"0:127.0.0.1:{link.port}", link.relay_host
      argv = ["ssh", "-v", *session, "-T", "-R", forward, "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
              "-o", "ServerAliveCountMax=3", "-o", f"BatchMode={batch}", "-o", "ControlPath=none", "-o", "ControlMaster=no",
              *extra, dest]
      log.debug(f"relay tunnel of link {link.id}: {shlex.join(argv)}")
      started = time.monotonic()
      try:
        proc = link.tunnel = await asyncio.create_subprocess_exec(*argv, stdin=subprocess.PIPE if kind == "pinggy" else subprocess.DEVNULL,
                                                                  stdout=subprocess.PIPE if kind == "pinggy" else subprocess.DEVNULL,
                                                                  stderr=subprocess.PIPE, start_new_session=True,
                                                                  preexec_fn=die_with_parent if sys.platform.startswith("linux") else None)
      except OSError as e:
        link.relay_state, link.relay_error = "broken", f"cannot run ssh: {e}"
        self.events("relay", link, None, link.relay_error)
        return
      last = []
      def became_ready():
        nonlocal delay
        link.relay_state, link.relay_error, delay = "ready", "", 2
        audit(f"link {link.id}: relay ready ({link.relay}" + (f", {link.public_host} port {link.relay_port})" if kind == "pinggy" else ")"))
        self.events("relay", link, None, "")
      async def public_address(): # (pinggy: tcp://host:port on its output)
        while True:
          line = await proc.stdout.readline()
          if not line:
            return
          m = re.search(r"tcp://([A-Za-z0-9][A-Za-z0-9.-]*):(\d{1,5})\b", line.decode(errors="replace"))
          if m and link.relay_state != "ready":
            link.public_host, link.relay_port = m[1], int(m[2])
            became_ready()
      reader = asyncio.ensure_future(public_address()) if kind == "pinggy" else None
      if kind == "pinggy":
        link.public_host, link.relay_port = "", 0
      while True:
        line = await proc.stderr.readline()
        if not line:
          break
        text = line.decode(errors="replace").strip()
        m = re.search(r"Allocated port (\d+) for remote forward", text) if kind == "openssh" else None
        if m:
          link.relay_port = int(m.group(1))
        if ("remote forward success" in text or m) and kind != "pinggy":
          if kind != "openssh" or link.relay_port:
            became_ready()
        if not text.startswith("debug"):
          last = (last + [text])[-3:]
      await proc.wait()
      if reader is not None:
        reader.cancel()
      link.tunnel = None
      if link.finished:
        break
      link.relay_state = "broken"
      if not (kind == "pinggy" and time.monotonic() - started > 55 * 60): # (pinggy: a new address every hour)
        link.relay_losses = [t for t in link.relay_losses if time.monotonic() - t < UNSTABLE] + [time.monotonic()]
      link.relay_error = " | ".join(last) or f"ssh ended with {proc.returncode}"
      log.warning(f"relay tunnel of link {link.id} ended: {link.relay_error}")
      self.events("relay", link, None, link.relay_error)
      if time.monotonic() - started > 60:
        delay = 2
      await asyncio.sleep(delay)
      delay = min(delay * 2, 60)
    link.relay_state = "closed"
    self.events("relay", link, None, "")

  async def _cloudflare(self, link):
    """A Cloudflare quick tunnel to the web view, kept up while the link lasts (a new one, at a new address, when
    it is lost)"""
    from . import cloudflare
    delay = 2
    while not link.finished:
      link.relay_state, link.public_host, link.relay_port = "connecting", "", 0
      self.events("relay", link, None, "")
      path = cloudflare.binary()
      if path is None:
        def progress(done, total):
          link.relay_note = f"downloading cloudflared: {done >> 20} of {total >> 20 or '?'} MB"
        link.relay_note = "downloading cloudflared"
        try:
          path = await asyncio.to_thread(cloudflare.download, progress)
        except Exception as e:
          link.relay_state, link.relay_error, link.relay_note = "broken", f"cannot get cloudflared: {e}", ""
          self.events("relay", link, None, link.relay_error)
          return
        audit(f"link {link.id}: downloaded cloudflared {cloudflare.version(path)} to {path}")
        link.relay_note = ""
      argv = cloudflare.argv(path, link.web_port)
      log.debug(f"cloudflare tunnel of link {link.id}: {shlex.join(argv)}")
      started = time.monotonic()
      try:
        proc = link.tunnel = await asyncio.create_subprocess_exec(*argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                                  stderr=subprocess.PIPE, start_new_session=True,
                                                                  preexec_fn=die_with_parent if sys.platform.startswith("linux") else None)
      except OSError as e:
        link.relay_state, link.relay_error = "broken", f"cannot run cloudflared: {e}"
        self.events("relay", link, None, link.relay_error)
        return
      host, last, waiting = "", [], None
      async def in_dns(host):
        """Ready once the address is in public DNS: a browser that asks too early gets "no such name" and its
        resolver keeps that for minutes"""
        nonlocal delay
        link.relay_note = "waiting for the address to be in DNS"
        self.events("relay", link, None, "")
        for _ in range(60):
          found = await asyncio.to_thread(cloudflare.resolves, host)
          if found is None: # (that resolver cannot be asked from here: a few seconds are usually enough)
            await asyncio.sleep(8)
            break
          if found:
            await asyncio.sleep(1)
            break
          await asyncio.sleep(1)
        if proc.returncode is None and not link.finished:
          link.public_host, link.relay_port, link.relay_note = host, 443, ""
          link.relay_state, link.relay_error, delay = "ready", "", 2
          audit(f"link {link.id}: Cloudflare tunnel ready at https://{host}")
          self.events("relay", link, None, "")
      while True:
        line = await proc.stderr.readline()
        if not line:
          break
        text = line.decode(errors="replace").strip()
        m = cloudflare.ADDRESS.search(text)
        if m and not host:
          host = m[1]
        if host and cloudflare.CONNECTED in text and waiting is None and link.relay_state != "ready":
          waiting = asyncio.ensure_future(in_dns(host))
        if " ERR " in f" {text} ":
          last = (last + [re.sub(r"^\S+ (ERR|WRN|INF) ", "", text)])[-2:]
      await proc.wait()
      if waiting is not None:
        waiting.cancel()
      link.tunnel, link.relay_note = None, ""
      if link.finished:
        break
      link.relay_state = "broken"
      link.relay_losses = [t for t in link.relay_losses if time.monotonic() - t < UNSTABLE] + [time.monotonic()]
      link.relay_error = " | ".join(last) or f"cloudflared ended with {proc.returncode}"
      log.warning(f"cloudflare tunnel of link {link.id} ended: {link.relay_error}")
      self.events("relay", link, None, link.relay_error)
      if time.monotonic() - started > 60:
        delay = 2
      await asyncio.sleep(delay)
      delay = min(delay * 2, 60)
    link.relay_state = "closed"
    self.events("relay", link, None, "")

  def tick(self):
    """Every second or so: close links that expired, were used up or have nothing left; heartbeats"""
    self.heartbeat()
    for link in self.links:
      if link.server is not None and not link.open():
        link.server.close() # no new logins; the sessions go on
        link.server = None
        audit(f"link {link.id} takes no more logins: {link.why_closed()}")
        self.events("closed", link, None, link.why_closed())
      if link.finished and link.tunnel is not None and link.tunnel.returncode is None:
        try:
          link.tunnel.terminate()
        except ProcessLookupError:
          pass

  def heartbeat(self):
    """A session not heard from for PING_INTERVAL (and a new one at once) gets a heartbeat: a global request its
    client answers (keepalive@openssh.com; a refusal is an answer too), as the tab's SSH login has"""
    from .remote import PING_INTERVAL
    now = time.monotonic()
    for session in self.live_sessions():
      if session.conn is not None and session.ping_sent is None and now - session.last_heard >= PING_INTERVAL:
        session.ping_sent = now
        asyncio.ensure_future(self._ping(session))

  async def _ping(self, session):
    try:
      await session.conn._make_global_request(b"keepalive@openssh.com")
    except Exception as e: # (asyncssh's own call: if it is gone, no heartbeat)
      log.debug(f"heartbeat of session {session.id}: {e!r}")
      return
    if session.ping_sent is not None:
      now = time.monotonic()
      session.latency, session.last_heard, session.ping_sent = now - session.ping_sent, now, None

  def revoke(self, link, end_sessions=True):
    link.revoked = True
    audit(f"link {link.id} revoked")
    if end_sessions:
      for s in link.live_sessions:
        s.close()
    self.tick()

  def disconnect(self, session):
    audit(f"session {session.id} of link {session.link.id} disconnected by the user")
    session.close()

  def new_password(self, link):
    link.password = make_password()
    link.failures = 0
    audit(f"link {link.id}: new password")

  def extend(self, link, seconds):
    if link.expiry is not None:
      link.expiry = max(link.expiry, int(time.time() - link.created) + seconds)
      audit(f"link {link.id}: extended to {datetime.fromtimestamp(link.expires).isoformat(timespec='seconds')}")

  def active(self):
    """Links still taking logins or with sessions"""
    return [l for l in self.links if not l.finished]

  def live_sessions(self):
    return [s for l in self.links for s in l.live_sessions]

  def shutdown(self):
    for link in self.links:
      link.revoked = True
      for s in link.live_sessions:
        s.close()
      if link.server is not None:
        link.server.close()
      if link.tunnel is not None and link.tunnel.returncode is None:
        try:
          link.tunnel.terminate()
        except ProcessLookupError:
          pass

  # ---- sessions
  def _started(self, session):
    link = session.link
    audit(f"session {session.id} of link {link.id} started: {session.kind}, client {session.client!r}"
          + (f", key {session.key}" if session.key else ""))
    self.events("login", link, session, "")

  def _ended(self, session):
    if session.ended is not None:
      return
    session.ended = time.time()
    audit(f"session {session.id} of link {session.link.id} ended after {session.ended - session.started:.0f} s, "
          f"{session.bytes_in} bytes in, {session.bytes_out} bytes out")
    self.events("ended", session.link, session, "")
    self.tick()

class _Channel(asyncssh.SSHServerSession if asyncssh else object):
  """A session channel of a link: a shell in a pseudo terminal, or the cluster's sftp-server, passed through byte by
  byte (asyncssh's own SFTP server would serve the files of this machine, so the low level session API is used)"""
  def __init__(self, manager, link, conn):
    self.manager, self.link, self.conn = manager, link, conn
    self.chan = self.term = self.mode = self.session = self.fd = self.proc = None
    self.size, self.refusal, self.paused, self.closed = (80, 24), "", False, False
    self.out = b"" # input for the shell that its terminal did not take yet
    self.early = [] # data for the sftp server that came while it was starting
    self.early_eof = False
    self.sftp_start = b"" # a drive: what the client sent first, until it names its folder
    self.can_write = asyncio.Event()
    self.can_write.set()

  # requests of the client
  def connection_made(self, chan):
    self.chan = chan

  def pty_requested(self, term_type, term_size, term_modes):
    self.term, self.size = term_type or "xterm-256color", tuple(term_size[:2]) # (a client without TERM sends none)
    return True

  def shell_requested(self):
    if self.link.kind == "shell":
      self.mode = "shell"
    else:
      self.mode, self.refusal = "refuse", "this link is a drive (sftp) only: use sshfs, SSHFS-Win, WinSCP or sftp"
    return True

  def exec_requested(self, command):
    self.mode = "refuse"
    self.refusal = ("this link gives an interactive shell only (no commands)" if self.link.kind == "shell"
                    else "this link is a drive (sftp) only: use sshfs, SSHFS-Win, WinSCP or sftp")
    return True

  def subsystem_requested(self, subsystem):
    if subsystem == "sftp" and self.link.kind == "drive":
      self.mode = "drive"
    else:
      self.mode, self.refusal = "refuse", f"this link has no {subsystem}" + (" (it is a shell)" if self.link.kind == "shell" else "")
    return True

  def session_started(self):
    log.debug(f"link {self.link.id}: session started ({self.mode}, terminal {self.term})")
    if self.mode == "shell" and not self.term:
      self.mode, self.refusal = "refuse", "this link needs a terminal (ssh -t)"
    if self.mode in (None, "refuse"):
      self.chan.write_stderr((self.refusal or "nothing to run") .encode() + b"\r\n")
      self.chan.exit(1)
      return
    session = next((s for s in self.link.sessions if s.conn is self.conn and not s.kind), None)
    if session is None: # another channel of the same connection
      session = Session(self.link, secrets.token_hex(3), conn=self.conn, client=self.conn.get_extra_info("client_version") or "",
                        username=self.conn.get_extra_info("username") or "")
      self.link.sessions.append(session)
    self.session = session
    session.kind = self.mode
    try:
      argv = self.link.argv()
      log.debug(f"session {session.id}: {shlex.join(argv)}")
      if self.mode == "shell":
        self.start_shell(argv)
      else:
        asyncio.ensure_future(self.run_drive(argv))
    except Exception as e:
      log.exception(f"session {session.id} of link {self.link.id} could not start")
      self.chan.write_stderr(f"slurp: cannot start: {e}\r\n".encode())
      self.manager._ended(session)
      self.chan.exit(1)

  # a shell
  def start_shell(self, argv):
    session = self.session
    session.term = self.term
    master, slave = os.openpty()
    self.fd = master
    self.winsize(*self.size)
    session.proc = self.proc = subprocess.Popen([sys.executable, "-c", CTTY, *argv], stdin=slave, stdout=slave, stderr=slave,
                                                start_new_session=True, env=os.environ | {"TERM": self.term}, close_fds=True)
    os.close(slave)
    os.set_blocking(master, False)
    asyncio.get_running_loop().add_reader(master, self.pty_readable)
    self.manager._started(session)

  def winsize(self, cols, rows):
    cols, rows = cols or 80, rows or 24
    if self.session is not None:
      self.session.size = (cols, rows)
    if self.fd is not None:
      try:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
      except OSError:
        pass

  def terminal_size_changed(self, width, height, pixwidth, pixheight):
    self.size = (width, height)
    self.winsize(width, height)

  def pty_readable(self):
    try:
      data = os.read(self.fd, 65536)
    except BlockingIOError:
      return
    except OSError: # EIO: the command closed its terminal
      data = b""
    if not data:
      asyncio.get_running_loop().remove_reader(self.fd)
      asyncio.ensure_future(self.shell_ended())
      return
    self.session.bytes_out += len(data)
    self.chan.write(data)

  async def shell_ended(self):
    for _ in range(40):
      if self.proc.poll() is not None:
        break
      await asyncio.sleep(0.05)
    code = self.proc.returncode if self.proc.returncode is not None else 0
    self.finish(code if code >= 0 else 128 - code) # (killed by a signal: 128 + its number, as a shell says)

  def write_pty(self):
    loop = asyncio.get_running_loop()
    while self.out:
      try:
        n = os.write(self.fd, self.out)
        self.out = self.out[n:]
      except BlockingIOError:
        loop.add_writer(self.fd, self.write_pty)
        return
      except OSError:
        self.out = b""
    loop.remove_writer(self.fd)

  # a drive
  async def run_drive(self, argv):
    session = self.session
    try:
      self.proc = session.proc = await asyncio.create_subprocess_exec(*argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                                      stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e: # (the client gets an error at once instead of waiting for its timeout)
      log.warning(f"session {session.id} of link {self.link.id} could not start: {e}")
      self.manager._ended(session)
      self.finish(127)
      return
    self.manager._started(session)
    for data in self.early: # (the client does not wait: its first packets came while the server started)
      self.proc.stdin.write(data)
    if self.early_eof:
      self.proc.stdin.write_eof()
    self.early = []
    while True:
      data = await self.proc.stdout.read(65536)
      if not data:
        break
      await self.can_write.wait()
      if self.closed:
        break
      session.bytes_out += len(data)
      self.chan.write(data)
    try:
      code = await asyncio.wait_for(self.proc.wait(), 5)
    except asyncio.TimeoutError:
      self.proc.kill()
      code = 137
    self.finish(code if code >= 0 else 128 - code)

  async def drain(self):
    try:
      await self.proc.stdin.drain()
    except (ConnectionError, BrokenPipeError):
      pass
    if not self.closed:
      self.chan.resume_reading()

  # data from the client
  def data_received(self, data, datatype):
    if self.session is None or datatype: # (nothing runs, or stderr data)
      return
    self.session.bytes_in += len(data)
    self.session.last_heard = time.monotonic()
    if self.mode == "drive" and not self.session.path and self.sftp_start is not None:
      self.sftp_start += data
      path = drive_path(self.sftp_start, self.link.path)
      if path is not None or len(self.sftp_start) > 1 << 16:
        self.session.path, self.sftp_start = path or "", None
    if self.mode == "shell":
      self.out += data
      self.write_pty()
    elif self.proc is None:
      self.early.append(data)
    elif self.proc.stdin is not None:
      self.proc.stdin.write(data)
      if self.proc.stdin.transport.get_write_buffer_size() > 1 << 20: # wait until it took most of it
        self.chan.pause_reading()
        asyncio.ensure_future(self.drain())

  def eof_received(self):
    if self.mode == "drive" and self.proc is None:
      self.early_eof = True
      return True
    if self.mode == "drive" and self.proc is not None and self.proc.stdin is not None:
      try:
        self.proc.stdin.write_eof()
      except (OSError, AttributeError):
        pass
      return True # (the server may still answer)
    return False

  def pause_writing(self):
    self.can_write.clear()
    if self.fd is not None and self.mode == "shell":
      asyncio.get_running_loop().remove_reader(self.fd)

  def resume_writing(self):
    self.can_write.set()
    if self.fd is not None and self.mode == "shell" and not self.closed:
      asyncio.get_running_loop().add_reader(self.fd, self.pty_readable)

  # the end
  def finish(self, code):
    if self.closed:
      return
    try:
      self.chan.exit(code)
    except Exception:
      pass
    self.cleanup()

  def connection_lost(self, exc):
    self.cleanup()

  def cleanup(self):
    if self.closed:
      return
    self.closed = True
    self.can_write.set()
    loop = asyncio.get_running_loop()
    proc = self.proc
    if isinstance(proc, subprocess.Popen) and proc.poll() is None: # the client went away: end the command
      try:
        os.killpg(proc.pid, signal.SIGHUP)
      except OSError:
        pass
    elif proc is not None and not isinstance(proc, subprocess.Popen) and proc.returncode is None:
      try:
        proc.terminate()
      except ProcessLookupError:
        pass
    if self.fd is not None:
      loop.remove_reader(self.fd)
      loop.remove_writer(self.fd)
      try:
        os.close(self.fd)
      except OSError:
        pass
      self.fd = None
    if self.session is not None:
      self.manager._ended(self.session)

class _WebNotice(asyncssh.SSHServerSession if asyncssh else object):
  """The session of a web link (ssh -L ... without -N): it runs nothing, it says that the tunnel is open and which URL
  to open, and keeps the connection (and so the tunnel) until Ctrl-C, q or the end of the connection"""
  def __init__(self, manager, link, conn):
    self.manager, self.link, self.conn = manager, link, conn
    self.chan = None

  def connection_made(self, chan):
    self.chan = chan

  def pty_requested(self, term_type, term_size, term_modes):
    return True

  def shell_requested(self):
    return True

  def exec_requested(self, command):
    return False # (no commands)

  def subsystem_requested(self, subsystem):
    return False

  def session_started(self):
    link = self.link
    session = next((s for s in link.sessions if s.conn is self.conn and s.ended is None), None)
    if session is not None and not session.kind:
      session.kind = "web"
      self.manager._started(session)
    url = f"http://localhost:8000/?token={link.web_token}"
    self.chan.write((f"\r\nslurp: the tunnel to the web view is open.\r\n"
                     f"  Open in your browser:  {url}\r\n"
                     f"  (with another local port than 8000 in the ssh command: that port in the URL)\r\n"
                     f"  Keep this window open while you use it; Ctrl-C (or q) here closes the tunnel.\r\n").encode())

  def data_received(self, data, datatype):
    if b"\x03" in data or b"\x04" in data or data.strip().lower() == b"q":
      self.chan.write(b"\r\nslurp: closing the tunnel\r\n")
      self.chan.exit(0)

  def eof_received(self):
    self.chan.exit(0)

  def connection_lost(self, exc):
    pass

class _Server(asyncssh.SSHServer if asyncssh else object):
  """One connection to a link: its login"""
  def __init__(self, manager, link):
    self.manager, self.link = manager, link

  def connection_made(self, conn):
    self.conn = conn
    log.debug(f"link {self.link.id}: connection from {conn.get_extra_info('peername')}")

  def session_requested(self):
    log.debug(f"link {self.link.id}: session requested")
    if self.link.kind == "web":
      return _WebNotice(self.manager, self.link, self.conn) # (it only says that the tunnel is open, and where to go)
    return _Channel(self.manager, self.link, self.conn)

  def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
    """ssh -L: only a web link, only to its web view"""
    link = self.link
    allowed = link.kind == "web" and link.web_port and dest_port == link.web_port and dest_host in ("127.0.0.1", "localhost")
    log.debug(f"link {link.id}: forwarding to {dest_host}:{dest_port} {'allowed' if allowed else 'refused'}")
    if not allowed:
      return False
    session = next((s for s in link.sessions if s.conn is self.conn and s.ended is None), None)
    if session is not None and not session.kind:
      session.kind = "web"
      self.manager._started(session)
    async def forward():
      """As asyncssh forwards (True), keeping the local port: the web view sees it as the browser's port"""
      from asyncssh.forward import SSHForwarder
      try:
        transport, peer = await asyncio.get_running_loop().create_connection(SSHForwarder, "127.0.0.1", link.web_port)
      except OSError as exc:
        raise asyncssh.ChannelOpenError(asyncssh.OPEN_CONNECT_FAILED, str(exc)) from None
      if session is not None:
        session.web_ports.add(transport.get_extra_info("sockname")[1])
      return SSHForwarder(peer)
    return forward()

  def begin_auth(self, username):
    return True # a login is always needed

  def password_auth_supported(self):
    return self.link.auth == "password"

  def public_key_auth_supported(self):
    return self.link.auth == "key"

  def kbdint_auth_supported(self):
    return False

  fingerprint = ""

  async def validate_password(self, username, password):
    link = self.link
    log.debug(f"link {link.id}: password for {username!r}")
    await asyncio.sleep(0) # (see validate_public_key)
    if link.open() and secrets.compare_digest(password.encode(), link.password.encode()):
      return True # (counted when the login completes)
    return await self._reject(username)

  async def validate_public_key(self, username, key):
    """Only checks: a client first asks which of its keys would do, then logs in with one (keys are not counted
    as failures either: a client may offer several, and they cannot be guessed).
    Both checks pause once before they answer: Textual runs the event loop with eager tasks, so asyncssh's login
    task starts inside create_task, before asyncssh has stored it as the connection's login; an answer without
    a pause came before that, and asyncssh dropped the success (the client waited until the login timeout)."""
    await asyncio.sleep(0)
    try:
      wanted = asyncssh.import_public_key(self.link.public_key)
    except (ValueError, asyncssh.KeyImportError):
      return False
    if self.link.open() and key.public_data == wanted.public_data:
      self.fingerprint = key.get_fingerprint("sha256")
      return True
    return False

  def auth_completed(self):
    link = self.link
    log.debug(f"link {link.id}: login completed")
    if not link.open(): # (used up meanwhile by another login)
      self.conn.close()
      return
    self._accept(self.conn.get_extra_info("username") or "", self.fingerprint)

  def _accept(self, username, fingerprint):
    link = self.link
    link.logins += 1
    link.sessions.append(Session(link, secrets.token_hex(3), conn=self.conn, username=username, key=fingerprint,
                                 client=self.conn.get_extra_info("client_version") or ""))
    audit(f"link {link.id}: login {link.logins} as {username!r}" + (f" with key {fingerprint}" if fingerprint else ""))
    self.manager.tick()

  async def _reject(self, username):
    link = self.link
    if not link.open():
      return False
    link.failures += 1
    audit(f"link {link.id}: failed login {link.failures} as {username!r}")
    self.manager.events("failure", link, None, f"wrong password ({link.failures} of {MAX_FAILURES})")
    if link.failures >= MAX_FAILURES:
      link.locked = True
      audit(f"link {link.id} locked after {link.failures} failed logins")
      self.manager.events("locked", link, None, "")
      self.manager.tick()
    await asyncio.sleep(1) # (slows down guessing)
    return False

  def connection_lost(self, exc):
    for s in self.link.sessions:
      if s.conn is self.conn and s.ended is None and s.proc is None: # logged in, but never ran anything
        s.ended = time.time()
        self.manager.events("ended", self.link, s, "")
