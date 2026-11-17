#!/bin/bash
# Memory of a job on one of its nodes: run by slurp on every node of the job, as a step of the job
# (srun --overlap --input=all ... bash -s -- <job id>, the script on stdin; it is not copied to the cluster).
# Prints KEY=value lines (memory in KiB, GPU memory in MiB as nvidia-smi gives it):
#   CGROUP_KIB=used,anon,file    the job's cgroup on the node: what Slurm counts against --mem and what the
#                                out-of-memory killer acts on (anon: the processes' own memory, file: page cache)
#   CPU_PSS_KIB, CPU_KIB         the job's processes: PSS (shared memory counted once) and RSS (counted per process)
#   NODE_KIB=total,available,free,buffcache   the node, as free computes it (used = total - available)
#   JOB_PIDS, GPU=pid,uuid,MiB of the processes on GPUs, GPUDEV=index,name,used,total MiB,uuid of each GPU
#   (nvidia-smi), or AMDPROC / AMDDEV (rocm-smi on AMD nodes)
job=$1
echo "== $(hostname)"

# the node, as free: MemAvailable, and buff/cache = Buffers + Cached + SReclaimable
awk '/^MemTotal:/ { t = $2 } /^MemAvailable:/ { a = $2 } /^MemFree:/ { f = $2 }
     /^Buffers:/ { c += $2 } /^Cached:/ { c += $2 } /^SReclaimable:/ { c += $2 }
     END { if (t) printf "NODE_KIB=%d,%d,%d,%d\n", t, a, f, c }' /proc/meminfo 2>/dev/null

# the job's cgroup: this step is in it (cgroup v2: .../job_<id>/step_<n>/...; v1: the memory controller's path)
# (SLURP_PROC_CGROUP, SLURP_CGROUP_ROOT: only for slurp's tests)
self_cg=${SLURP_PROC_CGROUP:-/proc/self/cgroup}
root=${SLURP_CGROUP_ROOT:-/sys/fs/cgroup}
cg=$(awk -F: '$1 == "0" && $2 == "" { print $3 }' "$self_cg" 2>/dev/null)
dir=""
if [ -n "$cg" ] && [ -f "$root/cgroup.controllers" ]; then
  case "$cg" in */job_"$job"/*|*/job_"$job") dir=$root${cg%%/job_"$job"/*}/job_$job ;; esac
  if [ -n "$dir" ] && [ -r "$dir/memory.current" ]; then
    used=$(cat "$dir/memory.current")
    stat=$(awk '$1 == "anon" { a = $2 } $1 == "file" { f = $2 } END { printf "%d,%d", a / 1024, f / 1024 }' "$dir/memory.stat" 2>/dev/null)
    echo "CGROUP_KIB=$((used / 1024)),$stat"
  fi
else
  cg=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ { print $3 }' "$self_cg" 2>/dev/null)
  case "$cg" in */job_"$job"/*|*/job_"$job") dir=$root/memory${cg%%/job_"$job"/*}/job_$job ;; esac
  if [ -n "$dir" ] && [ -r "$dir/memory.usage_in_bytes" ]; then
    used=$(cat "$dir/memory.usage_in_bytes")
    stat=$(awk '$1 == "total_rss" { a = $2 } $1 == "total_cache" { f = $2 } END { printf "%d,%d", a / 1024, f / 1024 }' "$dir/memory.stat" 2>/dev/null)
    echo "CGROUP_KIB=$((used / 1024)),$stat"
  fi
fi

# the processes of the job on this node, without this step
pids=$(scontrol listpids "$job" 2>/dev/null |
       awk -v self="$SLURM_STEP_ID" 'NR > 1 && $3 != self { printf "%s%s", sep, $1; sep = "," }')
if [ -n "$pids" ]; then
  cpu_kib=$(ps -o rss= -p "$pids" 2>/dev/null | awk '{ sum += $1 } END { print sum + 0 }')
  echo "CPU_KIB=$cpu_kib"
  pss=0; found=0
  for pid in ${pids//,/ }; do
    v=$(awk '/^Pss:/ { print $2; exit }' "/proc/$pid/smaps_rollup" 2>/dev/null) && [ -n "$v" ] && { pss=$((pss + v)); found=1; }
  done
  [ "$found" = 1 ] && echo "CPU_PSS_KIB=$pss"
  echo "JOB_PIDS=$pids"
else
  echo "CPU_UNAVAILABLE=no job process IDs found"
  echo "JOB_PIDS="
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  # NVIDIA: the processes on the GPUs (MiB; [N/A] where the driver does not tell), and used/total of each GPU,
  # the numbers of nvidia-smi's own table
  if gpu_output=$(nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits 2>/dev/null); then
    printf '%s\n' "$gpu_output" | sed '/^$/d; s/^/GPU=/'
    nvidia-smi --query-gpu=index,name,memory.used,memory.total,uuid --format=csv,noheader,nounits 2>/dev/null | sed 's/^/GPUDEV=/'
  else
    echo "GPU_UNAVAILABLE=nvidia-smi query failed"
  fi
elif command -v rocm-smi >/dev/null 2>&1; then
  # AMD: the processes with their VRAM (bytes), and the VRAM of each GPU
  rocm-smi --showpids --csv 2>/dev/null | sed 's/^/AMDPROC=/'
  rocm-smi --showproductname --showmeminfo vram --csv 2>/dev/null | sed 's/^/AMDDEV=/'
else
  echo "GPU_UNAVAILABLE=no nvidia-smi or rocm-smi on the node"
fi
