#!/usr/bin/env python3
"""Fake squeue/sacct/scontrol for the TUI tests: 40 jobs (5 running, 3 pending, 32 finished).
The file 'delay' next to this script holds seconds to sleep per call (simulates a slow controller)."""
import os
import sys
import time
from datetime import datetime, timedelta
S = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
  time.sleep(float(open(os.path.join(S, "delay")).read()))
except Exception:
  pass
prog = os.path.basename(sys.argv[0])
now = datetime.now().replace(microsecond=0)
user = os.environ.get("USER") or os.popen("id -un").read().strip()
jobs = []
for i in range(40):
  jid = str(9000 + i)
  state = "RUNNING" if i < 5 else "PENDING" if i < 8 else ("COMPLETED" if i % 3 else "FAILED")
  wd = os.path.join(S, "jobs", jid)
  os.makedirs(wd, exist_ok=True)
  out, err = os.path.join(wd, f"slurm-{jid}.out"), os.path.join(wd, f"slurm-{jid}.err")
  if state != "PENDING" and not os.path.exists(out):
    with open(out, "w") as f:
      f.write("".join(f"step {k}/100 done\n" for k in range(1, 20 + i)))
    with open(err, "w") as f:
      f.write("warning: something\n" if i % 4 == 0 else "")
  start = now - timedelta(hours=1 + i) if state != "PENDING" else now + timedelta(hours=2)
  end = "Unknown" if state in ("RUNNING", "PENDING") else (start + timedelta(minutes=30)).isoformat()
  jobs.append(dict(JobID=jid, JobIDRaw=jid, JobName=f"run{i:02d}", User=user, Account="acct1", Partition="normal",
                   State=state, ExitCode="0:0" if state != "FAILED" else "1:0", NNodes="2", NTasks="8",
                   NodeList=f"nid00{i:04d}" if state != "PENDING" else "None assigned", Submit=(start - timedelta(minutes=5)).isoformat(),
                   Start=start.isoformat(), End=end, Elapsed="00:30:00", Timelimit="01:00:00", WorkDir=wd,
                   StdOut=os.path.join(wd, "slurm-%j.out"), StdErr=os.path.join(wd, "slurm-%j.err"),
                   MaxRSS="", AveRSS="", TRESUsageInMax="", AllocCPUS="256", AllocTRES="" if state == "PENDING" else "billing=256,cpu=256,gres/gpu=8,mem=400G,node=2",
                   SubmitLine=f"sbatch job{i}.sh", Reason="Priority" if state == "PENDING" else "None"))
if os.path.exists(os.path.join(S, "arrays")): # array jobs: 9500 (6 tasks) and 9600 (150 tasks)
  def task(jid, raw, state, wd, start):
    out = os.path.join(wd, f"slurm-{jid}.out")
    if state != "PENDING" and not os.path.exists(out):
      open(out, "w").write("started\n")
    return dict(JobID=jid, JobIDRaw=raw, JobName=jid.split("_")[0] + "-arr", User=user, Account="acct1", Partition="normal",
                State=state, ExitCode=("1:0" if int(jid.split("_")[1]) % 2 else "2:0") if state == "FAILED" else "0:0", NNodes="1", NTasks="1",
                NodeList="nid000001" if state != "PENDING" else "None assigned", Submit=(start - timedelta(minutes=5)).isoformat(),
                Start=start.isoformat(), End="Unknown" if state in ("RUNNING", "PENDING") else (start + timedelta(minutes=20)).isoformat(),
                Elapsed="00:20:00", Timelimit="01:00:00", WorkDir=wd, StdOut=out, StdErr=out, MaxRSS="", AveRSS="",
                TRESUsageInMax="", SubmitLine="sbatch --array=1-6 arr.sh", Reason="Priority" if state == "PENDING" else "None")
  for base, n in ((9500, 6), (9600, 150), (9700, 12)):
    wd = os.path.join(S, "jobs", str(base)); os.makedirs(wd, exist_ok=True)
    for t in range(1, n + 1):
      if base == 9500:
        state = "RUNNING" if t <= 4 else "COMPLETED" if t == 5 else "PENDING"
      elif base == 9700:
        state = "COMPLETED"
      else:
        state = "RUNNING" if t <= 30 else "COMPLETED" if t <= 130 else "FAILED" if t <= 140 else "PENDING"
      jobs.append(task(f"{base}_{t}", str(base * 1000 + t), state, wd, now - timedelta(hours=1, minutes=t)))
args = sys.argv[1:]
with open(os.path.join(S, "slurm_calls"), "a") as f:
  f.write(prog + " " + " ".join(args) + "\n")
def opt(name):
  return args[args.index(name) + 1] if name in args else None
if prog == "squeue":
  fmt = next(a for a in args if a.startswith("--format=")).split("=", 1)[1]
  codes = {"%i": "JobID", "%j": "JobName", "%u": "User", "%a": "Account", "%P": "Partition", "%T": "State", "%r": "Reason",
           "%D": "NNodes", "%M": "Elapsed", "%l": "Timelimit", "%S": "Start", "%V": "Submit", "%Z": "WorkDir", "%N": "NodeList"}
  ids = opt("-j").split(",") if opt("-j") else None
  for j in jobs:
    if j["State"] in ("RUNNING", "PENDING") and (ids is None or j["JobID"] in ids or j["JobID"].split("_")[0] in ids):
      print("|".join(j[codes[c]] for c in fmt.split("|")))
      found = True
  if ids is not None and not globals().get("found"): # as Slurm: no queued job with these ids
    print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
    sys.exit(1)
elif prog == "sacct" and "--env-vars" in args:
  print(f"Environment used for {opt('-j')} (may differ from that in job):\n" + "-" * 50)
  print("PATH=/usr/local/bin:/usr/bin:/bin\nHOME=/home/x\nUENV_MOUNT_LIST=/capstor/uenv/prgenv-gnu.squashfs:/user-environment")
  print("UENV_VIEW=modules\nLOADEDMODULES=cuda/12.4:cray-mpich/8.1:python/3.11\nSLURM_JOB_ID=4242\nMY_SETTING=42")
  print("BASH_FUNC_module%%=() {  eval $($LMOD_CMD bash \"$@\")\n}")
elif prog == "sacct" and "--batch-script" in args:
  print(f"Batch Script for {opt('-j')}\n" + "-" * 50)
  print(f"#!/bin/bash\n#SBATCH -N 2\necho original script of {opt('-j')}")
elif prog == "sacct":
  fields = next(a for a in args if a.startswith("--format=")).split("=", 1)[1].split(",")
  ids = opt("-j").split(",") if opt("-j") else None
  for j in jobs:
    if ids is not None and j["JobID"] not in ids and j["JobID"].split("_")[0] not in ids: # an array id: all its tasks
      continue
    print("|".join(j.get(f, "") for f in fields))
    if j["State"] != "PENDING" and "-X" not in args:
      for step in ("batch", "0"):
        s = dict(j, JobID=f"{j['JobID']}.{step}", JobName="batch" if step == "batch" else "app",
                 SubmitLine="" if step == "batch" else f"srun ./app --input in{j['JobID']}.dat")
        print("|".join(s.get(f, "") for f in fields))
elif prog == "scontrol" and args[:2] != ["show", "reservation"]:
  if args[:1] == ["listpids"]:
    ppid = os.getppid() # the $( ) subshell; its parent is the bash running the probe (lives long enough for ps)
    try:
      ppid = int(open(f"/proc/{ppid}/stat").read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
      pass
    print(f"PID      JOBID    STEPID   LOCALID GLOBALID\n{ppid} {args[1]} 0 0 0\n{os.getpid()} {args[1]} 7 0 0")
  elif args[:2] == ["show", "config"]:
    print("Configuration data as of 2026-11-16T10:00:00\nAccountingStorageType = accounting_storage/slurmdbd\nClusterName             = lab\nSlurmctldPort = 6817")
  elif args[:2] == ["write", "batch_script"]:
    if args[2] == "9005": # a script with problems (the checks)
      print("#!/bin/bash\n#SBATCH -N 2\n#SBATCH --mem=64\n#SBATCH --time=2:00\n#SBATCH --output=nologs/out.txt\n"
            "module load gcc\nsrun ./app --input in9005.dat\n#SBATCH --account=acct1")
    else:
      print(f"#!/bin/bash\n#SBATCH -N 2\nsrun ./app --input in{args[2]}.dat")
  else:
    print("scontrol: fake")
elif prog == "sacctmgr":
  print("acct1\nproj2\nacct1")
elif prog == "scancel":
  with open(os.path.join(S, "scancel_calls"), "a") as f:
    f.write(" ".join(args) + "\n")
elif prog == "sinfo" and "--version" in args:
  print("slurm 23.11.4")
elif prog == "sinfo":
  states = ["allocated"] * 10 + ["mixed"] * 3 + ["idle"] * 4 + ["down*", "drained", "idle$"]
  fmt = args[args.index("-o") + 1]
  if "-N" in args:
    for i, st in enumerate(states):
      print(f"nid{i:04d}|{st}")
      if i < 3: print(f"nid{i:04d}|{st}") # in a second partition
  elif fmt == "%P":
    print("normal*\ndebug")
  elif fmt == "%P|%l|%c|%m|%G|%X|%Y|%Z|%h|%a":
    print("normal*|1-00:00:00|128|256000|gpu:4(S:0-3)|2|64|1|NO|up")
    print("debug|30:00|128|256000|gpu:4(S:0-3)|2|64|1|NO|up")
  elif "-R" in args:
    print("Not responding|down|1|nid0017|2026-11-13T10:00:00")
    print("bad gpu|drained|1|nid0018|2026-11-12T08:00:00")
  else:
    print("normal*|up|13/5/2/20|1-00:00:00|20")
    print("debug|up|1/2/0/3|30:00|3")
elif prog == "scontrol" and args[:2] == ["show", "reservation"]:
  print("ReservationName=maint1 StartTime=2026-11-20T08:00:00 EndTime=2026-11-20T18:00:00 Duration=10:00:00 Nodes=nid[0000-0019] NodeCnt=20 CoreCnt=2560 Features=(null) PartitionName=(null) Flags=MAINT,SPEC_NODES State=INACTIVE")
elif prog == "sbatch" and "--help" in args:
  print("Usage: sbatch [OPTIONS(0)...] [ : [OPTIONS(N)...]] script(0) [args(0)...]\n  -A, --account=name  charge job to specified account\n"
        "  -N, --nodes=N  number of nodes\n  -t, --time=minutes  time limit\n      --mem=MB  minimum amount of real memory\n"
        "      --uenv=name  a site plugin option")
elif prog == "sbatch" and "--version" in args:
  print("slurm 23.02.7")
elif prog == "sbatch" and "--test-only" in args:
  print("sbatch: Job 9101 to start at 2026-11-14T18:00:00 using 256 processors on nodes nid[0001-0002] in partition normal", file=sys.stderr)
elif prog == "sbatch":
  with open(os.path.join(S, "sbatch_calls"), "a") as f:
    script = next((a for a in args if a.endswith(".sh")), None)
    content = open(script).read() if script and os.path.exists(script) else ""
    f.write(repr(args) + " cwd=" + os.getcwd() + f" UENV={os.environ.get('UENV_MOUNT_LIST')} MY={os.environ.get('MY_SETTING')} "
            f"SLURM_JOB_ID={os.environ.get('SLURM_JOB_ID')} FUNC={'BASH_FUNC_module%%' in os.environ} SCRIPT={content!r}\n")
  print("Submitted batch job 9100")
elif prog == "sstat":
  pass
else:
  print(f"fake {prog}", file=sys.stderr)
  sys.exit(1)
