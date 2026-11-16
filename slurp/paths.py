"""Paths on the cluster of a tab: the variables of a job in them, the paths in command lines, which of them exist and
what they are (text, binary, HDF5, a core dump), reading text. jobs.py and details.py build on it."""

import os
import re
import shlex

from .remote import conn, fs_exists, fs_expanduser, fs_read


def job_vars(job):
  """Environment of the job as far as it can be known, to expand $VARS in its batch script"""
  array_id, _, task = job.jid.partition("_")
  raw = job.rec["JobIDRaw"]
  return os.environ | {
    "SLURM_JOB_ID": raw, "SLURM_JOBID": raw, "SLURM_ARRAY_JOB_ID": array_id, "SLURM_ARRAY_TASK_ID": task,
    "SLURM_SUBMIT_DIR": job.rec["WorkDir"], "SLURM_JOB_NAME": job.rec["JobName"], "PWD": job.rec["WorkDir"],
    "SLURM_JOB_NUM_NODES": job.rec["NNodes"], "SLURM_NNODES": job.rec["NNodes"], "SLURM_NTASKS": job.rec["NTasks"],
  }

def expand_vars(string, env):
  return re.sub(r'\$\{(\w+)\}|\$(\w+)', lambda m: env.get(m.group(1) or m.group(2), m.group(0)), string)

def path_candidates(line):
  """Words of a command line that might be file names, including the values of --opt=value"""
  try:
    words = shlex.split(line, comments=True)
  except ValueError:
    words = line.split()
  for word in words[:500]:
    for part in [word] + ([word.split("=", 1)[1]] if "=" in word else []):
      part = re.sub(r'^\d*[<>]+&?', '', part).rstrip(";&|")
      if part and not part.startswith("-"):
        yield part

FS_INFO_SCRIPT = r"""
for p; do
  if [ ! -e "$p" ]; then echo -; continue; fi
  t=o; [ -d "$p" ] && t=d; [ -f "$p" ] && t=f
  s=$(stat -L -c %s -- "$p" 2>/dev/null || echo 0); h=0; b=0
  if [ "$t" = f ] && [ -r "$p" ]; then
    [ "$(od -An -tx1 -N 8 -- "$p" 2>/dev/null | tr -d ' \n')" = 894844460d0a1a0a ] && h=1
    [ "$(head -c 8192 -- "$p" | wc -c)" = "$(head -c 8192 -- "$p" | tr -d '\000' | wc -c)" ] || b=1
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$t" "$s" "$h" "$b" "$(readlink -f -- "$p")"
done
"""

def fs_infos(paths):
  """{path: {"isdir", "isfile", "size", "hdf5", "binary", "realpath"}} of the existing ones among paths;
  on a remote cluster in one round trip"""
  paths = list(dict.fromkeys(paths))
  infos = {}
  if not paths:
    return infos
  if conn() is None:
    for p in paths:
      try:
        st = os.stat(p)
      except OSError:
        continue
      isfile = os.path.isfile(p)
      infos[p] = {"isdir": os.path.isdir(p), "isfile": isfile, "size": st.st_size, "realpath": os.path.realpath(p),
                  "hdf5": isfile and _is_hdf5_local(p), "binary": isfile and is_binary(p)}
    return infos
  res = conn().run(["sh", "-c", FS_INFO_SCRIPT, "sh", *paths], quiet=True)
  for p, line in zip(paths, res.stdout.splitlines()):
    fields = line.split("\t", 4)
    if len(fields) == 5:
      t, size, h, b, real = fields
      infos[p] = {"isdir": t == "d", "isfile": t == "f", "size": int(size) if size.isdigit() else 0,
                  "hdf5": h == "1", "binary": b == "1", "realpath": real or p}
  return infos

def existing_files(lines, workdir, env=None, dirs=False):
  """Existing files (and directories if dirs) named in command lines, relative paths are relative to workdir
  (checked together: one round trip on a remote cluster)"""
  cands = []
  for line in lines:
    if env is not None:
      line = expand_vars(line, env)
    for cand in path_candidates(line):
      path = fs_expanduser(cand)
      path = path if os.path.isabs(path) else os.path.join(workdir or ".", path)
      cands.append(os.path.normpath(path))
  infos = fs_infos(cands)
  return [p for p in dict.fromkeys(cands) if p in infos and (infos[p]["isfile"] or dirs and infos[p]["isdir"] and p != "/")]

PATH_DELIMS = set(" \t\"'`=,;()[]{}<>|")

def word_at(line, col):
  """The word around column col of line, delimited by white space, quotes, '=' and brackets"""
  if not (0 <= col < len(line)) or line[col] in PATH_DELIMS:
    return ""
  start, end = col, col
  while start > 0 and line[start-1] not in PATH_DELIMS:
    start -= 1
  while end < len(line) and line[end] not in PATH_DELIMS:
    end += 1
  return line[start:end]

def resolve_path(word, bases=(), env=None):
  """The existing file or directory a word of a text refers to (None if there is none).
  Relative paths are tried against the base directories in order, 'path:123' as path."""
  word = re.sub(r'^\d*[<>]+&?', '', word.strip()).rstrip(".,;:&|")
  if env:
    word = expand_vars(word, env)
  for cand in dict.fromkeys([word, word.split(":")[0]]):
    if not re.search(r'[\w~]', cand) or cand.startswith("-"):
      continue
    cand = fs_expanduser(cand)
    for path in [cand] if os.path.isabs(cand) else [os.path.join(b, cand) for b in bases if b]:
      if fs_exists(path):
        return os.path.normpath(path)
  return None

def is_binary(path):
  try:
    return b"\0" in fs_read(path, size=8192)
  except OSError:
    return False

def script_lines(text):
  """Lines of a batch script that may name files: commands and #SBATCH options, no other comments"""
  lines = []
  for line in text.splitlines()[1:]:
    stripped = line.strip()
    if stripped.startswith("#SBATCH"):
      lines.append(stripped[len("#SBATCH"):])
    elif stripped and not stripped.startswith("#"):
      lines.append(stripped)
  return lines

def read_text(path, limit=1024*1024):
  try:
    return fs_read(path, size=limit).decode("utf-8", errors="replace")
  except OSError:
    return ""

HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"

def _is_hdf5_local(path):
  """HDF5 signature at the start of the file or behind a user block (512, 1024, 2048, ... bytes)"""
  try:
    offset = 0
    while offset <= 1 << 20:
      sig = fs_read(path, offset, 8)
      if sig == HDF5_SIGNATURE:
        return True
      if len(sig) < 8:
        return False
      offset = 512 if offset == 0 else 2*offset
  except OSError:
    pass
  return False

def is_hdf5(path):
  return conn().is_hdf5(path) if conn() else _is_hdf5_local(path)

def is_core_dump(path):
  """Whether path is an ELF core file (not merely a file named 'core')."""
  try:
    header = fs_read(path, size=18)
    if len(header) < 18 or header[:4] != b"\x7fELF":
      return False
    byte_order = {1: "little", 2: "big"}.get(header[5])
    return byte_order is not None and int.from_bytes(header[16:18], byte_order) == 4 # ET_CORE
  except OSError:
    return False
