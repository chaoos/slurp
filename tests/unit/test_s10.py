"""S10: Slurm variables a job does not get (only set with an option it lacks), typos of Slurm's names; S06 leaves
the variables of the job's environment alone"""
from slurp.checks import engine as c
from slurp.checks.engine import Facts
from slurp.cluster.jobs import Job

FACTS = Facts(env={"HOME": "/users/me", "SCRATCH": "/scratch/me", "USER": "me"}, user="me")


def found(directives, body, job=None, rule="S10"):
  text = "#!/bin/bash -l\n#SBATCH --time=01:00:00\n" + "".join(f"#SBATCH {d}\n" for d in directives) + body + "\n"
  findings, _ = c.check_script_text(text, "/users/me/job.sh", facts=FACTS, job=job)
  return [((f.severity or f.rule.severity), f.evidence) for f in findings if f.rule.id == rule]


def test_conditional_variables(check):
  got = found(["--nodes=1"], "export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK\nsrun ./app")
  check("SLURM_CPUS_PER_TASK without --cpus-per-task: empty; a script may still get it (a hint)",
        got == [("info", "$SLURM_CPUS_PER_TASK is only set with --cpus-per-task, which this script does not have (unless you submit with it): it is empty")], got)
  check("with --cpus-per-task: nothing", not found(["--cpus-per-task=16"], "export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK"))
  got = found([], "./run input_${SLURM_ARRAY_TASK_ID}.dat")
  check("SLURM_ARRAY_TASK_ID without --array", got and "only set with --array" in got[0][1], got)
  check("…with --array: nothing", not found(["--array=1-10"], "./run input_${SLURM_ARRAY_TASK_ID}.dat"))
  check("a default (${…:-1}): nothing", not found([], "export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-1}"))
  check("a script that asks whether it is set: nothing", not found([], 'if [ -n "$SLURM_ARRAY_TASK_ID" ]; then i=$SLURM_ARRAY_TASK_ID; fi'))
  check("set by the script itself: nothing", not found([], "SLURM_CPUS_PER_TASK=4\necho $SLURM_CPUS_PER_TASK"))
  check("always set (SLURM_JOB_ID, SLURM_NTASKS, SLURM_PROCID): nothing", not found([], "echo $SLURM_JOB_ID $SLURM_NTASKS $SLURM_PROCID"))
  check("in single quotes (not expanded): nothing", not found([], "echo '$SLURM_CPUS_PER_TASK'"))


def test_a_job(check):
  from slurp.cluster.jobs import SACCT_FIELDS
  rec = dict.fromkeys(SACCT_FIELDS, "") | {"JobID": "7", "JobIDRaw": "7", "JobName": "job", "State": "RUNNING",
                                          "SubmitLine": "sbatch job.sh", "WorkDir": "/scratch/me", "NNodes": "1"}
  got = found([], "export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK", job=Job(rec=rec))
  check("a job (how it was submitted is known): a warning", got and got[0][0] == "warning" and "this job does not have" in got[0][1], got)
  rec["SubmitLine"] = "sbatch --cpus-per-task=8 job.sh"
  check("…given on the command line: nothing", not found([], "export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK", job=Job(rec=rec)))


def test_typos(check):
  got = found(["--array=1-4"], "./run $SLURM_ARRAY_TASKID")
  check("a typo of a Slurm name: did you mean", got and "did you mean $SLURM_ARRAY_TASK_ID" in got[0][1], got)
  check("a SLURM_ name of a site (not close to any): nothing", not found([], "echo $SLURM_MYSITE_THING"))
  check("S06 leaves SLURM_ and the job's own variables alone", not found([], "echo $SLURMD_NODENAME $ENVIRONMENT $ROCR_VISIBLE_DEVICES $SLURM_JOB_ID", rule="S06"))
