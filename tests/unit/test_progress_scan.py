"""How much of a file the progress is read from, and over how many values the current pace goes: set in the Options
(config.json), within limits; picked up when the config changes"""
import json
import os
import time
import types

from slurp.cluster import jobs as J


def write(tmp_path, **values):
  path = tmp_path / "slurp" / "config.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(values))
  os.utime(path, (time.time() + len(J._scan_cache) + 1,) * 2) # (a new mtime each time)


def test_the_settings(check, tmp_path, monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  J._scan_cache.clear()
  check("none set: the defaults", J.progress_scan() == {"bytes": 64 * 1024, "lines": 600, "pace": 6}, J.progress_scan())
  write(tmp_path, progress_tail_kib=8, progress_tail_lines=50, progress_pace_points=3)
  check("set: used (the config read again when it changed)", J.progress_scan() == {"bytes": 8 * 1024, "lines": 50, "pace": 3}, J.progress_scan())
  write(tmp_path, progress_tail_kib=10 ** 9, progress_tail_lines="x", progress_pace_points=1)
  check("out of range: the limits; not a number: the default", J.progress_scan() == {"bytes": 65536 * 1024, "lines": 600, "pace": 2}, J.progress_scan())
  write(tmp_path, progress_tail_lines=20)
  tail = "".join(f"step {i}/100\n" for i in range(1, 11)) + "".join(f"noise {i}\n" for i in range(30))
  check("the lines: progress further back than them is not seen", J.output_progress("x.log", tail=tail) is None)
  write(tmp_path, progress_tail_lines=100)
  check("more lines: it is", J.output_progress("x.log", tail=tail).current == 10)


def test_the_pace(check, tmp_path, monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  J._scan_cache.clear()
  # (slow at first, fast lately: 1 per minute, then 10 per minute)
  lines = [f"2026-11-17T10:{m:02d}:00 step {m}/1000" for m in range(0, 20)] + \
          [f"2026-11-17T10:{m:02d}:00 step {19 + 10 * (m - 19)}/1000" for m in range(20, 23)]
  job = types.SimpleNamespace(jid="77", rec={"Start": "2026-11-17T10:00:00"})
  def rate(points):
    write(tmp_path, progress_pace_points=points)
    return J.estimate_progress(job, J.output_progress("x.log", tail="\n".join(lines)))["recent"][0] * 60
  check("over the last 3 values: the fast pace", abs(rate(3) - 10) < 0.01, rate(3))
  check("over the last 20: mostly the slow one", rate(20) < 3, rate(20))
