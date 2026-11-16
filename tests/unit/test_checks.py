"""The rule corpus: scripts with the findings they must (and must not) give, without a cluster (facts made up)"""
from slurp.checks import engine as c
from slurp.checks.engine import Facts, Partition, PathInfo


def test_rule_corpus(check, tmp_path, monkeypatch):
  facts = Facts(partitions={"normal": Partition("normal", True, 1440, 288, 460000, 4, 4, 72, 1, False),
                            "debug": Partition("debug", False, 30, 288, 460000, 4, 4, 72, 1, False),
                            "cpu": Partition("cpu", False, 2880, 256, 500000, 0, 2, 64, 2, False)},
                accounts=["proj1", "proj2"], qos={"normal"}, sbatch_options=c.LONG_OPTIONS | {"uenv"},
                slurm_version=(23, 2), arch="aarch64", env={"HOME": "/users/me", "SCRATCH": "/scratch/me", "USER": "me"},
                user="me", site={"scratch_purge_days": 30})
  def ids(text, probes=None, **kw):
    f, ctx = c.check_script_text(text, "/users/me/job.sh", facts=facts, probes=probes, **kw)
    return {x.rule.id for x in f}, f
  def has(name, text, want, not_want=(), probes=None):
    got, f = ids(text, probes)
    ok = all(w in got for w in want) and not any(w in got for w in not_want)
    check(name, ok, f"got {sorted(got)}: " + " | ".join(f"{x.rule.id} {x.evidence}" for x in f))
  GOOD = """#!/bin/bash -l
#SBATCH --job-name=good
#SBATCH --account=proj1
#SBATCH --partition=normal
#SBATCH --time=01:00:00
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=72
#SBATCH --gpus-per-task=1
#SBATCH --hint=nomultithread
#SBATCH --output=logs/%x-%j.out
#SBATCH --chdir=/scratch/me/run
set -eo pipefail
module purge
module load gcc/12.3.0
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export OMP_PLACES=cores
export OMP_PROC_BIND=close
export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK
cd "$SCRATCH/run"
srun --cpu-bind=cores ./app input.dat
"""
  got, f = ids(GOOD)
  check("the good template has no errors or warnings", not [x for x in f if x.severity != "info"], [(x.rule.id, x.evidence) for x in f])
  has("S01 CRLF", GOOD.replace("\n", "\r\n"), ["S01"])
  has("S01 long dash", GOOD.replace("--time", "–-time"), ["S01"])
  has("S02 no shebang", GOOD.split("\n", 1)[1], ["S02"])
  has("S02 sh with bash syntax", GOOD.replace("#!/bin/bash -l", "#!/bin/sh") + "if [[ -f x ]]; then echo; fi\n", ["S02"])
  has("S03 directive after a command", GOOD + "#SBATCH --mem=4G\n", ["S03"])
  has("S03 near directive", GOOD.replace("#SBATCH --job-name=good", "# SBATCH --job-name=good"), ["S03"])
  has("S04 misspelled option", GOOD.replace("--ntasks-per-node", "--ntask-per-nod"), ["S04"])
  has("S04 abbreviation is fine", GOOD.replace("--partition=normal", "--part=normal"), [], ["S04"])
  has("S05 variable in directive", GOOD.replace("logs/%x-%j.out", "$SCRATCH/logs/%j.out"), ["S05"])
  has("S06 typo variable", GOOD + "cd $SRATCH/x || exit\n", ["S06"])
  has("S07 cd without check", GOOD.replace("set -eo pipefail\n", ""), ["S07"])
  has("S07 not with set -e", GOOD, [], ["S07"])
  has("S08 background without wait", GOOD + "srun -n1 ./a &\n", ["S08"])
  has("S08 with wait fine", GOOD + "srun -n1 ./a &\nwait\n", [], ["S08"])
  has("R01 mem without unit", GOOD.replace("#SBATCH --hint", "#SBATCH --mem=64\n#SBATCH --hint"), ["R01"])
  has("R01 mem with unit fine", GOOD.replace("#SBATCH --hint", "#SBATCH --mem=64G\n#SBATCH --hint"), [], ["R01"])
  has("R02 mm:ss", GOOD.replace("01:00:00", "2:00"), ["R02"])
  has("R03 too many cpus", GOOD.replace("--cpus-per-task=72", "--cpus-per-task=128").replace("OMP_PLACES", "X"), ["R03"])
  has("R03 too many gpus", GOOD.replace("--gpus-per-task=1", "--gpus-per-task=2"), ["R03"])
  has("R04 contradictory", GOOD.replace("#SBATCH --hint", "#SBATCH --ntasks=16\n#SBATCH --hint"), ["R04"])
  has("R04 mem and mem-per-cpu", GOOD.replace("#SBATCH --hint", "#SBATCH --mem=4G\n#SBATCH --mem-per-cpu=1G\n#SBATCH --hint"), ["R04"])
  has("R05 time over partition max", GOOD.replace("--partition=normal", "--partition=debug"), ["R05"])
  has("R06 unknown partition", GOOD.replace("--partition=normal", "--partition=nromal"), ["R06"])
  has("R06 not my account", GOOD.replace("--account=proj1", "--account=other"), ["R06"])
  has("R06 no account with several", GOOD.replace("#SBATCH --account=proj1\n", ""), ["R06"])
  has("R07 gpus on cpu partition", GOOD.replace("--partition=normal", "--partition=cpu").replace("--cpus-per-task=72", "--cpus-per-task=32"), ["R07"])
  has("R09 OMP fixed mismatch", GOOD.replace("OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK", "OMP_NUM_THREADS=8"), ["R09"])
  has("B01 nodes without srun", GOOD.replace("srun --cpu-bind=cores ./app", "./app"), ["B01"])
  has("B03 array without task id", GOOD.replace("#SBATCH --hint", "#SBATCH --array=1-10\n#SBATCH --hint"), ["B03", "G07"])
  has("G07 not with %A_%a", GOOD.replace("#SBATCH --hint", "#SBATCH --array=1-10\n#SBATCH --hint").replace("%x-%j", "%x_%A_%a") + "echo $SLURM_ARRAY_TASK_ID\n", [], ["G07", "B03"])
  has("B05 fixed log name", GOOD.replace("logs/%x-%j.out", "out.txt"), ["B05"])
  has("B07 /tmp not copied", GOOD + "./app > /tmp/result.txt\n", ["B07"])
  has("B07 copied back", GOOD + "./app > /tmp/result.txt\ncp /tmp/result.txt $SCRATCH/\n", [], ["B07"])
  has("B08 export none", GOOD.replace("#SBATCH --hint", "#SBATCH --export=NONE\n#SBATCH --hint").replace("module purge\nmodule load gcc/12.3.0\n", ""), ["B08"])
  has("B13 mail per task", GOOD.replace("#SBATCH --hint", "#SBATCH --array=1-100\n#SBATCH --mail-type=END,ARRAY_TASKS\n#SBATCH --hint"), ["B13"])
  has("B14 srun in loop", GOOD + "for f in a b c; do\n  srun -n1 ./p $f\ndone\n", ["B14"])
  has("B14 inline loop", GOOD + "for f in a b c; do srun -n1 ./p $f; done\n", ["B14"])
  has("E01 unversioned", GOOD.replace("gcc/12.3.0", "gcc"), ["E01"])
  has("E02 conda no hook", GOOD + "conda activate env\n", ["E02"])
  has("N02 no OMP binding", GOOD.replace("export OMP_PLACES=cores\nexport OMP_PROC_BIND=close\n", ""), ["N02"])
  has("N03 tasks share gpus", GOOD.replace("--gpus-per-task=1", "--gpus-per-node=4"), ["N03"])
  has("N04 srun without cpus", GOOD.replace("export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK\n", ""), ["N04"])
  has("N04 fine with -c", GOOD.replace("export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK\n", "").replace("srun --cpu", "srun -c $SLURM_CPUS_PER_TASK --cpu"), [], ["N04"])
  has("U06 long job", GOOD.replace("01:00:00", "2-00:00:00").replace("--partition=normal", "--partition=cpu").replace("--cpus-per-task=72", "--cpus-per-task=32").replace("#SBATCH --gpus-per-task=1\n", ""), ["U06"])
  has("ignore comment mutes", GOOD.replace("gcc/12.3.0", "gcc") + "# slurp: ignore E01\n", [], ["E01"])
  probes = {"d:/scratch/me/run/logs": PathInfo(False), "d:/scratch/me/run": PathInfo(True, True, True, True),
            "x:/scratch/me/run/app": PathInfo(True, False, False, True, 100, 0, "ELF 64-bit LSB executable, x86-64, version 1")}
  has("P01 log dir missing, P03 not executable, P04 arch", GOOD.replace("srun --cpu-bind=cores ./app", "srun --cpu-bind=cores $SCRATCH/run/app"), ["P01", "P03"], probes=probes)
  probes["x:/scratch/me/run/app"] = PathInfo(True, False, True, True, 100, 0, "ELF 64-bit LSB executable, x86-64, version 1")
  has("P04 wrong architecture", GOOD.replace("srun --cpu-bind=cores ./app", "srun --cpu-bind=cores $SCRATCH/run/app"), ["P04"], probes=probes)
  has("P02 cd target missing", GOOD.replace('cd "$SCRATCH/run"', "cd /scratch/me/rnu1"), ["P02"], probes={"d:/scratch/me/rnu1": PathInfo(False)})
  has("P02 mkdir before cd is fine", GOOD.replace('cd "$SCRATCH/run"', "mkdir -p /scratch/me/new\ncd /scratch/me/new"), [], ["P02"], probes={"d:/scratch/me/new": PathInfo(False)})

  # --- fixes
  from slurp.checks import fixes, templates, environment, placement
  bad = GOOD.replace("\n", "\r\n").replace("module purge\r\n", "").replace("export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK\r\n", "")
  bad = bad.replace("logs/%x-%j.out", "out.txt") + "#SBATCH --mem=4G\r\nsrun -n1 ./a &\r\n"
  got, f = ids(bad)
  new, done = fixes.apply_fixes(bad, f)
  got2, f2 = ids(new)
  check("Fixed copy: S01 S03 B05 N04 E01 S08 fixed", all(x not in got2 for x in ("S01", "S03", "B05", "N04", "S08")) and "\r" not in new,
        (sorted(got), sorted(got2), done))
  check("Fixed copy: the moved directive is before the first command", new.index("#SBATCH --mem=4G") < new.index("set -eo"), new[:300])
  # --- templates: no errors or warnings on the cluster they are made for
  for kind, _ in templates.KINDS:
    d = templates.defaults(kind, facts.partition("normal"), 4)
    text = templates.make_script(kind, "t", "proj1", "normal", d["nodes"], d["ntasks_per_node"], d["cpus_per_task"], d["gpus_per_node"], d["time"], directory="/scratch/me/run")
    got, f = ids(text)
    bad = [(x.rule.id, x.evidence) for x in f if x.severity != "info" and x.rule.id not in ("P01",)]
    check(f"template {kind}: no errors or warnings", not bad, bad)
  # --- environment replay parsing
  out = """@@MODERR 6\tLmod has detected the following error: unknown module gcc/99
@@PROG ./sim\t/scratch/me/sim
	linux-vdso.so.1 (0x00007ffd)
	libmpi.so.40 => not found
	libc.so.6 => /lib64/libc.so.6 (0x0000)
@@CUDA ELF file    1: sim.1.sm_80.cubin
@@PROG ./serial\t/scratch/me/serial
	libc.so.6 => /lib64/libc.so.6 (0x0000)
"""
  r = environment.parse_replay(out)
  check("replay parse: missing libs, cuda archs, module errors", r["missing"]["./sim"] == ["libmpi.so.40"] and r["cuda"]["./sim"] == {"sm_80"}
        and r["module_errors"] and r["libs"]["./serial"] == ["libc.so.6"], r)
  script = GOOD.replace("srun --cpu-bind=cores ./app", "srun --cpu-bind=cores ./sim\nsrun -n 8 ./serial")
  got, f = ids(script, replay=r)
  check("P05 E04 B02 from the replay", {"P05", "E04", "B02"} <= got, sorted(got))
  facts.site["cuda_arch"] = "sm_90"
  got, f = ids(script, replay=r)
  check("P06 GPU code for other GPUs", "P06" in got)
  facts.site.pop("cuda_arch")
  check("replay script runs only setup lines", "srun" not in environment.replay_script(c.make_context(GOOD, "/users/me/job.sh", facts=facts)).split("@@PROG")[0])
  # --- placement analysis
  lines = ["HOST=nid1"] + [f"CPU={i},{i},{i // 4}" for i in range(16)] + ["GPU=0,00000009:01:00.0,0", "GPU=1,00000019:01:00.0,1",
           "GPUPROC=100,00000019:01:00.0",
           "PROC=0|0|100|app|0-3|4|0:1 1:1 2:1 3:1 |0", "PROC=0|1|101|app|2-5|4|2:1 3:1 4:1 5:1 |0", "PROC=0|2|102|app|8-15|20|8:20 |0-1"]
  res = placement.analyse({0: lines})
  probs = " | ".join(res["problems"])
  check("placement: shared CPUs, too many threads, spread over NUMA, GPU of another domain",
        "share 2 CPUs" in probs and "20 threads" in probs and "spread over NUMA" in probs and "uses GPU 1 of NUMA domain 1" in probs, probs)
  got, f = ids(GOOD, live=res)
  check("N05 from the placement", "N05" in got)
  # --- quota rules
  quota = {"entries": [{"kind": "user", "name": "me", "filesystem": "/scratch", "path": "/scratch/me", "kb": 950, "kb_soft": 0,
                        "kb_hard": 1000, "files": 10, "files_soft": 0, "files_hard": 100000, "over": False}]}
  got, f = ids(GOOD, quota=quota)
  check("Q01 quota almost full where the job writes", "Q01" in got, [(x.rule.id, x.evidence) for x in f])
  quota["entries"][0].update(kb=10, kb_hard=500000)
  got, f = ids(GOOD, quota=quota)
  check("Q02 little room left", "Q02" in got and "Q01" not in got, sorted(got))

  # --- G: good practice
  from slurp.cluster.jobs import Job, SACCT_FIELDS
  def job(jid="123", state="COMPLETED", wd="/scratch/me/run", stdout="", queued=False, steps=(), script=None, submit="sbatch job.sh"):
    rec = dict.fromkeys(SACCT_FIELDS, ""); rec.update(JobID=jid, JobIDRaw=jid, JobName="sim", WorkDir=wd, NNodes="2", NTasks="8",
                                                       State=state, StdOut=stdout, SubmitLine=submit, ExitCode="0:0", Elapsed="00:10:00", Timelimit="01:00:00")
    j = Job(rec); j.queued = queued; j.steps = list(steps); j.script = script; return j
  noset = GOOD.replace("module purge\nmodule load gcc/12.3.0\n", "").replace("set -eo pipefail\n", "")
  got, f = ids(noset.replace("srun --cpu-bind=cores ./app input.dat", "srun --cpu-bind=cores ./app $MYVAR"),
               submit_env={"LOADEDMODULES": "gcc/12.3.0:cray-mpich/8.1", "MYVAR": "in.dat", "PATH": "/usr/bin"})
  g01 = [x for x in f if x.rule.id == "G01"]
  check("G01: modules and variables of the submit shell the script does not set up (warning, with a snippet)",
        g01 and g01[0].severity == "warning" and "gcc/12.3.0" in g01[0].evidence and "$MYVAR" in g01[0].evidence and "module load gcc/12.3.0" in g01[0].snippet,
        [(x.rule.id, x.severity, x.evidence) for x in g01])
  got, f = ids(GOOD, submit_env={"LOADEDMODULES": "gcc/12.3.0:cray-mpich/8.1"})
  check("G01: not when the script purges and loads itself", "G01" not in got, sorted(got))
  got, f = ids(noset)
  g01 = [x for x in f if x.rule.id == "G01"]
  check("G01: a script without any setup is a hint (no evidence)", g01 and g01[0].severity == "info", [(x.rule.id, x.severity) for x in g01])
  got, f = ids(GOOD, replay={"fresh": True, "notfound": ["app"], "libs": {}, "missing": {}, "cuda": {}, "errors": [], "module_errors": []})
  check("G01: a program not found after a fresh login (replay)", any(x.rule.id == "G01" and "not found" in x.evidence for x in f), sorted(got))
  got, f = ids(GOOD.replace("#SBATCH --hint", "#SBATCH --uenv=prgenv-gnu/24.11\n#SBATCH --hint"),
               replay={"fresh": True, "notfound": ["app"], "libs": {}, "missing": {}, "cuda": {}, "errors": [], "module_errors": []})
  check("G01: not from the replay when a uenv option provides the software", "G01" not in got, sorted(got))
  got, f = ids(noset + "echo done\n")
  check("G02: echo after srun without set -e", "G02" in got)
  check("G02: not with set -e", "G02" not in ids(GOOD + "echo done\n")[0])
  j = job(steps=[{"JobID": "123.0", "State": "FAILED", "ExitCode": "1:0", "SubmitLine": "", "JobName": "app"}])
  got, f = ids(GOOD, job=j)
  check("G02: a failed step under a COMPLETED job", any(x.rule.id == "G02" and "step 0" in x.evidence for x in f), sorted(got))
  got, f = ids(GOOD, job=job(), submit_env={"SLURM_JOB_ID": "999"})
  check("G03: submitted from inside another job", "G03" in got)
  got, f = ids(GOOD + "sbatch --dependency=afterok:$SLURM_JOB_ID next.sh\n")
  check("G03: a job that submits the next one passes on its SLURM_* settings (hint)", any(x.rule.id == "G03" and x.severity == "info" for x in f))
  check("G03: not with --export=NONE", "G03" not in ids(GOOD + "sbatch --export=NONE next.sh\n")[0])
  got, f = ids(GOOD.replace("srun --cpu-bind=cores ./app", "mpirun -np 16 ./app"))
  check("G04: mpirun -np that differs from the allocation (warning)", any(x.rule.id == "G04" and x.severity == "warning" for x in f), [(x.rule.id, x.evidence) for x in f if x.rule.id == "G04"])
  got, f = ids(GOOD.replace("srun --cpu-bind=cores ./app", "srun -c 8 ./app"))
  check("G04: srun -c that differs from --cpus-per-task", any(x.rule.id == "G04" and "-c 8" in x.evidence for x in f))
  got, f = ids(GOOD.replace("srun --cpu-bind=cores ./app", "srun -n 8 ./app"))
  check("G04: srun -n equal to the allocation is a hint", any(x.rule.id == "G04" and x.severity == "info" for x in f))
  a = job("201", "PENDING", "/w", "/w/out.txt", True, script="#!/bin/bash\nsrun ./a > result.txt\n")
  b = job("202", "PENDING", "/w", "/w/other.txt", True, script="#!/bin/bash\nsrun ./b > result.txt\n")
  ctx = c.make_context(a.script, "", job=a, facts=facts); ctx.others = [a, b]
  got = {x.rule.id: x for x in c.run_rules(ctx)}
  check("G05: two queued jobs in one directory write the same file", "G05" in got and "result.txt" in got["G05"].evidence)
  check("G06: a hint when nothing is recorded", "G06" in ids(GOOD)[0])
  check("G06: not with module list", "G06" not in ids(GOOD + "module list 2>&1\n")[0])
  got, f = ids(GOOD + 'vim notes.txt\nread -p "continue?" answer\nwhile read l; do echo $l; done < list.txt\ntop -b -n 1\n')
  g09 = [x.evidence for x in f if x.rule.id == "G09"]
  check("G09: vim and read are flagged, while-read and top -b are not", len(g09) == 2 and any("vim" in e for e in g09) and any("read" in e for e in g09), g09)
  nochdir = GOOD.replace("#SBATCH --chdir=/scratch/me/run\n", "")
  got, f = ids(nochdir)
  check("G18: relative logs with a cd in the script, without --chdir (warning)", any(x.rule.id == "G18" and x.severity == "warning" and "--output" in x.evidence for x in f),
        [(x.rule.id, x.evidence) for x in f if x.rule.id == "G18"])
  rel = nochdir.replace('cd "$SCRATCH/run"\n', "").replace("--output=logs/%x-%j.out", "--output=/scratch/me/logs/%x-%j.out")
  got, f = ids(rel)
  check("G18: relative program and input before any cd (hint)", any(x.rule.id == "G18" and "./app" in x.evidence for x in f), [(x.rule.id, x.evidence) for x in f if x.rule.id == "G18"])
  check("G18: not after an absolute cd", "G18" not in ids(rel.replace("srun --cpu", "cd /scratch/me/run || exit 1\nsrun --cpu"))[0])
  j = job(wd="/home/me/proj", submit="sbatch sbatch/job.sh", state="FAILED")
  f, ctx = c.check_script_text(rel, "", facts=facts, job=j)
  ctx.script_dir = "/home/me/proj/sbatch"
  ctx.probes = {"f:/home/me/proj/sbatch/app": PathInfo(True), "f:/home/me/proj/app": PathInfo(False),
                "f:/home/me/proj/sbatch/input.dat": PathInfo(True), "f:/home/me/proj/input.dat": PathInfo(False)}
  g18 = [x for x in c.run_rules(ctx) if x.rule.id == "G18"]
  check("G18: files that exist only next to the script, submitted from elsewhere (warning)", g18 and g18[0].severity == "warning" and "only next to the script" in g18[0].evidence,
        [(x.severity, x.evidence) for x in g18])
  check("G18: the probe asks for both directories", {"f:/home/me/proj/sbatch/app", "f:/home/me/proj/app"} <= {f"{k}:{p}" for k, p in c.interesting_paths(ctx)})
  got, f = ids(GOOD + 'cd "$(dirname "$0")"\n')
  check("G19: dirname $0", "G19" in got)
  check("B06 is retired", "B06" not in c.RULES)

  arr = GOOD.replace("#SBATCH --hint", "#SBATCH --array=1-10\n#SBATCH --hint") + "echo $SLURM_ARRAY_TASK_ID\n"
  def g07(text):
    return [(x.severity, x.evidence, x.snippet) for x in ids(text)[1] if x.rule.id == "G07"]
  r = g07(arr.replace("logs/%x-%j.out", "logs/run.out"))
  check("G07: no %a: the tasks race on one file (warning), the fix adds %A_%a", r and r[0][0] == "warning" and "race" in r[0][1] and "run_%A_%a.out" in r[0][2], r)
  r = g07(arr.replace("logs/%x-%j.out", "logs/run_%a.out"))
  check("G07: %a without %A: the next run overwrites them (warning), the fix inserts %A_", r and r[0][0] == "warning" and "no %A" in r[0][1] and "run_%A_%a.out" in r[0][2], r)
  r = g07(arr)
  check("G07: %j alone is unique: a hint only", r and r[0][0] == "info", r)
  check("G07: nothing with %A and %a", not g07(arr.replace("logs/%x-%j.out", "logs/%x_%A_%a.out")))
  check("G07: --error too", any("--error" in e for _, e, _ in g07(arr.replace("logs/%x-%j.out", "logs/%x_%A_%a.out") .replace("#SBATCH --hint", "#SBATCH --error=err_%a.txt\n#SBATCH --hint"))))
  t = job("555_3", "COMPLETED", stdout="/w/out_%a.txt")
  ctx = c.make_context(None, "", job=t, facts=facts)
  got = [x for x in c.run_rules(ctx) if x.rule.id == "G07"]
  check("G07: a task without a known script, from its log pattern", got and "no %A" in got[0].evidence, [(x.evidence) for x in got])
  bad = arr.replace("logs/%x-%j.out", "logs/run_%a.out")
  new, done = fixes.apply_fixes(bad, ids(bad)[1])
  check("G07: Fixed copy writes %A_%a", "--output=logs/run_%A_%a.out" in new and any("G07" in d for d in done), done)
  check("B04 is retired", "B04" not in c.RULES)

  # --- options after the program are the program's, not srun's
  no_srun_cpt = GOOD.replace("export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK\n", "")
  got, f = ids(no_srun_cpt.replace("srun --cpu-bind=cores ./app input.dat", "srun --cpu-bind=cores ./app -c 8 input.dat"))
  check("N04: the program's own -c (after it) does not count as srun's", "N04" in got, sorted(got))
  check("N04: srun's -c (before the program) counts", "N04" not in ids(no_srun_cpt.replace("srun --cpu-bind=cores ./app", "srun -c $SLURM_CPUS_PER_TASK --cpu-bind=cores ./app -x"))[0])
  got, f = ids(GOOD.replace("srun --cpu-bind=cores ./app input.dat", "srun --cpu-bind=cores ./app -n 3 -c 2 input.dat"))
  check("G04: -n/-c after the program are the program's (no finding)", "G04" not in got, [(x.rule.id, x.evidence) for x in f if x.rule.id == "G04"])
  got, f = ids(GOOD.replace("srun --cpu-bind=cores ./app input.dat", "srun -n 3 ./app -n 100 input.dat"))
  g04 = [x.evidence for x in f if x.rule.id == "G04"]
  check("G04: srun's own -n before the program still counts (3, not the program's 100)", g04 and "-n 3" in g04[0], g04)
  check("launcher_options: srun's options only", c.launcher_options(["srun", "-n", "4", "--cpu-bind=cores", "./sim", "-c", "8"]) == ["-n", "4", "--cpu-bind=cores"])
  check("launcher_options: mpirun -np 16 -oversubscribe ./app -c 3", c.launcher_options(["mpirun", "-np", "16", "-oversubscribe", "./app", "-c", "3"]) == ["-np", "16", "-oversubscribe"])
  check("launcher_options: srun bash -c '…'", c.launcher_options(["srun", "--overlap", "bash", "-c", "echo"]) == ["--overlap"])

  md = c.rules_markdown()
  check("catalogue has every rule heading", all(f"### {r.heading}" in md for r in c.RULES.values()))
