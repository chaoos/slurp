"""Remote web access through Cloudflare: a quick tunnel (no account, nothing to install on the user's side).

cloudflared (Cloudflare's tunnel client, one static binary) runs here and connects out to Cloudflare; Cloudflare
gives it a random public HTTPS address (https://<words>.trycloudflare.com) and passes what comes in there to the web
view on 127.0.0.1. The browser needs nothing but that address with the web view's token.

slurp uses cloudflared from the config ("cloudflared": a path), else from PATH, else the copy it downloaded before
into its data directory (~/.local/share/slurp/bin); otherwise it downloads it there (from Cloudflare's releases on
GitHub, about 40 MB) when the first such link is made."""

import os
import platform
import re
import shutil
import subprocess
import urllib.request

from .config import load_config

RELAY = "cloudflare"
DOMAIN = "trycloudflare.com"
RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
ADDRESS = re.compile(r"https://([a-z0-9][a-z0-9-]*\.trycloudflare\.com)\b")
CONNECTED = "Registered tunnel connection" # (cloudflared's log line once Cloudflare passes requests to it)

def data_dir():
  return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "slurp", "bin")

def asset():
  """The name of the release file for this machine (None: no build of cloudflared that slurp can use here)"""
  if not platform.system() == "Linux":
    return None
  machine = platform.machine().lower()
  return {"x86_64": "cloudflared-linux-amd64", "amd64": "cloudflared-linux-amd64", "aarch64": "cloudflared-linux-arm64",
          "arm64": "cloudflared-linux-arm64"}.get(machine)

def binary():
  """The cloudflared to use, or None (then download() gets one)"""
  configured = str(load_config().get("cloudflared") or "")
  if configured:
    path = os.path.expanduser(configured)
    return path if os.access(path, os.X_OK) else None
  found = shutil.which("cloudflared")
  if found:
    return found
  own = os.path.join(data_dir(), "cloudflared")
  return own if os.access(own, os.X_OK) else None

def available():
  """None if Cloudflare links can work here, else why not"""
  if binary() is not None or asset() is not None:
    return None
  configured = load_config().get("cloudflared")
  if configured:
    return f"the cloudflared of the config ({configured}) is not an executable file"
  return f"no cloudflared for {platform.system()} {platform.machine()} to download: put it on PATH or set \"cloudflared\" in the config"

def version(path):
  try:
    out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout
  except (OSError, subprocess.SubprocessError):
    return ""
  m = re.search(r"version (\S+)", out)
  return m[1] if m else ""

def download(progress=lambda done, total: None):
  """Downloads cloudflared into the data directory (blocking: run it in a thread); returns its path. It is kept only
  if it runs (cloudflared --version)."""
  name = asset()
  if name is None:
    raise RuntimeError(available() or "no cloudflared to download here")
  os.makedirs(data_dir(), exist_ok=True)
  path = os.path.join(data_dir(), "cloudflared")
  part = path + ".part"
  request = urllib.request.Request(RELEASES + name, headers={"User-Agent": "slurp"})
  try:
    with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
      total, done = int(response.headers.get("Content-Length") or 0), 0
      while True:
        chunk = response.read(1 << 20)
        if not chunk:
          break
        out.write(chunk)
        done += len(chunk)
        progress(done, total)
    os.chmod(part, 0o755)
    if not version(part):
      raise RuntimeError("the downloaded cloudflared does not run here")
    os.replace(part, path)
  finally:
    if os.path.exists(part):
      os.unlink(part)
  return path

def resolves(host):
  """The name is in public DNS (asked over HTTPS at Cloudflare's resolver: this machine's own resolver would keep a
  "no such name" from asking too early). Blocking. None if that cannot be asked."""
  import json
  request = urllib.request.Request(f"https://cloudflare-dns.com/dns-query?name={host}&type=A",
                                   headers={"Accept": "application/dns-json", "User-Agent": "slurp"})
  try:
    with urllib.request.urlopen(request, timeout=10) as response:
      answer = json.load(response)
  except (OSError, ValueError):
    return None
  return answer.get("Status") == 0 and any(a.get("type") == 1 for a in answer.get("Answer") or [])

CONFIG = "# slurp's quick tunnel (cloudflared tunnel --url): only this, not a config.yml of a named tunnel\nno-autoupdate: true\n"

def argv(path, port):
  """The quick tunnel to the web view on 127.0.0.1:port. A config of its own: a config.yml of the user (of a named
  tunnel) would change what it does. HTTP/2 over TCP: QUIC (UDP) is often blocked (on Alps clusters too), and
  cloudflared then waits minutes before it falls back."""
  config = os.path.join(data_dir(), "cloudflared-quick.yml")
  try:
    current = open(config).read()
  except OSError:
    current = None
  if current != CONFIG:
    os.makedirs(data_dir(), exist_ok=True)
    with open(config, "w") as f:
      f.write(CONFIG)
  return [path, "tunnel", "--config", config, "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{port}"]
