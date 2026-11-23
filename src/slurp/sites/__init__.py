"""Site packs: what slurp should know about a site that Slurm does not tell (for the checks, transfers, the overview,
the login).

A pack is a TOML file. The built-in ones are in packs/ next to this module, the user's own in
~/.config/slurp/rules/ (they take precedence). A pack applies when one of its "match" patterns (fnmatch) matches the
tab's name, its SSH destination, or the login node's host name.

Versions: a pack's "schema" is the version of its format (the keys and what they mean): this slurp reads SCHEMA and
older, and leaves out a pack of a newer schema (it might mean something else by a key); without one: 1. Its "version"
is the version of its content, a date (YYYY.MM.DD), raised with every change of a built-in pack: the rule catalogue
says which pack and version applies, so that a finding can be traced to the pack it came from. A new key that older
slurps can ignore needs no new schema; a key that changes its meaning, or one that must not be ignored, does.

Keys (all optional):
  schema = 1                       # the version of the format (see above)
  version = "2026.11.20"           # the version of the content (see above)
  match = ["daint*", "*.cscs.ch"]
  name = "CSCS Alps"
  scheduler = "slurm"              # the batch system: slurm (the default), pbs, tcs (Fujitsu, pjsub), sge, lsf, flux,
                                   # other; without Slurm slurp logs in, has files, shells, transfers, no job list
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

The clusters of the site, offered in the form of a new SSH connection (From a site): they fill in its fields, to see
and change before Add; nothing is used without the form. [login] is how all of them log in, a [[clusters]] entry
can say otherwise for its own:
  [login]
  user_label = "your CSCS username" # what the user is (the form asks for it)
  required = ["user"]              # the fields the user must fill in (of REQUIRABLE): marked in the form, Add and Test wait
  jump = "{user}@ela.cscs.ch"      # a jump host; {user}: the user filled in
  identities = ["~/.ssh/cscs-key"] # private keys (the jump host is offered them too)
  options = ["AddressFamily=inet"] # more ssh options, Key=Value
  port = 22
  pre_login = "cscs-key sign --headless" # a command here before the login (in the login pane: answer it there);
                                   # {user}: the user filled in, quoted (e.g. "sshproxy.sh -u {user}")
  pre_login_fallback = true        # …only when a login without it fails (a key still valid is not signed anew)
  needs = [{ command = "cscs-key", install = "https://…" }] # programs here that the login needs, and how to get them
  setup = "…"                      # what to do once, before the first login (plain text, shown in the form)
  [[clusters]]
  name = "daint"                   # the tab's name
  title = "Daint"                  # in the list (with the pack's name)
  host = "daint.alps.cscs.ch"

A login command (pre_login) runs on the user's machine and a host or jump host decides where the login goes: a pack
that is not built in or the user's own (one fetched from elsewhere, some day) must never set them without the user
seeing them in the form first.
"""

import fnmatch
import functools
import os
import pathlib
import re
import shlex
from importlib import resources

try:
  import tomllib
except ImportError: # Python < 3.11
  tomllib = None

BUILTIN = resources.files(__package__) / "packs" # (the packs that come with slurp)
SCHEMA = 1 # the newest pack format this slurp reads
VERSION = re.compile(r"\d{4}\.\d{2}\.\d{2}") # a pack's version: the date of its content

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
        log.warning(f"site pack {path}: {e}")
        continue
      schema = pack.get("schema", 1)
      if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1 or schema > SCHEMA:
        from ..state import log
        log.warning(f"site pack {path}: schema {schema!r}, this slurp reads 1 to {SCHEMA}: left out (a newer slurp reads it)")
        continue
      pack["_path"] = str(path)
      pack["_builtin"] = directory is BUILTIN
      packs.append(pack)
  return packs

LOGIN_KEYS = ("user_label", "required", "host", "port", "jump", "identities", "options", "pre_login", "pre_login_fallback", "needs", "setup")
REQUIRABLE = ("user", "host", "port", "jump", "identities", "pre_login") # (the fields of the form a pack can leave to the user)
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
JUMP_HOST = re.compile(r"(?:(?:\{user\}|[A-Za-z0-9._-]+)@)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?")
OPTION = re.compile(r"[A-Za-z][A-Za-z0-9]*\s*(=|\s)\s*\S.*")

def login_errors(entry):
  """What is wrong with the login of a pack's cluster (merged with its [login]): [text]"""
  errors = []
  def kind(key, type_, what):
    value = entry.get(key)
    if key in entry and (not isinstance(value, type_) or isinstance(value, bool) and type_ is not bool): # (true is no number)
      errors.append(f"{key}: not {what}")
  for key in ("name", "title", "host", "jump", "user_label", "pre_login", "setup"):
    kind(key, str, "text")
  kind("port", int, "a number")
  kind("pre_login_fallback", bool, "true or false")
  for key in ("identities", "options", "needs", "required"):
    kind(key, list, "a list")
  if errors:
    return errors
  if not entry.get("name") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", entry["name"]):
    errors.append(f"name {entry.get('name')!r}: letters, digits, . _ - (the tab's name)")
  if not HOST.fullmatch(entry.get("host") or ""):
    errors.append(f"host {entry.get('host')!r}: not a host name")
  if entry.get("jump") and not all(JUMP_HOST.fullmatch(j) for j in entry["jump"].split(",")):
    errors.append(f"jump {entry['jump']!r}: not [user@]host[:port] (more: commas)")
  if "port" in entry and not 0 < entry["port"] < 65536:
    errors.append(f"port {entry['port']}: not 1 to 65535")
  if not all(isinstance(i, str) and i.startswith(("~/", "/")) for i in entry.get("identities", [])):
    errors.append("identities: paths (~/… or /…)")
  if not all(isinstance(o, str) and OPTION.fullmatch(o) for o in entry.get("options", [])):
    errors.append("options: Key=Value")
  if not all(field in REQUIRABLE for field in entry.get("required", [])):
    errors.append(f"required: of {', '.join(REQUIRABLE)}")
  if "\n" in entry.get("pre_login", ""):
    errors.append("pre_login: one line")
  for need in entry.get("needs", []):
    if not isinstance(need, dict) or not re.fullmatch(r"[A-Za-z0-9._+-]+", str(need.get("command", ""))) \
       or not str(need.get("install", "https://")).startswith("https://"):
      errors.append(f"needs: {need!r}: {{ command = name, install = https://… }}")
  return errors

def site_clusters(packs=None):
  """The clusters the site packs offer for a new SSH connection, their login merged with the pack's [login]:
  [{"id": "<pack file>/<name>", "site", "version", "builtin", "name", "title", "host", …}] (an entry that is not
  right: left out, logged)"""
  packs = load_packs() if packs is None else packs
  out, seen = [], set()
  for pack in packs:
    login = pack.get("login") if isinstance(pack.get("login"), dict) else {}
    for raw in pack.get("clusters") or []:
      if not isinstance(raw, dict):
        continue
      entry = {k: v for k, v in login.items() if k in LOGIN_KEYS} | raw
      errors = login_errors(entry)
      ident = f"{os.path.basename(pack.get('_path', '')).removesuffix('.toml')}/{entry.get('name')}"
      if errors:
        from ..state import log
        log.warning(f"site pack {pack.get('_path')}: the cluster {entry.get('name')!r} is left out: {'; '.join(errors)}")
        continue
      if ident in seen: # (yours of the same file name before the built-in one)
        continue
      seen.add(ident)
      out.append({**{k: entry[k] for k in ("name", *LOGIN_KEYS) if k in entry}, "id": ident, "site": pack_name(pack),
                  "title": entry.get("title") or entry["name"], "version": str(pack.get("version") or ""),
                  "builtin": bool(pack.get("_builtin")), "scheduler": pack_scheduler(pack)})
  return out

def login_settings(entry, user=""):
  """The fields of the new-connection form for a site's cluster, the user filled in: {"name", "host", "user", "port",
  "jump", "identities", "options", "pre_login", "pre_login_fallback"}"""
  jump = entry.get("jump", "")
  jump = jump.replace("{user}@", f"{user}@" if user else "") # (no user yet: the jump host as ssh's default makes it)
  # (the command before the login: the user as one word of the shell, quoted; none yet: {user} stays, to be seen)
  pre_login = entry.get("pre_login", "").replace("{user}", shlex.quote(user) if user else "{user}")
  return {"name": entry["name"], "host": entry["host"], "user": user, "port": str(entry.get("port") or ""), "jump": jump,
          "identities": list(entry.get("identities", [])), "options": list(entry.get("options", [])),
          "pre_login": pre_login, "pre_login_fallback": bool(entry.get("pre_login_fallback"))}

def pack_text(pack):
  """What a pack is, for the rule catalogue: its name, version, and where it is from ("" without a pack)"""
  if not pack:
    return ""
  version = str(pack.get("version") or "")
  where = "built in" if pack.get("_builtin") else f"yours: {pack.get('_path', '')}"
  return f"{pack_name(pack)}" + (f", version {version}" if version else "") + f" ({where})"

SCHEDULERS = {"slurm": "Slurm", "pbs": "PBS", "tcs": "Fujitsu TCS (pjsub)", "sge": "Grid Engine", "lsf": "LSF",
              "flux": "Flux", "other": "another batch system"}

def pack_scheduler(pack):
  """The batch system of a pack's site (SCHEDULERS; "slurm" if it does not say, or says something unknown)"""
  value = str(pack.get("scheduler") or "slurm").lower()
  return value if value in SCHEDULERS else "slurm"

def pack_name(pack):
  return str(pack.get("name") or os.path.basename(pack.get("_path", "")).removesuffix(".toml") or "a site pack")

def packs_table(packs=None):
  """The packs this slurp reads, as lines (slurp --version): name, version, built in or yours (its file)"""
  packs = load_packs() if packs is None else packs
  if not packs:
    return [f"site packs (format {SCHEMA}): none"]
  width = max(len(pack_name(p)) for p in packs)
  return [f"site packs (format {SCHEMA}):"] + [
    f"  {pack_name(p):<{width}}  {str(p.get('version') or '-'):<10}  " + ("built in" if p.get("_builtin") else f"yours: {p.get('_path', '')}")
    for p in packs]

def packs_line(packs=None):
  """The packs this slurp reads, in one line (About slurp): the built-in ones, then yours"""
  packs = load_packs() if packs is None else packs
  def names(group):
    return ", ".join(pack_name(p) + (f" {p['version']}" if p.get("version") else "") for p in group)
  builtin, yours = [p for p in packs if p.get("_builtin")], [p for p in packs if not p.get("_builtin")]
  parts = ([f"built in: {names(builtin)}"] if builtin else []) + ([f"yours ({user_dir()}): {names(yours)}"] if yours else [])
  return "; ".join(parts) + f" (format {SCHEMA})" if parts else "none"

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
