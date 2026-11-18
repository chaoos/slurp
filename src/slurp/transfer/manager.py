"""The transfers of the app: it runs them (the relay in slurp; detached runs and transfer jobs on a cluster), follows
them (their progress, their end), pauses and continues them. A transfer runs only when the user says so (Run now):
there are no schedules (Copy as a command gives the command for scrontab or cron).

It needs of the app: clusters (the tabs), notify(message, title=, severity=, timeout=), call_from_thread(fn, *args),
login(cluster, show=False), needs_login(cluster) and login_visible(cluster). Everything that waits for a cluster runs in threads of its own;
tick() (every second, on the UI thread) only looks at the state."""

import threading
import time

from ..state import log
from . import endpoints as E, model as M, plan as P, relay as RL, rsync as R, runner as RN
from .host import Host, NotConnected

POLL_SHOWN = 3      # seconds between looks at a run on a cluster while its transfer is shown
POLL_HIDDEN = 15    # … while it is not
RATE_POINTS = 120   # points of the rate kept for the sparkline

QUEUED_STATES = ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED", "REQUEUE_HOLD",
                 "REQUEUE_FED", "RESIZING", "SIGNALING", "STAGE_OUT", "STOPPED", "PREEMPTED")

def job_gone(code, out, err=""):
  """Has a job ended, by squeue -h -j ID -o %T? Not listed, listed as ended (COMPLETED, TIMEOUT, … for a while
  after its end), or no longer known (squeue: Invalid job id)"""
  state = (out or "").strip().split()[0] if (out or "").strip() else ""
  if code == 0:
    return state not in QUEUED_STATES
  return "Invalid job id" in (err or "")

def client_side(route):
  """Which end runs rsync: "source" or "dest" """
  return "dest" if route == "direct-pull" else "source"

class Manager:
  def __init__(self, app, store=None, endpoints=None):
    self.app = app
    self.store = store or M.Store()
    self.endpoint_store = endpoints or E.EndpointStore()
    self.endpoint_status = {} # id of a defined endpoint -> (state, words) of this session
    self.relays = {}        # id -> Relay (runs in slurp)
    self.followed = {}      # id -> next time to look at its run on the cluster
    self.polling = set()    # ids being looked at now
    self.shown = set()      # ids whose details (or the list) are shown: looked at more often
    self.listeners = []     # fn(transfer or None) on the UI thread when something changed
    self.checking = set()   # ids being checked again (their details: Check again)
    self.lock = threading.RLock()
    for t in self.store.transfers: # (runs that went on without slurp: followed again)
      if t.run and t.runs_on in ("detached", "job"):
        self.followed[t.id] = 0

  # ---- the transfers
  @property
  def transfers(self):
    return self.store.transfers

  def get(self, tid):
    return self.store.get(tid)

  def ends(self, t):
    """(source tab, destination tab): the tabs (this machine: the local tab, or its stand-in) of its endpoints; None
    for a tab that is not open, or an endpoint that is no tab"""
    return self.end_cluster(t.source), self.end_cluster(t.dest)

  def end_cluster(self, ref):
    eid = E.ref_id(ref)
    if eid == E.LOCAL:
      return E.this_machine(self.app.clusters)
    if eid.startswith("tab:"):
      return M.find_tab(self.app.clusters, ref.get("tab") or {"destination": eid[4:]})
    return None

  def hosts(self, t):
    src, dst = self.ends(t)
    if src is None or dst is None:
      missing = M.tab_name(t.source.get("tab")) if src is None else M.tab_name(t.dest.get("tab"))
      raise NotConnected(f"the tab {missing} is not open")
    return Host(src), Host(dst)

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
  def start(self, t):
    """A run now. Ends that are not connected: it waits for them (they log in in their tabs)"""
    if t.active:
      return
    missing = self.not_ready(t)
    if missing:
      names = ", ".join(c.name if c else "a closed tab" for c in missing)
      t.state, t.message = "waiting", f"waiting for {names} (not connected)"
      t.pending = {}
      self.changed(t)
      return
    t.pending = None
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
    threading.Thread(target=target, args=(t,), daemon=True, name=f"transfer {t.name}").start()

  # ---- the relay (in slurp)
  def run_relay(self, t):
    try:
      source, dest = self.hosts(t)
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
    t.progress["phase"] = "copying" if p or current else "comparing"
    if not p and not current:
      self.changed(t, save=False)
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
      t.pending = {"resume": True}
      self.changed(t)
      return
    self.ended(t, code, message, stats=relay.output.stats, stopped=relay.stopped)

  # ---- runs on a cluster (detached, transfer job)
  def client(self, t):
    source, dest = self.hosts(t)
    return source if client_side(t.route) == "source" else dest

  def run_on_cluster(self, t):
    try:
      source, dest = self.hosts(t)
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
      login = RN.preflight(target, folder, key) if target else ""
      folder = RN.prepare(client, t, text, job=t.runs_on == "job", known_hosts=known, login=login)
      run_target = (target or {}).get("host", "")
      if key:
        self.install_key(t, client, source if client is dest else dest, folder)
      run = {"folder": folder, "client": client_side(t.route), "target": run_target}
      if t.runs_on == "job":
        run["job"] = RN.submit(client, folder)
        state = "queued"
        message = f"transfer job {run['job']}"
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
        source, dest = self.hosts(t)
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
    out.feed(status.get("log", "").rpartition("== run ")[2]) # (the log has all runs: this one's part)
    out.close()
    p = RN.progress_of(status.get("progress"))
    if p:
      # (before its first progress line rsync compares the files: with checksums that reads all of them)
      t.progress["phase"] = "comparing" if p.get("phase") == "scanning" else "copying"
      if p.get("phase") in ("copying", "ended", "scanning") and t.state in ("queued", "starting", "waiting"):
        t.state = "running"
        t.message = f"on {status['lock'].split()[1]}" if len(status.get("lock", "").split()) > 1 else ""
      self.progress(t, {"bytes": p.get("bytes", 0), "percent": p.get("percent", 0), "rate": p.get("rate", 0.0),
                        "eta": p.get("rsync_eta"), **({"left": p["left"], "checked": p["checked"]} if "left" in p else {})},
                    current=p.get("current", ""), files=p.get("files", 0), errors=out.errors, error_count=out.error_count)
    job = status.get("job", "")
    if job and job_gone(0, job): # (listed as ended for a while after its end: gone)
      job = ""
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
    if result and result.get("login") == "refused":
      message = self.login_refused(t, result, status.get("log", ""))
    self.ended(t, code, message, stats=out.stats, stopped=stopped)

  def login_refused(self, t, result, log_text):
    """Why a direct run stopped at once: the host it ran on could not log in to the other cluster (the check had
    logged in from another one, or that key works only from some hosts)"""
    lines = log_text.rpartition("== run ")[2].splitlines()
    at = next((i for i, l in enumerate(lines) if "cannot log in to the other cluster" in l), None)
    said = [l.strip() for l in (lines[at + 1:] if at is not None else []) if l.strip() and not l.startswith("==")]
    side = "dest" if client_side(t.route) == "dest" else "source"
    checked = ((t.plan.get("facts") or {}).get(side) or {}).get("host", "")
    host, target = result.get("host") or "the cluster", t.run.get("target") or "the other cluster"
    why = said[-1] if said else "the login was refused"
    hint = (" A transfer job runs on a node of its partition: the other site may accept your key only from some hosts "
            "(e.g. its login nodes)." if t.runs_on == "job" else
            " It may accept your key only from some hosts (e.g. from= in authorized_keys at JSC).")
    return (f"{host} could not log in to {target} by itself ({why})" + (f"; the check had logged in from {checked}" if checked and checked != host else "")
            + "." + hint + " Check again, or choose another route (Edit…).")

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
    self.changed(t)
    severity = {"done": "information", "done-errors": "warning", "paused": "information"}.get(state, "error")
    self.say(t, {"done": "done", "done-errors": f"done, with errors ({t.progress.get('errors', 0)} files)",
                 "paused": "paused", "failed": f"failed: {words}"}.get(state, state), severity, 10 if severity != "information" else 6)
    return entry

  # ---- pause, continue
  def pause(self, t):
    """Stop the run as a pause (a transfer job is cancelled: the UI warns first); one that waits for its tabs: it no
    longer waits"""
    if t.state == "waiting" and not t.run:
      t.state, t.message, t.pending = "new", "it no longer waits", None
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

  @staticmethod
  def paused_mid_run(t):
    """Paused while a run went on (Pause, slurp's end, a broken connection): Continue goes on with it"""
    return t.state == "paused" and bool(t.runs) and t.runs[-1]["result"] in ("paused", "interrupted")

  def resume(self, t):
    """Continue a paused transfer: a new run (rsync goes on where it stopped). Only a paused transfer continues: one
    that ended runs anew (Run now)"""
    if t.state != "paused":
      return
    self.start(t)

  # ---- every second (on the UI thread)
  def tick(self):
    mono = time.monotonic()
    for t in list(self.transfers):
      # runs on a cluster: look at them
      if t.id in self.followed and t.id not in self.polling and mono >= self.followed[t.id]:
        self.followed[t.id] = mono + (POLL_SHOWN if t.id in self.shown else POLL_HIDDEN)
        self.polling.add(t.id)
        threading.Thread(target=self.poll, args=(t,), daemon=True).start()
      # waiting for its tabs
      if t.state == "waiting" and getattr(t, "pending", None) is not None and not self.not_ready(t):
        t.state = "new"
        self.start(t)

  # ---- checks
  def probe_node(self, t, route, partition, account, done):
    """The login of a direct route from a node of a partition, as a transfer job would (srun, in a thread):
    done((True | False | None, words)) on the UI thread; None: no node within a while (not checked)"""
    def work():
      try:
        source, dest = self.hosts(t)
        src, dst = self.ends(t)
        client, other = (source, dst) if route == "direct-push" else (dest, src)
        target = P.target_of(other)
        folder = RN.folder_of(client, t)
        client.sh('mkdir -p "$1" && chmod 700 "$1"', folder, retry=False)
        result = P.login_works_on(client, target, folder, partition, account)
      except Exception as e:
        result = (None, f"not checked: {e}")
      self.from_thread(done, result)
    threading.Thread(target=work, daemon=True, name=f"node check {t.name}").start()

  def test_job(self, t, route, done):
    """sbatch --test-only of the transfer job t would submit (its options), on the cluster that runs rsync (in a
    thread): done((True | False, words)) on the UI thread"""
    def work():
      try:
        source, dest = self.hosts(t)
        client = dest if client_side(route) == "dest" else source
        folder = RN.folder_of(client, t)
        client.sh('mkdir -p "$1" && chmod 700 "$1"', folder, retry=False)
        result = RN.test_job(client, t, folder)
      except Exception as e:
        result = (False, f"not tested: {e}")
      self.from_thread(done, result)
    threading.Thread(target=work, daemon=True, name=f"job test {t.name}").start()

  @staticmethod
  def route_passed(t):
    """Did its route pass its last check (a route that logs in only with the temporary key: with it chosen)?"""
    info = (t.plan.get("routes") or {}).get(t.route) or {}
    return bool(info.get("ok") or (info.get("keyable") and t.options.get("temp_key")))

  def run_now(self, t):
    """Run now (the user's): a route that did not pass its last check (a manual transfer may be added so) is checked
    first, and the run starts only if it passes"""
    if t.active:
      return
    if self.route_passed(t):
      self.start(t)
      return
    def then():
      if self.route_passed(t):
        self.start(t)
        return
      info = (t.plan.get("routes") or {}).get(t.route) or {}
      t.state = "failed"
      t.message = (f"{M.ROUTES.get(t.route, t.route)} did not pass the check before the run"
                   + (f" ({info.get('why')})" if info.get("why") else "") + ": choose another route (Edit…), or Check again later")
      t.event(t.message)
      self.changed(t)
      self.say(t, t.message, "error", 12)
    if self.recheck(t, then=then):
      t.event("Run now: the route is checked first")

  def recheck(self, t, then=None):
    """Check a transfer again as it is set up (its paths, its options): its plan as the check goes (what remains, the
    routes), then() after it (on the UI thread); False if it cannot be now (a run under way, a tab not connected)"""
    if t.id in self.checking or t.active:
      return False
    missing = self.not_ready(t)
    if missing:
      self.say(t, "connect " + ", ".join(c.name if c else "its closed tab" for c in missing) + " first", "warning")
      return False
    self.checking.add(t.id)
    t.event("checking …")
    def report(plan):
      t.plan = plan
      self.changed(t, save=False)
    def done(plan):
      t.plan = plan
      self.checking.discard(t.id)
      remaining = plan.get("remaining_bytes")
      t.event("checked: " + ("in sync" if remaining == 0 and not plan.get("remaining_files") else
                              f"{P.human(remaining)} in {plan.get('remaining_files', '?')} file{'' if plan.get('remaining_files') == 1 else 's'} to copy"
                              if remaining is not None
                              else plan.get("error") or "what remains is not known"))
      self.changed(t)
      if then is not None: # (Run now: the run, if the route passed)
        then()
    self.check(t, report, done)
    return True

  def check(self, t, report, done):
    """The check of a transfer (in a thread): report(plan) as it goes, done(plan) at the end (on the UI thread)"""
    stop = threading.Event()
    def work():
      try:
        source, dest = self.hosts(t)
        src, dst = self.ends(t)
        plan = P.check(source, dest, t, src, dst, report=lambda p: self.from_thread(report, _copy(p)), stop=stop.is_set,
                       site=site_of)
      except Exception as e:
        log.debug(f"check of {t.name}: {e!r}")
        plan = {"done": True, "error": str(e), "steps": {}, "routes": {}}
      self.from_thread(done, _copy(plan))
    threading.Thread(target=work, daemon=True, name=f"check {t.name}").start()
    return stop

  def shutdown(self):
    """slurp ends: its relay runs are paused (rsync keeps what it copied; Continue goes on at the next start); the
    runs on clusters go on"""
    for tid, relay in list(self.relays.items()):
      relay.stop()
    end = time.monotonic() + 5
    for tid, relay in list(self.relays.items()):
      relay.wait(max(0.1, end - time.monotonic()))
      t = self.get(tid)
      if t is not None and t.state in M.ACTIVE:
        M.record_run(t, "paused", "slurp ended")
        t.state, t.message = "paused", "paused (slurp ended)"
    self.relays.clear()
    self.store.save()

  # ---- Copy as a command
  def command_text(self, t):
    """The command of a run, to put into scrontab or cron by hand (in a worker: it asks ssh -G for a direct route).
    Patterns are left for the shell to expand; secrets would be placeholders, never in clear."""
    import shlex
    src, dst = self.ends(t)
    def arg(p):
      if R.is_pattern(p):
        folder, name = p.rsplit("/", 1) if "/" in p else ("", p)
        return (shlex.quote(folder) + "/" if folder else "") + name.replace(" ", "\\ ")
      return shlex.quote(p)
    opts = [o for o in R.options(t.options, (3, 2, 0)) if not o.startswith(("--info", "--outbuf"))] + ["--info=progress2"]
    paths = " ".join(arg(p) for p in t.source.get("paths", []))
    dest = shlex.quote(t.dest.get("path", "").rstrip("/") + "/")
    head = [f"# slurp transfer {t.name!r}: {M.ROUTES.get(t.route, t.route)}", "# (as slurp runs it; it copies only what is missing or changed, and deletes nothing)"]
    def ssh_of(cluster):
      target = P.target_of(cluster) or {}
      if target.get("error") or not target.get("host"):
        return None, f"# ({cluster.name if cluster else '?'}: {target.get('error') or 'cannot be reached from a cluster'})"
      return target, ""
    if t.route in ("same-fs", "local"):
      body = [f"# on {t.source_name}:", f"rsync {shlex.join(opts)} -- {paths} {dest}"]
    elif t.route in ("direct-push", "direct-pull"):
      push = t.route == "direct-push"
      target, why = ssh_of(dst if push else src)
      if target is None:
        body = [why]
      else:
        rsh = shlex.join(["ssh", "-o", "BatchMode=yes", *(["-p", str(target["port"])] if str(target.get("port", "22")) != "22" else []),
                          *(["-l", target["user"]] if target.get("user") else []), *(["-J", target["jump"]] if target.get("jump") else [])])
        host = target["host"]
        if push:
          body = [f"# on {t.source_name} (it logs in to {t.dest_name} by itself):",
                  f"rsync {shlex.join(opts)} --rsh={shlex.quote(rsh)} -- {paths} {host}:{dest}"]
        else:
          remote = " ".join(f"{host}:{arg(p)}" for p in t.source.get("paths", []))
          body = [f"# on {t.dest_name} (it logs in to {t.source_name} by itself):",
                  f"rsync {shlex.join(opts)} --rsh={shlex.quote(rsh)} -- {remote} {dest}"]
    else: # (the relay)
      local_src, local_dst = src is not None and not src.destination, dst is not None and not dst.destination
      if local_src or local_dst:
        other = dst if local_src else src
        target, why = ssh_of(other)
        if target is None:
          body = [why]
        else:
          rsh = shlex.join(["ssh", *(["-p", str(target["port"])] if str(target.get("port", "22")) != "22" else []),
                            *(["-l", target["user"]] if target.get("user") else []), *(["-J", target["jump"]] if target.get("jump") else [])])
          host = target["host"]
          if local_src:
            body = ["# on this machine:", f"rsync {shlex.join(opts)} --rsh={shlex.quote(rsh)} -- {paths} {host}:{dest}"]
          else:
            remote = " ".join(f"{host}:{arg(p)}" for p in t.source.get("paths", []))
            body = ["# on this machine:", f"rsync {shlex.join(opts)} --rsh={shlex.quote(rsh)} -- {remote} {dest}"]
      else:
        target, why = ssh_of(dst)
        body = ["# The relay passes the data through slurp: as a command it is no single line.",
                f"# The nearest: on {t.source_name}, if it can log in to {t.dest_name} by itself (a direct route):"]
        body += [why] if target is None else [f"rsync {shlex.join(opts)} -- {paths} {target['host']}:{dest}"]
    if t.runs_on == "job" and t.route != "relay" and body and not body[-1].startswith("#"):
      o = t.options
      sbatch = ["sbatch", f"--job-name=slurp-xfer-{t.id}", f"--time={o.get('time') or '24:00:00'}", "--ntasks=1",
                *([f"--partition={o['partition']}"] if o.get("partition") else []), *([f"--account={o['account']}"] if o.get("account") else [])]
      body = [body[0].replace("# on ", "# a transfer job on "), f"{shlex.join(sbatch)} --wrap={shlex.quote(body[-1])}"]
    return "\n".join(head + body) + "\n"

  # ---- for the overview and the footer
  def active(self):
    return [t for t in self.transfers if t.state in ("running", "starting", "queued", "pausing", "waiting")]

  def summary_for(self, cluster):
    """A line for the overview of a tab: the transfers from or to it that run or wait"""
    mine = [t for t in self.transfers if cluster in self.ends(t) and t.state in ("running", "queued", "waiting", "starting")]
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
