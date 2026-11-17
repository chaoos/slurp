"""Query current memory: the numbers of the job (its cgroup, as Slurm counts it), of its processes (PSS, RSS), of
the nodes (as free) and of the GPUs (as nvidia-smi's table), in binary units as those tools show them"""
import os
import subprocess

from slurp.cluster.details import parse_gpu_probe, parse_memory_probe
from slurp.ui.screens.job import cpu_memory_text, gpu_memory_text, memory_text

PROBE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "slurp", "scripts", "memory_probe.sh")


def test_units_as_free_and_nvidia_smi(check):
  check("81559 MiB (an H100 in nvidia-smi) is 79.6 GiB, not 85.5 GB", memory_text(81559 * 1024**2) == "79.6 GiB", memory_text(81559 * 1024**2))
  check("KiB from /proc/meminfo: GiB as free -h", memory_text(527767304 * 1024) == "503.3 GiB", memory_text(527767304 * 1024))


def test_the_numbers_of_a_node(check):
  out = "\n".join(["== nid001", "NODE_KIB=527767304,401234567,350000000,40000000", "CGROUP_KIB=20971520,16777216,4194304",
                   "CPU_KIB=25165824", "CPU_PSS_KIB=17825792", "JOB_PIDS=100,101",
                   "GPU=100, GPU-aaa, 40000", "GPU=101, GPU-bbb, 1000", "GPU=999, GPU-ccc, 5000",
                   "GPUDEV=0, NVIDIA H100, 40512, 81559, GPU-aaa", "GPUDEV=1, NVIDIA H100, 1512, 81559, GPU-bbb",
                   "GPUDEV=2, NVIDIA H100, 5512, 81559, GPU-ccc"])
  node = parse_memory_probe(out)["nid001"]
  check("cgroup: used, anon, page cache (KiB)", node["cgroup"] == [20971520, 16777216, 4194304])
  check("the node as free: total, available, free, buff/cache", node["node"] == [527767304, 401234567, 350000000, 40000000])
  check("PSS and RSS of the processes", node["pss_kib"] == 17825792 and node["cpu_kib"] == 25165824)
  check("GPU: only the job's processes (not pid 999)", node["gpu_bytes"] == 41000 * 1024**2 and node["gpu_how"] == "processes", node["gpu_bytes"])
  check("GPU: every device, used and total", [g[2] for g in node["gpus"]] == [40512 * 1024**2, 1512 * 1024**2, 5512 * 1024**2]
        and node["gpus"][0][:2] == ("0", "NVIDIA H100"))
  result = {"cpu_bytes": 20971520 * 1024, "cpu_source": "cgroup", "cpu_partial": False, "cpu_cache": 4194304 * 1024,
            "cpu_pss": 17825792 * 1024, "cpu_rss": 25165824 * 1024,
            "nodes": [("nid001", *(v * 1024 for v in node["node"]))], "gpu_bytes": node["gpu_bytes"], "gpu_how": "processes",
            "gpu_partial": False, "gpu_errors": [], "gpus": [("nid001", *g) for g in node["gpus"]]}
  cpu = cpu_memory_text(result)
  check("CPU: the cgroup first, said what it is, with its page cache; the processes; the node as free",
        cpu.startswith("20.0 GiB (the job's cgroup: what Slurm counts against --mem; 4.0 GiB of it page cache)")
        and "the processes: 17.0 GiB PSS, 24.0 GiB RSS" in cpu
        and "nid001 (as free): used 120.7 GiB of 503.3 GiB" in cpu, cpu)
  gpu = gpu_memory_text(result)
  check("GPU: the job's processes, and the devices with nvidia-smi's numbers in MiB-based units",
        gpu.startswith("40.0 GiB (processes of the job)") and "nid001 GPU 0 NVIDIA H100: 39.6 GiB / 79.6 GiB" in gpu, gpu)


def test_gpu_memory_the_driver_does_not_report_per_process(check):
  lines = ["GPU=100, GPU-aaa, [N/A]", "GPU=101, GPU-bbb, [N/A]",
           "GPUDEV=0, NVIDIA GH200, 30000, 97871, GPU-aaa", "GPUDEV=1, NVIDIA GH200, 20000, 97871, GPU-bbb",
           "GPUDEV=2, NVIDIA GH200, 7000, 97871, GPU-ccc"]
  proc, devices, how = parse_gpu_probe(lines, {"100", "101"})
  check("[N/A] per process: the used memory of the GPUs the job's processes are on (not 0)", proc == 50000 * 1024**2 and how == "its GPUs", (proc, how))
  check("…and the old format (pid, MiB) still reads", parse_gpu_probe(["GPU=100, 1234"], {"100"})[0] == 1234 * 1024**2)


def test_the_probe_script(check, tmp_path):
  bin_ = tmp_path / "bin"; bin_.mkdir()
  (bin_ / "scontrol").write_text(f"#!/bin/sh\necho 'PID JOBID STEPID LOCALID GLOBALID'\necho '{os.getpid()} 42 0 0 0'\n") # (this test's process)
  (bin_ / "scontrol").chmod(0o755)
  env = {"PATH": f"{bin_}:/usr/bin:/bin", "SLURM_STEP_ID": "7"}
  out = subprocess.run(["bash", "-s", "--", "42"], stdin=open(PROBE), capture_output=True, text=True, env=env).stdout
  lines = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
  meminfo = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
  total, available = (int(v) for v in lines["NODE_KIB"].split(",")[:2])
  check("the node: MemTotal and MemAvailable of /proc/meminfo (free's total and available)",
        total == meminfo["MemTotal"] and abs(available - meminfo["MemAvailable"]) < 200000, lines.get("NODE_KIB"))
  check("the processes: RSS and PSS", int(lines["CPU_KIB"]) > 0 and int(lines.get("CPU_PSS_KIB", "0")) > 0, out)
  check("not in a job's cgroup here: none said", "CGROUP_KIB" not in lines)


def test_the_jobs_cgroup(check, tmp_path):
  """cgroup v2 (.../job_<id>/step_<n>/user/task_0) and v1 (the memory controller), as Slurm's cgroup plugin makes them"""
  def probe(proc_cgroup, root):
    (tmp_path / "self").write_text(proc_cgroup)
    env = {"PATH": "/usr/bin:/bin", "SLURP_PROC_CGROUP": str(tmp_path / "self"), "SLURP_CGROUP_ROOT": str(root)}
    out = subprocess.run(["bash", "-s", "--", "4242"], stdin=open(PROBE), capture_output=True, text=True, env=env).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l).get("CGROUP_KIB")
  v2 = tmp_path / "v2"; job = v2 / "system.slice" / "slurmstepd.scope" / "job_4242"
  (job / "step_0" / "user").mkdir(parents=True); (v2 / "cgroup.controllers").write_text("memory cpu\n")
  (job / "memory.current").write_text(str(21474836480) + "\n")
  (job / "memory.stat").write_text("anon 17179869184\nfile 4294967296\nkernel 1000\n")
  check("v2: the job's cgroup (not the step's), used, anon, page cache in KiB",
        probe("0::/system.slice/slurmstepd.scope/job_4242/step_extern/user/task_0\n", v2) == "20971520,16777216,4194304")
  check("v2: another job's cgroup is not taken", probe("0::/system.slice/slurmstepd.scope/job_42420/step_0\n", v2) is None)
  v1 = tmp_path / "v1"; job = v1 / "memory" / "slurm" / "uid_1000" / "job_4242"
  job.mkdir(parents=True)
  (job / "memory.usage_in_bytes").write_text("10737418240\n")
  (job / "memory.stat").write_text("cache 99\nrss 98\ntotal_cache 2147483648\ntotal_rss 8589934592\n")
  check("v1: the memory controller's job cgroup, with total_rss and total_cache",
        probe("12:memory:/slurm/uid_1000/job_4242/step_0/task_0\n11:cpu,cpuacct:/slurm/uid_1000/job_4242\n", v1)
        == "10485760,8388608,2097152")


def test_runtime_diagnostics_of_a_node(check):
  from slurp.ui.screens.diagnostics import diagnostics_text
  out = "\n".join(["== nid001", "NODE_KIB=527767304,401234567,350000000,40000000", "CGROUP_KIB=62914560,58720256,4194304",
                   "CPU_KIB=25165824", "CPU_PSS_KIB=17825792", "JOB_PIDS=100,101", "GPU=100, GPU-aaa, 40000",
                   "GPUDEV=0, NVIDIA H100, 40512, 81559, GPU-aaa", "GPUDEV=1, NVIDIA H100, 512, 81559, GPU-bbb",
                   "NODE_CPU=35,288", "LOADAVG=101.5,99.0,80.2,104/2300", "JOB_CPU=20.50,72", "JOB_IO=13107200,1048576",
                   "CGROUP_LIMIT_KIB=67108864", "MEM_EVENTS=2,40,0", "SWAP_KIB=0,0",
                   "DISK=/tmp,419430400,3221225,416209175", "DISK=/dev/shm,263883652,204800,263678852",
                   "PROC=100|1980.5|20971520|64|R|python|python train.py --epochs 10", "PROC=101|0.0|10240|1|D|tar|tar xf data.tar",
                   "GPULOAD=0, 97, 45, 64, 420.50, 700.00, 1980", "GPULOAD=1, 0, 0, 30, 70.00, 700.00, 345"])
  from slurp.cluster.details import parse_memory_probe
  d = parse_memory_probe(out)["nid001"]
  check("parsed: the node's and job's CPU, load, I/O, limit, events, disks, processes, GPU load",
        d["node_cpu"] == [35, 288] and d["job_cpu"] == (20.5, 72) and d["job_io"] == [13107200, 1048576]
        and d["limit"] == 67108864 and d["events"] == [2, 40, 0] and len(d["disks"]) == 2 and d["procs"][0]["cpu"] == 1980.5
        and d["gpu_load"][0]["util"] == 97 and d["gpu_load"][0]["power_limit"] == 700, d)
  text = diagnostics_text({"nodes": {"nid001": d}})
  plain = text.plain
  check("memory: the job of its limit, the out-of-memory kills", "job (its cgroup, as Slurm counts it): 60.0 GiB of 64.0 GiB limit (94%)" in plain
        and "out-of-memory kills in the job: 2" in plain, plain[:400])
  check("CPU: the job's CPUs busy (most idle: said), the node and its load", "job: 20.5 of its 72 CPUs busy (28%) · most of its CPUs are idle" in plain
        and "node: 35% of 288 CPUs busy · load 101.5 / 99.0 / 80.2" in plain, plain)
  check("GPUs: memory, busy, bandwidth, temperature, power, clock; an idle one said", "GPU 0 NVIDIA H100: 39.6 GiB / 79.6 GiB · busy 97% · memory bandwidth 45% · 64 °C · 420 / 700 W · 1980 MHz" in plain
        and "GPU 1 NVIDIA H100: 512.0 MiB / 79.6 GiB · busy 0%" in plain and "hardly used" in plain, plain)
  check("I/O, disks (/dev/shm counts as memory), the busiest processes; D: waits for I/O", "read 12.5 MiB/s, written 1.0 MiB/s" in plain
        and "/dev/shm: 200.0 MiB of 251.7 GiB used (counts as memory)" in plain and "python train.py --epochs 10" in plain
        and "(waits for I/O)" in plain, plain)
  styles = {str(span.style) for span in text.spans}
  check("what is wrong stands out (red, yellow)", "bold red" in styles and "yellow" in styles and "bold yellow" in styles, styles)


def test_the_probe_script_full(check, tmp_path):
  bin_ = tmp_path / "bin"; bin_.mkdir()
  (bin_ / "scontrol").write_text(f"#!/bin/sh\necho 'PID JOBID STEPID LOCALID GLOBALID'\necho '{os.getpid()} 42 0 0 0'\n")
  (bin_ / "scontrol").chmod(0o755)
  env = {"PATH": f"{bin_}:/usr/bin:/bin", "SLURM_STEP_ID": "7"}
  out = subprocess.run(["bash", "-s", "--", "42", "full"], stdin=open(PROBE), capture_output=True, text=True, env=env, timeout=30).stdout
  keys = {l.split("=", 1)[0] for l in out.splitlines() if "=" in l}
  check("full: the node's CPU and load, swap, the local disks, the processes (measured over a second)",
        {"NODE_CPU", "LOADAVG", "SWAP_KIB", "DISK", "PROC"} <= keys, sorted(keys))
  quick = subprocess.run(["bash", "-s", "--", "42"], stdin=open(PROBE), capture_output=True, text=True, env=env, timeout=30).stdout
  check("without full (the memory watch): only the memory, at once", "NODE_CPU" not in quick and "PROC=" not in quick and "NODE_KIB" in quick)
