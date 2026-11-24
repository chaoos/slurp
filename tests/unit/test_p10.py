"""P10: an argument of a program the job runs (srun ./binary --flag a/b/c) that looks like a path, but is not there
where that line runs (--chdir, the script's cd, srun --chdir); an output option's path only needs its directory; a path
an earlier line names is not checked; not paths: options, URLs, host:path, globs, fractions; node-local places not;
only scripts and jobs that wait"""
from slurp.checks import engine as c
from slurp.checks.engine import Facts, PathInfo
from slurp.cluster.jobs import Job, SACCT_FIELDS

ENV = {"HOME": "/users/me", "SCRATCH": "/scratch/me"}


def run(body, there=(), job=None):
  """The P10 findings [(line, severity, text)] and the argument paths probed; there: the paths that exist"""
  text = "#!/bin/bash\n#SBATCH --chdir=/scratch/me/base\n" + body + "\n"
  ctx = c.make_context(text, "/users/me/job.sh", facts=Facts(env=ENV))
  wanted = c.interesting_paths(ctx)
  exist = set(there) | {"/scratch/me/base", "/scratch/me/run", "/scratch/me"}
  probes = {f"{k}:{p}": PathInfo(p in exist, p in exist and "." not in p.rsplit("/", 1)[-1], True, True, 1, 0, "ELF") for k, p in wanted}
  findings, _ = c.check_script_text(text, "/users/me/job.sh", facts=Facts(env=ENV), probes=probes, job=job)
  return [(f.line, f.severity or f.rule.severity, f.evidence) for f in findings if f.rule.id == "P10"], [p for k, p in wanted if k == "a"]


def test_an_argument_not_there(check):
  got, _ = run("srun ./binary --flag a/b/c")
  check("srun ./binary --flag a/b/c: said, from --chdir; neither it nor its directory", len(got) == 1 and got[0][0] == 3
        and got[0][1] == "warning" and "--flag a/b/c (/scratch/me/base/a/b/c): neither it nor its directory /scratch/me/base/a/b exists" in got[0][2], got)
  got, _ = run("srun ./binary --flag a/b/c", there={"/scratch/me/base/a/b/c"})
  check("…there: nothing", got == [], got)
  got, _ = run("cd /scratch/me/run\nsrun ./binary --in=input.dat")
  check("after a cd: from there; --in=x as written", len(got) == 1 and "--in=input.dat (/scratch/me/run/input.dat) does not exist" in got[0][2], got)
  got, _ = run("srun --chdir=/opt/z ./tool data/x\npython run.py --config cfg/a.yaml\nmpirun -np 4 ./app $SCRATCH/in.h5")
  check("srun --chdir, a python script's arguments, mpirun, $VARS", [g[2].split(" ")[0 if "(" in g[2].split(" ")[1] else 1] for g in got] == ["data/x", "cfg/a.yaml", "$SCRATCH/in.h5"], got)


def test_outputs_and_paths_made_before(check):
  got, _ = run("srun ./binary -o results/out.h5 --save-dir=ckpt/a", there={"/scratch/me/base/results", "/scratch/me/base/ckpt"})
  check("an output option, its directory there: nothing (the program writes it)", got == [], got)
  got, _ = run("srun ./binary --output newdir/out.h5")
  check("…its directory not there: said, with mkdir -p", len(got) == 1 and "newdir" in got[0][2], got)
  got, paths = run("mkdir -p prep\n./make --to prep/in.h5 other/x.h5\nsrun ./binary prep/in.h5 other/x.h5\ncp ~/a.tar stage/\nsrun ./binary stage/a.tar")
  check("made by mkdir, or named by a line before (written there, maybe): not checked again",
        not any("prep/" in g[2] for g in got) and [g[0] for g in got if "other/x.h5" in g[2]] == [4] and not any("stage" in g[2] for g in got), got)
  got, paths = run("./prep ./ ../x\nsrun ./binary data/in.h5")
  check("./ or .. named before: what is below them still checked", any("data/in.h5" in g[2] for g in got), got)


def test_not_paths(check):
  _, paths = run('srun ./binary --gpus 1/2 http://x.org/a/b gpu:1 "*.h5" host:/data/x 0.5 a,b/c --name=run '
                 "/tmp/x /dev/shm/y {{input}} plain $UNKNOWN/x")
  check("options, fractions, URLs, host:path, globs, lists, /tmp and /dev (other on the nodes), placeholders, words "
        "without / or a data ending, unknown $VARS: not checked", paths == [], paths)
  _, paths = run("echo a/b/c\ncp a/b c/d\nrm -rf x/y\ntar xf in/a.tar")
  check("shell commands: their arguments not (only programs run by path, launched, or python scripts)", paths == [], paths)
  _, paths = run("python -c 'import a' x/y.py\npython -m pkg.mod in/a.h5")
  check("python -c, -m: not", paths == [], paths)


def test_only_scripts_and_waiting_jobs(check):
  def job(state):
    return Job(rec=dict.fromkeys(SACCT_FIELDS, "") | {"JobID": "7", "JobIDRaw": "7", "JobName": "job", "State": state,
                                                       "WorkDir": "/scratch/me/base", "NNodes": "1"})
  got, _ = run("srun ./binary --flag a/b/c", job=job("PENDING"))
  check("a job that waits: said", len(got) == 1, got)
  for state in ("RUNNING", "COMPLETED", "FAILED"):
    got, _ = run("srun ./binary --flag a/b/c", job=job(state))
    check(f"a job {state}: not (its inputs may be gone since, its outputs are being written)", got == [], got)
