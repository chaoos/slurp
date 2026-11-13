"""Slurm queries, the jobs of the job list, their progress and the estimates when they finish, array jobs"""

import argparse
import asyncio
import codecs
import collections
import fcntl
import functools
import json
import logging
import os
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
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
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, MarkdownViewer, OptionList, ProgressBar, RadioButton, RadioSet, Select, Static, Switch, TextArea, Tree
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual.suggester import SuggestFromList
from textual.worker import get_current_worker

import slurm as sl

from .state import *
from .remote import *

def parsable(out, fields):
  """Rows of '|'-separated output as dicts, the last field may contain '|'; other lines (e.g. messages) are skipped"""
  rows = [line.split("|", len(fields)-1) for line in out.splitlines()]
  return [dict(zip(fields, row)) for row in rows if len(row) == len(fields)]

SACCT_FIELDS = ["JobID", "JobIDRaw", "JobName", "User", "Account", "Partition", "State", "ExitCode", "NNodes", "NTasks",
                "NodeList", "Submit", "Start", "End", "Elapsed", "Timelimit", "WorkDir", "StdOut", "StdErr",
                "MaxRSS", "AveRSS", "TRESUsageInMax", "SubmitLine"]
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

PROGRESS = {} # (tab, job id) -> {"file": progress file chosen for the job or None, "points": [(epoch seen, n, N)]}

_saved_progress_files = None # from the config: "tab|job id" -> file

def progress_state(jid):
  global _saved_progress_files
  key = (tab_name(), jid)
  if key not in PROGRESS:
    if _saved_progress_files is None:
      from .config import load_config
      _saved_progress_files = dict(load_config().get("progress_files") or {})
    PROGRESS[key] = {"file": _saved_progress_files.get(f"{key[0]}|{jid}"), "points": []}
  return PROGRESS[key]

def set_progress_file(jid, path):
  """Choose the progress file of a job (None: its output); kept in the config"""
  global _saved_progress_files
  from .config import load_config, update_config
  state = progress_state(jid)
  state["file"] = path
  state["points"].clear() # another file, other points
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
  groups (dates, times, ...) match any digits. None if neither the index nor the job id is in the path."""
  # the index as it is written: exactly, else zero-padded (07 for 7) if it is nowhere exactly (a time like 10-01-07
  # must not count as task 1 when the name also has a plain 1)
  groups = re.findall(r"\d+", path)
  exact = task is not None and str(task) in groups
  def is_task(part):
    if task is None:
      return False
    return part == str(task) if exact else part.lstrip("0") == str(int(task)) and len(part) > len(str(task))
  def template(text, regex):
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
  dir_tpl, dir_found = template(directory, False)
  name_tpl, name_found = template(name, True)
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
      from .config import load_config
      _saved_array_patterns = dict(load_config().get("array_progress_patterns") or {})
    saved = _saved_array_patterns.get(f"{key[0]}|{array_id}")
    if saved:
      ARRAY_PATTERNS[key] = saved
  return ARRAY_PATTERNS.get(key)

def set_array_pattern(array_id, pattern):
  global _saved_array_patterns
  from .config import load_config, update_config
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
  recent = recent[-6:]
  if len(recent) >= 2 and recent[-1][1] > recent[0][1] and recent[-1][0] > recent[0][0]:
    rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
    est["recent"] = (rate, recent[-1][0] + (total - recent[-1][1]) / rate, len(recent))
  return est

def record_progress(job, progress):
  """Remember when the app saw a new n/N of the job, and estimate the end"""
  if progress is None:
    return None
  points = progress_state(job.jid)["points"]
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

def best_eta(progress):
  """The estimate shown in the job list: the recent rate if known, else the average since the start"""
  est = progress.estimate if progress else {}
  return (est.get("recent") or est.get("overall") or (None, None))[1]

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

def current_user():
  c = conn()
  return c.user if c else sl.getpass.getuser()

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
  from .details import expand_vars, job_vars # (details builds on this module)
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
  from .details import existing_files, read_text # (details builds on this module)
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
    from .details import existing_files # (details builds on this module)
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

def output_progress(path, tail=None):
  """Latest valid n/N entry in the tail of a file (only its last PROGRESS_TAIL_BYTES are read, here unless given),
  with the n/N points of the tail and their timestamps (if the lines have one)"""
  if not path:
    return None
  if tail is None:
    try:
      size = fs_getsize(path)
      tail = fs_read(path, max(0, size - PROGRESS_TAIL_BYTES), PROGRESS_TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
      return None
  latest, points = None, []
  for line in tail.splitlines()[-PROGRESS_TAIL_LINES * 20:]:
    for match in reversed(list(PROGRESS_PATTERN.finditer(line))):
      try:
        current, total = map(int, match.groups())
      except ValueError: # extremely long digit sequences
        continue
      if current <= total:
        latest = (current, total, line.strip())
        points.append((line_time(line), current, total))
        break
  if latest is None:
    return None
  return JobProgress(*latest, points=points[-50:], path=path)

def annotate(jobs):
  """Command, error file and progress of the jobs. On a remote cluster the sizes of the error files, the tails of
  the output files and the batch scripts are fetched together, in one round trip."""
  guess_array_progress(jobs) # progress files of array tasks, from the pattern of their array
  if conn() is None:
    cache = {}
    for job in jobs:
      job.command = command_of(job, cache)
      job.err = err_status(job)
      job.progress = record_progress(job, output_progress(progress_path(job))) if job.running else None
    return jobs
  err_paths = sorted({p for job in jobs if err_status(job, {})[0] == "missing" and (p := job.stderr_path())})
  out_paths = sorted({p for job in jobs if job.running and (p := progress_path(job))})
  scripts = sorted({job.jid.split("_")[0]: job.jid for job in jobs if job.queued and not step_commands(job)}.items())
  cmds = ([["stat", "-L", "-c", "%s", "--", p] for p in err_paths] +
          [["tail", "-c", str(PROGRESS_TAIL_BYTES), "--", p] for p in out_paths] +
          [["scontrol", "write", "batch_script", jid, "-"] for _, jid in scripts])
  results = run_many(cmds) if cmds else []
  sizes = {}
  for p, res in zip(err_paths, results):
    sizes[p] = int(res.stdout) if res.returncode == 0 and res.stdout.strip().isdigit() else None
  results = results[len(err_paths):]
  tails = {p: res.stdout if res.returncode == 0 else None for p, res in zip(out_paths, results)}
  cache = {key: res.stdout for (key, _), res in zip(scripts, results[len(out_paths):])}
  for job in jobs:
    job.command = command_of(job, cache)
    job.err = err_status(job, sizes)
    path = progress_path(job)
    job.progress = record_progress(job, output_progress(path, tails.get(path))) if job.running and tails.get(path) is not None else None
  return jobs
