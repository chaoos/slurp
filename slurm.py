#! /usr/bin/env python3
"""
Script to obtain slurm job information of finished, running and pending jobs
"""

import argparse
import importlib.util
import itertools
import os
from submit import schema, stringify
from types import SimpleNamespace
import glob as gl
import re
import subprocess
from tabulate import tabulate
from pathlib import Path
from tqdm import tqdm
import humanize
from termcolor import colored
from datetime import datetime
from dateutil.relativedelta import relativedelta
import helpers as hp
import getpass
from collections import Counter, OrderedDict
from functools import reduce
from textwrap import shorten
import grp

logger = hp.get_logger()

def sort_LD(LD, key):
  actual_key = key.replace("!", "")
  LD = sorted(LD, key=lambda d: d.get(actual_key, "N/A"), reverse=key.startswith("!"))
  for d in LD:
    order = "↓" if key.startswith("!") else "↑"
    d[f"{actual_key} {order}"] = d.get(actual_key, "N/A")
    d.pop(actual_key, None)
  return LD

colors = {
  "RUNNING":       lambda x: colored(x, 'light_green'),
  "COMPLETING":    lambda x: colored(x, 'light_green'),
  "CONFIGURING":   lambda x: colored(x, 'light_green'),
  "COMPLETED":     lambda x: colored(x, 'light_green'),
  "PENDING":       lambda x: colored(x, 'light_blue'),
  "FAILED":        lambda x: colored(x, 'light_red'),
  "OUT_OF_MEMORY": lambda x: colored(x, 'light_red'),
  "CANCELLED":     lambda x: colored(x, 'light_red'),
  "TIMEOUT":       lambda x: colored(x, 'light_red'),
}

def choose(val, colors=colors):
  for k, v in colors.items():
    try:
      if k in val:
        return v(val)
    except:
      pass
  return val

def colorize_dict_vals(dct):
  for key, val, in dct.items():
    dct[key] = map(choose, iter(val))
  return dct

# list of dicts --> dict of lists
def LDtoDL(LD, type=set.union, sort_by_key=None):
  if sort_by_key is not None:
    LD = sort_LD(LD, sort_by_key)
  common_keys = sorted(type(*map(set, LD)))
  DL = {k: [dct.get(k, None) for dct in LD] for k in common_keys}
  return DL, common_keys

def suffix_to_int(str, default=0):
  if str.endswith("K"):
    return int(float(str[:-1]))*1000
  elif str.endswith("M"):
    return int(float(str[:-1]))*1000*1000
  elif str.endswith("G"):
    return int(float(str[:-1]))*1000*1000*1000
  else:
    return default

seff_keys = ["Array Job ID", "State", "Job Wall-clock time", "Memory Efficiency", "Nodes"]
last = lambda x: x[-1]
frst = lambda x: x[0]
s2int = lambda x: humanize.naturalsize(suffix_to_int(x))
noop = lambda x: x
sacct = {
  "display": ["Job name",    "State", "Job Wall-clock time", "Memory/task", "Nodes"],
  "format":  ["JobName%100", "State", "Elapsed",             "MaxRSS",      "NNodes"],
  "aggr":    [frst,          last,    frst,                  last,          last],
  "post":    [noop,          noop,    noop,                  s2int,         noop],
}
def get_job_info(jid):
  dct = {}
  try:
    env = { "stdout": subprocess.PIPE, "text": True }
    cmd = ["seff", str(jid)]
    logger.debug(rf'running command: {" ".join(cmd)}')
    res = subprocess.run(cmd, **env)
    for line in res.stdout.split("\n"):
      if (m := re.match(r'^(.+): (.+)', line)):
        if m.group(1) in seff_keys:
          dct[m.group(1)] = m.group(2)
    cmd = ["sacct", "-j", str(jid), r'--format=JobName%100', "-P", "-n"]
    logger.debug(rf'running command: {" ".join(cmd)}')
    jname = subprocess.check_output(cmd).decode('utf8')
    dct["Job name"] = jname.partition('\n')[0]

  except:
    evaluate = lambda x: x[0](x[1])
    dct["Array Job ID"] = str(jid)
    cmd = ["sacct", "-j", str(jid), rf'--format={",".join(sacct["format"])}', "-P", "-n"]
    logger.debug(rf'running command: {" ".join(cmd)}')
    res = subprocess.check_output(cmd).decode('utf8')
    list_of_lines = list(map(list, itertools.zip_longest(*[l.split("|") for l in res.rstrip().split('\n')], fillvalue=None)))
    values = map(evaluate, zip(sacct["post"], map(evaluate, zip(sacct["aggr"], list_of_lines))))
    dct = dct | dict(zip(sacct["display"], values))
    pass

  return dct

def hours(walltime):
  seconds = 0
  for part in walltime.split(':'):
    days, part = part.split('-') if '-' in part else ("0", part)
    seconds = seconds*60 + int(part, 10) + int(days, 10)*24
  return seconds/3600

def to_nodehours(job):
  return int(job.get("Nodes", 1))*hours(job.get("Job Wall-clock time", "00:00:00"))

def slurm_finished(args):
  file_list = []
  for infile in args.infile:
    if infile.endswith(".py"):
      spec = importlib.util.spec_from_file_location("infile", infile)
      mod = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(mod)
      setup = schema.validate(mod.setup)
      dirs = [os.path.abspath(f"{setup.work_dir}/{run.run_name}") for run in setup.runs]
      file_list.extend([f for f in itertools.chain.from_iterable([gl.glob(f'{dir}/*/slurm-*.out') for dir in dirs])])
    else:
      file_list.extend([f.name for f in Path(infile).glob('**/slurm-*.out') if f.is_file()])

  jobs = []
  jids = []
  for file in tqdm(file_list):
    if (m := re.match(r'.*slurm-([0-9,\_]+)\.*', file)):
      jid = m.group(1)
      if not (jid in jids):
        jids.append(jid)
        jobs.append(get_job_info(jid))

  if jobs:
    DL, headers = LDtoDL(jobs, type=set.union, sort_by_key="!Array Job ID")
    print(tabulate(colorize_dict_vals(DL), headers=headers, showindex="always"))
    logger.info(f"Total resources in the list: {sum(map(to_nodehours, jobs)):.1f} nodehours")

def tsplit(str, types, delimiter="|"):
  return [types[i](v) for i, v in enumerate(str.split(delimiter))]

squeue_fields = {
  "keys":    ["Job ID", "Job name", "Tasks per node", "# Nodes", "State",   "Elapsed / Total (Start)", "Comment"],
  "fmt_str": ["%i",     "%j",       "%c",             "%D",      "%T (%r)", "%M / %l (%S)",            "%k"],
  "types":   [str,      str,        int,              int,       str,       str,                       str],
}
def accounts():
  return [grp.getgrgid(g).gr_name for g in os.getgroups()]

def get_queue(us=False):
  who = f"-A {','.join(accounts())}" if us else "--me"
  env = { "shell": True, "capture_output": True, "text": True }
  cmd = [fr'squeue {who} -r -h --format="{"|".join(squeue_fields["fmt_str"])}"']
  logger.debug(rf'running command: {" ".join(cmd)}')
  res = subprocess.run(cmd, **env)
  items = res.stdout.split("\n")
  vals = lambda x: tsplit(x, squeue_fields["types"])
  non_empty = lambda x: True
  return {item.split("|")[0]: dict(zip(squeue_fields["keys"], vals(item))) for item in filter(non_empty, items)}

def get_mem_usages(jobs):
  env = { "shell": True, "capture_output": True, "text": True }
  keys = list(set(map(lambda x: x.split("_")[0], jobs.keys()))) # TODO remove
  for jid in tqdm(keys):
    cmd = [rf'sstat --format="JobID,MaxRSS" -j {jid} --parsable2 -n']
    logger.debug(rf'running command: {" ".join(cmd)}')
    res = subprocess.run(cmd, **env)
    for item in res.stdout.split():
      jid, mem = tsplit(item, [lambda x: x[:-2], suffix_to_int])
      for j_id in jobs.keys():
        if (j_id.startswith(f"{jid}_") or j_id == jid):
          jobs[j_id]["CPU Memory usage (task, node, total)"] = mem_usage(mem, jobs[j_id])
  return jobs

def mem_usage(mem, job):
  task = humanize.naturalsize(mem)
  node = humanize.naturalsize(mem*job["Tasks per node"])
  total  = humanize.naturalsize(mem*job["Tasks per node"]*job["# Nodes"])
  return f"{task}, {node}, {total}"

duration_units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks", "mo": "months", "y": "years"}
duration_re = r'(\d+)(mo|[smhdwy])'
def parse_duration(string):
  """Parse a duration like 30m, 2h, 1d, 3w, 2mo, 1y or 1d12h and return the point in time that far back"""
  string = string.replace(" ", "")
  if not re.fullmatch(rf'(?:{duration_re})+', string):
    raise argparse.ArgumentTypeError(f"invalid duration '{string}' (expected e.g. 30s, 30m, 2h, 1d, 3w, 2mo, 1y or 1d12h)")
  delta = sum((relativedelta(**{duration_units[u]: int(n)}) for n, u in re.findall(duration_re, string)), relativedelta())
  return datetime.now().replace(microsecond=0) - delta

# states of jobs that are still in the queue (and therefore listed by squeue)
active_states = {"RUNNING", "PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "SUSPENDED", "RESIZING",
                 "CONFIGURING", "COMPLETING", "SIGNALING", "STAGE_OUT", "STOPPED", "RESV_DEL_HOLD"}
sacct_fields = ["JobID", "JobName%100", "NTasks", "NNodes", "State", "ExitCode", "Elapsed", "Timelimit", "Start", "End", "Comment", "MaxRSS", "AveRSS"]
def sacct_bytes(string):
  """sacct sizes like '22163328K', '21511.50M' or plain bytes '1726579370'"""
  m = re.fullmatch(r'([\d.]+)([KMGTP]?)', string)
  return int(float(m.group(1)) * 1024**" KMGTP".index(m.group(2) or " ")) if m else 0

def get_completed(since, us=False, mem=False):
  who = ["-a", "-A", ",".join(accounts())] if us else ["-u", getpass.getuser()]
  cmd = ["sacct", *who, "-S", since.isoformat(), "-E", "now", "-P", "-n", f"--format={','.join(sacct_fields)}"]
  logger.debug(rf'running command: {" ".join(cmd)}')
  # force ISO timestamps, a user-set SLURM_TIME_FORMAT (e.g. "relative") would print 'Ystday 18:55'
  env = os.environ | {"SLURM_TIME_FORMAT": "standard"}
  res = subprocess.run(cmd, capture_output=True, text=True, env=env)
  if res.returncode != 0:
    logger.warning(f"sacct failed: {res.stderr.strip()}")
    return {}

  jobs, steps = {}, {}
  for line in res.stdout.splitlines():
    fields = line.split("|")
    if len(fields) != len(sacct_fields):
      continue
    jid, name, ntasks, nodes, state, exitcode, elapsed, limit, start, end, comment, maxrss, avgrss = fields
    nodes = int(nodes or 0)
    if "." in jid: # job steps (123.batch, 123.0, ...) belong to job 123
      ntasks = int(ntasks or 1)
      tpn = -(-ntasks // max(nodes, 1))
      # MaxRSS is the peak of the most hungry task, AveRSS the mean over the tasks of the step
      total = sacct_bytes(avgrss)*ntasks
      st = steps.setdefault(jid.split(".")[0], {"tpn": 0, "task": 0, "node": 0, "total": 0})
      for key, val in zip(st, [tpn, sacct_bytes(maxrss), total // max(nodes, 1), total]):
        st[key] = max(st[key], val)
      continue
    if state.split(" ")[0] in active_states or end in ("Unknown", "None", ""):
      continue
    if datetime.fromisoformat(end) < since:
      continue
    jobs[jid] = {
      "Job ID":                  jid,
      "Job name":                name,
      "# Nodes":                 nodes,
      "State":                   f"{state} ({exitcode})",
      "Elapsed / Total (Start)": f"{elapsed} / {limit} ({start})",
      "End":                     end,
      "Comment":                 comment,
    }

  for jid, job in jobs.items():
    if (st := steps.get(jid)):
      job["Tasks per node"] = st["tpn"]
      if mem and st["task"]:
        job["CPU Memory usage (task, node, total)"] = ", ".join(humanize.naturalsize(st[k]) for k in ["task", "node", "total"])
  return jobs

def sacct_rows(jids, fields):
  """sacct records (jobs and steps) of the given jobs as dicts; the last field may contain '|'"""
  if not jids:
    return []
  cmd = ["sacct", "-j", ",".join(jids), "-P", "-n", f"--format={','.join(fields)}"]
  logger.debug(rf'running command: {" ".join(cmd)}')
  res = subprocess.run(cmd, capture_output=True, text=True, env=os.environ | {"SLURM_TIME_FORMAT": "standard"})
  if res.returncode != 0:
    logger.warning(f"sacct failed: {res.stderr.strip()}")
  return [dict(zip(fields, line.split("|", len(fields)-1))) for line in res.stdout.splitlines()]

def get_submit_lines(jids):
  """SubmitLine of the jobs (sbatch/salloc/srun ...) and of their srun steps, ordered by step number"""
  info = {}
  for row in sacct_rows(jids, ["JobID", "SubmitLine"]):
    jid, submit = row["JobID"], row.get("SubmitLine", "")
    base, dot, step = jid.partition(".")
    entry = info.setdefault(base, {"submit": "", "steps": []})
    if not dot:
      entry["submit"] = submit
    elif step.isdigit() and submit: # skip batch, extern and interactive steps
      entry["steps"].append((int(step), submit))
  return info

def batch_script_sruns(jid, cache):
  """srun lines in the batch script of a job that is still known to the controller (variables not expanded)"""
  base = jid.split("_")[0] # all tasks of an array job share the script
  if base not in cache:
    cmd = ["scontrol", "write", "batch_script", jid, "-"]
    logger.debug(rf'running command: {" ".join(cmd)}')
    res = subprocess.run(cmd, capture_output=True, text=True)
    cache[base] = srun_lines(res.stdout) if res.returncode == 0 else []
  return cache[base]

def srun_lines(script):
  """Lines of a (batch) script that call srun, without comments"""
  return [l.strip() for l in script.splitlines() if not l.lstrip().startswith("#") and re.search(r'(?:^|[\s;&|(`])srun(?:\s|$)', l)]

def add_commands(jobs, queued):
  """Add the srun command of each job: of its latest step, the job itself if submitted by srun,
  the srun lines of the batch script of queued jobs or the sbatch/salloc line as last resort"""
  info = get_submit_lines([jid for jid in jobs if jid])
  cache = {}
  for jid, job in jobs.items():
    entry = info.get(jid, {"submit": "", "steps": []})
    cmds = [submit for _, submit in sorted(entry["steps"])]
    if not cmds and entry["submit"].startswith("srun"):
      cmds = [entry["submit"]]
    if not cmds and jid and jid in queued:
      cmds = batch_script_sruns(jid, cache)
    cmds = list(dict.fromkeys(cmds)) # unique, keep order
    if cmds:
      job["Command"] = cmds[-1] + (f"  [+{len(cmds)-1} more]" if len(cmds) > 1 else "")
    else:
      job["Command"] = entry["submit"]
  return jobs

def first_node(nodelist):
  """nid[001234-001240,001300] -> nid001234"""
  m = re.match(r'([^\[,\s]+)(?:\[(\d+))?', nodelist)
  return (m.group(1) + (m.group(2) or "")) if m and nodelist not in ("None assigned", "") else ""

def expand_filename(pattern, job):
  """Expand the filename pattern of sbatch --output/--error (see man sbatch) for a job record of sacct"""
  array_id, _, task = job["JobID"].partition("_")
  raw = job["JobIDRaw"]
  vals = {
    "%": "%", "A": array_id if task else raw, "a": task or "4294967294", "b": str(int(task) % 10) if task.isdigit() else "0",
    "j": raw, "J": raw, "u": job["User"], "x": job["JobName"], "N": first_node(job["NodeList"]), "n": "0", "t": "0", "s": "batch",
  }
  def sub(m):
    val = vals[m.group(2)]
    return val.zfill(int(m.group(1))) if m.group(1) and val.isdigit() else val
  path = re.sub(r'%(\d*)([%AabjJuxNnts])', sub, pattern)
  return os.path.join(job["WorkDir"], path)

err_fields = ["JobID", "JobIDRaw", "User", "NodeList", "Start", "WorkDir", "StdOut", "StdErr", "JobName"]
def add_error_flags(jobs):
  """Flag jobs whose slurm error file (sbatch --error) is not empty"""
  rows = {row["JobID"]: row for row in sacct_rows([jid for jid in jobs if jid], err_fields) if "." not in row["JobID"]}
  n = 0
  for jid, job in jobs.items():
    row = rows.get(jid)
    if not row or not (row["StdErr"] or row["StdOut"]): # e.g. salloc: no files
      continue
    if row["Start"] in ("None", "Unknown", ""): # never started, no files written
      job["Err"] = ""
      continue
    err = expand_filename(row["StdErr"], row) if row["StdErr"] else None
    if err is None or err == expand_filename(row["StdOut"], row):
      job["Err"] = "(stdout)" # stderr goes to the output file as well
      continue
    try:
      size = os.path.getsize(err)
    except OSError:
      job["Err"] = "?"
      continue
    if size:
      n += 1
    job["Err"] = colored(f"! {humanize.naturalsize(size)}", "light_red", attrs=["bold"]) if size else ""
  if n:
    logger.info(f"{n} job(s) wrote to their error file")
  return jobs

def completed_summary(jobs, since):
  states = Counter(job["State"].split(" ")[0] for job in jobs.values())
  states = ", ".join(f"{n} {state}" for state, n in states.most_common())
  nh = sum(job["# Nodes"]*hours(job["Elapsed / Total (Start)"].split(" / ")[0]) for job in jobs.values())
  logger.info(f"Finished since {since:%Y-%m-%d %H:%M}: {len(jobs)} jobs ({states or 'none'}), {nh:.1f} nodehours")

def get_full_queue():
  env = { "shell": True, "capture_output": True, "text": True }
  cmd = [fr'squeue -r -h --format="%u|%i|%T|%r|%D|%l" --sort=-p,i']
  logger.debug(rf'running command: {" ".join(cmd)}')
  res = subprocess.run(cmd, **env)
  jid = lambda i: int(re.findall(r'\d+', i)[0])
  items = list(map(lambda x: dict(zip(["user", "jid", "state", "reason", "nodes", "nh"], tsplit(x, [str,jid,str,str,int,hours]))), res.stdout.split("\n")))
  running = list(filter(lambda x: x.get("state")=="RUNNING", items))
  pending = list(filter(lambda x: x.get("state")=="PENDING", items))
  before_me = list(filter(lambda x: x.get("user")==getpass.getuser(), pending))
  y = min(before_me, key=lambda x: x.get('jid'), default={"jid":0}).get("jid")
  before_me = list(filter(lambda x: x.get("jid") < y, pending))
  nodes = list(map(lambda x: x.get("nodes"), before_me))
  nodes = dict(zip(["min","max","sum"], [min(nodes, default=0), max(nodes, default=0), sum(nodes)]))
  nh = int(sum(map(lambda x: x.get("nodes")*x.get("nh"), before_me)))
  reasons = dict(Counter(map(lambda x: shorten(x.get('reason'), width=100), before_me)))
  return len(pending), len(running), len(before_me), reasons, nodes, nh

def slurm_summary():
  p, r, b, reasons, nodes, node_hours = get_full_queue()
  logger.info(f"Total jobs in queue: {p} pending, {r} running, {b} before mine ({reasons = }, {nodes = }, {node_hours = })")

def slurm_running(args):
  jobs = get_queue(args.us)
  if args.mem:
    jobs = get_mem_usages(jobs)
  completed = get_completed(args.completed, args.us, args.mem) if args.completed else {}
  queued = set(jobs)
  jobs = completed | jobs # jobs still in the queue take precedence
  if args.exec:
    jobs = add_commands(jobs, queued)
  if args.error:
    jobs = add_error_flags(jobs)
  jobs = list(jobs.values())
  if jobs:
    DL, headers = LDtoDL(jobs, sort_by_key="!Job ID")
    print(tabulate(colorize_dict_vals(DL), headers=headers, showindex="always"))
  slurm_summary()
  if args.completed:
    completed_summary(completed, args.completed)
  

def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('-i', '--infile', required=False, nargs = '+',
    help='Path to setup file(s) or directories to scan for slurm output files.')
  parser.add_argument('--mem', help="Obtain peak memory for running jobs", default=False, action="store_true")
  parser.add_argument("-v", "--verbosity", action="count", help="increase output verbosity", default=0)
  parser.add_argument('--us', help="Show jobs of all user accounts of current user", default=False, action="store_true")
  parser.add_argument('-c', '--completed', type=parse_duration, metavar="DURATION",
    help="Also list jobs that finished within the last DURATION, e.g. 30s, 30m, 2h, 1d, 3w, 2mo, 1y or combined like 1d12h")
  parser.add_argument('-e', '--exec', default=False, action="store_true",
    help="Show the command the job executes: the srun command(s) of its steps or in its batch script")
  parser.add_argument('--error', default=False, action="store_true",
    help="Flag jobs whose slurm error file (sbatch --error) is not empty with a red '!' and its size")
  args = parser.parse_args()
  logger = hp.setup_logger(args.verbosity)

  if args.infile:
    if args.completed or args.exec or args.error:
      logger.warning("-c/--completed, -e/--exec and --error are ignored together with -i/--infile")
    slurm_finished(args)
  else:
    slurm_running(args)

if __name__ == '__main__':
  main()
