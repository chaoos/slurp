"""slurp without a frontend: tabs (ClusterState), their job lists as plain data, transfers, and changes pushed to
subscribers. What tests and a web frontend need of the core; it grows with them. Imports no Textual.

  session = open_session("user@cluster")     # or open_session(None): this machine
  rows = load_jobs(session)                  # the job list as dicts (arrays collapsed as in the job list)
  subscribe(session, "jobs", print)          # topics: jobs, connection, transfers
  t = start_transfer(session, ["/data/run1"], other, "/scratch/in")

The command line: python -m slurp.headless --jobs [user@]host  prints the job list as JSON."""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

from .cluster import jobcache, remote
from .cluster.jobs import annotate, array_task_id, collapse_arrays, job_id_key, load_jobs as slurm_jobs, slurm_accounts
from .cluster.workers import ThreadScheduler
from .state import ClusterState, is_local_destination, log

TOPICS = ("jobs", "connection", "transfers")


class Hub:
  """What the core needs of an app, without one: the tabs, notifications (kept, and logged), results of threads
  (run at once, one at a time), the subscribers of each tab and topic. The transfer manager runs on it."""

  def __init__(self):
    self.clusters = []
    self.lock = threading.RLock()
    self.notices = []                  # (title, message, severity)
    self.subscribers = {}              # (id of a tab or None, topic) -> [fn]
    self.scheduler = ThreadScheduler(deliver_inline=True, on_error=lambda tab, message: self.notify(message, title=tab, severity="error"))
    self.watching = False
    self._transfers = None

  # ---- what the core calls (the transfer manager: clusters, notify, call_from_thread, login, needs_login, login_visible)
  def notify(self, message, title="", severity="information", timeout=None):
    with self.lock:
      self.notices.append((title, str(message), severity))
    (log.warning if severity in ("warning", "error") else log.info)(f"{title}: {message}")

  def call_from_thread(self, fn, *args):
    with self.lock:
      return fn(*args)

  def login(self, cluster, show=False):
    connect(cluster)

  def needs_login(self, cluster):
    return bool(cluster.destination) and not (cluster.connection and cluster.connection.ready)

  def login_visible(self, cluster):
    return False

  # ---- subscribers
  def subscribe(self, cluster, topic, fn):
    if topic not in TOPICS:
      raise ValueError(f"no topic {topic!r} (one of {', '.join(TOPICS)})")
    with self.lock:
      self.subscribers.setdefault((id(cluster) if cluster is not None else None, topic), []).append(fn)
    if topic == "connection":
      self.watch()
    if topic == "transfers":
      self.transfers()
    return lambda: self.unsubscribe(cluster, topic, fn)

  def unsubscribe(self, cluster, topic, fn):
    with self.lock:
      fns = self.subscribers.get((id(cluster) if cluster is not None else None, topic), [])
      if fn in fns:
        fns.remove(fn)

  def publish(self, cluster, topic, data):
    with self.lock:
      fns = [*self.subscribers.get((id(cluster), topic), []), *self.subscribers.get((None, topic), [])]
    for fn in fns:
      try:
        fn(data)
      except Exception:
        log.exception(f"a subscriber of {topic} failed")

  # ---- the connection of each tab: its state, pushed when it changes
  def watch(self):
    with self.lock:
      if self.watching:
        return
      self.watching = True
    threading.Thread(target=self.watcher, daemon=True, name="headless connections").start()

  def watcher(self):
    seen = {}
    while True:
      for cluster in list(self.clusters):
        now = connection_state(cluster)
        if seen.get(id(cluster)) != now:
          seen[id(cluster)] = now
          self.publish(cluster, "connection", now)
      time.sleep(0.2)

  # ---- transfers
  def transfers(self):
    """The transfer manager (made on first use; it ticks every second, on its own thread)"""
    with self.lock:
      if self._transfers is None:
        from .transfer.manager import Manager
        self._transfers = Manager(self)
        self._transfers.listeners.append(lambda t: self.publish(None, "transfers", transfers_data(self._transfers, t)))
        threading.Thread(target=self.ticker, daemon=True, name="headless transfers").start()
      return self._transfers

  def ticker(self):
    while True:
      time.sleep(1)
      try:
        with self.lock:
          self._transfers.tick()
      except Exception:
        log.exception("the transfers' tick failed")


HUB = Hub()


# ---------------------------------------------------------------------------
# tabs

def open_session(destination=None, ssh=None, timeout=60, hub=None):
  """A tab, connected: destination None (or localhost): this machine; else an SSH destination (ssh: the full settings
  as sshsetup has them, else the ssh config's). Raises ConnectionError when the login fails. The login asks nothing:
  a key without a passphrase, an agent, or the ssh config's own way."""
  hub = hub or HUB
  local = not destination or is_local_destination(destination)
  cluster = ClusterState("Local" if local else destination, None if local else destination, ssh=ssh)
  with hub.lock:
    hub.clusters.append(cluster)
  if not local:
    connect(cluster, timeout)
  return cluster

def ssh_options(cluster):
  if not cluster.ssh:
    return []
  from .cluster import sshsetup
  return sshsetup.ssh_args(cluster.ssh)

def connect(cluster, timeout=60):
  """Log in (again) to the tab's cluster; its login's terminal is a pty of its own, whose output goes to the log"""
  conn = cluster.connection
  if conn is None:
    conn = cluster.connection = remote.RemoteSSH(cluster.destination, ssh_options(cluster), cluster.pre_login, cluster.pre_login_fallback)
  elif conn.state in ("ready", "connecting"):
    return
  master, slave = os.openpty()
  def drain():
    while True:
      try:
        data = os.read(master, 4096)
      except OSError:
        break
      if not data:
        break
      log.debug(f"{cluster.name} login: {data.decode(errors='replace').strip()}")
  threading.Thread(target=drain, daemon=True, name=f"login {cluster.name}").start()
  conn.spawn(slave)
  os.close(slave)
  end = time.monotonic() + timeout
  while time.monotonic() < end and conn.state == "connecting":
    time.sleep(0.05)
  if not conn.ready:
    raise ConnectionError(f"no login to {cluster.destination}: {conn.error or conn.state}")
  cluster.broken_at = None

def disconnect(cluster):
  """End the session of the tab (as a dropped connection: connect() logs in again)"""
  if cluster.connection is not None:
    cluster.connection.kill_session()
    end = time.monotonic() + 10
    while time.monotonic() < end and cluster.connection.state not in ("broken", "closed"):
      time.sleep(0.05)
    cluster.broken_at = time.time()

def connection_state(cluster):
  c = cluster.connection
  if not cluster.destination:
    return {"tab": cluster.name, "state": "local"}
  if c is None:
    return {"tab": cluster.name, "state": "new"}
  return {"tab": cluster.name, "state": c.state, "user": c.user, "host": c.hostname, "error": c.error,
          "latency": c.latency}


# ---------------------------------------------------------------------------
# the job list

def run_in(cluster, work, name, hub=None, timeout=600):
  """work() in a worker bound to the tab's connection; its result (an error: raised)"""
  hub = hub or HUB
  if cluster.destination and not (cluster.connection and cluster.connection.ready):
    raise ConnectionError(f"not connected to {cluster.destination}")
  box = {}
  def body():
    try:
      box["result"] = work()
    except remote.Cancelled:
      raise
    except BaseException as e:
      box["error"] = e
  worker = remote.start_worker(hub.scheduler, body, f"{name}-{id(cluster)}", connection=cluster.connection if cluster.destination else None,
                               name=f"{name} of {cluster.name}")
  worker.wait(timeout)
  if worker.is_running:
    worker.abort.set()
    raise TimeoutError(f"{name} of {cluster.name}: no answer within {timeout} s")
  if "error" in box:
    raise box["error"]
  return box.get("result")

def load_jobs(cluster, since=None, scope=None, checks=True, hub=None):
  """Load the tab's job list (queued jobs, and the ones that ended after since: a datetime, or a duration such as
  "1d"; default: the tab's since_value) as the job list does: their files (command, error file, progress), the
  checks (checks=True). Kept for a load without the cluster (load_cached). The rows, published to "jobs"."""
  hub = hub or HUB
  from .cluster.slurm import parse_duration
  scope = scope or cluster.scope
  if since is None:
    since = cluster.since_value
  if isinstance(since, str):
    since = None if since == "off" else parse_duration(since)
  def work():
    accounts = cluster.accounts if cluster.accounts is not None else slurm_accounts()
    jobs = slurm_jobs(since, scope, accounts)
    annotate(jobs)
    if checks:
      from .checks.engine import check_jobs
      check_jobs(jobs)
    return jobs, accounts
  cluster.load_started = time.monotonic()
  jobs, cluster.accounts = run_in(cluster, work, "jobs", hub)
  cluster.jobs = {j.jid: j for j in jobs}
  cluster.since, cluster.scope, cluster.updated = since, scope, datetime.now()
  cluster.loaded, cluster.from_cache = True, False
  jobcache.save(cluster, jobs)
  rows = job_rows(cluster)
  hub.publish(cluster, "jobs", rows)
  return rows

def load_cached(cluster, hub=None):
  """The tab's job list as kept by its last load (on this machine; the cluster need not answer): the rows, or None"""
  hub = hub or HUB
  cluster.loaded = False
  if not jobcache.load(cluster):
    return None
  rows = job_rows(cluster)
  hub.publish(cluster, "jobs", rows)
  return rows

def job_rows(cluster, collapse=True, expanded=()):
  """The rows of the tab's job list as dicts, in its order (newest first): a job, or an array with
  several tasks in the list as one row (collapse; expanded: "array id" of rows shown with their tasks)"""
  ordered = sorted(cluster.jobs.values(), key=lambda j: job_id_key(j.jid), reverse=True) # (as the job list: newest first)
  items = collapse_arrays(ordered, expanded) if collapse else ordered
  return [array_data(*item, cluster) if isinstance(item, tuple) else job_data(item, cluster) for item in items]

def job_data(job, cluster=None):
  """One job as the job list shows it, as data"""
  from .checks.engine import counts
  rec, p = job.rec, job.progress
  checks = counts(job.checks) if job.checks is not None else None
  return {"kind": "job", "id": job.jid, "array": array_task_id(job) is not None and job.jid.partition("_")[0] or None,
          "name": rec.get("JobName", ""), "state": job.state, "reason": job.reason, "queued": job.queued,
          "running": job.running, "ended": not job.queued, "user": rec.get("User", ""), "account": rec.get("Account", ""),
          "partition": rec.get("Partition", ""), "nodes": rec.get("NNodes", ""), "nodelist": rec.get("NodeList", ""),
          "submit": rec.get("Submit", ""), "start": rec.get("Start", ""), "end": rec.get("End", ""),
          "elapsed": rec.get("Elapsed", ""), "timelimit": rec.get("Timelimit", ""), "exit": rec.get("ExitCode", ""),
          "workdir": rec.get("WorkDir", ""), "command": job.command, "error_file": list(job.err) if job.err else None,
          "progress": None if p is None else {"current": p.current, "total": p.total, "percent": p.percent, "line": p.line},
          "estimated_end": estimated_end(job), "checks": checks,
          "from_cache": bool(cluster is not None and cluster.from_cache)}

def array_data(array_id, tasks, cluster=None):
  """An array as one row: its tasks by state"""
  states = {}
  for t in tasks:
    states[t.state] = states.get(t.state, 0) + 1
  return {"kind": "array", "id": array_id, "name": tasks[0].rec.get("JobName", ""), "tasks": [t.jid for t in tasks],
          "states": states, "queued": any(t.queued for t in tasks), "running": any(t.running for t in tasks),
          "user": tasks[0].rec.get("User", ""), "account": tasks[0].rec.get("Account", ""),
          "from_cache": bool(cluster is not None and cluster.from_cache)}

def estimated_end(job):
  """The end of a running job: its start plus its time limit (an ISO time), else None"""
  from .cluster.slurm import hours
  if not job.running:
    return None
  try:
    return (datetime.fromisoformat(job.rec.get("Start", "")) + timedelta(hours=hours(job.rec.get("Timelimit", "")))).isoformat()
  except (TypeError, ValueError, AttributeError):
    return None


# ---------------------------------------------------------------------------
# transfers

def start_transfer(source, paths, dest, dest_path, name="", options=None, hub=None):
  """A transfer from paths on the tab source into the folder dest_path on the tab dest, through slurp (the relay: it
  works when neither cluster reaches the other), started now; its data. Its progress: subscribe(None, "transfers")."""
  hub = hub or HUB
  from .transfer import endpoints as E, model as M
  def ref(cluster):
    return {"endpoint": E.LOCAL} if not cluster.destination else {"endpoint": f"tab:{cluster.destination}", "tab": M.tab_ref(cluster)}
  src, dst = ref(source), ref(dest)
  src["paths"], dst["path"] = list(paths), dest_path
  t = M.Transfer(M.new_id(), name or M.default_name(paths, src, dst), src, dst, route="relay", runs_on="slurp", options=dict(options or {}))
  manager = hub.transfers()
  with hub.lock:
    manager.add(t)
    manager.start(t)
  return transfer_data(t)

def transfer_data(t):
  return {"id": t.id, "name": t.name, "state": t.state, "message": t.message, "route": t.route,
          "progress": {k: v for k, v in (t.progress or {}).items() if k != "history"}}

def transfers_data(manager, t=None):
  """What changed: one transfer, or (t None) all of them"""
  return [transfer_data(t)] if t is not None else [transfer_data(x) for x in manager.transfers]


# ---------------------------------------------------------------------------

def subscribe(cluster, topic, fn, hub=None):
  """fn(data) whenever the topic changes for the tab (cluster None: for every tab): "jobs" (the rows, after a load),
  "connection" (the state of its connection), "transfers" (the transfers that changed). Returns a function that ends it."""
  return (hub or HUB).subscribe(cluster, topic, fn)


def main(argv=None):
  parser = argparse.ArgumentParser(prog="python -m slurp.headless", description="slurp's core without a frontend")
  parser.add_argument("--jobs", metavar="DESTINATION", nargs="?", const="", help="print the job list of [user@]host (none: this machine) as JSON")
  parser.add_argument("--since", default="1d", help="finished jobs within this time (e.g. 1d, 2w; off: none)")
  parser.add_argument("--scope", default="me", help="me, all, or an account")
  parser.add_argument("--no-checks", action="store_true", help="without the checks of the jobs")
  parser.add_argument("--cached", action="store_true", help="the list kept by the last load (when the cluster cannot be reached)")
  args = parser.parse_args(argv)
  if args.jobs is None:
    parser.error("nothing to do (--jobs)")
  try:
    cluster = open_session(args.jobs or None) if not args.cached else ClusterState(args.jobs or "Local", args.jobs or None)
    rows = load_cached(cluster) if args.cached else load_jobs(cluster, since=args.since, scope=args.scope, checks=not args.no_checks)
  except (ConnectionError, RuntimeError, TimeoutError, OSError) as e:
    print(f"slurp: {e}", file=sys.stderr)
    return 1
  json.dump(rows, sys.stdout, indent=1, default=str)
  print()
  return 0


if __name__ == "__main__":
  sys.exit(main())
