"""Transfers: the manager with both ends on this machine (an app that only has what the manager needs): a relay run
in slurp with its progress and end, a pause and Continue (idempotent: what is there is not copied again), a detached
run followed in its folder, a schedule, a run that waits for its tab, the lines for the overview and the footer"""
import os
import queue
import shutil
import time
import types

import pytest

from slurp.transfer import manager as MG, model as M

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")


class App:
  def __init__(self):
    self.here = types.SimpleNamespace(name="Local", destination=None, connection=None, ssh=None, pre_login="")
    self.clusters = [self.here]
    self.calls = queue.Queue()
    self.notes = []
    self.logins = []

  def call_from_thread(self, fn, *args):
    self.calls.put((fn, args))

  def notify(self, message, title="", severity="information", timeout=None):
    self.notes.append((title, message, severity))

  def login(self, cluster, show=False):
    self.logins.append(cluster.name)

  def needs_login(self, cluster):
    return bool(cluster.destination)

  def pump(self):
    while True:
      try:
        fn, args = self.calls.get_nowait()
      except queue.Empty:
        return
      fn(*args)


def until(app, manager, cond, seconds=60):
  end = time.time() + seconds
  while time.time() < end:
    app.pump()
    manager.tick()
    if cond():
      return True
    time.sleep(0.1)
  app.pump()
  return cond()


def make(tmp_path, app, runs_on="slurp", route="relay", big=0, **options):
  src, dst = tmp_path / "src", tmp_path / "dst"
  (src / "sub").mkdir(parents=True, exist_ok=True)
  (src / "a.txt").write_text("alpha")
  (src / "sub" / "b.bin").write_bytes(os.urandom(300_000))
  if big:
    (src / "big.bin").write_bytes(os.urandom(big))
  dst.mkdir(exist_ok=True)
  ref = M.tab_ref(app.here)
  return M.Transfer(M.new_id(), "t", {"tab": ref, "paths": [str(src)]}, {"tab": ref, "path": str(dst)}, route=route,
                    runs_on=runs_on, options=options, plan={"remaining_bytes": 300_005 + big, "remaining_files": 3})


def test_relay_run(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app)
  mg.add(t)
  mg.go(t)
  check("done", until(app, mg, lambda: t.state == "done"), (t.state, t.message))
  check("the files are there", (tmp_path / "dst" / "src" / "sub" / "b.bin").read_bytes() == (tmp_path / "src" / "sub" / "b.bin").read_bytes())
  check("its progress: all of it", t.progress.get("percent") == 100.0 and t.progress.get("bytes") == 300_005, t.progress)
  check("in the history", t.runs[-1]["result"] == "done" and t.runs[-1]["route"] == "relay", t.runs)
  check("a notification", any(n[0] == "t" and n[1] == "done" for n in app.notes), app.notes)
  check("kept in the store", M.Store(mg.store.path).get(t.id).state == "done")


def test_pause_and_continue(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app, big=30_000_000, bwlimit=8)
  mg.add(t)
  mg.go(t)
  until(app, mg, lambda: t.progress.get("bytes", 0) > 3_000_000, 30)
  mg.pause(t)
  check("paused", until(app, mg, lambda: t.state == "paused"), (t.state, t.message))
  first = t.progress.get("bytes", 0)
  check("the half-copied file is kept", list((tmp_path / "dst" / "src").rglob(".slurp-partial/*")))
  t.options["bwlimit"] = 0
  mg.resume(t)
  check("continued: done", until(app, mg, lambda: t.state == "done"), (t.state, t.message))
  check("whole", (tmp_path / "dst" / "src" / "big.bin").read_bytes() == (tmp_path / "src" / "big.bin").read_bytes())
  check("the second run had the rest to do", t.progress.get("total") and t.progress["total"] < 30_300_005, (first, t.progress.get("total")))
  check("two runs in the history", [r["result"] for r in t.runs] == ["paused", "done"], t.runs)


def test_detached(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app, runs_on="detached", route="same-fs")
  mg.add(t)
  mg.shown.add(t.id)
  mg.go(t)
  check("done", until(app, mg, lambda: t.state == "done", 60), (t.state, t.message, t.log[-3:]))
  check("its folder on the 'cluster'", t.runs[-1]["runs_on"] == "detached" and (tmp_path / "dst" / "src" / "a.txt").exists())
  again = MG.Manager(app, M.Store(mg.store.path))
  check("a new slurp follows no run that ended", not again.followed)


def test_waits_for_its_tab(check, tmp_path, monkeypatch):
  class Here(MG.Endpoint): # (the remote tab's end runs on this machine once it is "connected")
    def __init__(self, cluster):
      super().__init__(cluster)
      self.local, self.connection = True, None
  monkeypatch.setattr(MG, "Endpoint", Here)
  app = App()
  remote = types.SimpleNamespace(name="far", destination="far", connection=None, ssh=None, pre_login="")
  app.clusters.append(remote)
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app)
  t.dest["tab"] = M.tab_ref(remote)
  t.schedule = {"kind": "every", "every": "6h", "login": True}
  mg.add(t)
  mg.go(t)
  check("waits: not connected", t.state == "waiting" and "far" in t.message, (t.state, t.message))
  check("the next run is planned", t.next_run and t.next_run > time.time() + 5 * 3600)
  mg.start(t, scheduled=True)
  check("a scheduled run logs in by itself", app.logins == ["far"], app.logins)
  remote.connection = types.SimpleNamespace(ready=True) # (as if it logged in)
  check("connected: it starts and ends", until(app, mg, lambda: t.runs and t.runs[-1]["result"] == "done"), (t.state, t.message))
  check("repeated: scheduled again", t.state == "scheduled" and "next run" in t.message, (t.state, t.message))


def test_schedule_tick(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app)
  t.schedule = {"kind": "at", "at": time.time() + 1}
  mg.add(t)
  mg.go(t)
  check("scheduled", t.state == "scheduled" and t.next_run, (t.state, t.next_run))
  check("at its time: it runs, once", until(app, mg, lambda: t.state == "done", 30) and t.next_run is None, (t.state, t.next_run))
  mg.pause(t)
  check("pause of an ended one: nothing", t.state == "done")


def test_texts(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app)
  t.state, t.progress = "running", {"percent": 45.0, "eta": 720, "rate": 3 * 1024 ** 2}
  mg.store.transfers.append(t)
  line = mg.summary_for(app.here)
  check("the overview: running, how far, how long", line.startswith("Transfers: t: running to Local (45 %, 12 min left)"), line)
  check("the footer", mg.footer() == "⇅ 1 · 45 % · 3.0 MiB/s", mg.footer())
  t.state = "done"
  check("nothing under way: none", mg.summary_for(app.here) == "" and mg.footer() == "")
  check("the average rate", MG.average_rate([[0, 0], [10, 1000], [20, 3000]]) == 150.0)


def test_job_gone(check):
  check("running, pending: not gone", not MG.job_gone(0, "RUNNING\n") and not MG.job_gone(0, "PENDING None"))
  check("not listed: gone", MG.job_gone(0, ""))
  check("listed as ended for a while: gone", MG.job_gone(0, "COMPLETED") and MG.job_gone(0, "TIMEOUT") and MG.job_gone(0, "CANCELLED by 1"))
  check("no longer known: gone", MG.job_gone(1, "", "slurm_load_jobs error: Invalid job id specified"))
  check("squeue failed otherwise: not gone (asked again)", not MG.job_gone(1, "", "Socket timed out"))


def test_manual(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app)
  t.schedule = {"kind": "manual"}
  mg.add(t)
  mg.go(t)
  check("Add: it is there, it does not run", t.state == "new" and not t.run and t.next_run is None and "Run now" in t.message, (t.state, t.message))
  until(app, mg, lambda: False, 2)
  check("…not by itself either", t.state == "new" and not t.runs)
  mg.start(t)
  check("Run now: it runs", until(app, mg, lambda: t.state == "done"), (t.state, t.message))


def test_comparing(check, tmp_path):
  """Before rsync's first progress line it compares the files (with checksums it reads all of them): said so"""
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app, runs_on="detached", route="same-fs", checksum=True)
  mg.add(t)
  t.state, t.run = "running", {"kind": "detached", "folder": "/x", "started": time.time()}
  mg.polled(t, {"progress": {"phase": "scanning", "updated": 1}, "result": None, "alive": True, "lock": "1 host", "job": "", "log": ""})
  check("scanning: comparing", t.progress.get("phase") == "comparing" and t.state == "running", t.progress)
  mg.polled(t, {"progress": {"phase": "copying", "bytes": 10, "percent": 1, "rate": "1.00MB/s", "eta": "0:00:09"},
                "result": None, "alive": True, "lock": "1 host", "job": "", "log": ""})
  check("its first progress line: copying", t.progress.get("phase") == "copying" and t.progress.get("bytes") == 10, t.progress)


def test_reschedule(check, tmp_path):
  """Saved after editing: its schedule armed again, nothing runs now"""
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app)
  mg.add(t)
  for schedule, state in (({"kind": "daily", "daily": "02:00"}, "scheduled"), ({"kind": "every", "every": "6h"}, "scheduled"),
                          ({"kind": "at", "at": time.time() + 3600}, "scheduled"), ({"kind": "after_job", "job": "9"}, "scheduled"),
                          ({"kind": "manual"}, "new")):
    t.state, t.schedule, t.next_run = "new", schedule, None
    mg.reschedule(t)
    later = schedule["kind"] in ("daily", "every", "at")
    check(f"{schedule['kind']}: {state}, " + ("its next time ahead" if later else "no time"), t.state == state and not t.run
          and (bool(t.next_run and t.next_run > time.time() + 60) if later else t.next_run is None), (t.state, t.next_run))
  check("nothing ran", not t.runs)


def test_login_refused_message(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app, runs_on="job", route="direct-push")
  t.plan["facts"] = {"source": {"host": "daint-ln001"}}
  t.state, t.run = "running", {"kind": "job", "folder": "/x", "job": "77", "target": "jureca.fz-juelich.de", "started": time.time()}
  mg.add(t)
  log = ("== run Wed on nid001\nslurp: nid001 cannot log in to the other cluster by itself:\n"
         "user@jureca.fz-juelich.de: Permission denied (publickey).\n== ended with 255 (no login)\n")
  mg.polled(t, {"progress": {"phase": "scanning"}, "result": {"exit": 255, "host": "nid001", "job": "77", "login": "refused"},
                "alive": False, "lock": "", "job": "", "log": log})
  check("failed", t.state == "failed", t.state)
  check("it says who could not log in where, what ssh said, where the check logged in, why that may be",
        "nid001 could not log in to jureca.fz-juelich.de by itself (user@jureca.fz-juelich.de: Permission denied (publickey).)" in t.message
        and "the check had logged in from daint-ln001" in t.message and "transfer job runs on a node" in t.message, t.message)


def test_continue_only_paused(check, tmp_path):
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app)
  mg.add(t)
  t.state, t.runs = "done", [{"started": 1.0, "ended": 2.0, "result": "done"}]
  mg.resume(t)
  check("an ended transfer does not continue", t.state == "done" and not t.run)
  t.state, t.schedule = "scheduled", {"kind": "daily", "daily": "02:00"}
  mg.pause(t)
  check("a scheduled one paused: its schedule waits, not mid-run", t.state == "paused" and not mg.paused_mid_run(t))
  mg.resume(t)
  check("…Continue: its schedule again, no run now", t.state == "scheduled" and t.next_run and not t.run, (t.state, t.next_run))
  t.state = "paused"
  t.runs.append({"started": 3.0, "ended": 4.0, "result": "paused"})
  check("paused mid-run", mg.paused_mid_run(t))


def test_run_now_checks_first(check, tmp_path):
  """Run now of a transfer whose route did not pass its check (added as manual): the check first; passed: the run;
  not: failed, and why (no run)"""
  app = App()
  mg = MG.Manager(app, M.Store(str(tmp_path / "s.json")))
  t = make(tmp_path, app, route="same-fs", runs_on="detached")
  t.schedule, t.plan = {"kind": "manual"}, {"routes": {"same-fs": {"ok": False, "why": "not checked"}}}
  mg.add(t)
  mg.run_now(t)
  check("checked first, then it ran (here both ends share their filesystem)", until(app, mg, lambda: t.runs and t.runs[-1]["result"] == "done", 60)
        and t.plan["routes"]["same-fs"]["ok"], (t.state, t.message, t.log[-3:]))
  t.route, t.runs_on, t.state = "direct-push", "detached", "new"
  runs = len(t.runs)
  mg.run_now(t)
  check("a route that does not pass: failed, why, no run", until(app, mg, lambda: t.state == "failed", 60)
        and "did not pass the check before the run" in t.message and len(t.runs) == runs, (t.state, t.message))
