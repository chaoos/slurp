"""Placement of a running job: which cores, NUMA domains and GPUs its tasks and threads use, and the problems
(shared cores, threads crossing NUMA domains, more threads than cores, GPUs of another NUMA domain)."""

from dataclasses import dataclass, field

def cpu_list(text):
  """'0-3,8,10-11' -> {0,1,2,3,8,10,11}"""
  out = set()
  for part in (text or "").split(","):
    part = part.strip()
    if not part:
      continue
    a, _, b = part.partition("-")
    try:
      out.update(range(int(a), int(b or a) + 1))
    except ValueError:
      pass
  return out

def ranges(cpus):
  """{0,1,2,3,8} -> '0-3,8'"""
  cpus = sorted(cpus)
  out, start = [], None
  for i, c in enumerate(cpus):
    if start is None:
      start = c
    if i + 1 == len(cpus) or cpus[i + 1] != c + 1:
      out.append(f"{start}-{c}" if c != start else str(c))
      start = None
  return ",".join(out)

@dataclass
class Task:
  step: str
  local: str
  pids: list = field(default_factory=list)
  comm: str = ""
  allowed: set = field(default_factory=set)
  threads: int = 0
  running: dict = field(default_factory=dict)   # cpu -> threads on it now
  gpus: set = field(default_factory=set)        # GPU indices it uses

  @property
  def label(self):
    return f"{self.step}.{self.local}"

@dataclass
class Node:
  host: str
  cpu_numa: dict = field(default_factory=dict)  # cpu -> NUMA domain
  cpu_core: dict = field(default_factory=dict)  # cpu -> core
  gpus: dict = field(default_factory=dict)      # index -> (bus, numa)
  tasks: dict = field(default_factory=dict)     # (step, local) -> Task

  def numa_of(self, cpus):
    return {self.cpu_numa[c] for c in cpus if c in self.cpu_numa}

def parse_node(lines):
  node = Node("?")
  procs_gpu = []
  for line in lines:
    key, _, value = line.partition("=")
    if key == "HOST":
      node.host = value.strip()
    elif key == "CPU":
      f = value.split(",")
      if len(f) >= 3 and f[0].isdigit():
        node.cpu_core[int(f[0])] = int(f[1]) if f[1].isdigit() else int(f[0])
        node.cpu_numa[int(f[0])] = int(f[2]) if f[2].isdigit() else 0
    elif key == "GPU":
      f = value.split(",")
      if len(f) >= 3:
        node.gpus[f[0].strip()] = (f[1].strip(), int(f[2]) if f[2].strip().lstrip("-").isdigit() else -1)
    elif key == "GPUPROC":
      f = value.split(",")
      if len(f) >= 2:
        procs_gpu.append((f[0], f[1]))
    elif key == "PROC":
      f = value.split("|")
      if len(f) < 7:
        continue
      step, local, pid, comm, allowed, threads, running = f[:7]
      task = node.tasks.setdefault((step, local), Task(step, local))
      task.pids.append(pid)
      if comm and comm not in ("bash", "sh", "srun") or not task.comm:
        task.comm = comm
      task.allowed |= cpu_list(allowed)
      task.threads += int(threads) if threads.isdigit() else 0
      for item in running.split():
        cpu, _, n = item.partition(":")
        if cpu.isdigit() and n.isdigit():
          task.running[int(cpu)] = task.running.get(int(cpu), 0) + int(n)
  bus_index = {bus: i for i, (bus, _) in node.gpus.items()}
  for pid, bus in procs_gpu:
    for task in node.tasks.values():
      if pid in task.pids:
        index = bus_index.get(bus) or next((i for b, i in bus_index.items() if b.endswith(bus) or bus.endswith(b)), None)
        if index is not None:
          task.gpus.add(index)
  return node

def problems(node):
  """Problems of the placement on a node, in words"""
  out = []
  all_cpus = set(node.cpu_numa)
  tasks = [t for t in node.tasks.values() if t.threads]
  domains = set(node.cpu_numa.values())
  per_domain = {d: {c for c, n in node.cpu_numa.items() if n == d} for d in domains}
  unbound = [t for t in tasks if all_cpus and t.allowed >= all_cpus]
  if len(tasks) > 1 and unbound:
    out.append(f"{node.host}: {len(unbound)} of {len(tasks)} tasks are not bound (each may run on any core): use srun --cpu-bind=cores")
  for i, a in enumerate(tasks):
    for b in tasks[i + 1:]:
      if a in unbound or b in unbound:
        continue
      shared = a.allowed & b.allowed
      if shared:
        out.append(f"{node.host}: tasks {a.label} and {b.label} share {len(shared)} CPUs ({ranges(shared)})")
  for t in tasks:
    cores = {node.cpu_core.get(c, c) for c in t.allowed}
    if t.threads > len(t.allowed) and len(t.allowed) and t.threads > 2 * len(cores):
      out.append(f"{node.host}: task {t.label} ({t.comm}) has {t.threads} threads on {len(t.allowed)} CPUs")
    spans = node.numa_of(t.allowed)
    if len(spans) > 1 and t not in unbound and any(len(t.allowed) <= len(cpus) for cpus in per_domain.values()):
      out.append(f"{node.host}: task {t.label} is spread over NUMA domains {', '.join(map(str, sorted(spans)))}")
    used = node.numa_of(set(t.running))
    if len(used) > 1 and t.threads > 1 and len(spans) <= 1:
      pass
    for g in t.gpus:
      numa = node.gpus.get(g, ("", -1))[1]
      if numa >= 0 and spans and numa not in spans:
        out.append(f"{node.host}: task {t.label} uses GPU {g} of NUMA domain {numa}, it runs on {', '.join(map(str, sorted(spans)))}")
  gpu_users = {}
  for t in tasks:
    for g in t.gpus:
      gpu_users.setdefault(g, []).append(t.label)
  for g, users in gpu_users.items():
    if len(users) > 1:
      out.append(f"{node.host}: tasks {', '.join(users)} all use GPU {g}")
  if node.gpus and tasks and gpu_users and len(gpu_users) < min(len(node.gpus), len(tasks)):
    idle = sorted(set(node.gpus) - set(gpu_users))
    if idle:
      out.append(f"{node.host}: GPUs {', '.join(idle)} are not used by the job")
  return out

def analyse(task_lines):
  """{task: lines} of placement.sh -> {"nodes": [Node], "problems": [str]}"""
  nodes = [parse_node(lines) for _, lines in sorted(task_lines.items())]
  out = {"nodes": nodes, "problems": []}
  for n in nodes:
    out["problems"] += problems(n)
  return out
