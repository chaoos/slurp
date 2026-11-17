#!/bin/bash
# Memory of a job on one of its nodes: run by slurp on every node of the job, as a step of the job
# (srun --overlap --input=all ... bash -s -- <job id>, the script on stdin; it is not copied to the cluster).
# Prints KEY=value lines (memory in KiB, GPU memory in MiB as nvidia-smi gives it):
#   CGROUP_KIB=used,anon,file    the job's cgroup on the node: what Slurm counts against --mem and what the
#                                out-of-memory killer acts on (anon: the processes' own memory, file: page cache)
#   CPU_PSS_KIB, CPU_KIB         the job's processes: PSS (shared memory counted once) and RSS (counted per process)
#   NODE_KIB=total,available,free,buffcache   the node, as free computes it (used = total - available)
# With "full" as the second argument (runtime diagnostics), also, measured over one second:
#   NODE_CPU=busy %,CPUs   LOADAVG=1,5,15 min,running/all   JOB_CPU=cores used,CPUs of the job (its cgroup)
#   JOB_IO=read,written bytes/s   CGROUP_LIMIT_KIB=memory limit ("max": none)   MEM_EVENTS=oom_kill,max,high
#   SWAP_KIB=total,free   DISK=path,size,used,available KiB   GPULOAD=index,util %,memory util %,°C,W,W limit,MHz
#   AMDLOAD=rocm-smi's use, temperature and power   PROC=pid|CPU %|RSS KiB|threads|state|name|command (the busiest)
#   JOB_PIDS, GPU=pid,uuid,MiB of the processes on GPUs, GPUDEV=index,name,used,total MiB,uuid of each GPU
#   (nvidia-smi), or AMDPROC / AMDDEV (rocm-smi on AMD nodes)
job=$1
mode=${2:-memory}
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

[ "$mode" = full ] || exit 0

# ---- runtime diagnostics: what the job and the node do, measured over one second
cpu_ticks() { awk '/^cpu / { t = 0; for (i = 2; i <= NF; i++) t += $i; print t, $5 + $6 }' /proc/stat; }
job_usage() { # the job's CPU time (µs) and bytes read and written so far (cgroup v2; v1: cpuacct)
  if [ -n "$dir" ] && [ -r "$dir/cpu.stat" ]; then
    awk '$1 == "usage_usec" { print $2 }' "$dir/cpu.stat"
  else
    acct=$(awk -F: '$2 ~ /(^|,)cpuacct(,|$)/ { print $3 }' "$self_cg" 2>/dev/null)
    case "$acct" in */job_"$job"/*) f=$root/cpu,cpuacct${acct%%/job_"$job"/*}/job_$job/cpuacct.usage
      [ -r "$f" ] || f=$root/cpuacct${acct%%/job_"$job"/*}/job_$job/cpuacct.usage
      [ -r "$f" ] && echo $(( $(cat "$f") / 1000 )) ;; esac
  fi
}
job_io() { [ -n "$dir" ] && [ -r "$dir/io.stat" ] && awk '{ for (i = 2; i <= NF; i++) { split($i, kv, "="); if (kv[1] == "rbytes") r += kv[2]; if (kv[1] == "wbytes") w += kv[2] } } END { print r + 0, w + 0 }' "$dir/io.stat"; }
proc_ticks() { # pid ticks of the job's processes (utime + stime; the name may have spaces: after the last ")")
  for pid in ${pids//,/ }; do
    [ -r "/proc/$pid/stat" ] && awk -v p="$pid" '{ sub(/.*\) /, ""); print p, $12 + $13 }' "/proc/$pid/stat" 2>/dev/null
  done
}
read -r t0 i0 < <(cpu_ticks); u0=$(job_usage); io0=$(job_io); p0=$(proc_ticks)
sleep 1
read -r t1 i1 < <(cpu_ticks); u1=$(job_usage); io1=$(job_io); p1=$(proc_ticks)
ncpu=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)
[ "$t1" -gt "$t0" ] && echo "NODE_CPU=$(( 100 - 100 * (i1 - i0) / (t1 - t0) )),$ncpu"
read -r l1 l5 l15 run _ < /proc/loadavg && echo "LOADAVG=$l1,$l5,$l15,$run"
cpus=""
if [ -n "$dir" ] && [ -r "$dir/cpuset.cpus.effective" ]; then cpus=$(cat "$dir/cpuset.cpus.effective"); fi
ncpus=$(printf '%s' "$cpus" | awk -F, '{ for (i = 1; i <= NF; i++) { n = split($i, r, "-"); c += (n == 2 ? r[2] - r[1] + 1 : 1) } } END { print c + 0 }')
[ "$ncpus" = 0 ] && ncpus=${SLURM_CPUS_ON_NODE:-}
[ -n "$u0" ] && [ -n "$u1" ] && echo "JOB_CPU=$(awk -v a="$u0" -v b="$u1" 'BEGIN { printf "%.2f", (b - a) / 1e6 }'),$ncpus"
[ -n "$io0" ] && [ -n "$io1" ] && set -- $io0 $io1 && echo "JOB_IO=$(( $3 - $1 )),$(( $4 - $2 ))"
if [ -n "$dir" ] && [ -r "$dir/memory.max" ]; then
  max=$(cat "$dir/memory.max"); [ "$max" = max ] && echo "CGROUP_LIMIT_KIB=max" || echo "CGROUP_LIMIT_KIB=$((max / 1024))"
  awk '$1 == "oom_kill" { k = $2 } $1 == "max" { m = $2 } $1 == "high" { h = $2 } END { printf "MEM_EVENTS=%d,%d,%d\n", k, m, h }' "$dir/memory.events" 2>/dev/null
elif [ -n "$dir" ] && [ -r "$dir/memory.limit_in_bytes" ]; then
  echo "CGROUP_LIMIT_KIB=$(( $(cat "$dir/memory.limit_in_bytes") / 1024 ))"
  awk '$1 == "oom_kill" { k = $2 } END { printf "MEM_EVENTS=%d,%d,0\n", k, '"$(cat "$dir/memory.failcnt" 2>/dev/null || echo 0)"' }' "$dir/memory.oom_control" 2>/dev/null
fi
awk '/^SwapTotal:/ { t = $2 } /^SwapFree:/ { f = $2 } END { printf "SWAP_KIB=%d,%d\n", t, f }' /proc/meminfo
for d in /tmp /dev/shm ${TMPDIR:+"$TMPDIR"}; do
  df -Pk "$d" 2>/dev/null | awk -v d="$d" 'NR == 2 { printf "DISK=%s,%d,%d,%d\n", d, $2, $3, $4 }'
done
# the busiest processes of the job: CPU % over the second, RSS, threads, state, name, command line
if [ -n "$pids" ]; then
  hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
  { printf '%s\n' "$p0" | sed 's/^/A /'; printf '%s\n' "$p1" | sed 's/^/B /'; } |
    awk -v hz="$hz" '$1 == "A" { a[$2] = $3 } $1 == "B" && ($2 in a) { printf "%s %.1f\n", $2, 100 * ($3 - a[$2]) / hz }' |
    sort -k2 -rn | head -12 | while read -r pid cpu; do
      rss=$(awk '/^VmRSS:/ { print $2 }' "/proc/$pid/status" 2>/dev/null)
      thr=$(awk '/^Threads:/ { print $2 }' "/proc/$pid/status" 2>/dev/null)
      st=$(awk '/^State:/ { print $2 }' "/proc/$pid/status" 2>/dev/null)
      name=$(cat "/proc/$pid/comm" 2>/dev/null)
      cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-160)
      echo "PROC=$pid|$cpu|${rss:-0}|${thr:-0}|${st:-?}|$name|$cmd"
    done
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,utilization.gpu,utilization.memory,temperature.gpu,power.draw,power.limit,clocks.sm \
    --format=csv,noheader,nounits 2>/dev/null | sed 's/^/GPULOAD=/'
elif command -v rocm-smi >/dev/null 2>&1; then
  rocm-smi --showuse --showtemp --showpower --csv 2>/dev/null | sed 's/^/AMDLOAD=/'
fi
