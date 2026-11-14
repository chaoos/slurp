"""The guides (F1): how to use a Slurm cluster well, with this cluster's numbers. Links: rule:R01 opens the rule in
the catalogue, #anchor goes to a heading of the guides."""

import re

from . import checks

def topology_table(facts):
  if not facts or not facts.partitions:
    return "_The partitions of this cluster are not known (sinfo did not answer)._\n"
  rows = ["| Partition | Max time | CPUs/node | Sockets × cores × threads | GPUs/node | Memory/node | Whole nodes |",
          "|---|---|---|---|---|---|---|"]
  for p in sorted(facts.partitions.values(), key=lambda p: (not p.default, p.name)):
    rows.append(f"| {p.name}{' (default)' if p.default else ''} | {checks.time_text(p.max_minutes)} | {p.cpus} | "
                f"{p.sockets or '?'} × {p.cores or '?'} × {p.threads} | {p.gpus or '-'} | {checks.mem_text(p.mem_mb) if p.mem_mb else '?'} | "
                f"{'yes' if p.exclusive else 'no'} |")
  return "\n".join(rows) + "\n"

def rule_list(prefix):
  return "\n".join(f"- [{r.id}](rule:{r.id}) **{r.title}.** {r.implies}" for r in checks.RULES.values() if r.id.startswith(prefix))

def guides_markdown(facts=None, cluster=""):
  site = (facts.site if facts else {}) or {}
  p = facts.partition() if facts else None
  domains = site.get("numa_domains") or (p.sockets if p else 0) or 2
  cores = p.cores_per_node if p and p.cores_per_node else 64
  per = max(cores // max(domains, 1), 1)
  gpus = p.gpus if p else 0
  docs = site.get("docs")
  accounts = ", ".join(facts.accounts) if facts and facts.accounts else "(none found)"
  return f"""# Guides

How to use {cluster or 'the cluster'} well. The numbers on these pages are this cluster's{f' ({site["name"]})' if site.get('name') else ''}.
slurp checks your scripts and jobs against the [rule catalogue](rule:); every finding links to its rule.
{f'The site documentation: {docs}' if docs else ''}

## Slurm in five minutes

- **sbatch job.sh** submits a batch script: it waits in the queue, then runs on the first node of its allocation. Its
  output goes to a file (`--output`). This is how almost all work should run.
- **srun program** inside the script starts the parallel part: one copy per task, on all nodes of the job. Without
  srun, only the first node works ([B01](rule:B01)).
- **salloc** (or *Interactive job*, `i` in the job list) holds nodes for interactive work; shells on them open from the
  job window. Give it a short time and cancel it when done ([B10](rule:B10)).
- **squeue --me** lists your queued jobs, **sacct** finished ones, **scancel** cancels: slurp shows all of that.
- A job asks for **nodes**, **tasks** (processes, e.g. MPI ranks), **CPUs per task** (threads), **GPUs**, **memory**
  and **time**. It gets exactly that, and is charged for it, whether it uses it or not.

Your accounts here: {accounts}. Every job is charged to one (`--account`, [R06](rule:R06)).

## Writing a job script

1. The first line is `#!/bin/bash -l` ([S02](rule:S02)).
2. All `#SBATCH` lines come right after it, before any command: later ones are ignored ([S03](rule:S03)).
3. Write units: `--mem=4G` (not 4: that is 4 MB, [R01](rule:R01)), `--time=02:00:00` (2:00 is two minutes,
   [R02](rule:R02)). `#SBATCH` lines do not expand `$VARIABLES` or `~` ([S05](rule:S05)).
4. Give logs the job id: `--output=logs/%x_%j.out`, and create `logs/` before submitting: a missing log directory
   makes the job fail without any message ([P01](rule:P01), [B05](rule:B05)).
5. Set up the environment in the script: `module purge`, then exact versions ([E01](rule:E01)).
6. Stop on errors: `set -eo pipefail`, and `cd dir || exit 1` ([S07](rule:S07)).
7. Ask for realistic time: short jobs start sooner, because the scheduler fits them into gaps (backfill, [U01](rule:U01)).

**New job script** (`n` in the job list) writes such a script for the usual kinds of jobs, with this cluster's numbers.
Opening a script in the file browser shows its problems; **Explain** shows what Slurm makes of every line, **Test**
asks Slurm whether it accepts it and when it would start.

## Common mistakes

{rule_list("B")}

And the ones about the requests:

{rule_list("R")}

## Arrays and dependencies

- **Arrays** run one script many times: `#SBATCH --array=1-100%10` (100 tasks, at most 10 at once). Each task has its
  `$SLURM_ARRAY_TASK_ID`: use it to pick the input ([B03](rule:B03)), and give each task its log with `%A_%a`
  ([B04](rule:B04)). Arrays are much better than thousands of separate jobs or srun in a loop ([B14](rule:B14)).
- **Dependencies** chain jobs: `sbatch --dependency=afterok:<jobid> next.sh` starts `next.sh` when the first succeeded.
  `afterany` starts it whatever happened. If the first fails, an `afterok` job waits forever; add
  `--kill-on-invalid-dep=yes` ([B12](rule:B12)). `first=$(sbatch --parsable a.sh)` gives the id for the next one.

## Software: modules, stacks, Python

- **Modules** set up compilers and libraries: `module avail`, `module spider name`, `module load name/version`. Load the
  same ones you built with, in every job script, with versions ([E01](rule:E01), [P05](rule:P05)).
- **Site stacks** ({'uenv on Alps: `--uenv=` and `--view=` in the script' if 'uenv' in site.get('options', []) else 'uenv, spack, EasyBuild'}) give consistent sets of
  software; use them before building your own.
- **Python**: create environments once, not in the job ([E02](rule:E02)); on parallel filesystems many small files make
  every start slow ([P07](rule:P07)): containers or squashfs images are faster.
- **Check environment** in the job window or the script view loads the script's modules on the login node and checks
  that every program finds its shared libraries.

## Filesystems and quotas

- **$HOME**: small, backed up: sources, scripts, small results. Not for job output ([Q03](rule:Q03)).
- **$SCRATCH**{f" ({facts.env.get('SCRATCH')})" if facts and facts.env.get('SCRATCH') else ''}: big and fast, not backed up{f', files unused for {site["scratch_purge_days"]} days are deleted' if site.get('scratch_purge_days') else ''}
  ([Q04](rule:Q04), [Q05](rule:Q05)). Run jobs here.
- **Project / store**: permanent; copy what you keep there.
- **Quotas** count space *and* files. When either is full, writes fail: `Disk quota exceeded` ([Q01](rule:Q01)).
  **Quota** (`Q`) shows yours, **Old scratch files** (`X`) what the purge would take.
- Parallel filesystems hate many small files: pack them (HDF5, tar), avoid tens of thousands in one directory
  ([Q06](rule:Q06)).
- The node's `/tmp` is local and wiped after the job: copy results back ([B07](rule:B07)).

## NUMA placement

A node of this cluster's default partition{f' ({p.name})' if p else ''} has **{cores} cores in {domains} NUMA domains**
({per} cores each){f' and {gpus} GPUs' if gpus else ''}. Every domain has its own memory (and, on GPU nodes, its own
GPU): a core reaches its own domain's memory fast, the others slower. Placement means: keep each task and its
threads inside one domain, next to its memory and its GPU.

Partitions:

{topology_table(facts)}

**Recipes** for one node:

| Job | Tasks/node | CPUs/task | Binding |
|---|---|---|---|
| Pure MPI | {cores} | 1 | `srun --cpu-bind=cores` |
| Hybrid MPI + OpenMP | {domains} | {per} | `srun --cpu-bind=cores`, `OMP_PLACES=cores`, `OMP_PROC_BIND=close` |
| OpenMP only | 1 | {cores} | `OMP_PLACES=cores`, `OMP_PROC_BIND=spread` |{f'''
| GPU | {gpus} | {max(cores // gpus, 1)} | `--gpus-per-task=1`, `srun --cpu-bind=cores` |''' if gpus else ''}

Always:

```bash
#SBATCH --hint=nomultithread                     # one thread per core
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK      # threads = CPUs of the task (R09)
export OMP_PLACES=cores OMP_PROC_BIND=close      # bound, next to each other (N02)
export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK   # srun does not inherit --cpus-per-task (N04)
srun --cpu-bind=cores ./app
```

Check it: **Placement** in the job window of a running job draws its nodes, NUMA domain by NUMA domain, with
where every task may run and where its threads are, and names the problems ([N05](rule:N05)): shared cores, tasks
spread over domains, unbound tasks, GPUs used from the wrong domain ([N03](rule:N03)).

## GPUs

- Request them: `--gpus-per-node`, or better `--gpus-per-task=1` so that each task sees its own GPU ([N03](rule:N03)).
- Build for the GPUs of the cluster{f' ({site["cuda_arch"]})' if site.get('cuda_arch') else ''} ([P06](rule:P06)).
- Watch them: **Query current memory** in the job window shows GPU memory; `nvidia-smi` on the node (Run on the nodes).
- Idle GPUs are expensive: a GPU job that does not use them should not ask for them ([R07](rule:R07)).

## When a job fails

1. The **state** says a lot ([U03](rule:U03)): OUT_OF_MEMORY (more `--mem`), TIMEOUT (more `--time` or checkpoints,
   [U06](rule:U06)), NODE_FAIL (resubmit), FAILED (the program's exit code).
2. The **end of the logs**: the job window looks for known errors in them ([U05](rule:U05)): missing modules and
   libraries, quota, crashes, MPI.
3. **No log at all**: the log directory did not exist ([P01](rule:P01)), or the script was rejected.
4. **What changed** (failed jobs, job window): compares the script and the command line with the last run of the
   same job that worked.
5. Crashes leave **core dumps**: the job window lists them, GDB opens them.

## Being a good neighbour

- Login nodes are shared: no heavy computing there ([B09](rule:B09)); use an interactive job.
- Ask for what you use: idle cores, GPUs and hours are taken from others ([U01](rule:U01), [U02](rule:U02)).
- Few big files rather than many small ones; arrays rather than floods of tiny jobs.
"""
