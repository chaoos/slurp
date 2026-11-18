"""A transfer and the store of all of them ($XDG_STATE_HOME/slurp/transfers.json: their definitions, their state and
their runs; the state changes often, so it is not in the settings)"""

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field

from ..state import log

# the states of a transfer (what the list shows)
STATES = {
  "new":         "not started",
  "checking":    "checking",
  "queued":      "queued",        # a transfer job waits in the queue
  "starting":    "starting",
  "running":     "running",
  "waiting":     "waiting",       # for a tab (its connection, its login)
  "pausing":     "pausing",
  "paused":      "paused",
  "done":        "done",
  "done-errors": "done with errors", # some files could not be copied (rsync exit 23)
  "failed":      "failed",
}
ACTIVE = ("starting", "running", "queued", "pausing", "checking") # a run is under way (not started twice)

ROUTES = {
  "local":       "on this machine",
  "same-fs":     "same filesystem",
  "direct-push": "direct (source pushes)",
  "direct-pull": "direct (destination pulls)",
  "relay":       "relay through slurp",
}
RUNS_ON = {
  "slurp":    "in slurp",
  "detached": "detached on the cluster",
  "job":      "as a transfer job",
}
HISTORY = 50 # runs kept per transfer

def tab_ref(cluster):
  """How a transfer names a tab: its destination (None: this machine), its name, and its SSH settings (to open it
  again when it is closed)"""
  ref = {"destination": cluster.destination, "name": cluster.name}
  if cluster.ssh:
    ref["ssh"] = cluster.ssh
  if cluster.pre_login:
    ref["pre_login"] = cluster.pre_login
  return ref

def find_tab(clusters, ref):
  """The open tab a transfer names (None: not open): the same destination, the same name if there are several"""
  ref = ref or {}
  same = [c for c in clusters if (c.destination or None) == (ref.get("destination") or None)]
  return next((c for c in same if c.name == ref.get("name")), same[0] if same else None)

def tab_name(ref):
  ref = ref or {}
  return ref.get("name") or ref.get("destination") or "Local"

@dataclass
class Transfer:
  id: str
  name: str
  source: dict                       # {"endpoint": id, "name", "tab": tab_ref (a tab), "paths": [path, …]}: each path is
                                     # copied into the destination (endpoints.py)
  dest: dict                         # {"endpoint": id, "name", "tab": tab_ref (a tab), "path": directory}
  route: str = "relay"               # ROUTES
  runs_on: str = "slurp"             # RUNS_ON
  options: dict = field(default_factory=dict)  # checksum, bwlimit (MB/s), compress, temp_key, partition, time, account, resubmit
  state: str = "new"
  message: str = ""                  # why it waits, how it failed, …
  progress: dict = field(default_factory=dict) # of the run: bytes, total, rate, eta, files, files_total, current, errors
  plan: dict = field(default_factory=dict)     # what the check found (remaining bytes and files, the estimate)
  run: dict = field(default_factory=dict)      # the run under way: kind, started, pid/job, dir, host
  runs: list = field(default_factory=list)     # the runs so far: {started, ended, result, bytes, files, job, message}
  created: float = field(default_factory=time.time)
  errors: list = field(default_factory=list)   # what rsync said about single files (of the last run)
  log: list = field(default_factory=list)      # events: [epoch, text]
  pending: dict | None = None        # a run that waits for its tabs: {resume: after a broken connection}
  runs_total: int = 0                # runs so far (the history keeps the last HISTORY of them)

  @property
  def run_count(self): return max(self.runs_total, len(self.runs))

  @property
  def source_name(self): return self.source.get("name") or tab_name(self.source.get("tab"))
  @property
  def dest_name(self): return self.dest.get("name") or tab_name(self.dest.get("tab"))
  @property
  def active(self): return self.state in ACTIVE

  def event(self, text):
    self.log.append([time.time(), text])
    del self.log[:-200]
    log.debug(f"transfer {self.name}: {text}")

  def to_json(self):
    return asdict(self)

  @classmethod
  def from_json(cls, data):
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    t = cls(**known)
    # (older transfers: their ends name their endpoint; a schedule is gone: they run on Run now, said once)
    for end in (t.source, t.dest):
      if "endpoint" not in end:
        tab = end.get("tab") or {}
        end["endpoint"] = f"tab:{tab['destination']}" if tab.get("destination") else "local"
    schedule = data.get("schedule") or {}
    t.unscheduled = schedule.get("kind") not in (None, "now", "manual", "never")
    if t.unscheduled:
      t.event("its schedule is gone: it runs on Run now (Copy as a command gives the command for scrontab or cron)")
    if t.state in ("scheduled",):
      t.state, t.message = "new", ""
    if isinstance(t.pending, dict):
      t.pending.pop("scheduled", None)
      t.pending.pop("begin", None)
    if t.state in ACTIVE and (t.runs_on == "slurp" or not t.run.get("folder")):
      # (it ran in slurp, which ended, or it was still starting: paused, Continue goes on)
      if t.run:
        record_run(t, "paused", "slurp ended")
      t.state, t.message = "paused", "paused (slurp ended)"
      t.run = {}
    return t

def new_id():
  return secrets.token_hex(4)

def default_name(source_paths, source, dest):
  first = os.path.basename((source_paths or [""])[0].rstrip("/")) or "transfer"
  more = f" +{len(source_paths) - 1}" if len(source_paths or []) > 1 else ""
  return f"{first}{more} {tab_name(source)} → {tab_name(dest)}"

def store_path():
  return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "slurp", "transfers.json")

Transfer.unscheduled = False # (read from an older store with a schedule: said once at the start)

class Store:
  """All transfers, written atomically (on changes; while runs go on at most every few seconds)"""
  SAVE_EVERY = 5 # seconds between writes for progress only

  def __init__(self, path=None):
    self.path = path or store_path()
    self.lock = threading.Lock()
    self.transfers = []
    self.saved_at = 0.0
    self.load()

  def load(self):
    try:
      with open(self.path) as f:
        data = json.load(f)
    except FileNotFoundError:
      data = {}
    except (OSError, ValueError) as e:
      log.warning(f"cannot read {self.path}: {e}")
      data = {}
    self.transfers = []
    for item in data.get("transfers", []) if isinstance(data, dict) else []:
      try:
        self.transfers.append(Transfer.from_json(item))
      except (TypeError, ValueError) as e:
        log.warning(f"a transfer in {self.path} cannot be read: {e}")

  def get(self, tid):
    return next((t for t in self.transfers if t.id == tid), None)

  def add(self, transfer):
    self.transfers.append(transfer)
    self.save()

  def remove(self, transfer):
    if transfer in self.transfers:
      self.transfers.remove(transfer)
      self.save()

  def save(self, progress_only=False):
    """Write the store (progress_only: only if the last write is a few seconds old)"""
    now = time.monotonic()
    if progress_only and now - self.saved_at < self.SAVE_EVERY:
      return
    self.saved_at = now
    with self.lock:
      data = {"transfers": [t.to_json() for t in self.transfers]}
      try:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".transfers.")
        with os.fdopen(fd, "w") as f:
          json.dump(data, f, indent=1)
        os.replace(tmp, self.path)
      except OSError as e:
        log.warning(f"cannot write {self.path}: {e}")

def record_run(transfer, result, message=""):
  """The run under way ended (result: a state, done, done-errors, failed, paused): into the history, with what it did
  (its number, times, bytes, files, rate, rsync's statistics, the errors, where it ran, its events)"""
  run = dict(transfer.run or {})
  progress = transfer.progress or {}
  started, ended = run.get("started") or time.time(), time.time()
  transfer.runs_total = transfer.run_count + 1
  done = progress.get("bytes", 0) or 0
  entry = {"number": transfer.runs_total, "started": started, "ended": ended, "result": result,
           "bytes": done, "files": progress.get("files", 0), "total": progress.get("total"),
           "rate": done / (ended - started) if ended > started and done else 0.0,
           "route": transfer.route, "runs_on": transfer.runs_on, "job": run.get("job", ""), "host": run.get("host", ""),
           "folder": run.get("folder", ""), "message": message, "stats": dict(progress.get("stats") or {}),
           "errors": list(transfer.errors or [])[:100], "error_count": progress.get("errors", 0) or len(transfer.errors or []),
           "options": dict(transfer.options or {}), "sources": list(transfer.source.get("paths", [])),
           "dest": transfer.dest.get("path", ""), "events": [e for e in transfer.log if e[0] >= started - 1][-60:]}
  transfer.runs.append(entry)
  del transfer.runs[:-HISTORY]
  transfer.run = {}
  return entry
