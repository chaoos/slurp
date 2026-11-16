"""The last job list of each tab, kept on this machine: browsing (and categorizing) works when the cluster cannot be
reached, a start shows the list at once (marked as possibly outdated) until the cluster answers.

~/.cache/slurp/joblists/<tab>.json (only for the user): the jobs as Slurm last reported them (accounting, steps, the
command, the error file), when, the finished-since and scope of that load; the categories of the cluster as they were
then, and the changes to them made while the cluster could not be reached (written to it with the next load)."""

import json
import os
import re
import tempfile
import time

from .jobs import Job, JobProgress
from ..state import log

VERSION = 1

def cache_dir():
  return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "slurp", "joblists")

def path_of(cluster):
  name = cluster.destination or "local"
  return os.path.join(cache_dir(), re.sub(r"[^\w.@-]", "_", name)[:120] + ".json")

def job_data(job):
  progress = job.progress
  return {"rec": job.rec, "steps": job.steps, "queued": job.queued, "reason": job.reason, "command": job.command,
          "err": list(job.err), "script": job.script,
          "progress": None if progress is None else {"current": progress.current, "total": progress.total, "line": progress.line}}

def job_from(data):
  p = data.get("progress")
  return Job(rec=dict(data["rec"]), steps=list(data.get("steps") or []), queued=bool(data.get("queued")),
             reason=data.get("reason") or "", command=data.get("command") or "", err=tuple(data.get("err") or (None, 0)),
             script=data.get("script"), annotated=True,
             progress=JobProgress(int(p["current"]), int(p["total"]), p.get("line") or "") if p else None)

def save(cluster, jobs=None):
  """Write the tab's list (jobs: the list just loaded; default: what the tab has) and its categories"""
  jobs = list(cluster.jobs.values()) if jobs is None else jobs
  data = {"version": VERSION, "saved": time.time(),
          "updated": cluster.updated.isoformat() if cluster.updated else None,
          "since": cluster.since.isoformat() if cluster.since else None, "since_value": cluster.since_value,
          "scope": cluster.scope, "slurm_cluster": cluster.slurm_cluster,
          "categories": cluster.categories.to_data() if cluster.categories is not None else None,
          "pending": cluster.pending_categories,
          "jobs": [job_data(j) for j in jobs]}
  path = path_of(cluster)
  try:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".joblist.")
    with os.fdopen(fd, "w") as f: # (mkstemp: only for the user)
      json.dump(data, f)
    os.replace(tmp, path)
  except (OSError, TypeError, ValueError) as e:
    log.warning(f"the job list of {cluster.name} cannot be kept: {e}")

def save_pending(cluster):
  """Only the categories (and their changes not yet on the cluster): the jobs as they are in the file"""
  try:
    with open(path_of(cluster)) as f:
      data = json.load(f)
  except (OSError, ValueError):
    data = None
  if not isinstance(data, dict) or data.get("version") != VERSION:
    save(cluster)
    return
  data["categories"] = cluster.categories.to_data() if cluster.categories is not None else None
  data["pending"] = cluster.pending_categories
  data["slurm_cluster"] = cluster.slurm_cluster
  path = path_of(cluster)
  try:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".joblist.")
    with os.fdopen(fd, "w") as f:
      json.dump(data, f)
    os.replace(tmp, path)
  except OSError as e:
    log.warning(f"the categories of {cluster.name} cannot be kept here: {e}")

def load(cluster):
  """Give the tab its kept list (if there is one and the tab has none yet); True if it did"""
  from datetime import datetime
  from .categories import Store
  try:
    with open(path_of(cluster)) as f:
      data = json.load(f)
  except (OSError, ValueError):
    return False
  if not isinstance(data, dict) or data.get("version") != VERSION:
    return False
  try:
    jobs = [job_from(j) for j in data.get("jobs") or []]
  except (KeyError, TypeError, ValueError) as e:
    log.warning(f"the kept job list of {cluster.name} is not usable: {e}")
    return False
  if cluster.loaded: # (a load was faster)
    return False
  cluster.jobs = {j.jid: j for j in jobs}
  cluster.updated = datetime.fromisoformat(data["updated"]) if data.get("updated") else None
  cluster.since = datetime.fromisoformat(data["since"]) if data.get("since") else None
  cluster.loaded, cluster.from_cache = True, True
  if cluster.slurm_cluster is None:
    cluster.slurm_cluster = data.get("slurm_cluster")
  if cluster.categories is None and data.get("categories") is not None:
    cluster.categories = Store(cluster.slurm_cluster or "default", data["categories"])
  cluster.pending_categories = [p for p in data.get("pending") or [] if isinstance(p, list) and p]
  return True
