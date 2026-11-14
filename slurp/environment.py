"""Check environment: replay the setup lines of a batch script on the login node (module loads, exports, sources, cd,
plain assignments) and look at the programs it runs: their shared libraries (ldd), their GPU code (cuobjdump).

Only setup lines run, never the job's commands; lines with command substitutions ($(...), backticks) are skipped, so
nothing but the module system and the sourced files runs. The result feeds rules P05, P06, E04 and B02."""

import os
import re
import shlex

from .checks import strip_comment, referenced_paths, launched_program, LAUNCHERS, SKIP_PROGRAMS

SETUP = re.compile(r"^\s*(module\s|ml\s|source\s|\.\s|export\s|cd\s|unset\s|[A-Za-z_]\w*=)")

def setup_lines(script):
  """The lines of the script that set up the environment and are safe to run"""
  out = []
  for line, text in script.code_lines():
    t = strip_comment(text).strip()
    if not SETUP.match(t) or "$(" in t or "`" in t or re.search(r"[;&|]\s*\S", t.replace("||", "").replace("&&", "")):
      continue
    out.append((line, t))
  return out

def programs(ctx):
  """The programs the script runs: absolute paths, and names to look up in the PATH of the replayed environment"""
  progs = []
  for kind, path, _line in referenced_paths(ctx):
    if kind == "x":
      progs.append(path)
  for line, text in ctx.script.code_lines():
    for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", text):
      try:
        words = shlex.split(part)
      except ValueError:
        continue
      if words and words[0] in LAUNCHERS:
        prog, _ = launched_program(words)
        if prog and "/" not in prog and prog not in SKIP_PROGRAMS and not prog.startswith("$"):
          progs.append(prog)
  return list(dict.fromkeys(progs))

def replay_script(ctx):
  lines = [f"cd {shlex.quote(ctx.workdir())} 2>/dev/null"] if ctx.workdir() else []
  for line, t in setup_lines(ctx.script):
    if re.match(r"(module|ml)\s", t):
      lines.append(f"__o=$({t} 2>&1); __s=$?; {t} >/dev/null 2>&1; "
                   f"if [ $__s -ne 0 ] || printf '%s' \"$__o\" | grep -qiE 'error|unknown|not found|failed|cannot'; then "
                   f"printf '@@MODERR {line}\\t%s\\n' \"$(printf '%s' \"$__o\" | grep -iE 'error|unknown|not found|failed|cannot' | head -2 | tr '\\n' ' ')\"; fi")
    elif re.match(r"cd\s", t):
      lines.append(f"{t} 2>/dev/null") # (a missing directory is P02's)
    else:
      lines.append(f"{t} 2>/dev/null || printf '@@ERR {line}\\t%s\\n' {shlex.quote(t[:120])}")
  for prog in programs(ctx):
    q = shlex.quote(prog)
    lines.append(f"p={q}; case \"$p\" in */*) ;; *) p=$(command -v \"$p\" 2>/dev/null || echo \"$p\") ;; esac; "
                 f"printf '@@PROG %s\\t%s\\n' {q} \"$p\"; "
                 f"if [ -f \"$p\" ]; then ldd \"$p\" 2>&1 | head -200; "
                 f"command -v cuobjdump >/dev/null 2>&1 && cuobjdump --list-elf \"$p\" 2>/dev/null | head -50 | sed 's/^/@@CUDA /'; fi")
  return "\n".join(lines) + "\n"

def parse_replay(text):
  out = {"libs": {}, "missing": {}, "cuda": {}, "errors": [], "module_errors": [], "resolved": {}}
  prog = None
  for line in text.splitlines():
    if line.startswith("@@PROG "):
      name, _, path = line[7:].partition("\t")
      prog = name
      out["resolved"][name] = path
      out["libs"][name] = []
      out["missing"][name] = []
    elif line.startswith("@@MODERR "):
      where, _, msg = line[9:].partition("\t")
      out["module_errors"].append(f"line {where}: {msg.strip()}")
    elif line.startswith("@@ERR "):
      where, _, cmd = line[6:].partition("\t")
      out["errors"].append(f"line {where}: {cmd} failed")
    elif line.startswith("@@CUDA ") and prog:
      m = re.search(r"sm_(\d+)", line)
      if m:
        out["cuda"].setdefault(prog, set()).add("sm_" + m.group(1))
    elif prog:
      s = line.strip()
      if "not a dynamic executable" in s or "not regular file" in s:
        out["libs"][prog] = None
      elif "=> not found" in s:
        out["missing"][prog].append(s.split()[0])
        out["libs"][prog].append(s.split()[0])
      elif "=>" in s or s.startswith("/"):
        out["libs"][prog].append(s.split()[0])
  return out

def replay_environment(ctx):
  """Run the replay on the login node of the current tab (worker)"""
  from .remote import run_cmd
  if ctx.script is None:
    return None
  res = run_cmd(["bash", "-lc", replay_script(ctx)], timeout=120)
  out = parse_replay(res.stdout or "")
  if res.returncode not in (0, 1) and not out["libs"] and res.stderr:
    out["errors"].append(res.stderr.strip()[:200])
  return out
