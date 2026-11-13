#! /usr/bin/env python3
"""
Interactive terminal UI to browse queued, running and finished slurm jobs:
click a job for its details, steps and files, click a file to read it (with
tail -f style following), and run commands like nvidia-smi on the nodes of a
running job.
"""

import argparse
import asyncio
import codecs
import fcntl
import functools
import logging
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass, field
from datetime import datetime

import humanize
from rich.color import Color
from rich.progress_bar import ProgressBar as RichProgressBar
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, OptionList, ProgressBar, RadioButton, RadioSet, Select, Static, Switch, TextArea, Tree
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual.worker import get_current_worker

import slurm as sl

log = logging.getLogger("slurm_tui")

TAIL_BYTES = 4*1024*1024 # files larger than this are shown from their end
PROGRESS_TAIL_BYTES = 64*1024
PROGRESS_TAIL_LINES = 30
PROGRESS_PATTERN = re.compile(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)")
STEP_NAME = "slurm_tui"  # job name of the steps that run commands on the nodes
PRESETS = {
  "nvidia-smi": "nvidia-smi",
  "GPU csv":    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv",
  "free":       "free -h",
  "top":        "top -bn1 | head -30",
  "uptime":     "uptime",
  "ps":         "ps -u $USER -o pid,pcpu,rss,etime,cmd",
}

# ---------------------------------------------------------------------------
# logging: never write to the terminal, collect warnings to show them in the UI

class Collector(logging.Handler):
  def __init__(self):
    super().__init__(logging.WARNING)
    self.messages = []
  def emit(self, record):
    self.messages.append(record.getMessage())
  def drain(self):
    msgs, self.messages = self.messages, []
    return msgs

collector = Collector()

def setup_logging(logfile):
  for name in ("slurm_tui", "utils"): # "utils" is the logger of slurm.py / helpers.py
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.DEBUG if logfile else logging.WARNING)
    lg.addHandler(collector)
    if logfile:
      fh = logging.FileHandler(logfile)
      fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(funcName)s] %(message)s"))
      lg.addHandler(fh)

# ---------------------------------------------------------------------------
# slurm queries

def run(cmd, timeout=60, quiet=False):
  """Run a command, return its stdout ('' on failure, with a warning unless quiet)"""
  log.debug(f"running command: {shlex.join(cmd)}")
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         env=os.environ | {"SLURM_TIME_FORMAT": "standard"})
  except (OSError, subprocess.TimeoutExpired) as e:
    log.warning(f"{cmd[0]} failed: {e}")
    return ""
  if res.returncode != 0 and not quiet:
    log.warning(f"{shlex.join(cmd[:3])} ... failed: {res.stderr.strip()[:300]}")
  return res.stdout

def parsable(out, fields):
  """Rows of '|'-separated output as dicts, the last field may contain '|'"""
  return [dict(zip(fields, line.split("|", len(fields)-1))) for line in out.splitlines() if line.strip()]

SACCT_FIELDS = ["JobID", "JobIDRaw", "JobName", "User", "Account", "Partition", "State", "ExitCode", "NNodes", "NTasks",
                "NodeList", "Submit", "Start", "End", "Elapsed", "Timelimit", "WorkDir", "StdOut", "StdErr",
                "MaxRSS", "AveRSS", "TRESUsageInMax", "SubmitLine"]
SQUEUE_FIELDS = ["JobID", "JobName", "User", "Account", "Partition", "State", "Reason", "NNodes", "Elapsed",
                 "Timelimit", "Start", "Submit", "WorkDir", "NodeList"]
SQUEUE_FMT = "%i|%j|%u|%a|%P|%T|%r|%D|%M|%l|%S|%V|%Z|%N"

def sacct(args):
  return parsable(run(["sacct", *args, "-P", "-n", f"--format={','.join(SACCT_FIELDS)}"]), SACCT_FIELDS)

def squeue(args, quiet=False):
  return parsable(run(["squeue", *args, "-r", "-h", f"--format={SQUEUE_FMT}"], quiet=quiet), SQUEUE_FIELDS)

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

@dataclass
class Job:
  rec: dict                              # sacct record of the allocation (or built from squeue)
  steps: list = field(default_factory=list)
  queued: bool = False                   # listed by squeue
  reason: str = ""                       # pending reason (squeue)
  command: str = ""                      # what the job executes (filled by annotate)
  err: tuple = (None, 0)                 # ("file"|"merged"|"missing"|None, size)
  progress: JobProgress | None = None      # latest n/N progress entry from a running job's output

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

def load_jobs(since, us, jids=None):
  """Queued jobs plus jobs that finished after `since` (datetime or None), or only the given job ids"""
  accts = ",".join(sl.accounts())
  if jids:
    queue = squeue(["-j", ",".join(jids)], quiet=True) # finished jobs are unknown to squeue
  else:
    queue = squeue(["-A", accts] if us else ["--me"])
  queued = {row["JobID"]: row for row in queue}

  rows = []
  ids = set(queued) | set(jids or [])
  if ids:
    rows += sacct(["-j", ",".join(sorted(ids))])
  if since and not jids:
    who = ["-a", "-A", accts] if us else ["-u", sl.getpass.getuser()]
    rows += sacct([*who, "-S", since.isoformat(), "-E", "now"])

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

def command_of(job, cache):
  """srun line of the latest step, the job's own srun line, the srun lines of
  the batch script of a queued job or the sbatch/salloc line"""
  cmds = [s["SubmitLine"] for s in job.steps
          if s["JobID"].partition(".")[2].isdigit() and s["SubmitLine"] and s["JobName"] != STEP_NAME]
  if not cmds and job.rec["SubmitLine"].startswith("srun"):
    cmds = [job.rec["SubmitLine"]]
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

def array_task_states(parent_id):
  """Current/accounted states by array task ID; fail if either Slurm query is unavailable"""
  env = os.environ | {"SLURM_TIME_FORMAT": "standard"}
  states = {}
  commands = [
    ["sacct", "-X", "-j", str(parent_id), "-n", "-P", "--format=JobIDRaw,State"],
    ["squeue", "-r", "-j", str(parent_id), "-h", "--format=%i|%T"],
  ]
  for cmd in commands:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env)
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

def err_status(job):
  if not (job.rec["StdOut"] or job.rec["StdErr"]) or not job.started:
    return (None, 0)
  if job.merged():
    return ("merged", 0)
  path = job.stderr_path()
  if not path:
    return (None, 0)
  try:
    return ("file", os.path.getsize(path))
  except OSError:
    return ("missing", 0)

def output_progress(path):
  """Read only the tail of an output file and return its latest valid n/N entry."""
  if not path:
    return None
  try:
    with open(path, "rb") as f:
      f.seek(0, os.SEEK_END)
      size = f.tell()
      f.seek(max(0, size - PROGRESS_TAIL_BYTES))
      tail = f.read(PROGRESS_TAIL_BYTES).decode("utf-8", errors="replace")
  except OSError:
    return None
  for line in reversed(tail.splitlines()[-PROGRESS_TAIL_LINES:]):
    for match in reversed(list(PROGRESS_PATTERN.finditer(line))):
      try:
        current, total = map(int, match.groups())
      except ValueError: # extremely long digit sequences
        continue
      if current <= total:
        return JobProgress(current, total, line.strip())
  return None

def annotate(jobs):
  cache = {}
  for job in jobs:
    job.command = command_of(job, cache)
    job.err = err_status(job)
    job.progress = output_progress(job.stdout_path()) if job.running else None
  return jobs

# ---------------------------------------------------------------------------
# job details

@dataclass
class Entry:
  kind: str             # stdout, stderr, script, command, ...
  label: str            # path or description
  path: str = None      # file on disk
  text: str = None      # or content that is not a file
  note: str = ""

def job_vars(job):
  """Environment of the job as far as it can be known, to expand $VARS in its batch script"""
  array_id, _, task = job.jid.partition("_")
  raw = job.rec["JobIDRaw"]
  return os.environ | {
    "SLURM_JOB_ID": raw, "SLURM_JOBID": raw, "SLURM_ARRAY_JOB_ID": array_id, "SLURM_ARRAY_TASK_ID": task,
    "SLURM_SUBMIT_DIR": job.rec["WorkDir"], "SLURM_JOB_NAME": job.rec["JobName"], "PWD": job.rec["WorkDir"],
    "SLURM_JOB_NUM_NODES": job.rec["NNodes"], "SLURM_NNODES": job.rec["NNodes"], "SLURM_NTASKS": job.rec["NTasks"],
  }

def expand_vars(string, env):
  return re.sub(r'\$\{(\w+)\}|\$(\w+)', lambda m: env.get(m.group(1) or m.group(2), m.group(0)), string)

def path_candidates(line):
  """Words of a command line that might be file names, including the values of --opt=value"""
  try:
    words = shlex.split(line, comments=True)
  except ValueError:
    words = line.split()
  for word in words[:500]:
    for part in [word] + ([word.split("=", 1)[1]] if "=" in word else []):
      part = re.sub(r'^\d*[<>]+&?', '', part).rstrip(";&|")
      if part and not part.startswith("-"):
        yield part

def existing_files(lines, workdir, env=None, dirs=False):
  """Existing files (and directories if dirs) named in command lines, relative paths are relative to workdir"""
  found = []
  for line in lines:
    if env is not None:
      line = expand_vars(line, env)
    for cand in path_candidates(line):
      path = os.path.expanduser(cand)
      path = path if os.path.isabs(path) else os.path.join(workdir or ".", path)
      if os.path.isfile(path) or (dirs and os.path.isdir(path) and os.path.normpath(path) != "/"):
        found.append(os.path.normpath(path))
  return found

PATH_DELIMS = set(" \t\"'`=,;()[]{}<>|")

def word_at(line, col):
  """The word around column col of line, delimited by white space, quotes, '=' and brackets"""
  if not (0 <= col < len(line)) or line[col] in PATH_DELIMS:
    return ""
  start, end = col, col
  while start > 0 and line[start-1] not in PATH_DELIMS:
    start -= 1
  while end < len(line) and line[end] not in PATH_DELIMS:
    end += 1
  return line[start:end]

def resolve_path(word, bases=(), env=None):
  """The existing file or directory a word of a text refers to (None if there is none).
  Relative paths are tried against the base directories in order, 'path:123' as path."""
  word = re.sub(r'^\d*[<>]+&?', '', word.strip()).rstrip(".,;:&|")
  if env:
    word = expand_vars(word, env)
  for cand in dict.fromkeys([word, word.split(":")[0]]):
    if not re.search(r'[\w~]', cand) or cand.startswith("-"):
      continue
    cand = os.path.expanduser(cand)
    for path in [cand] if os.path.isabs(cand) else [os.path.join(b, cand) for b in bases if b]:
      if os.path.exists(path):
        return os.path.normpath(path)
  return None

def is_binary(path):
  try:
    with open(path, "rb") as f:
      return b"\0" in f.read(8192)
  except OSError:
    return False

def script_lines(text):
  """Lines of a batch script that may name files: commands and #SBATCH options, no other comments"""
  lines = []
  for line in text.splitlines()[1:]:
    stripped = line.strip()
    if stripped.startswith("#SBATCH"):
      lines.append(stripped[len("#SBATCH"):])
    elif stripped and not stripped.startswith("#"):
      lines.append(stripped)
  return lines

def read_text(path, limit=1024*1024):
  try:
    with open(path, "rb") as f:
      return f.read(limit).decode("utf-8", errors="replace")
  except OSError:
    return ""

def sacct_all(job):
  """All sacct fields of the job and its steps as 'key: value' blocks"""
  out = run(["sacct", "-j", job.jid, "-P", "-o", "ALL"])
  lines = out.splitlines()
  if len(lines) < 2:
    return out
  header = lines[0].split("|")
  blocks = []
  for line in lines[1:]:
    vals = line.split("|", len(header)-1)
    width = max(map(len, header))
    blocks.append("\n".join(f"{k:<{width}}  {v}" for k, v in zip(header, vals) if v))
  return "\n\n".join(blocks) + "\n"

def load_details(job):
  """Everything shown in the job window that needs extra queries or file system access"""
  d = {"files": [], "sstat": {}, "nodes": [], "progress": output_progress(job.stdout_path()) if job.running else None}
  workdir = job.rec["WorkDir"]
  files, seen = d["files"], set()

  def add(entry):
    key = os.path.realpath(entry.path) if entry.path else (entry.kind, entry.label)
    if key not in seen:
      seen.add(key)
      files.append(entry)

  for kind, path in [("stdout", job.stdout_path()), ("stderr", job.stderr_path())]:
    if path:
      add(Entry(kind, path, path=path))
  if job.merged():
    files.append(Entry("stderr", "goes to the stdout file"))
  if workdir:
    add(Entry("workdir", workdir, path=workdir))
  for path in [job.stdout_path(), job.stderr_path()]:
    if path and os.path.isdir(os.path.dirname(path)):
      add(Entry("output dir", os.path.dirname(path), path=os.path.dirname(path)))

  for entry in likely_job_core_dumps(job):
    add(entry)

  submitted = batch_script(job.jid) if job.queued else ""
  if submitted:
    add(Entry("script", "batch script as submitted", text=submitted, note="scontrol write batch_script"))
  if job.queued:
    add(Entry("scontrol", "scontrol show job", text=run(["scontrol", "show", "job", job.jid])))

  on_disk = ""
  submit_line = job.rec["SubmitLine"]
  if submit_line.startswith("sbatch"):
    for path in existing_files([submit_line], workdir):
      on_disk = read_text(path)
      add(Entry("script", path, path=path, note="on disk now, may differ from the submitted one"))
      break

  cmds = [submit_line] + [s["SubmitLine"] for s in job.steps if s["SubmitLine"] and s["JobName"] != STEP_NAME]
  for path in existing_files(cmds, workdir, dirs=True):
    add(Entry("command", path, path=path))
  env = job_vars(job)
  for path in existing_files(script_lines(submitted or on_disk), workdir, env, dirs=True):
    add(Entry("in script", path, path=path))

  for e in files:
    if e.path and os.path.isdir(e.path):
      e.label = e.path.rstrip("/") + "/"
      e.note = e.note or "directory"
    elif e.path and not os.path.isfile(e.path):
      e.note = "does not exist (yet)"
    elif e.path:
      try:
        e.note = e.note or humanize.naturalsize(os.path.getsize(e.path))
        if is_hdf5(e.path):
          e.note += " HDF5"
        elif is_binary(e.path):
          e.note += " binary"
      except OSError:
        e.note = "not readable"
  add(Entry("sacct", "all accounting fields of the job and its steps", text=None, note="sacct -o ALL"))

  if job.running:
    if job.rec["NodeList"]:
      d["nodes"] = run(["scontrol", "show", "hostnames", job.rec["NodeList"]]).split()
  return d

CORE_DUMP_NAME = re.compile(r"^core_(.+)_(\d+)$")

def likely_job_core_dumps(job):
  """Find top-level core files in a job's workdir that match its node and run time."""
  rec = job.rec
  workdir = rec.get("WorkDir")
  if not workdir or workdir in ("(null)", "None") or not os.path.isdir(workdir):
    return []
  try:
    start = datetime.fromisoformat(rec["Start"].replace("Z", "+00:00")).timestamp()
  except (AttributeError, KeyError, TypeError, ValueError):
    return []
  try:
    end = datetime.fromisoformat(rec["End"].replace("Z", "+00:00")).timestamp()
  except (AttributeError, KeyError, TypeError, ValueError):
    if not job.running:
      return []
    end = time.time()

  # NodeList may be a Slurm hostlist expression. Expand it so core_<node>_<pid>
  # names can be checked against the allocation.
  node_list = rec.get("NodeList", "")
  if node_list in ("(null)", "None", "None assigned"):
    node_list = ""
  nodes = set(run(["scontrol", "show", "hostnames", node_list], timeout=10, quiet=True).split()) if node_list else set()
  if not nodes and node_list and not re.search(r"[\[\]]", node_list):
    nodes = set(node_list.split(","))

  found = []
  try:
    with os.scandir(workdir) as entries:
      for de in entries:
        match = CORE_DUMP_NAME.fullmatch(de.name)
        if not match:
          continue
        node, pid = match.groups()
        if nodes and node not in nodes:
          continue
        try:
          stat = de.stat(follow_symlinks=True)
          if not de.is_file(follow_symlinks=True) or not start - 300 <= stat.st_mtime <= end + 300:
            continue
          if not is_core_dump(de.path):
            continue
        except OSError:
          continue
        confidence = "likely" if nodes and node in nodes else "possible"
        note = f"{confidence} · {node} · PID {pid} · {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}"
        found.append(Entry("core", de.name, path=de.path, note=note))
  except OSError:
    return []
  return sorted(found, key=lambda entry: entry.label)

def run_on_nodes(job, cmd, node=None):
  """Run a shell command once per node inside the allocation of a running job"""
  args = ["srun", f"--jobid={job.rec['JobIDRaw']}", "--overlap", "--ntasks-per-node=1", f"--job-name={STEP_NAME}",
          "--label", "--quiet"]
  args += ["--nodes=1", "-w", node] if node else [f"--nodes={job.rec['NNodes'] or 1}"]
  args += ["bash", "-c", f'echo "== $(hostname)"; {cmd}']
  log.debug(f"running command: {shlex.join(args)}")
  try:
    res = subprocess.run(args, capture_output=True, text=True, timeout=60)
    out, err = res.stdout, res.stderr
  except subprocess.TimeoutExpired as e:
    out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    err = "timeout after 60 s"
  except OSError as e:
    out, err = "", str(e)
  tasks, other = {}, []
  for line in out.splitlines():
    if (m := re.match(r'\s*(\d+): ?(.*)', line)):
      tasks.setdefault(int(m.group(1)), []).append(m.group(2))
    else:
      other.append(line)
  text = [f"$ {cmd}    ({node or 'all nodes'}, {datetime.now():%H:%M:%S})", ""]
  for task in sorted(tasks):
    text += tasks[task] + [""]
  text += other + ([f"srun: {err.strip()}"] if err.strip() else [])
  return "\n".join(text) + "\n"

def memory_probe_command(job_id):
  """Per-node CPU RSS and GPU process-memory probe, limited to this job's PIDs."""
  return (
    f"pids=$(scontrol listpids {shlex.quote(str(job_id))} 2>/dev/null | "
    "awk -v self=\"$SLURM_STEP_ID\" 'NR > 1 && $3 != self { printf \"%s%s\", sep, $1; sep=\",\" }'); "
    "if [ -n \"$pids\" ]; then "
    "cpu_kib=$(ps -o rss= -p \"$pids\" 2>/dev/null | awk '{sum += $1} END {print sum+0}'); "
    "printf 'CPU_KIB=%s\\n' \"$cpu_kib\"; printf 'JOB_PIDS=%s\\n' \"$pids\"; "
    "else printf 'CPU_UNAVAILABLE=no job process IDs found\\n'; printf 'JOB_PIDS=\\n'; fi; "
    "if command -v nvidia-smi >/dev/null 2>&1; then "
    "if gpu_output=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null); then "
    "printf '%s\\n' \"$gpu_output\" | sed 's/^/GPU=/'; "
    "else printf 'GPU_UNAVAILABLE=nvidia-smi query failed\\n'; fi; "
    "else printf 'GPU_UNAVAILABLE=nvidia-smi not found\\n'; fi"
  )

def current_memory_usage(job):
  """Query current process RSS and per-job GPU usage; called only by the detail button."""
  fields = ["JobID", "MaxRSS", "AveRSS"]
  rows = parsable(run(["sstat", "-a", "-j", job.rec["JobIDRaw"], "-P", "-n", "-o", ",".join(fields)]), fields)
  sstat = {row["JobID"].partition(".")[2]: row for row in rows}
  output = run_on_nodes(job, memory_probe_command(job.rec["JobIDRaw"]))
  nodes = {}
  node = None
  for line in output.splitlines():
    if line.startswith("== "):
      node = line[3:].strip()
      nodes[node] = {"cpu_kib": None, "pids": set(), "gpu_mib": 0, "gpu_error": ""}
    elif node and line.startswith("CPU_KIB="):
      try:
        nodes[node]["cpu_kib"] = int(line.partition("=")[2])
      except ValueError:
        pass
    elif node and line.startswith("CPU_UNAVAILABLE="):
      nodes[node]["cpu_error"] = line.partition("=")[2]
    elif node and line.startswith("JOB_PIDS="):
      nodes[node]["pids"] = {pid for pid in line.partition("=")[2].split(",") if pid.isdigit()}
    elif node and line.startswith("GPU_UNAVAILABLE="):
      nodes[node]["gpu_error"] = line.partition("=")[2]
    elif node and line.startswith("GPU="):
      pid, sep, memory = line.partition("=")[2].partition(",")
      pid = pid.strip()
      match = re.match(r"\s*(\d+)", memory)
      if sep and pid in nodes[node]["pids"] and match:
        nodes[node]["gpu_mib"] += int(match.group(1))

  cpu_values = [node_data["cpu_kib"] for node_data in nodes.values() if node_data["cpu_kib"] is not None]
  cpu_bytes = sum(cpu_values) * 1024 if cpu_values else None
  cpu_source = "ps"
  if cpu_bytes is None:
    step_counts = {step["JobID"].partition(".")[2]: int(step["NTasks"] or 0) for step in job.steps}
    estimates = [sl.sacct_bytes(row.get("AveRSS") or "") * step_counts.get(step, 0)
                 for step, row in sstat.items()]
    estimates = [value for value in estimates if value]
    if estimates:
      cpu_bytes = sum(estimates)
      cpu_source = "sstat estimate"
  cpu_partial = len(cpu_values) < len(nodes)
  gpu_nodes = [node_data for node_data in nodes.values() if not node_data["gpu_error"]]
  gpu_bytes = sum(node_data["gpu_mib"] for node_data in gpu_nodes) * 1024**2 if gpu_nodes else None
  gpu_partial = len(gpu_nodes) < len(nodes)
  return {"cpu_bytes": cpu_bytes, "cpu_source": cpu_source, "cpu_partial": cpu_partial, "gpu_bytes": gpu_bytes,
          "gpu_partial": gpu_partial, "gpu_errors": [d["gpu_error"] for d in nodes.values() if d["gpu_error"]],
          "sstat": sstat}

def peak_job_memory(job):
  """Slurm-accounted CPU and (when configured) GPU peak memory for an ended job."""
  records = [job.rec, *job.steps]
  cpu_peak = max((sl.sacct_bytes(row.get("MaxRSS") or "") for row in records), default=0)
  gpu_peaks = []
  for row in records:
    for tres in (row.get("TRESUsageInMax") or "").split(","):
      key, sep, value = tres.partition("=")
      if sep and key.strip().lower().endswith("gpumem"):
        gpu_peaks.append(sl.sacct_bytes(value.strip()))
  return cpu_peak, max(gpu_peaks, default=0)

# ---------------------------------------------------------------------------
# widgets and screens

STATE_STYLES = {"RUNNING": "green", "COMPLETED": "green", "COMPLETING": "green", "CONFIGURING": "green",
                "PENDING": "blue", "REQUEUED": "blue", "SUSPENDED": "yellow"}

def state_text(state):
  return Text(state, style=STATE_STYLES.get(state.split(" ")[0], "red"))

def err_text(err):
  kind, size = err
  if kind == "file":
    return Text(f"! {humanize.naturalsize(size)}", style="bold red") if size else Text("")
  if kind == "merged":
    return Text("(stdout)", style="dim")
  if kind == "missing":
    return Text("?", style="yellow")
  return Text("")

class ClickTable(DataTable):
  """Row table where a single click selects (opens) a row and remembers the clicked column"""
  clicked_column = None

  async def _on_click(self, event):
    meta = event.style.meta
    row, col = meta.get("row"), meta.get("column")
    if row is None or col is None or row < 0 or col < 0 or self.cursor_type != "row":
      return # header clicks etc.: Textual calls DataTable._on_click next (handlers run along the MRO)
    event.prevent_default() # but not for clicks on rows, they are handled here
    event.stop()
    self.clicked_column = self.ordered_columns[col].key.value
    self.move_cursor(row=row)
    self.post_message(self.RowSelected(self, row, self.coordinate_to_cell_key(Coordinate(row, col)).row_key))

  def take_clicked_column(self):
    col, self.clicked_column = self.clicked_column, None
    return col

def clean(text):
  """Remove terminal escape sequences and lines overwritten with carriage returns"""
  text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07', '', text)
  return re.sub(r'[^\n]*\r(?!\n)', '', text.replace("\r\n", "\n"))

def crumb_title(crumbs):
  """Breadcrumbs for the header, shortened in the middle when they get long"""
  crumbs = list(crumbs)
  if len(crumbs) > 5:
    crumbs = crumbs[:2] + ["…"] + crumbs[-2:]
  return " › ".join(crumbs)

class PathArea(TextArea):
  """Read-only text where a click on the name of an existing file or directory opens it"""
  class PathClicked(Message):
    def __init__(self, path):
      super().__init__()
      self.path = path

  resolver = None # word -> existing path or None

  def path_at_cursor(self):
    if self.selected_text.strip():
      return self.resolver(self.selected_text.strip()) if self.resolver else None
    row, col = self.cursor_location
    return self.path_at(row, col)

  def path_at(self, row, col):
    if not self.resolver or row >= self.document.line_count:
      return None
    line = self.document.get_line(row)
    word = word_at(line, col) or word_at(line, col - 1) # also just behind the end of a word
    return self.resolver(word) if word else None

  def on_click(self, event):
    if event.chain > 1 or self.selected_text: # double clicks and dragging select text
      return
    row, col = self.get_target_document_location(event)
    path = self.path_at(row, col)
    if path:
      self.post_message(self.PathClicked(path))

@dataclass
class FileView:
  """A specialized viewer selected by inspecting a file, before the text fallback."""
  name: str
  matches: object
  open: object

FILE_VIEWS = []

def register_file_view(name, matches, open_view):
  FILE_VIEWS.append(FileView(name, matches, open_view))

def open_path(app, crumbs, path, bases=(), env=None, follow=False):
  """Open a directory or dispatch a file to its first matching specialized view."""
  if os.path.isdir(path):
    app.push_screen(DirScreen(crumbs, path, bases=bases, env=env))
  elif os.path.isfile(path):
    for view in FILE_VIEWS:
      if view.matches(path):
        view.open(app, crumbs, path, bases, env)
        return
    if is_binary(path):
      app.notify(f"{path} is a binary file", severity="warning")
      return
    bases = list(dict.fromkeys([*bases, os.path.dirname(path)]))
    app.push_screen(FileScreen([*crumbs, os.path.basename(path)], path=path, follow=follow, bases=bases, env=env))
  else:
    app.notify(f"{path} does not exist (yet)", severity="warning")

def open_hdf5(app, crumbs, path, bases=(), env=None):
  """Browse an HDF5 file, or without h5py show its structure as printed by h5dump -H"""
  try:
    import h5py # noqa: F401
  except ImportError:
    if shutil.which("h5dump"):
      cmd = ["h5dump", "-H", path]
      app.push_screen(FileScreen([*crumbs, os.path.basename(path)], text=run(cmd), refresher=lambda: run(cmd), bases=bases, env=env))
      app.notify("h5py is not installed: showing the structure printed by h5dump -H", severity="warning")
    else:
      app.notify(f"{path} is an HDF5 file: install h5py to browse it", severity="warning")
    return
  app.push_screen(H5Screen(crumbs, path, bases=bases, env=env))

class SplitScreen(Screen):
  """Window with its content on the left and, while a shell is open, the terminal pane on the right"""
  SHOW_BACK = False

  def compose(self) -> ComposeResult:
    with Horizontal(id="topbar"):
      if self.SHOW_BACK:
        yield Button("←", id="back", compact=True, tooltip="Back")
      yield Header()
    with Horizontal(classes="split"):
      with Vertical(classes="left"):
        yield from self.compose_left()
      yield Splitter()
      yield TerminalPane()
    yield Footer()

  @on(Button.Pressed)
  def back_pressed(self, event: Button.Pressed) -> None:
    if event.button.id != "back":
      return
    event.stop()
    self.action_back()

  def compose_left(self):
    return []

  def pop_back(self):
    self.app.pop_screen()
    # pop_screen completes after this key action; queue a full redraw behind it.
    self.app.call_later(self.app.refresh, layout=True)

DIR_COLUMNS = [("name", "Name"), ("size", "Size"), ("mtime", "Modified")]
DIR_LIMIT = 20000 # entries listed at most

class DirScreen(SplitScreen):
  """Directory browser: click (or Enter) a subdirectory to enter it, a file to open it"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("backspace,u", "parent", "Parent"),
    Binding("slash", "filter", "Filter"),
    Binding("f", "follow", "Follow"),
    Binding("r", "reload", "Reload"),
    Binding("full_stop", "hidden", "Hidden files"),
  ]

  def __init__(self, crumbs, path, bases=(), env=None):
    super().__init__()
    self.path = os.path.normpath(path)
    self.crumbs = [*crumbs, self.path.rstrip("/") + "/"]
    self.bases, self.env = list(bases), env # passed on to the files opened from here (e.g. the job's WorkDir)
    self.entries = []
    self.pattern = ""
    self.show_hidden = True
    self.following = False
    self.sort_column, self.sort_reverse = "name", False

  def compose_left(self):
    yield Static(id="dstatus")
    yield ClickTable(id="dir", cursor_type="row", zebra_stripes=True)
    yield Input(id="dfilter", placeholder="filter names (Enter: apply, Esc: clear)")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    table = self.query_one("#dir", ClickTable)
    for key, label in DIR_COLUMNS:
      table.add_column(label, key=key)
    self.timer = self.set_interval(2.0, self.action_reload, pause=True)
    self.action_reload()
    table.focus()

  def scan(self):
    entries = []
    try:
      with os.scandir(self.path) as it:
        for i, de in enumerate(it):
          if i >= DIR_LIMIT:
            break
          try:
            st = de.stat()
            isdir = de.is_dir()
          except OSError:
            st, isdir = None, False
          entries.append({"name": de.name, "dir": isdir, "size": st.st_size if st and not isdir else None,
                          "mtime": st.st_mtime if st else 0, "link": de.is_symlink()})
    except OSError as e:
      self.notify(f"cannot list {self.path}: {e}", severity="error")
    return entries

  def action_reload(self):
    self.entries = self.scan()
    self.populate()

  def populate(self):
    table = self.query_one("#dir", ClickTable)
    current = None
    if table.row_count:
      try:
        current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
      except Exception:
        pass
    table.clear()
    if self.path != "/":
      table.add_row(Text("../", style="bold"), "", "", key="..")
    keys = {"name": lambda e: e["name"].lower(), "size": lambda e: e["size"] or 0, "mtime": lambda e: e["mtime"]}
    shown = [e for e in self.entries if (self.show_hidden or not e["name"].startswith("."))
             and self.pattern.lower() in e["name"].lower()]
    shown.sort(key=keys[self.sort_column], reverse=self.sort_reverse)
    shown.sort(key=lambda e: not e["dir"]) # directories first
    for e in shown:
      style = "bold blue" if e["dir"] else ""
      if e["name"].startswith("."):
        style += " dim"
      name = Text(e["name"] + ("/" if e["dir"] else "") + (" @" if e["link"] else ""), style=style.strip())
      size = humanize.naturalsize(e["size"]) if e["size"] is not None else ""
      mtime = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M:%S") if e["mtime"] else ""
      table.add_row(name, size, mtime, key=e["name"])
    if current is not None:
      try:
        table.move_cursor(row=table.get_row_index(current))
      except Exception:
        pass
    status = f"{self.path} · {len(shown)} of {len(self.entries)} entries"
    if len(self.entries) >= DIR_LIMIT:
      status += f" (only the first {DIR_LIMIT} are listed)"
    if self.pattern:
      status += f" · filter '{self.pattern}'"
    status += " · following (f)" if self.following else ""
    self.query_one("#dstatus", Static).update(status)

  def open(self, name):
    path = os.path.dirname(self.path) if name == ".." else os.path.join(self.path, name)
    open_path(self.app, self.crumbs, path, bases=self.bases, env=self.env)

  @on(DataTable.RowSelected, "#dir")
  def selected(self, event):
    event.stop()
    self.query_one("#dir", ClickTable).take_clicked_column()
    if event.row_key.value is not None:
      self.open(event.row_key.value)

  @on(DataTable.HeaderSelected, "#dir")
  def sort_by(self, event):
    event.stop()
    col = event.column_key.value
    if col == self.sort_column:
      self.sort_reverse = not self.sort_reverse
    else:
      self.sort_column, self.sort_reverse = col, col != "name"
    self.populate()

  def action_back(self):
    search = self.query_one("#dfilter", Input)
    if search.has_class("visible"):
      search.remove_class("visible")
      self.pattern = ""
      self.populate()
      self.query_one("#dir").focus()
    else:
      self.pop_back()

  def action_parent(self):
    if self.path != "/":
      self.open("..")

  def action_filter(self):
    search = self.query_one("#dfilter", Input)
    search.add_class("visible")
    search.value = self.pattern
    search.focus()

  @on(Input.Changed, "#dfilter")
  def filter_changed(self, event):
    self.pattern = event.value
    self.populate()

  @on(Input.Submitted, "#dfilter")
  def filter_submitted(self, event):
    event.input.remove_class("visible")
    self.query_one("#dir").focus()

  def action_follow(self):
    self.following = not self.following
    (self.timer.resume if self.following else self.timer.pause)()
    self.populate()

  def action_hidden(self):
    self.show_hidden = not self.show_hidden
    self.populate()

class FileScreen(SplitScreen):
  """Read-only viewer for a file (with tail -f style following) or for text (optionally re-run by a refresher)"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("enter", "open_path", "Open path"),
    Binding("f", "follow", "Follow"),
    Binding("slash", "search", "Search"),
    Binding("n", "find(True)", "Next", show=False),
    Binding("N", "find(False)", "Prev", show=False),
    Binding("g", "top", "Top"),
    Binding("G", "bottom", "Bottom"),
    Binding("r", "reload", "Reload"),
    Binding("w", "watch", "Watch"),
    Binding("A", "load_all", "Load all", show=False),
    Binding("l", "less", "less"),
  ]

  def __init__(self, crumbs, path=None, text=None, refresher=None, follow=False, bases=(), env=None):
    super().__init__()
    self.crumbs, self.path, self.text, self.refresher = crumbs, path, text, refresher
    self.bases, self.env = list(bases), env # to resolve relative paths in the text
    self.following = follow and path is not None
    self.watching = False
    self.full = False
    self.file_pos = 0
    self.truncated = False
    self.term = ""
    self.decoder = None
    self.timer = None

  def compose_left(self):
    yield Static(id="fstatus")
    area = PathArea(id="text", read_only=True, show_line_numbers=True, soft_wrap=False, highlight_cursor_line=False)
    area.resolver = lambda word: resolve_path(word, self.bases, self.env)
    yield area
    yield Input(id="search", placeholder="search (Enter: find, Esc: cancel)")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.load()
    self.timer = self.set_interval(1.0, self.poll, pause=not self.following)
    self.rerun_timer = self.set_interval(5.0, self.rerun, pause=True)
    self.query_one("#text").focus()

  def check_action(self, action, parameters):
    if action == "follow" or action == "load_all":
      return self.path is not None
    if action == "watch":
      return self.refresher is not None
    return True

  @property
  def area(self):
    return self.query_one("#text", TextArea)

  def update_status(self):
    parts = []
    if self.path:
      try:
        parts.append(f"{self.path}  {humanize.naturalsize(os.path.getsize(self.path))}")
      except OSError:
        parts.append(f"{self.path}  (not readable)")
      if self.truncated:
        parts.append(f"showing the last {humanize.naturalsize(TAIL_BYTES)} (A: load all)")
      parts.append("following" if self.following else "not following (f)")
    if self.refresher:
      parts.append("watching every 5 s" if self.watching else "w: watch, r: run again")
    self.query_one("#fstatus", Static).update(" · ".join(parts))

  def load(self):
    area = self.area
    if self.path is None:
      area.load_text(clean(self.text or ""))
      self.update_status()
      return
    self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
      with open(self.path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        self.truncated = not self.full and size > TAIL_BYTES
        f.seek(size - TAIL_BYTES if self.truncated else 0)
        data = f.read()
        self.file_pos = f.tell()
    except OSError as e:
      data, self.file_pos = f"cannot read {self.path}: {e}\n".encode(), 0
    if self.truncated:
      data = data.partition(b"\n")[2] # drop the partial first line
    area.load_text(clean(self.decoder.decode(data)))
    area.history.clear()
    self.action_bottom()
    self.update_status()

  def poll(self):
    """Append what was written to the file since the last poll"""
    try:
      size = os.path.getsize(self.path)
      if size < self.file_pos: # truncated or replaced
        self.load()
        return
      if size == self.file_pos:
        return
      with open(self.path, "rb") as f:
        f.seek(self.file_pos)
        data = f.read(TAIL_BYTES)
        self.file_pos = f.tell()
    except OSError:
      return
    area = self.area
    at_end = area.scroll_y >= area.max_scroll_y - 1
    area.insert(clean(self.decoder.decode(data)), area.document.end, maintain_selection_offset=True)
    area.history.clear()
    if at_end:
      area.scroll_end(animate=False)
    self.update_status()

  def action_back(self):
    search = self.query_one("#search", Input)
    if search.has_class("visible"):
      search.remove_class("visible")
      self.area.focus()
    else:
      self.pop_back()

  @on(PathArea.PathClicked)
  def path_clicked(self, event):
    open_path(self.app, self.crumbs, event.path, self.bases, self.env)

  def action_open_path(self):
    path = self.area.path_at_cursor()
    if path:
      open_path(self.app, self.crumbs, path, self.bases, self.env)
    else:
      self.notify("no existing file or directory at the cursor", severity="warning")

  def action_follow(self):
    self.following = not self.following
    if self.following:
      self.poll()
      self.action_bottom()
      self.timer.resume()
    else:
      self.timer.pause()
    self.update_status()

  def action_top(self):
    self.area.move_cursor((0, 0))
    self.area.scroll_home(animate=False)

  def action_bottom(self):
    area = self.area
    area.move_cursor((area.document.line_count - 1, 0))
    area.scroll_end(animate=False)

  def action_load_all(self):
    self.full = True
    self.load()

  def action_reload(self):
    if self.refresher:
      self.rerun()
    else:
      self.load()

  def action_watch(self):
    self.watching = not self.watching
    (self.rerun_timer.resume if self.watching else self.rerun_timer.pause)()
    self.update_status()

  @work(thread=True, exclusive=True, group="rerun")
  def rerun(self):
    text = self.refresher()
    self.app.call_from_thread(self.show_text, text)

  def show_text(self, text):
    self.text = text
    self.load()

  def action_search(self):
    search = self.query_one("#search", Input)
    search.add_class("visible")
    search.value = self.term
    search.focus()

  @on(Input.Submitted, "#search")
  def search_submitted(self, event):
    event.input.remove_class("visible")
    self.term = event.value
    self.area.focus()
    self.action_find(True)

  def action_find(self, forward):
    if not self.term:
      self.action_search()
      return
    area, term = self.area, self.term.lower()
    doc = area.document
    n = doc.line_count
    sel = area.selection
    row, col = (max(sel.start, sel.end) if forward else min(sel.start, sel.end))
    for i in range(n + 1):
      r = (row + i) % n if forward else (row - i) % n
      line = doc.get_line(r).lower()
      if forward:
        c = line.find(term, col if i == 0 else 0)
      else:
        c = line.rfind(term, 0, col if i == 0 else len(line))
        c = -1 if (i == 0 and c >= col) else c
      if c >= 0:
        area.selection = Selection((r, c), (r, c + len(term)))
        area.scroll_cursor_visible(center=True)
        return
    self.notify(f"'{self.term}' not found", severity="warning")

  def action_less(self):
    path = self.path
    if path is None:
      import tempfile
      with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write(self.text or "")
        path = tmp.name
    try:
      with self.app.suspend():
        subprocess.run(["less", "-R", "+F" if self.following else "+G", path])
    except SuspendNotSupported:
      self.notify("cannot suspend the terminal here", severity="error")
    finally:
      if path != self.path:
        os.unlink(path)
    if self.path:
      self.poll()

HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
H5_ROWS = 500       # rows of a dataset shown at a time
H5_COLS = 50        # columns (or fields of a compound type) shown at a time
H5_MEMBERS = 5000   # members of a group listed at most

def is_hdf5(path):
  """HDF5 signature at the start of the file or behind a user block (512, 1024, 2048, ... bytes)"""
  try:
    with open(path, "rb") as f:
      offset = 0
      while offset <= 1 << 20:
        f.seek(offset)
        sig = f.read(8)
        if sig == HDF5_SIGNATURE:
          return True
        if len(sig) < 8:
          return False
        offset = 512 if offset == 0 else 2*offset
  except OSError:
    pass
  return False

def is_core_dump(path):
  """Whether path is an ELF core file (not merely a file named 'core')."""
  try:
    with open(path, "rb") as f:
      header = f.read(18)
    if len(header) < 18 or header[:4] != b"\x7fELF":
      return False
    byte_order = {1: "little", 2: "big"}.get(header[5])
    return byte_order is not None and int.from_bytes(header[16:18], byte_order) == 4 # ET_CORE
  except OSError:
    return False

def open_h5(path):
  """Open read-only without file locking, so files that a running job writes can be read too"""
  import h5py
  try:
    return h5py.File(path, "r", locking=False)
  except TypeError: # h5py < 3.5
    return h5py.File(path, "r")

def parse_selection(text, shape):
  """'[3, :, 10:20]' -> tuple with an int or a slice for every axis ('...' and missing trailing axes: all)"""
  text = text.strip()
  if text.startswith("[") and text.endswith("]"):
    text = text[1:-1]
  parts = [p.strip() for p in text.split(",")] if text.strip() else []
  if parts.count("...") > 1:
    raise ValueError("only one ... allowed")
  if "..." in parts:
    i = parts.index("...")
    parts[i:i + 1] = [":"] * max(0, len(shape) - len(parts) + 1)
  if len(parts) > len(shape):
    raise ValueError(f"the dataset has {len(shape)} dimensions, not {len(parts)}")
  parts += [":"] * (len(shape) - len(parts))
  sel = []
  for p, n in zip(parts, shape):
    items = p.split(":")
    try:
      nums = [int(x) if x.strip() else None for x in items]
    except ValueError:
      nums = None
    if nums is None or len(nums) > 3 or (len(nums) == 1 and nums[0] is None):
      raise ValueError(f"'{p}' is not an index or a slice like 10:20 or ::2")
    if len(nums) == 1:
      if not -n <= nums[0] < n:
        raise ValueError(f"index {nums[0]} is out of range for size {n}")
      sel.append(nums[0] % n)
    else:
      nums += [None] * (3 - len(nums))
      if nums[2] is not None and nums[2] < 1:
        raise ValueError(f"only positive steps are supported: '{p}'")
      sel.append(slice(*nums))
  return tuple(sel)

def window(sel, shape, row0=0, col0=0):
  """Restrict the (at most two) sliced axes of a selection to the rows and columns shown.
  Returns the new selection and for each sliced axis (indices shown, number of indices selected)."""
  sel = list(sel)
  axes = [k for k, s in enumerate(sel) if isinstance(s, slice)]
  if len(axes) > 2:
    raise ValueError(f"{len(axes)} dimensions selected: fix all but two with an index, e.g. [0, :, :]")
  shown = []
  for k, start, limit in zip(axes, (row0, col0), (H5_ROWS, H5_COLS)):
    full = range(*sel[k].indices(shape[k]))
    part = full[start:start + limit]
    sel[k] = slice(part[0], part[-1] + 1, part.step) if len(part) else slice(0, 0)
    shown.append((part, len(full)))
  return tuple(sel), shown

def h5_value(v, width=300):
  """Short text for a value of an attribute or a cell"""
  import numpy as np
  if isinstance(v, bytes):
    s = v.decode("utf-8", errors="replace")
  elif isinstance(v, np.ndarray):
    if v.dtype.kind == "S":
      v = np.char.decode(v, "utf-8", errors="replace")
    s = np.array2string(v, threshold=20, edgeitems=3, max_line_width=10**6)
  else:
    s = str(v)
  s = s.replace("\n", " ")
  return s if len(s) <= width else s[:width - 1] + "…"

def h5_link_target(path, filename):
  """File name of an external link, relative to the file that has the link"""
  return os.path.normpath(os.path.join(os.path.dirname(path), os.path.expanduser(filename)))

class H5Screen(SplitScreen):
  """HDF5 browser: tree of groups and datasets, info and attributes of the selected one, and a table of its data"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("s", "selection", "Selection"),
    Binding("left_square_bracket", "page(-1, 0)", "Rows ↑", key_display="["),
    Binding("right_square_bracket", "page(1, 0)", "Rows ↓", key_display="]"),
    Binding("left_curly_bracket", "page(0, -1)", "Cols ←", key_display="{"),
    Binding("right_curly_bracket", "page(0, 1)", "Cols →", key_display="}"),
    Binding("r", "reload", "Reload"),
  ]

  def __init__(self, crumbs, path, start="/", bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.start = start
    self.bases, self.env = list(bases), env
    self.file = None
    self.current = None # data of the selected tree node
    self.dataset_selections = {} # dataset path -> selection text typed for it
    self.row0 = self.col0 = 0

  def compose_left(self):
    yield Static(id="hstatus")
    with Horizontal(id="hbody"):
      yield Tree(os.path.basename(self.path), id="htree", data={"path": "/", "kind": "group"})
      with Vertical(id="hright"):
        with VerticalScroll(id="hinfo"):
          yield Static(id="hinfotext")
        yield Input(id="hslice", placeholder="selection, e.g. [0, :, 10:20] or [..., 3] (Enter: apply)")
        yield Static(id="hpage")
        yield DataTable(id="hdata", zebra_stripes=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#htree", Tree).show_root = True
    self.action_reload(first=True)
    self.query_one("#htree").focus()

  def on_unmount(self):
    self.close_file()

  def close_file(self):
    if self.file is not None:
      try:
        self.file.close()
      except Exception:
        pass
      self.file = None

  # --------------------------------------------------------------- tree
  def action_reload(self, first=False):
    """(Re)open the file and rebuild the tree with the same groups expanded and the same item selected"""
    tree = self.query_one("#htree", Tree)
    expanded = set()
    if not first:
      stack = [tree.root]
      while stack:
        node = stack.pop()
        if node.is_expanded and node.data and node.data["kind"] == "group":
          expanded.add(node.data["path"])
        stack.extend(node.children)
    target = self.start if first else (self.current or {}).get("path", "/")
    self.close_file()
    tree.clear()
    tree.root.data = {"path": "/", "kind": "group"}
    try:
      self.file = open_h5(self.path)
    except Exception as e:
      self.query_one("#hstatus", Static).update(f"{self.path} · cannot open: {e}")
      self.show_message(f"cannot open {self.path}:\n{e}")
      return
    size = humanize.naturalsize(os.path.getsize(self.path))
    self.query_one("#hstatus", Static).update(f"{self.path}  {size} · r: reload (e.g. while a job writes it)")
    self.load_children(tree.root)
    tree.root.expand()
    for p in sorted(expanded, key=lambda p: p.count("/")):
      node = self.find_node(p)
      if node is not None:
        self.load_children(node)
        node.expand()
    self.goto(target)

  def find_node(self, path, expand=False):
    """Tree node of a path; with expand, the groups on the way are expanded"""
    node = self.query_one("#htree", Tree).root
    for name in [p for p in path.split("/") if p]:
      self.load_children(node)
      if expand:
        node.expand()
      child = next((c for c in node.children if c.data and c.data.get("name") == name), None)
      if child is None:
        return node if expand else None
      node = child
    return node

  def goto(self, path):
    node = self.find_node(path or "/", expand=True)
    tree = self.query_one("#htree", Tree)
    self.call_after_refresh(lambda: tree.move_cursor(node))
    self.show(node.data)

  def load_children(self, node):
    import h5py
    data = node.data
    if not data or data["kind"] != "group" or data.get("loaded") or self.file is None:
      return
    data["loaded"] = True
    try:
      group = self.file[data["path"]]
      names = list(group)
    except Exception as e:
      node.add_leaf(Text(f"cannot list: {e}", style="red"), data={"path": data["path"], "kind": "error", "error": str(e)})
      return
    for name in names[:H5_MEMBERS]:
      path = data["path"].rstrip("/") + "/" + name
      item = {"path": path, "name": name}
      try:
        link = group.get(name, getlink=True)
      except Exception:
        link = None
      if isinstance(link, h5py.ExternalLink):
        item.update(kind="external", file=link.filename, target=link.path)
        node.add_leaf(Text.assemble((name, "bold magenta"), (f"  ⇢ {link.filename}:{link.path}", "dim")), data=item)
        continue
      soft = f"  → {link.path}" if isinstance(link, h5py.SoftLink) else ""
      try:
        obj = group[name]
      except Exception as e:
        item.update(kind="error", error=f"broken link{soft}: {e}")
        node.add_leaf(Text.assemble((name, "red"), (soft or "  (cannot open)", "dim")), data=item)
        continue
      if isinstance(obj, h5py.Group):
        item["kind"] = "group"
        node.add(Text.assemble((name + "/", "bold blue"), (soft, "dim")), data=item)
      elif isinstance(obj, h5py.Dataset):
        item["kind"] = "dataset"
        shape = "scalar" if obj.shape == () else "empty" if obj.shape is None else "×".join(map(str, obj.shape))
        node.add_leaf(Text.assemble(name, (soft, "dim"), (f"  {shape} {obj.dtype}", "dim")), data=item)
      else:
        item["kind"] = "datatype"
        node.add_leaf(Text.assemble(name, (soft, "dim"), ("  type", "dim")), data=item)
    if len(names) > H5_MEMBERS:
      node.add_leaf(Text(f"… {len(names) - H5_MEMBERS} more members not listed", style="dim"), data=None)

  @on(Tree.NodeExpanded, "#htree")
  def expanded(self, event):
    event.stop()
    self.load_children(event.node)

  @on(Tree.NodeHighlighted, "#htree")
  def highlighted(self, event):
    event.stop()
    if event.node.data and event.node.data is not self.current:
      self.show(event.node.data)

  @on(Tree.NodeSelected, "#htree")
  def selected(self, event):
    event.stop()
    data = event.node.data
    if data and data["kind"] == "external":
      target = h5_link_target(self.path, data["file"])
      if os.path.isfile(target) and is_hdf5(target):
        self.app.push_screen(H5Screen(self.crumbs, target, start=data["target"], bases=self.bases, env=self.env))
      else:
        self.notify(f"{target} of the external link does not exist or is no HDF5 file", severity="warning")
    elif data and data["kind"] == "dataset":
      self.query_one("#hdata").focus()

  # --------------------------------------------------------------- info and data
  def show_message(self, text):
    self.query_one("#hinfotext", Static).update(text)
    self.show_table(None)

  def show(self, data):
    self.current, self.row0, self.col0 = data, 0, 0
    if not data:
      return
    kind = data["kind"]
    if kind == "error":
      return self.show_message(Text(f"{data['path']}\n{data['error']}", style="red"))
    if kind == "external":
      target = h5_link_target(self.path, data["file"])
      return self.show_message(Text.assemble((data["path"], "bold"), "\nexternal link to ", (f"{data['target']}", "bold"),
                                             " in ", (target, "bold"), "\nEnter or click: open it"))
    try:
      obj = self.file[data["path"]]
      text = self.describe(obj, kind)
    except Exception as e:
      return self.show_message(Text(f"{data['path']}\ncannot read: {e}", style="red"))
    self.query_one("#hinfotext", Static).update(text)
    if kind == "dataset" and obj.shape:
      inp = self.query_one("#hslice", Input)
      inp.value = self.dataset_selections.get(data["path"], "")
    self.show_data()

  def describe(self, obj, kind):
    t = Text()
    t.append(obj.name, style="bold")
    if kind == "group":
      t.append(f"\ngroup with {len(obj)} members")
    elif kind == "dataset":
      shape = "scalar" if obj.shape == () else "empty (no dataspace)" if obj.shape is None else " × ".join(map(str, obj.shape))
      t.append(f"\ndataset  {shape}  {obj.dtype}")
      if obj.dtype.names:
        t.append("\nfields: " + ", ".join(f"{n} {obj.dtype.fields[n][0]}" for n in obj.dtype.names))
      layout = f"chunks {obj.chunks}" if obj.chunks else "contiguous"
      filters = [f for f in (obj.compression and f"{obj.compression}" + (f"({obj.compression_opts})" if obj.compression_opts is not None else ""),
                             obj.shuffle and "shuffle", obj.fletcher32 and "fletcher32", obj.scaleoffset and "scaleoffset") if f]
      t.append(f"\n{layout}" + (", " + ", ".join(filters) if filters else ""))
      try:
        stored = obj.id.get_storage_size()
        t.append(f"\n{humanize.naturalsize(obj.nbytes)} of data, {humanize.naturalsize(stored)} on disk")
        if stored and obj.nbytes:
          t.append(f" (ratio {obj.nbytes / stored:.2g})")
      except Exception:
        pass
      if obj.fillvalue is not None and obj.shape:
        t.append(f"\nfill value {h5_value(obj.fillvalue)}")
      try:
        dims = [", ".join(d.keys()) or (d.label if d.label else "") for d in obj.dims]
        if any(dims):
          t.append("\ndimensions: " + " × ".join(d or "-" for d in dims))
      except Exception:
        pass
      if obj.shape == ():
        t.append("\nvalue: ", style="bold")
        t.append(self.read_scalar(obj))
    else:
      t.append(f"\nnamed datatype {obj.dtype}")
    if obj.file.filename != self.file.filename:
      t.append(f"\nin file {obj.file.filename}")
    try:
      attrs = list(obj.attrs)
    except Exception as e:
      t.append(f"\ncannot list the attributes: {e}", style="red")
      return t
    t.append(f"\n\n{len(attrs)} attribute{'s' if len(attrs) != 1 else ''}" + (":" if attrs else ""), style="bold" if attrs else "dim")
    for name in attrs:
      t.append(f"\n  {name}", style="bold")
      try:
        t.append(f" = {h5_value(obj.attrs[name])}")
      except Exception as e:
        t.append(f" (cannot read: {e})", style="red")
    return t

  def read_scalar(self, obj):
    import h5py
    try:
      ds = obj.asstr() if h5py.check_string_dtype(obj.dtype) else obj
      return h5_value(ds[()], width=2000)
    except Exception as e:
      return f"cannot read: {e}"

  def show_table(self, rows, columns=(), status=""):
    table = self.query_one("#hdata", DataTable)
    table.clear(columns=True)
    sliced = rows is not None and self.current and self.current["kind"] == "dataset"
    self.query_one("#hslice", Input).display = bool(sliced and self.file[self.current["path"]].shape)
    self.query_one("#hpage", Static).update(status)
    self.query_one("#hpage", Static).display = bool(status)
    table.display = rows is not None
    if rows is None:
      return
    for c in columns:
      table.add_column(c)
    for label, cells in rows:
      table.add_row(*cells, label=label)

  def show_data(self):
    """Table with the rows and columns of the current page of the selected dataset"""
    import h5py
    data = self.current
    if not data or data["kind"] != "dataset":
      return self.show_table(None)
    ds = self.file[data["path"]]
    if not ds.shape: # scalar or empty: shown in the info
      return self.show_table(None)
    text = self.query_one("#hslice", Input).value
    try:
      sel = parse_selection(text, ds.shape)
      sel, shown = window(sel, ds.shape, self.row0, self.col0)
    except ValueError as e:
      return self.show_table([], status=Text(str(e), style="red"))
    if any(not len(part) for part, _ in shown):
      return self.show_table([], status="nothing selected")
    try:
      src = ds.asstr() if h5py.check_string_dtype(ds.dtype) else ds
      values = src[sel]
    except Exception as e:
      return self.show_table([], status=Text(f"cannot read: {e}", style="red"))
    names = ds.dtype.names
    status = []
    if len(shown) == 0: # every axis fixed by an index: a single value
      return self.show_table([("", [h5_value(values)])], ["value"])
    rows_part, nrows = shown[0]
    status.append(f"rows {rows_part[0]}–{rows_part[-1]} ({len(rows_part)} of {nrows}" + (", [ ]: page" if len(rows_part) < nrows else "") + ")")
    if len(shown) == 2:
      cols_part, ncols = shown[1]
      columns = [str(i) for i in cols_part]
      status.append(f"columns {cols_part[0]}–{cols_part[-1]} ({len(cols_part)} of {ncols}" + (", { }: page" if len(cols_part) < ncols else "") + ")")
      rows = [(str(i), [h5_value(v, 60) for v in row]) for i, row in zip(rows_part, values)]
    elif names: # one axis of a compound type: a column per field
      fields = names[self.col0:self.col0 + H5_COLS]
      columns = list(fields)
      if len(names) > H5_COLS:
        status.append(f"fields {self.col0}–{self.col0 + len(fields) - 1} of {len(names)}, {{ }}: page")
      rows = [(str(i), [h5_value(v[f], 60) for f in fields]) for i, v in zip(rows_part, values)]
    else:
      columns = ["value"]
      rows = [(str(i), [h5_value(v, 200)]) for i, v in zip(rows_part, values)]
    self.show_table(rows, columns, " · ".join(status))

  @on(Input.Submitted, "#hslice")
  def selection_submitted(self, event):
    event.stop()
    if self.current:
      self.dataset_selections[self.current["path"]] = event.value
    self.row0 = self.col0 = 0
    self.show_data()
    self.query_one("#hdata").focus()

  def action_selection(self):
    inp = self.query_one("#hslice", Input)
    if inp.display:
      inp.focus()

  def action_page(self, rows, cols):
    if not self.current or self.current["kind"] != "dataset":
      return
    self.row0 = max(0, self.row0 + rows * H5_ROWS)
    self.col0 = max(0, self.col0 + cols * H5_COLS)
    try:
      ds = self.file[self.current["path"]]
      sel = parse_selection(self.query_one("#hslice", Input).value, ds.shape)
      _, shown = window(sel, ds.shape)
      if shown: # do not page past the end
        self.row0 = min(self.row0, max(0, (shown[0][1] - 1) // H5_ROWS * H5_ROWS))
        ncols = shown[1][1] if len(shown) == 2 else len(ds.dtype.names or ())
        self.col0 = min(self.col0, max(0, (ncols - 1) // H5_COLS * H5_COLS))
    except Exception:
      pass
    self.show_data()

  def action_back(self):
    if self.focused is self.query_one("#hslice"):
      self.query_one("#htree").focus()
    else:
      self.pop_back()


class GdbCommandScreen(ModalScreen[list[str] | None]):
  """Edit the command that will be run interactively in the terminal pane."""
  DEFAULT_CSS = """
  GdbCommandScreen { align: center middle; }
  #gdb-dialog { width: 100; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #gdb-title { text-style: bold; margin-bottom: 1; }
  #gdb-command { width: 1fr; }
  #gdb-help { height: auto; margin-top: 1; color: $text-muted; }
  #gdb-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #gdb-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, default_args):
    super().__init__()
    self.default_command = shlex.join(default_args)

  def compose(self) -> ComposeResult:
    with Vertical(id="gdb-dialog"):
      yield Static("Open core dump with GDB", id="gdb-title")
      yield Label("Command (editable)", id="gdb-command-label")
      yield Input(self.default_command, id="gdb-command")
      yield Static("Arguments are parsed as a command line; shell operators and expansion are not evaluated.", id="gdb-help")
      with Horizontal(id="gdb-buttons"):
        yield Button("Cancel", id="gdb-cancel")
        yield Button("Run in terminal", id="gdb-run", variant="primary")

  def on_mount(self):
    command = self.query_one("#gdb-command", Input)
    command.focus()
    command.cursor_position = len(command.value)

  @on(Button.Pressed, "#gdb-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  @on(Button.Pressed, "#gdb-run")
  @on(Input.Submitted, "#gdb-command")
  def run_pressed(self, event):
    event.stop()
    command = self.query_one("#gdb-command", Input).value
    try:
      args = shlex.split(command)
    except ValueError as e:
      self.notify(f"Invalid command: {e}", severity="error")
      return
    if not args:
      self.notify("Enter a command to run", severity="warning")
      return
    self.dismiss(args)

  def action_cancel(self):
    self.dismiss(None)


def inspect_core_file(path):
  """Read process notes from a core file through GDB without starting an interactive session."""
  gdb = shutil.which("gdb")
  if not gdb:
    return {}, "gdb was not found on PATH"
  try:
    result = subprocess.run([gdb, "--batch", "--nx", f"--core={path}", "-ex", "info proc"],
                            capture_output=True, text=True, timeout=20)
  except (OSError, subprocess.TimeoutExpired) as e:
    return {}, f"could not inspect core metadata: {e}"
  output = (result.stdout or "") + "\n" + (result.stderr or "")
  info = {}
  for line in output.splitlines():
    key, sep, value = line.partition("=")
    key = key.strip()
    if sep and key in ("exe", "cwd", "cmdline"):
      value = value.strip()
      try:
        parsed = shlex.split(value)
        value = parsed[0] if len(parsed) == 1 else value
      except ValueError:
        value = value.strip("\"'")
      info[key] = value
  error = ""
  if not info:
    error = (result.stderr or result.stdout or "GDB could not read process details").strip()[:240]
  return info, error


class CoreScreen(SplitScreen):
  """Overview for an ELF core dump, with an editable GDB command launcher."""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back")]

  def __init__(self, crumbs, path, bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.bases, self.env = list(bases), env
    self.process_info = {}
    self.metadata_error = "Reading process details with GDB …"

  def compose_left(self) -> ComposeResult:
    with VerticalScroll(id="core-overview"):
      yield Static(id="core-info")
      yield Button("Open with GDB", id="open-gdb", variant="primary")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.show_info()
    self.inspect_metadata()

  def show_info(self):
    try:
      stat = os.stat(self.path)
      size = humanize.naturalsize(stat.st_size)
      modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError as e:
      size, modified = "unavailable", str(e)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Type", "ELF core dump")
    grid.add_row("File", self.path)
    grid.add_row("Size", size)
    grid.add_row("Modified", modified)
    for key, label in (("exe", "Executable"), ("cmdline", "Command line"), ("cwd", "Working directory")):
      if self.process_info.get(key):
        grid.add_row(label, self.process_info[key])
    if self.metadata_error:
      grid.add_row("Process details", self.metadata_error)
    self.query_one("#core-info", Static).update(grid)

  @work(thread=True, exclusive=True, group="core-metadata")
  def inspect_metadata(self):
    info, error = inspect_core_file(self.path)
    self.app.call_from_thread(self.metadata_loaded, info, error)

  def metadata_loaded(self, info, error):
    if not self.is_mounted:
      return
    self.process_info, self.metadata_error = info, error
    self.show_info()

  def gdb_args(self):
    executable = self.process_info.get("exe")
    if executable and os.path.isfile(executable):
      return ["gdb", executable, self.path]
    return ["gdb", f"--core={self.path}"]

  @on(Button.Pressed, "#open-gdb")
  def open_gdb(self, event):
    event.stop()
    title = f"GDB · {os.path.basename(self.path)}"
    self.app.push_screen(GdbCommandScreen(self.gdb_args()),
                         lambda args: self.app.open_shell(args, title, focus=True) if args is not None else None)

  def action_back(self):
    self.pop_back()


def open_core_view(app, crumbs, path, bases=(), env=None):
  app.push_screen(CoreScreen(crumbs, path, bases=bases, env=env))


register_file_view("HDF5", is_hdf5, open_hdf5)
register_file_view("ELF core dump", is_core_dump, open_core_view)


class ConfirmScreen(ModalScreen[bool]):
  """Confirmation dialog for an action that changes a Slurm job"""
  DEFAULT_CSS = """
  ConfirmScreen { align: center middle; }
  #confirm-dialog { width: 72; max-width: 90%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #confirm-title { text-style: bold; margin-bottom: 1; }
  #confirm-message { height: auto; margin-bottom: 1; }
  #confirm-buttons { height: auto; align-horizontal: right; }
  #confirm-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "No")]

  def __init__(self, title, message, confirm_label, variant="error"):
    super().__init__()
    self.dialog_title, self.dialog_message = title, message
    self.confirm_label, self.variant = confirm_label, variant

  def compose(self) -> ComposeResult:
    with Vertical(id="confirm-dialog"):
      yield Static(self.dialog_title, id="confirm-title")
      yield Static(self.dialog_message, id="confirm-message", markup=False)
      with Horizontal(id="confirm-buttons"):
        yield Button("No", id="confirm-no")
        yield Button(self.confirm_label, id="confirm-yes", variant=self.variant)

  @on(Button.Pressed, "#confirm-no")
  def no(self, event):
    event.stop()
    self.dismiss(False)

  @on(Button.Pressed, "#confirm-yes")
  def yes(self, event):
    event.stop()
    self.dismiss(True)

  def action_cancel(self):
    self.dismiss(False)


class RestartScreen(ModalScreen[list[str] | None]):
  """Choose the command to submit when restarting a job"""
  DEFAULT_CSS = """
  RestartScreen { align: center middle; }
  #restart-dialog { width: 100; max-width: 95%; max-height: 90%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #restart-title { text-style: bold; margin-bottom: 1; }
  #restart-options { max-height: 10; }
  #restart-command-label { margin-top: 1; }
  #restart-command { width: 1fr; }
  #restart-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #restart-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, jid, default_args, retry_args=None, retry_label="Retry unfinished array tasks unavailable"):
    super().__init__()
    self.jid = jid
    self.default_args = list(default_args)
    self.retry_args = list(retry_args) if retry_args else None
    self.retry_label = retry_label
    self.default_command = shlex.join(self.default_args)
    self.retry_command = shlex.join(self.retry_args) if self.retry_args else self.default_command
    self.custom_command = self.default_command

  def compose(self) -> ComposeResult:
    with Vertical(id="restart-dialog"):
      yield Static(f"Restart job {self.jid}", id="restart-title")
      with RadioSet(id="restart-options"):
        yield RadioButton("Use the current restart command", value=True)
        yield RadioButton(self.retry_label, disabled=self.retry_args is None)
        yield RadioButton("Custom command")
      yield Label("Command to submit (editable in Custom mode)", id="restart-command-label")
      yield Input(self.default_command, id="restart-command", disabled=True)
      with Horizontal(id="restart-buttons"):
        yield Button("Cancel", id="restart-cancel")
        yield Button("Submit job", id="restart-submit", variant="primary")

  def on_mount(self):
    self.query_one("#restart-options", RadioSet).focus()

  @on(RadioSet.Changed, "#restart-options")
  def option_changed(self, event):
    command = self.query_one("#restart-command", Input)
    if event.index == 2:
      command.disabled = False
      command.value = self.custom_command
      command.focus()
    else:
      command.disabled = True
      command.value = self.default_command if event.index == 0 else self.retry_command

  @on(Input.Changed, "#restart-command")
  def custom_command_changed(self, event):
    if self.query_one("#restart-options", RadioSet).pressed_index == 2:
      self.custom_command = event.value

  @on(Button.Pressed, "#restart-cancel")
  def cancel(self, event):
    event.stop()
    self.dismiss(None)

  @on(Button.Pressed, "#restart-submit")
  def submit(self, event):
    event.stop()
    index = self.query_one("#restart-options", RadioSet).pressed_index
    if index == 0:
      args = self.default_args
    elif index == 1 and self.retry_args is not None:
      args = self.retry_args
    elif index == 2:
      try:
        args = shlex.split(self.query_one("#restart-command", Input).value)
      except ValueError as e:
        self.notify(f"Invalid command: {e}", severity="error")
        return
      if not args or os.path.basename(args[0]) != "sbatch":
        self.notify("The restart command must start with sbatch", severity="error")
        return
    else:
      self.notify("Select a restart command", severity="warning")
      return
    self.dismiss(list(args))

  def action_cancel(self):
    self.dismiss(None)


class JobScreen(SplitScreen):
  """Details, files and steps of one job, commands on its nodes"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape,backspace", "back", "Back"),
    Binding("r", "reload", "Reload"),
    Binding("e", "open('stderr')", "Error file"),
    Binding("o", "open('stdout')", "Output file"),
    Binding("s", "shell", "Shell on node"),
  ]

  def __init__(self, job):
    super().__init__()
    self.job = job
    self.details = {"files": [], "sstat": {}, "nodes": []}

  def compose_left(self):
    with Horizontal(id="job-actions"):
      yield Button("Cancel job", id="cancel-job", variant="error")
      yield Button("Restart job", id="restart-job", variant="primary")
    with VerticalScroll(id="details"):
      yield Static(id="info")
      with Horizontal(id="memory-controls"):
        yield Button("Query current memory", id="memory-query", variant="primary")
        yield Label("CPU:")
        yield Static(id="memory-cpu", markup=False)
        yield Label("GPU:")
        yield Static(id="memory-gpu", markup=False)
      with Vertical(id="job-progress"):
        yield ProgressBar(total=1, show_eta=False, id="progress-bar")
        yield Static(id="progress-line", markup=False)
      yield Label("Files", classes="section")
      yield OptionList(Option("loading ...", disabled=True), id="files")
      yield Label("Steps", classes="section")
      yield ClickTable(id="steps", cursor_type="row", zebra_stripes=True)
      with Vertical(id="nodes"):
        yield Label("Run on the nodes of the job", classes="section")
        with Horizontal(id="nodebar"):
          yield Input(id="cmd", placeholder="command, e.g. nvidia-smi")
          yield Select([], id="node", prompt="all nodes", allow_blank=True)
          yield Button("Run", id="run", variant="primary")
          yield Button("Shell", id="shell", tooltip="interactive shell on the node, opens on the right")
        with Horizontal(id="presets"):
          for i, name in enumerate(PRESETS):
            yield Button(name, id=f"preset{i}", classes="preset")

  def on_mount(self):
    self.sub_title = f"Jobs › {self.job.jid}"
    table = self.query_one("#steps", ClickTable)
    for key, label in [("step", "Step"), ("name", "Name"), ("state", "State"), ("exit", "Exit"), ("elapsed", "Elapsed"),
                       ("tasks", "Tasks"), ("nodes", "Nodes"), ("maxrss", "MaxRSS"), ("total", "~Total RSS"), ("cmd", "Command")]:
      table.add_column(label, key=key)
    self.show_job()
    self.query_one("#files").focus()
    self.load()

  def show_job(self):
    job, rec = self.job, self.job.rec
    self.query_one("#cancel-job", Button).disabled = not (job.queued or job.running)
    self.query_one("#restart-job", Button).disabled = restart_args(job) is None
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    state = state_text(job.state)
    if job.reason and job.reason not in ("None", ""):
      state.append(f"  ({job.reason})", style="dim")
    elif not job.queued and rec["ExitCode"]:
      state.append(f"  exit code {rec['ExitCode']}")
    rows = [
      ("Job", f"{job.jid}  {rec['JobName']}"),
      ("State", state),
      ("User / account", f"{rec['User']} / {rec['Account']}   partition {rec['Partition']}"),
      ("Nodes", f"{rec['NNodes']}  {rec['NodeList'] if rec['NodeList'] not in ('None assigned', '') else ''}"),
      ("Tasks", str(rec["NTasks"] or max((int(s["NTasks"] or 0) for s in job.steps), default=0) or "-")),
      ("Submit", rec["Submit"].replace("T", " ")),
      ("Start", ("~ " if job.state == "PENDING" else "") + rec["Start"].replace("T", " ") if is_time(rec["Start"]) else "-"),
      ("End", rec["End"].replace("T", " ") if is_time(rec["End"]) else "-"),
      ("Elapsed / limit", f"{rec['Elapsed']} / {rec['Timelimit']}"),
      ("Node hours", f"{job.node_hours():.2f}"),
      ("WorkDir", rec["WorkDir"]),
      ("Submitted with", rec["SubmitLine"]),
      ("Command", job.command),
    ]
    state_name = job.state.split()[0].rstrip("+") if job.state else ""
    if not job.queued and state_name in (FAILED_JOB_STATES | {"COMPLETED"}):
      peak_cpu, peak_gpu = peak_job_memory(job)
      rows.append(("Peak CPU memory (Slurm DB)", humanize.naturalsize(peak_cpu) if peak_cpu else "not reported"))
      if peak_gpu:
        rows.append(("Peak GPU memory (Slurm DB)", humanize.naturalsize(peak_gpu)))
    for key, val in rows:
      grid.add_row(key, val)
    self.query_one("#info", Static).update(grid)
    self.query_one("#memory-controls", Horizontal).display = job.running
    self.query_one("#memory-query", Button).disabled = not job.running
    progress = progress_for_job(job)
    progress_view = self.query_one("#job-progress", Vertical)
    if progress is not None:
      progress_view.display = True
      self.query_one("#progress-bar", ProgressBar).update(total=max(progress.total, 1), progress=progress.current)
      self.query_one("#progress-line", Static).update(progress.line)
    else:
      progress_view.display = False
    self.query_one("#nodes").display = job.running
    self.show_steps()

  def show_steps(self):
    table = self.query_one("#steps", ClickTable)
    table.clear()
    sstat = self.details["sstat"]
    for s in self.job.steps:
      step = s["JobID"].partition(".")[2]
      live = sstat.get(step, {})
      maxrss = sl.sacct_bytes(live.get("MaxRSS") or s["MaxRSS"])
      ntasks = int(s["NTasks"] or 0)
      total = sl.sacct_bytes(live.get("AveRSS") or s["AveRSS"]) * ntasks
      table.add_row(step, s["JobName"], state_text(s["State"]), s["ExitCode"], s["Elapsed"], s["NTasks"], s["NNodes"],
                    humanize.naturalsize(maxrss) if maxrss else "", humanize.naturalsize(total) if total else "",
                    s["SubmitLine"], key=s["JobID"])
    if not self.job.steps:
      table.add_row("", "no steps", *[""]*8)

  def show_files(self):
    files = self.query_one("#files", OptionList)
    files.clear_options()
    for i, e in enumerate(self.details["files"]):
      prompt = Text()
      prompt.append(f"{e.kind:<10} ", style="bold")
      openable = (e.path and os.path.exists(e.path)) or e.text is not None or e.kind == "sacct"
      prompt.append(e.label, style="" if openable else "dim")
      if e.note:
        prompt.append(f"  {e.note}", style="dim")
      if e.kind == "stderr" and e.path and self.job.err[0] == "file" and self.job.err[1]:
        prompt.append("  !", style="bold red")
      files.add_option(Option(prompt, id=str(i), disabled=not openable))
    if self.details["nodes"]:
      self.query_one("#node", Select).set_options([(n, n) for n in self.details["nodes"]])

  @work(thread=True, exclusive=True, group="details")
  def load(self, refresh_job=False):
    job = self.job
    if refresh_job:
      jobs = annotate(load_jobs(None, False, jids=[job.jid]))
      job = next((j for j in jobs if j.jid == job.jid), job)
    details = load_details(job)
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.loaded, job, details)

  def loaded(self, job, details):
    self.job, self.details = job, details
    self.job.progress = details.get("progress")
    self.show_job()
    self.show_files()
    self.app.show_warnings()

  @on(Button.Pressed, "#memory-query")
  def memory_query_pressed(self, event):
    event.stop()
    if not self.job.running:
      return
    self.query_one("#memory-query", Button).disabled = True
    self.query_one("#memory-cpu", Static).update("querying …")
    self.query_one("#memory-gpu", Static).update("querying …")
    self.query_current_memory()

  @work(thread=True, exclusive=True, group="current-memory")
  def query_current_memory(self):
    try:
      result = current_memory_usage(self.job)
    except Exception as e:
      result = {"error": str(e)}
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.memory_loaded, result)

  def memory_loaded(self, result):
    if not self.is_mounted:
      return
    if "error" in result:
      self.query_one("#memory-cpu", Static).update(f"unavailable ({result['error']})")
      self.query_one("#memory-gpu", Static).update("unavailable")
      self.query_one("#memory-query", Button).disabled = not self.job.running
      self.app.show_warnings()
      return
    self.details["sstat"] = result["sstat"]
    cpu = result["cpu_bytes"]
    cpu_text = f"{humanize.naturalsize(cpu)} RSS" if cpu is not None else "unavailable"
    if cpu is not None and result["cpu_source"] != "ps":
      cpu_text += f" ({result['cpu_source']})"
    if cpu is not None and result["cpu_partial"]:
      cpu_text += " (partial)"
    gpu = result["gpu_bytes"]
    if gpu is None:
      errors = result["gpu_errors"]
      gpu_text = f"unavailable ({errors[0]})" if errors else "unavailable"
    else:
      gpu_text = humanize.naturalsize(gpu)
      if result["gpu_partial"]:
        gpu_text += " (partial)"
    self.query_one("#memory-cpu", Static).update(cpu_text)
    self.query_one("#memory-gpu", Static).update(gpu_text)
    self.query_one("#memory-query", Button).disabled = not self.job.running
    self.show_steps()
    self.app.show_warnings()

  def action_back(self):
    self.pop_back()

  @on(Button.Pressed, "#cancel-job")
  def confirm_cancel(self, event):
    event.stop()
    jid = self.job.rec["JobIDRaw"]
    self.app.push_screen(
      ConfirmScreen("Cancel job?", f"Cancel Slurm job {jid}? This cannot be undone.", "Cancel job"),
      lambda confirmed: self.run_job_action("cancel", ["scancel", str(jid)]) if confirmed else None,
    )

  @on(Button.Pressed, "#restart-job")
  def confirm_restart(self, event):
    event.stop()
    args = restart_args(self.job)
    if args is None:
      self.notify("The original sbatch command is not available for this job", severity="warning")
      return
    self.prepare_restart_options(args)

  @work(thread=True, exclusive=True, group="restart-options")
  def prepare_restart_options(self, default_args):
    task_id = array_task_id(self.job)
    retry_args, retry_label = None, "Retry array IDs not running/completed (not an array task)"
    if task_id is not None:
      parent_id = self.job.jid.partition("_")[0]
      try:
        spec = original_array_spec(self.job)
        if not spec:
          raise ValueError("the original array range is unavailable")
        all_ids = expand_array_spec(spec)
        states = array_task_states(parent_id)
        retry_ids = array_ids_not_running_or_completed(all_ids, states)
        if retry_ids:
          retry_spec = compress_array_ids(retry_ids)
          retry_args = with_array_spec(default_args, retry_spec)
          retry_label = f"Retry IDs not running/completed (--array={retry_spec})"
        else:
          retry_label = "Retry IDs not running/completed (none found)"
      except Exception as e:
        retry_label = f"Retry IDs not running/completed (unavailable: {str(e)[:80]})"
    self.app.call_from_thread(self.show_restart_options, default_args, retry_args, retry_label)

  def show_restart_options(self, default_args, retry_args, retry_label):
    if not self.is_mounted:
      return
    cwd = self.job.rec.get("WorkDir")
    self.app.push_screen(
      RestartScreen(self.job.jid, default_args, retry_args, retry_label),
      lambda args: self.run_job_action("restart", args, cwd=cwd) if args is not None else None,
    )

  @work(thread=True, exclusive=True, group="job-action")
  def run_job_action(self, action, args, cwd=None):
    cwd = cwd if cwd and cwd not in ("(null)", "None") else None
    try:
      result = subprocess.run(args, cwd=cwd or None, capture_output=True, text=True, timeout=60,
                              env=os.environ | {"SLURM_TIME_FORMAT": "standard"})
      self.app.call_from_thread(self.job_action_finished, action, result.returncode, result.stdout, result.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
      self.app.call_from_thread(self.job_action_finished, action, -1, "", str(e))

  def job_action_finished(self, action, returncode, stdout, stderr):
    output = (stdout or stderr).strip()
    if returncode == 0:
      if action == "cancel":
        self.notify(f"Cancellation requested for job {self.job.jid}")
        if self.is_mounted:
          self.load(refresh_job=True)
      else:
        self.notify(f"Restarted {self.job.jid}: {output or 'new job submitted'}")
    else:
      self.notify(f"Could not {action} job {self.job.jid}: {output or 'Slurm command failed'}", severity="error")

  def check_action(self, action, parameters):
    if action == "shell":
      return self.job.running
    return True

  @on(Button.Pressed, "#shell")
  def action_shell(self):
    if not self.job.running:
      self.notify("the job is not running", severity="error")
      return
    node = self.query_one("#node", Select).value
    node = (self.details["nodes"] or [None])[0] if node is Select.NULL else node
    if not node:
      self.notify("the nodes of the job are not known yet, try again in a moment", severity="warning")
      return
    args = ["srun", f"--jobid={self.job.rec['JobIDRaw']}", "--overlap", "--pty", "--nodes=1", "--ntasks=1", "-w", node,
            f"--job-name={STEP_NAME}", "bash", "-l"]
    self.app.open_shell(args, f"shell on {node} · job {self.job.jid}")

  def action_reload(self):
    self.load(refresh_job=True)

  def action_open(self, kind):
    for e in self.details["files"]:
      if e.kind == kind and e.path:
        self.open_entry(e)
        return
    self.notify(f"no {kind} file", severity="warning")

  def context(self):
    """Base directories and variables to resolve paths in texts of this job"""
    return [self.job.rec["WorkDir"]], job_vars(self.job)

  def open_entry(self, e):
    bases, env = self.context()
    crumbs = ["Jobs", self.job.jid]
    if e.kind == "sacct":
      text = sacct_all(self.job)
      self.app.push_screen(FileScreen([*crumbs, e.label], text=text, refresher=lambda: sacct_all(self.job), bases=bases, env=env))
    elif e.path:
      open_path(self.app, crumbs, e.path, bases, env, follow=self.job.running and e.kind in ("stdout", "stderr"))
    elif e.text is not None:
      self.app.push_screen(FileScreen([*crumbs, e.label], text=e.text, bases=bases, env=env))

  @on(OptionList.OptionSelected, "#files")
  def file_selected(self, event):
    self.open_entry(self.details["files"][int(event.option_id)])

  @on(DataTable.RowSelected, "#steps")
  def step_selected(self, event):
    event.stop()
    self.query_one("#steps", ClickTable).take_clicked_column()
    step = next((s for s in self.job.steps if s["JobID"] == event.row_key.value), None)
    if step:
      width = max(map(len, step))
      text = "\n".join(f"{k:<{width}}  {v}" for k, v in step.items() if v) + "\n"
      bases, env = self.context()
      self.app.push_screen(FileScreen(["Jobs", self.job.jid, f"step {step['JobID']}"], text=text, bases=bases, env=env))

  @on(Button.Pressed, "#run")
  @on(Input.Submitted, "#cmd")
  def run_pressed(self, event):
    self.run_command(self.query_one("#cmd", Input).value.strip())

  @on(Button.Pressed, ".preset")
  def preset_pressed(self, event):
    cmd = list(PRESETS.values())[int(event.button.id.removeprefix("preset"))]
    self.query_one("#cmd", Input).value = cmd
    self.run_command(cmd)

  def run_command(self, cmd):
    if not cmd:
      self.notify("enter a command first", severity="warning")
      return
    if not self.job.running:
      self.notify("the job is not running", severity="error")
      return
    node = self.query_one("#node", Select).value
    node = None if node is Select.NULL else node
    self.notify(f"running '{cmd}' on {node or 'all nodes'} ...")
    self.run_worker(lambda: self.command_done(cmd, node, run_on_nodes(self.job, cmd, node)), thread=True, group="nodes")

  def command_done(self, cmd, node, text):
    job = self.job
    crumbs = ["Jobs", job.jid, f"{cmd} @ {node or 'all nodes'}"]
    bases, env = self.context()
    self.app.call_from_thread(self.app.push_screen, FileScreen(crumbs, text=text, refresher=lambda: run_on_nodes(job, cmd, node),
                                                               bases=bases, env=env))

# ---------------------------------------------------------------------------
# terminal window: a command (srun --pty bash) in a pseudo terminal, emulated with pyte

# make the pseudo terminal the controlling terminal of the command (for Ctrl-C, job control), then run it
CTTY = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
ALT_SCREEN_MODES = (47, 1047, 1049)
DECCKM = 1 << 5 # private mode 1 (application cursor keys), pyte stores private modes shifted by 5 bits

TERM_KEYS = {
  "enter": "\r", "tab": "\t", "shift+tab": "\x1b[Z", "backspace": "\x7f", "ctrl+h": "\x08", "escape": "\x1b",
  "insert": "\x1b[2~", "delete": "\x1b[3~", "pageup": "\x1b[5~", "pagedown": "\x1b[6~", "home": "\x1b[H", "end": "\x1b[F",
  "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS", "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~",
  "f8": "\x1b[19~", "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}
TERM_ARROWS = {"up": "A", "down": "B", "right": "C", "left": "D"}

def vt_color(name):
  """pyte color (name or rrggbb) -> rich color"""
  if not name or name == "default":
    return None
  if re.fullmatch(r'[0-9a-fA-F]{6}', name):
    return Color.parse("#" + name)
  bright = name.startswith("bright")
  base = {"brown": "yellow"}.get(name[6:] if bright else name, name[6:] if bright else name)
  try:
    return Color.parse(("bright_" if bright else "") + base)
  except Exception:
    return None

@functools.lru_cache(maxsize=4096)
def vt_style(fg, bg, bold, italics, underscore, strikethrough, reverse):
  return Style(color=vt_color(fg), bgcolor=vt_color(bg), bold=bold, italic=italics, underline=underscore,
               strike=strikethrough, reverse=reverse)

def make_vt_screen(cols, rows, reply):
  """pyte screen that answers terminal queries through reply and supports the alternate screen of vim, less, htop"""
  import pyte

  class VTScreen(pyte.Screen):
    saved = None

    def write_process_input(self, data):
      reply(data.encode())

    def set_mode(self, *modes, **kwargs):
      if kwargs.get("private") and any(m in ALT_SCREEN_MODES for m in modes):
        self.saved = ({y: dict(line) for y, line in self.buffer.items()}, (self.cursor.x, self.cursor.y))
        self.erase_in_display(2)
        modes = tuple(m for m in modes if m not in ALT_SCREEN_MODES)
        if not modes:
          return
      super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
      if kwargs.get("private") and any(m in ALT_SCREEN_MODES for m in modes):
        if self.saved:
          buffer, (x, y) = self.saved
          self.saved = None
          self.buffer.clear()
          for row, line in buffer.items():
            if row < self.lines:
              self.buffer[row].update(line)
          self.cursor.x, self.cursor.y = min(x, self.columns - 1), min(y, self.lines - 1)
          self.dirty.update(range(self.lines))
        modes = tuple(m for m in modes if m not in ALT_SCREEN_MODES)
        if not modes:
          return
      super().reset_mode(*modes, **kwargs)

  return VTScreen(cols, rows), pyte

SWITCH_KEY = "ctrl+right_square_bracket" # Ctrl-]: the one key that does not go to the shell

class TerminalSession:
  """A command (srun --pty bash) in a pseudo terminal, emulated with pyte. It belongs to the app and is shown in
  the terminal pane of every window, so it keeps running while you navigate on the left."""
  def __init__(self, app, args, title):
    self.app, self.args, self.title = app, args, title
    self.vt = self.stream = self.proc = self.fd = None
    self.size = None
    self.done, self.code, self.error = False, None, ""

  def ensure(self, cols, rows):
    """Start with, or resize to, the size of the pane"""
    cols, rows = max(cols, 2), max(rows, 2)
    if self.vt is None and not self.done:
      self.start(cols, rows)
    elif self.vt is not None and (cols, rows) != self.size:
      self.size = (cols, rows)
      self.vt.resize(rows, cols)
      self.set_winsize(cols, rows)

  def start(self, cols, rows):
    self.size = (cols, rows)
    try:
      self.vt, pyte = make_vt_screen(cols, rows, self.write)
    except ImportError:
      self.done, self.code, self.error = True, -1, "the shell needs the python package pyte: pip install pyte"
      self.app.shell_changed()
      return
    self.stream = pyte.ByteStream(self.vt)
    master, slave = os.openpty()
    self.fd = master
    self.set_winsize(cols, rows)
    log.debug(f"running command: {shlex.join(self.args)}")
    try:
      self.proc = subprocess.Popen([sys.executable, "-c", CTTY, *self.args], stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True, env=os.environ | {"TERM": "xterm-256color"}, close_fds=True)
    except OSError as e:
      os.close(slave)
      self.stream.feed(f"cannot start {self.args[0]}: {e}\r\n".encode())
      self.finish()
      return
    os.close(slave)
    os.set_blocking(master, False)
    asyncio.get_running_loop().add_reader(master, self.read_output)
    self.app.shell_changed()

  def set_winsize(self, cols, rows):
    if self.fd is not None:
      try:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
      except OSError:
        pass

  def read_output(self):
    try:
      data = os.read(self.fd, 65536)
    except BlockingIOError:
      return
    except OSError: # EIO: the command closed the terminal
      data = b""
    if not data:
      self.finish()
      return
    self.stream.feed(data)
    self.app.refresh_terminal()

  def close_fd(self):
    if self.fd is not None:
      try:
        asyncio.get_running_loop().remove_reader(self.fd)
      except Exception:
        pass
      os.close(self.fd)
      self.fd = None

  def finish(self):
    if self.done:
      return
    self.done = True
    self.close_fd()
    self.code = -1
    if self.proc:
      try:
        self.code = self.proc.wait(timeout=3)
      except subprocess.TimeoutExpired:
        self.proc.kill()
        self.code = self.proc.wait()
    if self.proc and self.app.shell is self:
      self.app.close_shell()
    else:
      self.app.shell_changed()

  def terminate(self):
    """End the command (the pane is closed or the app quits)"""
    self.close_fd()
    if self.proc and self.proc.poll() is None:
      self.proc.terminate()
      try:
        self.proc.wait(timeout=3)
      except subprocess.TimeoutExpired:
        self.proc.kill()
    self.done = True

  def write(self, data):
    if self.fd is not None and not self.done:
      try:
        os.write(self.fd, data)
      except OSError:
        pass

class TerminalView(Widget, can_focus=True):
  """Shows the shell of the app; while it has the focus all keys except Ctrl-] go to the shell"""
  DEFAULT_CSS = "TerminalView { height: 1fr; }"

  @property
  def session(self):
    return self.app.shell

  def attach(self):
    """The pane was shown or got a new session: start/resize the session with the size of this view"""
    if self.session and self.size.width > 0 and self.size.height > 0:
      self.session.ensure(self.size.width, self.size.height)
    self.refresh()

  def on_resize(self, event):
    if self.session and self.display:
      self.session.ensure(event.size.width, event.size.height)
    self.refresh()

  def on_focus(self):
    self.refresh()

  def on_blur(self):
    self.refresh()

  async def on_key(self, event):
    event.stop()
    event.prevent_default()
    key = event.key
    if key == "ctrl+x":
      self.app.close_shell()
      return
    if key == SWITCH_KEY:
      self.app.action_switch_side()
      return
    s = self.session
    if s is None or s.vt is None and not s.done:
      return
    if s.done:
      if key == "enter":
        self.app.restart_shell()
      return
    if key in TERM_ARROWS:
      data = ("\x1bO" if DECCKM in s.vt.mode else "\x1b[") + TERM_ARROWS[key]
    elif key.startswith(("ctrl+", "shift+", "alt+")) and key.split("+")[-1] in TERM_ARROWS:
      mod = {"shift": 2, "alt": 3, "ctrl": 5}[key.split("+")[0]]
      data = f"\x1b[1;{mod}{TERM_ARROWS[key.split('+')[-1]]}"
    elif key in TERM_KEYS:
      data = TERM_KEYS[key]
    elif event.character:
      data = event.character
    elif key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
      data = chr(ord(key[5]) & 0x1f)
    elif key.startswith("alt+") and len(key) == 5:
      data = "\x1b" + key[4]
    else:
      return
    s.write(data.encode())

  def on_paste(self, event):
    s = self.session
    if s is None or s.vt is None:
      return
    text = event.text.replace("\r\n", "\r").replace("\n", "\r")
    if (2004 << 5) in s.vt.mode: # bracketed paste
      text = f"\x1b[200~{text}\x1b[201~"
    s.write(text.encode())

  def render_line(self, y):
    width = self.size.width
    s = self.session
    vt = s.vt if s else None
    base = self.rich_style
    if vt is None or y >= vt.lines:
      if s and s.error and y == 0:
        return Strip([Segment(s.error, base + Style(color="red"))]).adjust_cell_length(width, base)
      return Strip.blank(width, base)
    line = vt.buffer[y]
    cursor_x = vt.cursor.x if (y == vt.cursor.y and self.has_focus and not vt.cursor.hidden and not s.done) else -1
    segments, text, style = [], "", None
    for x in range(min(vt.columns, width)):
      ch = line[x]
      if ch.data == "": # second cell of a wide character
        continue
      st = base + vt_style(ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.strikethrough, ch.reverse != (x == cursor_x))
      if st != style:
        if text:
          segments.append(Segment(text, style))
        text, style = "", st
      text += ch.data
    if text:
      segments.append(Segment(text, style))
    return Strip(segments).adjust_cell_length(width, base)

class TerminalPane(Vertical):
  """Right side of every window: title bar with a close button and the shell"""
  def compose(self) -> ComposeResult:
    with Horizontal(classes="tbar"):
      yield Static(classes="ttitle")
      yield Button("✕", classes="tclose", compact=True, tooltip="close the shell (Ctrl-X)")
    yield TerminalView()

  def on_mount(self):
    self.watch(self.app, "shell_version", self.sync)
    self.watch(self.app, "pane_width", self.set_width)

  def sync(self, _=None):
    s = self.app.shell
    self.display = s is not None
    for splitter in self.parent.query(Splitter):
      splitter.display = s is not None
    if s is None:
      return
    if s.done:
      status = f"ended (exit code {s.code}) · Enter: new shell · ✕ or Ctrl-X: close"
    else:
      status = "Ctrl-] switches between the sides"
    self.query_one(".ttitle", Static).update(f"{s.title} · {status}")
    self.call_after_refresh(self.query_one(TerminalView).attach)

  def set_width(self, width):
    self.styles.width = width if width else "50%"

  @on(Button.Pressed, ".tclose")
  def close_pressed(self, event):
    event.stop()
    self.app.close_shell()

class Splitter(Widget):
  """Vertical bar between the window and the terminal pane, drag it with the mouse to resize"""
  def __init__(self):
    super().__init__()
    self.dragging = False

  def render(self):
    return ""

  def on_mouse_down(self, event):
    self.dragging = True
    self.capture_mouse()
    self.add_class("-dragging")
    event.stop()

  def on_mouse_move(self, event):
    if self.dragging:
      total = self.screen.size.width
      self.app.pane_width = max(20, min(total - event.screen_x - 1, total - 20))

  def on_mouse_up(self, event):
    if self.dragging:
      self.dragging = False
      self.release_mouse()
      self.remove_class("-dragging")

COLUMNS = [("jobid", "Job ID"), ("name", "Name"), ("user", "User"), ("state", "State"), ("progress", "Progress"),
           ("nodes", "Nodes"), ("time", "Elapsed / Limit"),
           ("start", "Start"), ("end", "End"), ("err", "Err"), ("cmd", "Command")]

SORT_KEYS = {
  "jobid": lambda j: job_id_key(j.jid),
  "name":  lambda j: j.rec["JobName"],
  "user":  lambda j: j.rec["User"].casefold(),
  "state": lambda j: j.state,
  "progress": lambda j: (p.current / p.total if (p := progress_for_job(j)) and p.total else 0),
  "nodes": lambda j: int(j.rec["NNodes"] or 0),
  "time":  lambda j: j.node_hours() / max(int(j.rec["NNodes"] or 1), 1),
  "start": lambda j: j.rec["Start"] if is_time(j.rec["Start"]) else "",
  "end":   lambda j: j.rec["End"] if is_time(j.rec["End"]) else "",
  "err":   lambda j: (j.err[0] == "file", j.err[1]),
  "cmd":   lambda j: j.command,
}

class MainScreen(SplitScreen):
  """The job table"""
  BINDINGS = [
    Binding("slash", "filter", "Filter jobs"),
    Binding("escape", "clear_filter", "Clear filter", show=False),
  ]

  def compose_left(self):
    with Horizontal(id="toolbar"):
      yield Button("Reload", id="reload", variant="primary")
      yield Label("finished since")
      yield Input("1d", id="since", placeholder="off")
      with Horizontal(id="accounts-toggle"):
        yield Label("all my accounts")
        yield Switch(id="us")
    yield Static(id="status")
    yield ClickTable(id="jobs", cursor_type="row", zebra_stripes=True)
    yield Input(id="jfilter", placeholder="filter jobs by name, command, ID, state, user, account, path…")

  def action_filter(self):
    search = self.query_one("#jfilter", Input)
    search.add_class("visible")
    search.value = self.app.job_filter
    search.focus()

  def action_clear_filter(self):
    search = self.query_one("#jfilter", Input)
    if search.has_class("visible"):
      search.value = ""
      search.remove_class("visible")
      self.app.job_filter = ""
      self.app.populate()
      self.query_one("#jobs", ClickTable).focus()

  def on_mount(self):
    self.app.setup_job_columns()
    table = self.query_one("#jobs", ClickTable)
    table.focus()
    self.app.action_reload()

class SlurmTUI(App):
  TITLE = "slurm_tui"
  CSS = """
  #toolbar { height: auto; padding: 0 1; }
  #toolbar > * { margin-right: 1; }
  #toolbar Label { padding-top: 1; }
  #accounts-toggle { width: auto; height: auto; }
  #accounts-toggle Label { width: auto; height: auto; content-align: center middle; margin-right: 1; padding: 0; }
  #us { width: auto; height: auto; padding: 0; border: none; }
  #since { width: 12; }
  #topbar { height: 1; }
  #topbar Header { dock: none; width: 1fr; height: 1; }
  #back { width: 3; min-width: 3; height: 1; padding: 0; }
  #status { height: 1; padding: 0 1; background: $panel; }
  #jobs { height: 1fr; }
  #details { height: 1fr; padding: 0 1; }
  #memory-controls { height: auto; margin: 1 0; }
  #memory-controls > * { margin-right: 1; }
  #memory-controls Label { width: auto; height: 1; content-align: center middle; padding: 0; }
  #memory-cpu, #memory-gpu { width: auto; height: 1; }
  #job-progress { height: auto; margin-bottom: 1; padding: 0 1; }
  #progress-bar { width: 40; }
  #progress-line { height: auto; }
  #core-overview { height: 1fr; padding: 1; }
  #core-info { height: auto; }
  #open-gdb { margin-top: 1; }
  #job-actions { height: auto; margin: 1 0; }
  #job-actions Button { margin-right: 1; }
  .section { margin-top: 1; text-style: bold; color: $accent; }
  #files { height: auto; max-height: 16; }
  #steps { height: auto; max-height: 14; }
  #nodes { height: auto; }
  #nodebar, #presets { height: auto; }
  #cmd { width: 1fr; }
  #node { width: 28; }
  #presets Button { margin-right: 1; min-width: 8; }
  #fstatus { height: 1; padding: 0 1; background: $panel; }
  #text { height: 1fr; }
  #search, #dfilter, #jfilter { dock: bottom; display: none; }
  #search.visible, #dfilter.visible, #jfilter.visible { display: block; }
  #dstatus { height: 1; padding: 0 1; background: $panel; }
  #dir { height: 1fr; }
  #hstatus, #hpage { height: 1; padding: 0 1; background: $panel; }
  #hbody { height: 1fr; }
  #htree { width: 40%; height: 1fr; border-right: vkey $panel-lighten-2; }
  #hright { width: 1fr; height: 1fr; }
  #hinfo { height: auto; max-height: 45%; padding: 0 1; }
  #hdata { height: 1fr; }
  .split { height: 1fr; }
  .left { width: 1fr; height: 1fr; }
  TerminalPane { width: 50%; height: 1fr; display: none; }
  Splitter { width: 1; height: 1fr; display: none; background: $panel-lighten-2; }
  Splitter:hover, Splitter.-dragging { background: $accent; }
  .tbar { height: 1; background: $panel; }
  TerminalPane:focus-within .tbar { background: $accent; color: $text; }
  .ttitle { width: 1fr; height: 1; padding: 0 1; }
  .tclose { width: 3; min-width: 3; height: 1; }
  """
  BINDINGS = [
    Binding("q", "quit", "Quit"),
    Binding("r", "reload", "Reload"),
    Binding("e", "open('stderr')", "Error file"),
    Binding("o", "open('stdout')", "Output file"),
    Binding(SWITCH_KEY, "switch_side", "Shell ⇄", key_display="^]"),
    Binding("ctrl+x", "close_shell", "Close shell"),
  ]
  shell_version = reactive(0) # bumped when the shell is opened, closed or ends: the panes update
  pane_width = reactive(0)    # width of the terminal pane in columns, 0: half of the window

  def __init__(self):
    super().__init__()
    self.shell = None # TerminalSession shown on the right of every window
    self.jobs = {}
    self.sort_column, self.sort_reverse = "jobid", True
    self.since = None
    self.job_filter = ""
    self.job_column_keys = None

  def get_default_screen(self):
    return MainScreen()

  def setup_job_columns(self, show_progress=None):
    table = self.query_one("#jobs", ClickTable)
    all_accounts = self.query_one("#us", Switch).value
    if show_progress is None:
      show_progress = any(progress_for_job(job) is not None for job in self.jobs.values())
    visible_columns = [(key, label) for key, label in COLUMNS
                       if (key != "user" or all_accounts) and (key != "progress" or show_progress)]
    column_keys = [key for key, _ in visible_columns]
    if column_keys == self.job_column_keys:
      return
    if not all_accounts and self.sort_column == "user":
      self.sort_column, self.sort_reverse = "jobid", True
    if not show_progress and self.sort_column == "progress":
      self.sort_column, self.sort_reverse = "jobid", True
    table.clear(columns=True)
    for key, label in visible_columns:
      if key == "progress":
        table.add_column(label, key=key, width=10)
      else:
        table.add_column(label, key=key)
    self.job_column_keys = column_keys

  # ---- the shell on the right
  def shell_changed(self):
    self.shell_version += 1

  def refresh_terminal(self):
    for view in self.screen.query(TerminalView):
      view.refresh()

  def open_shell(self, args, title, focus=False):
    if self.shell and not self.shell.done:
      self.notify("a shell is already open on the right, close it first (✕)", severity="warning")
      self.focus_terminal()
      return
    if self.shell:
      self.shell.terminate()
    if not focus and not isinstance(self.focused, TerminalView):
      self.screen.left_focus = self.focused
    self.shell = TerminalSession(self, args, title)
    self.shell_changed()
    self.call_after_refresh(self.focus_terminal if focus else self.focus_left)

  def restart_shell(self):
    if self.shell:
      args, title = self.shell.args, self.shell.title
      self.shell.terminate()
      self.shell = None
      self.open_shell(args, title, focus=True)

  def action_close_shell(self):
    self.close_shell()

  def close_shell(self):
    if self.shell:
      self.shell.terminate()
      self.shell = None
      self.shell_changed()
      self.call_after_refresh(self.focus_left)

  def focus_terminal(self):
    views = self.screen.query(TerminalView)
    if views and self.shell:
      if not isinstance(self.focused, TerminalView):
        self.screen.left_focus = self.focused
      views.first().focus()

  def focus_left(self):
    widget = getattr(self.screen, "left_focus", None)
    if widget is None or not widget.is_attached or widget.screen is not self.screen or not widget.focusable:
      chain = [w for w in self.screen.focus_chain if not w.has_class("tclose") and not isinstance(w, TerminalView)]
      widget = chain[0] if chain else None
    if widget is not None:
      widget.focus()

  def action_switch_side(self):
    if isinstance(self.focused, TerminalView):
      self.focus_left()
    else:
      self.focus_terminal()

  async def action_quit(self):
    # Stop mouse and focus reports right away, and clear the screen while still on the alternate screen:
    # terminals that switch back to the previous content are not affected, in terminals (or GNU screen / tmux
    # setups) without an alternate screen the last frame of the app would otherwise stay on the screen.
    if self._driver is not None:
      self._driver.write(REPORTS_OFF + "\x1b[0m\x1b[2J\x1b[H")
      self._driver.flush()
    self.exit()

  async def on_event(self, event):
    if isinstance(event, events.Key) and event is not getattr(self, "_logged_key", None):
      self._logged_key = event # the app sees a key twice: on its way to the focused widget and bubbling back
      # with --log: when do keys arrive (e.g. a lone Esc held back by the terminal until the next key)
      log.debug(f"key {event.key!r} in {type(self.screen).__name__} ({len(self.screen_stack)} windows open)")
    await super().on_event(event)

  def check_action(self, action, parameters):
    if action in ("open", "reload"): # keys of the job table, not of the other windows
      return self.screen is self.screen_stack[0]
    if action in ("switch_side", "close_shell"):
      return self.shell is not None
    if action in ("quit", "command_palette", "help_quit") and isinstance(self.focused, TerminalView):
      return False # Ctrl-Q, Ctrl-P, Ctrl-C go to the shell
    return True

  def show_warnings(self):
    for msg in dict.fromkeys(collector.drain()):
      self.notify(msg, severity="warning", timeout=8)

  def action_reload(self):
    value = self.query_one("#since", Input).value.strip()
    if value.lower() in ("", "0", "off", "no", "none"):
      since = None
    else:
      try:
        since = sl.parse_duration(value)
      except sl.argparse.ArgumentTypeError as e:
        self.notify(str(e), severity="error")
        return
    self.since = since
    self.query_one("#jobs", ClickTable).loading = True
    self.load(since, self.query_one("#us", Switch).value)

  @work(thread=True, exclusive=True, group="jobs")
  def load(self, since, us):
    jobs = annotate(load_jobs(since, us))
    if not get_current_worker().is_cancelled:
      self.call_from_thread(self.populate, jobs)

  def populate(self, jobs=None):
    table = self.query_one("#jobs", ClickTable)
    current = None
    if table.row_count:
      try:
        current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
      except Exception:
        pass
    if jobs is not None:
      self.jobs = {job.jid: job for job in jobs}
    all_jobs = list(self.jobs.values())
    self.setup_job_columns()
    pattern = self.job_filter.casefold()
    visible_jobs = [job for job in all_jobs if not pattern or pattern in " ".join(str(value or "") for value in (
      job.jid, job.rec.get("JobName"), job.state, job.rec.get("User"), job.rec.get("Account"),
      job.rec.get("Partition"), job.rec.get("NodeList"), job.rec.get("WorkDir"), job.command,
      job.rec.get("SubmitLine"), job.rec.get("ExitCode"),
    )).casefold()]
    table.clear()
    for job in sorted(visible_jobs, key=SORT_KEYS[self.sort_column], reverse=self.sort_reverse):
      rec = job.rec
      state = job.state if job.queued and job.state != "PENDING" else \
              f"{job.state} ({job.reason})" if job.queued else f"{job.state} ({rec['ExitCode']})"
      start = fmt_time(rec["Start"])
      values = [job.jid, rec["JobName"]]
      if self.query_one("#us", Switch).value:
        values.append(rec["User"])
      values.append(state_text(state))
      if "progress" in self.job_column_keys:
        progress = progress_for_job(job)
        values.append(RichProgressBar(total=max(progress.total, 1), completed=progress.current, width=8) if progress else "")
      values.extend((rec["NNodes"], f"{rec['Elapsed']} / {rec['Timelimit']}",
                     ("~" + start) if start and job.state == "PENDING" else start, fmt_time(rec["End"]), err_text(job.err),
                     job.command[:150]))
      table.add_row(*values, key=job.jid)
    if current in {job.jid for job in visible_jobs}:
      table.move_cursor(row=table.get_row_index(current))
    table.loading = False
    if not isinstance(self.focused, Input): # loading took the focus away from the table (to the Reload button)
      table.focus()
    queued = [j for j in all_jobs if j.queued]
    running = sum(j.running for j in queued)
    finished = [j for j in all_jobs if not j.queued]
    errors = sum(j.err[0] == "file" and j.err[1] > 0 for j in all_jobs)
    status = f"{len(queued)} queued ({running} running, {len(queued)-running} pending)"
    if self.since:
      status += f" · {len(finished)} finished since {self.since:%m-%d %H:%M}, {sum(j.node_hours() for j in finished):.1f} node hours"
    if pattern:
      status += f" · {len(visible_jobs)} of {len(all_jobs)} jobs match '{self.job_filter}'"
    if errors:
      status += f" · {errors} with output in their error file"
    self.query_one("#status", Static).update(status + f" · updated {datetime.now():%H:%M:%S}")
    self.show_warnings()

  @on(Button.Pressed, "#reload")
  @on(Input.Submitted, "#since")
  @on(Switch.Changed, "#us")
  def reload_requested(self, event):
    if isinstance(event, Switch.Changed):
      self.setup_job_columns()
    self.action_reload()

  @on(Input.Changed, "#jfilter")
  def job_filter_changed(self, event):
    self.job_filter = event.value
    self.populate(list(self.jobs.values()))

  @on(Input.Submitted, "#jfilter")
  def job_filter_submitted(self, event):
    event.input.remove_class("visible")
    self.query_one("#jobs", ClickTable).focus()

  @on(DataTable.HeaderSelected, "#jobs")
  def sort_by(self, event):
    col = event.column_key.value
    if col == self.sort_column:
      self.sort_reverse = not self.sort_reverse
    else:
      self.sort_column, self.sort_reverse = col, col in ("jobid", "start", "end", "err")
    self.populate(list(self.jobs.values()))

  def current_job(self):
    table = self.query_one("#jobs", ClickTable)
    if not table.row_count:
      return None
    key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
    return self.jobs.get(key)

  @on(DataTable.RowSelected, "#jobs")
  def job_selected(self, event):
    job = self.jobs.get(event.row_key.value)
    col = self.query_one("#jobs", ClickTable).take_clicked_column()
    if not job:
      return
    if col == "err" and job.stderr_path() and job.err[0] == "file":
      self.open_file(job, job.stderr_path())
    else:
      self.push_screen(JobScreen(job))

  def open_file(self, job, path):
    open_path(self, ["Jobs", job.jid], path, [job.rec["WorkDir"]], job_vars(job), follow=job.running)

  def action_open(self, kind):
    job = self.current_job()
    path = job and (job.stderr_path() if kind == "stderr" else job.stdout_path())
    if not path or not os.path.isfile(path):
      self.notify(f"no {kind} file" + (" (stderr goes to the output file)" if job and kind == "stderr" and job.merged() else ""),
                  severity="warning")
      return
    self.open_file(job, path)

REPORTS_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1015l\x1b[?1006l\x1b[?1004l" # mouse and focus reporting off

def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--log", metavar="FILE", help="write a debug log (all slurm commands that are run) to FILE")
  parser.add_argument("--serve", action="store_true", help="serve the app in a browser with textual-serve")
  parser.add_argument("--host", default="127.0.0.1", help="web server bind address for --serve (default: 127.0.0.1)")
  parser.add_argument("--port", type=int, default=8000, help="web server port for --serve (default: 8000)")
  args = parser.parse_args()

  if args.serve:
    try:
      from textual_serve.server import Server
    except ImportError:
      parser.error("--serve requires textual-serve; install it with: pip install textual-serve")
    command = [sys.executable, os.path.abspath(__file__)]
    if args.log:
      command += ["--log", args.log]
    Server(shlex.join(command), host=args.host, port=args.port, title="Slurm TUI").serve()
    return

  setup_logging(args.log)
  app = SlurmTUI()
  try:
    app.run()
  finally:
    if app.shell:
      app.shell.terminate()

if __name__ == "__main__":
  main()
