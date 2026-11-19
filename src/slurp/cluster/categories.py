"""Categories of jobs: a tree of names, like folders ("project X/production runs"). A job is in at most one; an array
is put in as a whole (its id), a single task can be too.

They are kept on the cluster, in ~/.config/slurp/categories.json of the home there (a local tab: this machine's
config directory), so slurp on your own machine and slurp on the login node see the same. One home is often shared
by several clusters (whose job ids repeat): the file has one part per Slurm cluster (its ClusterName).

Every change reads the file again, changes it and writes it back (another slurp may have changed it meanwhile); a
file that is not valid JSON is never overwritten.

Rules put jobs in a category by their name (a regular expression that matches the whole name: ci-[0-9]+), when the
job list loads. Each looks at a job once: it keeps the highest job id it has seen ("after"), so a job taken out of
its category by hand is not put back. What it takes (its scope): new jobs (submitted after it was made), the jobs in
no category (those of the job list then, and the new ones), or all (also moved out of other categories). The first
rule that matches a job (in their order) has it."""

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

SCOPES = {"new": "new jobs (submitted from now on)", "uncategorized": "the jobs in no category (now and new ones)",
          "all": "all jobs (also out of other categories, now and new ones)"}

def job_number(jid):
  """The number of a job or array ("123_4" -> 123), None if it is not one of the store's"""
  jid = array_of(jid)
  return int(jid) if jid.isdigit() else None

def rule_entry(entry):
  """A rule as kept: {id, pattern, category, scope, on, after, made}; ValueError if it is not one"""
  if not isinstance(entry, dict):
    raise ValueError("not a rule")
  rid = str(entry.get("id") or "")
  if not re.fullmatch(r"[0-9a-z]{1,16}", rid):
    raise ValueError(f"not the id of a rule: {rid!r}")
  pattern = str(entry.get("pattern") or "")
  if not pattern.strip():
    raise ValueError("give the rule a pattern of job names (ci-[0-9]+)")
  if len(pattern) > 200:
    raise ValueError("a pattern has at most 200 characters")
  try:
    re.compile(pattern)
  except re.error as e:
    raise ValueError(f"not a regular expression: {e}") from None
  scope = entry.get("scope") or "new"
  if scope not in SCOPES:
    raise ValueError(f"no scope {scope!r}")
  try:
    after = max(0, int(entry.get("after") or 0))
  except (TypeError, ValueError):
    after = 0
  return {"id": rid, "pattern": pattern, "category": normalize(entry.get("category")), "scope": scope,
          "on": entry.get("on", True) is not False, "after": after, "made": str(entry.get("made") or "")[:40]}

def rule_matches(rule, name):
  try:
    return re.fullmatch(rule["pattern"], name or "") is not None
  except re.error:
    return False

def rule_text(rule):
  return f"{rule['pattern']} → {rule['category']}"

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
    self.rules = []
    for entry in data.get("rules") or []:
      try:
        rule = rule_entry(entry)
      except ValueError:
        continue
      if not any(r["id"] == rule["id"] for r in self.rules):
        self.rules.append(rule)
        self.paths.update(lineage(rule["category"]))

  def to_data(self):
    return {"categories": sorted(self.paths), "jobs": dict(sorted(self.jobs.items())),
            **({"notes": dict(sorted(self.notes.items()))} if self.notes else {}),
            **({"job_notes": dict(sorted(self.job_notes.items(), key=lambda kv: [int(x) for x in kv[0].split("_")]))}
               if self.job_notes else {}),
            **({"cancelled": dict(sorted(self.cancelled.items(), key=lambda kv: [int(x) for x in kv[0].split("_")]))}
               if self.cancelled else {}),
            **({"rules": [dict(r) for r in self.rules]} if self.rules else {})}

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

  def rule(self, rid):
    return next((r for r in self.rules if r["id"] == rid), None)

  def rules_of(self, path):
    """The rules that put jobs in a category (not those of the categories in it)"""
    return [r for r in self.rules if r["category"] == path]

  def rule_count(self, rule, jobs):
    """What a rule makes of the jobs of the job list ([(jid, name)]; an array once): {match, there (in its category
    already), none (in no category), other (in another one)}"""
    out = {"match": 0, "there": 0, "none": 0, "other": 0}
    for jid in {array_of(j): n for j, n in jobs if job_number(j) is not None}.items():
      if rule_matches(rule, jid[1]):
        now = self.category_of(jid[0])
        out["match"] += 1
        out["there" if now == rule["category"] else "none" if not now else "other"] += 1
    return out

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

  def set_rule(self, entry):
    """Add a rule, or change the one with its id (in its place)"""
    rule = rule_entry(entry)
    self.paths.update(lineage(rule["category"]))
    for i, r in enumerate(self.rules):
      if r["id"] == rule["id"]:
        self.rules[i] = rule
        return rule["id"]
    self.rules.append(rule)
    return rule["id"]

  def drop_rule(self, rid):
    self.rules = [r for r in self.rules if r["id"] != rid]
    return rid

  def apply_rules(self, jobs):
    """The rules on the jobs of the job list ([(jid, name)]; an array as a whole): each job once per rule (the ones
    above its "after"), the first that matches has it. {path: [jids]} put in"""
    seen = {}
    for jid, name in jobs:
      if job_number(jid) is not None:
        seen.setdefault(array_of(jid), name)
    done = {}
    for jid, name in sorted(seen.items(), key=lambda kv: job_number(kv[0])):
      for rule in self.rules:
        if not rule["on"] or job_number(jid) <= rule["after"] or not rule_matches(rule, name):
          continue
        now = self.category_of(jid)
        if now != rule["category"] and (not now or rule["scope"] == "all"):
          self.assign([jid], rule["category"], {jid: name})
          done.setdefault(rule["category"], []).append(jid)
        break # (the first that matches has it, also when it is already there)
    top = max((job_number(j) for j in seen), default=0)
    for rule in self.rules:
      if rule["on"]:
        rule["after"] = max(rule["after"], top)
    return done

  def apply_rule(self, rid, jobs, move=False):
    """A rule now on the jobs of the job list ([(jid, name)]), whatever it has seen: the ones in no category, move:
    also those in another one. [jids] put in"""
    rule = self.rule(rid)
    if rule is None:
      raise ValueError("the rule is gone (deleted meanwhile?)")
    done = []
    for jid, name in {array_of(j): n for j, n in jobs if job_number(j) is not None}.items():
      now = self.category_of(jid)
      if rule_matches(rule, name) and now != rule["category"] and (not now or move):
        self.assign([jid], rule["category"], {jid: name})
        done.append(jid)
    return done

  def rules_due(self, jobs):
    """Would apply_rules change anything (a job to put in, an "after" to move)? (to write the file only then)"""
    numbers = [job_number(j) for j, _ in jobs]
    top = max((n for n in numbers if n is not None), default=0)
    return any(r["on"] and r["after"] < top for r in self.rules)

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
    for rule in self.rules:
      rule["category"] = moved(rule["category"])
    return new

  def delete(self, path):
    """Delete a category and the ones in it; their jobs go to the category it is in (none at the top)"""
    path = normalize(path)
    up = parent(path)
    self.paths = {p for p in self.paths if not within(p, path)}
    self.notes = {p: text for p, text in self.notes.items() if not within(p, path)}
    self.rules = [r for r in self.rules if not within(r["category"], path)] # (its rules go with it)
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

OPS = ("add", "assign", "unassign", "rename", "delete", "note", "job_note", "cancel_reason", "set_rule", "drop_rule", "apply_rules",
       "apply_rule") # (the changes, by name: kept to be done later, see jobcache)

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
