"""What the job window shows besides the accounting: files, core dumps, commands on the nodes, memory"""

import os
from importlib import resources
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime

import humanize

from . import slurm as sl

from ..state import log, OLD_STEP_NAMES, STEP_NAME
from .remote import fs_isdir, fs_scandir, run, run_cmd, tab_name
from .jobs import batch_script, output_progress, parsable, progress_path, progress_state, record_progress
from .paths import existing_files, fs_infos, is_core_dump, job_vars, read_text, script_lines

# ---------------------------------------------------------------------------
# job details

@dataclass
class Entry:
  kind: str             # stdout, stderr, script, command, ...
  label: str            # path or description
  path: str = None      # file on disk
  text: str = None      # or content that is not a file
  note: str = ""
  exists: bool = False  # the path exists (checked by load_details)


def sacct_all(job):
  """All sacct fields of the job and its steps as 'key: value' blocks"""
  out = run(["sacct", "-j", job.jid, "-P", "-o", "ALL"])
  lines = out.splitlines()
  if len(lines) < 2:
    return out
  header = lines[0].split("|")
  blocks = []
  for line in lines[1:]:
    vals = line.split("|", len(header)-1)
    width = max(map(len, header))
    blocks.append("\n".join(f"{k:<{width}}  {v}" for k, v in zip(header, vals) if v))
  return "\n\n".join(blocks) + "\n"

def load_details(job):
  """Everything shown in the job window that needs extra queries or file system access"""
  d = {"files": [], "sstat": {}, "nodes": [], "progress": record_progress(job, output_progress(progress_path(job))) if job.running else None}
  workdir = job.rec["WorkDir"]
  files = d["files"]

  def add(entry): # checked and deduplicated at the end, all at once
    files.append(entry)

  for kind, path in [("stdout", job.stdout_path()), ("stderr", job.stderr_path())]:
    if path:
      add(Entry(kind, path, path=path))
  chosen = progress_state(job.jid)["file"]
  if chosen and chosen != job.stdout_path(): # the file chosen for the progress
    add(Entry("progress", chosen, path=chosen, note="checked for n/N"))
  if job.merged():
    files.append(Entry("stderr", "goes to the stdout file"))
  if workdir:
    add(Entry("workdir", workdir, path=workdir))
  for path in [job.stdout_path(), job.stderr_path()]:
    if path:
      add(Entry("output dir", os.path.dirname(path), path=os.path.dirname(path)))

  for entry in likely_job_core_dumps(job):
    add(entry)

  submitted = batch_script(job.jid) if job.queued else ""
  if submitted:
    add(Entry("script", "batch script as submitted", text=submitted, note="scontrol write batch_script"))
  if job.queued:
    add(Entry("scontrol", "scontrol show job", text=run(["scontrol", "show", "job", job.jid])))

  on_disk = ""
  submit_line = job.rec["SubmitLine"]
  if submit_line.startswith("sbatch"):
    for path in existing_files([submit_line], workdir):
      on_disk = read_text(path)
      add(Entry("script", path, path=path, note="on disk now, may differ from the submitted one"))
      break

  cmds = [submit_line] + [s["SubmitLine"] for s in job.steps if s["SubmitLine"] and s["JobName"] not in OLD_STEP_NAMES]
  for path in existing_files(cmds, workdir, dirs=True):
    add(Entry("command", path, path=path))
  env = job_vars(job)
  for path in existing_files(script_lines(submitted or on_disk), workdir, env, dirs=True):
    add(Entry("in script", path, path=path))

  infos = fs_infos([e.path for e in files if e.path])
  checked, seen = [], set()
  for e in files:
    info = infos.get(e.path) if e.path else None
    if e.kind == "output dir" and not (info and info["isdir"]):
      continue
    key = info["realpath"] if info else e.path or (e.kind, e.label)
    if key in seen:
      continue
    seen.add(key)
    checked.append(e)
    e.exists = info is not None
    if info and info["isdir"]:
      e.label = e.path.rstrip("/") + "/"
      e.note = e.note or "directory"
    elif e.path and not info:
      e.note = "does not exist (yet)"
    elif info:
      e.note = (e.note or humanize.naturalsize(info["size"])) + (" HDF5" if info["hdf5"] else " binary" if info["binary"] else "")
  files[:] = checked
  add(Entry("sacct", "all accounting fields of the job and its steps", text=None, note="sacct -o ALL"))

  if job.running:
    if job.rec["NodeList"]:
      d["nodes"] = run(["scontrol", "show", "hostnames", job.rec["NodeList"]]).split()
  return d

CORE_DUMP_NAME = re.compile(r"^core_(.+)_(\d+)$")

def likely_job_core_dumps(job):
  """Find top-level core files in a job's workdir that match its node and run time."""
  rec = job.rec
  workdir = rec.get("WorkDir")
  if not workdir or workdir in ("(null)", "None") or not fs_isdir(workdir):
    return []
  try:
    start = datetime.fromisoformat(rec["Start"].replace("Z", "+00:00")).timestamp()
  except (AttributeError, KeyError, TypeError, ValueError):
    return []
  try:
    end = datetime.fromisoformat(rec["End"].replace("Z", "+00:00")).timestamp()
  except (AttributeError, KeyError, TypeError, ValueError):
    if not job.running:
      return []
    end = time.time()

  # NodeList may be a Slurm hostlist expression. Expand it so core_<node>_<pid>
  # names can be checked against the allocation.
  node_list = rec.get("NodeList", "")
  if node_list in ("(null)", "None", "None assigned"):
    node_list = ""
  nodes = set(run(["scontrol", "show", "hostnames", node_list], timeout=10, quiet=True).split()) if node_list else set()
  if not nodes and node_list and not re.search(r"[\[\]]", node_list):
    nodes = set(node_list.split(","))

  found = []
  try:
    with fs_scandir(workdir) as entries:
      for de in entries:
        match = CORE_DUMP_NAME.fullmatch(de.name)
        if not match:
          continue
        node, pid = match.groups()
        if nodes and node not in nodes:
          continue
        try:
          stat = de.stat(follow_symlinks=True)
          if not de.is_file(follow_symlinks=True) or not start - 300 <= stat.st_mtime <= end + 300:
            continue
          if not is_core_dump(de.path):
            continue
        except OSError:
          continue
        confidence = "likely" if nodes and node in nodes else "possible"
        note = f"{confidence} · {node} · PID {pid} · {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}"
        found.append(Entry("core", de.name, path=de.path, note=note))
  except OSError:
    return []
  return sorted(found, key=lambda entry: entry.label)

def run_on_nodes(job, cmd, node=None):
  """Run a shell command once per node inside the allocation of a running job"""
  args = ["srun", f"--jobid={job.rec['JobIDRaw']}", "--overlap", "--ntasks-per-node=1", f"--job-name={STEP_NAME}",
          "--label", "--quiet"]
  args += ["--nodes=1", "-w", node] if node else [f"--nodes={job.rec['NNodes'] or 1}"]
  args += ["bash", "-c", f'echo "== $(hostname)"; {cmd}']
  log.debug(f"running command: {shlex.join(args)}")
  try:
    res = run_cmd(args, timeout=60)
    out, err = res.stdout, res.stderr
  except OSError as e:
    out, err = "", str(e)
  tasks, other = {}, []
  for line in out.splitlines():
    if (m := re.match(r'\s*(\d+): ?(.*)', line)):
      tasks.setdefault(int(m.group(1)), []).append(m.group(2))
    else:
      other.append(line)
  text = [f"$ {cmd}    ({node or 'all nodes'}, {datetime.now():%H:%M:%S})", ""]
  for task in sorted(tasks):
    text += tasks[task] + [""]
  text += other + ([f"srun: {err.strip()}"] if err.strip() else [])
  return "\n".join(text) + "\n"

SCRIPTS = resources.files("slurp") / "scripts" # shell scripts run on the nodes (slurp/scripts)

def run_script_on_nodes(job, script, *args, node=None, timeout=60):
  """Run one of the scripts of slurp/scripts once per node of a running job, as a step of the job. The script is not
  copied to the cluster: its text goes to srun on stdin (--input=all: to the task on every node), bash -s runs it.
  -> {task: [lines]} (the output of each node), the other output, the error"""
  text = (SCRIPTS / script).read_text()
  srun = ["srun", f"--jobid={job.rec['JobIDRaw']}", "--overlap", "--ntasks-per-node=1", f"--job-name={STEP_NAME}",
          "--input=all", "--label", "--quiet"]
  srun += ["--nodes=1", "-w", node] if node else [f"--nodes={job.rec['NNodes'] or 1}"]
  argv = ["sh", "-c", 'script=$1; shift; printf "%s" "$script" | "$@"', "sh", text, *srun, "bash", "-s", "--", *map(str, args)]
  log.debug(f"running {script} on the nodes of job {job.jid}: {shlex.join(srun)} bash -s -- {shlex.join(map(str, args))}")
  try:
    res = run_cmd(argv, timeout=timeout)
    out, err = res.stdout, res.stderr
  except OSError as e:
    out, err = "", str(e)
  tasks, other = {}, []
  for line in out.splitlines():
    if (m := re.match(r'\s*(\d+): ?(.*)', line)):
      tasks.setdefault(int(m.group(1)), []).append(m.group(2))
    else:
      other.append(line)
  return tasks, other, err.strip()

def amd_table(lines):
  """rows of a rocm-smi --csv table (as dicts by lower-case header), from the line with a header on"""
  rows, header = [], None
  for line in lines:
    cells = [c.strip() for c in line.split(",")]
    if header is None:
      if len(cells) > 1 and any(c.lower() in ("pid", "device") for c in cells):
        header = [c.lower() for c in cells]
      continue
    if len(cells) == len(header):
      rows.append(dict(zip(header, cells)))
  return rows

def parse_gpu_probe(lines, pids):
  """Memory of the job's processes (bytes, None if unknown) and [(gpu, name, used, total bytes)] of a node"""
  proc, devices = None, []
  amd_proc, amd_dev = [], []
  for line in lines:
    key, _, value = line.partition("=")
    if key == "GPU":
      pid, sep, memory = value.partition(",")
      match = re.match(r"\s*(\d+)", memory)
      if sep and pid.strip() in pids and match:
        proc = (proc or 0) + int(match.group(1)) * 1024**2
      elif proc is None:
        proc = 0
    elif key == "GPUDEV":
      f = [c.strip() for c in value.split(",")]
      if len(f) == 4 and f[2].isdigit() and f[3].isdigit():
        devices.append((f[0], f[1], int(f[2]) * 1024**2, int(f[3]) * 1024**2))
    elif key == "AMDPROC":
      amd_proc.append(value)
    elif key == "AMDDEV":
      amd_dev.append(value)
  if amd_proc or amd_dev:
    proc = 0
    for row in amd_table(amd_proc):
      vram = next((v for k, v in row.items() if "vram" in k), "")
      if row.get("pid") in pids and vram.isdigit():
        proc += int(vram)
    for row in amd_table(amd_dev):
      used = next((v for k, v in row.items() if "vram" in k and "used" in k), "")
      total = next((v for k, v in row.items() if "vram" in k and "total" in k and "used" not in k), "")
      name = next((v for k, v in row.items() if "series" in k or "product" in k or "name" in k), "")
      if used.isdigit() and total.isdigit():
        devices.append((row.get("device", "?"), name, int(used), int(total)))
  return proc, devices

def current_memory_usage(job):
  """Query current process RSS and per-job GPU usage; called only by the detail button."""
  fields = ["JobID", "MaxRSS", "AveRSS"]
  rows = parsable(run(["sstat", "-a", "-j", job.rec["JobIDRaw"], "-P", "-n", "-o", ",".join(fields)]), fields)
  sstat = {row["JobID"].partition(".")[2]: row for row in rows}
  tasks, other, err = run_script_on_nodes(job, "memory_probe.sh", job.rec["JobIDRaw"])
  if err:
    log.warning(f"memory query of job {job.jid}: {err[:300]}")
  output = "\n".join(line for task in sorted(tasks) for line in tasks[task])
  nodes = {}
  node = None
  for line in output.splitlines():
    if line.startswith("== "):
      node = line[3:].strip()
      nodes[node] = {"cpu_kib": None, "pids": set(), "gpu_lines": [], "gpu_error": ""}
    elif node and line.startswith("CPU_KIB="):
      try:
        nodes[node]["cpu_kib"] = int(line.partition("=")[2])
      except ValueError:
        pass
    elif node and line.startswith("CPU_UNAVAILABLE="):
      nodes[node]["cpu_error"] = line.partition("=")[2]
    elif node and line.startswith("JOB_PIDS="):
      nodes[node]["pids"] = {pid for pid in line.partition("=")[2].split(",") if pid.isdigit()}
    elif node and line.startswith("GPU_UNAVAILABLE="):
      nodes[node]["gpu_error"] = line.partition("=")[2]
    elif node and line.split("=", 1)[0] in ("GPU", "GPUDEV", "AMDPROC", "AMDDEV"):
      nodes[node]["gpu_lines"].append(line)
  for data in nodes.values():
    data["gpu_bytes"], data["gpus"] = parse_gpu_probe(data["gpu_lines"], data["pids"])

  cpu_values = [node_data["cpu_kib"] for node_data in nodes.values() if node_data["cpu_kib"] is not None]
  cpu_bytes = sum(cpu_values) * 1024 if cpu_values else None
  cpu_source = "ps"
  if cpu_bytes is None:
    step_counts = {step["JobID"].partition(".")[2]: int(step["NTasks"] or 0) for step in job.steps}
    estimates = [sl.sacct_bytes(row.get("AveRSS") or "") * step_counts.get(step, 0)
                 for step, row in sstat.items()]
    estimates = [value for value in estimates if value]
    if estimates:
      cpu_bytes = sum(estimates)
      cpu_source = "sstat estimate"
  cpu_partial = len(cpu_values) < len(nodes)
  gpu_nodes = [node_data for node_data in nodes.values() if not node_data["gpu_error"] and node_data["gpu_bytes"] is not None]
  gpu_bytes = sum(node_data["gpu_bytes"] for node_data in gpu_nodes) if gpu_nodes else None
  gpu_partial = len(gpu_nodes) < len(nodes)
  gpus = [(name, *gpu) for name, data in nodes.items() for gpu in data["gpus"]]
  return {"cpu_bytes": cpu_bytes, "cpu_source": cpu_source, "cpu_partial": cpu_partial, "gpu_bytes": gpu_bytes,
          "gpu_partial": gpu_partial, "gpu_errors": [d["gpu_error"] for d in nodes.values() if d["gpu_error"]],
          "gpus": gpus, "sstat": sstat}

# ---- memory watch: queries of a job's memory, kept, at intervals, written to a file

MEMORY = {} # (tab, job id) -> watch: {"samples", "every", "due", "busy", "job", "file", "error", "misses"}
MEMORY_EVERY = [("no repeat", 0), ("every minute", 60), ("every 5 min", 300), ("every 15 min", 900),
                ("every 30 min", 1800), ("every hour", 3600)]

def memory_dir():
  return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "slurp", "memory")

def memory_watch(jid, job=None):
  """The memory watch of a job of the current tab (created on first use)"""
  key = (tab_name(), jid)
  if key not in MEMORY:
    name = re.sub(r"[^\w.@-]+", "_", f"{key[0]}-{jid}")
    MEMORY[key] = {"samples": [], "every": 0, "due": 0.0, "busy": False, "job": job, "error": "", "misses": 0,
                   "file": os.path.join(memory_dir(), f"{name}.csv")}
  if job is not None:
    MEMORY[key]["job"] = job
  return MEMORY[key]

def record_memory(watch, result):
  """Keep a query of current_memory_usage() and append it to the file of the watch. False: nothing was measured."""
  if "error" in result or result.get("cpu_bytes") is None and result.get("gpu_bytes") is None:
    watch["misses"] += 1
    watch["error"] = result.get("error") or "nothing measured (has the job ended?)"
    return False
  gpus = result.get("gpus") or []
  sample = (time.time(), result.get("cpu_bytes"), result.get("gpu_bytes"),
            sum(g[3] for g in gpus) if gpus else None, sum(g[4] for g in gpus) if gpus else None)
  watch["samples"].append(sample)
  watch["error"], watch["misses"] = "", 0
  try:
    os.makedirs(os.path.dirname(watch["file"]), exist_ok=True)
    new = not os.path.exists(watch["file"])
    with open(watch["file"], "a") as f:
      if new:
        f.write("time,cpu_rss_bytes,gpu_job_bytes,gpu_used_bytes,gpu_total_bytes\n")
      f.write(",".join([datetime.fromtimestamp(sample[0]).isoformat(timespec="seconds"),
                        *("" if v is None else str(v) for v in sample[1:])]) + "\n")
  except OSError as e:
    watch["error"] = f"cannot write {watch['file']}: {e}"
  return True

def peak_job_memory(job):
  """Slurm-accounted CPU and (when configured) GPU peak memory for an ended job."""
  records = [job.rec, *job.steps]
  cpu_peak = max((sl.sacct_bytes(row.get("MaxRSS") or "") for row in records), default=0)
  gpu_peaks = []
  for row in records:
    for tres in (row.get("TRESUsageInMax") or "").split(","):
      key, sep, value = tres.partition("=")
      if sep and key.strip().lower().endswith("gpumem"):
        gpu_peaks.append(sl.sacct_bytes(value.strip()))
  return cpu_peak, max(gpu_peaks, default=0)
