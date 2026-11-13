#!/bin/bash
# Memory of a job on one of its nodes: run by slurp on every node of the job, as a step of the job
# (srun --overlap --input=all ... bash -s -- <job id>, the script on stdin; it is not copied to the cluster).
# Prints KEY=value lines: CPU_KIB (RSS of the job's processes), JOB_PIDS, and the GPU memory of the
# processes and of every GPU of the node (nvidia-smi, or rocm-smi on AMD nodes).
job=$1
echo "== $(hostname)"

# the processes of the job on this node, without this step
pids=$(scontrol listpids "$job" 2>/dev/null |
       awk -v self="$SLURM_STEP_ID" 'NR > 1 && $3 != self { printf "%s%s", sep, $1; sep = "," }')
if [ -n "$pids" ]; then
  cpu_kib=$(ps -o rss= -p "$pids" 2>/dev/null | awk '{ sum += $1 } END { print sum + 0 }')
  echo "CPU_KIB=$cpu_kib"
  echo "JOB_PIDS=$pids"
else
  echo "CPU_UNAVAILABLE=no job process IDs found"
  echo "JOB_PIDS="
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  # NVIDIA: memory of the processes (MiB), and used/total of each GPU of the node
  if gpu_output=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null); then
    printf '%s\n' "$gpu_output" | sed 's/^/GPU=/'
    nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | sed 's/^/GPUDEV=/'
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
