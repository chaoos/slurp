"""Site packs: what slurp should know about a site that Slurm does not tell (for the checks, transfers, the overview,
the login).

A pack is a TOML file. The built-in ones are in packs/ next to this module, the user's own in
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
  stacks = ["uenv"]                # the software stacks ("modules", "uenv"), if slurp should not find them itself
  options = ["uenv", "view"]       # sbatch options of site plugins (S04)
  docs = "https://docs.cscs.ch"    # the site's documentation (guides)
  status = "https://status.cscs.ch" # the site's status page (outages, maintenance): shown when the login fails
  transfer_partition = "xfer"      # transfers: the Slurm partition for data transfers (transfer jobs run there)
  transfer_hosts = ["ela.cscs.ch"] # transfers: data mover nodes (shown in the check of a transfer)
  globus = "https://…"             # transfers: the site's Globus collection or its page (suggested for large transfers)
  login_node_limits = "…"          # transfers: what the site does to long processes on login nodes
  [[advice]]                       # E03: a pattern in the script and what to say about it
  pattern = "conda activate"
  text = "..."
  severity = "info"
"""

import fnmatch
import functools
import os
import pathlib
from importlib import resources

try:
  import tomllib
except ImportError: # Python < 3.11
  tomllib = None

BUILTIN = resources.files(__package__) / "packs" # (the packs that come with slurp)

def user_dir():
  base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
  return os.path.join(base, "slurp", "rules")

def load_packs():
  packs = []
  if tomllib is None:
    return packs
  for directory in (pathlib.Path(user_dir()), BUILTIN): # (a directory, or the packs of the package: the same interface)
    try:
      files = sorted((p for p in directory.iterdir() if p.name.endswith(".toml")), key=lambda p: p.name)
    except OSError:
      continue
    for path in files:
      try:
        with path.open("rb") as f:
          pack = tomllib.load(f)
      except (OSError, tomllib.TOMLDecodeError) as e:
        from ..state import log
        log.warning(f"rule pack {path}: {e}")
        continue
      pack["_path"] = str(path)
      packs.append(pack)
  return packs

@functools.lru_cache(maxsize=64)
def status_page(tab, destination):
  """The status page of the site of a tab (its pack's "status"), "" if none: matched by the tab's name and its SSH
  destination only (it is wanted when the login fails, before the login node can say who it is)"""
  url = str(site_pack(tab, destination).get("status") or "")
  return url if url.startswith(("https://", "http://")) else ""

@functools.lru_cache(maxsize=64)
def site_pack(tab, destination):
  """The pack of the site of a tab, matched by the tab's name and its SSH destination only ({} if none)"""
  names = [n.lower() for n in (tab, destination) if n]
  names += [n.split("@")[-1] for n in names if "@" in n]
  for pack in load_packs():
    if any(fnmatch.fnmatch(n, pattern.lower()) for pattern in pack.get("match", []) for n in names):
      return pack
  return {}

def pack_for(tab, destination, facts):
  """The first pack that matches the tab, its destination or the login node's host name ({} if none)"""
  names = [n.lower() for n in (tab, destination, facts.env.get("HOSTNAME", ""), facts.env.get("CLUSTER_NAME", "")) if n]
  names += [n.split("@")[-1] for n in names if "@" in n]
  for pack in load_packs():
    for pattern in pack.get("match", []):
      if any(fnmatch.fnmatch(n, pattern.lower()) for n in names):
        return pack
  return {}
