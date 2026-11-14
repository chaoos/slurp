"""Site rule packs: what the checks should know about a cluster that Slurm does not tell.

A pack is a TOML file. The built-in ones are in rules/ next to this module, the user's own in
~/.config/slurp/rules/ (they take precedence). A pack applies when one of its "match" patterns (fnmatch) matches the
tab's name, its SSH destination, or the login node's host name.

Keys (all optional):
  match = ["daint*", "*.cscs.ch"]
  name = "CSCS Alps"
  scratch_purge_days = 30          # Q04, Q05
  parallel_fs = ["/capstor"]       # P07: environments there are slow
  numa_domains = 4                 # N01: NUMA domains per node
  cuda_arch = "sm_90"              # P06
  gpus_implicit = false            # R07: GPU nodes give their GPUs without a request
  login_shell = true               # S02: module needs #!/bin/bash -l
  no_purge = false                 # E01: no module purge advice (e.g. uenv instead of modules)
  muted = ["R08"]                  # rules that do not apply here
  options = ["uenv", "view"]       # sbatch options of site plugins (S04)
  docs = "https://docs.cscs.ch"    # the site's documentation (guides)
  [[advice]]                       # E03: a pattern in the script and what to say about it
  pattern = "conda activate"
  text = "..."
  severity = "info"
"""

import fnmatch
import os

try:
  import tomllib
except ImportError: # Python < 3.11
  tomllib = None

BUILTIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")

def user_dir():
  base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
  return os.path.join(base, "slurp", "rules")

def load_packs():
  packs = []
  if tomllib is None:
    return packs
  for directory in (user_dir(), BUILTIN):
    try:
      names = sorted(os.listdir(directory))
    except OSError:
      continue
    for name in names:
      if name.endswith(".toml"):
        path = os.path.join(directory, name)
        try:
          with open(path, "rb") as f:
            pack = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
          from .state import log
          log.warning(f"rule pack {path}: {e}")
          continue
        pack["_path"] = path
        packs.append(pack)
  return packs

def pack_for(tab, destination, facts):
  """The first pack that matches the tab, its destination or the login node's host name ({} if none)"""
  names = [n.lower() for n in (tab, destination, facts.env.get("HOSTNAME", ""), facts.env.get("CLUSTER_NAME", "")) if n]
  names += [n.split("@")[-1] for n in names if "@" in n]
  for pack in load_packs():
    for pattern in pack.get("match", []):
      if any(fnmatch.fnmatch(n, pattern.lower()) for n in names):
        return pack
  return {}
