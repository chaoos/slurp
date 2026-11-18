"""The check before a transfer (it changes nothing on either side, except a marker file it removes at once): the
sources and the destination, what remains to copy (an rsync dry run), the room on the destination, which routes work
and how fast, and from that an estimate. Each step reports as soon as it has found something (report(plan)).

The routes:
  same-fs      both tabs see the same filesystem (a marker written on the destination is there on the source): rsync on
               the source, from one folder to the other
  direct-push  the source logs in to the destination by itself (without asking) and rsync copies straight there
  direct-pull  the other way: the destination logs in to the source
  relay        rsync on the source, its server on the destination, the bytes through slurp (always possible when both
               tabs are connected and both have rsync)"""

import secrets
import shlex
import time

from ..cluster import sshsetup
from ..cluster.storage import parse_quota_table
from ..state import log
from . import relay, rsync as R, runner as RN

SAMPLE = 64 * 1024 * 1024 # bytes of the speed probes at most
PROBE_SECONDS = 6
LARGE_BYTES = 1 << 40     # from here on (or a long estimate) Globus is suggested
LONG_SECONDS = 4 * 3600
SMALL_FILES = 100_000     # this many files, small on average: bundling them is suggested
PER_FILE = {"same-fs": 0.001, "direct-push": 0.002, "direct-pull": 0.002, "relay": 0.004} # seconds per file besides the bytes

FACTS_SCRIPT = ('printf "rsync=%s\\n" "$(rsync --version 2>/dev/null | head -n 1)"; printf "host=%s\\n" "$(hostname)"; '
                'printf "user=%s\\n" "$(id -un)"; printf "home=%s\\n" "$HOME"; '
                'for c in ssh ssh-keygen sbatch setsid stdbuf lfs timeout; do command -v $c >/dev/null 2>&1 && printf "has=%s\\n" $c; done; true')

def facts(host):
  """What a side has: rsync (its version), its host, user and home, the tools a route needs"""
  result = host.sh(FACTS_SCRIPT, timeout=60)
  out = {"has": []}
  for line in (result.stdout or "").splitlines():
    key, _, value = line.partition("=")
    if key == "has":
      out["has"].append(value)
    elif key in ("rsync", "host", "user", "home"):
      out[key] = value
  out["version"] = R.version_of(out.get("rsync", ""))
  return out

SOURCES_SCRIPT = ('for p in "$@"; do e=0; d=0; r=0; case $p in *[*?[]*) r=1; IFS=; for m in $p; do '
                  'if [ -e "$m" ]; then e=1; [ -r "$m" ] || r=0; fi; done; unset IFS;; '
                  '*) [ -e "$p" ] && e=1; [ -d "$p" ] && d=1; [ -r "$p" ] && r=1;; esac; '
                  'printf "%s\\t%s\\t%s\\t%s\\n" "$e" "$d" "$r" "$p"; done')

def sources(host, paths):
  result = host.sh(SOURCES_SCRIPT, *paths)
  found = {}
  for line in (result.stdout or "").splitlines():
    parts = line.split("\t", 3)
    if len(parts) == 4:
      found[parts[3]] = {"path": parts[3], "exists": parts[0] == "1", "dir": parts[1] == "1", "readable": parts[2] == "1"}
  return [found.get(p, {"path": p, "exists": False, "dir": False, "readable": False}) for p in paths]

COUNT_SCRIPT = r"""
for p in "$@"; do
  n=-1
  case $p in *[*?[]*) n=0; IFS=; for m in $p; do [ -e "$m" ] || [ -L "$m" ] && n=$((n + 1)); done; unset IFS;; esac
  if [ "$n" -eq 0 ] || { [ "$n" -lt 0 ] && [ ! -e "$p" ] && [ ! -L "$p" ]; }; then printf '%s\t-\n' "$p"; continue; fi
  if [ "$n" -gt 0 ]; then IFS=; for m in $p; do [ -e "$m" ] || [ -L "$m" ] && find "$m" -printf '%y %s\n' 2>/dev/null; done; unset IFS
  else find "$p" -printf '%y %s\n' 2>/dev/null; fi |
    awk -v p="$p" -v n="$n" '$1 == "f" { f++; b += $2 } $1 == "d" { d++ } $1 == "l" { l++ }
      END { printf "%s\t%d\t%d\t%d\t%.0f\t%d\n", p, f, d, l, b, n }'
done
"""

def count(host, paths, timeout=600):
  """What the sources hold (one find through each): {path: {"files", "dirs", "links", "bytes"} or None if it is not
  there}"""
  result = host.sh(COUNT_SCRIPT, *paths, timeout=timeout)
  out = {p: None for p in paths}
  for line in (result.stdout or "").splitlines():
    parts = line.split("\t")
    if len(parts) == 6 and parts[0] in out:
      try:
        out[parts[0]] = {"files": int(parts[1]), "dirs": int(parts[2]), "links": int(parts[3]), "bytes": int(float(parts[4]))}
        if int(parts[5]) >= 0: # (a pattern: how many match it)
          out[parts[0]]["matches"] = int(parts[5])
      except ValueError:
        pass
  return out

DEST_SCRIPT = ('p=$1; a=$p; while [ ! -e "$a" ] && [ "$a" != / ]; do a=$(dirname "$a"); done; '
               'e=0; d=0; w=0; [ -e "$p" ] && e=1; [ -d "$p" ] && d=1; [ -w "$a" ] && w=1; '
               'printf "dest\\t%s\\t%s\\t%s\\t%s\\n" "$e" "$d" "$w" "$a"; '
               'printf "df\\t%s\\n" "$(df -Pk "$a" 2>/dev/null | tail -n 1)"; '
               'if command -v lfs >/dev/null 2>&1; then printf "lfs\\n"; lfs quota -u "$(id -un)" "$a" 2>/dev/null; printf "end\\n"; fi; '
               'printf "quota\\n"; quota -w -p -u "$(id -un)" 2>/dev/null; printf "end\\n"; true')

def destination(host, path):
  """The destination: whether it is there, the folder it is created in (the nearest that exists) and whether it can be
  written, the free space there and the quota of its filesystem"""
  result = host.sh(DEST_SCRIPT, path, timeout=90)
  out = {"path": path, "exists": False, "dir": False, "writable": False, "ancestor": "", "free": None, "quota": None}
  section, lines = None, {"lfs": [], "quota": []}
  for line in (result.stdout or "").splitlines():
    if line.startswith("dest\t"):
      _, e, d, w, a = (line.split("\t", 4) + [""] * 5)[:5]
      out.update(exists=e == "1", dir=d == "1", writable=w == "1", ancestor=a)
    elif line.startswith("df\t"):
      words = line[3:].split()
      if len(words) >= 6 and words[3].isdigit():
        out["free"] = int(words[3]) * 1024
        out["filesystem"], out["mount"] = words[0], words[5]
    elif line in ("lfs", "quota"):
      section = line
    elif line == "end":
      section = None
    elif section:
      lines[section].append(line)
  rows = parse_quota_table("\n".join(lines["lfs"])) or [r for r in parse_quota_table("\n".join(lines["quota"]))
                                                          if r["filesystem"] in (out.get("filesystem"), out.get("mount"))]
  if rows:
    row = rows[0]
    limit_kb = row["kb_hard"] or row["kb_soft"]
    limit_files = row["files_hard"] or row["files_soft"]
    out["quota"] = {"used": row["kb"] * 1024, "limit": limit_kb * 1024 if limit_kb else None,
                    "files": row["files"], "files_limit": limit_files or None}
  return out

def room_warnings(dest, remaining_bytes, remaining_files, site=None):
  """What may not fit: the free space, the quota (space, files), the purge of a scratch filesystem"""
  out = []
  free = dest.get("free")
  quota = dest.get("quota") or {}
  left = [free] if free is not None else []
  if quota.get("limit"):
    left.append(max(quota["limit"] - quota["used"], 0))
  if left and remaining_bytes is not None and remaining_bytes > min(left):
    out.append(f"it may not fit: {human(remaining_bytes)} to copy, {human(min(left))} left there (free space or quota)")
  if quota.get("files_limit") and remaining_files is not None and quota["files"] + remaining_files > quota["files_limit"]:
    out.append(f"too many files for the quota: {remaining_files} more, {quota['files_limit'] - quota['files']} left of {quota['files_limit']}")
  days = (site or {}).get("scratch_purge_days")
  if days and "scratch" in dest.get("path", "").lower():
    out.append(f"the destination is in scratch: files unused for {days} days are deleted there")
  return out

def human(n):
  import humanize
  return humanize.naturalsize(n or 0, binary=True)

def duration(seconds):
  if seconds is None:
    return "?"
  seconds = int(seconds)
  if seconds < 90:
    return f"{seconds} s"
  minutes = seconds // 60
  if minutes < 90:
    return f"{minutes} min"
  hours, minutes = divmod(minutes, 60)
  if hours < 48:
    return f"{hours} h {minutes:02d} min"
  return f"{hours // 24} d {hours % 24} h"

def estimate(route, remaining_bytes, remaining_files, rate, total_bytes=None, checksum=False):
  """[low, high] seconds for what remains at the measured rate (None without a rate). With checksums rsync first reads
  every file on both sides (total_bytes), also when nothing changed"""
  if not rate or remaining_bytes is None:
    return None
  t = remaining_bytes / rate + (remaining_files or 0) * PER_FILE.get(route, 0.003)
  if checksum:
    t += checksum_seconds(total_bytes, rate) or 0
  return [t * 0.8, t * 1.6]

def checksum_seconds(total_bytes, rate):
  """About how long checksums take: every file read on both sides (at the measured rate, the sides in parallel)"""
  return total_bytes / rate if rate and total_bytes else None

# ---- the other side as the cluster that runs rsync sees it (direct routes)
def target_of(cluster):
  """Host, port, user, jump and the host keys of a tab as this machine knows them (ssh -G), for logging in to it
  from the other cluster (None for this machine)"""
  if not cluster.destination:
    return None
  conf, error = sshsetup.effective_config(cluster.destination, cluster.ssh)
  if conf is None:
    return {"error": error}
  first = lambda key: (conf.get(key) or [""])[0]
  host, port = first("hostname") or cluster.destination, first("port") or "22"
  jump = first("proxyjump")
  out = {"host": host, "port": port, "user": first("user"), "jump": "" if jump in ("", "none") else jump}
  alias = first("hostkeyalias")
  out["known_hosts"] = known_hosts(alias if alias and alias != "none" else host, port,
                                   [f for f in " ".join(conf.get("userknownhostsfile") or []).split() if f])
  return out

def known_hosts(host, port, files):
  """The lines of this machine's known_hosts for host (they go to the run's folder: the other cluster then checks
  the host key as this machine does)"""
  import os
  import subprocess
  name = host if str(port) == "22" else f"[{host}]:{port}"
  lines = []
  for path in files or ["~/.ssh/known_hosts"]:
    path = os.path.expanduser(path)
    if not os.path.exists(path):
      continue
    try:
      out = subprocess.run(["ssh-keygen", "-F", name, "-f", path], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
      continue
    lines += [line for line in out.splitlines() if line.strip() and not line.startswith("#")]
  return "\n".join(dict.fromkeys(lines)) + ("\n" if lines else "")

LOGIN_SCRIPT = 'printf "%s" "$1" > "$2/known_hosts"; chmod 600 "$2/known_hosts"; shift 2; ssh "$@" </dev/null 2>&1 | tail -n 5'
LOGIN_REMOTE = f"printf '\\n{RN.DIRECT_MARK} %s\\n' \"$(rsync --version 2>/dev/null | head -n 1)\""

def login_works(client, target, folder, key=False):
  """Can the cluster client log in to target by itself (no password, no question)? -> (ok, rsync there or why not)"""
  if not target or target.get("error"):
    return False, (target or {}).get("error") or "this machine cannot be reached from a cluster"
  args = [*RN.ssh_options(target, folder, key), target["host"], LOGIN_REMOTE]
  result = client.sh(LOGIN_SCRIPT, target.get("known_hosts", ""), folder, *args, timeout=90)
  out = result.stdout or ""
  for line in out.splitlines():
    if line.startswith(RN.DIRECT_MARK):
      version = R.version_of(line)
      return (True, R.version_text(version)) if version else (False, f"no rsync on {target['host']}")
  said = [l for l in out.splitlines() if l.strip()]
  return False, said[-1][:200] if said else "the login did not work"

NODE_SCRIPT = ('printf "%s" "$1" > "$2/known_hosts"; chmod 600 "$2/known_hosts"; shift 2; '
               'srun "$@" 2>&1 | tail -n 8')
NO_NODE = ("Immediate execution impossible", "Unable to allocate resources", "allocation failure", "Job submit/allocate failed",
           "Requested node configuration is not available", "Invalid partition", "Invalid account")

def login_works_on(client, target, folder, partition, account="", seconds=60):
  """The same login as login_works, from a node of a partition (srun --immediate: if no node is free within seconds
  it is not checked). -> (True | False | None, words)"""
  if not target or target.get("error"):
    return False, (target or {}).get("error") or "the other side cannot be reached"
  srun = [f"--partition={partition}", "--ntasks=1", "--time=00:05:00", f"--immediate={int(seconds)}", "--job-name=slurp-check"]
  if account:
    srun.append(f"--account={account}")
  ssh = ["ssh", *RN.ssh_options(target, folder), target["host"], LOGIN_REMOTE]
  result = client.sh(NODE_SCRIPT, target.get("known_hosts", ""), folder, *srun, "sh", "-c",
                     'ssh "$@" </dev/null 2>&1 | tail -n 5', "sh", *ssh[1:], timeout=seconds + 120, retry=False)
  out = result.stdout or ""
  for line in out.splitlines():
    if line.strip().startswith(RN.DIRECT_MARK):
      version = R.version_of(line)
      return (True, f"a node of {partition} logs in to {target['host']}") if version else (False, f"no rsync on {target['host']}")
  said = [l.strip() for l in out.splitlines() if l.strip()]
  if any(n in out for n in NO_NODE):
    return None, f"no node of {partition} within {seconds} s: not checked there ({said[-1][:120] if said else ''})"
  return False, f"a node of {partition} cannot log in to {target['host']} by itself: {said[-1][:160] if said else 'refused'}"

def rclone_on(client, partition, account="", seconds=60):
  """Is rclone on a node of a partition (a transfer job runs it there; the login nodes may have none)? srun
  --immediate: no node free within seconds: not checked. -> (True | False | None, words)"""
  srun = ["srun", f"--partition={partition}", "--ntasks=1", "--time=00:05:00", f"--immediate={int(seconds)}", "--job-name=slurp-check"]
  if account:
    srun.append(f"--account={account}")
  result = client.sh('"$@" sh -c \'rclone version 2>&1 | head -n 1 || true\' 2>&1 | tail -n 8', *srun, timeout=seconds + 120, retry=False)
  out = result.stdout or ""
  from . import rclone as RC
  found = RC.version_of(out)
  if found:
    return True, f"rclone {'.'.join(map(str, found))} on a node of {partition}"
  said = [l.strip() for l in out.splitlines() if l.strip()]
  if any(n in out for n in NO_NODE):
    return None, f"no node of {partition} within {seconds} s: not checked there ({said[-1][:120] if said else ''})"
  return False, f"no rclone on a node of {partition} either" + (f" ({said[-1][:120]})" if said else "")

TIMED_SCRIPT = ('t0=$(date +%s%N); sh -c "$1" >/dev/null 2>&1; r=$?; t1=$(date +%s%N); '
                'printf "%s %s\\n" "$r" "$(( (t1 - t0) / 1000000 ))"')

def timed(host, command, timeout=120):
  """(exit code, milliseconds) of a command there"""
  result = host.sh(TIMED_SCRIPT, command, timeout=timeout)
  words = (result.stdout or "").split()
  try:
    return int(words[-2]), int(words[-1])
  except (IndexError, ValueError):
    return 255, 0

def sample_file(host, paths):
  """A file of the sources to measure with (a big one: the disk counts too), "" for random data"""
  script = 'for p in "$@"; do find "$p" -type f -size +1M 2>/dev/null | head -n 1; done | head -n 1'
  return ((host.sh(script, *paths, timeout=60).stdout or "").strip().splitlines() or [""])[0]

def direct_rate(client, target, folder, sample, push, key=False, has_timeout=True):
  """Bytes per second of a direct route: a sample through ssh, less the time of the login"""
  ssh = shlex.join(["ssh", *RN.ssh_options(target, folder, key), target["host"]])
  read = f"head -c {SAMPLE} {shlex.quote(sample)}" if sample else f"head -c {SAMPLE} /dev/urandom"
  limit = "timeout 30 " if has_timeout else ""
  code, login = timed(client, f"{limit}{ssh} true </dev/null")
  if code:
    return None
  if push:
    code, ms = timed(client, f"{read} | {limit}{ssh} 'cat >/dev/null'")
  else:
    code, ms = timed(client, f"{limit}{ssh} {shlex.quote(read)} </dev/null | cat >/dev/null")
  if code or ms <= login:
    return None
  return SAMPLE / max((ms - login) / 1000, 0.05)

SAME_FS_WRITE = 'umask 077; printf "%s" "$2" > "$1/.slurp-probe-$2"'
SAME_FS_READ = 'cat "$1/.slurp-probe-$2" 2>/dev/null'
SAME_FS_CLEAN = 'rm -f "$1/.slurp-probe-$2"'

def same_filesystem(source, dest, folder):
  """Does the source see the destination's folder (a marker written there is there on the source too)?"""
  if not folder:
    return False
  token = secrets.token_hex(8)
  if dest.sh(SAME_FS_WRITE, folder, token, retry=False).returncode:
    return False
  try:
    return (source.sh(SAME_FS_READ, folder, token).stdout or "").strip() == token
  finally:
    dest.sh(SAME_FS_CLEAN, folder, token, retry=False)

def same_fs_rate(source, folder, sample):
  """Bytes per second copying a sample within the filesystem"""
  read = f"head -c {SAMPLE} {shlex.quote(sample)}" if sample else f"head -c {SAMPLE} /dev/zero"
  token = secrets.token_hex(6)
  target = f"{folder}/.slurp-probe-{token}"
  code, ms = timed(source, f"{read} > {shlex.quote(target)} && sync; rm -f {shlex.quote(target)}")
  return SAMPLE / max(ms / 1000, 0.05) if not code and ms else None

def globus_hint(source_site, dest_site, remaining_bytes, longest):
  """Globus, when both sites offer it (their rule packs say so), or generally for a large transfer"""
  a, b = (source_site or {}).get("globus"), (dest_site or {}).get("globus")
  big = (remaining_bytes or 0) >= LARGE_BYTES or (longest or 0) >= LONG_SECONDS
  if a and b:
    return {"strong": big, "text": f"Both sites offer Globus ({a}, {b}): for large transfers it is usually the better "
                                   "choice (parallel streams, retries, it runs without slurp)."}
  if a or b:
    return {"strong": False, "text": f"{'The source' if a else 'The destination'} offers Globus ({a or b}): if the other "
                                     "site does too (or with Globus Connect Personal), it may suit large transfers."}
  if big:
    return {"strong": True, "text": "This is a large transfer: a transfer service such as Globus, where both sites offer "
                                    "it, is usually the better choice (parallel streams, retries, it runs without slurp)."}
  return None

def check(source, dest, transfer, source_cluster, dest_cluster, report=lambda plan: None, stop=lambda: False,
          site=lambda cluster: {}):
  """The whole check (in a worker; the ends must be connected). -> the plan; report(plan) after every step"""
  plan = {"started": time.time(), "steps": {}, "routes": {}, "warnings": [], "done": False}
  def step(name, state, text=""):
    plan["steps"][name] = {"state": state, "text": text}
    report(plan)
  dest_path = transfer.dest["path"]
  try:
    paths = R.sources(transfer) # (braces expanded: {1..100}, {a,b})
  except R.TooMany as e:
    plan.update(done=True, error=str(e))
    report(plan)
    return plan

  step("facts", "running", "rsync and the tools on both sides")
  fs, fd = facts(source), facts(dest)
  plan["facts"] = {"source": fs, "dest": fd}
  missing = [e.name for e, f in ((source, fs), (dest, fd)) if not f.get("version")]
  step("facts", "bad" if missing else "ok", f"no rsync on {', '.join(missing)}" if missing else
       f"rsync {R.version_text(fs['version'])} on {source.name}, {R.version_text(fd['version'])} on {dest.name}")
  if stop():
    return plan

  step("paths", "running", "the sources and the destination")
  plan["sources"] = sources(source, paths)
  plan["dest"] = d = destination(dest, dest_path)
  problems = [f"{s['path']} is not there" for s in plan["sources"] if not s["exists"]]
  problems += [f"{s['path']} cannot be read" for s in plan["sources"] if s["exists"] and not s["readable"]]
  if len(problems) > 5: # (a brace expansion: many paths)
    problems = problems[:5] + [f"and {len(problems) - 5} more"]
  if d["exists"] and not d["dir"]:
    problems.append(f"{dest_path} is a file, not a folder")
  if not d["writable"]:
    problems.append(f"{d['ancestor'] or dest_path} cannot be written")
  step("paths", "bad" if problems else "ok", "; ".join(problems) if problems else
       f"{len(paths)} source{'s' if len(paths) != 1 else ''}; {dest_path} " + ("is there" if d["exists"] else f"will be created (in {d['ancestor']})"))
  if stop() or missing:
    plan["done"] = True
    report(plan)
    return plan

  step("same-fs", "running", "the same filesystem on both?")
  try:
    shared = same_filesystem(source, dest, d["path"] if d["exists"] else d["ancestor"])
  except Exception as e:
    log.debug(f"same filesystem: {e!r}")
    shared = False
  plan["routes"]["same-fs"] = {"ok": shared, "why": "" if shared else "the source does not see the destination's filesystem"}
  step("same-fs", "ok" if shared else "info", "the same filesystem: copied on the source, no network" if shared else "not the same filesystem")

  if not problems:
    step("remaining", "running", "what remains to copy (an rsync dry run)")
    try:
      if shared:
        dry = source.sh(R.EXPAND + '; exec rsync "$@"', *R.options(transfer.options, fs["version"], dry_run=True), "--",
                        *R.marked(paths), dest_path.rstrip("/") + "/", timeout=3600)
        stats, code, error = R.parse_stats(dry.stdout), dry.returncode, (dry.stderr or "").strip()[-200:]
      else:
        run = relay.run(source, dest, paths, dest_path, transfer.options, fs["version"], dry_run=True, timeout=3600)
        stats, code, error = run.output.stats, run.code, run.error or "; ".join(run.output.errors[-2:])
    except Exception as e:
      stats, code, error = {}, None, str(e)
    if stats:
      plan.update(total_bytes=stats.get("total_size"), total_files=stats.get("files"),
                  remaining_bytes=stats.get("to_transfer"), remaining_files=stats.get("transferred"))
      step("remaining", "ok" if code in (0, 23, 24) else "warn",
           f"{human(stats.get('to_transfer'))} in {stats.get('transferred', 0)} files to copy (of {human(stats.get('total_size'))}, "
           f"{stats.get('files', 0)} files and folders)" + (f"; {error}" if code not in (0, None) and error else ""))
    else:
      step("remaining", "warn", f"the dry run did not say ({error or 'no statistics'})")
    plan["warnings"] = room_warnings(d, plan.get("remaining_bytes"), plan.get("remaining_files"), site(dest_cluster))
    files, size = plan.get("total_files") or 0, plan.get("total_bytes") or 0
    if files >= SMALL_FILES and size / max(files, 1) < 256 * 1024:
      plan["warnings"].append(f"{files} files, {human(size / max(files, 1))} on average: bundling them first (tar) is "
                              "much faster and kinder to parallel filesystems")
  if stop():
    return plan

  sample = ""
  try:
    sample = sample_file(source, paths)
  except Exception:
    pass

  # the routes, each with its speed
  step("relay", "running", "the relay through slurp: its speed")
  rate = relay.probe(source, dest, sample or paths[0], seconds=PROBE_SECONDS) if fs.get("version") and fd.get("version") else None
  plan["routes"]["relay"] = {"ok": bool(fs.get("version") and fd.get("version")), "rate": rate,
                             "why": "" if fs.get("version") and fd.get("version") else "rsync is missing on a side"}
  step("relay", "ok" if rate else "warn", f"{human(rate)}/s through slurp" if rate else "its speed could not be measured")
  if shared:
    plan["routes"]["same-fs"]["rate"] = same_fs_rate(source, d["path"] if d["exists"] else d["ancestor"], sample)
  if stop():
    return plan

  for route, client, client_facts, other, push in (("direct-push", source, fs, dest_cluster, True),
                                                   ("direct-pull", dest, fd, source_cluster, False)):
    if shared:
      plan["routes"][route] = {"ok": False, "why": "not needed: the same filesystem"}
      continue
    step(route, "running", f"{client.name} logs in to {(other.name if other else '?')} by itself?")
    if client.local:
      plan["routes"][route] = {"ok": False, "why": "this machine is an end: the relay is as direct"}
      step(route, "info", plan["routes"][route]["why"])
      continue
    target = target_of(other)
    if target is None:
      plan["routes"][route] = {"ok": False, "why": f"{client.name} cannot reach this machine"}
      step(route, "info", plan["routes"][route]["why"])
      continue
    plan["routes"][route] = {"ok": False, "target": {k: v for k, v in target.items() if k != "known_hosts"}}
    if "ssh" not in client_facts.get("has", []):
      plan["routes"][route]["why"] = f"no ssh on {client.name}"
      step(route, "info", plan["routes"][route]["why"])
      continue
    try:
      probe_folder = RN.folder_of(client, transfer)
      client.sh('mkdir -p "$1" && chmod 700 "$1"', probe_folder, retry=False)
      ok, why = login_works(client, target, probe_folder, key=bool(transfer.options.get("temp_key")) and route == transfer.route)
    except Exception as e:
      ok, why = False, str(e)
    plan["routes"][route].update(ok=ok, why="" if ok else why)
    # (its sshd answers, only the login without asking is refused: the temporary key, opt-in, could do it)
    plan["routes"][route]["keyable"] = not ok and any(w in why for w in ("Permission denied", "publickey", "Too many authentication"))
    if ok:
      plan["routes"][route]["rate"] = r = direct_rate(client, target, probe_folder, sample, push,
                                                     has_timeout="timeout" in client_facts.get("has", []))
      step(route, "ok", f"{client.name} logs in to {target['host']} (rsync {why})" + (f", {human(r)}/s" if r else ", speed not measured"))
    elif plan["routes"][route]["keyable"]:
      step(route, "info", f"{client.name} reaches {target['host']}, but cannot log in without asking ({why}): "
                          "the temporary key (opt-in, in the next step) could")
    else:
      step(route, "info", f"no login from {client.name} to {target['host']} without asking: {why}")
    if stop():
      return plan

  # the estimates, the recommendation, Globus, transfer jobs
  for route, info in plan["routes"].items():
    info["estimate"] = estimate(route, plan.get("remaining_bytes"), plan.get("remaining_files"), info.get("rate")) if info.get("ok") else None
    client_cluster = dest_cluster if route == "direct-pull" else source_cluster
    info["runs_on"] = ["slurp"] if route == "relay" else ["detached"] + (["job"] if partition_of(client_cluster, site) else [])
  usable = {r: i for r, i in plan["routes"].items() if i.get("ok")}
  order = ["same-fs", "direct-push", "direct-pull", "relay"]
  # (the same filesystem when it can: no network at all; else the fastest)
  plan["recommended"] = ("same-fs" if "same-fs" in usable else
                         min(usable, key=lambda r: ((usable[r].get("estimate") or [float("inf")])[0], order.index(r))) if usable else None)
  longest = max((i["estimate"][1] for i in usable.values() if i.get("estimate")), default=None)
  plan["globus"] = globus_hint(site(source_cluster), site(dest_cluster), plan.get("remaining_bytes"),
                               min((i["estimate"][0] for i in usable.values() if i.get("estimate")), default=longest))
  plan["done"] = True
  plan["ended"] = time.time()
  report(plan)
  return plan

def partition_of(cluster, site):
  """The transfer partition of the site of a tab (its rule pack), "" if none"""
  return str((site(cluster) or {}).get("transfer_partition") or "") if cluster is not None and cluster.destination else ""
