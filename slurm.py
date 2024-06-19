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
  common_keys = type(*map(set, LD))
  DL = {k: [dct.get(k, None) for dct in LD] for k in common_keys}
  return DL, common_keys

def suffix_to_int(str):
  if str.endswith("K"):
    return int(str[:-1])*1000
  elif str.endswith("M"):
    return int(str[:-1])*1000*1000
  elif str.endswith("G"):
    return int(str[:-1])*1000*1000*1000
  else:
    return str

seff_keys = ["Array Job ID", "State", "Job Wall-clock time", "Memory Efficiency", "Nodes"]
last = lambda x: x[-1]
frst = lambda x: x[0]
s2int = lambda x: humanize.naturalsize(suffix_to_int(x)) if isinstance(suffix_to_int(x), int) else x
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
    res = subprocess.run(["seff", str(jid)], **env)
    for line in res.stdout.split("\n"):
      if (m := re.match(r'^(.+): (.+)', line)):
        if m.group(1) in seff_keys:
          dct[m.group(1)] = m.group(2)
    jname = subprocess.check_output(["sacct", "-j", str(jid), r'--format=JobName%100', "-P", "-n"]).decode('utf8')
    dct["Job name"] = jname.partition('\n')[0]

  except:
    evaluate = lambda x: x[0](x[1])
    dct["Array Job ID"] = str(jid)
    res = subprocess.check_output(["sacct", "-j", str(jid), rf'--format={",".join(sacct["format"])}', "-P", "-n"]).decode('utf8')
    list_of_lines = list(map(list, itertools.zip_longest(*[l.split("|") for l in res.rstrip().split('\n')], fillvalue=None)))
    values = map(evaluate, zip(sacct["post"], map(evaluate, zip(sacct["aggr"], list_of_lines))))
    #dct = dct | dict(zip(sacct["display"], res.partition('\n')[0].split("|")))
    dct = dct | dict(zip(sacct["display"], values))
    pass

  return dct

def to_nodehours(job):
  walltime = job.get("Job Wall-clock time", "00:00:00")
  try:
    t = datetime.strptime(walltime, "%H:%M:%S")
    day = 0
  except:
    t = datetime.strptime(walltime, "%d-%H:%M:%S")
    day = t.day
  return int(job.get("Nodes", 1))*(24*day + t.hour + t.minute/60 + t.second/3600)

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
    print(f"Total resources in the list: {sum(map(to_nodehours, jobs)):.1f} nodehours")

def tsplit(str, types, delimiter="|"):
  return [types[i](v) for i, v in enumerate(str.split(delimiter))]

squeue_fields = {
  "keys":    ["Job ID", "Job name", "Tasks per node", "# Nodes", "State",   "Elapsed / Total (Start)", "Comment"],
  "fmt_str": ["%i",     "%j",       "%c",             "%D",      "%T (%r)", "%M / %l (%S)",            "%k"],
  "types":   [str,      str,        int,              int,       str,       str,                       str],
}
def get_queue():
  env = { "shell": True, "capture_output": True, "text": True }
  res = subprocess.run([fr'squeue --me -r -h --format="{"|".join(squeue_fields["fmt_str"])}"'], **env)
  items = res.stdout.split("\n")
  vals = lambda x: tsplit(x, squeue_fields["types"])
  return {item.split("|")[0]: dict(zip(squeue_fields["keys"], vals(item))) for item in items}

def get_mem_usages(jobs):
  env = { "shell": True, "capture_output": True, "text": True }
  keys = list(set(map(lambda x: x.split("_")[0], jobs.keys()))) # TODO remove
  for jid in tqdm(keys):
    res = subprocess.run([rf'sstat -n --format="JobID,MaxRSS" -j {jid} --parsable2 -n'], **env)
    for item in res.stdout.split():
      jid, mem = tsplit(item, [lambda x: x[:-2], suffix_to_int])
      for j_id in jobs.keys():
        if (j_id.startswith(f"{jid}_") or j_id == jid):
          task = humanize.naturalsize(mem)
          node = humanize.naturalsize(mem*jobs[j_id]["Tasks per node"])
          total  = humanize.naturalsize(mem*jobs[j_id]["Tasks per node"]*jobs[j_id]["# Nodes"])
          jobs[j_id]["Memory usage (task, node, total)"] = f"{task}, {node}, {total}"
  return jobs

def slurm_running(args):
  jobs = get_queue()
  jobs = get_mem_usages(jobs).values() if args.mem else jobs.values()
  if jobs:
    DL, headers = LDtoDL(jobs, sort_by_key="!Job ID")
    print(tabulate(colorize_dict_vals(DL), headers=headers, showindex="always"))

def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('-i', '--infile', required=False, nargs = '+',
    help='Path to setup file(s) or directories to scan for slurm output files.')
  parser.add_argument('--mem', help="Obtain peak memory for running jobs", default=False, action="store_true")
  args = parser.parse_args()

  if args.infile:
    slurm_finished(args)
  else:
    slurm_running(args)

if __name__ == '__main__':
  main()
