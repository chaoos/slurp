"""Paths of a script that exist but cannot be read (or entered): the probe says so, P02/P03/P07 report it"""
import os

import pytest

from slurp.checks import engine as c
from slurp.checks.engine import PathInfo

HEAD = "#!/bin/bash -l\n#SBATCH --time=00:10:00\n"


def found(script, probes, rule):
  ctx_findings, _ = c.check_script_text(script, "/scratch/me/job.sh", probes=probes)
  return [x.evidence for x in ctx_findings if x.rule.id == rule]


def test_unreadable_paths_are_reported(check):
  ok = PathInfo(True, False, True, True)
  probes = {"f:/scratch/me/in.dat": PathInfo(True, readable=False), "f:/scratch/me/env.sh": ok}
  got = found(HEAD + "source /scratch/me/env.sh\n./sim < /scratch/me/in.dat\n", probes, "P02")
  check("an input you may not read: P02", any("cannot read" in e and "in.dat" in e for e in got) and not any("env.sh" in e for e in got), got)
  got = found(HEAD + "cd /scratch/other\n", {"d:/scratch/other": PathInfo(True, True, False, False)}, "P02")
  check("a directory you cannot enter: P02", any("cannot enter" in e for e in got), got)
  got = found(HEAD + "cd /scratch/other\n", {"d:/scratch/other": PathInfo(True, True, True, False, readable=False)}, "P02")
  check("one you may enter but not list: fine", not got, got)
  script = PathInfo(True, False, True, False, 10, 0, "Bourne-Again shell script, ASCII text executable", readable=False)
  got = found(HEAD + "srun /scratch/me/run.sh\n", {"x:/scratch/me/run.sh": script}, "P03")
  check("a script with x but not r: P03", any("cannot read" in e for e in got), got)
  binary = PathInfo(True, False, True, False, 10, 0, "ELF 64-bit LSB executable", readable=False)
  got = found(HEAD + "srun /scratch/me/sim\n", {"x:/scratch/me/sim": binary}, "P03")
  check("a program with x but not r: runs (fine)", not got, got)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_the_probe_sees_it(check, tmp_path, monkeypatch):
  from slurp.cluster import remote
  monkeypatch.setattr(remote, "conn", lambda: None)
  f = tmp_path / "secret"; f.write_text("x"); f.chmod(0o200)
  d = tmp_path / "closed"; d.mkdir(); d.chmod(0o600)
  g = tmp_path / "fine"; g.write_text("x")
  try:
    got = c.run_probe([f"f:{f}", f"d:{d}", f"f:{g}", f"f:{tmp_path}/none"])
  finally:
    d.chmod(0o700)
  check("a file without r: not readable", got[f"f:{f}"].exists and not got[f"f:{f}"].readable, got[f"f:{f}"])
  check("a directory without x: cannot be entered", got[f"d:{d}"].isdir and not got[f"d:{d}"].executable, got[f"d:{d}"])
  check("a readable file: readable", got[f"f:{g}"].readable)
  check("a missing one: does not exist", not got[f"f:{tmp_path}/none"].exists)
