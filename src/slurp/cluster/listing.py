"""A directory listed with what the directory view shows of each entry: kind, size, time, permissions (as ls -l shows
them), owner and group, the extended ACL entries (getfacl), and what the user may do with it (read, write, execute
or enter: asked of the filesystem, so ACLs, groups and root squash count). On a cluster in one round trip."""

import contextlib
import grp
import os
import pwd
import re
import shutil
import stat
import subprocess

from .remote import conn

ACL_MARK = "SLURP-ACL"

# find prints a record per entry (the directory itself first, its %P empty): what the user may do (r, w, x or -),
# then the fields; getfacl -s prints only the entries with more than the base ACL
SCRIPT = r"""
find -- "$1" -mindepth 0 -maxdepth 1 \( -readable -printf r -o -printf - \) , \( -writable -printf w -o -printf - \) , \
  \( -executable -printf x -o -printf - \) , -printf '\t%Y\t%s\t%T@\t%y\t%M\t%u\t%g\t%P\0' 2>/dev/null
status=$?
printf '%s\0' SLURP-ACL
if command -v getfacl >/dev/null 2>&1; then
  find -- "$1" -mindepth 0 -maxdepth 1 -exec getfacl -s -p -- {} + 2>/dev/null
fi
exit $status
"""

def listing(path, limit=None):
  """(the directory, [its entries]): dicts with name, dir, size, mtime, link, mode ("drwxr-x---"), owner, group,
  access ("rwx", "r-x", …: what the user may do), acl (["user:bob:r-x", …], the extended entries; [] if none)"""
  c = conn()
  if c is not None:
    res = c.run(["sh", "-c", SCRIPT, "sh", path], timeout=120, quiet=True)
    out = res.stdout or ""
    if not out.partition(ACL_MARK)[0]: # (not even the directory itself: it is not there; a directory that cannot
      raise OSError((res.stderr or "").strip() or f"cannot list {path}") # be read lists itself, without entries)
    return parse(out, path, limit)
  return local_listing(path, limit)

def parse(out, path, limit=None):
  records, _, acl_text = out.partition(ACL_MARK + "\0")
  acls = parse_getfacl(acl_text, path)
  this, entries = None, []
  for record in records.split("\0"):
    fields = record.split("\t", 8)
    if len(fields) != 9:
      continue
    access, kind, size, mtime, own, mode, owner, group, name = fields
    entry = {"name": name, "dir": kind == "d", "size": int(size) if kind != "d" else None, "mtime": float(mtime),
             "link": own == "l", "mode": mode, "owner": owner, "group": group, "access": access,
             "acl": acls.get(name, [])}
    if name == "":
      this = entry
    elif limit is None or len(entries) < limit:
      entries.append(entry)
  return this, entries

def parse_getfacl(text, path):
  """{name in the directory ("" the directory itself): [extended ACL entries]} of getfacl -s -p output"""
  acls, name = {}, None
  base = os.path.normpath(path)
  for line in text.splitlines():
    if line.startswith("# file: "):
      file = os.path.normpath(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line[8:]))
      name = "" if file == base else os.path.basename(file)
      acls[name] = []
    elif name is not None and line and not line.startswith("#"):
      entry = line.split("#")[0].strip() # ("#effective:…" after an entry the mask limits)
      if entry and not re.fullmatch(r"(default:)?(user|group|other)::\S+", entry):
        acls[name].append(entry)
  return {k: v for k, v in acls.items() if v}

def local_listing(path, limit=None):
  def info(p, name):
    lst = os.lstat(p)
    try:
      st = os.stat(p)
    except OSError:
      st = lst
    isdir = stat.S_ISDIR(st.st_mode)
    return {"name": name, "dir": isdir, "size": None if isdir else lst.st_size, "mtime": lst.st_mtime, # (as ls -l)
            "link": stat.S_ISLNK(lst.st_mode), "mode": stat.filemode(lst.st_mode), "owner": user_name(lst.st_uid),
            "group": group_name(lst.st_gid), "acl": [],
            "access": "".join(c if os.access(p, m) else "-" for c, m in (("r", os.R_OK), ("w", os.W_OK), ("x", os.X_OK)))}
  this, entries = info(path, ""), []
  try:
    it = os.scandir(path)
  except PermissionError: # (listed as the directory itself, without entries: what the user may do shows why)
    it = None
  with it or contextlib.nullcontext([]) as it:
    for de in it:
      if limit is not None and len(entries) >= limit:
        break
      try:
        entries.append(info(de.path, de.name))
      except OSError:
        entries.append({"name": de.name, "dir": False, "size": None, "mtime": 0, "link": False, "mode": "?", "owner": "",
                        "group": "", "access": "---", "acl": []})
  if shutil.which("getfacl"):
    paths = [path] + [os.path.join(path, e["name"]) for e in entries]
    text = ""
    for i in range(0, len(paths), 500):
      try:
        text += subprocess.run(["getfacl", "-s", "-p", "--", *paths[i:i + 500]], capture_output=True, text=True,
                               timeout=60).stdout
      except (OSError, subprocess.SubprocessError):
        break
    acls = parse_getfacl(text, path)
    for e in [this, *entries]:
      e["acl"] = acls.get(e["name"], [])
  return this, entries

names = {}

def user_name(uid):
  if ("u", uid) not in names:
    try:
      names["u", uid] = pwd.getpwuid(uid).pw_name
    except KeyError:
      names["u", uid] = str(uid)
  return names["u", uid]

def group_name(gid):
  if ("g", gid) not in names:
    try:
      names["g", gid] = grp.getgrgid(gid).gr_name
    except KeyError:
      names["g", gid] = str(gid)
  return names["g", gid]

def can_read(entry):
  """The user can read it: a file, or list and enter a directory"""
  return entry["access"][0] == "r" and (not entry["dir"] or entry["access"][2] == "x")

def can_write(entry):
  """The user can write it: a file, or create and remove entries in a directory"""
  return entry["access"][1] == "w" and (not entry["dir"] or entry["access"][2] == "x")

def mode_text(entry):
  """The permissions as ls -l shows them: a + when there are extended ACL entries"""
  return entry["mode"] + ("+" if entry["acl"] else "")

def short_acl(entries):
  """user:bob:r-x → u:bob:r-x, default:group:p:rwx → d:g:p:rwx (for a column)"""
  return " ".join(e.replace("default:", "d:").replace("user:", "u:").replace("group:", "g:").replace("mask:", "m:")
                  .replace("other:", "o:") for e in entries)

# ---------------------------------------------------------------------------
# search: names that match, in a directory and its subdirectories

# One round trip for a batch of directories: their subdirectories (D, to look into next; symlinks are not followed,
# so no loops) and their entries whose name matches the pattern (case-insensitive glob), with what the listing has
SEARCH_SCRIPT = r"""
pat=$1; shift
find "$@" -mindepth 1 -maxdepth 1 \( -type d -printf 'D\t%p\0' \) , \( -iname "$pat" \( \
  \( -readable -printf r -o -printf - \) , \( -writable -printf w -o -printf - \) , \
  \( -executable -printf x -o -printf - \) , -printf '\t%Y\t%s\t%T@\t%y\t%M\t%u\t%g\t%p\0' \) \) 2>/dev/null
true
"""
SEARCH_BATCH = 100       # directories per round trip
SEARCH_ARGS = 100_000    # bytes of their paths per round trip at most

def name_pattern(text):
  """The glob a search text means: a glob as it is (*.out, run_?), else the names that contain it"""
  text = text.strip()
  return text if any(c in text for c in "*?[") else f"*{text}*"

def matches_name(name, text):
  import fnmatch
  return fnmatch.fnmatch(name.lower(), name_pattern(text).lower())

def search_batch(dirs, text):
  """([matching entries, name: the full path], [subdirectories]) of a batch of directories (worker)"""
  from .remote import run_cmd
  res = run_cmd(["sh", "-c", SEARCH_SCRIPT, "sh", name_pattern(text), *dirs], timeout=300)
  matches, subdirs = [], []
  for record in (res.stdout or "").split("\0"):
    if record.startswith("D\t"):
      subdirs.append(record[2:])
      continue
    fields = record.split("\t", 8)
    if len(fields) != 9:
      continue
    access, kind, size, mtime, own, mode, owner, group, path = fields
    matches.append({"name": path, "dir": kind == "d", "size": int(size) if kind != "d" else None, "mtime": float(mtime),
                    "link": own == "l", "mode": mode, "owner": owner, "group": group, "access": access, "acl": []})
  return matches, subdirs

def search(root, text, found, limit=5000):
  """Look for names that match text under root, breadth first, a batch of directories per round trip; found(matches,
  directories looked at, directories still to look at) after each batch (from the worker). Stops at limit matches,
  or when the worker is cancelled (remote.check_cancelled). The number of matches"""
  from .remote import check_cancelled
  queue, done, count = [os.path.normpath(root)], 0, 0
  while queue and count < limit:
    check_cancelled()
    batch, size = [], 0
    while queue and len(batch) < SEARCH_BATCH and size < SEARCH_ARGS:
      batch.append(queue.pop(0))
      size += len(batch[-1]) + 1
    matches, subdirs = search_batch(batch, text)
    check_cancelled()
    queue += subdirs
    done += len(batch)
    matches = matches[:limit - count]
    count += len(matches)
    found(matches, done, len(queue))
  return count
