"""Q08: core dumps in $HOME. A crash writes its core file into the program's working directory (where sbatch was run,
--chdir, the script's cd, srun --chdir), not where the program or the script is; only if core files are allowed (the
script's ulimit -c, else the login shell's: Slurm passes it on); core files already there: said, with their size"""
import os
import subprocess
import time

from slurp.checks import engine as c
from slurp.checks.engine import Facts, PathInfo
from slurp.cluster.jobs import Job, SACCT_FIELDS

HOME, SCRATCH = "/users/me", "/capstor/scratch/cscs/me"


def facts(soft="unlimited", hard="unlimited", pattern="core"):
  return Facts(env={"HOME": HOME, "SCRATCH": SCRATCH, "USER": "me"}, user="me", core={"soft": soft, "hard": hard, "pattern": pattern})


def job(workdir, submit="sbatch job.sh", start="2026-11-20T10:00:00"):
  rec = dict.fromkeys(SACCT_FIELDS, "") | {"JobID": "7", "JobIDRaw": "7", "JobName": "job", "State": "FAILED", "SubmitLine": submit,
                                          "WorkDir": workdir, "NNodes": "1", "Start": start}
  return Job(rec=rec)


def found(body, directives=(), f=None, **kw):
  text = "#!/bin/bash -l\n#SBATCH --time=01:00:00\n" + "".join(f"#SBATCH {d}\n" for d in directives) + body + "\n"
  findings, _ = c.check_script_text(text, "/users/me/src/job.sh", facts=f or facts(), **kw)
  return [x for x in findings if x.rule.id == "Q08"]


def test_where_the_programs_run(check):
  q = found("srun ./sim", job=job(HOME))
  check("sbatch run in home, the login shell allows core files: said, why (no --chdir; the shell's limit, passed on)",
        q and "sbatch was run there, without --chdir" in q[0].evidence and "ulimit -c unlimited" in q[0].evidence
        and "passes the limits" in q[0].evidence and "ulimit -c 0" in q[0].snippet, [x.evidence for x in q])
  check("sbatch run in scratch, the program in home: nothing (the program's place does not count)",
        not found(f"srun {HOME}/bin/sim", job=job(SCRATCH + "/run")))
  q = found("srun ./sim", [f"--chdir={HOME}/run"])
  check("--chdir in home (a script alone): said", q and "--chdir=/users/me/run" in q[0].evidence, [x.evidence for x in q])
  q = found("cd ~\n./sim", job=job(SCRATCH + "/run"))
  check("submitted in scratch, but the script changes to home: said, with its line", q and "cd ~ (line 3)" in q[0].evidence
        and q[0].line == 3, [x.evidence for x in q])
  q = found("cd $HOME/runs/a || exit 1\nsrun ./sim", job=job(SCRATCH))
  check("…cd $HOME/…: the directory", q and f"{HOME}/runs/a" in q[0].evidence, [x.evidence for x in q])
  check("submitted in home, changes to scratch before the program: nothing", not found(f"cd {SCRATCH}/run || exit 1\nsrun ./sim", job=job(HOME)))
  check("…cd $SCRATCH: nothing", not found("cd $SCRATCH/run\nsrun ./sim", job=job(HOME)))
  q = found(f"srun --chdir={HOME}/x ./sim", job=job(SCRATCH))
  check("srun --chdir to home: said", q and "its --chdir" in q[0].evidence, [x.evidence for x in q])
  check("a script alone without --chdir: where it is submitted is not known, nothing", not found("srun ./sim"))
  check("echo, mkdir in home before the cd: not programs that crash", not found(f"mkdir -p {SCRATCH}/run\ncd {SCRATCH}/run\nsrun ./sim", job=job(HOME)))


def test_whether_core_files_are_allowed(check):
  check("the login shell has ulimit -c 0: nothing", not found("srun ./sim", job=job(HOME), f=facts(soft="0")))
  q = found("ulimit -c unlimited\nsrun ./sim", job=job(HOME), f=facts(soft="0"))
  check("…but the script allows them: said, with its line", q and "the script allows them (ulimit -c unlimited, line 3)" in q[0].evidence,
        [x.evidence for x in q])
  check("…the hard limit 0: it cannot", not found("ulimit -c unlimited\nsrun ./sim", job=job(HOME), f=facts(soft="0", hard="0")))
  check("the script switches them off: nothing", not found("ulimit -c 0\nsrun ./sim", job=job(HOME)))
  check("handed to a program (systemd-coredump, apport): nothing", not found("srun ./sim", job=job(HOME), f=facts(pattern="|/usr/lib/systemd/systemd-coredump %P")))
  check("not known (no facts of the limit): nothing", not found("srun ./sim", job=job(HOME), f=Facts(env={"HOME": HOME, "SCRATCH": SCRATCH})))
  q = found("srun ./sim", job=job(SCRATCH), f=facts(pattern=f"{HOME}/cores/core.%p"))
  check("the cluster writes them to one place in home: said", q and "core_pattern" in q[0].evidence, [x.evidence for x in q])


def test_core_files_there(check):
  newest = time.mktime(time.strptime("2026-11-20 11:30", "%Y-%m-%d %H:%M"))
  probes = {f"c:{HOME}": PathInfo(True, True, file=f"128 {300 * 1024 ** 3} {int(newest)}")}
  q = found("srun ./sim", job=job(HOME), probes=probes, f=facts(soft="0"), logs={"stderr": "srun: error: nid001: task 3: Segmentation fault (core dumped)"})
  check("core files there (even with the limit 0 here: the nodes may differ): how many, how big, from this job, why",
        q and "128 core files (300.0 GB) in /users/me" in q[0].evidence and "after this job started" in q[0].evidence
        and "sbatch was run there" in q[0].evidence and "core dumped" in q[0].evidence, [x.evidence for x in q])
  check("…in home and big: an error, how to find and delete them", q and q[0].severity == "error" and "find /users/me -maxdepth 1" in q[0].fix)


def test_the_probe_finds_core_files(check, tmp_path):
  (tmp_path / "core").write_bytes(b"x" * 10)
  (tmp_path / "core.1234").write_bytes(b"x" * 5)
  (tmp_path / "core.py").write_text("not a core file")
  (tmp_path / "score.1").write_text("no")
  out = subprocess.run(["sh", "-c", c.PROBE_SCRIPT, "sh", f"c:{tmp_path}"], capture_output=True, text=True).stdout
  fields = out.rstrip("\n").split("\t")
  n, size, newest = (int(x) for x in fields[5].split())
  check("core and core.<pid>, not core.py: their number, size, newest", (n, size) == (2, 15) and abs(newest - time.time()) < 60, fields)
  ctx = c.make_context("#!/bin/bash\ncd ~/run\n./sim\n", "/x/job.sh", facts=facts())
  check("the probe asks for the directories the programs run in", ("c", f"{HOME}/run") in c.interesting_paths(ctx))
  check("the facts: ulimit and core_pattern of a login shell (what it prints besides: left out)",
        c.parse_core_facts("Welcome!\nslurp-core-soft=0\nslurp-core-hard=unlimited\nslurp-core-pattern=|/bin/false %p\n")
        == {"soft": "0", "hard": "unlimited", "pattern": "|/bin/false %p"})
  real = subprocess.run(["bash", "-c", c.CORE_FACTS], capture_output=True, text=True).stdout
  check("…of a real shell", set(c.parse_core_facts(real)) == {"soft", "hard", "pattern"} and os.path.exists("/proc"), real)
