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


def get_job_info(jid, seff="seff", sacct="sacct"):
  dct = {}
  try:
    env = { "stdout": subprocess.PIPE, "text": True }
    res = subprocess.run([seff, str(jid)], **env)
    for line in res.stdout.split("\n"):
      if (m := re.match(r'^(.+): (.+)', line)):
        dct[m.group(1)] = m.group(2)
    jname = subprocess.check_output([sacct, "-j", str(jid), r'--format=JobName%100', "-P", "-n"]).decode('utf8')
    dct["Job name"] = jname.partition('\n')[0]

  except:
    dct["Array Job ID"] = str(jid)
    res = subprocess.check_output([sacct, "-j", str(jid), r'--format=JobName%100,State', "-P", "-n"]).decode('utf8')
    dct = dct | dict(zip(["Job name", "State"], res.partition('\n')[0].split("|")))
    pass


  return dct

def slurm_finished(args):
  file_list = []
  for infile in args.infile:
    if infile.endswith(".py"):
      spec = importlib.util.spec_from_file_location("infile", infile)
      mod = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(mod)
      setup = schema.validate(mod.setup)
      dirs = [os.path.abspath(f"{setup.work_dir}/{run.run_name}") for run in setup.runs]
      file_list.extend([f for f in itertools.chain.from_iterable([gl.glob(f'{dir}/*/slurm-*') for dir in dirs])])
    else:
      file_list.extend([f.name for f in Path(infile).glob('**/slurm-*') if f.is_file()])

  jobs = []
  jids = []
  for file in tqdm(file_list):
    if (m := re.match(r'.*slurm-([0-9,\_]+).*', file)):
      jid = m.group(1)
      if not (jid in jids):
        jids.append(jid)
        jobs.append(get_job_info(jid))

  if jobs:
    DL, headers = LDtoDL(jobs, type=set.intersection, sort_by_key="!Array Job ID")
    print(tabulate(colorize_dict_vals(DL), headers=headers))

def tsplit(str, types, delimiter="|"):
  return [types[i](v) for i, v in enumerate(str.split(delimiter))]

squeue = {
  "keys":    ["Job ID", "Job name", "Tasks per node", "# Nodes", "State",   "Elasped / Total (Start)", "Comment"],
  "fmt_str": ["%i",     "%j",       "%c",             "%D",      "%T (%r)", "%M / %l (%S)",            "%k"],
  "types":   [str,      str,        int,              int,       str,       str,                       str],
}
def get_queue():
  env = { "shell": True, "capture_output": True, "text": True }
  res = subprocess.run([fr'squeue --me -r -h --format="{"|".join(squeue["fmt_str"])}"'], **env)
  items = res.stdout.split("\n")
  vals = lambda x: tsplit(x, squeue["types"])
  return {item.split("|")[0]: dict(zip(squeue["keys"], vals(item))) for item in items}

def suffix_to_int(str):
  if str.endswith("K"):
    return int(str[:-1])*1000
  elif str.endswith("M"):
    return int(str[:-1])*1000*1000
  elif str.endswith("G"):
    return int(str[:-1])*1000*1000*1000
  else:
    return int(str[:-1])

def get_mem_usages(jobs):
  env = { "shell": True, "capture_output": True, "text": True }
  keys = list(set(map(lambda x: x.split("_")[0], jobs.keys()))) # TODO remove
  for jid in tqdm(keys):
    res = subprocess.run([f'sstat -n --format="JobID,MaxRSS" -j {jid} --parsable2 -n'], **env)
    for item in res.stdout.split():
      jid, mem = tsplit(item, [lambda x: x[:-2], suffix_to_int])
      for j_id in jobs.keys():
        if (j_id.startswith(f"{jid}_") or j_id == jid):
          jobs[j_id]["Memory usage per task"] = humanize.naturalsize(mem)
          jobs[j_id]["Memory usage per node"] = humanize.naturalsize(mem*jobs[j_id]["Tasks per node"])
          jobs[j_id]["Total memory usage"] = humanize.naturalsize(mem*jobs[j_id]["Tasks per node"]*jobs[j_id]["# Nodes"])
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
