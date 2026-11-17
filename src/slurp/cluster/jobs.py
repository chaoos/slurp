"""Slurm queries, the jobs of the job list, their progress and the estimates when they finish, array jobs"""

import getpass
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime


from . import slurm as sl

from ..state import log, OLD_STEP_NAMES, PROGRESS_PATTERN, PROGRESS_PERCENT, PROGRESS_SETTINGS
from .remote import conn, fs_getsize, fs_read, fs_scandir, run, run_cmd, run_many, SLURM_ENV, tab_name
from .paths import existing_files, expand_vars, job_vars, read_text

def parsable(out, fields):
  """Rows of '|'-separated output as dicts, the last field may contain '|'; other lines (e.g. messages) are skipped"""
  rows = [line.split("|", len(fields)-1) for line in out.splitlines()]
  return [dict(zip(fields, row)) for row in rows if len(row) == len(fields)]

SACCT_FIELDS = ["JobID", "JobIDRaw", "JobName", "User", "Account", "Partition", "State", "ExitCode", "NNodes", "NTasks",
                "NodeList", "Submit", "Start", "End", "Elapsed", "Timelimit", "WorkDir", "StdOut", "StdErr",
                "MaxRSS", "AveRSS", "TRESUsageInMax", "ReqMem", "AllocCPUS", "TotalCPU", "AllocTRES", "SubmitLine"] # (SubmitLine last: it may have |)
SQUEUE_FIELDS = ["JobID", "JobName", "User", "Account", "Partition", "State", "Reason", "NNodes", "Elapsed",
                 "Timelimit", "Start", "Submit", "WorkDir", "NodeList"]
SQUEUE_FMT = "%i|%j|%u|%a|%P|%T|%r|%D|%M|%l|%S|%V|%Z|%N"

def sacct_cmd(args):
  return ["sacct", *args, "-P", "-n", f"--format={','.join(SACCT_FIELDS)}"]

def squeue_cmd(args):
  return ["squeue", *args, "-r", "-h", f"--format={SQUEUE_FMT}"]

def sacct(args):
  return parsable(run(sacct_cmd(args)), SACCT_FIELDS)

def squeue(args, quiet=False):
  return parsable(run(squeue_cmd(args), quiet=quiet), SQUEUE_FIELDS)

def is_time(val):
  return bool(val) and val not in ("Unknown", "None", "N/A", "(null)")

def fmt_time(val):
  """2026-11-12T15:14:29 -> 11-12 15:14"""
  try:
    return datetime.fromisoformat(val).strftime("%m-%d %H:%M")
  except (TypeError, ValueError):
    return ""

def job_id_key(jid):
  return tuple(int(n) for n in re.findall(r'\d+', jid)) or (0,)

@dataclass
class JobProgress:
  current: int
  total: int
  line: str
  points: list = field(default_factory=list) # (epoch seconds or None, n, N) of the n/N lines in the tail, oldest first
  path: str = ""                             # the file it was read from
  estimate: dict = field(default_factory=dict)
  percent: bool = False                      # an M% line (current: M, total: 100), not n/N
  shape: str = ""                            # the structure of its line (digits as #): later lines must be like it

  @property
  def text(self):
    """3/10, or 45.5%"""
    return f"{self.current:g}%" if self.percent else f"{self.current}/{self.total}"

PROGRESS = {} # (tab, job id) -> {"file": progress file chosen for the job or None, "points": [(epoch seen, n, N)]}

_saved_progress_files = None # from the config: "tab|job id" -> file

def progress_state(jid):
  global _saved_progress_files
  key = (tab_name(), jid)
  if key not in PROGRESS:
    if _saved_progress_files is None:
      from ..config import load_config
      _saved_progress_files = dict(load_config().get("progress_files") or {})
    PROGRESS[key] = {"file": _saved_progress_files.get(f"{key[0]}|{jid}"), "points": []}
  return PROGRESS[key]

def set_progress_file(jid, path):
  """Choose the progress file of a job (None: its output); kept in the config"""
  global _saved_progress_files
  from ..config import load_config, update_config
  state = progress_state(jid)
  state["file"] = path
  state["points"].clear() # another file, other points …
  state.pop("shape", None) # … and other lines
  files = dict(load_config().get("progress_files") or {})
  key = f"{tab_name()}|{jid}"
  if path:
    files[key] = path
  else:
    files.pop(key, None)
  _saved_progress_files = files
  update_config(progress_files=files)

def progress_path(job):
  """The file checked for n/N lines: the one chosen for the job, else its output file"""
  return progress_state(job.jid)["file"] or job.stdout_path()

# ---- progress files of a whole array, guessed from the one chosen for one of its tasks

ARRAY_PATTERNS = {} # (tab, array job id) -> pattern from progress_pattern()
_saved_array_patterns = None

def progress_pattern(path, task, raw):
  """A pattern for the progress files of the other tasks of an array, from the file chosen for one task: digit groups
  equal to the task's index (zero padding allowed) or else to its job id become placeholders, all other digit
  groups (dates, times, ...) match any digits. When the file name has the index or the job id, the directory is the
  same for all tasks (its digits, as in run1/ or pytest-1/, are part of the path); otherwise the deepest directory
  with one of them is the task's own (task_7/out.log). None if neither is in the path."""
  def template(text, regex):
    # the index as it is written: exactly, else zero-padded (07 for 7) if it is nowhere exactly (a time like 10-01-07
    # must not count as task 1 when the name also has a plain 1)
    exact = task is not None and str(task) in re.findall(r"\d+", text)
    def is_task(part):
      if task is None:
        return False
      return part == str(task) if exact else part.lstrip("0") == str(int(task)) and len(part) > len(str(task))
    out, found = [], set()
    for i, part in enumerate(re.split(r"(\d+)", text)):
      if i % 2 == 0:
        out.append(re.escape(part) if regex else part)
      elif is_task(part):
        out.append("{task}"); found.add("task")
      elif part == raw:
        out.append("{raw}"); found.add("raw")
      else:
        out.append(r"\d+" if regex else part)
    return "".join(out), found
  directory, name = os.path.split(path)
  name_tpl, name_found = template(name, True)
  dir_tpl, dir_found = directory, set()
  if not name_found: # (only the deepest directory that has the index or the job id: the ones above are shared)
    parts = directory.split("/")
    for i in reversed(range(len(parts))):
      tpl, found = template(parts[i], False)
      if found:
        dir_tpl, dir_found = "/".join(parts[:i] + [tpl] + parts[i + 1:]), found
        break
  if not (dir_found | name_found):
    return None
  return {"dir": dir_tpl, "name": name_tpl}

def pattern_for(pattern, task, raw):
  """(directory, compiled name regex) of the pattern for a task"""
  directory = pattern["dir"].replace("{task}", str(task)).replace("{raw}", str(raw))
  name = pattern["name"].replace("{task}", "0*" + re.escape(str(task))).replace("{raw}", re.escape(str(raw)))
  return directory, re.compile(name + "$")

def array_pattern(array_id):
  global _saved_array_patterns
  key = (tab_name(), array_id)
  if key not in ARRAY_PATTERNS:
    if _saved_array_patterns is None:
      from ..config import load_config
      _saved_array_patterns = dict(load_config().get("array_progress_patterns") or {})
    saved = _saved_array_patterns.get(f"{key[0]}|{array_id}")
    if saved:
      ARRAY_PATTERNS[key] = saved
  return ARRAY_PATTERNS.get(key)

def set_array_pattern(array_id, pattern):
  global _saved_array_patterns
  from ..config import load_config, update_config
  ARRAY_PATTERNS[(tab_name(), array_id)] = pattern
  saved = dict(load_config().get("array_progress_patterns") or {})
  saved[f"{tab_name()}|{array_id}"] = pattern
  _saved_array_patterns = saved
  update_config(array_progress_patterns=saved)

def guess_array_progress(jobs, only_missing=True):
  """Progress files for the running tasks of arrays that have a pattern, without a file of their own: the newest file
  of each task's directory that matches (one listing per directory). -> {job id: file}"""
  wanted = []
  for job in jobs:
    task = array_task_id(job)
    if task is None or not job.running or only_missing and progress_state(job.jid)["file"]:
      continue
    pattern = array_pattern(job.jid.partition("_")[0])
    if pattern:
      wanted.append((job, *pattern_for(pattern, task, job.rec.get("JobIDRaw", ""))))
  listings, found = {}, {}
  for job, directory, rx in wanted:
    if directory not in listings:
      try:
        with fs_scandir(directory) as entries:
          listings[directory] = [(e.name, e.stat().st_mtime) for e in entries if e.is_file()]
      except OSError:
        listings[directory] = []
    matches = [(mtime, name) for name, mtime in listings[directory] if rx.match(name)]
    if matches:
      path = os.path.join(directory, max(matches)[1])
      progress_state(job.jid)["file"] = path
      found[job.jid] = path
  return found

LINE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})|(?<![\d:])(\d{2}):(\d{2}):(\d{2})(?![\d:])")

def line_time(line):
  """Epoch seconds of a timestamp in a log line (date and time, or a time of today), None without one"""
  m = LINE_TIME.search(line)
  if not m:
    return None
  try:
    if m.group(1):
      return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").timestamp()
    now = datetime.now()
    t = now.replace(hour=int(m.group(3)), minute=int(m.group(4)), second=int(m.group(5)), microsecond=0)
    return (t.timestamp() - 86400) if t > now else t.timestamp()
  except ValueError:
    return None

def job_start(job):
  try:
    return datetime.fromisoformat(job.rec["Start"]).timestamp() if is_time(job.rec["Start"]) else None
  except ValueError:
    return None

_scan_cache = {}

def progress_scan():
  """How the progress is read, as set in the Options (else the defaults): {"bytes": the end of a file read, "lines":
  of it the last lines, "pace": the current pace over the last so many progress values} (the config is read again only
  when it changed)"""
  from ..config import config_path, load_config
  path = config_path()
  try:
    stamp = (path, os.stat(path).st_mtime)
  except OSError:
    stamp = (path, None)
  if _scan_cache.get("stamp", "none") != stamp:
    c = load_config()
    def value(key):
      default, low, high, _slow = PROGRESS_SETTINGS[key]
      try:
        return min(max(int(c.get(key, default)), low), high)
      except (TypeError, ValueError):
        return default
    _scan_cache.update(stamp=stamp, value={"bytes": value("progress_tail_kib") * 1024, "lines": value("progress_tail_lines"),
                                           "pace": value("progress_pace_points")})
  return _scan_cache["value"]

def estimate_progress(job, progress):
  """When the job will reach N/N: from the average rate since the start of the job, and from the rate of the last
  few progress points. The points are the timestamps of the n/N lines if they have them, else the times the app saw
  a new n (at reloads, so they are a little late)."""
  n, total = progress.current, progress.total
  file_points = [(t, c) for t, c, tot in progress.points if t is not None and tot == total]
  seen = [(t, c) for t, c, tot in progress_state(job.jid)["points"] if tot == total]
  points, source = (file_points, "timestamps in the file") if len(file_points) >= 2 else (seen, "seen at reloads")
  est = {"source": source}
  last_t = points[-1][0] if points and points[-1][1] == n else time.time()
  start = job_start(job)
  if start and n > 0 and last_t > start:
    rate = n / (last_t - start)
    est["overall"] = (rate, last_t + (total - n) / rate)
  recent = []
  for t, c in points:
    if not recent or c != recent[-1][1]:
      recent.append((t, c))
  recent = recent[-progress_scan()["pace"]:]
  if len(recent) >= 2 and recent[-1][1] > recent[0][1] and recent[-1][0] > recent[0][0]:
    rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
    est["recent"] = (rate, recent[-1][0] + (total - recent[-1][1]) / rate, len(recent))
  return est

def record_progress(job, progress):
  """Remember when the app saw a new n/N of the job, and estimate the end"""
  if progress is None:
    return None
  state = progress_state(job.jid)
  if progress.shape and state.get("shape") != progress.shape: # (the structure of its progress lines, for the next reads)
    if state.get("shape"):
      state["points"].clear() # (another phase, another progress: the times seen of the old one do not count)
    state["shape"] = progress.shape
  points = state["points"]
  if not points or points[-1][1:] != (progress.current, progress.total):
    points.append((time.time(), progress.current, progress.total))
    del points[:-200]
  progress.estimate = estimate_progress(job, progress)
  return progress

def duration_text(seconds):
  seconds = max(0, int(seconds))
  if seconds < 60:
    return f"{seconds} s"
  if seconds < 3600:
    return f"{seconds // 60} min"
  if seconds < 86400:
    return f"{seconds // 3600} h {seconds // 60 % 60} min"
  return f"{seconds // 86400} d {seconds // 3600 % 24} h"

def eta_text(eta):
  now = time.time()
  when = datetime.fromtimestamp(eta)
  at = when.strftime("%H:%M" if when.date() == datetime.now().date() else "%m-%d %H:%M")
  return f"~{duration_text(eta - now)} (at {at})" if eta > now else f"overdue (expected {at})"

def rate_text(rate):
  return f"{rate * 3600:.3g}/h" if rate * 60 < 1 else f"{rate * 60:.3g}/min" if rate < 1 else f"{rate:.3g}/s"

def pace_eta(progress, pace):
  """When the job gets to N/N, as a time stamp (None if unknown): pace "same" at the average rate since the start of
  the job, "current" at the rate of the last few progress points"""
  est = progress.estimate if progress else {}
  return (est.get("overall" if pace == "same" else "recent") or (None, None))[1]

@dataclass
class Job:
  rec: dict                              # sacct record of the allocation (or built from squeue)
  steps: list = field(default_factory=list)
  queued: bool = False                   # listed by squeue
  reason: str = ""                       # pending reason (squeue)
  command: str = ""                      # what the job executes (filled by annotate)
  err: tuple = (None, 0)                 # ("file"|"merged"|"missing"|None, size)
  progress: JobProgress | None = None      # latest n/N progress entry from a running job's output
  annotated: bool = True                 # False while command, error file and progress are still being read
  script: str | None = None              # the batch script as submitted (queued jobs, fetched by annotate)
  checks: list | None = None             # findings of the checks (None: not checked yet)
  category: str = ""                     # its category (categories.py), set when the list is drawn

  @property
  def jid(self):
    return self.rec["JobID"]

  @property
  def state(self):
    return self.rec["State"]

  @property
  def running(self):
    return self.state.startswith("RUNNING")

  @property
  def started(self):
    return is_time(self.rec["Start"]) and self.rec["Start"] <= datetime.now().isoformat()

  def stdout_path(self):
    pat = self.rec["StdOut"]
    return sl.expand_filename(pat, self.rec) if pat and pat != "(null)" else None

  def stderr_path(self):
    """Path of the error file, None if there is none or it is the output file"""
    pat = self.rec["StdErr"]
    if not pat or pat == "(null)":
      return None
    path = sl.expand_filename(pat, self.rec)
    return None if path == self.stdout_path() else path

  def merged(self):
    return self.stdout_path() is not None and self.stderr_path() is None

  def node_hours(self):
    try:
      return int(self.rec["NNodes"] or 0) * sl.hours(self.rec["Elapsed"])
    except ValueError:
      return 0

FAILED_JOB_STATES = {
  "BOOT_FAIL", "CANCELLED", "DEADLINE", "FAILED", "NODE_FAIL",
  "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "TIMEOUT",
}

def progress_for_job(job):
  """Return parsed live progress or a terminal-state bar for completed/failed jobs."""
  state = job.state.split()[0].rstrip("+") if job.state else ""
  if state == "COMPLETED":
    return JobProgress(1, 1, "Job completed successfully")
  if state in FAILED_JOB_STATES:
    return JobProgress(0, 1, f"Job ended with state {state}")
  return job.progress if job.running else None

def record_from_squeue(row):
  rec = dict.fromkeys(SACCT_FIELDS, "")
  rec.update({k: row.get(k, "") for k in SQUEUE_FIELDS if k in rec})
  rec["JobIDRaw"] = row["JobID"]
  rec["NodeList"] = "" if row.get("NodeList") in ("", "(null)") else row["NodeList"]
  return rec

def current_user():
  c = conn()
  return c.user if c else getpass.getuser()

def slurm_accounts():
  """The Slurm accounts the user has associations with (sacctmgr), sorted"""
  out = run(["sacctmgr", "-nP", "show", "associations", f"user={current_user()}", "format=account"])
  return sorted({line.strip() for line in out.splitlines() if line.strip()})

def load_jobs(since, scope="me", accounts=(), jids=None):
  """Queued jobs plus jobs that finished after `since` (datetime or None), or only the given job ids.
  scope: "me" (the user's jobs), "all" (the jobs of all the user's accounts) or the name of one account."""
  accts = ",".join(accounts) if scope == "all" else scope
  by_account = scope != "me" and bool(accts)
  # squeue and the finished jobs together (one round trip on a remote cluster), then the accounting of the queued jobs
  cmds = [squeue_cmd(["-j", ",".join(jids)] if jids else ["-A", accts] if by_account else ["--me"])]
  if since and not jids:
    cmds.append(sacct_cmd([*(["-a", "-A", accts] if by_account else ["-u", current_user()]), "-S", since.isoformat(), "-E", "now"]))
  results = run_many(cmds)
  for cmd, res in zip(cmds, results):
    if res.returncode and not (jids and cmd[0] == "squeue"): # finished jobs are unknown to squeue
      log.warning(f"{shlex.join(cmd[:3])} ... failed: {(res.stderr or '').strip()[:300]}")
  if results[0].returncode and not jids:
    raise RuntimeError(f"squeue failed: {(results[0].stderr or '').strip()[:300]}")
  queued = {row["JobID"]: row for row in parsable(results[0].stdout, SQUEUE_FIELDS)}

  rows = []
  ids = set(queued) | set(jids or [])
  if ids:
    rows += sacct(["-j", ",".join(sorted(ids))])
  if len(results) > 1:
    rows += parsable(results[1].stdout, SACCT_FIELDS)

  jobs, steps = {}, {}
  for row in rows:
    base, dot, _ = row["JobID"].partition(".")
    if dot:
      steps.setdefault(base, {})[row["JobID"]] = row
    elif "[" not in base: # pending array tasks like 123_[4-7]: squeue -r lists them one by one
      jobs.setdefault(base, Job(row))

  for jid, row in queued.items():
    job = jobs.setdefault(jid, Job(record_from_squeue(row)))
    job.queued = True
    job.rec["State"] = row["State"] # squeue is up to date
    job.reason = row["Reason"]
    if row["State"] == "PENDING":
      job.rec["Start"] = row["Start"] # estimated start time

  def step_key(row):
    step = row["JobID"].partition(".")[2]
    return (0, int(step)) if step.isdigit() else (-1, step)
  for jid, job in jobs.items():
    job.steps = sorted(steps.get(jid, {}).values(), key=step_key)
  return list(jobs.values())

def step_commands(job):
  cmds = [s["SubmitLine"] for s in job.steps
          if s["JobID"].partition(".")[2].isdigit() and s["SubmitLine"] and s["JobName"] not in OLD_STEP_NAMES]
  if not cmds and job.rec["SubmitLine"].startswith("srun"):
    cmds = [job.rec["SubmitLine"]]
  return cmds

def command_of(job, cache):
  """srun line of the latest step, the job's own srun line, the srun lines of
  the batch script of a queued job or the sbatch/salloc line"""
  cmds = step_commands(job)
  if not cmds and job.queued:
    cmds = [expand_vars(line, job_vars(job)) for line in sl.srun_lines(batch_script(job.jid, cache))]
  cmds = list(dict.fromkeys(cmds))
  if cmds:
    return cmds[-1] + (f"  [+{len(cmds)-1} more]" if len(cmds) > 1 else "")
  return job.rec["SubmitLine"]

def batch_script(jid, cache=None):
  """Batch script as submitted, only available while the job is known to the controller"""
  key = jid.split("_")[0] # all tasks of an array share the script
  if cache is not None and key in cache:
    return cache[key]
  out = run(["scontrol", "write", "batch_script", jid, "-"]) if jid else ""
  if cache is not None:
    cache[key] = out
  return out

def array_task_id(job):
  """Array task index from a selected Slurm array element ID (for example 12345_7)"""
  for jid in (job.jid, job.rec.get("JobIDRaw", "")):
    match = re.fullmatch(r"\d+_(\d+)", str(jid))
    if match:
      return match.group(1)
  return None

def array_spec_in_args(args):
  for i, arg in enumerate(args[1:], 1):
    if arg in ("-a", "--array"):
      return args[i + 1] if i + 1 < len(args) else None
    if arg.startswith("--array="):
      return arg.split("=", 1)[1]
    if arg.startswith("-a") and arg != "-a":
      return arg[2:].lstrip("=")
  return None

def original_array_spec(job):
  """Array expression from sbatch arguments or a #SBATCH directive in its script"""
  submit_line = job.rec.get("SubmitLine") or ""
  try:
    args = shlex.split(submit_line)
  except ValueError:
    args = []
  if spec := array_spec_in_args(args):
    return spec
  workdir = job.rec.get("WorkDir") or "."
  for path in existing_files([submit_line], workdir):
    text = read_text(path)
    spec = None
    for line in text.splitlines():
      line = line.strip()
      if not line:
        continue
      if line.startswith("#SBATCH"):
        try:
          directive = shlex.split(line[len("#SBATCH"):].strip())
        except ValueError:
          continue
        spec = array_spec_in_args(["sbatch", *directive]) or spec
      elif not line.startswith("#"):
        break
    if spec:
      return spec
  return None

def expand_array_spec(spec, limit=1_000_000):
  """Expand Slurm array lists/ranges such as 1,4-10:2%3 to task IDs"""
  ids = set()
  spec = spec.split("%", 1)[0]
  for part in spec.split(","):
    match = re.fullmatch(r"(\d+)(?:-(\d+)(?::(\d+))?)?", part.strip())
    if not match:
      raise ValueError(f"unsupported Slurm array range: {spec}")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    step = int(match.group(3) or 1)
    if end < start or step < 1:
      raise ValueError(f"invalid Slurm array range: {spec}")
    ids.update(range(start, end + 1, step))
    if len(ids) > limit:
      raise ValueError(f"array has more than {limit} task IDs")
  return sorted(ids)

def compress_array_ids(ids):
  """Compress sorted task IDs into the range-list syntax accepted by sbatch"""
  ids = sorted(set(ids))
  if not ids:
    return ""
  ranges = []
  start = previous = ids[0]
  for value in ids[1:]:
    if value == previous + 1:
      previous = value
      continue
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    start = previous = value
  ranges.append(str(start) if start == previous else f"{start}-{previous}")
  return ",".join(ranges)

def array_ids_not_running_or_completed(task_ids, states):
  def should_retry(task_id):
    state = states.get(str(task_id), "").split()
    return not state or state[0].rstrip("+") not in ("RUNNING", "COMPLETED")
  return [task_id for task_id in task_ids if should_retry(task_id)]

def with_array_spec(args, spec):
  """Replace any existing --array option in an sbatch argv with one specific expression"""
  filtered, skip = [], False
  for arg in args[1:]:
    if skip:
      skip = False
    elif arg in ("-a", "--array"):
      skip = True
    elif arg.startswith("--array=") or (arg.startswith("-a") and arg != "-a"):
      continue
    else:
      filtered.append(arg)
  return [args[0], f"--array={spec}", *filtered]

def restart_args(job):
  """Original sbatch command as argv, or None when the job cannot be resubmitted this way"""
  submit_line = (job.rec.get("SubmitLine") or "").strip()
  try:
    args = shlex.split(submit_line)
  except ValueError:
    return None
  if not args or os.path.basename(args[0]) != "sbatch":
    return None
  task_id = array_task_id(job)
  if task_id is not None:
    # Replace command-line array ranges and add a single-task override for any #SBATCH directive.
    args = with_array_spec(args, task_id)
  return args

def sacct_dump(text):
  """The content of sacct --env-vars / --batch-script output: what follows the line of dashes under its title"""
  lines = text.splitlines()
  for i, line in enumerate(lines[:4]):
    if line.strip() and set(line.strip()) == {"-"}:
      return "\n".join(lines[i + 1:]).strip("\n")
  return text.strip("\n")

ENV_NAME = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:%%)?)=")

def parse_env(text):
  """NAME=value lines (a value may go on over several lines, e.g. exported bash functions) -> dict"""
  env, name = {}, None
  for line in text.splitlines():
    m = ENV_NAME.match(line)
    if m:
      name = m.group(1)
      env[name] = line[m.end():]
    elif name is not None:
      env[name] += "\n" + line
  return env

def restart_env(env):
  """The environment of a submission without what belongs to the job that ran (SLURM_*)"""
  return {k: v for k, v in env.items() if not k.startswith("SLURM_") or k == "SLURM_CONF"}

def original_submission(job, args):
  """What Slurm kept of the submission of a job: its environment (if the cluster stores it: AccountingStoreFlags=
  job_env) and its batch script (while the job is known to the controller, or if stored: job_script), and which
  argument of args is the script. -> {"env": dict or None, "script": text or None, "script_arg": index or None}"""
  jid = job.jid
  cmds = [["sacct", "-j", jid, "--env-vars"], ["sacct", "-j", jid, "--batch-script"],
          ["scontrol", "write", "batch_script", jid, "-"]]
  env_res, script_res, scontrol_res = run_many(cmds)
  env = parse_env(sacct_dump(env_res.stdout)) if env_res.returncode == 0 else {}
  script = scontrol_res.stdout if scontrol_res.returncode == 0 and scontrol_res.stdout.startswith("#!") else \
           sacct_dump(script_res.stdout) if script_res.returncode == 0 else ""
  script = script if script.lstrip().startswith("#!") else ""
  script_arg = None
  if script and "--wrap" not in " ".join(args):
    workdir = job.rec.get("WorkDir") or "."
    named = {os.path.normpath(p) for p in existing_files([shlex.join(args[1:])], workdir)}
    for i, arg in enumerate(args[1:], 1):
      path = os.path.normpath(arg if os.path.isabs(arg) else os.path.join(workdir, arg))
      if not arg.startswith("-") and path in named:
        script_arg = i
        break
  return {"env": restart_env(env) or None, "script": script or None, "script_arg": script_arg}

def env_summary(env):
  """What matters most in an environment, in a few words: uenv, modules, Python environment"""
  parts = []
  uenv = env.get("UENV_MOUNT_LIST") or env.get("UENV_VIEW")
  if uenv:
    parts.append(f"uenv {uenv}" + (f" (view {env['UENV_VIEW']})" if env.get("UENV_VIEW") and env.get("UENV_MOUNT_LIST") else ""))
  if env.get("LOADEDMODULES"):
    mods = env["LOADEDMODULES"].split(":")
    parts.append(f"{len(mods)} modules: " + ", ".join(mods[:6]) + (" …" if len(mods) > 6 else ""))
  if env.get("VIRTUAL_ENV") or env.get("CONDA_DEFAULT_ENV"):
    parts.append(f"Python env {env.get('VIRTUAL_ENV') or env.get('CONDA_DEFAULT_ENV')}")
  return f"{len(env)} variables" + (": " + "; ".join(parts) if parts else "")

def array_overview(array_id):
  """All tasks of an array job, from Slurm (sacct for every task, squeue for those still queued; pending ranges like
  12_[8-20%5] are expanded): {"tasks": {index: {"state", "exit", "reason", "start", "end", "elapsed", "nodes", "node"}},
  "errors"}"""
  fields = ["JobID", "State", "ExitCode", "Submit", "Start", "End", "Elapsed", "NNodes", "NodeList"]
  cmds = [["sacct", "-X", "-j", str(array_id), "-P", "-n", f"--format={','.join(fields)}"],
          ["squeue", "-r", "-j", str(array_id), "-h", "--format=%i|%T|%r"]]
  acct, queue = run_many(cmds, timeout=300) # sacct of a big array can take a while
  tasks = {}
  def indices(jid):
    m = re.fullmatch(rf"{re.escape(str(array_id))}_(\d+|\[(.+)\])", jid.strip())
    if not m:
      return []
    if m.group(2) is None:
      return [int(m.group(1))]
    try:
      return expand_array_spec(m.group(2))
    except ValueError:
      return []
  for row in parsable(acct.stdout, fields):
    for i in indices(row["JobID"]):
      tasks[i] = {"state": (row["State"].split() or ["?"])[0].rstrip("+"), "exit": row["ExitCode"], "reason": "",
                  "submit": row["Submit"], "start": row["Start"], "end": row["End"], "elapsed": row["Elapsed"],
                  "nodes": row["NNodes"], "node": row["NodeList"]}
  for line in queue.stdout.splitlines(): # up to date for queued tasks
    jid, _, rest = line.partition("|")
    state, _, reason = rest.partition("|")
    for i in indices(jid):
      task = tasks.setdefault(i, {"exit": "", "submit": "", "start": "", "end": "", "elapsed": "", "nodes": "", "node": ""})
      task.update(state=state.strip(), reason=reason.strip() if state.strip() == "PENDING" else "")
  # squeue answers "Invalid job id specified" when no task of the array is queued any more: no error
  queue_gone = queue.returncode and "invalid job id" in (queue.stderr or "").lower()
  errors = [f"{shlex.join(c[:3])}: {(r.stderr or '').strip()[:200]}" for c, r in zip(cmds, (acct, queue))
            if r.returncode and not (r is queue and queue_gone)]
  return {"tasks": tasks, "errors": errors}

def task_group(task):
  """How a task of an array is grouped: its state, with the exit code of an ended task or the reason of a pending one"""
  state = task["state"]
  if state == "PENDING" and task.get("reason") and task["reason"] != "None":
    return f"PENDING ({task['reason']})"
  if state not in ("RUNNING", "PENDING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED") and task.get("exit"):
    return f"{state} (exit {task['exit']})"
  return state

def array_task_states(parent_id):
  """Current/accounted states by array task ID; fail if either Slurm query is unavailable"""
  states = {}
  commands = [
    ["sacct", "-X", "-j", str(parent_id), "-n", "-P", "--format=JobIDRaw,State"],
    ["squeue", "-r", "-j", str(parent_id), "-h", "--format=%i|%T"],
  ]
  for cmd in commands:
    result = run_cmd(cmd, timeout=45, env=SLURM_ENV)
    if result.returncode:
      raise RuntimeError(result.stderr.strip() or f"{shlex.join(cmd)} failed")
    for line in result.stdout.splitlines():
      fields = line.split("|", 1)
      if len(fields) != 2:
        continue
      jid, state = fields[0].strip().split(".", 1)[0], fields[1].strip()
      match = re.fullmatch(rf"{re.escape(str(parent_id))}_(\d+)", jid)
      if match:
        states[match.group(1)] = state
  return states

def err_status(job, sizes=None):
  """("file", size) of the error file, or why there is none; sizes: known sizes by path (None: missing)"""
  if not (job.rec["StdOut"] or job.rec["StdErr"]) or not job.started:
    return (None, 0)
  if job.merged():
    return ("merged", 0)
  path = job.stderr_path()
  if not path:
    return (None, 0)
  if sizes is not None:
    return ("file", sizes[path]) if sizes.get(path) is not None else ("missing", 0)
  try:
    return ("file", fs_getsize(path))
  except OSError:
    return ("missing", 0)

SIMILAR = 0.6 # how alike the structures of two progress lines must be (difflib's ratio) to count as the same progress

def line_shape(line):
  """The structure of a line: numbers as #, progress bar blocks and repeated spaces gone ("step #/# loss #")"""
  shape = re.sub(r"\d+(?:[.,:]\d+)*", "#", line.strip())
  shape = re.sub(r"[\u2580-\u259f]+", "", shape) # (█▌: a bar's length changes)
  return re.sub(r"\s+", " ", shape)[:160]

def similar(a, b):
  import difflib
  return a == b or difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= SIMILAR

def progress_lines(found, shape=""):
  """Of the lines with n/N or M% (oldest first: (line, n, N, percent)), those of the progress: like the structure
  of the progress so far (shape, from earlier reads); without one, or when it is no longer in the tail, the structure
  the most lines share (a lone line of another structure, a "1/1" somewhere, is not progress; a new one that repeats
  is: the program went on to another phase)"""
  if not found:
    return [], ""
  shapes = [line_shape(f[0]) for f in found]
  if shape:
    like = [f for f, s in zip(found, shapes) if similar(s, shape)]
    if like:
      return like, shape
  groups = [] # [representative shape, [indices]]
  for i, s in enumerate(shapes):
    group = next((g for g in groups if similar(s, g[0])), None)
    if group is None:
      groups.append([s, [i]])
    else:
      group[1].append(i)
  if shape and all(len(g[1]) < 2 for g in groups): # (only lone lines unlike the progress so far: none of them)
    return [], shape
  best = max(groups, key=lambda g: (len(g[1]), g[1][-1])) # (the most lines; of equal ones, the latest)
  return [found[i] for i in best[1]], best[0]

def output_progress(path, tail=None, job=None):
  """Latest valid n/N entry in the tail of a file (only its end is read, see progress_scan; here unless given),
  with the n/N points of the tail and their timestamps (if the lines have one). A line without n/N may say M% (0 to
  100, integer or not): M of 100 (a line with both, as tqdm writes: its n/N, which is exact). Only the lines like
  the progress (progress_lines): of the job's earlier reads (job), else those most lines share."""
  if not path:
    return None
  if tail is None:
    try:
      size = fs_getsize(path)
      scan = progress_scan()["bytes"]
      tail = fs_read(path, max(0, size - scan), scan).decode("utf-8", errors="replace")
    except OSError:
      return None
  found_lines = []
  for line in tail.splitlines()[-progress_scan()["lines"]:]:
    found = None
    for match in reversed(list(PROGRESS_PATTERN.finditer(line))):
      try:
        current, total = map(int, match.groups())
      except ValueError: # extremely long digit sequences
        continue
      if current <= total:
        found = (current, total, False)
        break
    if found is None:
      for match in reversed(list(PROGRESS_PERCENT.finditer(line))):
        value = float(match.group(1))
        if value <= 100:
          found = (int(value) if value.is_integer() else value, 100, True)
          break
    if found is not None:
      found_lines.append((line.strip(), *found))
  shape = progress_state(job.jid).get("shape", "") if job is not None else ""
  lines, shape = progress_lines(found_lines, shape)
  if not lines:
    return None
  line, current, total, percent = lines[-1]
  points = [(line_time(l), c, t) for l, c, t, _p in lines]
  return JobProgress(current, total, line, points=points[-max(50, 2 * progress_scan()["pace"]):], path=path, percent=percent,
                     shape=shape)

PROGRESS_CHUNK = 200 # files per command that reads the progress
PROGRESS_LINES_SCRIPT = (
  # for each file: a marker, then the last lines of its tail that have an n/N or M% (\r too ends a line, as progress
  # bars write), so only these come back over the connection, not the whole tails
  'for f; do printf "\\n%s\\n" "$0"; tail -c {bytes} -- "$f" 2>/dev/null | tr "\\r" "\\n" | tail -n {lines} '
  '| grep -E "[0-9][[:space:]]*/[[:space:]]*[0-9]|[0-9][[:space:]]?%" | tail -n {keep}; done')

def progress_tails(paths):
  """The progress lines of many files in one round trip and few processes: {path: text with the last n/N lines
  of its tail (output_progress parses it as the tail) or None (cannot be read)}"""
  marker = f"==slurp-progress-{os.urandom(6).hex()}=="
  scan = progress_scan()
  script = PROGRESS_LINES_SCRIPT.format(bytes=scan["bytes"], lines=scan["lines"], keep=max(60, 2 * scan["pace"]))
  chunks = [paths[i:i + PROGRESS_CHUNK] for i in range(0, len(paths), PROGRESS_CHUNK)]
  return chunks, [["sh", "-c", script, marker, *chunk] for chunk in chunks], marker

def parse_progress_tails(chunks, results, marker):
  tails = {}
  for chunk, res in zip(chunks, results):
    parts = res.stdout.split("\n" + marker + "\n")[1:] if res.returncode in (0, 1) else []
    for path, text in zip(chunk, parts):
      tails[path] = text
  return tails

ERR_SIZES = {} # (tab, path) -> size of the error file of an ended job: it no longer changes, it is not asked again
STAT_CHUNK = 200 # paths per stat command

def annotate(jobs):
  """Command, error file and progress of the jobs. On a remote cluster the sizes of the error files, the tails of
  the output files and the batch scripts are fetched together, in one round trip."""
  guess_array_progress(jobs) # progress files of array tasks, from the pattern of their array
  if conn() is None:
    cache = {}
    for job in jobs:
      job.command = command_of(job, cache)
      job.script = cache.get(job.jid.split("_")[0]) if job.queued else None
      job.err = err_status(job)
      job.progress = record_progress(job, output_progress(progress_path(job), job=job)) if job.running else None
      job.annotated = True
    return jobs
  tab = tab_name()
  ended = {p for job in jobs if not job.queued and (p := job.stderr_path())} # (not one a queued job writes too)
  ended -= {p for job in jobs if job.queued and (p := job.stderr_path())}
  sizes = {p: ERR_SIZES[(tab, p)] for p in ended if (tab, p) in ERR_SIZES}
  err_paths = sorted({p for job in jobs if err_status(job, {})[0] == "missing" and (p := job.stderr_path())} - set(sizes))
  out_paths = sorted({p for job in jobs if job.running and (p := progress_path(job))})
  scripts = sorted({job.jid.split("_")[0]: job.jid for job in jobs if job.queued and not step_commands(job)}.items())
  chunks = [err_paths[i:i + STAT_CHUNK] for i in range(0, len(err_paths), STAT_CHUNK)]
  out_chunks, progress_cmds, marker = progress_tails(out_paths)
  cmds = ([["stat", "-L", "-c", "%s|%n", "--", *chunk] for chunk in chunks] + progress_cmds +
          [["scontrol", "write", "batch_script", jid, "-"] for _, jid in scripts])
  results = run_many(cmds) if cmds else [] # (all in one round trip)
  for p in err_paths:
    sizes[p] = None
  for res in results[:len(chunks)]: # (stat says on stderr what it cannot read, and reports the others)
    for line in res.stdout.splitlines():
      size, _, path = line.partition("|")
      if path in sizes and size.isdigit():
        sizes[path] = int(size)
  for p in ended & set(err_paths):
    if sizes[p] is not None:
      ERR_SIZES[(tab, p)] = sizes[p]
  results = results[len(chunks):]
  tails = parse_progress_tails(out_chunks, results[:len(progress_cmds)], marker)
  cache = {key: res.stdout for (key, _), res in zip(scripts, results[len(progress_cmds):])}
  for job in jobs:
    job.command = command_of(job, cache)
    job.script = cache.get(job.jid.split("_")[0]) if job.queued else None
    job.err = err_status(job, sizes)
    path = progress_path(job)
    job.progress = record_progress(job, output_progress(path, tails.get(path), job=job)) if job.running and tails.get(path) is not None else None
    job.annotated = True
  return jobs
