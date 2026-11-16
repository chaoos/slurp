"""Fixed copy: the fixes of the checks that are safe to make automatically, applied to a copy of a script.
Ambiguous ones (--mem=64: GB or MB?) are left for the user."""

import os
import re

def apply_fixes(text, findings):
  """(new text, [what was changed]) for the findings of a script"""
  ids = {f.rule.id for f in findings}
  lines = text.replace("\r\n", "\n").split("\n")
  done = []
  if "S01" in ids:
    new = [l.rstrip("\r").replace(" ", " ").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
           for l in lines]
    new = [re.sub(r"[–—]-?(?=\w)", "--", l) for l in new]
    if new != lines or "\r" in text:
      done.append("S01: Windows line endings, curly quotes, long dashes and non-breaking spaces replaced")
      lines = new
  if "S02" in ids and not (lines and lines[0].startswith("#!")):
    lines.insert(0, "#!/bin/bash -l")
    done.append("S02: #!/bin/bash -l added")

  def first_command():
    for i, l in enumerate(lines):
      s = l.strip()
      if i == 0 and s.startswith("#!"):
        continue
      if s and not s.startswith("#"):
        return i
    return len(lines)

  if "S03" in ids:
    fc = first_command()
    moved = [l for i, l in enumerate(lines) if i > fc and l.strip().startswith("#SBATCH")]
    fixed_near = []
    for i, l in enumerate(lines):
      m = re.match(r"#\s+SBATCH\b|#sbatch\b", l.strip())
      if m and i < fc:
        lines[i] = re.sub(r"^\s*#\s*[sS][bB][aA][tT][cC][hH]", "#SBATCH", l)
        fixed_near.append(i + 1)
    if moved:
      lines = [l for i, l in enumerate(lines) if not (i > fc and l.strip().startswith("#SBATCH"))]
      lines[fc:fc] = moved
      done.append(f"S03: {len(moved)} #SBATCH lines moved before the first command")
    if fixed_near:
      done.append(f"S03: # SBATCH written as #SBATCH (lines {', '.join(map(str, fixed_near))})")

  def directive_index(option):
    for i, l in enumerate(lines):
      if re.match(rf"\s*#SBATCH\s+(--{option}[=\s]|-[{'o' if option == 'output' else 'e'}]\s)", l) and option in ("output", "error"):
        return i
    return None

  if "G07" in ids or "B05" in ids:
    array = any(re.match(r"\s*#SBATCH\s+(--array|-a)\b", l) for l in lines)
    for option in ("output", "error"):
      i = directive_index(option)
      if i is None:
        continue
      m = re.search(r"((?:--output|--error)[=\s]+|-[oe]\s+)(\S+)", lines[i])
      if not m:
        continue
      value = m.group(2)
      stem, ext = os.path.splitext(value)
      if array and not ("%a" in value and "%A" in value) and not re.search(r"%[jJ]", value):
        new = value.replace("%a", "%A_%a", 1) if "%a" in value else f"{stem}_%A_%a{ext}"
      elif not array and not re.search(r"%[jJA]", value):
        new = f"{stem}_%j{ext}"
      else:
        continue
      lines[i] = lines[i][:m.start(2)] + new + lines[i][m.end(2):]
      done.append(f"{'G07' if array else 'B05'}: --{option}={new}")
  if "B13" in ids:
    for i, l in enumerate(lines):
      if re.match(r"\s*#SBATCH\s+--mail-type", l) and "ARRAY_TASKS" in l:
        lines[i] = re.sub(r",?ARRAY_TASKS", "", l).replace("=,", "=")
        done.append("B13: ARRAY_TASKS removed from --mail-type")
  if "R09" in ids:
    for i, l in enumerate(lines):
      new = re.sub(r"\b(OMP_NUM_THREADS|MKL_NUM_THREADS|OPENBLAS_NUM_THREADS)=\d+\b", r"\1=$SLURM_CPUS_PER_TASK", l)
      if new != l:
        lines[i] = new
        done.append("R09: thread counts from $SLURM_CPUS_PER_TASK")
  if "S07" in ids:
    for i, l in enumerate(lines):
      if re.match(r"\s*cd\s+\S", l) and not re.search(r"\|\||&&|#", l):
        lines[i] = l.rstrip() + " || exit 1"
        done.append(f"S07: || exit 1 after the cd of line {i + 1}")
        break

  inserts = [] # (before line index, [lines], what)
  def first(pattern):
    for i, l in enumerate(lines):
      if not l.strip().startswith("#") and re.search(pattern, l):
        return i
    return None
  srun = first(r"(^|[\s;&|(])srun\s")
  if "N04" in ids and srun is not None:
    inserts.append((srun, ["export SRUN_CPUS_PER_TASK=$SLURM_CPUS_PER_TASK   # srun does not inherit --cpus-per-task"], "N04"))
  if "N02" in ids:
    at = first(r"OMP_NUM_THREADS")
    at = at + 1 if at is not None else srun if srun is not None else first_command()
    inserts.append((at, ["export OMP_PLACES=cores", "export OMP_PROC_BIND=close"], "N02"))
  if "E02" in ids:
    at = first(r"\bconda\s+activate\b")
    if at is not None:
      inserts.append((at, ['eval "$(conda shell.bash hook)"'], "E02"))
  if "E01" in ids and first(r"\bmodule\s+(purge|reset)") is None:
    at = first(r"^\s*(module\s+(load|add)|ml\s+\w)")
    if at is not None:
      inserts.append((at, ["module purge"], "E01"))
  for at, new, rid in sorted(inserts, key=lambda x: -x[0]):
    lines[at:at] = new
    done.append(f"{rid}: {' / '.join(new)} added")
  if "S08" in ids and not any(re.match(r"\s*wait\b", l) for l in lines):
    while lines and not lines[-1].strip():
      lines.pop()
    lines += ["wait   # for the programs started in the background", ""]
    done.append("S08: wait added at the end")
  return "\n".join(lines), done

def fixed_name(path):
  stem, ext = os.path.splitext(path)
  return f"{stem}.fixed{ext or '.sh'}"
