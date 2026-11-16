#!/usr/bin/env python3
"""slurp helper for Linux: opens slurp links (slurp://shell, drive, web) with one click and the link's password.

The setup (slurp/links/linux/__init__.py) puts this file into ~/.local/share/slurp-helper/slurp-helper and registers it
for slurp:// links (a .desktop file, xdg-mime; no root). It needs python3 (only its standard library), ssh and
ssh-keyscan; a drive also needs sshfs. It asks with zenity or kdialog (or in the terminal it runs in) and reports with
desktop notifications.

A link is one connection, run by one process of this script (it stays while the connection is open):
  the tunnel to the relay (ssh -N -L, no password; none for a direct link) -> slurp's host key through it
  (ssh-keyscan, checked against the fingerprint in the link) -> drive: sshfs of the folder picked ($HOME, $SCRATCH,
  ...) at ~/slurp/<link>/<folder>, then the file manager; web view: ssh -N -L to the web view, then the browser;
  shell: a terminal window that runs all of it and then ssh, which asks for the password there.
The password is not in the link and never written to disk: ssh gets it from this script as its askpass program.

  slurp-helper                 the open connections (a list to pick one: open or stop it)
  slurp-helper URL             open a link (what the browser runs); also: slurp-helper open URL
  slurp-helper list            the open connections, in the terminal
  slurp-helper stop ID|all     end connections (unmount a drive, close a web view)
  slurp-helper uninstall       stop all and remove the helper
Options of open: --password-stdin (no dialog), --folder PATH (a drive: no dialog), --batch (no dialogs,
notifications or browser; messages on stderr), --yes (trust an unknown slurp without asking).

Kept to Python 3.8 and its standard library: it runs on the user's machine, where slurp is not installed."""

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

VERSION = "dev" # (the setup puts slurp's version here)
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "slurp-helper")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "slurp-helper")
CONNS = os.path.join(STATE, "connections")
TRUST = os.path.join(DATA, "trusted.txt")
ICON = os.path.join(DATA, "slurp.png")
MOUNTS = os.path.expanduser("~/slurp")
KIND_WORD = {"drive": "Drive", "shell": "Shell", "web": "Web view"}

BATCH = False # (no dialogs, notifications or browser)


class Failed(Exception):
  pass


# ---- messages: the log, notifications, dialogs
def log(text):
  try:
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "helper.log"), "a") as f:
      f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
  except OSError:
    pass

def notify(title, text="", urgent=False):
  log(f"{title}: {text}")
  if BATCH or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    print(f"slurp helper: {title}{': ' + text if text else ''}", file=sys.stderr)
    return
  if shutil.which("notify-send"):
    run_quiet(["notify-send", "-a", "slurp helper", *(["-i", ICON] if os.path.exists(ICON) else []),
               *(["-u", "critical"] if urgent else []), title, text])
  elif shutil.which("kdialog"):
    run_quiet(["kdialog", "--title", "slurp helper", "--passivepopup", f"{title}\n{text}", "8"])
  else:
    print(f"slurp helper: {title}{': ' + text if text else ''}", file=sys.stderr)

def run_quiet(argv, timeout=10):
  try:
    subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
  except (OSError, subprocess.SubprocessError):
    pass

def gui():
  """zenity, kdialog, or None (then the terminal, if there is one)"""
  if BATCH or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    return None
  for name in ("zenity", "kdialog"):
    if shutil.which(name):
      return name
  return None

def ask_dialog(argv):
  """The dialog's answer (its stdout, stripped), or None when it was cancelled"""
  try:
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
  except (OSError, subprocess.SubprocessError):
    return None
  return r.stdout.decode(errors="replace").rstrip("\n") if r.returncode == 0 else None

def tty():
  return sys.stdin.isatty() and not BATCH

def error(title, text):
  """A failure: a dialog the user sees (a notification may pass unseen), and the log"""
  log(f"error: {title}: {text}")
  g = gui()
  if g == "zenity":
    subprocess.Popen(["zenity", "--error", "--title", "slurp helper", "--width", "480", "--no-markup", "--text", f"{title}\n\n{text}"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
  elif g == "kdialog":
    subprocess.Popen(["kdialog", "--title", "slurp helper", "--error", f"{title}\n\n{text}"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
  else:
    print(f"slurp helper: {title}: {text}", file=sys.stderr)

def ask_yes(title, text):
  g = gui()
  if g == "zenity":
    return ask_dialog(["zenity", "--question", "--title", "slurp helper", "--width", "520", "--no-markup", "--text", f"{title}\n\n{text}"]) is not None
  if g == "kdialog":
    return ask_dialog(["kdialog", "--title", "slurp helper", "--yesno", f"{title}\n\n{text}"]) is not None
  if tty():
    print(f"{title}\n\n{text}\n")
    return input("yes/no? ").strip().lower() in ("y", "yes")
  return False

def ask_password(link):
  what = f"{KIND_WORD[link['kind']]}: {link['title']}"
  g = gui()
  if g == "zenity":
    return ask_dialog(["zenity", "--password", "--title", f"slurp helper · {what}"])
  if g == "kdialog":
    return ask_dialog(["kdialog", "--title", "slurp helper", "--password", f"{what}\n\nThe password of the link (in slurp's link window: Copy):"])
  if tty():
    import getpass
    return getpass.getpass(f"{what}\nThe password of the link: ")
  raise Failed("no way to ask for the password: install zenity (or kdialog), or run: slurp-helper open --password-stdin URL")

def ask_folder(link):
  """The folder a drive maps: one slurp sent ($HOME, $SCRATCH, ...), / or typed in"""
  choices = [(f"${name}", path) for name, path in link["dirs"]] + [("/", "/")]
  g = gui()
  if len(choices) == 1 and g is None and not tty():
    return "/"
  if g == "zenity":
    argv = ["zenity", "--list", "--radiolist", "--title", "slurp helper · map a folder", "--width", "560", "--height", "360",
            "--text", f"{link['title']}: which folder?", "--column", "", "--column", "Folder", "--column", "Path",
            "--print-column", "3"]
    for i, (name, path) in enumerate(choices + [("other …", "")]):
      argv += ["TRUE" if i == 0 else "FALSE", name, path]
    picked = ask_dialog(argv)
    if picked is None:
      return None
    if picked:
      return picked
    typed = ask_dialog(["zenity", "--entry", "--title", "slurp helper · map a folder",
                        "--text", "The folder on the cluster: an absolute path, $SCRATCH/run1, or relative to $HOME"])
    return None if typed is None else resolve_folder(link, typed)
  if g == "kdialog":
    argv = ["kdialog", "--title", "slurp helper", "--radiolist", f"{link['title']}: which folder?"]
    for i, (name, path) in enumerate(choices):
      argv += [path, f"{name}   {path}" if name != "/" else "/   (the whole file system)", "on" if i == 0 else "off"]
    return ask_dialog(argv)
  if tty():
    for i, (name, path) in enumerate(choices, 1):
      print(f"  {i}. {name}   {path}")
    typed = input("The folder (a number, or a path): ").strip()
    if typed.isdigit() and 1 <= int(typed) <= len(choices):
      return choices[int(typed) - 1][1]
    return resolve_folder(link, typed)
  return choices[0][1]

def resolve_folder(link, text):
  """The absolute path a typed folder means ($NAME of the folders slurp sent, ~, relative to $HOME)"""
  t = text.strip()
  dirs = dict(link["dirs"])
  home = dirs.get("HOME", "")
  m = re.match(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)$", t)
  if m:
    base = dirs.get(m.group(1).upper())
    if base is None:
      raise Failed(f"slurp did not send ${m.group(1)} (it sent: {', '.join('$' + n for n in dirs) or 'nothing'})")
    t = base + m.group(2)
  elif t.startswith("~"):
    t = home + t[1:]
  elif not t.startswith("/"):
    if not home:
      raise Failed("type an absolute path (/...)")
    t = home + "/" + t
  t = re.sub("/{2,}", "/", t)
  t = t.rstrip("/") or "/"
  if re.search(r"[\x00-\x1f\x7f]", t) or re.search(r"(^|/)\.\.?(/|$)", t) or len(t) > 1000:
    raise Failed(f"not a folder to map: {t[:100]}")
  return t


# ---- the link: slurp://kind?..., every value checked (as the Windows helper does)
def alias_of(fingerprint):
  return "slurp-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:8]

def parse_link(url):
  url = url.strip()
  if len(url) > 4096:
    raise Failed("the link is too long")
  m = re.match(r"^slurp:(?://)?(drive|shell|web)/?\?([^#]*?)/?$", url)
  if not m:
    raise Failed("this is not a slurp link (slurp://drive, shell or web)")
  kind = m.group(1)
  q = {}
  for pair in m.group(2).split("&"):
    if "=" in pair:
      k, v = pair.split("=", 1)
      q[k] = urllib.parse.unquote_plus(v)

  def need(key, pattern):
    v = q.get(key, "")
    if not re.fullmatch(pattern, v):
      raise Failed(f"the link has no valid '{key}'")
    return v
  if need("v", r"\d+") != "1":
    raise Failed("this link is for a newer slurp helper: set the helper up again from slurp")
  link = {
    "kind": kind,
    "id": need("id", r"[0-9a-f]{8}"),
    "relay": need("r", r"ssh-j|serveo|openssh|direct|tcp"),
    "relay_host": need("rh", r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}"),
    "host": need("h", r"slurp|localhost|[A-Za-z0-9][A-Za-z0-9.-]{0,252}"),
    "port": int(need("p", r"\d{1,5}")),
    "fp": need("fp", r"SHA256:[A-Za-z0-9+/]{43}"),
    "alias": need("a", r"slurp-[0-9a-f]{8}"),
    "user": need("u", r"[A-Za-z0-9_-]{1,32}"),
    "expires": int(need("x", r"\d{1,12}")),
    "read_only": q.get("ro") == "1",
    "title": re.sub(r"[\x00-\x1f\x7f]", " ", q.get("t", ""))[:120] or f"{kind} link",
    "path": re.sub(r"[\x00-\x1f\x7f]", " ", q.get("path", "")),
    "slurp_version": q.get("sv", "") if re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,30}", q.get("sv", "")) else "",
    "dirs": [], "web_port": 0, "token": "", "jump": "",
  }
  link["straight"] = link["relay"] in ("direct", "tcp")
  if link["relay"] in ("serveo", "ssh-j"):
    link["jump"] = need("j", r"[0-9a-f]{24}@[A-Za-z0-9][A-Za-z0-9.-]*")
    if link["relay"] == "serveo" and (link["host"] != link["jump"].split("@")[0] or link["port"] != 22):
      raise Failed("the link's host is not valid")
  elif not link["straight"]:
    link["jump"] = need("j", r"[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?")
  if link["relay"] not in ("serveo",) and not link["straight"] and link["host"] not in ("slurp", "localhost"):
    raise Failed("the link's host is not valid")
  if not 1 <= link["port"] <= 65535:
    raise Failed("the link's port is not valid")
  if alias_of(link["fp"]) != link["alias"]:
    raise Failed("the link's host key and its name do not match")
  if kind == "drive":
    for entry in q.get("dirs", "").split("\n"):
      m = re.fullmatch(r"([A-Z][A-Z0-9_]{0,31})=(/[^\x00-\x1f\x7f]{0,1000})", entry)
      if m and len(link["dirs"]) < 16:
        link["dirs"].append((m.group(1), m.group(2)))
    if link["path"].startswith("/") and not any(n == "HOME" for n, _ in link["dirs"]):
      link["dirs"].insert(0, ("HOME", link["path"]))
  if kind == "web":
    link["web_port"] = int(need("wp", r"\d{1,5}"))
    link["token"] = need("tok", r"[A-Za-z0-9_-]{8,64}")
    if not 1 <= link["web_port"] <= 65535:
      raise Failed("the link's web port is not valid")
  return link


# ---- slurps known here: their host keys
def trusted():
  try:
    with open(TRUST) as f:
      return {line.split()[1] for line in f if len(line.split()) == 2}
  except OSError:
    return set()

def trust(link, yes=False):
  if link["fp"] in trusted():
    return
  if not (yes or ask_yes("A slurp this helper does not know yet",
                         f"{link['alias']}\nhost key {link['fp']}\n{link['title']}\n\nUse the link only if you just made it "
                         "in your own slurp. Trust this slurp from now on?")):
    raise Failed(f"not trusted: {link['alias']} ({link['fp']})")
  os.makedirs(DATA, exist_ok=True)
  with open(TRUST, "a") as f:
    f.write(f"{link['alias']} {link['fp']}\n")
  log(f"trusted {link['alias']} {link['fp']}")


# ---- the steps: tunnel, host key, login
def free_port():
  s = socket.socket()
  s.bind(("127.0.0.1", 0))
  port = s.getsockname()[1]
  s.close()
  return port

def answers(port, host="127.0.0.1"):
  try:
    with socket.create_connection((host, port), timeout=1):
      return True
  except OSError:
    return False

def last_lines(path, n=3):
  try:
    with open(path, errors="replace") as f:
      lines = [line.strip() for line in f if line.strip() and not line.startswith("debug")]
  except OSError:
    return "ssh ended"
  return " / ".join(lines[-n:]) or "ssh ended"

def need_tool(name, why):
  path = shutil.which(name)
  if not path:
    raise Failed(f"{name} is missing: {why}")
  return path

class Connection:
  """One link opened: its folder (logs, known_hosts, state), its processes"""

  def __init__(self, link):
    self.link = link
    self.cid = f"{link['id']}-{os.getpid()}"
    self.dir = os.path.join(CONNS, self.cid)
    os.makedirs(self.dir, mode=0o700, exist_ok=True)
    self.tunnel = self.client = None
    self.port, self.host = link["port"], link["host"]
    self.local = self.mount = None
    self.state = "connecting"
    self.since = time.time()
    self.save()

  def say(self, text):
    log(f"{self.link['kind']} {self.link['id']}: {text}")
    if self.link["kind"] == "shell" or tty():
      print(f"slurp helper: {text}", file=sys.stderr if not sys.stdout.isatty() else sys.stdout, flush=True)

  def save(self):
    info = {"pid": os.getpid(), "kind": self.link["kind"], "title": self.link["title"], "id": self.link["id"],
            "alias": self.link["alias"], "state": self.state, "since": self.since, "mount": self.mount,
            "url": f"http://127.0.0.1:{self.local}/?token={self.link['token']}" if self.link["kind"] == "web" and self.local else None,
            "expires": self.link["expires"], "relay": self.link["relay_host"] if not self.link["straight"] else "direct"}
    try:
      with open(os.path.join(self.dir, "state.json"), "w") as f:
        json.dump(info, f)
    except OSError:
      pass

  def start_tunnel(self):
    """ssh -N -L to the relay (no password: the relay takes anyone, slurp's sshd behind it checks the password)"""
    link = self.link
    known = []
    batch = "yes"
    if link["relay"] in ("ssh-j", "serveo"):
      known = ["-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={os.path.join(DATA, 'relay_hosts')}"]
    if link["relay"] == "serveo": # (anonymous: keyboard-interactive without prompts, which BatchMode refuses)
      known += ["-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive"]
      batch = "no"
    argv = [need_tool("ssh", "install OpenSSH (e.g. sudo apt install openssh-client)"), "-N", "-v", "-E", os.path.join(self.dir, "tunnel.log"),
            "-o", f"BatchMode={batch}", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            *known, "-L", f"127.0.0.1:{self.port}:{link['host']}:{link['port']}", link["jump"]]
    self.tunnel = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

  def connect(self):
    """The tunnel (unless the link is direct) and slurp's host key: then self.host:self.port is slurp's sshd"""
    link = self.link
    if link["straight"]:
      self.say(f"connecting {'directly ' if link['relay'] == 'direct' else ''}to {self.host} port {self.port} ...")
    else:
      self.host, self.port = "127.0.0.1", free_port()
      self.say(f"connecting to the relay {link['relay_host']} ...")
      self.start_tunnel()
      t0 = time.time()
      while not answers(self.port):
        if self.tunnel.poll() is not None:
          raise Failed(f"the relay connection ended: {last_lines(os.path.join(self.dir, 'tunnel.log'))}")
        if time.time() - t0 > 30:
          raise Failed(f"no answer from the relay {link['relay_host']} within 30 s")
        time.sleep(0.2)
      self.say("connected to the relay")
    self.check_host_key()

  def check_host_key(self):
    link = self.link
    keyscan = need_tool("ssh-keyscan", "it comes with OpenSSH (e.g. sudo apt install openssh-client)")
    line = ""
    for attempt in range(3):
      try:
        r = subprocess.run([keyscan, "-T", "10", "-t", "ed25519", "-p", str(self.port), self.host],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        line = next((ln for ln in r.stdout.decode(errors="replace").splitlines() if " ssh-ed25519 " in ln), "")
      except subprocess.SubprocessError:
        line = ""
      if line:
        break
      time.sleep(0.7)
    if not line:
      if link["relay"] == "direct":
        raise Failed(f"slurp does not answer at {self.host} port {self.port}: is this computer in a network that reaches it (VPN?), "
                     "is slurp running, the link still valid?")
      raise Failed("slurp does not answer: is slurp running, and the link still valid (not revoked or expired)?")
    kind, blob = line.split()[1:3]
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")
    if fp != link["fp"]:
      raise Failed(f"WRONG HOST KEY: got {fp}, the link says {link['fp']}. Something between you and slurp is not slurp: "
                   "do not use this link.")
    with open(os.path.join(self.dir, "known_hosts"), "w") as f:
      f.write(f"{link['alias']} {kind} {blob}\n")
    self.say(f"host key checked: {fp}")

  def ssh_options(self, prompts=1):
    kh = os.path.join(self.dir, "known_hosts")
    return ["-o", f"HostKeyAlias={self.link['alias']}", "-o", f"UserKnownHostsFile={kh}", "-o", f"GlobalKnownHostsFile={kh}",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password", "-o", f"NumberOfPasswordPrompts={prompts}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]

  def askpass_env(self, password):
    """ssh asks this script for the password (never on disk): SSH_ASKPASS, with the password in its environment"""
    env = dict(os.environ)
    env.update({"SSH_ASKPASS": os.path.abspath(__file__), "SSH_ASKPASS_REQUIRE": "force", "SLURP_HELPER_ASKPASS": "1",
                "SLURP_HELPER_PASSWORD": password})
    env.setdefault("DISPLAY", ":0") # (older ssh use askpass only with a display)
    return env

  def keep_tunnel(self):
    """The tunnel ended (the relay dropped it): open it again on the same port, so ssh and sshfs can reconnect"""
    if self.tunnel is not None and self.tunnel.poll() is not None:
      self.say(f"the relay connection ended ({last_lines(os.path.join(self.dir, 'tunnel.log'), 1)}); again ...")
      time.sleep(3)
      self.start_tunnel()

  def close(self):
    for p in (self.client, self.tunnel):
      if p is not None and p.poll() is None:
        p.terminate()
        try:
          p.wait(5)
        except subprocess.TimeoutExpired:
          p.kill()
    shutil.rmtree(self.dir, ignore_errors=True)


# ---- the kinds
def drive(c, password, folder, open_it):
  link = c.link
  sshfs = need_tool("sshfs", "a drive needs it (e.g. sudo apt install sshfs, sudo dnf install fuse-sshfs)")
  if not os.path.exists("/dev/fuse"):
    raise Failed("/dev/fuse is missing: this machine cannot mount drives (FUSE)")
  name = next((n for n, p in link["dirs"] if p == folder), None) or (os.path.basename(folder) or "root")
  base = re.sub(r"[^\w.@-]+", "_", link["title"]).strip("_") or link["id"]
  mnt = os.path.join(MOUNTS, base, name)
  if mounted(mnt):
    raise Failed(f"{mnt} is mounted already (slurp-helper stop, or: fusermount -u {shlex.quote(mnt)})")
  if os.path.isdir(mnt) and os.listdir(mnt):
    raise Failed(f"{mnt} is not empty")
  c.connect()
  os.makedirs(mnt, exist_ok=True)
  opts = ["reconnect", f"port={c.port}", "ServerAliveInterval=15", "ServerAliveCountMax=3"] + \
         [o for o in c.ssh_options()[1::2] if not o.startswith("ServerAlive")] + (["ro"] if link["read_only"] else [])
  argv = [sshfs, "-f", "-o", ",".join(opts), f"{link['user']}@{c.host}:{folder}", mnt]
  c.say(f"mounting {folder} at {mnt} (password) ...")
  errf = open(os.path.join(c.dir, "sshfs.err"), "w")
  c.client = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf, env=c.askpass_env(password))
  # (the mount point shows before the login is done: mounted when it answers a stat, made in a thread, since a stat
  # of a FUSE mount waits for sshfs; a wrong password ends sshfs instead)
  import threading
  answered = threading.Event()
  def probe():
    while c.client.poll() is None and not answered.is_set():
      if mounted(mnt):
        try:
          os.stat(mnt)
          if mounted(mnt):
            answered.set()
            return
        except OSError:
          pass
      time.sleep(0.2)
  threading.Thread(target=probe, daemon=True).start()
  t0 = time.time()
  while not answered.is_set():
    if c.client.poll() is not None:
      why = last_lines(os.path.join(c.dir, "sshfs.err"))
      if "ermission denied" in why or "Connection reset" in why or "closed by remote host" in why:
        why += " (wrong password, or the link takes no more logins: expired or revoked; 5 wrong passwords lock it)"
      unmount(mnt)
      remove_dir(mnt)
      raise Failed(f"the drive was not mounted: {why}")
    if time.time() - t0 > 45:
      c.client.terminate()
      unmount(mnt)
      remove_dir(mnt)
      raise Failed("the drive was not mounted within 45 s")
    time.sleep(0.2)
  c.mount, c.state = mnt, "connected"
  c.save()
  c.say(f"mounted: {mnt}")
  notify(f"Drive mounted: {link['title']}", f"{folder} is at {mnt}. Unmount: eject it in the file manager, or slurp-helper stop {c.cid}")
  if open_it:
    open_thing(mnt)
  try:
    while c.client.poll() is None: # (until it is unmounted: by the file manager, fusermount -u, or stop)
      c.keep_tunnel()
      time.sleep(1)
  finally:
    unmount(mnt)
    remove_dir(mnt)
  c.say("unmounted")
  notify(f"Drive unmounted: {link['title']}", mnt)

def unmount(mnt):
  if mnt and mounted(mnt):
    for tool in ("fusermount3", "fusermount"):
      if shutil.which(tool):
        run_quiet([tool, "-u", "-z", mnt])
        break

def mounted(path):
  """Whether path is a mount point, from /proc/self/mounts: never touches the mount (a stat of a FUSE mount whose
  connection hangs would hang too)"""
  path = os.path.abspath(path)
  try:
    with open("/proc/self/mounts") as f:
      points = [line.split()[1] for line in f if len(line.split()) > 1]
  except OSError:
    return os.path.ismount(path)
  return any(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), p) == path for p in points)

def remove_dir(mnt):
  try:
    os.rmdir(mnt)
    os.rmdir(os.path.dirname(mnt))
  except OSError:
    pass

def web(c, password, open_it):
  link = c.link
  c.connect()
  c.local = free_port()
  argv = [need_tool("ssh", "install OpenSSH"), "-N", "-E", os.path.join(c.dir, "client.log"), "-o", "ExitOnForwardFailure=yes",
          "-L", f"127.0.0.1:{c.local}:127.0.0.1:{link['web_port']}", "-p", str(c.port), *c.ssh_options(),
          f"{link['user']}@{c.host}"]
  c.say("logging in (password) ...")
  c.client = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=c.askpass_env(password), start_new_session=True)
  t0 = time.time()
  while not answers(c.local): # (ssh opens the local port only after the login)
    if c.client.poll() is not None:
      why = last_lines(os.path.join(c.dir, "client.log"))
      if "ermission denied" in why:
        why += " (wrong password, or the link takes no more logins: expired or revoked; 5 wrong passwords lock it)"
      raise Failed(f"the login ended: {why}")
    if time.time() - t0 > 60:
      raise Failed("the login took too long")
    time.sleep(0.2)
  url = f"http://127.0.0.1:{c.local}/?token={link['token']}"
  c.state = "connected"
  c.save()
  c.say(f"the web view is at http://127.0.0.1:{c.local}/")
  notify(f"Web view open: {link['title']}", f"in your browser. Close it: slurp-helper stop {c.cid}")
  if open_it:
    open_thing(url)
  while c.client.poll() is None:
    c.keep_tunnel()
    time.sleep(1)
  notify(f"Web view closed: {link['title']}", last_lines(os.path.join(c.dir, "client.log"), 1), urgent=True)

def shell(c):
  """In the terminal window: connect, then ssh, which asks for the password here"""
  link = c.link
  c.connect()
  c.state = "connected"
  c.save()
  print(f"slurp helper: logging in to the shell of {link['title']}: give the password of the link (in slurp's link window: Copy)",
        flush=True)
  argv = [need_tool("ssh", "install OpenSSH"), "-o", "RequestTTY=yes", "-p", str(c.port), *c.ssh_options(prompts=3), f"{link['user']}@{c.host}"]
  c.client = subprocess.Popen(argv)
  while c.client.poll() is None:
    c.keep_tunnel()
    try:
      c.client.wait(1)
    except subprocess.TimeoutExpired:
      pass
  return c.client.returncode

def open_thing(target):
  if BATCH:
    return
  for argv in (["xdg-open", target], ["gio", "open", target]):
    if shutil.which(argv[0]):
      subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
      return

TERMINALS = [("ptyxis", ["--"]), ("kgx", ["--"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
             ("mate-terminal", ["-x"]), ("tilix", ["-e"]), ("kitty", []), ("alacritty", ["-e"]), ("wezterm", ["start", "--"]),
             ("foot", []), ("xterm", ["-e"]), ("x-terminal-emulator", ["-e"])]

def terminal_argv(argv):
  """A terminal window that runs argv: $SLURP_HELPER_TERMINAL (a command, argv is appended), $TERMINAL, the usual ones"""
  own = os.environ.get("SLURP_HELPER_TERMINAL")
  if own:
    return shlex.split(own) + argv
  term = os.environ.get("TERMINAL")
  if term and shutil.which(term):
    flag = next((f for name, f in TERMINALS if name == os.path.basename(term)), ["-e"])
    return [term, *flag, *argv]
  for name, flag in TERMINALS:
    if shutil.which(name):
      return [name, *flag, *argv]
  return None


# ---- the commands
def connections():
  """The open connections: (cid, info) of the ones whose process runs"""
  out = []
  try:
    names = sorted(os.listdir(CONNS))
  except OSError:
    return out
  for cid in names:
    try:
      with open(os.path.join(CONNS, cid, "state.json")) as f:
        info = json.load(f)
      os.kill(int(info["pid"]), 0)
    except (OSError, ValueError, KeyError):
      shutil.rmtree(os.path.join(CONNS, cid), ignore_errors=True) # (its process ended: left behind)
      continue
    out.append((cid, info))
  return out

def describe(info):
  what = info.get("mount") or info.get("url") or ""
  return f"{KIND_WORD.get(info['kind'], info['kind'])}: {info['title']} ({info['state']}{', ' + what if what else ''})"

def stop(which):
  stopped = 0
  for cid, info in connections():
    if which in ("all", cid, info.get("id")):
      try:
        os.kill(int(info["pid"]), signal.SIGTERM)
        stopped += 1
      except OSError:
        pass
  return stopped

def cmd_list():
  conns = connections()
  if not conns:
    print("slurp helper: no open connections")
  for cid, info in conns:
    print(f"{cid}  {describe(info)}")
  return 0

def cmd_window():
  """No link: the open connections in a list, to open or stop one"""
  conns = connections()
  g = gui()
  if g is None:
    return cmd_list()
  if not conns:
    if g == "zenity":
      ask_dialog(["zenity", "--info", "--title", "slurp helper", "--no-markup", "--text",
                  f"slurp helper {VERSION}: no open connections.\n\nIn slurp, a link window: Open on my computer."])
    else:
      ask_dialog(["kdialog", "--title", "slurp helper", "--msgbox", f"slurp helper {VERSION}: no open connections."])
    return 0
  if g == "zenity":
    argv = ["zenity", "--list", "--title", f"slurp helper {VERSION}", "--width", "720", "--height", "320",
            "--text", "Open connections: pick one", "--column", "ID", "--column", "Connection", "--print-column", "1"]
    for cid, info in conns:
      argv += [cid, describe(info)]
  else:
    argv = ["kdialog", "--title", f"slurp helper {VERSION}", "--menu", "Open connections: pick one"]
    for cid, info in conns:
      argv += [cid, describe(info)]
  cid = ask_dialog(argv)
  info = dict(conns).get(cid or "")
  if info is None:
    return 0
  target = info.get("mount") or info.get("url")
  if target and ask_yes(describe(info), "Open it? (No: more choices)"):
    open_thing(target)
  elif ask_yes(describe(info), "End this connection?" + (" (Unmounts the drive.)" if info.get("mount") else "")):
    stop(cid)
  return 0

def cmd_uninstall():
  stop("all")
  apps = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "applications")
  try:
    os.unlink(os.path.join(apps, "slurp-helper.desktop"))
  except OSError:
    pass
  shutil.rmtree(DATA, ignore_errors=True)
  shutil.rmtree(STATE, ignore_errors=True)
  print("slurp helper: removed (slurp:// links open nothing now)")
  return 0

def cmd_open(url, password_stdin=False, folder=None, yes=False):
  if not sys.stdin.isatty(): # (started by the browser: not ended with it. From a terminal: in it, Ctrl-C ends it)
    try:
      os.setsid()
    except OSError:
      pass
  try:
    link = parse_link(url)
  except Failed as e:
    error("Not a link the helper opens", str(e))
    return 2
  log(f"open {link['kind']} {link['id']} of {link['alias']} (slurp {link['slurp_version'] or '?'}, helper {VERSION})")
  try:
    if link["expires"] and link["expires"] < time.time():
      raise Failed("the link has expired: make a new one in slurp")
    trust(link, yes)
    if link["kind"] == "shell" and not password_stdin:
      if tty() or BATCH: # (already in a terminal: right here)
        return run_shell(link)
      argv = terminal_argv([sys.executable, os.path.abspath(__file__), "_shell", url])
      if argv is None:
        raise Failed("no terminal program found: set SLURP_HELPER_TERMINAL (e.g. 'gnome-terminal --')")
      subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
      notify(f"Shell: {link['title']}", "a terminal opens: give the password of the link there")
      return 0
    if link["kind"] == "drive":
      folder = resolve_folder(link, folder) if folder else ask_folder(link)
      if folder is None:
        return 1
    password = sys.stdin.readline().rstrip("\n") if password_stdin else ask_password(link)
    if not password:
      return 1
  except Failed as e:
    error(f"{KIND_WORD[link['kind']]}: {link['title']}", str(e))
    return 1
  return run(link, lambda c: drive(c, password, folder, True) if link["kind"] == "drive" else web(c, password, True))

def run(link, body):
  c = Connection(link)
  def ended(*_):
    raise SystemExit(0)
  signal.signal(signal.SIGTERM, ended)
  signal.signal(signal.SIGHUP, ended)
  try:
    body(c)
    return 0
  except Failed as e:
    error(f"{KIND_WORD[link['kind']]}: {link['title']}", str(e))
    return 1
  except KeyboardInterrupt:
    return 0
  finally:
    unmount(c.mount)
    c.close()

def run_shell(link):
  code = run(link, shell)
  if code and sys.stdin.isatty() and not BATCH:
    input("slurp helper: the shell ended. Enter closes this window ...")
  return code

def main(argv):
  global BATCH
  if os.environ.get("SLURP_HELPER_ASKPASS") == "1": # (ssh asks for the password)
    print(os.environ.get("SLURP_HELPER_PASSWORD", ""))
    return 0
  args = list(argv)
  BATCH = "--batch" in args
  flags = {a for a in args if a.startswith("--")}
  folder = None
  if "--folder" in args:
    i = args.index("--folder")
    folder = args[i + 1] if i + 1 < len(args) else ""
    args[i:i + 2] = []
  args = [a for a in args if not a.startswith("--")]
  unknown = flags - {"--batch", "--password-stdin", "--yes", "--folder", "--version", "--help"}
  if unknown or "--help" in flags:
    print(__doc__)
    return 2 if unknown else 0
  if "--version" in flags:
    print(f"slurp helper {VERSION}")
    return 0
  if not args:
    return cmd_window()
  cmd = args[0]
  if cmd.startswith("slurp:"):
    args, cmd = ["open", *args], "open"
  if cmd == "open" and len(args) == 2:
    return cmd_open(args[1], "--password-stdin" in flags, folder, "--yes" in flags)
  if cmd == "_shell" and len(args) == 2:
    try:
      link = parse_link(args[1])
    except Failed as e:
      print(f"slurp helper: {e}")
      return 2
    return run_shell(link)
  if cmd == "list":
    return cmd_list()
  if cmd == "stop" and len(args) == 2:
    n = stop(args[1])
    print(f"slurp helper: {n} connection{'s' if n != 1 else ''} ended")
    return 0 if n else 1
  if cmd == "uninstall":
    return cmd_uninstall()
  print(__doc__)
  return 2

if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
