"""The workers of cluster/remote.py without a frontend: ThreadScheduler (cluster/workers.py) as the receiver; the same
semantics as in the Textual app: results delivered while the receiver is there, exclusive groups abort the older
worker, Cancelled is quiet, errors are told (not after an abort), the connection is the one at the start. And
load_jobs end to end through it against the lab's fake Slurm on this machine, with no Textual imported"""
import os
import subprocess
import sys
import textwrap
import threading
import time

from slurp.cluster import remote
from slurp.cluster.workers import ThreadScheduler

LAB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "e2e", "lab")


def fake_slurm(tmp_path):
  """The lab's fake Slurm, local: tmp/fakeslurm.py and its commands in tmp/fakebin (it keeps its jobs in tmp)"""
  script = tmp_path / "fakeslurm.py"
  script.write_text(open(os.path.join(LAB, "fakeslurm.py")).read())
  script.chmod(0o755)
  (tmp_path / "fakebin").mkdir()
  for name in ("sacct", "sacctmgr", "scontrol", "squeue", "sinfo", "sstat"):
    os.symlink(script, tmp_path / "fakebin" / name)
  return str(tmp_path / "fakebin")


def test_results_and_receiver(check):
  s = ThreadScheduler()
  got = []
  w = remote.in_background(s, lambda: 6 * 7, got.append, group="g")
  w.wait(5)
  check("the result waits for the frontend's loop", got == [])
  check("pump: it is delivered there", s.pump(2) == 1 and got == [42], got)
  s.close()
  w = remote.in_background(s, lambda: 1, got.append, group="g")
  time.sleep(0.2); s.pump(1); w.wait(5)
  check("the receiver gone: dropped silently", got == [42] and not s.errors, (got, s.errors))


def test_inline_errors_and_cancel(check):
  s = ThreadScheduler(deliver_inline=True)
  def boom():
    raise ValueError("no such file")
  remote.in_background(s, boom, lambda r: s.errors.append(("result", r)), group="g", name="reading").wait(5)
  check("an error: told with the tab, the result None", s.errors[0] == ("Local", "reading failed: no such file")
        and s.errors[1] == ("result", None), s.errors)
  s.errors.clear()
  def shown_fails(_):
    raise KeyError("x")
  remote.in_background(s, lambda: 1, shown_fails, group="g", name="list").wait(5)
  check("showing the result fails: told, not raised", s.errors == [("Local", "showing list failed: 'x'")], s.errors)
  s.errors.clear()
  def cancelled():
    raise remote.Cancelled()
  w = remote.start_worker(s, cancelled, "g")
  check("Cancelled: quiet, None", w.wait(5) is None and not s.errors, s.errors)


def test_exclusive_group_aborts_the_older(check):
  s = ThreadScheduler(deliver_inline=True)
  started = threading.Event()
  def long():
    started.set()
    for _ in range(100):
      remote.check_cancelled()
      time.sleep(0.02)
    raise RuntimeError("not aborted")
  first = remote.start_worker(s, long, "load")
  started.wait(5)
  second = remote.start_worker(s, lambda: "new", "load")
  check("the newer one of the group runs", second.wait(5) == "new")
  check("the older one is aborted (its abort event), quietly", first.abort.is_set() and first.wait(5) is None and not s.errors, s.errors)
  other = remote.start_worker(s, lambda: "x", "other", exclusive=True)
  check("another group: not touched", other.wait(5) == "x" and not second.abort.is_set())


def test_connection_bound_at_the_start(check):
  s = ThreadScheduler(deliver_inline=True)
  go = threading.Event()
  class Conn:
    destination = "cluster-a"
  old = remote.ssh_connection
  remote.ssh_connection = Conn()
  try:
    w = remote.start_worker(s, lambda: (go.wait(5), remote.tab_name())[1], "g")
    remote.ssh_connection = None # (another tab active now)
    go.set()
    check("the worker keeps the tab it started with", w.wait(5) == "cluster-a")
  finally:
    remote.ssh_connection = old


def test_load_jobs_without_textual(check, tmp_path):
  """load_jobs through start_worker and a ThreadScheduler that collects the result, in a fresh Python where importing
  textual fails"""
  fakebin = fake_slurm(tmp_path)
  code = textwrap.dedent("""
    import sys
    class NoTextual:
      def find_spec(self, name, path=None, target=None):
        if name == "textual" or name.startswith("textual."):
          raise ImportError("textual is blocked")
    sys.meta_path.insert(0, NoTextual())
    from slurp.cluster import remote
    from slurp.cluster.jobs import load_jobs
    from slurp.cluster.workers import ThreadScheduler
    s = ThreadScheduler()
    got = []
    remote.in_background(s, lambda: load_jobs(None), got.append, group="jobs")
    s.pump(30)
    jobs = got[0]
    print(len(jobs), sorted({j.state for j in jobs}), s.errors, "textual" in sys.modules)
  """)
  env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}", XDG_CONFIG_HOME=str(tmp_path / "config"),
             XDG_STATE_HOME=str(tmp_path / "state"), XDG_CACHE_HOME=str(tmp_path / "cache"))
  out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
  check("the queued jobs of the fake Slurm (5 running, 3 pending), no errors, no textual",
        out.stdout.strip() == "8 ['PENDING', 'RUNNING'] [] False", (out.stdout, out.stderr[-2000:]))
