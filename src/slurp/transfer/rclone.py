"""rclone: S3 (and S3-compatible) endpoints, and the copies to and from them.

- Remotes are made of environment variables (RCLONE_CONFIG_<NAME>_<OPTION>), with --config "": no rclone.conf is read
  or written, and the keys are never on a command line (only in the environment of rclone, or, for a run on a
  cluster the user agreed to, in a file of the run's folder that the run reads and deletes at once).
- Paths of an S3 endpoint are keys in its bucket, written as paths: "/" is the bucket's root ("/runs/2026" is the
  prefix runs/2026/).
- A copy is `rclone copy` (never sync: nothing is deleted on the destination): what is there already (same size and
  time, or with --checksum the same hash) is skipped, so a run again copies only the rest; a file half copied starts
  over. Sources of one folder are one rclone run (filters: each folder, file or pattern), its statistics (JSON log
  lines) are the progress.
- rclone is optional: where it is not installed (here, or on a cluster) the routes that need it say so."""

import json
import os
import posixpath
import re
import secrets as random
import shlex
import shutil
import subprocess

from . import secrets as S

SFTP_SSH = (1, 64) # (rclone --sftp-ssh: through a tab's own SSH session; not used yet)
PROVIDERS = ["AWS", "Ceph", "Cloudflare", "DigitalOcean", "GCS", "IBMCOS", "Minio", "Wasabi", "Other"]
EXIT = {0: "", 1: "it failed", # (rclone's docs: a syntax or usage error; a copy that failed ends so too)
         2: "error not otherwise categorised", 3: "directory not found",
        4: "file not found", 5: "temporary error (more retries might fix it)", 6: "less serious errors",
        7: "fatal error (more retries will not fix it: e.g. access denied, no such bucket)", 8: "transfer limit exceeded",
        9: "no files transferred"}

class RcloneError(Exception):
  """What rclone said, in a few words (never a key)"""

def installed():
  return shutil.which("rclone") is not None

def version_of(text):
  """(major, minor, patch) of `rclone version`'s first line ("rclone v1.60.1-DEV"); None if none"""
  m = re.search(r"rclone v(\d+)\.(\d+)(?:\.(\d+))?", text or "")
  return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None

def version():
  if not installed():
    return None
  try:
    return version_of(subprocess.run(["rclone", "version"], capture_output=True, text=True, timeout=20).stdout)
  except (OSError, subprocess.TimeoutExpired):
    return None

def available():
  """(True, "") if S3, SMB and WebDAV endpoints can be used here; else (False, why)"""
  if not installed():
    return False, "needs rclone on this machine (rclone.org/install)"
  return S.available()

# ---- remotes (S3, SMB, WebDAV)
KINDS = ("s3", "smb", "webdav")
REQUIRED = {"s3": ["access_key", "secret_key"], "smb": ["password"], "webdav": []} # (secrets a remote cannot do without)
SECRET_WORDS = {"access_key": "access key", "secret_key": "secret key", "session_token": "session token",
                "password": "password", "bearer_token": "token"}
PLACEHOLDERS = {"access_key": "$AWS_ACCESS_KEY_ID", "secret_key": "$AWS_SECRET_ACCESS_KEY", "session_token": "$AWS_SESSION_TOKEN",
                "password": "$PASSWORD", "bearer_token": "$TOKEN"}

def remote_name(eid):
  """A remote's name for an endpoint (letters and digits: an environment variable's name)"""
  return "E" + re.sub(r"[^A-Za-z0-9]", "", eid).upper()

def obscure(value):
  """A password as rclone takes it (obscured: rclone obscure, the value on its stdin, not on a command line)"""
  result = subprocess.run(["rclone", "obscure", "-"], input=value, capture_output=True, text=True, timeout=30)
  if result.returncode:
    raise RcloneError(f"rclone obscure failed: {said(result)}")
  return result.stdout.strip()

def remote_env(name, settings, keys, kind="s3", placeholder=False):
  """The environment that makes the remote <name> of an endpoint of a kind (keys: its secrets; placeholder: they are
  placeholders, for Copy as a command: a password is obscured by the command itself)"""
  p = f"RCLONE_CONFIG_{name}_"
  def secret(key, option, obscured=False):
    if keys.get(key):
      value = keys[key]
      env[p + option] = (f'$(printf %s "{value}" | rclone obscure -)' if placeholder else obscure(value)) if obscured else value
  if kind == "smb":
    env = {p + "TYPE": "smb", p + "HOST": settings.get("host", "")}
    for key, option in (("port", "PORT"), ("user", "USER"), ("domain", "DOMAIN")):
      if settings.get(key):
        env[p + option] = str(settings[key])
    secret("password", "PASS", obscured=True)
    return env
  if kind == "webdav":
    env = {p + "TYPE": "webdav", p + "URL": settings.get("url", ""), p + "VENDOR": settings.get("vendor") or "other"}
    if settings.get("user"):
      env[p + "USER"] = settings["user"]
    secret("password", "PASS", obscured=True)
    secret("bearer_token", "BEARER_TOKEN")
    return env
  env = {p + "TYPE": "s3", p + "PROVIDER": settings.get("provider") or ("AWS" if not settings.get("endpoint") else "Other"),
         p + "ENV_AUTH": "false", p + "NO_CHECK_BUCKET": "true"} # (no_check_bucket: copying needs no right to create it)
  if settings.get("endpoint"):
    env[p + "ENDPOINT"] = settings["endpoint"]
  if settings.get("region"):
    env[p + "REGION"] = settings["region"]
  for key, option in (("access_key", "ACCESS_KEY_ID"), ("secret_key", "SECRET_ACCESS_KEY"), ("session_token", "SESSION_TOKEN")):
    secret(key, option)
  return env

def keys_of(definition):
  """The secrets of an endpoint from the keyring (RcloneError if one it needs is missing)"""
  out = {name: S.get(S.user(definition.id, name)) for name in definition.secrets}
  missing = [n for n in REQUIRED.get(definition.kind, []) if not out.get(n)]
  if missing:
    raise RcloneError(f"the keyring has no {' and no '.join(SECRET_WORDS.get(m, m) for m in missing)} of {definition.name}: Edit… the endpoint")
  return {k: v for k, v in out.items() if v}

def placeholders(definition):
  """The secrets as placeholders, for Copy as a command"""
  return {name: PLACEHOLDERS.get(name, "$" + name.upper()) for name in definition.secrets}

def path(name, settings, p, kind="s3"):
  """The rclone path of a path of the endpoint: S3: in its bucket ("/" its root); SMB: in its share; WebDAV: under its URL"""
  key = posixpath.normpath("/" + (p or "/").lstrip("/")).lstrip("/")
  if kind == "webdav":
    return f"{name}:{key}"
  root = settings.get("share" if kind == "smb" else "bucket", "")
  return f"{name}:{root}" + (f"/{key}" if key else "")

def address(definition, p):
  """Where a path of an endpoint is, for people (to copy): s3://bucket/key, //server/share/path, the URL"""
  s, key = definition.settings, "/" + (p or "/").lstrip("/")
  if definition.kind == "smb":
    return f"//{s.get('host', '')}/{s.get('share', '')}{key}"
  if definition.kind == "webdav":
    return s.get("url", "").rstrip("/") + key
  return f"s3://{s.get('bucket', '')}{key}"

class Endpoint:
  """An endpoint for rclone (S3, SMB, WebDAV): its remote's environment and paths (keys: its secrets, from the keyring
  if None; placeholder: placeholders, for Copy as a command)"""
  def __init__(self, definition, keys=None, placeholder=False):
    self.definition, self.settings, self.kind = definition, definition.settings, definition.kind
    self.name = remote_name(definition.id)
    self.env = remote_env(self.name, self.settings, keys if keys is not None else keys_of(definition), self.kind, placeholder)

  def path(self, p):
    return path(self.name, self.settings, p, self.kind)

  def kind_of(self, p):
    """"dir", "file" or None of a path of the endpoint"""
    return kind(self.env, self.path(p), 0 if self.kind == "webdav" else 1)

# ---- a cluster tab, reached over slurp's SSH session (rclone on this machine: the tab's files as an sftp remote)
SFTP_SSH = (1, 64, 0) # (rclone's --sftp-ssh: an ssh command of ours for the sftp remote)

def ssh_value(argv):
  """An ssh command line as rclone's option takes it (words split at spaces; a word with a space or a quote: quoted)"""
  return " ".join('"' + a.replace('"', '""') + '"' if any(c in a for c in ' "') else a for a in argv)

class TabEnd:
  """A cluster tab as an rclone remote (sftp): rclone runs ssh on the tab's own session (its control socket: no new
  login), the sftp subsystem of the cluster's sshd. placeholder: plain `ssh <destination>`, for Copy as a command"""
  kind = "tab"

  def __init__(self, cluster, placeholder=False):
    self.cluster, self.name = cluster, remote_name("tab" + (cluster.destination or ""))
    c = cluster.connection
    if placeholder or c is None:
      ssh = ["ssh", cluster.destination]
    else: # (slurp's wrapper: ssh on the session's control socket, the output of login scripts dropped)
      import sys
      ssh = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sftp_ssh.py"), c.control_path, c.destination]
    p = f"RCLONE_CONFIG_{self.name}_"
    self.env = {p + "TYPE": "sftp", p + "SSH": ssh_value(ssh), p + "SHELL_TYPE": "unix"}

  def path(self, p):
    return f"{self.name}:{p or '/'}"

  def kind_of(self, p):
    """"dir", "file" or None of a path on the cluster"""
    p = posixpath.normpath(p or "/")
    if p == "/":
      return "dir"
    try:
      entries = lsjson(self.env, self.path(posixpath.dirname(p)))
    except RcloneError as e:
      if "not found" in str(e).lower() or "no such file" in str(e).lower():
        return None
      raise
    entry = next((e for e in entries if e["name"] == posixpath.basename(p)), None)
    return None if entry is None else "dir" if entry["dir"] else "file"

def relay_ready():
  """(True, "") if rclone here can reach a cluster over slurp's SSH session; else (False, why)"""
  have = version()
  if not have:
    return False, "rclone is not installed on this machine"
  if have < SFTP_SSH:
    return False, (f"rclone {'.'.join(map(str, SFTP_SSH[:2]))} or newer is needed on this machine to reach a cluster over "
                   f"slurp's SSH (here {'.'.join(map(str, have))}: rclone.org/install)")
  return True, ""

def run(args, env, timeout=120):
  """rclone args with the remotes of env (no config file) -> CompletedProcess"""
  if not installed():
    raise RcloneError("rclone is not installed on this machine")
  try:
    return subprocess.run(["rclone", "--config", "", *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **env}, stdin=subprocess.DEVNULL)
  except subprocess.TimeoutExpired:
    raise RcloneError(f"rclone gave no answer within {timeout} s") from None

def said(result):
  """What rclone said of an error, in a few words (its log lines without times and levels)"""
  lines = []
  for line in (result.stderr or "").splitlines():
    line = re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d (ERROR|NOTICE|INFO|CRITICAL|Failed to \w+:)?\s*:?\s*", "", line).strip()
    if line and not line.startswith(("Config file", "DEBUG")):
      lines.append(line)
  text = lines[-1] if lines else f"rclone ended with {result.returncode}"
  for words, short in (("AccessDenied", "access denied"), ("InvalidAccessKeyId", "the access key is not known there"),
                       ("SignatureDoesNotMatch", "the secret key does not match"), ("NoSuchBucket", "no such bucket"),
                       ("ExpiredToken", "the session token expired"), ("no such host", "the endpoint's host is not known"),
                       ("connection refused", "the endpoint refused the connection"),
                       ("directory not found", "not there (no such folder, or no such bucket)")):
    if words.lower() in text.lower():
      return f"{short} ({text[:160]})"
  return text[:240]

def check(result):
  if result.returncode:
    raise RcloneError(said(result))
  return result

def lsjson(env, p, max_depth=1):
  """A folder: [{name, dir, size, mtime}] (as cluster.listing gives)"""
  from datetime import datetime
  result = check(run(["lsjson", "--max-depth", str(max_depth), "--no-mimetype", p], env))
  out = []
  for e in json.loads(result.stdout or "[]"):
    mtime = 0.0
    try:
      mtime = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", e.get("ModTime", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
      pass
    out.append({"name": e["Name"], "dir": bool(e.get("IsDir")), "size": max(int(e.get("Size") or 0), 0), "mtime": mtime})
  return out

def kind(env, p, root_depth=1):
  """"dir", "file" or None (not there) of an rclone path ("NAME:rest"). A key prefix is a folder. root_depth: the
  parts of rest that are the endpoint's root (S3: its bucket, SMB: its share: 1; WebDAV: none, 0)"""
  remote, _, rest = p.partition(":")
  parts = [x for x in rest.split("/") if x]
  if len(parts) <= root_depth: # (the root of the endpoint)
    return "dir"
  folder, name = f"{remote}:" + "/".join(parts[:-1]), parts[-1]
  try:
    entries = lsjson(env, folder)
  except RcloneError as e:
    if "not found" in str(e).lower() or "not there" in str(e).lower():
      return None
    raise
  entry = next((e for e in entries if e["name"] == name), None)
  return None if entry is None else "dir" if entry["dir"] else "file"

def size(env, paths):
  """{"bytes", "files"} of rclone paths (folders: all below)"""
  total = {"bytes": 0, "files": 0}
  for p in paths:
    data = json.loads(check(run(["size", "--json", p], env, timeout=600)).stdout or "{}")
    total["bytes"] += int(data.get("bytes") or 0)
    total["files"] += int(data.get("count") or 0)
  return total

def test(env, p, write=False):
  """Can it be listed (and with write: written: a marker object written and deleted)? -> words; RcloneError why not"""
  entries = lsjson(env, p)
  words = f"lists {len(entries)} entr{'ies' if len(entries) != 1 else 'y'}"
  if write:
    marker = f"{p.rstrip('/')}/.slurp-test-{random.token_hex(4)}"
    result = subprocess.run(["rclone", "--config", "", "rcat", marker], input="slurp write test\n", capture_output=True,
                            text=True, timeout=120, env={**os.environ, **env})
    if result.returncode:
      raise RcloneError(f"it lists, but cannot be written: {said(result)}")
    gone = run(["deletefile", marker], env)
    words += ", can be written" + ("" if not gone.returncode else f" (the marker {marker.split(':', 1)[1]} could not be deleted: {said(gone)})")
  return words

def make_folder(e, p):
  """New folder… of an Endpoint: the folder p made and shown to be writable (a marker written and deleted in it: an
  S3 mkdir does nothing and says nothing, also without write rights) -> a note for people ("" none); RcloneError why
  not"""
  check(run(["mkdir", e.path(p)], e.env))
  try:
    words = test(e.env, e.path(p), write=True)
  except RcloneError as err:
    raise RcloneError(f"{p} cannot be written: {str(err).removeprefix('it lists, but cannot be written: ')}") from None
  note = "S3 keeps no empty folders: it is there once something is copied into it" if e.kind == "s3" else ""
  if "could not be deleted" in words:
    note = "; ".join(filter(None, [note, words.split("(", 1)[1].rstrip(")")]))
  return note

# ---- a copy
def groups(sources):
  """Sources by their folder: [(folder, [filter rule, …], [what, …])] (a folder: all below it; a file; a pattern:
  its matches in the folder)"""
  out = {}
  for src, is_dir in sources:
    folder, name = posixpath.split(src.rstrip("/"))
    rules = out.setdefault(folder or "/", ([], []))
    if any(ch in name for ch in "*?["):
      rules[0].append(f"+ /{name}")
    elif is_dir:
      rules[0].append(f"+ /{name}/**")
    else:
      rules[0].append(f"+ /{name}")
    rules[1].append(name)
  return [(folder, rules + ["- **"], names) for folder, (rules, names) in out.items()]

def options(o):
  """rclone's options of a transfer's options (checksum, transfers: files at a time, bwlimit in MB/s)"""
  out = ["--use-json-log", "--stats", "2s", "--stats-log-level", "NOTICE", "-v"]
  if o.get("checksum"):
    out.append("--checksum")
  if o.get("transfers"):
    out += ["--transfers", str(o["transfers"])]
  if o.get("bwlimit"):
    out += ["--bwlimit", f"{o['bwlimit']}M"]
  return out

def copy_argv(src_path, dst_path, rules, o):
  """One rclone copy of a folder's sources (rules: --filter of each) into the destination folder"""
  return ["copy", *options(o), *[a for r in rules for a in ("--filter", r)], src_path, dst_path]

def stats_of(line):
  """The statistics of an rclone JSON log line (None: another line): bytes, total, rate, eta, files, files_total,
  errors, current"""
  try:
    data = json.loads(line)
  except ValueError:
    return None
  s = data.get("stats") if isinstance(data, dict) else None
  if not isinstance(s, dict):
    return None
  current = (s.get("transferring") or [{}])[0].get("name", "") if s.get("transferring") else ""
  return {"bytes": int(s.get("bytes") or 0), "total": int(s.get("totalBytes") or 0), "rate": float(s.get("speed") or 0),
          "eta": s.get("eta"), "files": int(s.get("transfers") or 0), "files_total": int(s.get("totalTransfers") or 0),
          "errors": int(s.get("errors") or 0), "checks": int(s.get("checks") or 0), "current": current}

def error_of(line):
  """An error of an rclone log line, JSON or not (None: none): "path: what"; its summaries (Attempt 2/3 failed with
  …) none: they repeat the errors"""
  try:
    data = json.loads(line)
  except ValueError:
    data = None
  if not isinstance(data, dict):
    text = re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s*(<\d>)?\s*((ERROR|CRITICAL|NOTICE)\s*:\s*)?", "", line.strip())
    if not text or text.startswith("Attempt "):
      return None
    return re.sub(r"^Failed to \w+:\s*", "", text)
  if data.get("level") not in ("error", "critical"):
    return None
  obj, msg = data.get("object") or "", data.get("msg") or ""
  if msg.startswith("Attempt "):
    return None
  msg = re.sub(r"^Failed to \w+:\s*", "", msg)
  return f"{obj}: {msg}" if obj else msg

# (what some servers do that rclone cannot take, said for people)
HINTS = (("broken error response format", "the SMB server answers reads in a way rclone does not understand (impacket's "
          "test server does so; Samba and Windows do not)"),)

def causes(errors, n=2):
  """The last n different errors of a run, for its message (one that another holds: once), with a hint where one is
  known"""
  out = []
  for e in reversed(errors):
    if any(e in o or o in e for o in out):
      continue
    out.insert(0, e)
    if len(out) == n:
      break
  text = "; ".join(out)
  for words, hint in HINTS:
    if words in text:
      text += f" ({hint})"
  return text

def outcome(code, stopped=False):
  """(state, words) of an rclone run that ended with code (stopped on purpose: paused)"""
  if stopped and code != 0:
    return "paused", "paused"
  if code == 0:
    return "done", ""
  if code == 6:
    return "done-errors", "some files could not be copied"
  return "failed", f"rclone ended with {code}: {EXIT.get(code, '')}".rstrip(": ") if code is not None else "the run ended without a result"

# ---- on a cluster: the command of a run (through the runner: its progress lines as rsync's, the log its errors)
# (the keys: in the file `keys` of the run's folder, read and deleted at the start; the runner deletes it at its end
# in any case, and slurp when it sees the end)
PROGRESS_AWK = r"""
function num(key,   v) { if (match($0, "\"" key "\":[0-9.eE+-]+")) { v = substr($0, RSTART + length(key) + 3, RLENGTH - length(key) - 3); return v + 0 } return 0 }
function str(key,   v) { if (match($0, "\"" key "\":\"[^\"]*\"")) { v = substr($0, RSTART + length(key) + 4, RLENGTH - length(key) - 5); return v } return "" }
/"stats":/ {
  b = num("bytes"); t = num("totalBytes"); s = num("speed"); e = num("eta"); x = num("transfers"); tt = num("totalTransfers")
  if (b < last) base += last
  last = b
  pct = t > 0 ? int(100 * b / t) : 0
  h = int(e / 3600); m = int((e % 3600) / 60); sec = int(e % 60)
  name = ""
  if (match($0, /"transferring":\[\{[^}]*"name":"[^"]*"/)) { n = substr($0, RSTART, RLENGTH); sub(/.*"name":"/, "", n); sub(/"$/, "", n); name = n }
  if (name != "" && name != shown) { print name; shown = name }
  printf "%d %d%% %.2fMB/s %d:%02d:%02d (xfr#%d, to-chk=%d/%d)\n", base + b, pct, s / 1048576, h, m, sec, x, tt - x, tt
  fflush()
  next
}
/"level":"(error|critical)"/ { o = str("object"); g = str("msg"); print (o != "" ? o ": " : "") g > "/dev/stderr"; next }
"""

def cluster_command(transfer, local_side, s3, s3_paths, local_paths, dest):
  """The command of an rclone run on a cluster (the file `command`): local_side "source" (the cluster's files to S3)
  or "dest"; s3: the Endpoint (its remote's environment comes from the file `keys`)"""
  lines = ["#!/bin/sh", f"# the rclone command of slurp transfer {transfer.id}",
           'd=$(dirname "$0")', 'if [ -f "$d/keys" ]; then set -a; . "$d/keys"; set +a; rm -f "$d/keys"; fi',
           "command -v rclone >/dev/null 2>&1 || { echo 'slurp: rclone is not installed here' >&2; exit 127; }",
           # (Pause: TERM to this script, passed on to rclone; the pipeline runs in the background, so that the trap
           # runs at once, not after it)
           "stop=0; rc=0",
           """trap 'stop=1; kill -TERM "$(cat "$d/rclone.pid" 2>/dev/null)" 2>/dev/null' TERM INT HUP"""]
  sources = local_paths if local_side == "source" else s3_paths
  for folder, rules, _names in groups(sources):
    src = folder if local_side == "source" else s3.path(folder)
    dst = s3.path(dest) if local_side == "source" else dest
    argv = ["rclone", "--config", "", *copy_argv(src, dst, rules, transfer.options)]
    lines += ['if [ $stop = 0 ]; then',
              f"  ( {{ {shlex.join(argv)} 2>&1 >/dev/null & echo $! > \"$d/rclone.pid\"; wait $!; echo $? > \"$d/rclone.rc\"; }}"
              f" | awk '{PROGRESS_AWK}' ) &",
              "  p=$!; wait $p; while kill -0 $p 2>/dev/null; do wait $p; done",
              '  r=$(cat "$d/rclone.rc" 2>/dev/null || echo 1); [ "$r" -ne 0 ] && rc=$r',
              "fi"]
  lines += ['rm -f "$d/rclone.pid" "$d/rclone.rc"', "[ $stop = 1 ] && exit 143", "exit $rc"]
  return "\n".join(lines) + "\n"

def keys_file(env):
  """The file `keys` of a run on a cluster: the remote's environment (sh syntax)"""
  return "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items())
