"""The environment check: replay the setup lines of a batch script on the login node (module loads, exports, sources, cd,
plain assignments) and look at the programs it runs: their shared libraries (ldd), their GPU code (cuobjdump).

Only setup lines run, never the job's commands; lines with command substitutions ($(...), backticks) are skipped, so
nothing but the module system and the sourced files runs. The result feeds rules P05, P06, E04 and B02.

The job's software may come from a site option instead: a uenv (sbatch --uenv, --view, on Alps) is started for the
replay as for the job (uenv run, the replay inside it); in a container (--environment, --container-image) the
programs are not looked at, since the login node does not have what the container has."""

import re
import shlex

from .engine import strip_comment, referenced_paths, launched_program, LAUNCHERS, SKIP_PROGRAMS

SETUP = re.compile(r"^\s*(module\s|ml\s|source\s|\.\s|export\s|cd\s|unset\s|[A-Za-z_]\w*=|"
                   r"(conda|mamba|micromamba)\s+activate\s|spack\s+(load|env\s+activate)\s)")

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

CONTAINER_OPTIONS = ("environment", "container-image", "container") # (Alps' container engine, pyxis)

def container_option(ctx):
  """The option that runs the job in a container, if any"""
  return next((o for o in CONTAINER_OPTIONS if ctx.opt(o) is not None), None)

def login_shell(ctx):
  """The job script runs as a login shell (#!/bin/bash -l)"""
  words = ctx.script.shebang[2:].split() if ctx.script and ctx.script.shebang else []
  return any(w in ("-l", "--login") or w.startswith("-") and not w.startswith("--") and "l" in w for w in words[1:])

def replay_command(ctx):
  """The shell code the login shell runs: the replay, inside the job's uenv if it has one (--uenv, --view: as the job,
  the replay runs in it, its views set up and its modules there); in a container without the programs"""
  container = container_option(ctx)
  if container:
    return f"printf '@@CONTAINER %s\\n' {shlex.quote(container)}\n" + replay_script(ctx, run_programs=False)
  uenv = ctx.opt("uenv")
  if not uenv:
    return replay_script(ctx)
  view = ctx.opt("view")
  inner = "printf '@@UENVSTARTED\\n'\n" + replay_script(ctx)
  run = ["uenv", "run", *([f"--view={view}"] if view else []), uenv, "--", "bash", "-lc" if login_shell(ctx) else "-c", inner]
  return ("if command -v uenv >/dev/null 2>&1; then " + shlex.join(run) + "; "
          "else printf '@@NOUENV\\n'; fi\n")

def replay_script(ctx, run_programs=True):
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
  for prog in programs(ctx) if run_programs else []:
    q = shlex.quote(prog)
    lines.append(f"p={q}; case \"$p\" in */*) ;; *) p=$(command -v \"$p\" 2>/dev/null) || printf '@@NOTFOUND %s\\n' {q} ;; esac; "
                 f"printf '@@PROG %s\\t%s\\n' {q} \"$p\"; "
                 f"if [ -f \"$p\" ]; then ldd \"$p\" 2>&1 | head -200; "
                 f"command -v cuobjdump >/dev/null 2>&1 && cuobjdump --list-elf \"$p\" 2>/dev/null | head -50 | sed 's/^/@@CUDA /'; fi")
  return "\n".join(lines) + "\ntrue\n"

def parse_replay(text):
  out = {"libs": {}, "missing": {}, "cuda": {}, "errors": [], "module_errors": [], "resolved": {}, "notfound": [],
         "fresh": False, "uenv_started": False, "no_uenv": False, "container": None}
  prog = None
  for line in text.splitlines():
    if line.startswith("@@UENVSTARTED"):
      out["uenv_started"] = True
      continue
    if line.startswith("@@NOUENV"):
      out["no_uenv"] = True
      continue
    if line.startswith("@@CONTAINER "):
      out["container"] = line[12:].strip()
      continue
    if line.startswith("@@NOTFOUND "):
      out["notfound"].append(line[11:].strip())
      prog = None
      continue
    if line.startswith("@@PROG "):
      name, _, path = line[7:].partition("\t")
      prog = name
      out["resolved"][name] = path
      out["libs"][name] = [] if path else None # (not found: nothing known of it)
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
  from ..cluster.remote import run_cmd
  if ctx.script is None:
    return None
  # as after a fresh login (G01): a clean environment, a login shell, then only what the script sets up itself
  env = ctx.facts.env if ctx.facts else {}
  home, user = env.get("HOME", ""), env.get("USER") or env.get("LOGNAME") or (ctx.facts.user if ctx.facts else "")
  fresh = ["env", "-i", *[f"{k}={v}" for k, v in (("HOME", home), ("USER", user), ("LOGNAME", user)) if v],
           "PATH=/usr/local/bin:/usr/bin:/bin", "TERM=dumb", "LANG=C.UTF-8"]
  return replay_result(ctx, run_cmd([*fresh, "bash", "-lc", replay_command(ctx)], timeout=180))

def replay_result(ctx, res):
  """What the replay found (res: the CompletedProcess of replay_command)"""
  out = parse_replay(res.stdout or "")
  out["fresh"] = True
  uenv = ctx.opt("uenv")
  if uenv and not out["container"]:
    out["uenv"] = uenv
    if out["no_uenv"]:
      out["uenv_error"] = "the login node has no uenv command"
    elif not out["uenv_started"]:
      why = " ".join((res.stderr or "").strip().splitlines()[-2:])[:200]
      out["uenv_error"] = f"uenv run {uenv} failed" + (f": {why}" if why else "")
    return out
  if res.returncode not in (0, 1) and not out["libs"] and res.stderr:
    out["errors"].append(res.stderr.strip()[:200])
  return out
