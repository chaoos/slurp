"""The rules of the checks (see engine.py). Each rule documents itself for the rule catalogue."""

import difflib
import math
from ..cluster import slurm as sl
import os
import re
import shlex

from .engine import (
  rule, Finding, ERROR, WARNING, INFO, RULES, parse_time, time_text, parse_mem, mem_text, to_int, known_options, output_pattern, referenced_paths, strip_comment, submit_options, is_placeholder
)

def F(rule_id, evidence, line=None, severity="", fix="", snippet=""):
  return Finding(RULES[rule_id], evidence, line, severity, fix, snippet)

# ===========================================================================
# S: script syntax and hygiene

@rule("S01", ERROR, "Windows line endings or pasted typography",
      "Carriage returns (Windows line endings), curly quotes, non-breaking spaces or long dashes that came in with "
      "copy and paste from a web page, a PDF or a Windows editor.",
      "With Windows line endings the job fails at once (/bin/bash^M: bad interpreter, or $'\\r': command not found). "
      "Curly quotes are not quotes for the shell, a long dash is not an option dash: the commands do something else.",
      "Convert the file: dos2unix job.sh (or sed -i 's/\\r$//' job.sh). Retype quotes and dashes as plain ' \" and -.",
      "#SBATCH –-time=01:00:00     # a long dash: not an option\necho “hello”                 # curly quotes")
def s01(ctx):
  s = ctx.script
  crlf = [i for i, l in enumerate(s.raw_lines, 1) if l.endswith("\r")]
  if crlf:
    yield F("S01", f"{len(crlf)} lines end with a carriage return (Windows line endings)", crlf[0],
            fix="dos2unix " + (os.path.basename(s.path) if s.path else "job.sh"))
  for i, l in enumerate(s.lines, 1):
    if re.search(r"[“”‘’]", l) and not l.lstrip().startswith("#") or l.startswith("#SBATCH") and re.search(r"[“”‘’]", l):
      yield F("S01", "curly quotes (from a web page or a word processor)", i, WARNING, fix="retype them as ' or \"")
    if " " in l:
      yield F("S01", "a non-breaking space (from copy and paste)", i, WARNING, fix="replace it by a normal space")
    if re.search(r"[–—]-?\w|(?<!\w)[–—]\w", l):
      yield F("S01", "a long dash where an option dash belongs (–- instead of --)", i, fix="type the dashes as --")

@rule("S02", ERROR, "Missing or wrong shebang",
      "The first line is not #! with an existing interpreter, or the script is run by sh but uses bash syntax "
      "([[ ]], arrays, source, function).",
      "sbatch refuses a script without #! (\"This does not look like a batch script\"). With #!/bin/sh, bash syntax "
      "fails on systems where sh is not bash. Without a login shell (#!/bin/bash -l) the module command can be missing "
      "on some sites.",
      "Start the script with #!/bin/bash -l (or #!/bin/bash and source the module setup yourself).",
      "#!/bin/bash -l\n#SBATCH --time=01:00:00")
def s02(ctx):
  s = ctx.script
  if not s.shebang:
    yield F("S02", "the first line is not #!… (sbatch: \"This does not look like a batch script\")", 1, snippet="#!/bin/bash -l")
    return
  interp = s.shebang[2:].strip().split()
  if interp and os.path.basename(interp[0]) == "sh":
    for line, t in s.code_lines():
      if re.search(r"\[\[|\bsource\s|\bfunction\s|\w+=\(|\$\{\w+\[", t):
        yield F("S02", "#!/bin/sh but bash syntax ([[ ]], source, arrays or function)", line, WARNING, fix="use #!/bin/bash")
        break
  info = ctx.probe("x", interp[0]) if interp and interp[0].startswith("/") else None
  if info is not None and not info.exists:
    yield F("S02", f"the interpreter {interp[0]} does not exist", 1)
  if interp and os.path.basename(interp[0]) == "bash" and "-l" not in interp[1:] and "--login" not in interp[1:] \
     and s.uses(r"(^|[;\s])(module|ml)\s") and (ctx.facts and ctx.facts.site.get("login_shell")):
    yield F("S02", "module is used, but bash is not a login shell (this site needs #!/bin/bash -l)", 1, INFO,
            snippet="#!/bin/bash -l")

@rule("S03", ERROR, "#SBATCH lines Slurm ignores",
      "#SBATCH lines after the first command, or lines that look like #SBATCH but are not (# SBATCH, #sbatch, #SBTACH).",
      "Slurm reads #SBATCH options only up to the first command and only exactly written: the rest is a comment. "
      "The job silently runs with the defaults (one core, the default time, the default partition).",
      "Put all #SBATCH lines right after the #! line, before any command, and write #SBATCH exactly.",
      "#!/bin/bash\nmodule load gcc\n#SBATCH --time=02:00:00   # ignored: after a command")
def s03(ctx):
  s = ctx.script
  seen = set()
  for d in s.directives:
    if d.ignored and d.line not in seen:
      seen.add(d.line)
      yield F("S03", f"after the first command (line {s.first_command}): Slurm ignores it", d.line,
              fix="move it above line " + str(s.first_command))
  for line, text in s.near_directives:
    yield F("S03", f"looks like #SBATCH but is not ({text.split()[0] if text.split() else text}): a plain comment",
            line, WARNING, fix="write #SBATCH exactly (no space, upper case)")

@rule("S04", WARNING, "Unknown, misspelled or repeated options",
      "#SBATCH options sbatch does not know (--ntask, --cpus-per-tasks), and options given twice with different values.",
      "sbatch rejects unknown options (the job is never submitted). For repeated options the last one wins silently.",
      "Fix the spelling (sbatch --help lists the options); keep one of the repeated lines.",
      "#SBATCH --ntask=4           # --ntasks\n#SBATCH --cpus-per-tasks=8  # --cpus-per-task")
def s04(ctx):
  s = ctx.script
  known = known_options(ctx.facts)
  seen = {}
  for d in s.directives:
    if not d.option:
      continue
    if d.option.startswith("-"):
      yield F("S04", f"{d.written}: no such short option", d.line, ERROR)
      continue
    if d.option not in known and not any(k.startswith(d.option) for k in known if len(d.option) >= 3):
      close = difflib.get_close_matches(d.option, sorted(known), 1, 0.75)
      yield F("S04", f"--{d.option} is not an sbatch option" + (f" (did you mean --{close[0]}?)" if close else ""),
              d.line, ERROR, fix=f"--{close[0]}" if close else "")
    if not d.ignored:
      if d.option in seen and seen[d.option][0] != d.value:
        yield F("S04", f"--{d.option} is given again (line {seen[d.option][1]}: {seen[d.option][0]}, here: {d.value}); "
                       "this one wins", d.line, INFO)
      seen[d.option] = (d.value, d.line)

@rule("S05", ERROR, "Variables or ~ in #SBATCH lines",
      "$VARIABLES or ~ in the values of #SBATCH options.",
      "Slurm does not expand variables or ~ in #SBATCH lines: --output=$SCRATCH/out.txt writes to a directory "
      "literally named $SCRATCH (which does not exist, so the job fails without a log).",
      "Write the full path, or use Slurm's own patterns (%j job id, %x job name, %u user, %A/%a array). For a "
      "directory of the user: --chdir and relative paths, or pass the option on the command line: "
      "sbatch --output=$SCRATCH/out.txt job.sh (there the shell expands it).",
      "#SBATCH --output=$SCRATCH/logs/%j.out   # not expanded\n#SBATCH --output=/scratch/me/logs/%j.out  # fine")
def s05(ctx):
  for d in ctx.script.directives:
    if d.value and re.search(r"\$\{?\w|^~", d.value) and not d.ignored:
      yield F("S05", f"--{d.option}={d.value}: Slurm does not expand $ or ~ here", d.line,
              fix="write the full path, or use %j %x %u %A %a")

KNOWN_VARS = {"HOME", "USER", "PATH", "PWD", "SHELL", "HOSTNAME", "LANG", "TMPDIR", "OLDPWD", "RANDOM", "SECONDS",
              "LD_LIBRARY_PATH", "PYTHONPATH", "OMP_NUM_THREADS", "BASH_SOURCE", "LINENO", "PPID", "UID", "IFS",
              "CUDA_VISIBLE_DEVICES", "SCRATCH", "LOGNAME", "BASH_REMATCH", "FUNCNAME", "PIPESTATUS", "MODULEPATH",
              "LOADEDMODULES", "CONDA_PREFIX", "VIRTUAL_ENV", "REPLY", "OPTARG", "OPTIND", "EPOCHSECONDS"}

@rule("S06", WARNING, "Variables that are never set",
      "Variables the script uses but nobody sets: not the script, not Slurm (SLURM_*), not the login environment. "
      "Usually a typo ($SRATCH) or a variable only your interactive shell has.",
      "An unset variable is empty: cd $SRATCH/run goes to /run, rm -rf $DIR/ removes from /, paths point nowhere.",
      "Fix the name, set it in the script, or stop on unset variables with set -u.",
      "cd $SRATCH/run     # $SCRATCH")
def s06(ctx):
  s = ctx.script
  assigned = set(s.assignments())
  env = set(ctx.facts.env) if ctx.facts else set()
  known = assigned | env | KNOWN_VARS
  reported = set()
  for line, t in s.code_lines():
    t = re.sub(r"'[^']*'", "''", t) # nothing is expanded in single quotes
    for m in re.finditer(r"\$\{?([A-Za-z_]\w*)", t):
      name = m.group(1)
      if name in known or name.startswith(("SLURM_", "SBATCH_", "SRUN_", "PMI_", "OMPI_", "MPI", "BASH")) or name in reported:
        continue
      close = difflib.get_close_matches(name, sorted(known), 1, 0.8)
      if close or (ctx.facts and ctx.facts.env):
        reported.add(name)
        yield F("S06", f"${name} is not set" + (f" (did you mean ${close[0]}?)" if close else
                                                 " by the script or the login environment"), line,
                WARNING if close else INFO)

@rule("S07", WARNING, "cd without a check",
      "cd to a directory without set -e and without || exit.",
      "If the directory is missing, cd fails, the script goes on in the directory it was in (often $HOME) and "
      "writes or deletes there.",
      "cd /path/to/run || exit 1   (or set -e at the top of the script).",
      "cd $SCRATCH/run\nrm -f *.tmp        # runs in $HOME if cd failed")
def s07(ctx):
  s = ctx.script
  if s.uses(r"\bset\s+-[a-zA-Z]*e") or s.uses(r"\bset\s+-o\s+errexit"):
    return
  for line, t in s.code_lines():
    if re.match(r"\s*cd\s+\S", t) and not re.search(r"\|\||&&", t):
      yield F("S07", f"{t.strip()}: if it fails, the script goes on elsewhere", line,
              snippet=t.strip() + " || exit 1")
      return

@rule("S08", ERROR, "Background programs without wait",
      "Programs started in the background (a line ending with &) without a wait before the end of the script; nohup "
      "inside a job.",
      "The job ends when the script ends: Slurm then kills everything it started, also what runs in the background. "
      "The job looks completed but the work was cut off.",
      "Add wait after the background commands (it waits for all of them).",
      "srun -n1 ./a &\nsrun -n1 ./b &\nwait          # without it, the job ends right here")
def s08(ctx):
  s = ctx.script
  bg = [(line, t) for line, t in s.code_lines() if re.search(r"(?<![&>|])&\s*$", t)]
  if bg and not s.uses(r"(^|[;\s])wait(\s|;|$)"):
    yield F("S08", f"{len(bg)} commands run in the background, there is no wait", bg[0][0], snippet="wait")
  for line, t in s.code_lines():
    if re.match(r"\s*nohup\s", t):
      yield F("S08", "nohup does not keep anything alive after the job ends", line, WARNING)

@rule("S09", INFO, "Options overridden at submission",
      "Options in the script that the sbatch command line (or SBATCH_* environment variables) overrode.",
      "The script says one thing, the job ran with another: reading the script misleads, and resubmitting it "
      "without the same command line gives a different job.",
      "Keep the options in one place, or note in the script that they come from the command line.",
      "#SBATCH --time=01:00:00\n$ sbatch --time=12:00:00 job.sh   # the job gets 12 h", needs="job")
def s09(ctx):
  if not ctx.script:
    return
  sub, _ = submit_options(ctx.submit_line)
  script_opts = ctx.script.options()
  for name, value in sub.items():
    if name in script_opts and script_opts[name][0] != value:
      yield F("S09", f"--{name}: {script_opts[name][0]} in the script, {value} on the command line", script_opts[name][1].line)

# ===========================================================================
# R: resources and requests that make no sense

@rule("R01", ERROR, "Memory without a unit",
      "--mem, --mem-per-cpu or --mem-per-gpu without a unit.",
      "Without a unit Slurm counts megabytes: --mem=4 is 4 MB, not 4 GB. The job is killed as soon as it starts "
      "(OUT_OF_MEMORY), or does not start at all.",
      "Always write the unit: --mem=4G, --mem-per-cpu=2000M.",
      "#SBATCH --mem=64      # 64 MB!\n#SBATCH --mem=64G     # 64 GB")
def r01(ctx):
  for name in ("mem", "mem-per-cpu", "mem-per-gpu"):
    v = ctx.opt(name)
    if v is None or is_placeholder(v):
      continue
    parsed = parse_mem(v)
    line = ctx.directive_line(name)
    if parsed is None:
      yield F("R01", f"--{name}={v} is not a memory size", line)
    elif not parsed[1] and parsed[0] != 0:
      sev = ERROR if parsed[0] <= 256 else WARNING if parsed[0] < 4096 else INFO
      yield F("R01", f"--{name}={v} means {mem_text(parsed[0])} (no unit: megabytes)", line, sev,
              fix=f"--{name}={v}G if you meant gigabytes")

@rule("R02", WARNING, "A time limit that means something else",
      "--time values whose Slurm meaning is easy to get wrong: a plain number is minutes, A:B is minutes:seconds.",
      "--time=1:00 is one minute, --time=24 is 24 minutes: the job is killed (TIMEOUT) long before it is done.",
      "Write hours:minutes:seconds or days-hours: --time=01:00:00, --time=1-00 (one day).",
      "#SBATCH --time=2:00     # 2 minutes!\n#SBATCH --time=02:00:00  # 2 hours")
def r02(ctx):
  v = ctx.opt("time")
  if v is None or is_placeholder(v):
    return
  line = ctx.directive_line("time")
  minutes = parse_time(v)
  if minutes is None:
    yield F("R02", f"--time={v} is not a Slurm time", line, ERROR, fix="hours:minutes:seconds, e.g. 02:00:00")
    return
  m = re.fullmatch(r"(\d+):(\d+)", v.strip())
  if m and int(m.group(1)) <= 72:
    yield F("R02", f"--time={v} means {int(m.group(1))} minutes {int(m.group(2))} seconds (minutes:seconds)", line,
            fix=f"--time={int(m.group(1)):02d}:{int(m.group(2)):02d}:00 for hours:minutes")
  elif re.fullmatch(r"\d+", v.strip()) and 1 <= int(v) <= 24:
    yield F("R02", f"--time={v} means {v} minutes", line, INFO, fix=f"--time={int(v):02d}:00:00 for {v} hours")

@rule("R03", ERROR, "More than a node has",
      "Per node more CPUs (tasks per node × CPUs per task), GPUs or memory than the nodes of the partition have.",
      "The job never starts (pending with a reason like Resources or BadConstraints) or sbatch rejects it.",
      "Spread the tasks over more nodes, or ask for less per node. The partition's nodes are listed in Cluster info.",
      "#SBATCH --ntasks-per-node=8\n#SBATCH --cpus-per-task=64   # 512 CPUs per node, the nodes have 288")
def r03(ctx):
  p = ctx.partition()
  if p is None:
    return
  nodes, tpn, cpt, gpn = ctx.counts()
  hint = (ctx.opt("hint") or "").lower()
  cpus = p.cores_per_node if "nomultithread" in hint else p.cpus
  if tpn and p.cpus and tpn * cpt > cpus:
    yield F("R03", f"{tpn} tasks × {cpt} CPUs = {tpn * cpt} CPUs per node, the nodes of {p.name} have {cpus}",
            ctx.directive_line("cpus-per-task", "ntasks-per-node", "ntasks"))
  if gpn and gpn > p.gpus:
    yield F("R03", f"{gpn} GPUs per node, the nodes of {p.name} have {p.gpus}",
            ctx.directive_line("gpus-per-node", "gres", "gpus-per-task", "gpus"))
  mem = parse_mem(ctx.opt("mem")) if ctx.opt("mem") else None
  if mem and p.mem_mb and mem[0] > p.mem_mb:
    yield F("R03", f"--mem={ctx.opt('mem')} per node, the nodes of {p.name} have {mem_text(p.mem_mb)}", ctx.directive_line("mem"))

@rule("R04", WARNING, "Contradictory requests",
      "Options that do not fit together: nodes × tasks per node ≠ tasks, fewer tasks than nodes, --mem together with "
      "--mem-per-cpu, --ntasks-per-node with a different --ntasks.",
      "sbatch rejects some combinations; for others Slurm silently changes the request (and you get other "
      "resources than you think).",
      "Give either --ntasks or --ntasks-per-node with --nodes, and either --mem or --mem-per-cpu.",
      "#SBATCH --nodes=2\n#SBATCH --ntasks-per-node=4\n#SBATCH --ntasks=16      # 2 × 4 = 8")
def r04(ctx):
  nodes = to_int((ctx.opt("nodes") or "").split("-")[0], None)
  ntasks, tpn = to_int(ctx.opt("ntasks"), None), to_int(ctx.opt("ntasks-per-node"), None)
  if nodes and ntasks and tpn and nodes * tpn != ntasks:
    yield F("R04", f"{nodes} nodes × {tpn} tasks per node = {nodes * tpn}, but --ntasks={ntasks}", ctx.directive_line("ntasks"))
  if nodes and ntasks and ntasks < nodes:
    yield F("R04", f"{ntasks} tasks on {nodes} nodes: some nodes get no task (Slurm reduces the nodes)", ctx.directive_line("ntasks"))
  if ctx.opt("mem") is not None and (ctx.opt("mem-per-cpu") is not None or ctx.opt("mem-per-gpu") is not None):
    yield F("R04", "--mem together with --mem-per-cpu/--mem-per-gpu: sbatch rejects that", ctx.directive_line("mem"), ERROR)

@rule("R05", ERROR, "Time limit over the partition's maximum",
      "--time longer than the partition allows.",
      "The job never starts (pending with reason PartitionTimeLimit), or sbatch rejects it.",
      "Ask for at most the maximum (sinfo shows it), or use a partition with longer jobs; split long runs with "
      "checkpoints.",
      "#SBATCH --partition=debug\n#SBATCH --time=04:00:00   # debug allows 30 minutes")
def r05(ctx):
  p = ctx.partition()
  minutes = parse_time(ctx.opt("time")) if ctx.opt("time") else None
  if p and minutes and p.max_minutes and minutes > p.max_minutes:
    yield F("R05", f"--time={ctx.opt('time')} ({time_text(minutes)}), {p.name} allows {time_text(p.max_minutes)}",
            ctx.directive_line("time"), fix=f"--time={time_text(p.max_minutes)}")

@rule("R06", ERROR, "Partition, account or QOS that do not exist or are not yours",
      "A partition the cluster does not have, an account or QOS you have no association with, or no account where "
      "you have several.",
      "sbatch rejects the job (Invalid account or account/partition combination, Invalid partition name). Without "
      "--account, the default account is charged, which may not be the project you meant.",
      "Use one of your accounts (sacctmgr show assoc user=$USER) and partitions (sinfo -s).",
      "#SBATCH --account=myproject\n#SBATCH --partition=normal")
def r06(ctx):
  f = ctx.facts
  if not f:
    return
  part = ctx.opt("partition")
  if part and f.partitions and not is_placeholder(part):
    for name in part.split(","):
      if name not in f.partitions:
        close = difflib.get_close_matches(name, sorted(f.partitions), 1, 0.6)
        yield F("R06", f"there is no partition {name}" + (f" (did you mean {close[0]}?)" if close else
                f"; there are {', '.join(sorted(f.partitions))}"), ctx.directive_line("partition"))
  account = ctx.opt("account")
  if account and f.accounts and account not in f.accounts and not is_placeholder(account):
    yield F("R06", f"you have no association with the account {account}; yours: {', '.join(f.accounts)}", ctx.directive_line("account"))
  elif account is None and len([a for a in f.accounts if a != "root"]) > 1 and (ctx.job is None or ctx.job.queued):
    mine = [a for a in f.accounts if a != "root"]
    yield F("R06", f"no --account, and you have {len(mine)} ({', '.join(mine)}): "
                   + (f"your default, {f.default_account}, is charged" if f.default_account else "the default one is charged"),
            None, INFO, snippet=f"#SBATCH --account={mine[0]}")
  qos = ctx.opt("qos")
  if qos and f.qos and qos not in f.qos and not is_placeholder(qos):
    yield F("R06", f"--qos={qos} is not one of your QOS ({', '.join(sorted(f.qos))})", ctx.directive_line("qos"), WARNING)

@rule("R07", WARNING, "GPUs and the partition do not match",
      "GPUs requested on a partition without GPUs, or a GPU partition without GPUs requested.",
      "On a partition without GPUs the job never starts. On GPU nodes without a GPU request, the job may get none "
      "(the program falls back to the CPU, or fails), or the GPUs sit idle while charged.",
      "Request GPUs on GPU partitions (--gpus-per-node, --gpus-per-task) and none elsewhere.",
      "#SBATCH --partition=normal   # GPU nodes\n#SBATCH --gpus-per-node=4")
def r07(ctx):
  p = ctx.partition()
  if not p:
    return
  _, _, _, gpn = ctx.counts()
  if gpn and not p.gpus:
    yield F("R07", f"{gpn} GPUs per node requested, the partition {p.name} has none", None, ERROR)
  elif p.gpus and not gpn and not (ctx.facts and ctx.facts.site.get("gpus_implicit")):
    yield F("R07", f"the nodes of {p.name} have {p.gpus} GPUs, the job requests none", None, INFO,
            snippet=f"#SBATCH --gpus-per-node={p.gpus}")

@rule("R08", INFO, "Whole nodes for a small job, or options the cluster ignores",
      "--exclusive for a job that uses a small part of a node, or --mem on a cluster that only gives whole nodes.",
      "An exclusive node is charged in full: a 4-core job on a 288-core node wastes almost all of it. On clusters "
      "that only allocate whole nodes, --mem changes nothing.",
      "Leave out --exclusive for small jobs (or pack several small tasks into one job); leave out --mem where nodes "
      "are always whole.",
      "#SBATCH --exclusive\n#SBATCH --ntasks=1   # one core, a whole node charged")
def r08(ctx):
  p = ctx.partition()
  nodes, tpn, cpt, _ = ctx.counts()
  if ctx.opt("exclusive") is not None and p and p.cpus and tpn and tpn * cpt * 4 <= p.cpus:
    yield F("R08", f"--exclusive with {tpn * cpt} of {p.cpus} CPUs per node", ctx.directive_line("exclusive"))
  if ctx.opt("mem") is not None and p and p.exclusive:
    yield F("R08", f"--mem: the nodes of {p.name} are always whole, with all their memory", ctx.directive_line("mem"))

@rule("R09", WARNING, "Thread count that does not match the allocation",
      "OMP_NUM_THREADS (or similar) set to a fixed number that differs from --cpus-per-task.",
      "More threads than cores: they compete and run slower. Fewer: cores you pay for stay idle.",
      "Derive it from the allocation: export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK.",
      "#SBATCH --cpus-per-task=16\nexport OMP_NUM_THREADS=64   # 4 threads per core")
def r09(ctx):
  _, _, cpt, _ = ctx.counts()
  for name, (value, line) in ctx.script.assignments().items():
    if name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS") and value.isdigit() and int(value) != cpt:
      yield F("R09", f"{name}={value}, but --cpus-per-task={cpt}", line, snippet=f"export {name}=$SLURM_CPUS_PER_TASK")

# ===========================================================================
# B: beginner mistakes and classic traps

@rule("B01", ERROR, "Several nodes but no srun",
      "More than one node requested, but nothing in the script starts work on them (no srun, mpirun or similar).",
      "The batch script runs only on the first node. The other nodes are allocated and charged, and do nothing.",
      "Start the parallel part with srun (srun ./program), or request one node.",
      "#SBATCH --nodes=4\n./simulation        # runs on 1 node\nsrun ./simulation   # runs on all 4")
def b01(ctx):
  nodes = to_int((ctx.opt("nodes") or "").split("-")[0], None)
  if nodes and nodes > 1 and not list(ctx.script.launcher_lines()) and \
     not ctx.script.uses(r"\b(torchrun|accelerate|deepspeed|horovodrun|parallel|pdsh|clush|ssh)\b"):
    yield F("B01", f"{nodes} nodes, but no srun/mpirun: only the first node works", ctx.directive_line("nodes"))

@rule("B02", WARNING, "Many copies of a program that is not parallel",
      "srun with several tasks, running a program that is not MPI parallel (not linked to an MPI library).",
      "Every task runs the full program: N identical copies that all compute the same and write the same files.",
      "Run it with one task (srun -n1), use a job array for independent runs, or build the program with MPI.",
      "#SBATCH --ntasks=32\nsrun ./serial_program    # 32 identical runs", needs="replay")
def b02(ctx):
  nodes, tpn, _, _ = ctx.counts()
  ntasks = to_int(ctx.opt("ntasks"), None) or ((nodes or 1) * (tpn or 1))
  if ntasks <= 1:
    return
  for prog, libs in (ctx.replay or {}).get("libs", {}).items():
    if libs is not None and not any(re.search(r"libmpi|libmpich|libfabric|libpmi|libnccl|libgasnet", l) for l in libs):
      line = next((l for l, t, _ in ctx.script.launcher_lines() if prog in t), None)
      if line:
        yield F("B02", f"{ntasks} tasks, {os.path.basename(prog)} is not linked to an MPI library", line)

@rule("B03", WARNING, "Array tasks that all do the same",
      "A job array whose script never uses $SLURM_ARRAY_TASK_ID (or the %a pattern).",
      "Every array task does exactly the same work: N copies of one run instead of N different runs.",
      "Use the task id to pick the input: input=inputs/case_${SLURM_ARRAY_TASK_ID}.dat.",
      "#SBATCH --array=1-100\n./run input.dat                              # 100 times the same\n./run input_${SLURM_ARRAY_TASK_ID}.dat        # 100 cases")
def b03(ctx):
  if ctx.opt("array") is None:
    return
  if not re.search(r"SLURM_ARRAY_TASK_ID|SLURM_ARRAY_TASK|%a", ctx.script.text):
    yield F("B03", f"--array={ctx.opt('array')}, but the script never uses $SLURM_ARRAY_TASK_ID", ctx.directive_line("array"))

@rule("B05", WARNING, "A log name without the job id",
      "--output/--error with a fixed name (no %j or %A).",
      "Every run writes the same file: the log of the last run replaces the earlier ones, and two runs at the same "
      "time write into one file.",
      "Add %j (job id), and %x (job name) if you like: --output=logs/%x_%j.out.",
      "#SBATCH --output=out.txt\n#SBATCH --output=logs/%x_%j.out")
def b05(ctx):
  if ctx.opt("array") is not None:
    return # (G07)
  for name in ("output", "error"):
    v = ctx.opt(name)
    if v and not re.search(r"%[jJA]", v) and v != "/dev/null" and "--open-mode" not in (ctx.script.text if ctx.script else ""):
      yield F("B05", f"--{name}={v}: every run overwrites it", ctx.directive_line(name), INFO if ctx.opt("open-mode") else "")

@rule("B07", WARNING, "Results left on the node's local disk",
      "Results written to /tmp (or $TMPDIR) of the compute node and not copied back.",
      "The node's local disk is wiped when the job ends: whatever is not copied back is lost.",
      "Copy the results back at the end (cp -r $TMPDIR/results $SCRATCH/...), or write to a shared filesystem.",
      "./sim --out /tmp/result.h5\ncp /tmp/result.h5 $SCRATCH/run1/     # without this line it is lost")
def b07(ctx):
  s = ctx.script
  writes = [(line, t) for line, t in s.code_lines() if re.search(r"(>\s*|--?\w*out\w*[=\s]+|-o\s+)(/tmp/|\$TMPDIR|\$\{TMPDIR\})", t)]
  if writes and not s.uses(r"\b(cp|mv|rsync|tar|sbcast)\b.*(/tmp/|\$TMPDIR|\$\{TMPDIR\})"):
    yield F("B07", "output goes to the node's local /tmp, nothing copies it back", writes[0][0])

@rule("B08", INFO, "A clean environment without setup",
      "--export=NONE (the job does not inherit your environment) and the script loads no modules and sources nothing.",
      "Programs, libraries and variables of your login are missing in the job: command not found, missing shared "
      "libraries.",
      "Load what the job needs in the script (module load ..., source env.sh), which is the reproducible way anyway.",
      "#SBATCH --export=NONE\nmodule load gcc openmpi")
def b08(ctx):
  if (ctx.opt("export") or "").upper() == "NONE" and not ctx.script.uses(r"(^|[;\s])(module|ml|source|\.|uenv|spack|conda)\s"):
    yield F("B08", "--export=NONE, and the script sets up no environment", ctx.directive_line("export"))

@rule("B10", INFO, "An allocation that seems unused",
      "A running job that only holds its nodes (sleep infinity, salloc) without running anything for a long time.",
      "The nodes are charged all the time, and blocked for others.",
      "Cancel it when you are done (Cancel job), or give interactive jobs a short time limit.",
      needs="job")
def b10(ctx):
  job = ctx.job
  if not job.running:
    return
  steps = [s for s in job.steps if s["JobID"].partition(".")[2].isdigit()]
  submit = job.rec.get("SubmitLine") or ""
  holds = "sleep infinity" in (job.command or "") or submit.startswith("salloc") or "sleep infinity" in (ctx.script.text if ctx.script else "")
  hours = slurm_hours_of(job.rec.get("Elapsed"))
  if holds and not any(not s["State"].startswith("COMPLETED") for s in steps) and hours >= 1:
    yield F("B10", f"it holds its nodes for {hours:.1f} h and runs no step now")

@rule("B11", WARNING, "The same job twice",
      "Two queued jobs with the same name, the same command line and the same directory.",
      "Usually a submission done twice: twice the node hours, and both write the same files.",
      "Cancel one of them.", needs="list")
def b11(ctx):
  job = ctx.job
  if job is None or not job.queued:
    return
  key = (job.rec.get("JobName"), job.rec.get("SubmitLine"), job.rec.get("WorkDir"))
  twins = [o for o in ctx.others if o is not job and o.queued and "_" not in o.jid and "_" not in job.jid and
           (o.rec.get("JobName"), o.rec.get("SubmitLine"), o.rec.get("WorkDir")) == key]
  if twins and key[1]:
    yield F("B11", f"job {twins[0].jid} is the same ({key[0]}, {key[1]}, in {key[2]})")

@rule("B12", ERROR, "Waiting for a dependency that can never be met",
      "A pending job whose dependency failed (afterok on a job that failed or was cancelled).",
      "The job waits forever (reason DependencyNeverSatisfied) and blocks your queue slots.",
      "Cancel it and submit it again once the job it depends on succeeded, or use afterany. "
      "--kill-on-invalid-dep=yes cancels such jobs automatically.",
      needs="job")
def b12(ctx):
  if ctx.job.queued and "DependencyNeverSatisfied" in (ctx.job.reason or ""):
    yield F("B12", "pending with DependencyNeverSatisfied: it will never start")

@rule("B13", WARNING, "An email for every array task",
      "--mail-type with ARRAY_TASKS on a large array.",
      "Every task sends its own mails: thousands of messages, mail servers may block you.",
      "Leave out ARRAY_TASKS: then the mails are for the array as a whole.",
      "#SBATCH --array=1-1000\n#SBATCH --mail-type=END,ARRAY_TASKS   # 1000 mails")
def b13(ctx):
  mail = (ctx.opt("mail-type") or "").upper()
  arr = ctx.opt("array")
  if arr and "ARRAY_TASKS" in mail:
    from ..cluster.jobs import expand_array_spec
    try:
      n = len(expand_array_spec(arr.split("%")[0]))
    except Exception:
      n = 0
    if n > 20:
      yield F("B13", f"--mail-type={mail} on {n} tasks: {n} mails or more", ctx.directive_line("mail-type"))

@rule("B14", WARNING, "srun in a loop",
      "srun (job steps) started in a loop, one after the other, many times.",
      "Every srun is a job step the controller has to handle: thousands of tiny steps slow down the controller for "
      "everybody and are slow themselves (each takes seconds to start).",
      "Use a job array, or one srun that runs many tasks (srun --multi-prog, a task farm, GNU parallel inside one step).",
      "for f in inputs/*; do\n  srun -n1 ./process $f     # a step per file\ndone")
def b14(ctx):
  for c in ctx.script.commands:
    if c.depth > 0 and re.search(r"(^|[\s;&|(])srun\s", strip_comment(c.text)):
      yield F("B14", "srun inside a loop: one job step per iteration", c.line)
      return

@rule("B15", INFO, "Setup in .bashrc",
      "module load, conda activate or uenv in ~/.bashrc or ~/.bash_profile.",
      "Every job (and every shell) silently gets this environment: jobs depend on your login files, change when you "
      "edit them, and conflict with what the script loads.",
      "Keep ~/.bashrc minimal and load what a job needs in its script.",
      needs="facts")
def b15(ctx):
  if ctx.facts and ctx.facts.bashrc and ctx.script is not None:
    yield F("B15", "your login files set up software: " + "; ".join(l.strip() for l in ctx.facts.bashrc[:2]))

# ===========================================================================
# P: paths, files and programs (the probe)

@rule("P01", ERROR, "The log directory does not exist",
      "The directory of --output or --error does not exist (or is not writable).",
      "Slurm cannot open the log: the job fails at once and leaves no log at all, so nothing tells you why.",
      "Create the directory before submitting: mkdir -p logs.",
      "#SBATCH --output=logs/%j.out    # logs/ must exist\n$ mkdir -p logs && sbatch job.sh", needs="probe")
def p01(ctx):
  if ctx.job is not None and not ctx.job.queued:
    return
  for name in ("output", "error"):
    v = ctx.opt(name)
    if not v or v == "/dev/null":
      continue
    p = ctx.abspath(output_pattern(v, ctx))
    if not p:
      continue
    d = os.path.dirname(p)
    info = ctx.probe("d", d)
    guessed = ctx.job is None and not v.startswith("/") and not ctx.opt("chdir")
    if info is not None and not info.exists:
      yield F("P01", f"--{name}: the directory {d} does not exist" + (" (relative: checked from the script's directory, "
              "Slurm uses the directory you submit from)" if guessed else ""), ctx.directive_line(name), WARNING if guessed else "",
              fix=f"create it before submitting: mkdir -p {shlex.quote(d)}", snippet=f"mkdir -p {shlex.quote(d)}")
    elif info is not None and info.exists and not info.writable:
      yield F("P01", f"--{name}: the directory {d} is not writable for you", ctx.directive_line(name))

@rule("P02", ERROR, "Directories and files that do not exist",
      "The --chdir directory, cd targets, sourced files, Python scripts and redirected inputs that do not exist.",
      "The job fails at that line, or (cd without a check) goes on in the wrong directory.",
      "Fix the path; create directories before use (mkdir -p dir before cd dir).",
      "cd /scratch/me/rnu1     # typo: run1\nsource ~/envs/sim.sh", needs="probe")
def p02(ctx):
  chdir = ctx.opt("chdir")
  if chdir:
    p = ctx.workdir() if ctx.expand(chdir) is not None else None
    info = ctx.probe("d", p)
    if info is not None and not info.exists:
      yield F("P02", f"--chdir={chdir}: {p} does not exist", ctx.directive_line("chdir"))
  for kind, path, line in referenced_paths(ctx):
    if kind in ("d", "f"):
      info = ctx.probe(kind, path)
      if info is not None and not info.exists:
        what = "directory" if kind == "d" else "file"
        guessed = path in ctx.uncertain
        yield F("P02", f"the {what} {path} does not exist" + (" (relative: checked from the script's directory)" if guessed else ""),
                line, WARNING if kind == "f" or guessed else ERROR)

@rule("P03", ERROR, "Programs that cannot run",
      "Programs run by path (./sim, /path/bin/app, via srun) that do not exist or are not executable.",
      "The job fails right away: No such file or directory, Permission denied.",
      "Build the program first, fix the path, or make it executable (chmod +x).",
      "srun ./build/sim     # not built yet", needs="probe")
def p03(ctx):
  for kind, path, line in referenced_paths(ctx):
    if kind != "x":
      continue
    info = ctx.probe("x", path)
    if info is None:
      continue
    if not info.exists:
      guessed = path in ctx.uncertain
      yield F("P03", f"{path} does not exist" + (" (relative: checked from the script's directory; Slurm runs it from the "
                     "directory you submit from)" if guessed else ""), line, WARNING if guessed else "")
    elif info.isdir:
      yield F("P03", f"{path} is a directory", line)
    elif not info.executable:
      yield F("P03", f"{path} is not executable", line, snippet=f"chmod +x {shlex.quote(path)}")

ARCH_WORDS = {"aarch64": ("aarch64", "ARM aarch64", "ARM"), "x86_64": ("x86-64", "x86_64")}

@rule("P04", ERROR, "Built for another CPU",
      "A program built for another CPU architecture than the cluster's (an x86-64 binary on ARM nodes, or the other way).",
      "It cannot run at all: Exec format error / cannot execute binary file.",
      "Build it on the cluster (with the cluster's compilers), or use the build for this architecture.",
      "$ file ./sim\n./sim: ELF 64-bit LSB executable, x86-64 ...   # the nodes are aarch64", needs="probe")
def p04(ctx):
  arch = ctx.facts.arch if ctx.facts else ""
  if arch not in ARCH_WORDS:
    return
  for kind, path, line in referenced_paths(ctx):
    info = ctx.probe("x", path) if kind == "x" else None
    if info and info.exists and info.file and "ELF" in info.file:
      if not any(w in info.file for w in ARCH_WORDS[arch]):
        other = next((a for a, words in ARCH_WORDS.items() if a != arch and any(w in info.file for w in words)), "another CPU")
        yield F("P04", f"{os.path.basename(path)} is built for {other}, the cluster is {arch}", line)

@rule("P05", ERROR, "Shared libraries not found",
      "ldd finds that the program needs shared libraries that are not found in the job's environment (after the "
      "script's module loads and exports).",
      "The job fails at once: error while loading shared libraries: libxyz.so: cannot open shared object file.",
      "Load the modules the program was built with (the same compiler and MPI), or set LD_LIBRARY_PATH in the script.",
      "module load gcc/12 openmpi/4.1    # what the program was built with\nsrun ./sim", needs="replay")
def p05(ctx):
  for prog, libs in (ctx.replay or {}).get("missing", {}).items():
    if libs:
      line = next((l for k, p, l in referenced_paths(ctx) if p == prog), None)
      yield F("P05", f"{os.path.basename(prog)}: not found: {', '.join(libs[:4])}" + (" …" if len(libs) > 4 else ""), line)
  for err in (ctx.replay or {}).get("errors", [])[:3]:
    yield F("P05", f"setting up the environment: {err}", None, WARNING)

@rule("P06", WARNING, "GPU code not built for the node's GPUs",
      "A CUDA program without code for the GPUs of the cluster (e.g. no sm_90 for H100/GH200).",
      "Without native code the driver compiles PTX at start (slow), or the program fails: no kernel image is "
      "available for execution on the device.",
      "Build for the GPU's architecture: nvcc -arch=sm_90 (or CMAKE_CUDA_ARCHITECTURES=90).",
      needs="replay")
def p06(ctx):
  want = (ctx.facts.site.get("cuda_arch") if ctx.facts else None)
  for prog, archs in (ctx.replay or {}).get("cuda", {}).items():
    if want and archs and want not in archs:
      yield F("P06", f"{os.path.basename(prog)} has code for {', '.join(sorted(archs))}, the GPUs need {want}")

@rule("P07", WARNING, "Python environment problems",
      "The Python interpreter or the virtual environment the script uses does not exist, or the environment lives "
      "on a parallel filesystem.",
      "A missing environment fails the job at once. A conda or venv environment on Lustre makes every job start "
      "slowly (thousands of small files) and loads the filesystem for everybody.",
      "Fix the path; put environments in a container or squashfs image, or use the site's software stack.",
      "source $SCRATCH/venv/bin/activate     # 30000 small files on Lustre", needs="probe")
def p07(ctx):
  lustre = (ctx.facts.site.get("parallel_fs") if ctx.facts else None) or []
  for line, t in ctx.script.code_lines():
    m = re.search(r"(?:source|\.)\s+(\S+)/bin/activate|conda\s+activate\s+(\S+/\S+)", t)
    if not m:
      continue
    env = ctx.abspath(m.group(1) or m.group(2))
    if not env:
      continue
    info = ctx.probe("d", env)
    if info is not None and not info.exists:
      yield F("P07", f"the environment {env} does not exist", line, ERROR)
    elif any(env.startswith(p.rstrip("/") + "/") for p in lustre):
      yield F("P07", f"the environment {env} is on a parallel filesystem (many small files)", line, INFO)

@rule("P08", INFO, "Changed since submission",
      "The script file on disk differs from the script Slurm stored at submission, or the program was rebuilt "
      "after the job was submitted.",
      "Slurm runs the stored copy of the script, not the file: edits after sbatch have no effect on this job. "
      "A program rebuilt while the job waits is the new one when it starts (which may or may not be what you want).",
      "Resubmit to run the edited script; keep a copy of the program per run if it must not change.",
      needs="job")
def p08(ctx):
  if ctx.on_disk is not None and ctx.submitted and ctx.job is not None and ctx.job.queued:
    if ctx.on_disk.strip() != ctx.submitted.strip():
      yield F("P08", "the script file was changed after submission: this job runs the old version")
  job = ctx.job
  if job is None or not job.queued or ctx.probes is None or not ctx.script:
    return
  from datetime import datetime
  try:
    submitted = datetime.fromisoformat(job.rec.get("Submit")).timestamp()
  except (TypeError, ValueError):
    return
  for kind, path, line in referenced_paths(ctx):
    info = ctx.probe(kind, path) if kind == "x" else None
    if info and info.exists and info.mtime > submitted + 60:
      yield F("P08", f"{os.path.basename(path)} was changed after submission (rebuilt?)", line)

# ===========================================================================
# Q: quota and storage

def quota_for(ctx, path):
  """The quota entries whose path contains path (the most specific first)"""
  if not ctx.quota or not path:
    return []
  rows = [e for e in ctx.quota.get("entries", []) if e.get("path") and (path == e["path"] or path.startswith(e["path"].rstrip("/") + "/"))]
  rows.sort(key=lambda e: -len(e["path"]))
  return rows

def quota_state(e):
  """(fraction of the limit used, what) for the fuller of space and files, None without limits"""
  out = []
  for used, soft, hard, what in ((e["kb"], e["kb_soft"], e["kb_hard"], "space"), (e["files"], e["files_soft"], e["files_hard"], "files")):
    limit = soft or hard
    if limit:
      out.append((used / limit, what, used, limit))
  return max(out) if out else None

@rule("Q01", ERROR, "Writing where the quota is (almost) full",
      "The job writes (its logs, its working directory) to a filesystem where your quota of space or of files is "
      "full or nearly full.",
      "When the quota is reached, writes fail: Disk quota exceeded, logs that stop in the middle, corrupt result "
      "files; the job may run on and waste its time. The file count (inodes) runs out as easily as the space.",
      "Clean up (Old scratch files lists candidates), move results to permanent storage, pack many small files "
      "into archives, or write elsewhere.",
      "$ lfs quota -u $USER /scratch     # files: 999500 of 1000000", needs="quota")
def q01(ctx):
  if ctx.job is not None and not ctx.job.queued and not ctx.job.running:
    return
  targets = []
  for name in ("output", "error"):
    v = ctx.opt(name) if ctx.script else None
    p = ctx.abspath(output_pattern(v, ctx)) if v else None
    if p:
      targets.append((p, ctx.directive_line(name)))
  if ctx.job is not None:
    for p in (ctx.job.stdout_path(), ctx.job.stderr_path()):
      if p:
        targets.append((p, None))
  wd = ctx.workdir()
  if wd:
    targets.append((wd, None))
  seen = set()
  for path, line in targets:
    for e in quota_for(ctx, path)[:1]:
      key = (e["kind"], e["name"], e["filesystem"])
      st = quota_state(e)
      if key in seen or not st:
        continue
      seen.add(key)
      frac, what, used, limit = st
      if frac >= 0.9 or e.get("over"):
        who = "your" if e["kind"] == "user" else f"the group {e['name']}'s"
        yield F("Q01", f"{e['path']}: {who} {what} quota is {frac:.0%} full ({used} of {limit}"
                       f"{' kB' if what == 'space' else ' files'})", line, ERROR if frac >= 0.98 or e.get("over") else WARNING)

@rule("Q02", WARNING, "Little room left for the output",
      "The filesystem the job writes to has little of your quota left in absolute terms (less than 1 GB, or less "
      "than 1000 files), although it is not yet 90% full.",
      "A job that writes more than what is left fails in the middle (Disk quota exceeded), possibly after hours.",
      "Free space before submitting, or write to a filesystem with room.", needs="quota")
def q02(ctx):
  if ctx.job is not None and not ctx.job.queued:
    return
  wd = ctx.workdir()
  for e in quota_for(ctx, wd)[:1]:
    st = quota_state(e)
    if st and st[0] >= 0.9:
      return # (Q01)
    for used, soft, hard, what, small in ((e["kb"], e["kb_soft"], e["kb_hard"], "space", 1024 * 1024),
                                          (e["files"], e["files_soft"], e["files_hard"], "files", 1000)):
      limit = soft or hard
      if limit and limit - used < small:
        left = f"{(limit - used) / 1024:.0f} MB" if what == "space" else f"{limit - used} files"
        yield F("Q02", f"{e['path']}: only {left} of your quota left")

@rule("Q03", INFO, "Heavy output in $HOME",
      "Logs or the working directory in your home directory although there is a scratch filesystem.",
      "Home quotas are small and home filesystems slow for job output: the quota fills up (and then even logging "
      "in fails), and jobs wait on I/O.",
      "Run in $SCRATCH (cd $SCRATCH/run, --chdir) and keep only sources and small results in $HOME.",
      "#SBATCH --chdir=/scratch/me/run1")
def q03(ctx):
  if not ctx.facts:
    return
  home, scratch = ctx.facts.env.get("HOME"), ctx.facts.env.get("SCRATCH")
  if not home or not scratch or scratch.startswith(home):
    return
  if ctx.job is None and not ctx.opt("chdir"):
    return # (a script alone: where it runs is decided when it is submitted)
  wd = ctx.workdir()
  if wd and (wd == home or wd.startswith(home.rstrip("/") + "/")) and ctx.script and \
     not ctx.script.uses(r"\bcd\s+[\"']?(\$SCRATCH|\$\{SCRATCH\}|" + re.escape(scratch) + ")"):
    yield F("Q03", f"the job runs in {wd} (home); scratch is {scratch}", ctx.directive_line("chdir"))

@rule("Q04", WARNING, "Inputs close to the scratch purge",
      "Inputs on scratch that were not used for a long time, on clusters that delete old scratch files.",
      "The purge may delete them while the job is still waiting: the job then fails with missing files.",
      "Touch or copy the inputs before the job starts (Old scratch files → keep), or keep them on permanent storage.",
      needs="probe")
def q04(ctx):
  days = (ctx.facts.site.get("scratch_purge_days") if ctx.facts else None)
  scratch = ctx.facts.env.get("SCRATCH") if ctx.facts else None
  if not days or not scratch or (ctx.job is not None and not ctx.job.queued):
    return
  import time as _time
  limit = _time.time() - (days - 7) * 86400
  for kind, path, line in referenced_paths(ctx):
    info = ctx.probe(kind, path)
    if info and info.exists and path.startswith(scratch) and info.mtime and info.mtime < limit:
      yield F("Q04", f"{path} was last changed {int((_time.time() - info.mtime) / 86400)} days ago; scratch deletes "
                     f"files after {days} days", line)

@rule("Q05", INFO, "Results on scratch",
      "A finished job that wrote its results to scratch, on a cluster that deletes old scratch files.",
      "Scratch is not backed up and old files are deleted: results left there are lost after the purge period.",
      "Copy what you keep to permanent storage (project, store) soon after the job.",
      needs="job")
def q05(ctx):
  days = (ctx.facts.site.get("scratch_purge_days") if ctx.facts else None)
  scratch = ctx.facts.env.get("SCRATCH") if ctx.facts else None
  job = ctx.job
  if days and scratch and job.rec.get("State", "").startswith("COMPLETED") and (job.rec.get("WorkDir") or "").startswith(scratch):
    yield F("Q05", f"the results are in {job.rec['WorkDir']} (scratch, deleted after {days} days without use)")

@rule("Q06", WARNING, "Very many files in one directory",
      "The job's working directory holds tens of thousands of files.",
      "Parallel filesystems handle many small files badly: listing, opening and creating them is slow for the job "
      "and for everybody (the metadata server), and the file quota runs out.",
      "Write fewer, bigger files (one HDF5 file, tar archives of small outputs), or spread them over subdirectories; "
      "delete what is no longer needed.", needs="probe")
def q06(ctx):
  wd = ctx.workdir()
  info = ctx.probe("n", wd)
  if info and info.exists and info.file.strip().isdigit() and int(info.file) >= 50000:
    n = int(info.file)
    yield F("Q06", f"{wd} holds {'more than 100000' if n > 100000 else n} entries")

@rule("B09", WARNING, "Heavy work on the login node",
      "Your processes on the login node that keep CPUs busy for a long time.",
      "Login nodes are shared by everybody: heavy work there slows down everybody's editing, compiling and "
      "submitting, and is often killed by the site.",
      "Run it as a job: an interactive job (i in the job list) for interactive work, sbatch for the rest.",
      needs="login")
def b09(ctx):
  for pid, cpu, minutes, comm in (ctx.login or []):
    yield F("B09", f"{comm} (pid {pid}) uses {cpu:.0f}% CPU for {minutes} minutes on the login node")

# ===========================================================================
# E: environment and software stack

@rule("E01", INFO, "Modules without versions, or without a clean start",
      "module load without a version, or module loads without module purge before them.",
      "Defaults change when the site updates its software: the same script then builds and runs with other "
      "versions, results change, programs fail to find their libraries. Without a purge, whatever your login loaded "
      "comes along.",
      "Start with module purge (or the site's equivalent) and load exact versions: module load gcc/12.3.0.",
      "module purge\nmodule load gcc/12.3.0 openmpi/4.1.6")
def e01(ctx):
  s = ctx.script
  loads = [(line, t) for line, t in s.code_lines() if re.match(r"\s*(module\s+(load|add)|ml)\s+\w", t)]
  if not loads:
    return
  unversioned = []
  for line, t in loads:
    words = t.split()[2:] if t.split()[0] == "module" else t.split()[1:]
    names = [w for w in words if not w.startswith("-")]
    bad = [n for n in names if "/" not in n and "@" not in n]
    if bad:
      unversioned.append((line, bad))
  if unversioned:
    line, bad = unversioned[0]
    yield F("E01", f"module load {' '.join(bad)} without a version", line)
  if not s.uses(r"\bmodule\s+(purge|reset)|\bml\s+(purge|reset)") and not (ctx.facts and ctx.facts.site.get("no_purge")):
    yield F("E01", "no module purge before the module loads", loads[0][0], snippet="module purge")

@rule("E02", WARNING, "conda and pip inside the job",
      "conda activate without the shell hook, or pip/conda install while the job runs.",
      "conda activate fails in batch scripts without the hook (CondaError: Run 'conda init'). Installing in every "
      "job is slow, needs the network, and changes the environment of running jobs.",
      "Install once, before submitting. In the script: eval \"$(conda shell.bash hook)\" before conda activate, or "
      "source the environment's activate script.",
      "eval \"$(conda shell.bash hook)\"\nconda activate myenv")
def e02(ctx):
  s = ctx.script
  act = s.first_line(r"\bconda\s+activate\b")
  if act and not s.uses(r"conda\s+shell\.\w+\s+hook|conda\.sh|conda\s+init") and not (ctx.facts and any("conda" in l for l in ctx.facts.bashrc)):
    yield F("E02", "conda activate without the shell hook", act, snippet='eval "$(conda shell.bash hook)"')
  inst = s.first_line(r"\b(pip3?|conda|mamba)\s+install\b")
  if inst:
    yield F("E02", "packages are installed while the job runs", inst)

@rule("E03", INFO, "Site advice",
      "Recommendations of the site's rule pack: the software stack, launchers, filesystems.",
      "Sites have their own ways that work best (and some that do not work at all).",
      "Follow the advice, or mute the rule.")
def e03(ctx):
  for advice in (ctx.facts.site.get("advice", []) if ctx.facts else []):
    pattern = advice.get("pattern")
    line = ctx.script.first_line(pattern) if pattern else None
    if pattern and line is None:
      continue
    yield F("E03", advice.get("text", ""), line, advice.get("severity", INFO))

@rule("E04", WARNING, "Modules that cannot be loaded",
      "module load commands of the script that fail now (the module does not exist (any more) or conflicts).",
      "The job starts without the software: command not found, missing libraries.",
      "Use modules that exist (module avail / module spider), with versions.", needs="replay")
def e04(ctx):
  for err in (ctx.replay or {}).get("module_errors", [])[:3]:
    yield F("E04", err)

# ===========================================================================
# N: NUMA placement and binding

@rule("N01", INFO, "A task's CPUs do not match the NUMA domains",
      "--cpus-per-task that is not a whole NUMA domain (or a divisor of one), so tasks straddle NUMA domains.",
      "Memory of a task straddling domains is partly remote: slower memory access, uneven performance between tasks.",
      "Choose --cpus-per-task as a NUMA domain (or a part of one) and bind: --cpu-bind=cores. "
      "See the NUMA guide (F1).",
      "# a node: 4 NUMA domains of 72 cores\n#SBATCH --ntasks-per-node=4\n#SBATCH --cpus-per-task=72")
def n01(ctx):
  p = ctx.partition()
  domains = (ctx.facts.site.get("numa_domains") if ctx.facts else None) or (p.sockets if p else 0)
  if not p or not domains or not p.cores_per_node:
    return
  per_domain = p.cores_per_node // domains
  _, tpn, cpt, _ = ctx.counts()
  if cpt > 1 and per_domain and not (cpt % per_domain == 0 or per_domain % cpt == 0):
    yield F("N01", f"--cpus-per-task={cpt}, a NUMA domain has {per_domain} cores", ctx.directive_line("cpus-per-task"))

@rule("N02", INFO, "Threads and tasks not bound",
      "Threaded programs (OMP_NUM_THREADS > 1 or --cpus-per-task > 1) without OMP_PLACES/OMP_PROC_BIND, or "
      "launchers without binding (--cpu-bind, --hint=nomultithread).",
      "Unbound threads wander between cores and NUMA domains: caches are cold, memory is remote, timings vary.",
      "export OMP_PLACES=cores OMP_PROC_BIND=close, and srun --cpu-bind=cores (with --hint=nomultithread to use "
      "one thread per core).",
      "export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK\nexport OMP_PLACES=cores\nexport OMP_PROC_BIND=close\nsrun --cpu-bind=cores ./app")
def n02(ctx):
  s = ctx.script
  _, _, cpt, _ = ctx.counts()
  threads = "OMP_NUM_THREADS" in s.assignments()
  if (cpt > 1 or threads) and not ("OMP_PLACES" in s.assignments() or "OMP_PROC_BIND" in s.assignments()):
    yield F("N02", "threads without OMP_PLACES / OMP_PROC_BIND", s.first_line(r"OMP_NUM_THREADS") or ctx.directive_line("cpus-per-task"),
            snippet="export OMP_PLACES=cores\nexport OMP_PROC_BIND=close")

@rule("N03", WARNING, "GPUs not assigned to tasks",
      "Several tasks per node with GPUs, but no per-task GPU binding (--gpus-per-task, --gpu-bind, or a wrapper "
      "that sets CUDA_VISIBLE_DEVICES from SLURM_LOCALID).",
      "All tasks see all GPUs and usually all use GPU 0: one GPU is overloaded, the others idle; or GPUs far from "
      "the task's NUMA domain are used.",
      "Give each task its GPU: #SBATCH --gpus-per-task=1 (and --gpu-bind=closest where supported), or a wrapper "
      "with export CUDA_VISIBLE_DEVICES=$SLURM_LOCALID.",
      "#SBATCH --ntasks-per-node=4\n#SBATCH --gpus-per-task=1")
def n03(ctx):
  _, tpn, _, gpn = ctx.counts()
  if gpn and tpn and tpn > 1 and ctx.opt("gpus-per-task") is None and ctx.opt("gpu-bind") is None and \
     not re.search(r"CUDA_VISIBLE_DEVICES|ROCR_VISIBLE_DEVICES|SLURM_LOCALID|gpu-bind", ctx.script.text):
    yield F("N03", f"{tpn} tasks per node share {gpn} GPUs without a binding", None, snippet="#SBATCH --gpus-per-task=1")

@rule("N04", WARNING, "srun does not get --cpus-per-task",
      "--cpus-per-task in #SBATCH but not given to srun, on Slurm 22.05 or newer.",
      "Since Slurm 22.05, srun does not inherit --cpus-per-task from sbatch: every task gets one CPU, all its threads "
      "share that core. The program runs many times slower, without any error.",
      "export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK before srun, or srun --cpus-per-task=$SLURM_CPUS_PER_TASK.",
      "#SBATCH --cpus-per-task=16\nexport SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK\nsrun ./app")
def n04(ctx):
  v = ctx.facts.slurm_version if ctx.facts else ()
  _, _, cpt, _ = ctx.counts()
  if cpt <= 1 or (v and v < (22, 5)):
    return
  s = ctx.script
  if "SRUN_CPUS_PER_TASK" in s.assignments() or re.search(r"SRUN_CPUS_PER_TASK", s.text):
    return
  for line, t, launcher in s.launcher_lines():
    try:
      words = shlex.split(t)
    except ValueError:
      words = t.split()
    options = " ".join(launcher_options(words)) # (only srun's own options: the program's -c is not srun's)
    if launcher == "srun" and not re.search(r"(?:^|\s)(-c\s*\S|-c$|--cpus-per-task)", options):
      yield F("N04", f"--cpus-per-task={cpt}, but this srun gets 1 CPU per task", line,
              snippet="export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK")
      return

@rule("N05", WARNING, "Placement problems of a running job",
      "Seen on the nodes of a running job: tasks sharing cores, threads spread over NUMA domains, more threads "
      "than cores, a task using a GPU of another NUMA domain.",
      "The job runs slower than it could; shared cores halve the speed of both tasks.",
      "Bind tasks and threads (see N02, N03) and check again (Placement in the job window).", needs="live")
def n05(ctx):
  for problem in (ctx.live or {}).get("problems", []):
    yield F("N05", problem)

# ===========================================================================
# U: usage and outcome

def slurm_hours_of(value):
  try:
    return sl.hours(value) if value else 0
  except (ValueError, TypeError, AttributeError):
    return 0

@rule("U01", INFO, "Much more time or memory than used",
      "A finished job that used much less time or memory than it asked for.",
      "Long time limits wait longer in the queue (the scheduler cannot fit them into gaps: backfill). Memory asked "
      "for and not used blocks the node for others (and is charged on some clusters).",
      "Ask for about 1.5 times what the runs need, from the values here.", needs="job")
def u01(ctx):
  job = ctx.job
  if not job.rec.get("State", "").startswith("COMPLETED"):
    return
  elapsed = slurm_hours_of(job.rec.get("Elapsed")) * 60
  limit = parse_time(job.rec.get("Timelimit"))
  if limit and limit != math.inf and limit >= 60 and elapsed and elapsed < 0.25 * limit:
    want = max(15, math.ceil(elapsed * 1.5 / 15) * 15)
    yield F("U01", f"ran {time_text(elapsed)} of {time_text(limit)}", None, fix=f"--time={time_text(want)}",
            snippet=f"#SBATCH --time={time_text(want)}")
  req = job.rec.get("ReqMem") or ""
  m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT])?(n|c)?", req)
  if m:
    from ..cluster.details import peak_job_memory
    peak, _ = peak_job_memory(job)
    reqmb = float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1048576}.get(m.group(2) or "M", 1)
    if m.group(3) == "c":
      reqmb *= to_int(job.rec.get("AllocCPUS"), 1) or 1
    peakmb = peak / 1048576 if peak else 0
    if peakmb and reqmb >= 4096 and peakmb * 4 < reqmb:
      want = math.ceil(peakmb * 1.5 / 1024)
      yield F("U01", f"used {mem_text(peakmb)} of {mem_text(reqmb)} memory", None, fix=f"--mem={want}G")

@rule("U02", INFO, "Low CPU usage",
      "A finished job whose CPUs were busy for a small part of the time (CPU time ÷ (CPUs × elapsed time)).",
      "The cores were allocated and charged but idle: threads not set (OMP_NUM_THREADS), waiting for I/O, or more "
      "cores requested than the program uses.",
      "Check the thread settings (N02, N04), ask for fewer CPUs, or overlap I/O and computing.", needs="job")
def u02(ctx):
  job = ctx.job
  if not job.rec.get("State", "").startswith("COMPLETED"):
    return
  cpus = to_int(job.rec.get("AllocCPUS"), 0)
  elapsed = slurm_hours_of(job.rec.get("Elapsed"))
  total = slurm_hours_of((job.rec.get("TotalCPU") or "").split(".")[0])
  if cpus >= 4 and elapsed * 60 >= 10 and total:
    eff = total / (cpus * elapsed)
    if eff < 0.3:
      yield F("U02", f"the CPUs were busy {eff:.0%} of the time ({cpus} CPUs, {elapsed:.2f} h)")

EXIT_MEANINGS = {
  1: "a general error of the program", 2: "a shell error (a wrong command or argument)",
  126: "the program could not be executed (permissions)", 127: "command not found",
  134: "aborted (SIGABRT: an assertion or abort())", 135: "bus error (SIGBUS: often a file truncated or a full disk)",
  137: "killed (SIGKILL: out of memory or the time limit)", 139: "segmentation fault (SIGSEGV: a crash)",
  143: "terminated (SIGTERM: cancelled or the time limit)",
}

@rule("U03", ERROR, "Why the job failed",
      "The state and exit code of a failed job, explained: OUT_OF_MEMORY, TIMEOUT, NODE_FAIL, exit codes.",
      "Knowing the cause tells what to change: memory, time, the program, or nothing (a node failure).",
      "See the finding: more memory (--mem), more time or checkpoints, debug the crash, or just resubmit after a node failure.",
      needs="job")
def u03(ctx):
  job = ctx.job
  state = (job.rec.get("State") or "").split()[0].rstrip("+")
  code = job.rec.get("ExitCode") or "0:0"
  rc, _, sig = code.partition(":")
  rc, sig = to_int(rc, 0), to_int(sig, 0)
  if state == "OUT_OF_MEMORY":
    from ..cluster.details import peak_job_memory
    peak, _ = peak_job_memory(job)
    yield F("U03", "out of memory" + (f" (peak {mem_text(peak / 1048576)})" if peak else "") + ": the job used more than it asked for",
            fix="ask for more: --mem (per node) or --mem-per-cpu, or use more nodes")
  elif state == "TIMEOUT":
    yield F("U03", f"the time limit ({job.rec.get('Timelimit')}) was reached", None, fix="more time (--time), or checkpoint and restart (U06)")
  elif state == "NODE_FAIL":
    yield F("U03", "a node failed: not your fault", None, WARNING, fix="resubmit it (--requeue does that automatically)")
  elif state == "FAILED":
    code_here = rc or (128 + sig if sig else 0)
    meaning = EXIT_MEANINGS.get(code_here) or (f"killed by signal {sig}" if sig else f"exit code {rc} of the program")
    yield F("U03", f"failed with exit code {rc}" + (f", signal {sig}" if sig else "") + f": {meaning}",
            fix="look at the end of the error log (and U05 below)")
  elif state in ("BOOT_FAIL", "DEADLINE", "PREEMPTED"):
    yield F("U03", f"{state.lower()}: ended by the system", None, WARNING, fix="resubmit it")

PENDING_REASONS = {
  "Priority": (INFO, "other jobs have a higher priority: it waits its turn", "a shorter --time can start it earlier (backfill)"),
  "Resources": (INFO, "it is next, but the resources are not free yet", ""),
  "Dependency": (INFO, "it waits for the jobs it depends on", ""),
  "BeginTime": (INFO, "it waits for its --begin time", ""),
  "JobHeldUser": (WARNING, "you held it: it never starts on its own", "scontrol release <jobid>"),
  "JobHeldAdmin": (WARNING, "an administrator held it", "ask the support"),
  "PartitionTimeLimit": (ERROR, "the time limit is over the partition's maximum: it will never start", "cancel it, resubmit with less --time (R05)"),
  "PartitionNodeLimit": (ERROR, "more nodes than the partition allows: it will never start", "cancel it, resubmit with fewer nodes"),
  "AssocGrpCpuLimit": (INFO, "your project's CPU limit is reached by your running jobs", "it starts when they end"),
  "AssocGrpNodeLimit": (INFO, "your project's node limit is reached", "it starts when other jobs end"),
  "AssocGrpGRES": (INFO, "your project's GPU limit is reached", "it starts when other jobs end"),
  "AssocMaxJobsLimit": (INFO, "you have the maximum number of running jobs", "it starts when one ends"),
  "QOSMaxJobsPerUserLimit": (INFO, "you have the maximum number of running jobs of this QOS", "it starts when one ends"),
  "QOSMaxWallDurationPerJobLimit": (ERROR, "the time limit is over the QOS's maximum: it will never start", "cancel it, resubmit with less --time"),
  "AssocGrpBillingMinutes": (ERROR, "your project's budget is used up", "ask your PI or the support for more"),
  "ReqNodeNotAvail": (WARNING, "a requested node is down, drained or reserved (maintenance?)", "remove --nodelist, or wait for the reservation to end"),
  "InvalidAccount": (ERROR, "the account is not valid", "cancel it, resubmit with one of your accounts (R06)"),
  "InvalidQOS": (ERROR, "the QOS is not valid", "cancel it, resubmit with a valid --qos"),
  "BadConstraints": (ERROR, "no node fits the request: it will never start", "cancel it, fix the request (R03)"),
  "Reservation": (INFO, "it waits for its reservation", ""),
  "launch failed requeued held": (ERROR, "the launch failed and the job was held", "look at the log, then scontrol release it"),
}

@rule("U04", INFO, "Why the job is waiting",
      "The pending reason of a waiting job in plain words, with what you can do.",
      "Some reasons only mean waiting; others mean the job will never start.",
      "See the finding.", needs="job")
def u04(ctx):
  job = ctx.job
  if not job.queued or not (job.state or "").startswith("PENDING"):
    return
  reason = (job.reason or "").strip("()")
  for key, (sev, text, todo) in PENDING_REASONS.items():
    if reason == key or reason.startswith(key):
      yield F("U04", f"{reason}: {text}", None, sev, fix=todo)
      return
  if reason and reason != "None":
    yield F("U04", f"{reason}: see 'man squeue', JOB REASON CODES")

LOG_PATTERNS = [
  (r"module: command not found|ml: command not found", ERROR, "the module command is missing: use #!/bin/bash -l (S02)"),
  (r"error while loading shared libraries: (\S+)", ERROR, "a shared library is missing ({0}): load the modules the program was built with (P05)"),
  (r"CUDA out of memory|cudaErrorMemoryAllocation|out of memory on device", ERROR, "the GPU memory ran out: smaller batches or more GPUs"),
  (r"oom[-_ ]kill|Out Of Memory|Killed process \d+|oom_kill_process", ERROR, "the kernel killed it for using too much memory: more --mem"),
  (r"Disk quota exceeded|EDQUOT", ERROR, "the quota was reached: free space or files, or write elsewhere (Q01)"),
  (r"No space left on device", ERROR, "the filesystem is full"),
  (r"Bus error", ERROR, "bus error: often a truncated input file, a full disk, or a memory-mapped file that changed"),
  (r"Segmentation fault|SIGSEGV|segfault", ERROR, "the program crashed (segmentation fault): debug it (core dumps, gdb)"),
  (r"Permission denied", WARNING, "a file or program is not accessible: check the permissions"),
  (r"command not found", WARNING, "a command is missing: check the modules and the PATH"),
  (r"No such file or directory", WARNING, "a path does not exist (P02, P03; relative paths: G18)"),
  (r"CANCELLED AT .* DUE TO TIME LIMIT", ERROR, "the time limit was reached: more --time, or checkpoints"),
  (r"CANCELLED AT .* DUE TO PREEMPTION", WARNING, "preempted by a higher priority job: resubmit, or --requeue"),
  (r"MPI_ABORT|MPI_Init.*fail|PMI.*error|PMIx.*error|mpi.*init.*error", ERROR, "MPI failed or aborted: use srun with the cluster's MPI"),
  (r"Exec format error|cannot execute binary file", ERROR, "the program is built for another CPU (P04)"),
  (r"no kernel image is available for execution", ERROR, "the GPU code is not built for these GPUs (P06)"),
  (r"CondaError|conda init", WARNING, "conda is not set up in the job (E02)"),
  (r"Traceback \(most recent call last\)", WARNING, "a Python exception"),
  (r"srun: error: .*: task \d+: (Exited with exit code \d+|Killed|Segmentation)", WARNING, "a task failed: {0}"),
  (r"slurmstepd: error: Detected \d+ oom.kill event", ERROR, "Slurm detected out-of-memory kills: more --mem"),
  (r"illegal instruction|SIGILL", ERROR, "illegal instruction: built for a newer CPU than the node's"),
  (r"Stale file handle", WARNING, "a filesystem problem on the node (stale file handle): resubmit"),
]

@rule("U05", WARNING, "Known errors in the logs",
      "Error messages in the end of the job's output and error files that have a known cause.",
      "The message is the clue: it tells what to fix before resubmitting.",
      "See the finding; each names the likely cause.", needs="logs")
def u05(ctx):
  seen = set()
  for which, text in ctx.logs.items():
    lines = text.splitlines()
    for pattern, sev, explain in LOG_PATTERNS:
      for i in range(len(lines) - 1, -1, -1):
        m = re.search(pattern, lines[i], re.I)
        if m:
          if explain in seen:
            break
          seen.add(explain)
          sample = lines[i].strip()[:140]
          yield F("U05", f"{which}: {sample}  → {explain.format(*m.groups())}", None, sev)
          break
  return

@rule("U06", INFO, "A long job without a safety net",
      "A job of a day or more without checkpoint handling (--signal, a trap, --requeue).",
      "When it hits the time limit or a node fails, all work since the start is lost.",
      "Let Slurm warn the job before the end and save a checkpoint: #SBATCH --signal=B:USR1@300 and trap 'save' USR1 "
      "in the script (or the program's own checkpoints), plus --requeue to restart after node failures.",
      "#SBATCH --signal=B:USR1@300\ntrap 'touch checkpoint.request' USR1\nsrun ./app & wait")
def u06(ctx):
  minutes = parse_time(ctx.opt("time")) if ctx.opt("time") else None
  if minutes and minutes >= 24 * 60 and minutes != math.inf and ctx.opt("signal") is None and ctx.opt("requeue") is None \
     and not ctx.script.uses(r"\btrap\b"):
    yield F("U06", f"--time={ctx.opt('time')} without --signal, trap or --requeue", ctx.directive_line("time"))

# ===========================================================================
# G: good practice

from .engine import relative_uses, alternative_bases, launcher_options # noqa: E402

SETUP_PATTERN = (r"(^|[;\s])(source|\.)\s+\S*activate|conda\s+activate|mamba\s+activate|uenv\s+(start|run|view)|"
                 r"spack\s+(env\s+activate|load)|(^|[;\s])(source|\.)\s+\S+")

def module_loads(script):
  """The modules the script loads (names as written)"""
  names = []
  for _, t in script.code_lines():
    m = re.match(r"\s*(?:module\s+(?:load|add|try-load|swap)|ml)\s+(.*)", t)
    if m:
      names += [w for w in m.group(1).split() if not w.startswith("-") and w not in ("purge", "reset", "list", "avail", "spider")]
  return names

def site_environment(ctx):
  """The job's software comes from a site option (uenv, container): the script cannot be replayed without it"""
  return any(ctx.opt(o) is not None for o in ("uenv", "view", "environment", "container-image", "container"))

@rule("G01", WARNING, "The script sets up its own environment",
      "A job that depends on the shell it was submitted from: modules, a uenv, a Python environment, PATH entries or "
      "variables that were active when sbatch ran, but that the script does not set up itself. Checked from the "
      "environment Slurm kept of the submission (sacct --env-vars), and by replaying the "
      "script's setup after a fresh login.",
      "By default (--export=ALL) a job inherits the submitting shell. It works today, but not after a fresh login, from "
      "another terminal, a cron job, a colleague, or Restart: then programs or libraries are missing (command not "
      "found, error while loading shared libraries) or other versions are used, and nobody can tell from the script.",
      "Set up everything in the script: module purge, then the module loads (or the uenv, or source .../activate), the "
      "exports it needs. Test it as it will run: log in fresh and call sbatch (the checks do that for you). To enforce "
      "it: #SBATCH --export=NONE, and after the setup export SLURM_EXPORT_ENV=ALL (else srun steps get an empty "
      "environment).",
      "#SBATCH --export=NONE\nmodule purge\nmodule load gcc/12.3.0 cray-mpich/8.1.29\nsource $HOME/venvs/sim/bin/activate\n"
      "export SLURM_EXPORT_ENV=ALL     # srun steps get this environment\nsrun ./sim", needs="any")
def g01(ctx):
  s = ctx.script
  if s is None:
    return
  export_none = (ctx.opt("export") or "").upper() == "NONE"
  loaded = module_loads(s)
  bases = {m.split("/")[0] for m in loaded}
  purges = s.uses(r"\b(module|ml)\s+(purge|reset)")
  sets_up = purges or bool(loaded) or s.uses(SETUP_PATTERN) or site_environment(ctx)
  evidence = False
  env = ctx.submit_env
  login = ctx.facts.env if ctx.facts else {}
  if env and not export_none:
    problems, snippet = [], []
    if not purges:
      login_mods = set(login.get("LOADEDMODULES", "").split(":"))
      extra = [m for m in env.get("LOADEDMODULES", "").split(":") if m and m not in login_mods and m.split("/")[0] not in bases]
      if extra:
        problems.append("the modules " + ", ".join(extra[:5]) + (" …" if len(extra) > 5 else ""))
        snippet += ["module purge", "module load " + " ".join(extra)]
    if env.get("UENV_MOUNT_LIST") and not login.get("UENV_MOUNT_LIST") and not site_environment(ctx) and not s.uses(r"\buenv\s"):
      image = os.path.basename(env["UENV_MOUNT_LIST"].split(",")[0].split(":")[0])
      problems.append(f"the uenv {image}")
      snippet.append(f"#SBATCH --uenv={image.rsplit('.', 1)[0]}")
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
      value = env.get(var)
      if value and value != login.get(var) and not (var == "CONDA_PREFIX" and env.get("CONDA_DEFAULT_ENV") == "base") \
         and not s.uses(r"activate"):
        problems.append(f"the Python environment {value}")
        snippet.append(f"source {value}/bin/activate" if var == "VIRTUAL_ENV" else f'eval "$(conda shell.bash hook)"\nconda activate {value}')
    text = re.sub(r"'[^']*'", "''", "\n".join(t for _, t in s.code_lines()))
    used = set(re.findall(r"\$\{?([A-Za-z_]\w*)", text))
    assigned = set(s.assignments())
    only = sorted(n for n in used if n in env and n not in login and n not in assigned and n not in KNOWN_VARS
                  and not n.startswith(("SLURM_", "SBATCH_", "SRUN_", "PMI", "BASH")) and n not in ("PWD", "OLDPWD", "SHLVL", "_"))
    if only:
      problems.append("the variables " + ", ".join("$" + n for n in only[:5]))
      snippet += [f"export {n}={shlex.quote(env[n])}" for n in only[:5]]
    if not problems:
      login_path = set(login.get("PATH", "").split(":"))
      extra_path = [p for p in env.get("PATH", "").split(":") if p and p not in login_path and p not in s.text]
      if extra_path:
        problems.append("PATH entries " + ", ".join(extra_path[:3]) + (" …" if len(extra_path) > 3 else ""))
    if problems:
      evidence = True
      yield F("G01", "it depends on the shell it was submitted from: " + "; ".join(problems) +
              " were active then, and the script does not set them up", s.first_command, WARNING, snippet="\n".join(snippet))
  replay = ctx.replay or {}
  if replay.get("fresh") and replay.get("notfound") and not site_environment(ctx):
    missing = replay["notfound"]
    evidence = True
    line = next((l for l, t, _ in s.launcher_lines() if any(m in t for m in missing)), None)
    yield F("G01", f"after a fresh login and the script's own setup, {', '.join(missing[:4])} "
                   f"{'is' if len(missing) == 1 else 'are'} not found: it only works from a prepared shell", line, WARNING)
  if not evidence and not sets_up and not export_none and \
     (list(s.launcher_lines()) or s.uses(r"(^|[;\s])(python3?|Rscript|julia|matlab|mpirun|mpiexec)\s")):
    yield F("G01", "the script sets up no environment (no module loads, uenv or activate): it relies on the shell "
                   "it is submitted from", s.first_command, INFO, snippet="module purge\nmodule load <what the program needs>")

@rule("G02", WARNING, "The job's result shows whether the work failed",
      "Commands after the last srun (echo done, cleanup, copies) without set -e; or a job that ended COMPLETED although "
      "one of its steps failed.",
      "The state of a job is the exit code of its script, which is that of its last command. With echo done at the "
      "end, a crashed program still gives COMPLETED: failures go unnoticed, dependencies (afterok) start on broken "
      "results.",
      "Stop at the first error with set -eo pipefail at the top, or end with the program's status: srun ./sim; "
      "status=$?; ...; exit $status.",
      "set -eo pipefail\nsrun ./sim\necho done       # only reached if srun succeeded", needs="any")
def g02(ctx):
  job, s = ctx.job, ctx.script
  if job is not None and (job.rec.get("State") or "").startswith("COMPLETED"):
    for st in job.steps:
      step = st["JobID"].partition(".")[2]
      if step.isdigit() and (not st["State"].startswith("COMPLETED") or st["ExitCode"] not in ("0:0", "")):
        yield F("G02", f"step {step} ended {st['State']} (exit {st['ExitCode']}), but the job says COMPLETED", None, WARNING)
        return
  if s is None or s.uses(r"\bset\s+-[a-zA-Z]*e") or s.uses(r"\bset\s+-o\s+errexit"):
    return
  launchers = list(s.launcher_lines())
  if not launchers:
    return
  last = launchers[-1][0]
  after = [(l, t) for l, t in s.code_lines() if l > last]
  if after and not re.match(r"\s*(exit|wait)\b", after[-1][1]) and not any("$?" in t or "||" in t for _, t in after):
    yield F("G02", f"after the last srun (line {last}) come {len(after)} more commands, last {after[-1][1].strip()[:40]}: "
                   "the job's state is theirs, not the program's", after[0][0], WARNING,
            snippet="set -eo pipefail      # at the top")

@rule("G03", WARNING, "Not submitted from inside another job",
      "A job submitted from within an allocation (an salloc or interactive job shell): its submit environment has the "
      "other job's SLURM_* variables.",
      "The new job inherits settings of the old one (SLURM_NTASKS, SLURM_CPUS_PER_TASK, SLURM_MEM_PER_NODE, …): srun "
      "in it may use the old job's numbers and fail or use the wrong resources.",
      "Submit from a login shell, not from inside a job; a job that submits the next one (a chain) should do it with "
      "sbatch --export=NONE (the next script sets up its own environment, see G01).", needs="any")
def g03(ctx):
  env = ctx.submit_env or {}
  other = env.get("SLURM_JOB_ID")
  own = ctx.job.rec.get("JobIDRaw") if ctx.job is not None else None
  if other and other != own:
    yield F("G03", f"it was submitted from inside job {other}: it inherited its SLURM_* settings")
  s = ctx.script
  if s is None:
    return
  for line, t in s.code_lines():
    if re.search(r"(^|[;\s(`])sbatch\s", t) and "--export=NONE" not in t and "env -i" not in t \
       and not re.search(r"unset\s+\$?\(?\s*(compgen|env\b)|for\s+\w+\s+in\s+\$\(\s*(compgen\s+-e|env)\b", s.text):
      yield F("G03", "the job submits another job from inside: it passes on its SLURM_* settings (tasks, CPUs, memory), "
                     "which the next job then uses", line, INFO,
              snippet="sbatch --export=NONE next.sh      # the next script sets up its own environment (G01)")
      return

def literal_counts(words):
  """{"ntasks": n, "cpus": c} given as numbers on a launcher line"""
  out, i = {}, 1
  while i < len(words):
    w, nxt = words[i], words[i + 1] if i + 1 < len(words) else ""
    for names, key in ((("-n", "--ntasks", "-np"), "ntasks"), (("-c", "--cpus-per-task"), "cpus")):
      for name in names:
        if w == name and nxt.isdigit():
          out[key] = int(nxt)
        elif w.startswith(name + "=") and w.split("=", 1)[1].isdigit():
          out[key] = int(w.split("=", 1)[1])
        elif name in ("-n", "-c") and re.fullmatch(re.escape(name) + r"\d+", w):
          out[key] = int(w[2:])
    i += 1
  return out

@rule("G04", WARNING, "Task and thread counts come from Slurm",
      "srun/mpirun lines with literal task or CPU counts (-n 128, -np 64, -c 16) instead of Slurm's variables.",
      "When the request changes (more nodes, another partition), literal counts silently disagree with the allocation: "
      "too many tasks fail, too few leave the rest of the allocation idle; mpirun -np does not know about Slurm at all.",
      "Use the allocation's values: srun without -n (it uses all tasks), or -n $SLURM_NTASKS, "
      "-c $SLURM_CPUS_PER_TASK.",
      "#SBATCH --ntasks=256\nsrun -n 128 ./sim              # half of the allocation idle\nsrun ./sim                     # all of it")
def g04(ctx):
  nodes, tpn, cpt, _ = ctx.counts()
  ntasks = to_int(ctx.opt("ntasks"), None) or (nodes * tpn if nodes and tpn else None)
  for line, t, launcher in ctx.script.launcher_lines():
    try:
      words = shlex.split(t)
    except ValueError:
      continue
    c = literal_counts([words[0], *launcher_options(words)]) # (the launcher's options, not the program's)
    n, cpus = c.get("ntasks"), c.get("cpus")
    if n is not None and ntasks:
      if launcher != "srun" and n != ntasks:
        yield F("G04", f"{launcher} -np {n}, the job has {ntasks} tasks", line, snippet="srun ./program")
      elif n > ntasks:
        yield F("G04", f"srun -n {n}, the job has only {ntasks} tasks", line)
      elif n < ntasks:
        yield F("G04", f"srun -n {n} uses {n} of the job's {ntasks} tasks", line, INFO)
      else:
        yield F("G04", f"-n {n} is written as a number: -n $SLURM_NTASKS keeps it right when the request changes", line, INFO)
      continue
    if cpus is not None:
      if cpus != cpt:
        yield F("G04", f"-c {cpus}, the job has --cpus-per-task={cpt}", line, snippet="-c $SLURM_CPUS_PER_TASK")
      else:
        yield F("G04", f"-c {cpus} is written as a number: -c $SLURM_CPUS_PER_TASK keeps it right", line, INFO)

def fixed_outputs(job):
  """Files a queued or running job writes under fixed names: its log (without %j/%A/%a) and redirections of its script"""
  out = set()
  pattern = job.rec.get("StdOut") or ""
  if pattern and pattern != "(null)" and not re.search(r"%[jJAa]", pattern) and job.stdout_path():
    out.add(job.stdout_path())
  wd = job.rec.get("WorkDir") or ""
  for m in re.finditer(r">>?\s*([^\s<>&|;$`%{}]+)", getattr(job, "script", None) or ""):
    target = m.group(1)
    if target not in ("/dev/null", "&1", "&2") and not target.startswith("&"):
      out.add(os.path.normpath(target if target.startswith("/") else os.path.join(wd, target)))
  return out

@rule("G05", WARNING, "Each run in its own directory",
      "Queued or running jobs in the same directory that write the same files: logs without %j, or results under fixed "
      "names (> result.txt).",
      "They overwrite each other's results and logs: what is in the file at the end is a mix, or the last one's.",
      "Give each run its own directory (mkdir -p run_$SLURM_JOB_ID && cd run_$SLURM_JOB_ID), or put the job id into "
      "the names (%j in #SBATCH lines, $SLURM_JOB_ID in the script).",
      "#SBATCH --output=logs/%x_%j.out\nRUN=$SCRATCH/runs/$SLURM_JOB_ID\nmkdir -p $RUN && cd $RUN || exit 1", needs="list")
def g05(ctx):
  job = ctx.job
  if job is None or not job.queued:
    return
  mine = fixed_outputs(job)
  if not mine:
    return
  for other in ctx.others:
    if other is job or not other.queued or (other.rec.get("WorkDir") or "") != (job.rec.get("WorkDir") or ""):
      continue
    shared = mine & fixed_outputs(other)
    if shared:
      yield F("G05", f"job {other.jid} writes the same {'file' if len(shared) == 1 else 'files'}: "
                     + ", ".join(sorted(os.path.basename(p) for p in shared)[:3]) + f" (in {job.rec.get('WorkDir')})")
      return

RECORD_PATTERN = (r"module\s+list|(^|[;\s])ml\s*($|;)|uenv\s+status|\bprintenv\b|(^|[;\s])env\s*($|[>|;])|"
                  r"git\s+.*(rev-parse|describe|log\s)|scontrol\s+show\s+job|pip3?\s+freeze|conda\s+(list|env\s+export)|"
                  r"spack\s+find|lscpu|nvidia-smi")

@rule("G06", INFO, "Record what ran",
      "A job script that runs programs but records nothing about how: no module list, uenv status, environment, code "
      "version (git), node, date.",
      "When a result looks wrong weeks later, nobody can tell which versions, modules, inputs and nodes produced it, "
      "or what changed against a run that worked (What changed can only compare the script).",
      "Write a short record into the log at the start of the job.",
      'echo "job $SLURM_JOB_ID ($SLURM_JOB_NAME) on $(hostname), $(date)"\nmodule list 2>&1        # or: uenv status\n'
      'git -C $HOME/code/sim rev-parse HEAD\nscontrol show job $SLURM_JOB_ID')
def g06(ctx):
  s = ctx.script
  if list(s.launcher_lines()) and not s.uses(RECORD_PATTERN):
    yield F("G06", "the job records nothing about its environment or code version", s.first_command,
            snippet='echo "job $SLURM_JOB_ID on $(hostname), $(date)"\nmodule list 2>&1\ngit -C /path/to/code rev-parse HEAD')

INTERACTIVE = [
  (r"(vim?|nano|emacs|less|more|man|htop|passwd|su)(\s|$)", "needs a terminal; a batch job has none"),
  (r"top(?!.*\s-b)(\s|$)", "needs a terminal (top -b works in batch)"),
  (r"sudo\s", "asks for a password nobody can type (and is not allowed on most clusters)"),
  (r"ssh\s+(.*\s)?-t", "asks for a terminal a batch job does not have"),
  (r"conda\s+init\b", "edits your ~/.bashrc; it is meant to be run once, interactively"),
  (r"srun\s.*--pty", "a pseudo terminal without a terminal to attach"),
  (r"(tmux|screen)(?!.*\s-d)(\s|$)", "needs a terminal (tmux new -d / screen -dm start detached)"),
  (r"(python3?|ipython|R|julia|matlab)\s*$", "starts an interactive interpreter: in a batch job it reads nothing and ends at once"),
  (r"read(\s|$)", "waits for input a batch job never gets (it reads nothing)"),
]

@rule("G09", WARNING, "No interactive commands in batch scripts",
      "Commands that need a person at a terminal: editors and pagers, top/htop, read prompts, sudo, ssh -t, srun --pty, "
      "tmux/screen, an interpreter without a script, conda init.",
      "A batch job has no terminal and nobody to answer: these fail, do nothing, or wait until the time limit and waste "
      "the allocation.",
      "Remove them, use their batch forms (top -b, tmux new -d), give interpreters a script, run prompts' answers as "
      "arguments; interactive work belongs in an interactive job (i in the job list).")
def g09(ctx):
  for line, t in ctx.script.code_lines():
    for part in re.split(r"\s*(?:&&|\|\||;)\s*", t):
      cmd = part.strip()
      if not cmd or cmd.startswith(("#", "while ", "if ")) or "<" in cmd:
        continue
      if "|" in t and cmd.startswith("read"): # (cmd | while read ...)
        continue
      for pattern, why in INTERACTIVE:
        if re.match(pattern, cmd):
          yield F("G09", f"{cmd[:50]}: {why}", line)
          break

@rule("G18", INFO, "The script works from any directory",
      "Relative paths (programs like ./sim, inputs, sourced files, logs) used before the script sets its directory, so "
      "they are relative to wherever sbatch was run. Also: relative --output/--error with a cd in the script (the logs "
      "are opened before the script runs, so the cd does not apply to them).",
      "The script only works when submitted from one particular directory: from anywhere else programs and inputs are "
      "not found, or a different input is used, and logs land elsewhere or nowhere (the job fails without a log).",
      "Name the directory once at the top: #SBATCH --chdir=/path/to/run (a literal path: #SBATCH lines expand no "
      "variables), or cd /path/to/run || exit 1 (or cd \"$1\" with the directory as an argument); relative paths "
      "below it are fine. Or use absolute paths.",
      "#SBATCH --chdir=/scratch/me/project/run1\n#SBATCH --output=logs/%x_%j.out   # relative to --chdir\nsrun ./sim input.dat",
      needs="any")
def g18(ctx):
  s = ctx.script
  if s is None:
    return
  uses, anchored, has_cd = relative_uses(ctx)
  uses = [u for u in uses if not is_placeholder(u[1])]
  chdir = ctx.opt("chdir")
  rel_logs = [n for n in ("output", "error", "input") if ctx.opt(n) and not ctx.opt(n).startswith("/")
              and not is_placeholder(ctx.opt(n)) and ctx.opt(n) != "/dev/null"]
  if rel_logs and has_cd and not chdir:
    n = rel_logs[0]
    yield F("G18", f"--{n}={ctx.opt(n)} is opened before the script runs, relative to the directory sbatch runs in: "
                   "the cd in the script does not apply to it", ctx.directive_line(n), WARNING,
            fix="#SBATCH --chdir=/path/to/run (then the log path is relative to it), or an absolute log path")
  evidence = False
  submit, other = alternative_bases(ctx)
  if other and submit and uses and ctx.probes is not None and not chdir:
    only = []
    for _line, word, _kind in uses:
      here, there = ctx.probe("f", os.path.normpath(os.path.join(submit, word))), ctx.probe("f", os.path.normpath(os.path.join(other, word)))
      if there is not None and there.exists and not (here is not None and here.exists):
        only.append(word)
    if only:
      evidence = True
      yield F("G18", f"{', '.join(only[:3])} {'exists' if len(only) == 1 else 'exist'} only next to the script ({other}), "
                     f"but it was submitted from {submit}", uses[0][0], WARNING)
  if ctx.others and ctx.job is not None and not evidence:
    name, wd = ctx.job.rec.get("JobName"), ctx.job.rec.get("WorkDir")
    same = [j for j in ctx.others if j.rec.get("JobName") == name and j.rec.get("WorkDir") != wd and not j.queued]
    failed_here = (ctx.job.rec.get("State") or "").startswith(("FAILED",)) and uses
    worked = [j for j in same if (j.rec.get("State") or "").startswith("COMPLETED")]
    if failed_here and worked:
      evidence = True
      yield F("G18", f"this run (from {wd}) failed, job {worked[0].jid} of the same script from {worked[0].rec.get('WorkDir')} "
                     "completed: it depends on where it is submitted from", None, WARNING)
  if uses and not evidence:
    words = list(dict.fromkeys(w for _, w, _ in uses))
    yield F("G18", f"relative paths before the script sets its directory ({', '.join(words[:4])}{' …' if len(words) > 4 else ''}): "
                   "it only works when submitted from the right directory", uses[0][0],
            snippet="#SBATCH --chdir=/path/to/run       # or: cd /path/to/run || exit 1")
  elif rel_logs and not chdir and not has_cd and not uses and ctx.job is None:
    n = rel_logs[0]
    yield F("G18", f"--{n}={ctx.opt(n)} is relative to the directory you run sbatch in", ctx.directive_line(n))

@rule("G19", WARNING, "The script does not look for itself",
      "$0, ${BASH_SOURCE[0]} or dirname \"$0\" used to find the script's own directory.",
      "Slurm runs a copy of the script from its spool directory (e.g. /var/spool/slurmd/job123/slurm_script): $0 points "
      "there, not to your file, so files next to the script are not found.",
      "Name the directory explicitly (#SBATCH --chdir, cd /path || exit 1, see G18), or use $SLURM_SUBMIT_DIR if "
      "submitting from the script's own directory is intended.",
      'cd "$(dirname "$0")"          # Slurm\'s spool directory, not yours\ncd /scratch/me/project || exit 1')
def g19(ctx):
  for line, t in ctx.script.code_lines():
    if re.search(r"\$\{?0\}?(?!\d)|BASH_SOURCE", t) and re.search(r"dirname|realpath|readlink|\bcd\b|\$\(", t):
      yield F("G19", f"{t.strip()[:60]}: in a batch job this is Slurm's copy of the script, in its spool directory", line)
      return

def array_log_name(value):
  """The %A_%a form of a log name: %A before an existing %a, else _%A_%a before the extension"""
  if "%a" in value and "%A" not in value:
    return value.replace("%a", "%A_%a", 1)
  stem, ext = os.path.splitext(value)
  return f"{stem}_%A_%a{ext}"

@rule("G07", WARNING, "Array logs name the array and the task (%A and %a)",
      "The --output or --error of a job array without both %A (the array's job id) and %a (the task's index) in the "
      "file name.",
      "Without %a, the tasks of one array run at the same time and all write into the same file: their lines are mixed "
      "or overwrite each other (a race), and no log tells about one task. Without %A, the next submission of the array "
      "writes into the same files as the last one and overwrites its logs (and two arrays running at once race).",
      "Name both: --output=logs/%x_%A_%a.out (and the same for --error). %j alone is unique too (every task has its "
      "own job id), but %A_%a says which array and which task a log belongs to.",
      "#SBATCH --array=1-100\n#SBATCH --output=run_%a.out          # the next run overwrites these\n"
      "#SBATCH --output=logs/run_%A_%a.out   # one file per array and task", needs="any")
def g07(ctx):
  from ..cluster.jobs import array_task_id
  array = ctx.opt("array") if ctx.script is not None else None
  is_task = ctx.job is not None and array_task_id(ctx.job) is not None
  if array is None and not is_task:
    return
  for name in ("output", "error"):
    v = ctx.opt(name) if ctx.script is not None else None
    line = ctx.directive_line(name)
    if v is None and is_task and ctx.script is None:
      raw = ctx.job.rec.get("StdOut" if name == "output" else "StdErr") or ""
      v = raw if raw and raw != "(null)" and "%" in raw else None # (the pattern of a task, as the accounting has it)
      line = None
    if not v or v == "/dev/null" or is_placeholder(v):
      continue
    has_a, has_A, has_j = "%a" in v, "%A" in v, bool(re.search(r"%[jJ]", v))
    if has_a and has_A:
      continue
    if has_j:
      yield F("G07", f"--{name}={v}: unique per task (%j), but %A_%a would say which array and task", line, INFO,
              snippet=f"#SBATCH --{name}={array_log_name(v.replace('%j', '').replace('%J', '').replace('__', '_'))}")
    elif not has_a:
      yield F("G07", f"--{name}={v} has no %a: the tasks of the array write into the same file at the same time (race)",
              line, snippet=f"#SBATCH --{name}={array_log_name(v)}")
    else:
      yield F("G07", f"--{name}={v} has no %A: the next run of the array overwrites these logs (and two runs at once race)",
              line, snippet=f"#SBATCH --{name}={array_log_name(v)}")
