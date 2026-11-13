"""Storage of the user on the cluster: quotas of the filesystems, and the oldest files in the scratch directory (which
the cluster may delete after some time without asking)"""

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

# ---------------------------------------------------------------------------
# old files in the scratch directory

OLD_FILES_LIMIT = 2000 # oldest files listed

def scratch_dir():
  """The scratch directory of the user on the cluster ($SCRATCH, also from a login shell)"""
  out = run(["sh", "-c", 'printf "%s" "${SCRATCH:-}"'], quiet=True).strip()
  if not out:
    out = run(["bash", "-lc", 'printf "%s" "${SCRATCH:-}"'], quiet=True, timeout=60).strip().splitlines()[-1:] or [""]
    out = out[0].strip()
  return out

def oldest_files(directory, by="atime", limit=OLD_FILES_LIMIT):
  """The user's files in directory with the oldest access (or modification) time, and the number and size of all of
  them: one find over the whole tree on the cluster (it can take minutes; the worker can be aborted)"""
  key = "%A@" if by == "atime" else "%T@"
  script = ('find "$1" -xdev -type f -user "$(id -u)" -printf "' + key + ' %A@ %T@ %s %p\\n" 2>/dev/null '
            '| awk \'{n++; s += $4; print} END {printf "TOTAL %d %d\\n", n, s > "/dev/stderr"}\' '
            f'| sort -n -k1,1 | head -n {int(limit)}')
  res = run_cmd(["sh", "-c", script, "sh", directory], timeout=3600)
  files = []
  for line in res.stdout.splitlines():
    parts = line.split(" ", 4)
    if len(parts) == 5:
      try:
        files.append({"atime": float(parts[1]), "mtime": float(parts[2]), "size": int(parts[3]), "path": parts[4]})
      except ValueError:
        pass
  m = re.search(r"TOTAL (\d+) (\d+)", res.stderr or "")
  total = (int(m.group(1)), int(m.group(2))) if m else (len(files), sum(f["size"] for f in files))
  return {"files": files, "total": total, "by": by, "directory": directory, "time": datetime.now(),
          "error": "" if res.returncode == 0 or files else (res.stderr or "").strip()[:300]}

def touch_files(paths, what="atime", directory=None, exclude=""):
  """Touch files (access time, or access and modification time): the listed ones, or every file of the user in
  directory, without those whose path matches exclude (an extended regex). -> (number touched, error)"""
  flag = ["-a"] if what == "atime" else []
  if directory is not None:
    keep = '| grep -zvE -e "$2" ' if exclude else ""
    script = (f'find "$1" -xdev -type f -user "$(id -u)" -print0 {keep}| xargs -0 -r touch {" ".join(flag)} -c -- ; echo DONE')
    res = run_cmd(["sh", "-c", script, "sh", directory, exclude], timeout=7200, retry=False)
    return (None if "DONE" in res.stdout else 0), (res.stderr or "").strip()[:300]
  done, errors = 0, []
  for i in range(0, len(paths), 500):
    chunk = paths[i:i + 500]
    res = run_cmd(["touch", *flag, "-c", "--", *chunk], timeout=600, retry=False)
    if res.returncode:
      errors.append((res.stderr or "").strip()[:200])
    done += len(chunk)
  return done, "; ".join(errors)
