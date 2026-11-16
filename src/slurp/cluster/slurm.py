"""Slurm helpers: durations, sacct sizes and walltimes, the srun lines of a script, the file names of sbatch
--output/--error (from slurm.py of the scripts repository, where slurp started)"""
import argparse
import os
import re
from datetime import datetime

from dateutil.relativedelta import relativedelta

def hours(walltime):
  """Slurm walltime ([days-]hours:minutes:seconds, or fewer parts) in hours"""
  seconds = 0
  for part in walltime.split(':'):
    days, part = part.split('-') if '-' in part else ("0", part)
    seconds = seconds*60 + int(part, 10) + int(days, 10)*24
  return seconds/3600

duration_units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks", "mo": "months", "y": "years"}
duration_re = r'(\d+)(mo|[smhdwy])'
def parse_duration(string):
  """Parse a duration like 30m, 2h, 1d, 3w, 2mo, 1y or 1d12h and return the point in time that far back"""
  string = string.replace(" ", "")
  if not re.fullmatch(rf'(?:{duration_re})+', string):
    raise argparse.ArgumentTypeError(f"invalid duration '{string}' (expected e.g. 30s, 30m, 2h, 1d, 3w, 2mo, 1y or 1d12h)")
  delta = sum((relativedelta(**{duration_units[u]: int(n)}) for n, u in re.findall(duration_re, string)), relativedelta())
  return datetime.now().replace(microsecond=0) - delta

def sacct_bytes(string):
  """sacct sizes like '22163328K', '21511.50M' or plain bytes '1726579370'"""
  m = re.fullmatch(r'([\d.]+)([KMGTP]?)', string)
  return int(float(m.group(1)) * 1024**" KMGTP".index(m.group(2) or " ")) if m else 0

def srun_lines(script):
  """Lines of a (batch) script that call srun, without comments"""
  return [l.strip() for l in script.splitlines() if not l.lstrip().startswith("#") and re.search(r'(?:^|[\s;&|(`])srun(?:\s|$)', l)]

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
