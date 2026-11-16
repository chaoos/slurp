"""The model of the categories (slurp/categories.py): paths, jobs and arrays, rename/move, delete, the file's data,
the numbers of a category; and the file itself (read, change, a broken one is not overwritten), isolated"""
import json, os
from slurp import categories as c


def test_categories(check, tmp_path, monkeypatch):
  T = str(tmp_path)
  monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(T, "config"))
  check("normalize", c.normalize(" project X / production runs/ ") == "project X/production runs")
  for bad in ("", "/", "a/" + "x" * 81, "a\nb", "/".join("abcdefghi")):
    try: c.normalize(bad); check(f"refused {bad!r}", False)
    except ValueError: check(f"refused {bad[:12]!r}", True)
  s = c.Store("daint")
  s.assign(["100", "200_3"], "proj/prod", {"100": "run1"})
  check("assign makes the category and its parents", s.paths == {"proj", "proj/prod"} and s.category_of("100") == "proj/prod" and s.jobs["100"]["name"] == "run1")
  check("an array task: its own, else its array's", s.category_of("200_3") == "proj/prod" and s.category_of("100_7") == "proj/prod" and s.category_of("300") == "")
  s.assign(["200"], "proj/test")
  check("the whole array: its tasks go with it", "200_3" not in s.jobs and s.category_of("200_3") == "proj/test")
  s.assign(["200_5"], "proj/test")
  check("a task into its array's category: nothing extra", "200_5" not in s.jobs)
  s.assign(["200_6"], "proj/prod")
  check("a task elsewhere: its own", s.category_of("200_6") == "proj/prod" and s.category_of("200_5") == "proj/test")
  check("children and recursive jobs", s.children() == ["proj"] and s.children("proj") == ["proj/prod", "proj/test"]
        and s.job_ids("proj") == ["100", "200", "200_6"] and s.job_ids("proj", recursive=False) == [] and s.count("proj/prod") == 2)
  s.rename("proj/prod", "archive/prod")
  check("rename moves the category, its jobs, makes the new parents", s.category_of("100") == "archive/prod" and "archive" in s.paths and "proj/prod" not in s.paths)
  try: s.rename("archive", "archive/sub"); check("not into itself", False)
  except ValueError: check("not into itself", True)
  s.add("archive/prod/deep")
  s.assign(["400"], "archive/prod/deep")
  up = s.delete("archive/prod")
  check("delete: the category and those in it; their jobs go one up", up == "archive" and s.category_of("400") == "archive" and s.category_of("100") == "archive"
        and not any(c.within(p, "archive/prod") for p in s.paths))
  s.delete("archive")
  check("delete at the top: its jobs are in none", s.category_of("100") == "" and "archive" not in s.paths)
  s2 = c.Store("daint", s.to_data())
  check("round trip through the file's data", s2.to_data() == s.to_data())
  check("bad data is skipped", c.Store("x", {"categories": ["", "ok"], "jobs": {"12": {"category": ""}, "x;rm": {"category": "a"}, "13": "no"}}).to_data() == {"categories": ["ok"], "jobs": {}})
  class J:
    def __init__(self, jid, state, nodes, elapsed, cpus="0", tres="", part="normal"):
      self.jid, self.state = jid, state
      self.rec = {"NNodes": nodes, "Elapsed": elapsed, "AllocCPUS": cpus, "AllocTRES": tres, "Partition": part, "Account": "a1", "User": "u",
                  "Submit": "2026-11-01T10:00:00", "Start": "2026-11-01T10:05:00", "End": "2026-11-01T12:05:00" if state != "RUNNING" else "Unknown"}
  jobs = [J("1", "COMPLETED", "2", "02:00:00", "256", "billing=1,cpu=256,gres/gpu=8,node=2"), J("2_1", "FAILED", "1", "01:00:00", "128", "cpu=128,gres/gpu:gh200=4,node=1"),
          J("2_2", "RUNNING", "1", "00:30:00", "128"), J("3", "CANCELLED by 1", "4", "00:00:00")]
  m = c.summary(jobs)
  check("summary: jobs (an array once), tasks, states", m["jobs"] == 3 and m["tasks"] == 4 and m["states"] == {"completed": 1, "failed": 1, "running": 1, "cancelled": 1})
  check("summary: node, CPU, GPU hours", abs(m["node_hours"] - 5.5) < 1e-9 and abs(m["cpu_hours"] - (512 + 128 + 64)) < 1e-9 and abs(m["gpu_hours"] - 20) < 1e-9, m)
  check("summary: success among the ended ones", abs(m["success"] - 1/3) < 1e-9)
  # the file (a local tab: the config directory)
  path = c.file_path()
  check("a local tab: the file is in the config directory", path == os.path.join(T, "config", "slurp", "categories.json"), path)
  check("no file: no categories", c.load("daint").to_data() == {"categories": [], "jobs": {}})
  store, _ = c.change("daint", lambda st: st.assign(["7"], "a/b"))
  store, _ = c.change("eiger", lambda st: st.assign(["7"], "other"))
  data = json.load(open(path))
  check("one part per Slurm cluster (the same job id on two clusters)", data["clusters"]["daint"]["jobs"]["7"]["category"] == "a/b"
        and data["clusters"]["eiger"]["jobs"]["7"]["category"] == "other" and c.load("daint").category_of("7") == "a/b", data)
  check("written whole (no temporary file left)", sorted(os.listdir(os.path.dirname(path))) == ["categories.json"])
  open(path, "w").write("{ broken")
  try: c.change("daint", lambda st: st.add("x")); refused = False
  except ValueError as e: refused = "not valid JSON" in str(e)
  check("a broken file is not overwritten (and says so)", refused and open(path).read() == "{ broken")
