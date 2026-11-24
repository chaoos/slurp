"""A run on a cluster: detached on the login node, or as a Slurm transfer job. It needs no slurp: it writes what it
does into its folder there, ~/.cache/slurp/transfers/<id>/ ($XDG_CACHE_HOME), and slurp reads it from there (also
after a new start).

  run.sh         this runner (POSIX sh, awk, rsync)
  command        the rsync command of the transfer (written by slurp)
  rsh            for a direct route: ssh to the other cluster, without what its login scripts print
  job.sh         for a transfer job: the batch script
  lock/          while a run goes on (a second run of the same transfer does not start); lock/owner: pid, host, job
  alive          touched every 20 s while it runs (a run whose alive is older than 2 minutes is gone)
  preflight      for a direct route: the login to the other cluster, tried first where rsync runs (refused: the run
                 ends at once, result.json says "login": "refused", the log what ssh said)
  progress.json  {"phase", "bytes", "percent", "rate", "eta", "xfr", "left", "checked", "current", "updated"}
  log            what rsync said: its errors, its statistics
  result.json    {"exit", "ended", "host", "job"} when the run ended
"""

import json
import os
import shlex
import time

from . import rsync as R

RUNNER = r"""#!/bin/sh
# One run of a slurp transfer (detached on a login node, or in a transfer job): $1 is its folder
d=$1
cd "$d" || exit 1
if ! mkdir lock 2>/dev/null; then
  if [ -n "$(find alive -mmin -2 2>/dev/null)" ]; then echo "slurp: this transfer runs already" >&2; exit 75; fi
  rm -rf lock; mkdir lock || exit 75
fi
echo "$$ $(hostname) ${SLURM_JOB_ID:-}" > lock/owner
rm -f result.json pipe
touch alive
( while sleep 20; do [ -d "$d/lock" ] || exit 0; touch "$d/alive"; done ) &
beat=$!
srand_now='BEGIN { srand(); t = srand(); print t }'
printf '{"phase": "scanning", "updated": %s}\n' "$(awk "$srand_now")" > progress.json
start=$(wc -l < log 2>/dev/null || echo 0)
echo "== run $(date) on $(hostname)${SLURM_JOB_ID:+, job $SLURM_JOB_ID}" | tee -a log
if [ -f preflight ]; then # (a direct route: the login to the other cluster first, here where rsync runs)
  if ! sh ./preflight </dev/null > preflight.out 2>&1; then
    { echo "slurp: $(hostname) cannot log in to the other cluster by itself:"; grep -v '^ *$' preflight.out | tail -n 5; } >> log
    printf '{"exit": 255, "ended": %s, "host": "%s", "job": "%s", "login": "refused"}\n' "$(awk "$srand_now")" "$(hostname)" "${SLURM_JOB_ID:-}" > result.json
    echo "== ended with 255 (no login)" >> log
    kill $beat 2>/dev/null
    rm -rf lock
    exit 255
  fi
fi
mkfifo pipe || exit 1
awk=awk # (mawk reads a pipe in big blocks: without -W interactive the progress would come only at the end)
if awk -W version 2>&1 | grep -q mawk; then awk="awk -W interactive"; fi
tr=tr # (rsync's progress lines end with a carriage return: lines for awk; stdbuf: as they come, not in blocks)
if command -v stdbuf >/dev/null 2>&1; then tr="stdbuf -o0 tr"; fi
$tr '\r' '\n' < pipe | $awk '
function esc(s) { gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); gsub(/\t/, " ", s); return s }
function now(   t) { srand(); t = srand(); return t }
function write(phase) {
  printf "{\"phase\": \"%s\", \"bytes\": %s, \"percent\": %s, \"rate\": \"%s\", \"eta\": \"%s\", \"xfr\": %s, \"left\": %s, \"checked\": %s, \"current\": \"%s\", \"names\": %s, \"updated\": %s}\n", phase, bytes + 0, pct + 0, rate, eta, xfr + 0, left + 0, checked + 0, esc(current), names + 0, now() > "progress.json"
  close("progress.json")
}
/^Number of files:/ { stats = 1 }
stats || /^total size is / { print >> "log"; next }
/^ *[0-9][0-9,.]* +[0-9]+% / {
  split($0, f, " ")
  bytes = f[1]; gsub(/[,.]/, "", bytes); pct = f[2]; sub(/%/, "", pct); rate = f[3]; eta = f[4]
  if (match($0, /xfr#[0-9]+/)) xfr = substr($0, RSTART + 4, RLENGTH - 4)
  if (match($0, /-chk=[0-9]+\/[0-9]+/)) { s = substr($0, RSTART + 5, RLENGTH - 5); split(s, c, "/"); left = c[1]; checked = c[2] }
  write("copying")
  next
}
/^$/ || /^(sending|receiving) incremental file list$/ { next }
{ current = $0; names++ }
END { write("ended") }' &
apid=$!
sh ./command > pipe 2>> log &
rpid=$!
trap 'kill -TERM $rpid 2>/dev/null' TERM INT HUP
wait $rpid; rc=$?
while [ $rc -gt 128 ] && kill -0 $rpid 2>/dev/null; do wait $rpid; rc=$?; done
if [ $rc -gt 128 ]; then wait $rpid; q=$?; [ $q -ne 127 ] && rc=$q; fi
wait $apid
rm -f pipe
printf '{"exit": %s, "ended": %s, "host": "%s", "job": "%s"}\n' "$rc" "$(awk "$srand_now")" "$(hostname)" "${SLURM_JOB_ID:-}" > result.json
echo "== ended with $rc" >> log
if [ "$rc" -ne 0 ]; then # (its log, also where the run's own output goes: a job's job-<id>.out, a detached run's out)
  echo "slurp: the run ended with $rc; its log ($d/log):" >&2
  tail -n +"$((start + 1))" log | tail -n 60 >&2
fi
rm -f keys rclone.rc # (the keys of an rclone run: never left behind)
kill $beat 2>/dev/null
rm -rf lock
exit $rc
"""

DIRECT_MARK = "SLURP-DIRECT"
# the remote shell of a direct route: ssh, and of its output only what comes after the marker the remote side prints
# (login scripts that print would break rsync's protocol); `read` takes one byte at a time from a pipe, cat the rest
RSH = ('#!/bin/sh\nssh "$@" | { while IFS= read -r l; do [ "$l" = ' + DIRECT_MARK + ' ] && break; done; exec cat; }\n')

# your login to the other cluster (a direct route that logs in "with your login now"): an ssh connection that stays
# open on the login node of the cluster that runs rsync, in a folder of /tmp only you can use (%i: your uid there)
CONTROL_DIR = "/tmp/slurp-%i"
PERSIST = "2h" # (it closes after so long unused; Disconnect closes it at once)

def control_path(tid):
  return f"{CONTROL_DIR}/{tid}"

def ssh_options(target, folder, key=False, master=None, batch=True):
  """ssh's options on the cluster that runs rsync, to log in to the other one (target: host, port, user, jump):
  never asking (batch), the host key as this machine knows it (in the run's folder) or as that cluster knows it;
  master: through your open login (its socket), never a new one"""
  out = (["-o", "BatchMode=yes"] if batch else []) + [
         "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30", "-o", "StrictHostKeyChecking=yes",
         "-o", f"UserKnownHostsFile={folder}/known_hosts ~/.ssh/known_hosts"]
  if master:
    out += ["-o", f"ControlPath={master}", "-o", "ControlMaster=no"]
  if target.get("port") and str(target["port"]) != "22":
    out += ["-p", str(target["port"])]
  if target.get("user"):
    out += ["-l", target["user"]]
  if target.get("jump"):
    out += ["-J", target["jump"]]
  if key:
    out += ["-i", f"{folder}/key", "-o", "IdentitiesOnly=yes"]
  return out

def key_scope(transfer, route):
  """The folder a temporary key is kept to (rrsync on the other cluster): a push: the destination (written only); a
  pull: the folder all its sources are in (read only). None: no such folder (a path that is not absolute)"""
  if route == "direct-push":
    dest = transfer.dest["path"]
    return (dest.rstrip("/") or "/") if dest.startswith("/") else None
  bases = []
  for path in R.sources(transfer):
    if not path.startswith("/"):
      return None
    parts = path.rstrip("/").split("/")
    first = next((i for i, part in enumerate(parts) if R.is_pattern(part)), None) # (a pattern: the folder before it)
    bases.append("/".join(parts[:first if first is not None else -1]) or "/")
  return os.path.commonpath(bases) if bases else None

def relative(path, scope):
  """A path as the restricted key sees it: within its folder (a folder's trailing / kept: its contents)"""
  rel = os.path.relpath(path.rstrip("/") or "/", scope)
  return rel + ("/" if path.endswith("/") and rel != "." else "")

def command(transfer, route, folder, version, target=None, key=False, master=None, scope=None):
  """The rsync command of a run on a cluster (the file `command`): same-fs on the source; direct-push on the source to
  target (the destination); direct-pull on the destination from target (the source). scope: the folder the temporary
  key is kept to there (rrsync): the paths there are within it"""
  opts = R.options(transfer.options, version)
  paths, dest = R.sources(transfer), transfer.dest["path"].rstrip("/") + "/"
  if route == "same-fs":
    args = [*opts, "--", *R.marked(paths), dest]
  else:
    rsh = shlex.join(["sh", f"{folder}/rsh", *ssh_options(target, folder, key, master)])
    remote = [f"--rsh={rsh}", f"--rsync-path=printf '\\n{DIRECT_MARK}\\n'; exec rsync"]
    host = target["host"]
    if route == "direct-push":
      args = [*opts, *remote, "--", *R.marked(paths), f"{host}:{'./' if scope else dest}"]
    else: # (patterns on the other side: its rsync, the sender, expands them)
      args = [*opts, *remote, "--", *[f"{host}:{relative(p, scope) if scope else p}" for p in paths], dest]
  return ("#!/bin/sh\n# the rsync command of slurp transfer " + transfer.id + " (patterns among the sources: their matches)\n"
          "set -- " + shlex.join(args) + "\n" + R.EXPAND + "\nexec rsync \"$@\"\n")

def preflight(target, folder, key=False, master=None):
  """The login to the other cluster with the ssh of the run (the file `preflight`): it must work where rsync runs"""
  # (only the login: ssh ends with 255 when it could not log in; the temporary key refuses `true` (rsync only), so
  # another exit code is a login all the same)
  return ("#!/bin/sh\n# the login of a direct route, tried before rsync\nssh "
          + shlex.join([*ssh_options(target, folder, key, master), target["host"], "true"]) + " </dev/null\n"
          "[ $? -ne 255 ] || exit 255\n")

# your login now: on the cluster that runs rsync, in a terminal (you answer what the other cluster asks), an ssh
# connection to it that stays open there; its folder in /tmp: yours only ($1 the transfer, $2 its run folder, $3 the
# host keys as this machine knows them, then ssh's options and the host)
LOGIN_SCRIPT = r"""d=/tmp/slurp-$(id -u)
mkdir -m 700 "$d" 2>/dev/null
if [ ! -d "$d" ] || [ -L "$d" ] || [ ! -O "$d" ]; then echo "slurp: $d is not yours: not used" >&2; exit 1; fi
chmod 700 "$d" || exit 1
t=$1 f=$2; shift 2
mkdir -p "$f" && chmod 700 "$f" || exit 1
printf '%s' "$1" > "$f/known_hosts"; chmod 600 "$f/known_hosts"; shift
if ssh -O check -o ControlPath="$d/$t" "$@" 2>/dev/null; then echo "slurp: the connection is open already"; exit 0; fi
rm -f "$d/$t"
echo "slurp: logging in to the other cluster from $(hostname); answer what it asks"
ssh -o ControlMaster=yes -o ControlPath="$d/$t" -o ControlPersist=""" + PERSIST + r""" "$@" true || exit $?
ssh -O check -o ControlPath="$d/$t" "$@" 2>/dev/null || { echo "slurp: the login worked, but no connection stayed open" >&2; exit 1; }
echo "slurp: connected; it stays open on $(hostname) until """ + PERSIST + r""" unused, or Disconnect"
"""

def login_args(transfer, folder, target, known_hosts):
  """The arguments of LOGIN_SCRIPT (sh -c LOGIN_SCRIPT sh ARGS…)"""
  return [transfer.id, folder, known_hosts or "", *ssh_options(target, folder, batch=False), target["host"]]

# is it open? / close it ($1 the transfer, then ssh's options and the host): "open <host>" or nothing
CHECK_SCRIPT = ('t=$1; shift; ssh -O check -o ControlPath="/tmp/slurp-$(id -u)/$t" "$@" </dev/null >/dev/null 2>&1 '
                '&& echo "open $(hostname)"; exit 0')
EXIT_SCRIPT = 't=$1; shift; ssh -O exit -o ControlPath="/tmp/slurp-$(id -u)/$t" "$@" </dev/null >/dev/null 2>&1; rm -f "/tmp/slurp-$(id -u)/$t"; exit 0'

def login_state(host, transfer, target, folder):
  """(open, the login node it is open on): your login of a transfer, on the cluster that runs rsync"""
  out = host.sh(CHECK_SCRIPT, transfer.id, *ssh_options(target, folder), target["host"], retry=False).stdout or ""
  words = out.split()
  return (bool(words) and words[0] == "open"), (words[1] if len(words) > 1 else "")

def close_login(host, transfer, target, folder):
  host.sh(EXIT_SCRIPT, transfer.id, *ssh_options(target, folder), target["host"], retry=False)

def job_script(transfer, folder):
  """The batch script of a transfer job"""
  o = transfer.options
  lines = ["#!/bin/sh", f"#SBATCH --job-name={job_name(transfer)}", f"#SBATCH --output={folder}/job-%j.out",
           "#SBATCH --ntasks=1", f"#SBATCH --time={o.get('time') or '24:00:00'}"]
  if o.get("partition"):
    lines.append(f"#SBATCH --partition={o['partition']}")
  if o.get("account"):
    lines.append(f"#SBATCH --account={o['account']}")
  lines += [f"# a run of the slurp transfer {transfer.name!r} ({transfer.id})", f"exec sh {shlex.quote(folder)}/run.sh {shlex.quote(folder)}", ""]
  return "\n".join(lines)

def job_name(transfer):
  return f"slurp-xfer-{transfer.id}"

def folder_of(host, transfer):
  return f"{host.state_root()}/{transfer.id}"

def prepare(host, transfer, command_text, job=False, known_hosts="", login=""):
  """The run's folder on the cluster with its files (login: the preflight of a direct route) -> the folder"""
  folder = folder_of(host, transfer)
  # (the result and progress of the run before: gone, or a new run that waits in the queue would look ended at once)
  result = host.sh('mkdir -p "$1" && chmod 700 "$1" && rm -f "$1/preflight" "$1/preflight.out" "$1/result.json" "$1/progress.json"',
                   folder, retry=False)
  if result.returncode:
    raise OSError(f"cannot create {folder} on {host.name}: {(result.stderr or '').strip()[:200]}")
  host.write(f"{folder}/run.sh", RUNNER, 0o700)
  host.write(f"{folder}/command", command_text, 0o700)
  if "--rsh=" in command_text:
    host.write(f"{folder}/rsh", RSH, 0o700)
    host.write(f"{folder}/known_hosts", known_hosts, 0o600)
  if login:
    host.write(f"{folder}/preflight", login, 0o700)
  if job:
    host.write(f"{folder}/job.sh", job_script(transfer, folder), 0o700)
  return folder

def start_detached(host, folder):
  """Start the run on the login node, away from the session (it goes on when slurp ends) -> the host it runs on"""
  result = host.sh('cd "$1" || exit 1; (setsid sh ./run.sh "$1" </dev/null >/dev/null 2>&1 &) ; hostname', folder, retry=False)
  if result.returncode:
    raise OSError(f"the run did not start on {host.name}: {(result.stderr or '').strip()[:200]}")
  return (result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""

TEST_SCRIPT = 'cd "$1" && printf "%s" "$2" > job-test.sh && sbatch --test-only job-test.sh 2>&1; r=$?; rm -f job-test.sh; exit $r'

def test_job(host, transfer, folder):
  """sbatch --test-only of the transfer's job script there: (ok, what Slurm said: when and where it would start, or
  the error)"""
  result = host.sh(TEST_SCRIPT, folder, job_script(transfer, folder), timeout=90, retry=False)
  said = [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
  line = next((l for l in said if "to start at" in l), "") or (said[-1] if said else "")
  line = line.removeprefix("sbatch: ")
  if result.returncode == 0 and "error" not in line.lower():
    return True, line or "accepted"
  return False, line or f"sbatch --test-only ended with {result.returncode}"

def submit(host, folder, begin=None):
  """Submit the transfer job (begin: epoch it may start) -> its job id"""
  args = ["sbatch", "--parsable"]
  if begin:
    args.append("--begin=" + time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(begin)))
  result = host.run([*args, f"{folder}/job.sh"], timeout=60, retry=False) # (never twice)
  if result.returncode:
    raise OSError(f"sbatch failed on {host.name}: {((result.stderr or '') + (result.stdout or '')).strip()[:300]}")
  jid = (result.stdout or "").strip().splitlines()[-1].split(";")[0] if (result.stdout or "").strip() else ""
  if not jid.isdigit() and not jid.replace("_", "").isdigit():
    raise OSError(f"sbatch said: {(result.stdout or '').strip()[:200]}")
  return jid

STATUS_SCRIPT = ('d=$1; printf "%s\\n" "--progress"; cat "$d/progress.json" 2>/dev/null; printf "\\n%s\\n" "--result"; '
                 'cat "$d/result.json" 2>/dev/null; printf "\\n%s\\n" "--alive"; find "$d/alive" -mmin -2 2>/dev/null; '
                 'printf "%s\\n" "--lock"; cat "$d/lock/owner" 2>/dev/null; printf "%s\\n" "--job"; '
                 'if [ -n "$2" ]; then squeue -h -j "$2" -o "%T %r" 2>/dev/null; fi; printf "%s\\n" "--log"; tail -c 6000 "$d/log" 2>/dev/null')

def status(host, folder, jid=""):
  """What a run on a cluster does (one round trip): {progress, result, alive, lock, job (squeue: state reason), log}"""
  result = host.sh(STATUS_SCRIPT, folder, jid, timeout=60)
  if result.returncode == 255 and not (result.stdout or "").strip():
    raise OSError((result.stderr or "").strip()[:200] or f"{host.name} did not answer")
  return parse_status(result.stdout or "")

def parse_status(text):
  parts, key = {}, None
  for line in text.splitlines():
    if line in ("--progress", "--result", "--alive", "--lock", "--job", "--log"):
      key = line[2:]
      parts[key] = []
      continue
    if key is not None:
      parts[key].append(line)
  def json_of(lines):
    text = "\n".join(lines).strip()
    try:
      return json.loads(text) if text else None
    except ValueError:
      return None
  return {"progress": json_of(parts.get("progress", [])), "result": json_of(parts.get("result", [])),
          "alive": bool("".join(parts.get("alive", [])).strip()), "lock": " ".join(parts.get("lock", [])).strip(),
          "job": " ".join(parts.get("job", [])).strip(), "log": "\n".join(parts.get("log", []))}

def progress_of(p, total=0):
  """The runner's progress.json as a transfer's progress"""
  if not p:
    return {}
  out = {"bytes": int(p.get("bytes") or 0), "percent": int(p.get("percent") or 0), "rate": R.rate_of(str(p.get("rate") or "")),
         "files": int(p.get("names") or 0), "current": str(p.get("current") or ""), "phase": p.get("phase", ""),
         "updated": p.get("updated")}
  eta = str(p.get("eta") or "")
  if eta.count(":") == 2:
    h, m, s = eta.split(":")
    if h.isdigit() and m.isdigit() and s.isdigit():
      out["rsync_eta"] = int(h) * 3600 + int(m) * 60 + int(s)
  if p.get("checked"):
    out["checked"], out["left"] = int(p["checked"]), int(p.get("left") or 0)
  return out

def stop(host, lock):
  """Pause a detached run: TERM to the runner (it passes it on to rsync); lock: "pid host job" of its lock/owner.
  On another login node than the session's, that does not reach it: False"""
  words = lock.split()
  if not words or not words[0].isdigit():
    return False
  if len(words) > 1:
    here = host.run(["hostname"], timeout=30)
    if (here.stdout or "").strip() and (here.stdout or "").strip() != words[1]:
      return host.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", words[1], "kill", "-TERM", words[0]],
                          timeout=60, retry=False).returncode == 0
  return host.kill(words[0], "TERM")

# ---- the temporary key (opt-in): a key on the cluster that runs rsync, allowed on the other one for a while
KEY_COMMENT = "slurp-transfer-{id}"
# the forced command of that key: exactly the command of a direct route (the marker, then an rsync server with its
# arguments, none of the shell's special characters among them), nothing else
FORCED_PROGRAM = ('p="printf \'\\n' + DIRECT_MARK + '\\n\'; exec rsync --server "; c=${SSH_ORIGINAL_COMMAND#"$p"}; '
                  'case "$c" in "$SSH_ORIGINAL_COMMAND"|*[\\;\\&\\|\\`\\$\\(\\)\\<\\>]*) '
                  'echo "slurp: this key only runs rsync" >&2; exit 1;; esac; '
                  'printf \'\\n' + DIRECT_MARK + '\\n\'; eval "exec rsync --server $c"')
FORCED = FORCED_PROGRAM.replace('"', '\\"') # (in authorized_keys' command="…" a " is \")

def make_key(host, folder, transfer):
  """The key pair in the run's folder (made once) -> its public key"""
  comment = KEY_COMMENT.format(id=transfer.id)
  result = host.sh('[ -f "$1/key" ] || ssh-keygen -q -t ed25519 -N "" -C "$2" -f "$1/key" </dev/null && cat "$1/key.pub"',
                       folder, comment, retry=False)
  key = (result.stdout or "").strip().splitlines()
  if result.returncode or not key or not key[-1].startswith("ssh-"):
    raise OSError(f"no key made on {host.name}: {(result.stderr or '').strip()[:200]}")
  return " ".join(key[-1].split()[:2])

def forced(rrsync="", mode="", scope=""):
  """The forced command of a temporary key: rrsync (mode -ro: a pull reads, -wo: a push writes) kept to its folder
  (scope); without rrsync there: any rsync server"""
  if not (rrsync and scope):
    return FORCED
  program = FORCED_PROGRAM.replace('eval "exec rsync --server $c"',
                                   'SSH_ORIGINAL_COMMAND="rsync --server $c" exec ' + shlex.join([rrsync, mode, scope]))
  return program.replace('"', '\\"')

def key_line(public, transfer, hours, rrsync="", mode="", scope=""):
  """The line in authorized_keys: only rsync (the command of a direct route; with rrsync: only in its folder, only
  one way), no shell, no forwarding, until it expires"""
  until = time.strftime("%Y%m%d%H%M", time.gmtime(time.time() + hours * 3600))
  return f'expiry-time="{until}Z",restrict,command="{forced(rrsync, mode, scope)}" {public} {KEY_COMMENT.format(id=transfer.id)}'

# rrsync on the other cluster (rsync's own script that keeps a key to a folder): its path if it can be used (its
# rsync there, python3 for the python one, -wo known), else nothing
RRSYNC_SCRIPT = r"""r=$(command -v rrsync 2>/dev/null) || exit 0
p=$(sed -n "s/^RSYNC *= *'\(.*\)'.*/\1/p; s/^my *\$rsync *= *'\(.*\)'.*/\1/p" "$r" | head -n 1)
[ -n "$p" ] && [ -x "$p" ] || exit 0
if head -n 1 "$r" | grep -q python; then command -v python3 >/dev/null 2>&1 || exit 0; fi
grep -q -- "-wo" "$r" || exit 0
echo "$r"
"""

def rrsync_on(host):
  try:
    return (host.sh(RRSYNC_SCRIPT, retry=False, timeout=30).stdout or "").strip().splitlines()[-1:][0] if host else ""
  except Exception:
    return ""

KEYS_SCRIPT = ('f="$HOME/.ssh/authorized_keys"; mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" || exit 1; '
               '[ -e "$f" ] || { : > "$f" && chmod 600 "$f"; } || exit 1; '
               't=$(mktemp "$HOME/.ssh/.slurp.XXXXXX") || exit 1; grep -v " $1\\$" "$f" > "$t"; '
               'if [ -n "$2" ]; then printf "%s\\n" "$2" >> "$t"; fi; cat "$t" > "$f"; r=$?; rm -f "$t"; exit $r')

def install_key(host, line, transfer, make=""):
  """Put the line into authorized_keys there (one of this transfer replaces the old one); make: a folder made first
  (the destination a restricted key writes to: rrsync needs it there)"""
  if make:
    host.sh('mkdir -p -- "$1"', make, retry=False)
  result = host.sh(KEYS_SCRIPT, KEY_COMMENT.format(id=transfer.id), line, retry=False)
  if result.returncode:
    raise OSError(f"cannot change ~/.ssh/authorized_keys on {host.name}: {(result.stderr or '').strip()[:200]}")

def remove_key(host, transfer):
  """Take this transfer's line out of authorized_keys there"""
  return host.sh(KEYS_SCRIPT, KEY_COMMENT.format(id=transfer.id), "", retry=False).returncode == 0
