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
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass, field
from datetime import datetime

import humanize
from rich.color import Color
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
from textual.screen import Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual.worker import get_current_worker

import slurm as sl

log = logging.getLogger("slurm_tui")

TAIL_BYTES = 4*1024*1024 # files larger than this are shown from their end
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
                "MaxRSS", "AveRSS", "SubmitLine"]
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
class Job:
  rec: dict                              # sacct record of the allocation (or built from squeue)
  steps: list = field(default_factory=list)
  queued: bool = False                   # listed by squeue
  reason: str = ""                       # pending reason (squeue)
  command: str = ""                      # what the job executes (filled by annotate)
  err: tuple = (None, 0)                 # ("file"|"merged"|"missing"|None, size)

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

def annotate(jobs):
  cache = {}
  for job in jobs:
    job.command = command_of(job, cache)
    job.err = err_status(job)
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
  d = {"files": [], "sstat": {}, "nodes": []}
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
        if is_binary(e.path):
          e.note += " binary"
      except OSError:
        e.note = "not readable"
  add(Entry("sacct", "all accounting fields of the job and its steps", text=None, note="sacct -o ALL"))

  if job.running:
    raw = job.rec["JobIDRaw"]
    for row in parsable(run(["sstat", "-a", "-j", raw, "-P", "-n", "-o", "JobID,MaxRSS,AveRSS"]), ["JobID", "MaxRSS", "AveRSS"]):
      d["sstat"][row["JobID"].partition(".")[2]] = row
    if job.rec["NodeList"]:
      d["nodes"] = run(["scontrol", "show", "hostnames", job.rec["NodeList"]]).split()
  return d

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

def open_path(app, crumbs, path, bases=(), env=None, follow=False):
  """Open a directory in the browser or a file in the viewer"""
  if os.path.isdir(path):
    app.push_screen(DirScreen(crumbs, path, bases=bases, env=env))
  elif is_binary(path):
    app.notify(f"{path} is a binary file", severity="warning")
  elif os.path.isfile(path):
    bases = list(dict.fromkeys([*bases, os.path.dirname(path)]))
    app.push_screen(FileScreen([*crumbs, os.path.basename(path)], path=path, follow=follow, bases=bases, env=env))
  else:
    app.notify(f"{path} does not exist (yet)", severity="warning")

DIR_COLUMNS = [("name", "Name"), ("size", "Size"), ("mtime", "Modified")]
DIR_LIMIT = 20000 # entries listed at most

class DirScreen(Screen):
  """Directory browser: click (or Enter) a subdirectory to enter it, a file to open it"""
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

  def compose(self) -> ComposeResult:
    yield Header()
    yield Static(id="dstatus")
    yield ClickTable(id="dir", cursor_type="row", zebra_stripes=True)
    yield Input(id="dfilter", placeholder="filter names (Enter: apply, Esc: clear)")
    yield Footer()

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
      self.app.pop_screen()

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

class FileScreen(Screen):
  """Read-only viewer for a file (with tail -f style following) or for text (optionally re-run by a refresher)"""
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

  def compose(self) -> ComposeResult:
    yield Header()
    yield Static(id="fstatus")
    area = PathArea(id="text", read_only=True, show_line_numbers=True, soft_wrap=False, highlight_cursor_line=False)
    area.resolver = lambda word: resolve_path(word, self.bases, self.env)
    yield area
    yield Input(id="search", placeholder="search (Enter: find, Esc: cancel)")
    yield Footer()

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
      self.app.pop_screen()

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

class JobScreen(Screen):
  """Details, files and steps of one job, commands on its nodes"""
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

  def compose(self) -> ComposeResult:
    yield Header()
    with VerticalScroll(id="details"):
      yield Static(id="info")
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
          yield Button("Shell", id="shell", tooltip="interactive shell on the node (Ctrl-D closes it)")
        with Horizontal(id="presets"):
          for i, name in enumerate(PRESETS):
            yield Button(name, id=f"preset{i}", classes="preset")
    yield Footer()

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
    for key, val in rows:
      grid.add_row(key, val)
    self.query_one("#info", Static).update(grid)
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
    self.show_job()
    self.show_files()
    self.app.show_warnings()

  def action_back(self):
    self.app.pop_screen()

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
    self.app.push_screen(TerminalScreen(["Jobs", self.job.jid, f"shell on {node}"], args,
                                        f"shell on {node} in job {self.job.jid} · Ctrl-D closes it"))

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

class TerminalView(Widget, can_focus=True):
  """Runs a command in a pseudo terminal and shows it; all keys go to the command"""
  DEFAULT_CSS = "TerminalView { height: 1fr; }"

  class Exited(Message):
    def __init__(self, code, elapsed):
      super().__init__()
      self.code, self.elapsed = code, elapsed

  def __init__(self, args, **kwargs):
    super().__init__(**kwargs)
    self.args = args
    self.vt = self.stream = self.proc = self.fd = None
    self.started = None
    self.done = False

  def on_mount(self):
    self.call_after_refresh(self.start)

  def start(self):
    cols, rows = max(self.size.width, 20), max(self.size.height, 5)
    try:
      self.vt, pyte = make_vt_screen(cols, rows, self.write)
    except ImportError:
      self.post_message(self.Exited(-1, 0))
      self.app.notify("the shell window needs the python package pyte: pip install pyte", severity="error", timeout=10)
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
      self.stream.feed(f"cannot start {self.args[0]}: {e}\r\n".encode())
      os.close(slave)
      self.finish()
      return
    finally:
      if self.proc:
        os.close(slave)
    self.started = time.time()
    os.set_blocking(master, False)
    asyncio.get_running_loop().add_reader(master, self.read_output)

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
    self.refresh()

  def finish(self):
    if self.done:
      return
    self.done = True
    if self.fd is not None:
      try:
        asyncio.get_running_loop().remove_reader(self.fd)
      except Exception:
        pass
      os.close(self.fd)
      self.fd = None
    code = -1
    if self.proc:
      try:
        code = self.proc.wait(timeout=3)
      except subprocess.TimeoutExpired:
        self.proc.kill()
        code = self.proc.wait()
    self.refresh()
    self.post_message(self.Exited(code, time.time() - (self.started or time.time())))

  def write(self, data):
    if self.fd is not None and not self.done:
      try:
        os.write(self.fd, data)
      except OSError:
        pass

  def on_unmount(self):
    if not self.done and self.proc and self.proc.poll() is None:
      self.proc.terminate() # window closed while the command runs (e.g. the app quits)
    if self.fd is not None:
      try:
        asyncio.get_running_loop().remove_reader(self.fd)
      except Exception:
        pass

  def on_resize(self, event):
    if self.vt is not None:
      cols, rows = max(event.size.width, 2), max(event.size.height, 2)
      self.vt.resize(rows, cols)
      self.set_winsize(cols, rows)
      self.refresh()

  async def on_key(self, event):
    if self.done or self.vt is None:
      return # let the window handle Esc to close it
    event.stop()
    event.prevent_default()
    key = event.key
    if key in TERM_ARROWS:
      data = ("\x1bO" if DECCKM in self.vt.mode else "\x1b[") + TERM_ARROWS[key]
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
    self.write(data.encode())

  def on_paste(self, event):
    text = event.text.replace("\r\n", "\r").replace("\n", "\r")
    if self.vt is not None and (2004 << 5) in self.vt.mode: # bracketed paste
      text = f"\x1b[200~{text}\x1b[201~"
    self.write(text.encode())

  def render_line(self, y):
    width = self.size.width
    vt = self.vt
    if vt is None or y >= vt.lines:
      return Strip.blank(width, self.rich_style)
    line = vt.buffer[y]
    cursor_x = vt.cursor.x if (y == vt.cursor.y and self.has_focus and not vt.cursor.hidden and not self.done) else -1
    base = self.rich_style
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

class TerminalScreen(Screen):
  """Window with an interactive command (a shell on a node); it closes when the command ends"""
  BINDINGS = [Binding("escape", "close", "Close", show=False)] # only reaches the window once the command ended

  def __init__(self, crumbs, args, title):
    super().__init__()
    self.crumbs, self.args, self.title_text = crumbs, args, title

  def compose(self) -> ComposeResult:
    yield Header()
    yield Static(self.title_text, id="tstatus")
    yield TerminalView(self.args, id="term")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#term").focus()

  @property
  def running(self):
    term = self.query_one("#term", TerminalView)
    return not term.done

  @on(TerminalView.Exited)
  def exited(self, event):
    if event.code == 0 or event.elapsed > 10: # Ctrl-D / exit (bash passes on the status of its last command)
      self.app.pop_screen()
    else: # probably failed to start: keep the window to show why
      self.query_one("#tstatus", Static).update(f"{self.title_text} · ended with exit code {event.code} · Esc closes the window")

  def action_close(self):
    self.app.pop_screen()

COLUMNS = [("jobid", "Job ID"), ("name", "Name"), ("state", "State"), ("nodes", "Nodes"), ("time", "Elapsed / Limit"),
           ("start", "Start"), ("end", "End"), ("err", "Err"), ("cmd", "Command")]

SORT_KEYS = {
  "jobid": lambda j: job_id_key(j.jid),
  "name":  lambda j: j.rec["JobName"],
  "state": lambda j: j.state,
  "nodes": lambda j: int(j.rec["NNodes"] or 0),
  "time":  lambda j: j.node_hours() / max(int(j.rec["NNodes"] or 1), 1),
  "start": lambda j: j.rec["Start"] if is_time(j.rec["Start"]) else "",
  "end":   lambda j: j.rec["End"] if is_time(j.rec["End"]) else "",
  "err":   lambda j: (j.err[0] == "file", j.err[1]),
  "cmd":   lambda j: j.command,
}

class SlurmTUI(App):
  TITLE = "slurm_tui"
  CSS = """
  #toolbar { height: auto; padding: 0 1; }
  #toolbar > * { margin-right: 1; }
  #toolbar Label { padding-top: 1; }
  #since { width: 12; }
  #status { height: 1; padding: 0 1; background: $panel; }
  #jobs { height: 1fr; }
  #details { padding: 0 1; }
  .section { margin-top: 1; text-style: bold; color: $accent; }
  #files { height: auto; max-height: 16; }
  #steps { height: auto; max-height: 14; }
  #nodes { height: auto; }
  #nodebar, #presets { height: auto; }
  #cmd { width: 1fr; }
  #node { width: 28; }
  #presets Button { margin-right: 1; min-width: 8; }
  #fstatus, #tstatus { height: 1; padding: 0 1; background: $panel; }
  #text { height: 1fr; }
  #search, #dfilter { dock: bottom; display: none; }
  #search.visible, #dfilter.visible { display: block; }
  #dstatus { height: 1; padding: 0 1; background: $panel; }
  #dir { height: 1fr; }
  """
  BINDINGS = [
    Binding("q", "quit", "Quit"),
    Binding("r", "reload", "Reload"),
    Binding("e", "open('stderr')", "Error file"),
    Binding("o", "open('stdout')", "Output file"),
  ]

  def __init__(self):
    super().__init__()
    self.jobs = {}
    self.sort_column, self.sort_reverse = "jobid", True
    self.since = None

  def compose(self) -> ComposeResult:
    yield Header()
    with Horizontal(id="toolbar"):
      yield Button("Reload", id="reload", variant="primary")
      yield Label("finished since")
      yield Input("1d", id="since", placeholder="off")
      yield Checkbox("all my accounts", id="us")
    yield Static(id="status")
    yield ClickTable(id="jobs", cursor_type="row", zebra_stripes=True)
    yield Footer()

  def on_mount(self):
    table = self.query_one("#jobs", ClickTable)
    for key, label in COLUMNS:
      table.add_column(label, key=key)
    table.focus()
    self.action_reload()

  async def on_event(self, event):
    if isinstance(event, events.Key) and event is not getattr(self, "_logged_key", None):
      self._logged_key = event # the app sees a key twice: on its way to the focused widget and bubbling back
      # with --log: when do keys arrive (e.g. a lone Esc held back by the terminal until the next key)
      log.debug(f"key {event.key!r} in {type(self.screen).__name__} ({len(self.screen_stack)} windows open)")
    await super().on_event(event)

  def check_action(self, action, parameters):
    if action in ("open", "reload"): # keys of the job table, not of the other windows
      return self.screen is self.screen_stack[0]
    if action in ("quit", "command_palette", "help_quit") and isinstance(self.screen, TerminalScreen) and self.screen.running:
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
    self.load(since, self.query_one("#us", Checkbox).value)

  @work(thread=True, exclusive=True, group="jobs")
  def load(self, since, us):
    jobs = annotate(load_jobs(since, us))
    if not get_current_worker().is_cancelled:
      self.call_from_thread(self.populate, jobs)

  def populate(self, jobs):
    table = self.query_one("#jobs", ClickTable)
    current = None
    if table.row_count:
      try:
        current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
      except Exception:
        pass
    self.jobs = {job.jid: job for job in jobs}
    table.clear()
    for job in sorted(jobs, key=SORT_KEYS[self.sort_column], reverse=self.sort_reverse):
      rec = job.rec
      state = job.state if job.queued and job.state != "PENDING" else \
              f"{job.state} ({job.reason})" if job.queued else f"{job.state} ({rec['ExitCode']})"
      start = fmt_time(rec["Start"])
      table.add_row(
        job.jid, rec["JobName"], state_text(state), rec["NNodes"], f"{rec['Elapsed']} / {rec['Timelimit']}",
        ("~" + start) if start and job.state == "PENDING" else start, fmt_time(rec["End"]), err_text(job.err),
        job.command[:150], key=job.jid)
    if current in self.jobs:
      table.move_cursor(row=table.get_row_index(current))
    table.loading = False
    if not isinstance(self.focused, Input): # loading took the focus away from the table (to the Reload button)
      table.focus()
    queued = [j for j in jobs if j.queued]
    running = sum(j.running for j in queued)
    finished = [j for j in jobs if not j.queued]
    errors = sum(j.err[0] == "file" and j.err[1] > 0 for j in jobs)
    status = f"{len(queued)} queued ({running} running, {len(queued)-running} pending)"
    if self.since:
      status += f" · {len(finished)} finished since {self.since:%m-%d %H:%M}, {sum(j.node_hours() for j in finished):.1f} node hours"
    if errors:
      status += f" · {errors} with output in their error file"
    self.query_one("#status", Static).update(status + f" · updated {datetime.now():%H:%M:%S}")
    self.show_warnings()

  @on(Button.Pressed, "#reload")
  @on(Input.Submitted, "#since")
  @on(Checkbox.Changed, "#us")
  def reload_requested(self, event):
    self.action_reload()

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

def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--log", metavar="FILE", help="write a debug log (all slurm commands that are run) to FILE")
  args = parser.parse_args()
  setup_logging(args.log)
  SlurmTUI().run()

if __name__ == "__main__":
  main()
