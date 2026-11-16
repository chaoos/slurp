# Where the tasks and threads of a job run on this node (slurp: Placement). Run as a step of the job on every node.
# $1: the job id. Prints KEY=value lines.
jid=$1
echo "HOST=$(hostname)"
# CPU -> core, NUMA domain
lscpu -p=CPU,CORE,NODE 2>/dev/null | grep -v '^#' | sed 's/^/CPU=/'
# GPUs and their NUMA domain, and the processes using them
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader 2>/dev/null | while IFS=, read -r i bus; do
    bus=$(echo "$bus" | tr -d ' ' | tr 'A-F' 'a-f')
    short=$(echo "$bus" | sed 's/^0000\([0-9a-f]\{4\}:\)/\1/')
    n=$(cat "/sys/bus/pci/devices/$short/numa_node" 2>/dev/null || cat "/sys/bus/pci/devices/$bus/numa_node" 2>/dev/null)
    echo "GPU=$i,$bus,${n:--1}"
  done
  nvidia-smi --query-compute-apps=pid,gpu_bus_id --format=csv,noheader 2>/dev/null | tr -d ' ' | tr 'A-F' 'a-f' | sed 's/^/GPUPROC=/'
fi
# the processes of the job (not of this step), with their binding and where their threads are now
scontrol listpids "$jid" 2>/dev/null | awk 'NR > 1 {print $1, $3, $4}' | while read -r pid step localid; do
  case "$step" in extern|batch|"${SLURM_STEP_ID:-x}") continue ;; esac
  [ -r "/proc/$pid/status" ] || continue
  comm=$(cat "/proc/$pid/comm" 2>/dev/null)
  allowed=$(awk '/^Cpus_allowed_list/ {print $2}' "/proc/$pid/status")
  mems=$(awk '/^Mems_allowed_list/ {print $2}' "/proc/$pid/status")
  threads=$(ls "/proc/$pid/task" 2>/dev/null | wc -l)
  running=$(for t in /proc/"$pid"/task/*; do sed 's/.*) //' "$t/stat" 2>/dev/null | awk '{print $37}'; done | sort -n | uniq -c | awk '{printf "%s:%s ", $2, $1}')
  echo "PROC=$step|$localid|$pid|$comm|$allowed|$threads|$running|$mems"
done
