"""Q07: the job's logs (--output, --error, or slurm-%j.out in the working directory) are on scratch, not in home or
project storage"""
from slurp.checks import engine as c
from slurp.checks.engine import Facts, Partition

SCRATCH = "/capstor/scratch/cscs/me"
FACTS = Facts(partitions={"normal": Partition("normal", True, 1440, 288, 460000, 4, 4, 72, 1, False)}, accounts=["p"],
              env={"HOME": "/users/me", "SCRATCH": SCRATCH, "PROJECT": "/capstor/store/cscs/proj", "USER": "me"}, user="me")


def found(directives, body="srun ./app", facts=FACTS):
  text = "#!/bin/bash -l\n#SBATCH --time=01:00:00\n" + "".join(f"#SBATCH {d}\n" for d in directives) + body + "\n"
  findings, _ = c.check_script_text(text, "/users/me/job.sh", facts=facts)
  return {f.rule.id: f for f in findings if f.rule.id in ("Q07", "Q03")}, [f for f in findings if f.rule.id == "Q07"]


def test_logs_on_scratch(check):
  ids, q = found(["--output=/users/me/logs/%x-%j.out"])
  check("--output in home: said, with the path of scratch to use (no variables in #SBATCH)", q and "--output writes to /users/me/logs/"
        in q[0].evidence and "$HOME" in q[0].evidence and q[0].snippet == f"#SBATCH --output={SCRATCH}/logs/%x-%j.out", [f.evidence for f in q])
  check("--output on scratch: nothing", not found([f"--output={SCRATCH}/logs/%x-%j.out"])[1])
  ids, q = found([f"--output={SCRATCH}/o.out", "--error=/capstor/store/cscs/proj/e.err"])
  check("--error in project: said", len(q) == 1 and "--error" in q[0].evidence and "$PROJECT" in q[0].evidence, [f.evidence for f in q])
  check("/dev/null: nothing", not found(["--output=/dev/null", "--error=/dev/null"])[1])


def test_relative_and_default_logs(check):
  check("relative, before submitting (no --chdir): not known where, nothing", not found(["--output=logs/%j.out"])[1])
  ids, q = found(["--chdir=/users/me/run", "--output=logs/%j.out"])
  check("relative with --chdir in home: said", q and "/users/me/run/logs/" in q[0].evidence, [f.evidence for f in q])
  ids, q = found(["--chdir=/users/me/run"], body=f"cd {SCRATCH}/run\nsrun ./app")
  check("no --output, the job in home but it changes to scratch: the default log stays in home (opened before the cd)",
        q and "slurm-<job id>.out goes to the working directory /users/me/run" in q[0].evidence and "only after" in q[0].evidence, [f.evidence for f in q])
  ids, q = found(["--chdir=/users/me/run"])
  check("no --output and the whole job in home: Q03 says it, not Q07 too", "Q03" in ids and not q, sorted(ids))
  ids, q = found(["--chdir=/capstor/store/cscs/proj/run"])
  check("…in project: Q07 (Q03 is about home)", q and "$PROJECT" in q[0].evidence, [f.evidence for f in q])


def test_severity_and_no_scratch(check):
  q = found(["--output=/users/me/o.out", "--ntasks=512"])[1]
  check("many tasks (all their output in this file): a warning", q and q[0].severity == "warning")
  q = found(["--output=/users/me/o.out"])[1]
  check("else a hint", q and (q[0].severity or q[0].rule.severity) == "info")
  check("no scratch on the cluster: nothing", not found(["--output=/users/me/o.out"], facts=Facts(env={"HOME": "/users/me"}))[1])
