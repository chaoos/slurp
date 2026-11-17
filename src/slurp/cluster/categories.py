"""Categories of jobs: a tree of names, like folders ("project X/production runs"). A job is in at most one; an array
is put in as a whole (its id), a single task can be too.

They are kept on the cluster, in ~/.config/slurp/categories.json of the home there (a local tab: this machine's
config directory), so slurp on your own machine and slurp on the login node see the same. One home is often shared
by several clusters (whose job ids repeat): the file has one part per Slurm cluster (its ClusterName).

Every change reads the file again, changes it and writes it back (another slurp may have changed it meanwhile); a
file that is not valid JSON is never overwritten."""

import json
import os
import re
from collections import Counter
from datetime import datetime

from ..state import log

FILE = "~/.config/slurp/categories.json" # on the cluster of an SSH tab
MAX_DEPTH = 8
MAX_NAME = 80

def normalize(text):
  """The path of a category as typed ("project X / production runs" -> "project X/production runs"); ValueError"""
  parts = [p.strip() for p in str(text or "").replace("\\", "/").split("/")]
  parts = [p for p in parts if p]
  if not parts:
    raise ValueError("give the category a name (a/b: b in the category a)")
  if len(parts) > MAX_DEPTH:
    raise ValueError(f"at most {MAX_DEPTH} levels")
  for p in parts:
    if len(p) > MAX_NAME:
      raise ValueError(f"a name has at most {MAX_NAME} characters")
    if re.search(r"[\x00-\x1f\x7f]", p):
      raise ValueError("no control characters in a name")
  return "/".join(parts)

def parent(path):
  return path.rpartition("/")[0]

def leaf(path):
  return path.rpartition("/")[2]

def within(path, top):
  """path is top or in it (top "": everything)"""
  return not top or path == top or path.startswith(top + "/")

def lineage(path):
  """"a/b/c" -> ["a", "a/b", "a/b/c"]"""
  parts = path.split("/")
  return ["/".join(parts[:i + 1]) for i in range(len(parts))]

def array_of(jid):
  """The array (or job) a job id belongs to: "123_4" -> "123" """
  return str(jid).partition(".")[0].partition("_")[0]

MAX_NOTES = 20000 # characters of a category's notes at most

def cancel_entry(jid, entry):
  """Why a job was cancelled, as kept: {reason (a key of the list), label, text (details), related (a job id: the
  one it duplicates, …), what (the pending tasks, …), at}; ValueError if it is not one"""
  if not re.fullmatch(r"[0-9]+(_[0-9]+)?", str(jid)) or not isinstance(entry, dict):
    raise ValueError(f"not a cancel reason of a job: {jid}")
  out = {k: str(entry.get(k) or "")[:limit] for k, limit in
         (("reason", 40), ("label", 200), ("text", 2000), ("related", 40), ("what", 200), ("at", 40))}
  if out["related"] and not re.fullmatch(r"[0-9]+(_[0-9]+)?", out["related"]):
    out["related"] = ""
  if not (out["reason"] or out["text"]):
    raise ValueError("no reason")
  return {k: v for k, v in out.items() if v}

class Store:
  """The categories of one Slurm cluster: their paths, the jobs in them ({jid: {category, name, added}}), notes of
  categories (Markdown, {path: text}), notes of jobs (Markdown, {jid: text}; of any job, in a category or not) and
  why jobs were cancelled ({jid: {reason, label, text, related, what, at}})"""

  def __init__(self, cluster="default", data=None):
    data = data if isinstance(data, dict) else {}
    self.cluster = cluster
    self.paths = set()
    for p in data.get("categories") or []:
      try:
        self.paths.update(lineage(normalize(p)))
      except ValueError:
        pass
    self.jobs = {}
    for jid, entry in (data.get("jobs") or {}).items():
      if isinstance(entry, dict) and re.fullmatch(r"[0-9]+(_[0-9]+)?", str(jid)):
        try:
          entry = dict(entry, category=normalize(entry.get("category")))
        except ValueError:
          continue
        self.jobs[str(jid)] = entry
        self.paths.update(lineage(entry["category"]))
    self.notes = {}
    for p, text in (data.get("notes") or {}).items():
      try:
        if isinstance(text, str) and text.strip():
          self.notes[normalize(p)] = text[:MAX_NOTES]
          self.paths.update(lineage(normalize(p)))
      except ValueError:
        pass
    self.job_notes = {str(jid): text[:MAX_NOTES] for jid, text in (data.get("job_notes") or {}).items()
                      if isinstance(text, str) and text.strip() and re.fullmatch(r"[0-9]+(_[0-9]+)?", str(jid))}
    self.cancelled = {}
    for jid, entry in (data.get("cancelled") or {}).items():
      try:
        self.cancelled[str(jid)] = cancel_entry(jid, entry)
      except ValueError:
        pass

  def to_data(self):
    return {"categories": sorted(self.paths), "jobs": dict(sorted(self.jobs.items())),
            **({"notes": dict(sorted(self.notes.items()))} if self.notes else {}),
            **({"job_notes": dict(sorted(self.job_notes.items(), key=lambda kv: [int(x) for x in kv[0].split("_")]))}
               if self.job_notes else {}),
            **({"cancelled": dict(sorted(self.cancelled.items(), key=lambda kv: [int(x) for x in kv[0].split("_")]))}
               if self.cancelled else {})}

  # ---- reading
  def category_of(self, jid):
    """The category of a job (an array task: its own, else its array's), "" if none"""
    jid = str(jid)
    entry = self.jobs.get(jid) or self.jobs.get(array_of(jid))
    return entry["category"] if entry else ""

  def children(self, path=""):
    """The categories directly in path ("": the top ones), sorted"""
    return sorted((p for p in self.paths if parent(p) == path), key=str.casefold)

  def job_ids(self, path="", recursive=True):
    """The jobs (and arrays) put in path (and, recursive, in the categories in it)"""
    return sorted((jid for jid, e in self.jobs.items() if (within(e["category"], path) if recursive else e["category"] == path)),
                  key=lambda j: [int(x) for x in j.split("_")])

  def count(self, path, recursive=True):
    return len(self.job_ids(path, recursive))

  def notes_of(self, path):
    """The notes of a category (Markdown), "" if none"""
    return self.notes.get(path, "")

  def cancel_reason_of(self, jid):
    """Why a job was cancelled ({reason, label, text, related, what, at}; an array task: its own, else its array's),
    None if not said"""
    jid = str(jid)
    return self.cancelled.get(jid) or self.cancelled.get(array_of(jid))

  def job_notes_of(self, jid):
    """The notes of a job (Markdown; an array task: its own), "" if none"""
    return self.job_notes.get(str(jid), "")

  # ---- changes (on a store just read from the file: see change())
  def note(self, path, text):
    """Notes of a category (Markdown; empty: none)"""
    path = self.add(path)
    text = (text or "").strip()
    if text:
      self.notes[path] = text[:MAX_NOTES]
    else:
      self.notes.pop(path, None)
    return path

  def job_note(self, jid, text):
    """Notes of a job (Markdown; empty: none)"""
    jid = str(jid)
    if not re.fullmatch(r"[0-9]+(_[0-9]+)?", jid):
      raise ValueError(f"not a job id: {jid}")
    text = (text or "").strip()
    if text:
      self.job_notes[jid] = text[:MAX_NOTES]
    else:
      self.job_notes.pop(jid, None)
    return jid

  def cancel_reason(self, jid, entry):
    """Why a job (an array: as a whole) was cancelled; None: not said (any more)"""
    jid = str(jid)
    if entry is None:
      self.cancelled.pop(jid, None)
    else:
      self.cancelled[jid] = cancel_entry(jid, entry)
    return jid

  def add(self, path):
    path = normalize(path)
    self.paths.update(lineage(path))
    return path

  def assign(self, jids, path, names=None):
    """Put jobs in a category (made if it is new); a task of an array that is in it as a whole stays with it"""
    path = self.add(path)
    now = datetime.now().isoformat(timespec="seconds")
    for jid in jids:
      jid = str(jid)
      if "_" in jid and self.jobs.get(array_of(jid), {}).get("category") == path:
        self.jobs.pop(jid, None)
        continue
      if "_" not in jid: # (the whole array: its tasks go with it)
        for task in [j for j in self.jobs if j.startswith(jid + "_")]:
          del self.jobs[task]
      self.jobs[jid] = {"category": path, "name": (names or {}).get(jid, ""), "added": now}
    return path

  def unassign(self, jids):
    for jid in jids:
      jid = str(jid)
      self.jobs.pop(jid, None)
      if "_" not in jid:
        for task in [j for j in self.jobs if j.startswith(jid + "_")]:
          del self.jobs[task]

  def rename(self, old, new):
    """Rename or move a category, with what is in it ("a/b" -> "c/b" moves it into c); into an existing one: merged"""
    old, new = normalize(old), normalize(new)
    if old not in self.paths:
      raise ValueError(f"there is no category {old}")
    if within(new, old) and new != old:
      raise ValueError(f"{new} is in {old}: a category cannot go into itself")
    def moved(p):
      return new + p[len(old):] if within(p, old) else p
    self.paths = {moved(p) for p in self.paths}
    self.paths.update(lineage(new))
    notes = {}
    for p, text in self.notes.items(): # (they go with it; into an existing one: one after the other)
      key = moved(p)
      notes[key] = notes[key] + "\n\n" + text if key in notes else text
    self.notes = notes
    for entry in self.jobs.values():
      entry["category"] = moved(entry["category"])
    return new

  def delete(self, path):
    """Delete a category and the ones in it; their jobs go to the category it is in (none at the top)"""
    path = normalize(path)
    up = parent(path)
    self.paths = {p for p in self.paths if not within(p, path)}
    self.notes = {p: text for p, text in self.notes.items() if not within(p, path)}
    for jid, entry in list(self.jobs.items()):
      if within(entry["category"], path):
        if up:
          entry["category"] = up
        else:
          del self.jobs[jid]
    return up

# ---- the file (in a worker: on the cluster of the current tab)
def file_path():
  from .remote import conn, fs_expanduser
  if conn() is None:
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "slurp", "categories.json")
  return fs_expanduser(FILE)

def read_all():
  """The whole file ({"version", "clusters": {name: store data}}); a missing one is empty. ValueError if it is not
  valid (then it is left as it is)."""
  from .remote import fs_exists, fs_read
  path = file_path()
  if not fs_exists(path):
    return {"version": 1, "clusters": {}}
  text = fs_read(path).decode("utf-8", errors="replace")
  try:
    data = json.loads(text) if text.strip() else {}
  except ValueError as e:
    raise ValueError(f"{path} is not valid JSON ({e}): fix or remove it, slurp does not overwrite it") from None
  if not isinstance(data, dict):
    raise ValueError(f"{path} is not what slurp writes: fix or remove it")
  data.setdefault("version", 1)
  if not isinstance(data.get("clusters"), dict):
    data["clusters"] = {}
  return data

def write_all(data):
  """Written whole and then moved into place (a reader never sees half a file)"""
  from .remote import conn, fs_write, run_cmd
  path = file_path()
  text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
  if conn() is None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
      f.write(text)
    os.replace(tmp, path)
    return
  res = run_cmd(["mkdir", "-p", os.path.dirname(path)], retry=False)
  if res.returncode:
    raise OSError((res.stderr or "").strip() or f"cannot create {os.path.dirname(path)}")
  tmp = path + ".slurp.tmp"
  fs_write(tmp, text)
  res = run_cmd(["mv", "-f", tmp, path], retry=False)
  if res.returncode:
    raise OSError((res.stderr or "").strip() or f"cannot write {path}")

def cluster_name():
  """Slurm's name of the cluster (ClusterName), "default" if it is not known"""
  from .remote import run
  out = run(["scontrol", "show", "config"], timeout=30, quiet=True)
  m = re.search(r"^\s*ClusterName\s*=\s*(\S+)", out, re.M)
  return m[1] if m else "default"

def load(cluster):
  return Store(cluster, read_all()["clusters"].get(cluster))

OPS = ("add", "assign", "unassign", "rename", "delete", "note", "job_note", "cancel_reason") # (the changes, by name: kept to be done later, see jobcache)

def apply_op(store, op):
  """[name, args...] on a store; its result"""
  name, *args = op
  if name not in OPS:
    raise ValueError(f"no change {name!r}")
  return getattr(store, name)(*args)

def change(cluster, fn):
  """Apply fn(store) to the categories as the file has them now and write them back; returns (store, fn's result)"""
  data = read_all()
  store = Store(cluster, data["clusters"].get(cluster))
  result = fn(store)
  data["clusters"][cluster] = store.to_data()
  write_all(data)
  log.info(f"categories of {cluster}: {len(store.paths)} categories, {len(store.jobs)} jobs")
  return store, result

# ---- what a category shows
def gpus(rec):
  """GPUs of a job (its AllocTRES: gres/gpu=4, or typed: gres/gpu:a100=4)"""
  tres = rec.get("AllocTRES") or ""
  plain = re.search(r"(?:^|,)gres/gpu=(\d+)", tres)
  if plain:
    return int(plain[1])
  return sum(int(n) for n in re.findall(r"(?:^|,)gres/gpu:[^=,]+=(\d+)", tres))

FAILED = {"BOOT_FAIL", "DEADLINE", "FAILED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "TIMEOUT"}

def state_group(state):
  s = (state or "").split()[0].rstrip("+")
  return ("running" if s in ("RUNNING", "COMPLETING") else "pending" if s in ("PENDING", "REQUEUED", "SUSPENDED")
          else "completed" if s == "COMPLETED" else "cancelled" if s == "CANCELLED" else "failed" if s in FAILED else "other")

def summary(jobs):
  """The numbers of a category from its jobs (Job objects; an array: all its tasks): counts by state, node, CPU and
  GPU hours, when, where"""
  from . import slurm as sl
  def hours(job):
    try:
      return sl.hours(job.rec.get("Elapsed") or "0")
    except ValueError:
      return 0.0
  states = Counter(state_group(j.state) for j in jobs)
  submits = [j.rec["Submit"] for j in jobs if re.match(r"\d{4}-", j.rec.get("Submit") or "")]
  starts = [j.rec["Start"] for j in jobs if re.match(r"\d{4}-", j.rec.get("Start") or "") and state_group(j.state) != "pending"]
  ends = [j.rec["End"] for j in jobs if re.match(r"\d{4}-", j.rec.get("End") or "")]
  ended = [j for j in jobs if state_group(j.state) in ("completed", "failed", "cancelled")]
  return {
    "jobs": len({array_of(j.jid) for j in jobs}),
    "tasks": len(jobs),
    "states": dict(states),
    "node_hours": sum(int(j.rec.get("NNodes") or 0) * hours(j) for j in jobs),
    "cpu_hours": sum(int(j.rec.get("AllocCPUS") or 0) * hours(j) for j in jobs),
    "gpu_hours": sum(gpus(j.rec) * hours(j) for j in jobs),
    "elapsed_hours": sum(hours(j) for j in jobs),
    "first_submit": min(submits) if submits else "",
    "first_start": min(starts) if starts else "",
    "last_end": max(ends) if ends else "",
    "success": (states["completed"] / len(ended)) if ended else None,
    "partitions": Counter(j.rec.get("Partition") or "?" for j in jobs),
    "accounts": Counter(j.rec.get("Account") or "?" for j in jobs),
    "users": Counter(j.rec.get("User") or "?" for j in jobs),
  }
