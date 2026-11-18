"""The transfers of the app: it runs them (the relay in slurp; detached runs and transfer jobs on a cluster), follows
them (their progress, their end), pauses and continues them, and runs their schedules.

It needs of the app: clusters (the tabs), notify(message, title=, severity=, timeout=), call_from_thread(fn, *args),
login(cluster, show=False) and needs_login(cluster). Everything that waits for a cluster runs in threads of its own;
tick() (every second, on the UI thread) only looks at the state."""

import threading
import time

from ..state import log
from . import model as M, plan as P, relay as RL, rsync as R, runner as RN, schedule as S
from .endpoint import Endpoint, NotConnected

POLL_SHOWN = 3      # seconds between looks at a run on a cluster while its transfer is shown
POLL_HIDDEN = 15    # … while it is not
LOGIN_WAIT = 120    # seconds a scheduled run waits for the logins it started before it says so
RATE_POINTS = 120   # points of the rate kept for the sparkline
JOB_WATCH = 30      # seconds between looks at the job a transfer waits for

def client_side(route):
  """Which end runs rsync: "source" or "dest" """
  return "dest" if route == "direct-pull" else "source"

class Manager:
  def __init__(self, app, store=None):
    self.app = app
    self.store = store or M.Store()
    self.relays = {}        # id -> Relay (runs in slurp)
    self.followed = {}      # id -> next time to look at its run on the cluster
    self.polling = set()    # ids being looked at now
    self.shown = set()      # ids whose details (or the list) are shown: looked at more often
    self.listeners = []     # fn(transfer or None) on the UI thread when something changed
    self.login_started = {} # id -> when the logins for a scheduled run started
    self.job_watch = {}     # id -> next look at the job it waits for
    self.lock = threading.RLock()
    for t in self.store.transfers: # (runs that went on without slurp: followed again)
      if t.run and t.runs_on in ("detached", "job"):
        self.followed[t.id] = 0
      if S.repeated(t.schedule) and t.next_run is None and t.state not in M.ACTIVE:
        t.next_run = S.first_time(t.schedule)

  # ---- the transfers
  @property
  def transfers(self):
    return self.store.transfers

  def get(self, tid):
    return self.store.get(tid)

  def ends(self, t):
    """(source tab, destination tab), None for one that is not open"""
    clusters = self.app.clusters
    return M.find_tab(clusters, t.source.get("tab")), M.find_tab(clusters, t.dest.get("tab"))

  def endpoints(self, t):
    src, dst = self.ends(t)
    if src is None or dst is None:
      missing = M.tab_name(t.source.get("tab")) if src is None else M.tab_name(t.dest.get("tab"))
      raise NotConnected(f"the tab {missing} is not open")
    return Endpoint(src), Endpoint(dst)

  def not_ready(self, t):
    """The ends that are not connected: [cluster or None (not open)]"""
    src, dst = self.ends(t)
    return [c for c in (src, dst) if c is None or (c.destination and not (c.connection and c.connection.ready))]

  def changed(self, t=None, save=True, progress_only=False):
    if save:
      self.store.save(progress_only=progress_only)
    for fn in list(self.listeners):
      try:
        fn(t)
      except Exception as e:
        log.debug(f"transfer listener: {e!r}")

  def from_thread(self, fn, *args):
    try:
      self.app.call_from_thread(fn, *args)
    except RuntimeError: # (the app is gone)
      pass

  def say(self, t, message, severity="information", timeout=8):
    self.app.notify(message, title=t.name, severity=severity, timeout=timeout)

  def add(self, t):
    self.store.add(t)
    self.changed(t)

  def delete(self, t):
    if t.active:
      raise ValueError("pause it first")
    self.store.remove(t)
    self.followed.pop(t.id, None)
    self.changed(None)

  # ---- starting a run
  def go(self, t):
    """The wizard's Go (and Continue, Run now): now, or by its schedule"""
    kind = (t.schedule or {}).get("kind", "now")
    if kind in ("at", "daily") and not t.run:
      t.next_run = S.first_time(t.schedule)
      if t.runs_on == "job" and kind == "at": # (a single run later: the job is submitted now, with --begin)
        self.start(t, begin=t.next_run)
        t.next_run = None
        return
      t.state, t.message = "scheduled", S.describe(t.schedule, t.next_run)
      self.changed(t)
      return
    if kind == "after_job":
      t.state, t.message = "scheduled", S.describe(t.schedule)
      self.job_watch[t.id] = 0
      self.changed(t)
      return
    if kind == "every" and t.next_run is None:
      t.next_run = S.after_run(t.schedule, time.time())
    self.start(t)

  def start(self, t, begin=None, scheduled=False):
    """A run now (begin: a transfer job that may start then). Not connected ends: it waits for them (a scheduled
    run logs in to them if its schedule says so)"""
    if t.active:
      return
    missing = self.not_ready(t)
    if missing:
      names = ", ".join(c.name if c else "a closed tab" for c in missing)
      t.state, t.message = "waiting", f"waiting for {names} (not connected)"
      t.pending = {"begin": begin, "scheduled": scheduled}
      if scheduled and (t.schedule or {}).get("login", True):
        for c in missing:
          if c is not None and self.app.needs_login(c):
            self.app.login(c, show=False)
        self.login_started.setdefault(t.id, time.time())
      self.changed(t)
      return
    t.pending = None
    self.login_started.pop(t.id, None)
    t.state, t.message, t.errors = "starting", "", []
    total = t.plan.get("remaining_bytes") if not t.runs else None
    last = t.runs[-1] if t.runs else {}
    if last.get("result") == "paused" and t.progress.get("total"): # (going on: what was left)
      total = max(t.progress["total"] - t.progress.get("bytes", 0), 0) or None
    t.progress = {"total": total, "files_total": t.plan.get("remaining_files") if not t.runs else None, "history": []}
    t.run = {"kind": t.runs_on, "started": time.time(), "route": t.route}
    t.event(f"run started ({M.ROUTES.get(t.route, t.route)}, {M.RUNS_ON.get(t.runs_on, t.runs_on)})")
    self.changed(t)
    target = self.run_relay if t.runs_on == "slurp" else self.run_on_cluster
    threading.Thread(target=target, args=(t, begin), daemon=True, name=f"transfer {t.name}").start()

  # ---- the relay (in slurp)
  def run_relay(self, t, begin=None):
    try:
      source, dest = self.endpoints(t)
      facts = P.facts(source)
      relay = RL.Relay(source, dest, t.source["paths"], t.dest["path"], t.options, facts.get("version")).start()
    except Exception as e:
      self.from_thread(self.ended, t, None, f"the run did not start: {e}")
      return
    self.relays[t.id] = relay
    self.from_thread(self.set_state, t, "running")
    while relay.poll() is None:
      time.sleep(1)
      self.from_thread(self.relay_progress, t, relay)
    code = relay.wait(30)
    self.relays.pop(t.id, None)
    self.from_thread(self.relay_progress, t, relay)
    self.from_thread(self.relay_ended, t, relay, code)

  def set_state(self, t, state, message=None):
    if t.state in ("pausing",) and state == "running":
      return
    t.state = state
    if message is not None:
      t.message = message
    self.changed(t)

  def relay_progress(self, t, relay):
    with relay.lock:
      p, out = dict(relay.output.progress), relay.output
      current, errors, error_count, files = out.current, list(out.errors), out.error_count, out.files
    self.progress(t, p, current=current, files=files, errors=errors, error_count=error_count, transport=relay.rate)

  def relay_ended(self, t, relay, code):
    message = relay.error
    if code not in (0, 23, 24) and not relay.stopped and not message:
      message = "; ".join(relay.output.errors[-2:])
    lost = not relay.stopped and code in (12, 255, None) and self.not_ready(t)
    if lost: # (a tab's connection broke: the run goes on when it is back)
      t.event(f"the connection broke ({message or code}): going on when both are connected")
      M.record_run(t, "interrupted", message or f"rsync ended with {code}")
      t.state, t.message = "waiting", "the connection broke: it goes on when both tabs are connected"
      t.pending = {"begin": None, "scheduled": False, "resume": True}
      self.changed(t)
      return
    self.ended(t, code, message, stats=relay.output.stats, stopped=relay.stopped)

  # ---- runs on a cluster (detached, transfer job)
  def client(self, t):
    source, dest = self.endpoints(t)
    return source if client_side(t.route) == "source" else dest

  def run_on_cluster(self, t, begin=None):
    try:
      source, dest = self.endpoints(t)
      client = source if client_side(t.route) == "source" else dest
      facts = P.facts(client)
      if not facts.get("version"):
        raise OSError(f"no rsync on {client.name}")
      target, known, key = None, "", False
      if t.route in ("direct-push", "direct-pull"):
        src, dst = self.ends(t)
        target = P.target_of(dst if t.route == "direct-push" else src)
        if not target or target.get("error"):
          raise OSError((target or {}).get("error") or "the other side cannot be reached")
        known = target.pop("known_hosts", "")
        key = bool(t.options.get("temp_key"))
      folder = RN.folder_of(client, t)
      text = RN.command(t, t.route, folder, facts["version"], target, key=key)
      folder = RN.prepare(client, t, text, job=t.runs_on == "job", known_hosts=known)
      if key:
        self.install_key(t, client, source if client is dest else dest, folder)
      run = {"folder": folder, "client": client_side(t.route)}
      if t.runs_on == "job":
        run["job"] = RN.submit(client, folder, begin)
        state = "queued"
        message = f"transfer job {run['job']}" + (f", starts at {S.when_text(begin)}" if begin else "")
      else:
        run["host"] = RN.start_detached(client, folder)
        state, message = "running", f"on {run['host'] or client.name}"
    except Exception as e:
      log.debug(f"transfer {t.name}: {e!r}")
      self.from_thread(self.ended, t, None, f"the run did not start: {e}")
      return
    def started():
      t.run.update(run)
      t.event(message)
      self.set_state(t, state, message)
      self.followed[t.id] = time.monotonic() + 2
    self.from_thread(started)

  def install_key(self, t, client, server, folder):
    """The temporary key (opt-in): made on the cluster that runs rsync, allowed on the other one for a while"""
    public = RN.make_key(client, folder, t)
    hours = 12 + 3 * ((t.plan.get("routes", {}).get(t.route, {}).get("estimate") or [0, 0])[1] / 3600)
    if t.runs_on == "job":
      hours += 24 # (it may wait in the queue)
    RN.install_key(server, RN.key_line(public, t, min(hours, 24 * 14)), t)
    t.event(f"temporary key installed on {server.name} (for {hours:.0f} h)")

  def remove_key(self, t):
    if not t.options.get("temp_key") or t.route not in ("direct-push", "direct-pull"):
      return
    def work():
      try:
        source, dest = self.endpoints(t)
        server = dest if t.route == "direct-push" else source
        if server.ready and RN.remove_key(server, t):
          self.from_thread(t.event, f"temporary key removed from {server.name}")
      except Exception as e:
        log.debug(f"removing the key of {t.name}: {e!r}")
    threading.Thread(target=work, daemon=True).start()

  def poll(self, t):
    """Look at a run on a cluster (in a thread)"""
    try:
      client = self.client(t)
      if not client.ready:
        raise NotConnected(f"{client.name} is not connected")
      status = RN.status(client, t.run.get("folder", ""), t.run.get("job", ""))
    except NotConnected as e:
      self.from_thread(self.unreachable, t, str(e))
      return
    except Exception as e:
      log.debug(f"looking at {t.name}: {e!r}")
      self.from_thread(self.unreachable, t, f"no answer: {e}")
      return
    finally:
      self.polling.discard(t.id)
    self.from_thread(self.polled, t, status)

  def unreachable(self, t, why):
    if t.state not in ("pausing",):
      t.message = f"{why}: it runs on, slurp shows it again when it can look"
    self.changed(t, save=False)

  def polled(self, t, status):
    if not t.run:
      return
    out = R.Output()
    out.feed(status.get("log", ""))
    out.close()
    p = RN.progress_of(status.get("progress"))
    if p:
      if p.get("phase") in ("copying", "ended", "scanning") and t.state in ("queued", "starting", "waiting"):
        t.state = "running"
        t.message = f"on {status['lock'].split()[1]}" if len(status.get("lock", "").split()) > 1 else ""
      self.progress(t, {"bytes": p.get("bytes", 0), "percent": p.get("percent", 0), "rate": p.get("rate", 0.0),
                        "eta": p.get("rsync_eta"), **({"left": p["left"], "checked": p["checked"]} if "left" in p else {})},
                    current=p.get("current", ""), files=p.get("files", 0), errors=out.errors, error_count=out.error_count)
    job = status.get("job", "")
    if job and t.state == "queued":
      t.message = f"transfer job {t.run.get('job')}: {job.lower()}"
    result = status.get("result")
    ended = result is not None and not status.get("lock")
    if t.runs_on == "job" and not job and result is None and not status.get("lock"):
      ended = True # (the job is gone without a result: cancelled, its time was up, its node failed)
    if t.runs_on == "detached" and result is None and not status.get("lock") and not status.get("alive") and t.state != "starting":
      ended = True # (killed without a word)
    if not ended:
      self.changed(t, save=False)
      return
    code = result.get("exit") if result else None
    self.followed.pop(t.id, None)
    stopped = t.state == "pausing"
    if t.runs_on == "job" and not stopped and t.options.get("resubmit") and (code in (20, None)):
      moved = t.progress.get("bytes", 0) > 0
      t.event(f"the transfer job {t.run.get('job')} ended (its time was up?)" + (": submitting the next one" if moved else ""))
      M.record_run(t, "interrupted", f"job {t.run.get('job', '')} ended before the transfer did")
      if moved:
        t.state = "new"
        self.changed(t)
        self.start(t)
        return
    message = "" if result else "the run ended without a result" + (" (the job was cancelled, or its time was up)" if t.runs_on == "job" else "")
    self.ended(t, code, message, stats=out.stats, stopped=stopped)

  # ---- the progress of a run
  def progress(self, t, p, current="", files=0, errors=(), error_count=0, transport=None):
    if not p and not current:
      return
    prog = t.progress
    if t.state == "starting":
      t.state = "running"
    done = p.get("bytes", prog.get("bytes", 0))
    prog.update(bytes=done, rsync_percent=p.get("percent"), rate=p.get("rate", 0.0), current=current or prog.get("current", ""),
                files=files, errors=error_count, updated=time.time())
    if transport:
      prog["transport"] = transport
    if "left" in p:
      prog["left"], prog["checked"] = p["left"], p["checked"]
    history = prog.setdefault("history", [])
    history.append([round(time.time(), 1), done])
    del history[:-RATE_POINTS]
    total = prog.get("total")
    if total:
      prog["percent"] = min(100.0, 100.0 * done / total) if total else 0.0
    elif p.get("percent") is not None:
      prog["percent"] = float(p["percent"])
    rate = average_rate(history) or prog.get("rate") or 0
    if total and rate:
      prog["eta"] = max(total - done, 0) / rate
    elif p.get("eta") is not None:
      prog["eta"] = p["eta"]
    t.errors = list(errors)[-200:]
    self.changed(t, progress_only=True)

  # ---- the end of a run
  def ended(self, t, code, message="", stats=None, stopped=False):
    state, words = R.outcome(code, stopped) if code is not None else ("paused" if stopped else "failed", message or "")
    if message and state not in ("paused",):
      words = f"{words}; {message}" if words and message not in words else message
    if stats:
      t.progress["stats"] = stats
    entry = M.record_run(t, state, words)
    self.followed.pop(t.id, None)
    t.state, t.message = state, words
    t.event(f"run ended: {M.STATES.get(state, state)}" + (f" ({words})" if words else ""))
    self.remove_key(t)
    if state in ("done", "done-errors") and t.progress.get("total"):
      t.progress["percent"] = 100.0
    nxt = t.next_run
    if state in ("done", "done-errors") and S.repeated(t.schedule) and nxt:
      t.state, t.message = "scheduled", f"{M.STATES[state]}; next run {S.when_text(nxt)}"
    self.changed(t)
    severity = {"done": "information", "done-errors": "warning", "paused": "information"}.get(state, "error")
    self.say(t, {"done": "done", "done-errors": f"done, with errors ({t.progress.get('errors', 0)} files)",
                 "paused": "paused", "failed": f"failed: {words}"}.get(state, state), severity, 10 if severity != "information" else 6)
    return entry

  # ---- pause, continue
  def pause(self, t):
    """Stop the run as a pause (a transfer job is cancelled: the UI warns first); a scheduled one: off schedule"""
    if t.state in ("scheduled", "waiting") and not t.run:
      t.state, t.message, t.next_run, t.pending = "paused", "paused (its schedule waits)", None, None
      self.job_watch.pop(t.id, None)
      self.changed(t)
      return
    if not t.active:
      return
    prev, t.state = t.state, "pausing"
    t.message = "pausing …"
    self.changed(t)
    relay = self.relays.get(t.id)
    if relay is not None:
      threading.Thread(target=relay.stop, daemon=True).start()
      return
    if prev == "starting":
      return # (it starts and is stopped once it has its handles)
    def work():
      try:
        client = self.client(t)
        if t.runs_on == "job" and t.run.get("job"):
          result = client.run(["scancel", t.run["job"]], timeout=60, retry=False)
          if result.returncode:
            raise OSError((result.stderr or "").strip()[:200] or "scancel failed")
        else:
          lock = RN.status(client, t.run.get("folder", "")).get("lock", "")
          if not RN.stop(client, lock):
            raise OSError("the run could not be reached (another login node?)")
      except Exception as e:
        self.from_thread(self.say, t, f"pausing did not work: {e}", "error")
        self.from_thread(self.set_state, t, prev)
        return
      self.followed[t.id] = 0
    threading.Thread(target=work, daemon=True).start()

  def resume(self, t):
    """Continue: a new run (rsync goes on where it stopped)"""
    if t.active:
      return
    if S.repeated(t.schedule) and t.next_run is None:
      t.next_run = S.after_run(t.schedule, time.time())
    self.start(t)

  # ---- every second (on the UI thread)
  def tick(self):
    now, mono = time.time(), time.monotonic()
    for t in list(self.transfers):
      # runs on a cluster: look at them
      if t.id in self.followed and t.id not in self.polling and mono >= self.followed[t.id]:
        self.followed[t.id] = mono + (POLL_SHOWN if t.id in self.shown else POLL_HIDDEN)
        self.polling.add(t.id)
        threading.Thread(target=self.poll, args=(t,), daemon=True).start()
      # waiting for its tabs
      if t.state == "waiting" and getattr(t, "pending", None) is not None and not self.not_ready(t):
        pending = t.pending
        t.state = "new"
        self.start(t, begin=pending.get("begin"), scheduled=pending.get("scheduled", False))
        continue
      if t.state == "waiting" and t.id in self.login_started and now - self.login_started[t.id] > LOGIN_WAIT:
        self.login_started[t.id] = float("inf") # (said once)
        names = ", ".join(c.name if c else "a closed tab" for c in self.not_ready(t))
        t.message = f"needs a login: {names} (open its tab and Connect)"
        self.say(t, t.message, "warning", 15)
        self.changed(t)
      # by its schedule
      if t.next_run and not t.active and t.state not in ("paused", "waiting") and now >= t.next_run:
        run, nxt = S.catch_up(t.schedule, t.next_run, now)
        if run:
          planned = t.next_run
          t.next_run = S.after_run(t.schedule, planned, now) if S.repeated(t.schedule) else None
          t.event(f"scheduled run ({S.describe(t.schedule)})")
          self.start(t, scheduled=True)
        else:
          t.next_run = nxt
          t.event("a scheduled run was missed: skipped")
          if nxt is None:
            t.state, t.message = "new", "the scheduled run was missed"
          self.changed(t)
      # after a job
      if (t.schedule or {}).get("kind") == "after_job" and t.state == "scheduled" and now >= self.job_watch.get(t.id, 0):
        self.job_watch[t.id] = now + JOB_WATCH
        threading.Thread(target=self.watch_job, args=(t,), daemon=True).start()

  def watch_job(self, t):
    """Has the job the transfer waits for ended? (on the source's tab, or the tab the schedule names)"""
    s = t.schedule
    cluster = M.find_tab(self.app.clusters, s.get("job_tab") or t.source.get("tab"))
    if cluster is None:
      return
    end = Endpoint(cluster)
    if not end.ready:
      return
    result = end.run(["squeue", "-h", "-j", str(s.get("job")), "-o", "%T"], timeout=60)
    if result.returncode == 0 and not (result.stdout or "").strip():
      def run():
        if t.state != "scheduled":
          return
        t.event(f"job {s.get('job')} ended")
        t.schedule = {**s, "kind": "now", "after": s.get("job")}
        self.job_watch.pop(t.id, None)
        t.state = "new"
        self.start(t, scheduled=True)
      self.from_thread(run)

  # ---- checks
  def check(self, t, report, done):
    """The check of a transfer (in a thread): report(plan) as it goes, done(plan) at the end (on the UI thread)"""
    stop = threading.Event()
    def work():
      try:
        source, dest = self.endpoints(t)
        src, dst = self.ends(t)
        plan = P.check(source, dest, t, src, dst, report=lambda p: self.from_thread(report, _copy(p)), stop=stop.is_set,
                       site=site_of)
      except Exception as e:
        log.debug(f"check of {t.name}: {e!r}")
        plan = {"done": True, "error": str(e), "steps": {}, "routes": {}}
      self.from_thread(done, _copy(plan))
    threading.Thread(target=work, daemon=True, name=f"check {t.name}").start()
    return stop

  # ---- for the overview and the footer
  def active(self):
    return [t for t in self.transfers if t.state in ("running", "starting", "queued", "pausing", "waiting")]

  def summary_for(self, cluster):
    """A line for the overview of a tab: the transfers from or to it that run, wait or are scheduled"""
    mine = [t for t in self.transfers if cluster in self.ends(t) and t.state in ("running", "queued", "waiting", "scheduled", "starting")]
    if not mine:
      return ""
    parts = []
    for t in mine[:3]:
      src, dst = self.ends(t)
      other = f"to {t.dest_name}" if src is cluster else f"from {t.source_name}"
      if t.state == "running":
        p = t.progress
        extra = f" ({p['percent']:.0f} %" + (f", {P.duration(p['eta'])} left" if p.get("eta") else "") + ")" if p.get("percent") is not None else ""
        parts.append(f"{t.name}: running {other}{extra}")
      else:
        parts.append(f"{t.name}: {M.STATES.get(t.state, t.state)} {other}")
    more = f" and {len(mine) - 3} more" if len(mine) > 3 else ""
    return "Transfers: " + "; ".join(parts) + more

  def footer(self):
    running = [t for t in self.transfers if t.state in ("running", "starting", "pausing")]
    if not running and not any(t.state in ("queued", "waiting") for t in self.transfers):
      return ""
    rate = sum(t.progress.get("rate") or 0 for t in running)
    pcts = [t.progress.get("percent") for t in running if t.progress.get("percent") is not None]
    text = f"⇅ {len(running)}" if running else "⇅ waiting"
    if len(running) == 1 and pcts:
      text += f" · {pcts[0]:.0f} %"
    if rate:
      text += f" · {P.human(rate)}/s"
    return text

def average_rate(history, seconds=30):
  """Bytes per second over the last seconds of the history ([[time, bytes]])"""
  if len(history) < 2:
    return 0.0
  end_t, end_b = history[-1]
  start = next(((tt, b) for tt, b in history if end_t - tt <= seconds), history[0])
  span = end_t - start[0]
  return (end_b - start[1]) / span if span > 0 else 0.0

def site_of(cluster):
  from ..checks.rulepacks import site_pack
  return site_pack(cluster.name, cluster.destination or "") if cluster is not None else {}

def _copy(plan):
  import copy
  return copy.deepcopy(plan)
