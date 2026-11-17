"""P09: the program (or an interpreter's script) that many tasks start lives in $HOME / $PROJECT, not on scratch"""
from slurp.checks import engine as c
from slurp.checks.engine import Facts, Partition

FACTS = Facts(partitions={"normal": Partition("normal", True, 1440, 288, 460000, 4, 4, 72, 1, False)}, accounts=["p"],
              env={"HOME": "/users/me", "SCRATCH": "/capstor/scratch/cscs/me", "PROJECT": "/capstor/store/cscs/proj",
                   "USER": "me"}, user="me")


def p09(body, tasks="--nodes=4 --ntasks-per-node=64", chdir="/users/me/run", replay=None):
  text = (f"#!/bin/bash -l\n#SBATCH {tasks.split()[0]}\n" + "".join(f"#SBATCH {t}\n" for t in tasks.split()[1:])
          + f"#SBATCH --time=01:00:00\n#SBATCH --chdir={chdir}\n{body}\n")
  findings, _ = c.check_script_text(text, "/users/me/run/job.sh", facts=FACTS, replay=replay)
  return [(f.severity or f.rule.severity, f.evidence) for f in findings if f.rule.id == "P09"]


def test_where_the_program_is(check):
  got = p09("srun ./sim input.dat")
  check("256 tasks, the program in home (the job's directory): a warning, which and where", got and got[0][0] == "warning"
        and "256 tasks start the program /users/me/run/sim, which is in $HOME" in got[0][1], got)
  check("on scratch: nothing", not p09("srun $SCRATCH/run/sim"))
  got = p09("srun /capstor/store/cscs/proj/bin/sim")
  check("in project: said", got and "$PROJECT" in got[0][1], got)
  check("the software stack (/user-environment, /usr): nothing", not p09("srun /user-environment/bin/sim") and not p09("srun /usr/bin/hostname"))
  check("copied to the nodes first (srun --bcast, sbcast): nothing", not p09("srun --bcast=/tmp/sim ./sim")
        and not p09("sbcast ./sim /tmp/sim\nsrun /tmp/sim"))


def test_how_many_tasks(check):
  check("8 tasks: nothing", not p09("srun ./sim", tasks="--nodes=1 --ntasks-per-node=8"))
  got = p09("srun ./sim", tasks="--nodes=1 --ntasks-per-node=32")
  check("32 tasks: a hint (info)", got and got[0][0] == "info", got)
  got = p09("srun ./sim", tasks="--ntasks=4096")
  check("4096 tasks: a warning", got and got[0][0] == "warning" and "4096 tasks" in got[0][1], got)


def test_an_interpreter_and_a_program_from_path(check):
  got = p09("srun python $HOME/train.py --epochs 3")
  check("python with a script in home: the script", got and "the script /users/me/train.py" in got[0][1], got)
  check("python from the stack, the script on scratch: nothing", not p09("srun python $SCRATCH/train.py"))
  got = p09("srun sim", replay={"resolved": {"sim": "/users/me/bin/sim"}})
  check("a name found on PATH in home (the environment check): said", got and "/users/me/bin/sim" in got[0][1], got)
  check("…found in the software stack: nothing", not p09("srun sim", replay={"resolved": {"sim": "/user-environment/bin/sim"}}))
  check("no scratch on the cluster: nothing", not c.check_script_text(
    "#!/bin/bash\n#SBATCH --ntasks=512\nsrun ./sim\n", "/users/me/job.sh",
    facts=Facts(env={"HOME": "/users/me"}))[0] or all(f.rule.id != "P09" for f in c.check_script_text(
    "#!/bin/bash\n#SBATCH --ntasks=512\nsrun ./sim\n", "/users/me/job.sh", facts=Facts(env={"HOME": "/users/me"}))[0]))
