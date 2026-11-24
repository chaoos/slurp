"""slurp's core without a frontend (slurp/headless.py) against the lab: connect, the job list as data (arrays as one
row, finished jobs, the checks), the list kept for a cluster that cannot be reached, a transfer, and the changes
pushed to subscribers (jobs, connection, transfers). Nothing here imports Textual; the smoke path runs with Textual
blocked."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

DEST = "lab-relay" # (the lab's sshd, its key without a passphrase: a login that asks nothing)


@pytest.fixture
def env(lab, tmp_path, monkeypatch):
  """The lab's ssh first on PATH; slurp's settings, state and cache (the kept job lists) in tmp_path"""
  monkeypatch.setenv("PATH", os.path.join(lab.dir, "bin") + os.pathsep + os.environ["PATH"])
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  arrays = os.path.join(lab.dir, "arrays")
  yield lab
  if os.path.exists(arrays):
    os.unlink(arrays)


@pytest.fixture
def hub(env):
  from slurp import headless
  h = headless.Hub()
  yield h
  for c in h.clusters:
    if c.connection is not None:
      c.connection.wanted = False
      c.connection.close()


def until(cond, seconds=20):
  end = time.monotonic() + seconds
  while time.monotonic() < end:
    if cond():
      return True
    time.sleep(0.1)
  return cond()


BLOCK_TEXTUAL = textwrap.dedent("""
  import runpy, sys
  class NoTextual:
    def find_spec(self, name, path=None, target=None):
      if name == "textual" or name.startswith("textual."):
        raise ImportError("textual is blocked")
  sys.meta_path.insert(0, NoTextual())
  sys.argv = ["slurp.headless", *sys.argv[1:]]
  try:
    runpy.run_module("slurp.headless", run_name="__main__")
  finally:
    assert not any(m == "textual" or m.startswith("textual.") for m in sys.modules), "textual was imported"
""")


def test_smoke_jobs_as_json_without_textual(check, env):
  """python -m slurp.headless --jobs DESTINATION: the job list as JSON, with Textual blocked"""
  out = subprocess.run([sys.executable, "-c", BLOCK_TEXTUAL, "--jobs", DEST], capture_output=True, text=True, timeout=120)
  check("it ends well", out.returncode == 0, out.stderr[-3000:])
  rows = json.loads(out.stdout or "[]")
  ids = {r["id"] for r in rows}
  check("the jobs of the lab: running, pending and finished ones", {"9000", "9005", "9010"} <= ids and len(rows) >= 40, sorted(ids)[:10])
  job = next((r for r in rows if r["id"] == "9001"), {})
  check("a running job as data: state, user, account, nodes, elapsed, its estimated end, its progress, its checks",
        job.get("state") == "RUNNING" and job.get("running") and job.get("account") == "acct1" and job.get("nodes") == "2"
        and job.get("elapsed") == "00:30:00" and job.get("estimated_end") and job.get("progress", {}).get("total") == 100
        and set(job.get("checks") or {}) == {"error", "warning", "info"}, job)
  out = subprocess.run([sys.executable, "-c", BLOCK_TEXTUAL, "--jobs", DEST, "--cached"], capture_output=True, text=True, timeout=60)
  rows = json.loads(out.stdout or "[]") if out.returncode == 0 else []
  check("--cached: the list kept by that load, marked so", rows and all(r["from_cache"] for r in rows), (out.returncode, out.stderr[-500:]))


def test_arrays_and_finished_jobs(check, hub, env):
  from slurp import headless
  s = headless.open_session(DEST, hub=hub)
  rows = headless.load_jobs(s, since="2d", checks=False, hub=hub)
  check("no arrays yet: only jobs", all(r["kind"] == "job" for r in rows))
  ended = [r for r in rows if r["ended"]]
  check("finished jobs (since 2 days) are in the list, as ended, with their exit code",
        len(ended) >= 30 and all(r["state"] in ("COMPLETED", "FAILED") and r["exit"] for r in ended), len(ended))
  open(os.path.join(env.dir, "arrays"), "w").close() # (the fake Slurm lists its arrays from now on)
  rows = headless.load_jobs(s, since="2d", checks=False, hub=hub)
  arrays = {r["id"]: r for r in rows if r["kind"] == "array"}
  check("an array: one row with its tasks by state (9600: 150 tasks)", "9600" in arrays and len(arrays["9600"]["tasks"]) == 150
        and sum(arrays["9600"]["states"].values()) == 150, {k: (len(v["tasks"]), v["states"]) for k, v in arrays.items()})
  check("…and not its tasks one by one", not any(r["kind"] == "job" and r["array"] == "9600" for r in rows))
  opened = headless.job_rows(s, expanded={"9600"})
  check("an array shown open: its row, then its tasks", sum(r.get("array") == "9600" for r in opened) == 150
        and any(r["kind"] == "array" and r["id"] == "9600" for r in opened))
  flat = headless.job_rows(s, collapse=False)
  check("not collapsed: every task a row", not any(r["kind"] == "array" for r in flat) and len(flat) == sum(len(v["tasks"]) for v in arrays.values()) + len(rows) - len(arrays))


def test_topics_jobs_connection_and_reconnect_from_cache(check, hub):
  from slurp import headless
  s = headless.open_session(DEST, hub=hub)
  pushed, states = [], []
  headless.subscribe(s, "jobs", pushed.append, hub=hub)
  headless.subscribe(s, "connection", lambda st: states.append(st["state"]), hub=hub)
  check("connection: its state pushed (ready)", until(lambda: states[-1:] == ["ready"], 10), states)
  rows = headless.load_jobs(s, hub=hub)
  check("jobs: the rows pushed after the load, with the counts of the checks", pushed and pushed[-1] == rows
        and any(r.get("checks") and sum(r["checks"].values()) for r in rows), len(pushed))
  headless.disconnect(s)
  check("connection: the drop pushed", until(lambda: states and states[-1] in ("broken", "closed"), 10), states)
  try:
    headless.load_jobs(s, hub=hub)
    refused = False
  except ConnectionError:
    refused = True
  check("no connection: a load is refused", refused)
  kept = headless.ClusterState(DEST, DEST)
  cached = headless.load_cached(kept, hub=hub)
  check("the list kept by the last load: the same jobs, marked as kept", cached and [r["id"] for r in cached] == [r["id"] for r in rows]
        and all(r["from_cache"] for r in cached), (cached or [])[:2])
  headless.connect(s)
  check("connection: back (pushed)", until(lambda: states[-1:] == ["ready"], 10), states)
  rows2 = headless.load_jobs(s, hub=hub)
  check("after the reconnect: loaded anew (not the kept list)", rows2 and not any(r["from_cache"] for r in rows2) and len(pushed) == 2)


def test_topic_transfers(check, hub, tmp_path):
  from slurp import headless
  src = tmp_path / "src"
  src.mkdir()
  (src / "a.dat").write_bytes(os.urandom(200_000))
  (src / "b.dat").write_bytes(os.urandom(100_000))
  dest = tmp_path / "dest"
  local = headless.open_session(None, hub=hub)
  remote = headless.open_session(DEST, hub=hub)
  seen = []
  done = threading.Event()
  def changed(items):
    seen.extend(items)
    if any(i["state"] in ("done", "failed") for i in items):
      done.set()
  headless.subscribe(None, "transfers", changed, hub=hub)
  t = headless.start_transfer(local, [str(src)], remote, str(dest), hub=hub)
  check("a transfer: started through slurp", t["route"] == "relay" and t["state"] in ("starting", "running", "waiting", "done"), t)
  check("transfers: its changes pushed until its end", done.wait(60) and seen[-1]["id"] == t["id"], [(i["state"], i["message"]) for i in seen][-5:])
  check("…done, the files there", seen[-1]["state"] == "done" and (dest / "src" / "a.dat").read_bytes() == (src / "a.dat").read_bytes(),
        (seen[-1], sorted(p.name for p in dest.rglob("*"))))
