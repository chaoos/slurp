"""A run on a cluster: detached on the login node, or as a Slurm transfer job. It needs no slurp: it writes what it
does into its folder there, ~/.cache/slurp/transfers/<id>/ ($XDG_CACHE_HOME), and slurp reads it from there (also
after a new start).

  run.sh         this runner (POSIX sh, awk, rsync)
  command        the rsync command of the transfer (written by slurp)
  rsh            for a direct route: ssh to the other cluster, without what its login scripts print
  job.sh         for a transfer job: the batch script
  lock/          while a run goes on (a second run of the same transfer does not start); lock/owner: pid, host, job
  alive          touched every 20 s while it runs (a run whose alive is older than 2 minutes is gone)
  progress.json  {"phase", "bytes", "percent", "rate", "eta", "xfr", "left", "checked", "current", "updated"}
  log            what rsync said: its errors, its statistics
  result.json    {"exit", "ended", "host", "job"} when the run ended
"""

import json
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
echo "== run $(date) on $(hostname)${SLURM_JOB_ID:+, job $SLURM_JOB_ID}" >> log
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
kill $beat 2>/dev/null
rm -rf lock
exit $rc
"""

DIRECT_MARK = "SLURP-DIRECT"
# the remote shell of a direct route: ssh, and of its output only what comes after the marker the remote side prints
# (login scripts that print would break rsync's protocol); `read` takes one byte at a time from a pipe, cat the rest
RSH = ('#!/bin/sh\nssh "$@" | { while IFS= read -r l; do [ "$l" = ' + DIRECT_MARK + ' ] && break; done; exec cat; }\n')

def ssh_options(target, folder, key=False):
  """ssh's options on the cluster that runs rsync, to log in to the other one (target: host, port, user, jump):
  never asking, the host key as this machine knows it (in the run's folder) or as that cluster knows it"""
  out = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30", "-o", "StrictHostKeyChecking=yes",
         "-o", f"UserKnownHostsFile={folder}/known_hosts ~/.ssh/known_hosts"]
  if target.get("port") and str(target["port"]) != "22":
    out += ["-p", str(target["port"])]
  if target.get("user"):
    out += ["-l", target["user"]]
  if target.get("jump"):
    out += ["-J", target["jump"]]
  if key:
    out += ["-i", f"{folder}/key", "-o", "IdentitiesOnly=yes"]
  return out

def command(transfer, route, folder, version, target=None, key=False):
  """The rsync command of a run on a cluster (the file `command`): same-fs on the source; direct-push on the source to
  target (the destination); direct-pull on the destination from target (the source)"""
  opts = R.options(transfer.options, version)
  paths, dest = transfer.source["paths"], transfer.dest["path"].rstrip("/") + "/"
  if route == "same-fs":
    args = [*opts, "--", *paths, dest]
  else:
    rsh = shlex.join(["sh", f"{folder}/rsh", *ssh_options(target, folder, key)])
    remote = [f"--rsh={rsh}", f"--rsync-path=printf '\\n{DIRECT_MARK}\\n'; exec rsync"]
    host = target["host"]
    if route == "direct-push":
      args = [*opts, *remote, "--", *paths, f"{host}:{dest}"]
    else:
      args = [*opts, *remote, "--", *[f"{host}:{p}" for p in paths], dest]
  return "#!/bin/sh\n# the rsync command of slurp transfer " + transfer.id + "\nexec rsync " + shlex.join(args) + "\n"

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

def folder_of(endpoint, transfer):
  return f"{endpoint.state_root()}/{transfer.id}"

def prepare(endpoint, transfer, command_text, job=False, known_hosts=""):
  """The run's folder on the cluster with its files (-> the folder)"""
  folder = folder_of(endpoint, transfer)
  result = endpoint.sh('mkdir -p "$1" && chmod 700 "$1"', folder, retry=False)
  if result.returncode:
    raise OSError(f"cannot create {folder} on {endpoint.name}: {(result.stderr or '').strip()[:200]}")
  endpoint.write(f"{folder}/run.sh", RUNNER, 0o700)
  endpoint.write(f"{folder}/command", command_text, 0o700)
  if "--rsh=" in command_text:
    endpoint.write(f"{folder}/rsh", RSH, 0o700)
    endpoint.write(f"{folder}/known_hosts", known_hosts, 0o600)
  if job:
    endpoint.write(f"{folder}/job.sh", job_script(transfer, folder), 0o700)
  return folder

def start_detached(endpoint, folder):
  """Start the run on the login node, away from the session (it goes on when slurp ends) -> the host it runs on"""
  result = endpoint.sh('cd "$1" || exit 1; (setsid sh ./run.sh "$1" </dev/null >/dev/null 2>&1 &) ; hostname', folder, retry=False)
  if result.returncode:
    raise OSError(f"the run did not start on {endpoint.name}: {(result.stderr or '').strip()[:200]}")
  return (result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""

def submit(endpoint, folder, begin=None):
  """Submit the transfer job (begin: epoch it may start) -> its job id"""
  args = ["sbatch", "--parsable"]
  if begin:
    args.append("--begin=" + time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(begin)))
  result = endpoint.run([*args, f"{folder}/job.sh"], timeout=60, retry=False) # (never twice)
  if result.returncode:
    raise OSError(f"sbatch failed on {endpoint.name}: {((result.stderr or '') + (result.stdout or '')).strip()[:300]}")
  jid = (result.stdout or "").strip().splitlines()[-1].split(";")[0] if (result.stdout or "").strip() else ""
  if not jid.isdigit() and not jid.replace("_", "").isdigit():
    raise OSError(f"sbatch said: {(result.stdout or '').strip()[:200]}")
  return jid

STATUS_SCRIPT = ('d=$1; printf "%s\\n" "--progress"; cat "$d/progress.json" 2>/dev/null; printf "\\n%s\\n" "--result"; '
                 'cat "$d/result.json" 2>/dev/null; printf "\\n%s\\n" "--alive"; find "$d/alive" -mmin -2 2>/dev/null; '
                 'printf "%s\\n" "--lock"; cat "$d/lock/owner" 2>/dev/null; printf "%s\\n" "--job"; '
                 'if [ -n "$2" ]; then squeue -h -j "$2" -o "%T %r" 2>/dev/null; fi; printf "%s\\n" "--log"; tail -c 6000 "$d/log" 2>/dev/null')

def status(endpoint, folder, jid=""):
  """What a run on a cluster does (one round trip): {progress, result, alive, lock, job (squeue: state reason), log}"""
  result = endpoint.sh(STATUS_SCRIPT, folder, jid, timeout=60)
  if result.returncode == 255 and not (result.stdout or "").strip():
    raise OSError((result.stderr or "").strip()[:200] or f"{endpoint.name} did not answer")
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

def stop(endpoint, lock):
  """Pause a detached run: TERM to the runner (it passes it on to rsync); lock: "pid host job" of its lock/owner.
  On another login node than the session's, that does not reach it: False"""
  words = lock.split()
  if not words or not words[0].isdigit():
    return False
  if len(words) > 1:
    here = endpoint.run(["hostname"], timeout=30)
    if (here.stdout or "").strip() and (here.stdout or "").strip() != words[1]:
      return endpoint.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", words[1], "kill", "-TERM", words[0]],
                          timeout=60, retry=False).returncode == 0
  return endpoint.kill(words[0], "TERM")

# ---- the temporary key (opt-in): a key on the cluster that runs rsync, allowed on the other one for a while
KEY_COMMENT = "slurp-transfer-{id}"
# the forced command of that key: exactly the command of a direct route (the marker, then an rsync server with its
# arguments, none of the shell's special characters among them), nothing else
FORCED_PROGRAM = ('p="printf \'\\n' + DIRECT_MARK + '\\n\'; exec rsync --server "; c=${SSH_ORIGINAL_COMMAND#"$p"}; '
                  'case "$c" in "$SSH_ORIGINAL_COMMAND"|*[\\;\\&\\|\\`\\$\\(\\)\\<\\>]*) '
                  'echo "slurp: this key only runs rsync" >&2; exit 1;; esac; '
                  'printf \'\\n' + DIRECT_MARK + '\\n\'; eval "exec rsync --server $c"')
FORCED = FORCED_PROGRAM.replace('"', '\\"') # (in authorized_keys' command="…" a " is \")

def make_key(endpoint, folder, transfer):
  """The key pair in the run's folder (made once) -> its public key"""
  comment = KEY_COMMENT.format(id=transfer.id)
  result = endpoint.sh('[ -f "$1/key" ] || ssh-keygen -q -t ed25519 -N "" -C "$2" -f "$1/key" </dev/null && cat "$1/key.pub"',
                       folder, comment, retry=False)
  key = (result.stdout or "").strip().splitlines()
  if result.returncode or not key or not key[-1].startswith("ssh-"):
    raise OSError(f"no key made on {endpoint.name}: {(result.stderr or '').strip()[:200]}")
  return " ".join(key[-1].split()[:2])

def key_line(public, transfer, hours):
  """The line in authorized_keys: only rsync (the command of a direct route), no shell, no forwarding, until it expires"""
  until = time.strftime("%Y%m%d%H%M", time.gmtime(time.time() + hours * 3600))
  return f'expiry-time="{until}Z",restrict,command="{FORCED}" {public} {KEY_COMMENT.format(id=transfer.id)}'

KEYS_SCRIPT = ('f="$HOME/.ssh/authorized_keys"; mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" || exit 1; '
               '[ -e "$f" ] || { : > "$f" && chmod 600 "$f"; } || exit 1; '
               't=$(mktemp "$HOME/.ssh/.slurp.XXXXXX") || exit 1; grep -v " $1\\$" "$f" > "$t"; '
               'if [ -n "$2" ]; then printf "%s\\n" "$2" >> "$t"; fi; cat "$t" > "$f"; r=$?; rm -f "$t"; exit $r')

def install_key(endpoint, line, transfer):
  """Put the line into authorized_keys there (one of this transfer replaces the old one)"""
  result = endpoint.sh(KEYS_SCRIPT, KEY_COMMENT.format(id=transfer.id), line, retry=False)
  if result.returncode:
    raise OSError(f"cannot change ~/.ssh/authorized_keys on {endpoint.name}: {(result.stderr or '').strip()[:200]}")

def remove_key(endpoint, transfer):
  """Take this transfer's line out of authorized_keys there"""
  return endpoint.sh(KEYS_SCRIPT, KEY_COMMENT.format(id=transfer.id), "", retry=False).returncode == 0
