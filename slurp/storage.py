"""Storage of the user on the cluster: quotas of the filesystems"""

import os
import re
import shlex
from datetime import datetime

from .state import *
from .remote import *
from .jobs import current_user

STORAGE_VARS = ("SCRATCH", "HOME", "PROJECT", "STORE", "WORK")

def storage_dirs():
  """The directories of the usual storage variables ({name: path}) and the Lustre mounts, on the cluster"""
  script = "for v in " + " ".join(STORAGE_VARS) + '; do eval "p=\\${$v:-}"; [ -n "$p" ] && printf "%s=%s\\n" "$v" "$p"; done; ' \
           "awk '$3 == \"lustre\" {print \"MOUNT=\" $2}' /proc/mounts 2>/dev/null | sort -u"
  out = run(["sh", "-c", script], quiet=True)
  dirs, mounts = {}, []
  for line in out.splitlines():
    key, _, value = line.partition("=")
    if key == "MOUNT":
      mounts.append(value)
    elif key in STORAGE_VARS and value:
      dirs[key] = value
  return dirs, mounts

def parse_quota_table(text):
  """Rows of lfs quota / quota -w -p output (the Filesystem table; sizes in kB):
  [{"filesystem", "kb", "kb_soft", "kb_hard", "files", "files_soft", "files_hard", "over"}]"""
  lines = text.splitlines()
  start = next((i for i, l in enumerate(lines) if l.split()[:1] == ["Filesystem"]), None)
  if start is None:
    return []
  tokens = " ".join(lines[start + 1:]).split()
  rows, i = [], 0
  number = re.compile(r"^\d+\*?$")
  while i < len(tokens):
    if number.match(tokens[i]): # a value without a filesystem before it
      i += 1
      continue
    fs, values = tokens[i], tokens[i + 1:i + 9]
    if len(values) < 8:
      break
    def num(v):
      v = v.rstrip("*")
      return int(v) if v.isdigit() else 0
    kb, kq, kl, _, files, fq, fl, _ = values
    if not number.match(kb) or not number.match(files):
      i += 1
      continue
    rows.append({"filesystem": fs, "kb": num(kb), "kb_soft": num(kq), "kb_hard": num(kl),
                 "files": num(files), "files_soft": num(fq), "files_hard": num(fl), "over": "*" in kb + files})
    i += 9
  return rows

def quota_info():
  """Quotas of the user (and the user's groups) on the cluster's filesystems: lfs quota for every Lustre mount and
  storage directory, quota -w -p for the others, and the output of the site's own quota command"""
  dirs, mounts = storage_dirs()
  c = conn()
  user = current_user()
  groups = (c.groups if c else [g for g in run(["id", "-Gn"], quiet=True).split()])[:12]
  paths = list(dict.fromkeys([*mounts, *dirs.values()]))
  has_lfs = bool(run(["sh", "-c", "command -v lfs || true"], quiet=True).strip())
  cmds = [["quota", "-w", "-p", "-u", user], ["quota"]]
  targets = []
  if has_lfs:
    for path in paths:
      cmds.append(["lfs", "quota", "-u", user, path])
      targets.append(("user", user, path))
      for group in groups:
        cmds.append(["lfs", "quota", "-g", group, path])
        targets.append(("group", group, path))
  res = run_many(cmds, timeout=120)
  entries, seen = [], set()
  for (kind, name, path), r in zip(targets, res[2:]):
    if r.returncode:
      continue
    for row in parse_quota_table(r.stdout):
      limited = row["kb_soft"] or row["kb_hard"] or row["files_soft"] or row["files_hard"]
      if kind == "group" and not limited and not row["kb"]:
        continue # groups without usage and without a limit there
      key = (kind, name, row["filesystem"])
      if key not in seen:
        seen.add(key)
        entries.append(dict(row, kind=kind, name=name, path=path, source="lfs quota"))
  if res[0].returncode in (0, 1): # quota exits 1 when over quota
    for row in parse_quota_table(res[0].stdout):
      key = ("user", user, row["filesystem"])
      if key not in seen:
        seen.add(key)
        entries.append(dict(row, kind="user", name=user, path=row["filesystem"], source="quota"))
  site = res[1].stdout.strip() if res[1].returncode in (0, 1) else ""
  return {"time": datetime.now(), "user": user, "dirs": dirs, "entries": entries,
          "site": site if site and not parse_quota_table(site) else "",
          "errors": [] if entries or site else ["no quota information: neither lfs quota nor quota answered"]}
