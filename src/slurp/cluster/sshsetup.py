"""SSH destinations of tabs: a Host of ~/.ssh/config (the main way: its user, jump hosts and keys are there), or the
full settings of a tab (user, host, port, jump hosts, identity files, more options) for the ssh that logs in; and
the check of a destination: what OpenSSH makes of it, name, port, the server, a login that cannot ask anything (ssh -v
-o BatchMode=yes), with precise findings and the verbose log. Runs on the machine slurp runs on."""

import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import time
from dataclasses import dataclass, field

SETTINGS = ("name", "user", "host", "port", "jump", "identities", "options")


def clean(settings):
  """The full settings of a tab as kept in the config (empty ones left out); None if there is no host"""
  if not settings or not str(settings.get("host") or "").strip():
    return None
  out = {}
  for key in SETTINGS:
    value = settings.get(key)
    if isinstance(value, str):
      value = value.strip()
    if key in ("identities", "options"):
      value = [v.strip() for v in (value or []) if str(v).strip()]
    if value:
      out[key] = value
  return out


def ssh_args(settings):
  """ssh's options for the full settings of a tab (before the destination)"""
  if not settings:
    return []
  args = []
  if settings.get("user"):
    args += ["-l", settings["user"]]
  if settings.get("port"):
    args += ["-p", str(settings["port"])]
  if settings.get("jump"):
    args += ["-J", settings["jump"]]
  for identity in settings.get("identities") or []:
    args += ["-i", os.path.expanduser(identity)]
  if settings.get("identities"):
    args += ["-o", "IdentitiesOnly=yes"] # (only these keys: the others could use up the server's attempts)
  for option in settings.get("options") or []:
    key, _, value = option.replace("=", " ", 1).partition(" ")
    if key and value.strip():
      args += ["-o", f"{key}={value.strip()}"]
  return args


def target(destination, settings):
  """What ssh connects to: the host of the full settings, else the destination"""
  return (settings or {}).get("host") or destination


def label(settings):
  """A tab's name for full settings: its name, else user@host(:port)"""
  if settings.get("name"):
    return settings["name"]
  text = (f"{settings['user']}@" if settings.get("user") else "") + settings["host"]
  return text + (f":{settings['port']}" if settings.get("port") and str(settings["port"]) != "22" else "")


def option_errors(settings):
  """What is wrong with full settings before ssh is asked: [text]"""
  errors = []
  if settings.get("port") and not re.fullmatch(r"\d{1,5}", str(settings["port"])) or \
     settings.get("port") and not 0 < int(settings["port"]) < 65536:
    errors.append(f"the port {settings['port']} is no port number (1 to 65535)")
  for option in settings.get("options") or []:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*\s*(=|\s)\s*\S.*", option):
      errors.append(f"'{option}' is no option: write Key=Value (e.g. ForwardAgent=yes)")
  for name in ("host", "user", "jump"):
    if re.search(r"\s", str(settings.get(name) or "")):
      errors.append(f"the {name} has a space: '{settings[name]}'")
  return errors


def config_hosts(path=None):
  """The Host entries of ~/.ssh/config (and the files it includes) without wildcards: what to suggest"""
  hosts, seen = [], set()
  def read(p, depth=0):
    if depth > 4 or p in seen:
      return
    seen.add(p)
    try:
      lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
      return
    for line in lines:
      words = line.strip().split()
      if not words or words[0].startswith("#"):
        continue
      key = words[0].lower()
      if key == "host":
        hosts.extend(w for w in words[1:] if not any(c in w for c in "*?!") and w not in hosts)
      elif key == "include":
        import glob
        for pattern in words[1:]:
          pattern = os.path.expanduser(pattern)
          if not os.path.isabs(pattern):
            pattern = os.path.join(os.path.expanduser("~/.ssh"), pattern)
          for f in sorted(glob.glob(pattern)):
            read(f, depth + 1)
  read(path or os.path.expanduser("~/.ssh/config"))
  return hosts


def config_blocks(destination, path=None):
  """The blocks of ~/.ssh/config (and the files it includes) that apply to a destination: [(file, line, text)], in
  order (a Host whose patterns match it, also Host * ; a Match block is shown as it is, not evaluated)"""
  import fnmatch
  import glob
  host = destination.rsplit("@", 1)[-1]
  blocks, seen = [], set()
  def matches(patterns):
    hit = False
    for p in patterns:
      if p.startswith("!"):
        if fnmatch.fnmatch(host, p[1:]):
          return False
      elif fnmatch.fnmatch(host, p):
        hit = True
    return hit
  def read(p, depth=0):
    if depth > 4 or p in seen:
      return
    seen.add(p)
    try:
      lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
      return
    current = None # (line number, lines) of a block that applies
    def close():
      if current:
        blocks.append((p, current[0], "\n".join(current[1]).rstrip()))
    for number, line in enumerate(lines, 1):
      words = line.strip().split()
      key = words[0].lower() if words else ""
      if key in ("host", "match"):
        close()
        current = (number, [line]) if key == "match" or matches(words[1:]) else None
      elif key == "include" and current is None:
        for pattern in words[1:]:
          pattern = os.path.expanduser(pattern)
          if not os.path.isabs(pattern):
            pattern = os.path.join(os.path.expanduser("~/.ssh"), pattern)
          for f in sorted(glob.glob(pattern)):
            read(f, depth + 1)
      elif current is not None:
        current[1].append(line)
    close()
  read(path or os.path.expanduser("~/.ssh/config"))
  return blocks


# ---------------------------------------------------------------------------
# the check

@dataclass
class Finding:
  level: str      # "ok", "info", "warn" (works, but …), "error" (does not work)
  text: str
  detail: str = ""


@dataclass
class CheckResult:
  verdict: str = "error"        # ok: logs in without asking; warn: the login asks (or is unclear); error: it cannot
  summary: str = ""
  findings: list = field(default_factory=list)
  config: dict = field(default_factory=dict)
  log: str = ""                 # the verbose output of ssh
  seconds: float = 0.0

  def add(self, level, text, detail=""):
    self.findings.append(Finding(level, text, detail))


def effective_config(destination, settings, ssh="ssh"):
  """ssh -G: what OpenSSH makes of the destination with the settings (no connection). {key: [values]}"""
  try:
    res = subprocess.run([ssh, "-G", *ssh_args(settings), target(destination, settings)], capture_output=True, text=True, timeout=10)
  except (OSError, subprocess.TimeoutExpired) as e:
    return None, str(e)
  if res.returncode:
    return None, (res.stderr or res.stdout).strip()
  conf = {}
  for line in res.stdout.splitlines():
    key, _, value = line.partition(" ")
    conf.setdefault(key.lower(), []).append(value)
  conf["_warnings"] = [l.strip() for l in (res.stderr or "").splitlines() if l.startswith("Warning") or "line" in l]
  return conf, ""


def key_state(path):
  """(exists, mode, encrypted) of a private key; encrypted None if not known"""
  try:
    st = os.stat(path)
  except OSError:
    return False, None, None
  encrypted = None
  try:
    with open(path, "rb") as f:
      head = f.read(4096)
    if b"ENCRYPTED" in head:
      encrypted = True
    elif b"BEGIN OPENSSH PRIVATE KEY" in head:
      import base64
      body = b"".join(head.split(b"-----")[2].split())
      raw = base64.b64decode(body + b"=" * (-len(body) % 4))
      encrypted = b"bcrypt" in raw[:64] or b"aes" in raw[:64]
    elif b"PRIVATE KEY" in head:
      encrypted = False
  except (OSError, ValueError, IndexError):
    pass
  return True, stat.S_IMODE(st.st_mode), encrypted


def resolve(host, timeout=5):
  """The addresses of a host name ([] and the error if it has none)"""
  result = {}
  def work():
    try:
      result["addrs"] = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
    except OSError as e:
      result["error"] = str(e)
  import threading
  t = threading.Thread(target=work, daemon=True)
  t.start()
  t.join(timeout)
  if "addrs" in result:
    return result["addrs"], ""
  return [], result.get("error") or f"no answer within {timeout} s"


def tcp_banner(host, port, timeout=6):
  """Connect to host:port and read the server's greeting: (banner, error)"""
  try:
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
      s.settimeout(timeout)
      data = b""
      while b"\n" not in data and len(data) < 256:
        chunk = s.recv(256)
        if not chunk:
          break
        data += chunk
      return data.decode(errors="replace").strip(), ""
  except OSError as e:
    return "", str(e)


# what ssh -v says, and what it means: (pattern, level, text); {0}: the first group
SSH_MESSAGES = [
  (r"Could not resolve hostname (\S+?):", "error", "the host name {0} is not known (a typo? the VPN or the network not there?)"),
  (r"connect to host (\S+) port (\d+): Connection refused", "error", "{0} refuses connections on port {1}: no SSH server there (the wrong port or host?)"),
  (r"connect to host (\S+) port (\d+): (?:Connection|Operation) timed out", "error",
   "{0} does not answer on port {1}: a firewall, the VPN or the network (or the wrong host)"),
  (r"connect to host (\S+) port (\d+): No route to host", "error", "no route to {0}: the network (the VPN?)"),
  (r"connect to host (\S+) port (\d+): Network is unreachable", "error", "the network to {0} is unreachable"),
  (r"REMOTE HOST IDENTIFICATION HAS CHANGED", "error",
   "the host key of the server has changed: it was reinstalled, or someone is in between. Ask the site; then remove the old "
   "key (ssh-keygen -R <host>)"),
  (r"Offending \S+ key in (\S+):(\d+)", "info", "the old key is in {0}, line {1}"),
  (r"Host key verification failed(?![\s\S]*--- again)", "error", "the server's host key could not be verified"),
  (r"(?:UNPROTECTED PRIVATE KEY FILE|bad permissions)", "error", "a private key can be read by others: ssh does not use it (chmod 600 the key)"),
  (r"(?:no such identity|Identity file) (\S+?):? (?:No such file or directory|not accessible)", "error", "the identity file {0} does not exist"),
  (r"Too many authentication failures", "error", "too many keys were tried: give the key to use (IdentityFile, and IdentitiesOnly=yes)"),
  (r"Bad owner or permissions on (\S+)", "error", "{0} has the wrong owner or permissions: ssh does not read it (chmod 600)"),
  (r"(\S+): line (\d+): Bad configuration option: (\S+)", "error", "{0}, line {1}: unknown option {2}"),
  (r"command-line line \d+: Bad configuration option: (\S+)", "error", "unknown option {0}"),
  (r"kex_exchange_identification: (?:Connection closed by remote host|read: Connection reset by peer)", "error",
   "the server (or a jump host) closed the connection at once: too many logins, blocked, or not allowed from here"),
  (r"channel \d+: open failed: administratively prohibited", "error", "a jump host does not allow forwarding to the next host"),
  (r"stdio forwarding failed", "error", "the jump host could not reach the next host"),
  (r"Connection closed by (\S+) port (\d+)", "info", "the connection was closed by {0}"),
]


def parse_ssh_log(log, result):
  """The findings in the verbose output of ssh (in order, once each); the authentication: methods and keys"""
  seen = set()
  for pattern, level, text in SSH_MESSAGES:
    m = re.search(pattern, log)
    if m and text not in seen:
      seen.add(text)
      result.add(level, text.format(*m.groups()))
  auth = re.findall(r"Authentications that can continue: (\S+)", log)
  offered = list(dict.fromkeys(re.findall(r"(?:Offering public key|Will attempt key): (\S+)", log)))
  accepted = re.findall(r"Server accepts key: (\S+)", log)
  return (auth[-1].split(",") if auth else []), offered, accepted


def check(destination, settings=None, ssh="ssh", timeout=30, pre_login=""):
  """Check a destination (with full settings) without logging in interactively: a CheckResult. pre_login: the
  command the tab runs before its login (not run here: it may ask, e.g. for a one-time code)"""
  start = time.monotonic()
  result = CheckResult()
  if pre_login:
    result.add("info", f"the command before the login ({pre_login}) is not run by the check: if it signs a key, a key "
               "not signed yet (or expired) shows as not accepted here, and the login runs it first")
  settings = clean(settings)
  if not shutil.which(ssh):
    result.add("error", "there is no ssh on this machine (install OpenSSH)")
    result.summary = "no ssh"
    return result
  errors = option_errors(settings or {})
  for e in errors:
    result.add("error", e)
  if errors:
    result.summary = "the settings are not valid"
    return result
  dest = target(destination, settings)
  if not dest:
    result.add("error", "no destination: a Host of ~/.ssh/config, user@host, or the host of the full settings")
    result.summary = "no destination"
    return result
  # 1. what OpenSSH makes of it
  conf, err = effective_config(destination, settings, ssh)
  if conf is None:
    result.add("error", f"ssh does not take these settings: {err}")
    result.summary = "the settings are not valid"
    return result
  result.config = conf
  one = lambda key: (conf.get(key) or [""])[0]
  host, port, user = one("hostname"), one("port") or "22", one("user")
  jump, proxy = one("proxyjump"), one("proxycommand")
  jump = "" if jump == "none" else jump
  proxy = "" if proxy == "none" else proxy
  result.add("info", f"ssh connects to {user}@{host} port {port}" + (f" through {jump}" if jump else "")
             + (f" through the command {proxy}" if proxy else ""))
  if not settings and not re.search(r"[@.]", dest) and dest not in config_hosts():
    result.add("warn", f"{dest} is no Host of ~/.ssh/config: it is taken as a host name")
  # the keys: of the settings (ssh -G leaves out the ones it cannot read, with a warning), then what ssh uses
  for given in (settings or {}).get("identities") or []:
    if not os.path.exists(os.path.expanduser(given)):
      result.add("error", f"the identity file {os.path.expanduser(given)} does not exist")
  for warning in conf.pop("_warnings", []):
    m = re.search(r"Identity file (\S+) not accessible", warning)
    if m and not any(m.group(1) in f.text for f in result.findings):
      result.add("error", f"the identity file {m.group(1)} cannot be read (in ~/.ssh/config?)")
    elif not m:
      result.add("warn", warning)
  identities = [os.path.expanduser(p) for p in conf.get("identityfile", [])]
  for key in identities:
    exists, mode, encrypted = key_state(key)
    if not exists:
      continue
    if mode is not None and mode & 0o077:
      result.add("error", f"{key} can be read by others ({oct(mode)}): ssh does not use it (chmod 600 {shlex.quote(key)})")
    elif encrypted:
      result.add("info", f"key {key}: has a passphrase (the login asks for it, or ssh-agent has it)")
    else:
      result.add("info", f"key {key}")
  # 2. the name, the port, the server (directly; through a jump host ssh does it)
  if not jump and not proxy:
    addrs, err = resolve(host)
    if not addrs:
      result.add("error", f"the host name {host} is not known: {err} (a typo? the VPN or the network not there?)")
      result.summary = f"{host} is not known"
      result.seconds = time.monotonic() - start
      return result
    result.add("ok", f"{host} is {', '.join(addrs[:3])}" + (" …" if len(addrs) > 3 else ""))
    banner, err = tcp_banner(host, port)
    if not banner:
      what = ("refuses connections on port {}: no SSH server there (the wrong port?)" if "refused" in err.lower() else
              "does not answer on port {}: a firewall, the VPN or the network" if "timed out" in err.lower() else
              "cannot be reached on port {}: " + err)
      result.add("error", f"{host} " + what.format(port))
      result.summary = f"{host}:{port} cannot be reached"
      result.seconds = time.monotonic() - start
      return result
    result.add("ok" if banner.startswith("SSH-") else "warn",
               f"port {port} answers: {banner[:80]}" if banner.startswith("SSH-") else f"port {port} answers, but not as an SSH server: {banner[:80]}")
  # 3. a login that cannot ask (no password, passphrase or host key question: nothing hangs)
  base = [ssh, "-v", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(timeout, 15)}", "-o", "ControlMaster=no",
          "-o", "ControlPath=none", "-o", "NumberOfPasswordPrompts=0"]
  def attempt(extra=()):
    # (ssh takes the first value of an option: these before the tab's settings, which may set the same)
    argv = [*base, *extra, *ssh_args(settings), dest, "exit 0"]
    try:
      res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
      return (res.stderr or "") + (res.stdout or ""), res.returncode
    except subprocess.TimeoutExpired as e:
      result.add("error", f"no login within {timeout} s (a jump host or the server does not answer)")
      return (e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")), None
    except OSError as e:
      return str(e), None
  log, code = attempt()
  # an unknown host key: ssh says so with StrictHostKeyChecking=yes; with ask (the default) in batch mode only "Host
  # key verification failed" (a changed key says more, above)
  unknown = re.search(r"No \S+ host key is known for (\S+)", log)
  if unknown or ("Host key verification failed" in log and "HAS CHANGED" not in log):
    fingerprint = re.search(r"Server host key: (\S+ \S+)", log)
    who = unknown.group(1) if unknown else host
    result.add("warn", f"the host key of {who} is not known yet: the first login asks you to accept it"
               + (f" ({fingerprint.group(1)}: compare it with the one the site publishes)" if fingerprint else ""))
    first = log
    log, code = attempt(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "GlobalKnownHostsFile=/dev/null"])
    log = first + "\n--- again, the host key not checked (nothing kept) ---\n" + log
  result.log = log
  methods, offered, accepted = parse_ssh_log(log, result)
  if code == 0 or "Authenticated to" in log:
    result.verdict, result.summary = "ok", "the login works (without asking)"
    result.add("ok", "logged in without a password, passphrase or question (keys or the agent)")
  elif any(f.level == "error" for f in result.findings):
    result.verdict = "error"
    result.summary = next(f.text for f in result.findings if f.level == "error")
  elif "Permission denied" in log or methods:
    asks = [m for m in methods if m in ("password", "keyboard-interactive")]
    if accepted:
      result.verdict, result.summary = "warn", "the server accepts your key; it needs its passphrase: the login asks for it"
      result.add("warn", f"the server accepts {accepted[-1]}, which needs its passphrase (the login pane asks; or add it to ssh-agent)")
    elif asks:
      result.verdict = "warn"
      result.summary = "the server asks for a password or a code: the login asks you"
      result.add("warn", f"the server asks for {' or '.join(asks)} (a password, or a one-time code): slurp's login asks you")
      if offered:
        result.add("info", f"keys tried and not accepted: {', '.join(offered)}")
    else:
      result.verdict = "error"
      result.summary = "the server does not accept your keys"
      result.add("error", f"the server accepts only {', '.join(methods) or 'keys'}, and none of yours"
                 + (f" ({', '.join(offered)})" if offered else " (ssh had no key to offer)")
                 + ": register your public key with the site, or give the right identity file")
  elif "No " in log and "host key is known" in log:
    result.verdict, result.summary = "warn", "the host key is not known yet: the first login asks you to accept it"
  else:
    result.verdict = "error"
    last = [l for l in log.splitlines() if l.strip() and not l.startswith("debug")]
    result.summary = last[-1] if last else f"ssh ended with {code}"
    result.add("error", f"ssh ended with {code}: {result.summary}")
  result.seconds = time.monotonic() - start
  return result


def check_text(result, log_lines=60):
  """A CheckResult in words, for the dialogs: the verdict, the findings, the end of the verbose log"""
  from rich.text import Text
  styles = {"ok": "green", "info": "", "warn": "yellow", "error": "bold red"}
  marks = {"ok": "✔", "info": "·", "warn": "!", "error": "✖"}
  verdict = {"ok": ("✔ works", "bold green"), "warn": ("! works, the login asks", "bold yellow"),
             "error": ("✖ does not work", "bold red")}[result.verdict]
  out = Text()
  out.append(f"{verdict[0]}: {result.summary}", style=verdict[1])
  out.append(f"  ({result.seconds:.1f} s)\n", style="dim")
  for f in result.findings:
    out.append(f"{marks[f.level]} {f.text}\n", style=styles[f.level])
  if result.log:
    lines = [l for l in result.log.splitlines() if l.strip()]
    out.append(f"\nssh -v (the last {min(log_lines, len(lines))} of {len(lines)} lines):\n", style="bold")
    out.append("\n".join(lines[-log_lines:]) + "\n", style="dim")
  return out
