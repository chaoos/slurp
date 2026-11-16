"""Job script templates: commented scripts for the usual kinds of jobs, with the cluster's real numbers"""


KINDS = [
  ("serial", "Serial: one program on one core"),
  ("openmp", "OpenMP: one program with threads on one node"),
  ("mpi", "MPI: many tasks on one or more nodes"),
  ("hybrid", "Hybrid MPI + OpenMP: a task per NUMA domain, threads inside"),
  ("gpu", "GPU: a task per GPU, threads next to its GPU"),
  ("array", "Array: many independent runs of the same script"),
  ("chain", "Chain: a job that starts after another one succeeded"),
  ("checkpoint", "Long run with checkpoints: saves before the time limit, resubmits itself"),
]

def defaults(kind, partition, numa_domains=0):
  """{nodes, ntasks_per_node, cpus_per_task, gpus_per_node, time} for a kind on a partition"""
  cores = partition.cores_per_node if partition else 0
  gpus = partition.gpus if partition else 0
  domains = numa_domains or (partition.sockets if partition else 0) or 1
  d = {"nodes": 1, "ntasks_per_node": 1, "cpus_per_task": 1, "gpus_per_node": 0, "time": "01:00:00"}
  if kind == "openmp":
    d["cpus_per_task"] = cores or 8
  elif kind == "mpi":
    d["nodes"], d["ntasks_per_node"] = 2, cores or 32
  elif kind == "hybrid":
    d["nodes"], d["ntasks_per_node"] = 2, domains
    d["cpus_per_task"] = max((cores // domains) if cores else 8, 1)
  elif kind == "gpu":
    n = gpus or 1
    d["ntasks_per_node"], d["gpus_per_node"] = n, n
    d["cpus_per_task"] = max((cores // n) if cores else 8, 1)
  elif kind == "checkpoint":
    d["time"] = "1-00:00:00"
  return d

def make_script(kind, name, account, partition_name, nodes, ntasks_per_node, cpus_per_task, gpus_per_node, time,
                program="./my_program", site=None, directory=""):
  site = site or {}
  lines = ["#!/bin/bash -l",
           f"# {dict(KINDS)[kind]}",
           "# Made by slurp (New job script). The rule catalogue (F2) explains every check, the guides (F1) the why.",
           "#",
           "# All #SBATCH lines must come before the first command, or Slurm ignores them.",
           f"#SBATCH --job-name={name}"]
  if account:
    lines.append(f"#SBATCH --account={account}          # which project is charged")
  if partition_name:
    lines.append(f"#SBATCH --partition={partition_name}")
  lines.append(f"#SBATCH --time={time}              # realistic, not the maximum: short jobs start sooner (backfill)")
  if directory:
    lines.append(f"#SBATCH --chdir={directory}   # the job runs here, wherever you submit it from (G18)")
  if kind in ("serial", "array", "chain"):
    lines += ["#SBATCH --ntasks=1", "#SBATCH --cpus-per-task=1"]
  else:
    lines += [f"#SBATCH --nodes={nodes}",
              f"#SBATCH --ntasks-per-node={ntasks_per_node}" + ("      # one MPI rank per NUMA domain" if kind == "hybrid" else
                                                                 "      # one task per GPU" if kind == "gpu" else ""),
              f"#SBATCH --cpus-per-task={cpus_per_task}" + ("        # the cores of that domain" if kind in ("hybrid", "gpu") else "")]
    if kind in ("openmp", "mpi", "hybrid", "gpu", "checkpoint"):
      lines.append("#SBATCH --hint=nomultithread        # one thread per core")
  if kind == "gpu" or gpus_per_node:
    lines.append("#SBATCH --gpus-per-task=1             # each task sees its own GPU")
  if kind == "array":
    lines += ["#SBATCH --array=1-10%5                # tasks 1..10, at most 5 at a time",
              "#SBATCH --output=logs/%x_%A_%a.out    # %A array job, %a task: one log per task"]
  else:
    lines.append("#SBATCH --output=logs/%x_%j.out       # %x job name, %j job id; logs/ must exist (slurp made it)")
  if kind == "checkpoint":
    lines += ["#SBATCH --signal=B:USR1@600            # 10 minutes before the limit: USR1 to this script",
              "#SBATCH --requeue                     # restarted after a node failure"]
  lines += ["", "set -eo pipefail                       # stop at the first error", ""]
  if site.get("software_hint"):
    lines.append(f"# {site['software_hint']}")
  lines += ["# everything the job needs is set up here, not in the shell you submit from (G01); to enforce that:",
            "# #SBATCH --export=NONE above, and export SLURM_EXPORT_ENV=ALL after the setup (for the srun steps)",
            "module purge                           # a clean, reproducible environment",
            "# module load gcc/12.3.0 openmpi/4.1.6  # what your program was built with, with versions (module spider)", ""]
  threads = kind in ("openmp", "hybrid", "gpu", "checkpoint") or (cpus_per_task or 1) > 1
  if threads:
    lines += ["export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK      # threads = cores of the task",
              "export OMP_PLACES=cores                          # one thread per core ...",
              "export OMP_PROC_BIND=close                       # ... next to each other",
              "export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK   # srun does not inherit --cpus-per-task (Slurm 22.05+)", ""]
  lines += ["# the job's own record: what ran where (G06)",
            'echo "job $SLURM_JOB_ID ($SLURM_JOB_NAME) on $(hostname), $(date)"',
            "module list 2>&1", ""]
  srun = "srun --cpu-bind=cores" if kind not in ("serial", "array", "chain") else "srun"
  if kind == "array":
    lines += ["# every task works on its own input: $SLURM_ARRAY_TASK_ID is 1, 2, ... 10",
              f"{srun} {program} input_${{SLURM_ARRAY_TASK_ID}}.dat"]
  elif kind == "chain":
    lines += [f"{srun} {program}",
              "",
              "# Submit the chain:",
              "#   first=$(sbatch --parsable step1.sh)",
              "#   sbatch --dependency=afterok:$first --kill-on-invalid-dep=yes step2.sh",
              "# afterok: only if the first succeeded; --kill-on-invalid-dep cancels it if the first failed (else it waits forever)"]
  elif kind == "checkpoint":
    lines += ["# the program saves a checkpoint when told to (here: when the file checkpoint.request appears)",
              "trap 'echo \"time limit near: checkpointing\"; touch checkpoint.request' USR1",
              "",
              f"{srun} {program} --restart-from-latest-checkpoint &",
              "wait || true    # (wait, not the program directly: so the trap runs when the signal comes)",
              "wait            # (the program finishes its checkpoint)",
              "",
              "# not done yet? submit the next part (a chain of parts until the work is finished)",
              "# [ -f finished ] || sbatch --export=NONE /path/to/this/script.sh   # (not \"$0\": Slurm runs a copy, G19)"]
  else:
    lines.append(f"{srun} {program}")
  return "\n".join(lines) + "\n"
