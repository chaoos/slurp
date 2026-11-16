"""The job list kept per tab (slurp/jobcache.py): round trip of the jobs and the categories, only for the user, a
broken or old file is ignored, a load that came first wins; the changes made offline, replayed in order"""
import json, os, stat, sys, tempfile
from datetime import datetime
from slurp import jobcache, categories as cat
from slurp.state import ClusterSession
from slurp.jobs import Job, JobProgress, SACCT_FIELDS


def test_jobcache(check, tmp_path, monkeypatch):
  T = str(tmp_path)
  monkeypatch.setenv("XDG_CACHE_HOME", os.path.join(T, "cache"))
  monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(T, "config"))
  def job(jid, state="COMPLETED"):
    rec = dict.fromkeys(SACCT_FIELDS, ""); rec.update(JobID=jid, JobIDRaw=jid, JobName=f"n{jid}", State=state, NNodes="2", Elapsed="01:00:00")
    return Job(rec, steps=[dict(rec, JobID=jid + ".0")], queued=state == "RUNNING", command="srun ./app", err=("file", 12),
               progress=JobProgress(3, 10, "step 3/10") if state == "RUNNING" else None)
  c = ClusterSession("daint", "daint-alps")
  c.jobs = {j.jid: j for j in (job("100"), job("101", "RUNNING"), job("200_1"))}
  c.updated, c.since, c.slurm_cluster = datetime(2026, 11, 16, 9, 12), datetime(2026, 11, 15, 9, 12), "daint"
  c.categories = cat.Store("daint"); c.categories.assign(["100"], "proj/prod")
  c.pending_categories = [["assign", ["101"], "proj/test", {}]]
  jobcache.save(c)
  path = jobcache.path_of(c)
  check("kept in the cache directory, named after the destination", path == os.path.join(T, "cache", "slurp", "joblists", "daint-alps.json"), path)
  check("only for the user (the file, and its directory)", stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700,
        (oct(os.stat(path).st_mode), oct(os.stat(os.path.dirname(path)).st_mode)))
  d = ClusterSession("daint", "daint-alps")
  check("loaded into a new tab", jobcache.load(d) and d.loaded and d.from_cache and sorted(d.jobs) == ["100", "101", "200_1"])
  j = d.jobs["101"]
  check("a job as it was: accounting, steps, queued, command, error file, progress", j.rec["JobName"] == "n101" and j.steps[0]["JobID"] == "101.0"
        and j.queued and j.command == "srun ./app" and j.err == ("file", 12) and j.progress.current == 3 and j.annotated)
  check("when, since, the Slurm cluster", d.updated == c.updated and d.since == c.since and d.slurm_cluster == "daint")
  check("its categories, and the changes not yet on the cluster", d.categories.category_of("100") == "proj/prod" and d.pending_categories == c.pending_categories)
  e = ClusterSession("daint", "daint-alps"); e.loaded = True
  check("a tab that has loaded already keeps its list", not jobcache.load(e) and not e.jobs)
  check("another destination: nothing", not jobcache.load(ClusterSession("eiger", "eiger")))
  open(path, "w").write("{ broken")
  check("a broken file is ignored", not jobcache.load(ClusterSession("daint", "daint-alps")))
  json.dump({"version": 99, "jobs": []}, open(path, "w"))
  check("one of another version too", not jobcache.load(ClusterSession("daint", "daint-alps")))
  # only the categories: the jobs stay as they are in the file
  jobcache.save(c)
  c.categories.assign(["200"], "proj/arrays"); c.pending_categories.append(["assign", ["200"], "proj/arrays", {}])
  jobcache.save_pending(c)
  f = ClusterSession("daint", "daint-alps"); jobcache.load(f)
  check("save_pending: the categories and the changes, the jobs as they were", f.categories.category_of("200_1") == "proj/arrays"
        and len(f.pending_categories) == 2 and sorted(f.jobs) == ["100", "101", "200_1"])
  # the changes, replayed on the file (a local tab: the config directory), one that no longer fits is skipped
  store = cat.Store("daint")
  ops = [["add", "a/b"], ["assign", ["1", "2"], "a/b", {"1": "x"}], ["rename", "a/b", "a/c"], ["rename", "gone", "x"], ["unassign", ["2"]], ["delete", "a/c"]]
  skipped = []
  for op in ops:
    try: cat.apply_op(store, op)
    except ValueError as e: skipped.append(op[0])
  check("the changes in their order; one that no longer fits is skipped", skipped == ["rename"] and store.category_of("1") == "a" and store.category_of("2") == "", (skipped, store.to_data()))
  try: cat.apply_op(store, ["__init__", "x"]); refused = False
  except ValueError: refused = True
  check("only the known changes (a kept file cannot call anything else)", refused)
