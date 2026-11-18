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
EXIT = {0: "", 1: "syntax or usage error", 2: "error not otherwise categorised", 3: "directory not found",
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
  """(True, "") if S3 endpoints can be used here; else (False, why)"""
  if not installed():
    return False, "needs rclone on this machine (rclone.org/install)"
  return S.available()

# ---- remotes
def remote_name(eid):
  """A remote's name for an endpoint (letters and digits: an environment variable's name)"""
  return "E" + re.sub(r"[^A-Za-z0-9]", "", eid).upper()

def remote_env(name, settings, keys):
  """The environment that makes the remote <name>: an S3 endpoint (settings: provider, endpoint, region; keys:
  access_key, secret_key, session_token)"""
  p = f"RCLONE_CONFIG_{name}_"
  env = {p + "TYPE": "s3", p + "PROVIDER": settings.get("provider") or ("AWS" if not settings.get("endpoint") else "Other"),
         p + "ENV_AUTH": "false", p + "NO_CHECK_BUCKET": "true"} # (no_check_bucket: copying needs no right to create it)
  if settings.get("endpoint"):
    env[p + "ENDPOINT"] = settings["endpoint"]
  if settings.get("region"):
    env[p + "REGION"] = settings["region"]
  for key, option in (("access_key", "ACCESS_KEY_ID"), ("secret_key", "SECRET_ACCESS_KEY"), ("session_token", "SESSION_TOKEN")):
    if keys.get(key):
      env[p + option] = keys[key]
  return env

def keys_of(definition):
  """The keys of an S3 endpoint from the keyring (RcloneError if one it needs is missing)"""
  out = {name: S.get(S.user(definition.id, name)) for name in definition.secrets}
  missing = [n for n in ("access_key", "secret_key") if n in definition.secrets and not out.get(n)]
  if missing:
    raise RcloneError(f"the keyring has no {' and no '.join(m.replace('_', ' ') for m in missing)} of {definition.name}: Edit… the endpoint")
  return {k: v for k, v in out.items() if v}

def placeholders(definition):
  """The keys as placeholders, for Copy as a command"""
  return {"access_key": "$AWS_ACCESS_KEY_ID", "secret_key": "$AWS_SECRET_ACCESS_KEY",
          **({"session_token": "$AWS_SESSION_TOKEN"} if "session_token" in definition.secrets else {})}

def path(name, settings, p):
  """The rclone path of a path of the endpoint ("/" is the bucket's root)"""
  key = posixpath.normpath("/" + (p or "/").lstrip("/")).lstrip("/")
  return f"{name}:{settings.get('bucket', '')}" + (f"/{key}" if key else "")

class Endpoint:
  """An S3 endpoint for rclone: its remote's environment and paths (keys: from the keyring, or placeholders)"""
  def __init__(self, definition, keys=None):
    self.definition, self.settings = definition, definition.settings
    self.name = remote_name(definition.id)
    self.env = remote_env(self.name, self.settings, keys if keys is not None else keys_of(definition))

  def path(self, p):
    return path(self.name, self.settings, p)

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

def kind(env, p):
  """"dir", "file" or None (not there). A key prefix is a folder; the bucket's root is one"""
  folder, name = posixpath.split(p.rstrip("/"))
  if not name or p.rstrip("/").endswith(":") or p.rstrip("/").count("/") == 0:
    return "dir"
  try:
    entries = lsjson(env, folder)
  except RcloneError as e:
    if "not found" in str(e).lower() or "directory not found" in str(e).lower():
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
  """An error of an rclone JSON log line (None: none): "path: what" """
  try:
    data = json.loads(line)
  except ValueError:
    return line.strip() or None
  if not isinstance(data, dict) or data.get("level") not in ("error", "critical"):
    return None
  obj, msg = data.get("object") or "", data.get("msg") or ""
  return f"{obj}: {msg}" if obj else msg

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
