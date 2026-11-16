# The overview

Every tab opens on its **Overview**, the first subtab, before **Jobs**: the cluster at a glance. `C` (or *Overview
of the cluster* in the ☰ Menu) shows it and asks the cluster again.

![The overview of a cluster: its health, your jobs, your quotas, the partitions](../images/overview.svg)

Its title says whether the cluster is **healthy** (green), **degraded** (yellow) or **unhealthy** (red) and why, who
you are logged in as (with the round trip), and the Slurm version. Below it:

- **Your jobs**: running (on how many nodes), pending and why, those finished within the job list's *finished since*
  by state with their node hours, and how many have problems. `j` (or **Jobs**) opens the job list.
- **Health**: nodes down, drained, in maintenance or not responding, with the reasons Slurm gives; reservations
  (a maintenance soon makes the cluster *degraded*).
- **Occupancy**: busy and idle nodes, running and pending jobs and why they wait.
- **Your quotas**: space and files on each filesystem, with a bar (all of them: **Quota**, `Q`).
- **Partitions** and the **node topology** of each: CPUs, sockets × cores × threads, NUMA domains, GPUs, memory.
- **The site**, when a [rule pack](checks.md) knows it: its status page and documentation (a click opens them), and
  how long scratch keeps unused files.

Each part loads in the background when the overview is shown, and again after 5 minutes; `r` (**Reload**) asks at
once. A tab without an SSH session shows **Connect** instead, a tab without Slurm says so.

To open tabs on the job list instead: **Options** (`F3`), *A tab opens on*: its job list (`"start_page": "jobs"` in
the [settings](settings.md)).
