"""Checks of batch scripts and jobs: beginner mistakes, Slurm pitfalls, requests that make no sense, missing paths and
programs, quota, environment, placement, and what a job really used.

The rules are listed in RULES with their explanation (the rule catalogue, rules_markdown()). A rule is a function of a
Context that yields Findings. A context holds what is known: the parsed script, the job record, facts about the
cluster (partitions, accounts, sbatch options, environment), the results of the remote probe (paths, programs), the
quota, the tails of the job's logs. Rules skip what they cannot know.

Muting: "checks_muted" in the config (rule ids), or a comment in the script:  # slurp: ignore R08 S07"""

import difflib
import math
import os
import re
import shlex
import string
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

ERROR, WARNING, INFO = "error", "warning", "info"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}
SEVERITY_ICON = {ERROR: "✖", WARNING: "⚠", INFO: "ℹ"}
SEVERITY_STYLE = {ERROR: "bold red", WARNING: "yellow", INFO: "cyan"}

CATEGORIES = {
  "S": "Script syntax and hygiene",
  "R": "Resources, and requests that make no sense",
  "B": "Beginner mistakes and classic traps",
  "P": "Paths, files and programs",
  "Q": "Quota and storage",
  "E": "Environment and software stack",
  "N": "NUMA placement and binding",
  "U": "Usage and outcome",
  "G": "Good practice",
}

# ---------------------------------------------------------------------------
# rules

@dataclass
class Rule:
  id: str
  severity: str           # the usual severity (a finding may say otherwise)
  title: str
  what: str               # what the rule finds
  implies: str            # why it matters
  fix: str                # how to solve it
  example: str = ""       # bad / good, shown in the catalogue
  needs: str = "script"   # what it needs: script, job, probe, quota, logs, live
  check: object = None
  stack: str = "any"      # the software stack it is about: "any", "modules" or "uenv" (STACKS)

  @property
  def category(self):
    return self.id[0]

  @property
  def heading(self):
    return f"{self.id} {self.title}"

  @property
  def anchor(self):
    return slug(self.heading)

# The anchor of a heading as Textual's Markdown viewer makes it (goto_anchor): a copy of Textual's slug function (Textual 8,
# MIT; private there), so the checks import no Textual. tests/unit/test_slug.py compares the two.
_SLUG_STRIP = re.compile("[" + string.punctuation.replace("-", "").replace("_", "") + r"\U000024C2-\U0001F251\U00002702-\U000027B0"
                         r"\U0001F1E0-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF\u200D\u2640-\u2642]+")

def slug(text):
  """GitHub-flavoured Markdown's slug of a heading, as Textual makes it"""
  return urllib.parse.quote(re.sub(r"\s", "-", _SLUG_STRIP.sub("", text.strip().lower())))

RULES = {}

# The software stacks of clusters: some rules are about one of them only, and apply where it is (Facts.stacks)
STACKS = {"modules": "clusters with environment modules (module load)",
          "uenv": "clusters with uenv (--uenv, --view)"}

def rule(id, severity, title, what, implies, fix, example="", needs="script", stack="any"):
  def register(fn):
    RULES[id] = Rule(id, severity, title, what, implies, fix, example, needs, fn, stack)
    return fn
  return register

@dataclass
class Finding:
  rule: Rule
  evidence: str               # what was found, concretely
  line: int | None = None     # 1-based line of the script
  severity: str = ""          # "" : the rule's
  fix: str = ""               # concrete fix (else the rule's)
  snippet: str = ""           # code that fixes it

  def __post_init__(self):
    self.severity = self.severity or self.rule.severity

  @property
  def icon(self):
    return SEVERITY_ICON[self.severity]

  def sort_key(self):
    return (SEVERITY_ORDER[self.severity], self.line or 0, self.rule.id)

def counts(findings):
  out = {ERROR: 0, WARNING: 0, INFO: 0}
  for f in findings or ():
    out[f.severity] += 1
  return out

def summary(findings):
  """'✖1 ⚠2' (no infos: they are advice, not problems)"""
  c = counts(findings)
  return " ".join(f"{SEVERITY_ICON[s]}{c[s]}" for s in (ERROR, WARNING) if c[s])

# ---------------------------------------------------------------------------
# the script

SHORT_OPTIONS = {
  "A": "account", "a": "array", "B": "extra-node-info", "b": "begin", "C": "constraint", "c": "cpus-per-task",
  "D": "chdir", "d": "dependency", "e": "error", "F": "nodefile", "G": "gpus", "H": "hold", "h": "help",
  "i": "input", "J": "job-name", "k": "no-kill", "L": "licenses", "M": "clusters", "m": "distribution",
  "N": "nodes", "n": "ntasks", "O": "overcommit", "o": "output", "p": "partition", "Q": "quiet", "q": "qos",
  "S": "core-spec", "s": "oversubscribe", "t": "time", "V": "version", "v": "verbose", "W": "wait",
  "w": "nodelist", "x": "exclude",
}
FLAG_OPTIONS = {"exclusive", "hold", "no-kill", "overcommit", "oversubscribe", "quiet", "requeue", "no-requeue",
                "test-only", "wait", "verbose", "contiguous", "spread-job", "use-min-nodes", "parsable", "help",
                "usage", "version", "ignore-pbs", "reboot", "nice", "kill-on-invalid-dep", "get-user-env", "wrap"}
LONG_OPTIONS = {
  "account", "acctg-freq", "array", "batch", "bb", "bbf", "begin", "chdir", "cluster-constraint", "clusters",
  "comment", "constraint", "container", "container-id", "contiguous", "core-spec", "cores-per-socket", "cpu-freq",
  "cpus-per-gpu", "cpus-per-task", "deadline", "delay-boot", "dependency", "distribution", "error", "exclude",
  "exclusive", "export", "export-file", "extra", "extra-node-info", "get-user-env", "gid", "gpu-bind", "gpu-freq",
  "gpus", "gpus-per-node", "gpus-per-socket", "gpus-per-task", "gres", "gres-flags", "help", "hint", "hold",
  "ignore-pbs", "input", "job-name", "kill-on-invalid-dep", "licenses", "mail-type", "mail-user", "mcs-label", "mem",
  "mem-bind", "mem-per-cpu", "mem-per-gpu", "mincpus", "network", "nice", "no-kill", "no-requeue", "nodefile",
  "nodelist", "nodes", "ntasks", "ntasks-per-core", "ntasks-per-gpu", "ntasks-per-node", "ntasks-per-socket",
  "open-mode", "output", "overcommit", "oversubscribe", "parsable", "partition", "power", "prefer", "priority",
  "profile", "propagate", "qos", "quiet", "reboot", "requeue", "reservation", "resv-ports", "segment", "signal",
  "sockets-per-node", "spread-job", "switches", "test-only", "thread-spec", "threads-per-core", "time", "time-min",
  "tmp", "tres-bind", "tres-per-task", "uid", "usage", "use-min-nodes", "verbose", "version", "wait", "wait-all-nodes",
  "wckey", "wrap", "x11", "cpu-bind", "mem_bind", "stepmgr", "oom-kill-step",
}

@dataclass
class Directive:
  line: int        # 1-based
  raw: str
  option: str      # long name without dashes ("" if not parseable)
  value: str | None
  written: str     # as written ("-N", "--nodes")
  ignored: bool    # after the first command: Slurm ignores it

@dataclass
class Command:
  line: int        # 1-based line where it starts
  text: str        # with continuation lines joined
  depth: int = 0   # inside loops (for/while/until)

class Script:
  """A batch script, parsed as far as the checks need it"""
  def __init__(self, text, path=""):
    self.text, self.path = text, path
    self.raw_lines = text.split("\n")
    self.lines = [l.rstrip("\r") for l in self.raw_lines]
    self.shebang = self.lines[0] if self.lines and self.lines[0].startswith("#!") else ""
    self.directives, self.commands, self.near_directives = [], [], []
    self.first_command = None
    self.ignored_rules = set()
    depth = 0
    pending, start = "", 0
    for i, line in enumerate(self.lines, 1):
      stripped = line.strip()
      m = re.search(r"#\s*slurp:\s*ignore\s+([A-Z]\d\d(?:[\s,]+[A-Z]\d\d)*)", line)
      if m:
        self.ignored_rules |= set(re.findall(r"[A-Z]\d\d", m.group(1)))
      if pending:
        pending += " " + stripped
        if not stripped.endswith("\\"):
          self.add_command(start, pending, depth)
          depth = self.depth_after(pending, depth)
          pending = ""
        else:
          pending = pending[:-1]
        continue
      if i == 1 and stripped.startswith("#!"):
        continue
      if stripped.startswith("#SBATCH"):
        self.add_directive(i, stripped, ignored=self.first_command is not None)
        continue
      if stripped.startswith("#"):
        # a directive gone wrong: "# SBATCH -", "#sbatch -", "#SBTACH -" (followed by an option, unlike a comment)
        m = re.match(r"#(\s*)([A-Za-z]{4,8})\s+-", stripped)
        if m:
          space, word = m.groups()
          if (word == "SBATCH" and space) or (word.lower() == "sbatch" and word != "SBATCH" and not space) or \
             (word.upper() != "SBATCH" and not space and difflib.SequenceMatcher(None, word.upper(), "SBATCH").ratio() >= 0.75):
            self.near_directives.append((i, stripped))
        continue
      if not stripped:
        continue
      if self.first_command is None:
        self.first_command = i
      if stripped.endswith("\\"):
        pending, start = stripped[:-1], i
        continue
      self.add_command(i, stripped, depth)
      depth = self.depth_after(stripped, depth)
    if pending:
      self.add_command(start, pending, depth)

  @staticmethod
  def depth_after(text, depth):
    for w in loop_words(text):
      depth += -1 if w == "done" else 1
    return max(depth, 0)

  def add_command(self, line, text, depth):
    # a loop header on the same line ("for f in *; do srun ...; done") counts as inside the loop
    inline_loop = re.search(r"(?:^|[;&|({]|\b(?:then|else|do)\b|!)\s*(for|while|until)\b.*\bdo\b(.*)", code_only(text))
    self.commands.append(Command(line, text, depth + (1 if inline_loop and inline_loop.group(2).strip() else 0)))

  def add_directive(self, line, stripped, ignored):
    rest = stripped[len("#SBATCH"):].strip()
    rest = re.split(r"\s+#", rest, 1)[0].strip() # a trailing comment
    try:
      words = shlex.split(rest)
    except ValueError:
      words = rest.split()
    if not words:
      self.directives.append(Directive(line, stripped, "", None, "", ignored))
      return
    for option, value, written in parse_options(words):
      self.directives.append(Directive(line, stripped, option, value, written, ignored))

  # --- helpers for the rules
  def options(self, include_ignored=False):
    """{option: (value, directive)}: the last one wins, as in Slurm"""
    out = {}
    for d in self.directives:
      if d.option and (include_ignored or not d.ignored):
        out[d.option] = (d.value, d)
    return out

  def code_lines(self):
    """(line, text) of the commands, without comments at the end"""
    for c in self.commands:
      yield c.line, strip_comment(c.text)

  def uses(self, pattern):
    return any(re.search(pattern, t) for _, t in self.code_lines())

  def first_line(self, pattern):
    for line, t in self.code_lines():
      if re.search(pattern, t):
        return line
    return None

  def assignments(self):
    """{name: (value, line)} of NAME=value and export NAME=value (the last one)"""
    out = {}
    for line, t in self.code_lines():
      for m in re.finditer(r"(?:^|[;&|]\s*|\bexport\s+|\blocal\s+|\bdeclare\s+(?:-\w+\s+)*|\breadonly\s+)([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|[^\s;&|]*)", t):
        out[m.group(1)] = (m.group(2).strip("\"'"), line)
      for m in re.finditer(r"\bfor\s+([A-Za-z_]\w*)\s+in\b", t):
        out.setdefault(m.group(1), ("", line))
      for m in re.finditer(r"\bread\s+(?:-\w+\s+)*([A-Za-z_][\w\s]*)", t):
        for name in m.group(1).split():
          out.setdefault(name, ("", line))
      for m in re.finditer(r"\bexport\s+([A-Za-z_]\w*)(?=\s|;|$)", t):
        out.setdefault(m.group(1), ("", line))
    return out

  def launcher_lines(self):
    """(line, text, launcher) of srun / mpirun / mpiexec calls"""
    for line, t in self.code_lines():
      for m in re.finditer(r"(?:^|[\s;&|(`])(srun|mpirun|mpiexec|mpiexec\.hydra|aprun)(?=\s|$)", t):
        yield line, t[m.start(1):], m.group(1)
        break

def code_only(text):
  """The command without its comment, its quoted strings blanked ("…" and '…' as ""), to look for shell words"""
  return re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", '""', strip_comment(text))

# a loop's words where a command starts (not in a comment or a string, not an argument: echo done, "wait for it")
LOOP_WORD = re.compile(r"(?:^|[;&|({]|\b(?:do|then|else|elif)\b|!)\s*(for|while|until|done)(?=[\s;)]|$)")

def loop_words(text):
  """for, while, until and done of a command line, in order"""
  return LOOP_WORD.findall(code_only(text))

def strip_comment(text):
  """The command without a trailing # comment (not inside quotes, not ${#...} or $#)"""
  out, quote = [], None
  for i, ch in enumerate(text):
    if quote:
      if ch == quote:
        quote = None
    elif ch in "'\"":
      quote = ch
    elif ch == "#" and (i == 0 or text[i - 1] in " \t;") :
      break
    out.append(ch)
  return "".join(out).rstrip()

def parse_options(words):
  """[(long option, value, as written)] of sbatch/srun options"""
  out, i = [], 0
  while i < len(words):
    w = words[i]
    if w.startswith("--"):
      name, eq, value = w[2:].partition("=")
      if not eq and name not in FLAG_OPTIONS and i + 1 < len(words) and not words[i + 1].startswith("-"):
        value = words[i + 1]
        i += 1
      out.append((name, value if (eq or value) else None, "--" + name))
    elif w.startswith("-") and len(w) >= 2 and not w[1].isdigit():
      letter = w[1]
      name = SHORT_OPTIONS.get(letter, "-" + letter)
      value = w[2:]
      if value.startswith("="):
        value = value[1:]
      if not value and name not in FLAG_OPTIONS and i + 1 < len(words) and not words[i + 1].startswith("-"):
        value = words[i + 1]
        i += 1
      out.append((name, value or None, "-" + letter))
    i += 1
  return out

def submit_options(submit_line):
  """{option: value} of an sbatch command line (before the script name)"""
  try:
    words = shlex.split(submit_line or "")
  except ValueError:
    words = (submit_line or "").split()
  if not words or os.path.basename(words[0]) != "sbatch":
    return {}, None
  opts, script = {}, None
  i = 1
  parsed = []
  while i < len(words):
    w = words[i]
    if not w.startswith("-"):
      script = w
      break
    j = i + 1
    if "=" not in w and (w.startswith("--") and w[2:] not in FLAG_OPTIONS or not w.startswith("--") and len(w) == 2
                         and SHORT_OPTIONS.get(w[1], "") not in FLAG_OPTIONS) and j < len(words):
      parsed.append([w, words[j]])
      i = j + 1
    else:
      parsed.append([w])
      i += 1
  for chunk in parsed:
    for option, value, _ in parse_options(chunk):
      opts[option] = value
  return opts, script

# ---------------------------------------------------------------------------
# values

PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\{%|<[A-Za-z_][\w -]*>|@[A-Za-z_]+@|__[A-Z][A-Z_]+__|\$\{\{")

def is_placeholder(value):
  """A value of a script template ({{account}}, <time>, @NODES@): filled in later, not checked"""
  return bool(value and PLACEHOLDER.search(str(value)))

def parse_time(value):
  """Minutes of a Slurm time ("60", "1:00" (mm:ss), "1:00:00", "1-12", "1-12:00", "1-12:00:00"), None if unknown"""
  if not value:
    return None
  v = value.strip()
  if v.upper() in ("INFINITE", "UNLIMITED"):
    return math.inf
  m = re.fullmatch(r"(?:(\d+)-)?(\d+)(?::(\d+))?(?::(\d+))?", v)
  if not m:
    return None
  days, a, b, c = m.groups()
  if days is not None:
    hours = int(a)
    minutes = int(b or 0)
    seconds = int(c or 0)
    return int(days) * 1440 + hours * 60 + minutes + seconds / 60
  if c is not None:
    return int(a) * 60 + int(b) + int(c) / 60
  if b is not None:
    return int(a) + int(b) / 60 # minutes:seconds
  return int(a)

def time_text(minutes):
  if minutes is None:
    return "?"
  if minutes == math.inf:
    return "unlimited"
  minutes = int(round(minutes))
  d, rest = divmod(minutes, 1440)
  h, m = divmod(rest, 60)
  return (f"{d}-" if d else "") + f"{h:02d}:{m:02d}:00"

MEM_UNITS = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}

def parse_mem(value):
  """(MB, has a unit), None if unknown"""
  if value is None:
    return None
  m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTkmgt])?[Bb]?\s*", value)
  if not m:
    return None
  number, unit = float(m.group(1)), (m.group(2) or "").upper()
  return number * MEM_UNITS.get(unit or "M", 1), bool(unit)

def mem_text(mb):
  if mb is None:
    return "?"
  if mb >= 1024 * 1024:
    return f"{mb / 1024 / 1024:.3g} TB"
  if mb >= 1024:
    return f"{mb / 1024:.3g} GB"
  return f"{mb:.3g} MB"

def to_int(value, default=None):
  try:
    return int(str(value).split("(")[0].strip())
  except (TypeError, ValueError):
    return default

def gres_gpus(gres):
  """GPUs per node in a gres string ("gpu:4(S:0-3)", "gpu:a100:4", "gres/gpu=4")"""
  if not gres or gres in ("(null)", "N/A"):
    return 0
  total = 0
  for part in re.split(r",(?![^(]*\))", gres):
    m = re.match(r"(?:gres/)?gpu(?::[\w.-]+)?[:=](\d+)", part.strip())
    if m:
      total += int(m.group(1))
  return total

# ---------------------------------------------------------------------------
# facts about the cluster (cached per tab)

FACTS_TTL = 600
_facts, _facts_lock = {}, threading.Lock()

@dataclass
class Partition:
  name: str
  default: bool = False
  max_minutes: float | None = None
  cpus: int = 0            # CPUs per node (hardware threads)
  mem_mb: int = 0
  gpus: int = 0
  sockets: int = 0
  cores: int = 0           # per socket
  threads: int = 1         # per core
  exclusive: bool = False  # whole nodes only (OverSubscribe=EXCLUSIVE)
  available: bool = True

  @property
  def cores_per_node(self):
    return self.sockets * self.cores if self.sockets and self.cores else self.cpus // max(self.threads, 1)

@dataclass
class Facts:
  partitions: dict = field(default_factory=dict)
  accounts: list = field(default_factory=list)
  qos: set = field(default_factory=set)
  default_account: str = ""
  sbatch_options: set = field(default_factory=set)   # from sbatch --help (includes site plugins)
  slurm_version: tuple = ()
  arch: str = ""
  env: dict = field(default_factory=dict)            # of a login shell
  bashrc: list = field(default_factory=list)         # (line, text) of module/conda lines in ~/.bashrc
  user: str = ""
  site: dict = field(default_factory=dict)           # the rule pack
  stacks: set = field(default_factory=set)           # the software stacks there: "modules", "uenv" (empty: unknown)
  core: dict = field(default_factory=dict)           # core files: "soft", "hard" (ulimit -c of a login shell), "pattern" (kernel)
  time: float = 0

  def partition(self, name=None):
    if name:
      return self.partitions.get(name)
    return next((p for p in self.partitions.values() if p.default), None)

FACTS_SINFO = ["sinfo", "-h", "-o", "%P|%l|%c|%m|%G|%X|%Y|%Z|%h|%a"]

def parse_sinfo(text):
  parts = {}
  for line in text.splitlines():
    f = line.split("|")
    if len(f) < 10:
      continue
    name = f[0].rstrip("*")
    p = parts.get(name) or Partition(name, default=f[0].endswith("*"))
    p.max_minutes = parse_time(f[1]) if f[1] not in ("", "n/a") else None
    p.cpus = max(p.cpus, to_int(f[2].rstrip("+"), 0))
    p.mem_mb = max(p.mem_mb, to_int(f[3].rstrip("+"), 0))
    p.gpus = max(p.gpus, gres_gpus(f[4]))
    p.sockets = max(p.sockets, to_int(f[5], 0))
    p.cores = max(p.cores, to_int(f[6], 0))
    p.threads = max(p.threads, to_int(f[7], 1))
    p.exclusive = f[8].upper().startswith("EXCLUSIVE")
    p.available = f[9] == "up"
    parts[name] = p
  return parts

def parse_sbatch_help(text):
  return set(re.findall(r"--([a-z][a-z0-9_-]+)", text))

def gather_facts(force=False):
  """Facts about the cluster of the current tab (worker; cached for FACTS_TTL)"""
  from ..cluster.remote import run_many, tab_name, conn
  from ..cluster.jobs import current_user
  key = tab_name()
  with _facts_lock:
    f = _facts.get(key)
    if f and not force and time.time() - f.time < FACTS_TTL:
      return f
  user = current_user()
  cmds = [FACTS_SINFO,
          ["sacctmgr", "-nP", "show", "associations", f"user={user}", "format=account,partition,qos"],
          ["sbatch", "--help"],
          ["sbatch", "--version"],
          ["uname", "-m"],
          ["bash", "-lc", "env"],
          ["sh", "-c", "grep -nE '^[^#]*(module +(load|add|swap)|^ *ml +[A-Za-z]|conda +activate|source +activate|uenv +start)' "
                       "\"$HOME/.bashrc\" \"$HOME/.bash_profile\" 2>/dev/null | head -10"],
          ["sacctmgr", "-nP", "show", "user", user, "format=defaultaccount"],
          ["sh", "-c", "command -v uenv"],
          ["bash", "-lc", CORE_FACTS]]
  res = run_many(cmds, timeout=60)
  f = Facts(time=time.time(), user=user)
  f.partitions = parse_sinfo(res[0].stdout)
  accounts, qos = [], set()
  for line in res[1].stdout.splitlines():
    a = line.split("|")
    if a and a[0] and a[0] not in accounts:
      accounts.append(a[0])
    if len(a) > 2:
      qos |= {q for q in a[2].split(",") if q}
  f.accounts, f.qos = accounts, qos
  f.sbatch_options = parse_sbatch_help(res[2].stdout)
  m = re.search(r"(\d+)\.(\d+)", res[3].stdout)
  f.slurm_version = (int(m.group(1)), int(m.group(2))) if m else ()
  f.arch = res[4].stdout.strip()
  for line in res[5].stdout.splitlines():
    name, eq, value = line.partition("=")
    if eq and re.fullmatch(r"[A-Za-z_]\w*", name):
      f.env[name] = value
  for line in res[6].stdout.splitlines():
    f.bashrc.append(line.strip())
  f.default_account = res[7].stdout.strip().splitlines()[0].strip() if res[7].stdout.strip() else ""
  from .. import sites
  f.site = sites.pack_for(key, getattr(conn(), "destination", "") or "", f)
  f.stacks = detect_stacks(f, bool(res[8].stdout.strip()))
  f.core = parse_core_facts(res[9].stdout if len(res) > 9 else "")
  with _facts_lock:
    _facts[key] = f
  return f

# Core files: the limit of a login shell (Slurm passes the limits of the shell sbatch runs in on to the job:
# PropagateResourceLimits, ALL by default) and where the kernel writes them (a name: in the working directory of the
# program; /path: there; |program: handed to a program, systemd-coredump, apport, …; of this node: compute nodes alike)
CORE_FACTS = 'echo "slurp-core-soft=$(ulimit -Sc)"; echo "slurp-core-hard=$(ulimit -Hc)"; ' \
             'echo "slurp-core-pattern=$(cat /proc/sys/kernel/core_pattern 2>/dev/null)"'

def parse_core_facts(text):
  """{"soft", "hard", "pattern"} of CORE_FACTS' output (a login shell may print more: its lines are left out)"""
  return {m.group(1): m.group(2).strip() for m in re.finditer(r"^slurp-core-(soft|hard|pattern)=(.*)$", text, re.M)}

def detect_stacks(facts, uenv_command=False):
  """The software stacks of a cluster: "modules" (Lmod or environment modules in a login shell), "uenv" (sbatch's
  --uenv option, or the uenv command); a rule pack's "stacks" says it instead"""
  if facts.site.get("stacks"):
    return set(facts.site["stacks"]) & set(STACKS)
  stacks = set()
  if any(facts.env.get(k) for k in ("LMOD_CMD", "MODULESHOME", "LMOD_DIR", "MODULEPATH")):
    stacks.add("modules")
  if "uenv" in facts.sbatch_options or uenv_command:
    stacks.add("uenv")
  return stacks

def cached_facts(tab):
  """The facts of a tab if they were gathered already (None: not yet), without asking the cluster"""
  with _facts_lock:
    return _facts.get(tab)

def uenv_views(value):
  """The view names of a --view value ("default", "prgenv-gnu:modules,spack": [default] / [modules, spack])"""
  return [part.split(":")[-1].strip() for part in (value or "").split(",") if part.strip()]

def rule_active(r, facts, ctx=None):
  """A rule applies on the cluster: it is about any stack, or one the cluster has (unknown stacks: all apply); module
  rules also where the script's uenv brings its modules (--view=modules), in part"""
  if r.stack == "any" or facts is None or not facts.stacks or r.stack in facts.stacks:
    return True
  return r.stack == "modules" and ctx is not None and ctx.uenv_modules()

LOGIN_IGNORE = {"ssh", "sshd", "bash", "zsh", "sh", "tmux", "screen", "sftp-server", "sleep", "ps", "less", "vim",
                "vi", "nano", "emacs", "top", "htop", "git", "systemd", "dbus-daemon", "(sd-pam)", "mosh-server"}

def login_hogs(min_cpu=50, min_seconds=600):
  """Your processes on the login node that use a lot of CPU for a while: [(pid, cpu %, minutes, command)] (worker)"""
  from ..cluster.remote import run
  out = run(["sh", "-c", 'ps -u "$(id -un)" -o pid=,pcpu=,etimes=,comm='], quiet=True)
  hogs = []
  for line in out.splitlines():
    f = line.split(None, 3)
    if len(f) < 4:
      continue
    try:
      pid, cpu, secs, comm = int(f[0]), float(f[1]), int(f[2]), f[3].strip()
    except ValueError:
      continue
    if cpu >= min_cpu and secs >= min_seconds and comm not in LOGIN_IGNORE:
      hogs.append((pid, cpu, secs // 60, comm))
  return hogs

def known_options(facts):
  extra = facts.sbatch_options if facts else set()
  site = set((facts.site or {}).get("options", [])) if facts else set()
  return LONG_OPTIONS | extra | site

# ---------------------------------------------------------------------------
# the remote probe: paths and programs in one round trip

PROBE_SCRIPT = r"""
for a in "$@"; do
  k=${a%%:*}; p=${a#*:}
  if [ -e "$p" ]; then
    t=f; [ -d "$p" ] && t=d
    x=0; [ -x "$p" ] && x=1
    w=0; [ -w "$p" ] && w=1
    r=0; [ -r "$p" ] && r=1
    s=$(stat -L -c '%s|%Y' -- "$p" 2>/dev/null)
    info=""
    if [ "$k" = x ] && [ "$t" = f ]; then info=$(file -L -b -- "$p" 2>/dev/null | head -c 160); fi
    if [ "$k" = n ] && [ "$t" = d ]; then info=$(find "$p" -maxdepth 1 2>/dev/null | head -100001 | wc -l); fi
    if [ "$k" = c ] && [ "$t" = d ]; then info=$(find "$p" -maxdepth 1 -type f \( -name core -o -name 'core.[0-9]*' \) -printf '%s %T@\n' 2>/dev/null | awk '{n++; s+=$1; if ($2>m) m=$2} END {printf "%d %d %d", n, s, m}'); fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$a" "$t" "$x" "$w" "$s" "$info" "$r"
  else
    printf '%s\t-\n' "$a"
  fi
done
"""

@dataclass
class PathInfo:
  exists: bool
  isdir: bool = False
  executable: bool = False
  writable: bool = False
  size: int = 0
  mtime: float = 0
  file: str = ""
  readable: bool = True # (a directory: its names can be listed; executable: it can be entered)

def run_probe(requests):
  """{"kind:path": PathInfo} (worker)"""
  from ..cluster.remote import run_cmd
  requests = list(dict.fromkeys(requests))
  out = {}
  for i in range(0, len(requests), 200):
    chunk = requests[i:i + 200]
    res = run_cmd(["sh", "-c", PROBE_SCRIPT, "sh", *chunk], timeout=60)
    for line in res.stdout.splitlines():
      f = line.split("\t")
      if len(f) < 2:
        continue
      if f[1] == "-":
        out[f[0]] = PathInfo(False)
      elif len(f) >= 5:
        size, _, mtime = f[4].partition("|")
        out[f[0]] = PathInfo(True, f[1] == "d", f[2] == "1", f[3] == "1", to_int(size, 0), float(mtime or 0),
                             f[5] if len(f) > 5 else "", f[6] != "0" if len(f) > 6 else True)
  return out

# ---------------------------------------------------------------------------
# the context of a check

@dataclass
class Context:
  script: Script | None = None
  job: object = None             # jobs.Job
  facts: Facts | None = None
  probes: dict | None = None     # "kind:path" -> PathInfo (None: not probed)
  quota: dict | None = None      # storage.quota_info()
  logs: dict = field(default_factory=dict)   # "stdout"/"stderr" -> tail text
  on_disk: str | None = None     # the script file as it is now (a queued job)
  submitted: str | None = None   # the script as Slurm stored it
  replay: dict | None = None     # result of the environment replay (ldd)
  live: dict | None = None       # placement of a running job
  login: list | None = None      # heavy processes on the login node (B09)
  others: list = field(default_factory=list) # the other jobs in the list (duplicates)
  submit_line: str = ""
  count_files: bool = False                   # probe the number of files in the working directory (Q06)
  uncertain: set = field(default_factory=set) # paths resolved from a guessed directory (no job, relative)
  arguments: dict = field(default_factory=dict) # argument paths of programs (P10): abspath -> {word, flag, output}
  submit_env: dict | None = None              # the environment the job was submitted with (sacct --env-vars)
  script_dir: str = ""                        # the directory of the script file (a job: from its submit line)
  env: dict = field(default_factory=dict)     # for $VARS in paths
  muted: set = field(default_factory=set)

  # --- derived
  def options(self):
    """Effective options: the script's directives, overridden by the submit line"""
    if hasattr(self, "_options"):
      return self._options
    opts = {k: v for k, (v, _) in self.script.options().items()} if self.script else {}
    sub, _ = submit_options(self.submit_line)
    opts.update(sub)
    self._options = opts
    return opts

  def opt(self, *names):
    opts = self.options()
    for n in names:
      if n in opts:
        return opts[n] if opts[n] is not None else ""
    return None

  def directive_line(self, *names):
    if not self.script:
      return None
    found = None
    for d in self.script.directives:
      if d.option in names and not d.ignored:
        found = d.line
    return found

  def partition(self):
    if not self.facts:
      return None
    name = self.opt("partition")
    if name and "," in name:
      name = name.split(",")[0]
    if not name and self.job is not None:
      name = self.job.rec.get("Partition")
    return self.facts.partition(name)

  def workdir(self):
    d = self.opt("chdir")
    if d and not os.path.isabs(d) and "$" not in d and not d.startswith("~"):
      if self.job is not None: # (Slurm's WorkDir is the --chdir, resolved already)
        d = self.job.rec.get("WorkDir") or d
      else: # relative to where it is submitted: the script's directory as the guess
        base = self.script_dir or (os.path.dirname(self.script.path) if self.script and self.script.path else "")
        d = os.path.normpath(os.path.join(base, d)) if base else d
    elif d and not os.path.isabs(d): # ($VARS, ~: expanded as the user meant them; S05 says Slurm does not)
      d = self.expand(d) or d
    if not d and self.job is not None:
      d = self.job.rec.get("WorkDir") or ""
    if not d and self.script and self.script.path:
      d = os.path.dirname(self.script.path)
    return d or ""

  def expand(self, text):
    """$VARS of the environment, the script's simple assignments, and of the job; None if some are unknown"""
    env = dict(self.env)
    if self.facts:
      env = {**self.facts.env, **env}
    if self.script:
      for name, (value, _) in self.script.assignments().items():
        if value and "$" not in value and "`" not in value:
          env.setdefault(name, value)
    def sub(m):
      name = m.group(1) or m.group(2)
      return env.get(name, "\0")
    out = re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, text)
    if out.startswith("~/") and "HOME" in env:
      out = env["HOME"] + out[1:]
    return None if "\0" in out or "$" in out or "`" in out else out

  def abspath(self, path):
    if is_placeholder(path):
      return None
    p = self.expand(path)
    if p is None:
      return None
    if not os.path.isabs(p):
      base = self.workdir()
      if not base:
        return None
      p = os.path.join(base, p)
    return os.path.normpath(p)

  def probe(self, kind, path):
    if self.probes is None or path is None:
      return None
    return self.probes.get(f"{kind}:{path}")

  def uenv_modules(self):
    """The script's uenv brings modules: --uenv with the modules view (--view=modules or <uenv>:modules)"""
    return self.opt("uenv") is not None and "modules" in uenv_views(self.opt("view"))

  def counts(self):
    """(nodes, tasks per node, cpus per task, gpus per node) as far as known"""
    nodes = to_int((self.opt("nodes") or "").split("-")[0], None)
    ntasks = to_int(self.opt("ntasks"), None)
    tpn = to_int(self.opt("ntasks-per-node"), None)
    cpt = to_int(self.opt("cpus-per-task"), 1) or 1
    if self.job is not None and nodes is None:
      nodes = to_int(self.job.rec.get("NNodes"), None)
    if tpn is None and ntasks is not None:
      tpn = math.ceil(ntasks / max(nodes or 1, 1))
    gpn = to_int(self.opt("gpus-per-node"), None)
    if gpn is None and self.opt("gres"):
      gpn = gres_gpus(self.opt("gres"))
    gpt = to_int(self.opt("gpus-per-task"), None)
    if gpn is None and gpt is not None:
      gpn = gpt * (tpn or 1)
    gpus = self.opt("gpus")
    if gpn is None and gpus:
      total = to_int(gpus.split(":")[-1], None)
      if total is not None:
        gpn = math.ceil(total / max(nodes or 1, 1))
    return nodes, tpn, cpt, gpn

def make_context(script_text=None, path="", job=None, facts=None, **kw):
  from ..config import load_config
  script = Script(script_text, path) if script_text else None
  muted = set((load_config().get("checks_muted") or []))
  if facts and facts.site:
    muted |= set(facts.site.get("muted", []))
  ctx = Context(script=script, job=job, facts=facts, muted=muted, **kw)
  if job is not None and not ctx.submit_line:
    ctx.submit_line = job.rec.get("SubmitLine") or ""
  if job is not None:
    from ..cluster.paths import job_vars
    env = job_vars(job)
    ctx.env = {k: v for k, v in env.items() if k.startswith("SLURM_") or k == "PWD"} | ctx.env
  return ctx

# ---------------------------------------------------------------------------
# what the probe needs

def interesting_paths(ctx):
  """[(kind, abspath)] the probe checks for this context: d (directory), x (program), f (file)"""
  out = []
  s = ctx.script
  if s is None:
    return out
  for opt in ("output", "error"):
    v = ctx.opt(opt)
    if v and v not in ("/dev/null",):
      p = ctx.abspath(output_pattern(v, ctx))
      if p:
        out.append(("d", os.path.dirname(p)))
        out.append(("f", p))
  chdir = ctx.opt("chdir")
  if chdir and ctx.expand(chdir) is not None and not is_placeholder(chdir):
    out.append(("d", ctx.workdir()))
  wd = ctx.workdir()
  if wd:
    out.append(("d", wd))
    if ctx.count_files:
      out.append(("n", wd))
  if s.shebang:
    interp = s.shebang[2:].strip().split()
    if interp and interp[0].startswith("/"):
      out.append(("x", interp[0]))
  if ctx.count_files or ctx.job is None: # (core files there: Q08)
    out += [("c", d) for d, _, _ in core_dirs(ctx)]
  for kind, path, _line in referenced_paths(ctx):
    out.append((kind, path))
    if kind == "a": # (P10: its directory too, a file the program writes needs one)
      out.append(("d", os.path.dirname(path)))
  submit, other = alternative_bases(ctx)
  if other and not ctx.opt("chdir"): # (G18: does it exist from the script's directory instead?)
    uses, anchored, _ = relative_uses(ctx)
    for _line, word, _kind in uses[:20]:
      out.append(("f", os.path.normpath(os.path.join(other, word))))
      if submit:
        out.append(("f", os.path.normpath(os.path.join(submit, word))))
  return list(dict.fromkeys(out))

# Where the programs of a job run: a crash writes its core file into the program's working directory (not where the
# program or the script is): --chdir or where sbatch was run, then the script's cd, srun --chdir
CD = re.compile(r"""^\s*cd(?:\s+(?:--\s+)?("[^"]*"|'[^']*'|[^\s;&|]+))?\s*(?:$|&&|\|\||;)""")
NOT_PROGRAMS = {"cd", "export", "unset", "module", "ml", "echo", "printf", "mkdir", "cp", "mv", "rm", "ln", "touch", "source",
                ".", "set", "ulimit", "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case",
                "esac", "date", "hostname", "sleep", "wait", "exit", "return", "true", "false", "cat", "ls", "pwd", "env",
                "uenv", "conda", "trap", "shift", "local", "declare", "[", "[[", "test", "{", "}", "tar", "sbatch"}
LAUNCH_DIR = re.compile(r"\s(?:--chdir[= ]|-D\s*)(\S+)")

def core_dirs(ctx):
  """[(directory, why the programs run there, line)] of the job's programs, as far as known (a script alone, without
  --chdir: where it will be submitted is not known; a cd to an unknown place: from there on not known)"""
  s = ctx.script
  if s is None:
    return []
  home = (ctx.facts.env.get("HOME") if ctx.facts else None) or ctx.env.get("HOME")
  wd, chdir = ctx.workdir(), ctx.opt("chdir")
  if chdir:
    cur = (wd, f"--chdir={chdir}", ctx.directive_line("chdir")) if wd and os.path.isabs(wd) else None
  elif ctx.job is not None and wd:
    cur = (wd, "sbatch was run there, without --chdir", None)
  else:
    cur = None
  out = []
  for line, t in s.code_lines():
    m = CD.match(t)
    if m:
      target = (m.group(1) or "~").strip("\"'")
      if target in ("~", "$HOME", "${HOME}"):
        path = home
      elif target == "-":
        path = None
      else:
        path = ctx.expand(target)
        if path and not os.path.isabs(path):
          path = os.path.normpath(os.path.join(cur[0], path)) if cur else None
      cur = (os.path.normpath(path), f"{t.strip()} (line {line})", line) if path else None
      continue
    words = t.split()
    if not words or "=" in words[0] and not words[0].startswith(("./", "/")) or words[0] in NOT_PROGRAMS:
      continue
    launched = LAUNCH_DIR.search(t) if re.match(r"\s*(srun|mpirun|mpiexec)\b", t) else None
    if launched:
      path = ctx.expand(launched.group(1).strip("\"'"))
      entry = (os.path.normpath(os.path.join(cur[0], path) if cur and not os.path.isabs(path) else path),
               f"its --chdir (line {line})", line) if path and (os.path.isabs(path) or cur) else None
    else:
      entry = cur
    if entry and entry[0] not in [d for d, _, _ in out]:
      out.append(entry)
  return out

def output_pattern(value, ctx):
  """An --output pattern with the job's values, %-patterns of unknown values as in a fresh job"""
  job = ctx.job
  rec = job.rec if job is not None else {}
  raw = rec.get("JobIDRaw") or "0"
  aid, _, task = (rec.get("JobID") or "").partition("_")
  repl = {"%j": raw, "%J": raw, "%A": aid or raw, "%a": task or "0", "%x": rec.get("JobName") or ctx.opt("job-name") or
          (os.path.basename(ctx.script.path) if ctx.script and ctx.script.path else "job"),
          "%u": ctx.facts.user if ctx.facts else os.environ.get("USER", ""), "%N": "node", "%n": "0", "%t": "0", "%s": "0",
          "%%": "%"}
  return re.sub(r"%(?:\d+)?[jJAaxuNnts%]", lambda m: repl.get("%" + m.group(0)[-1], m.group(0)), value)

LAUNCHERS = {"srun", "mpirun", "mpiexec", "mpiexec.hydra", "aprun"}
SKIP_PROGRAMS = {"echo", "cd", "export", "module", "ml", "source", ".", "set", "unset", "if", "then", "else", "fi",
                 "for", "do", "done", "while", "until", "case", "esac", "exit", "return", "wait", "sleep", "date",
                 "mkdir", "cp", "mv", "rm", "ln", "cat", "printf", "time", "env", "exec", "nohup", "ulimit", "umask",
                 "trap", "local", "declare", "readonly", "true", "false", "test", "[", "[[", "eval", "shift", "conda",
                 "uenv", "spack", "hostname", "pwd", "ls", "tar", "find", "grep", "sed", "awk", "head", "tail", "touch",
                 "chmod", "python", "python3", "bash", "sh", "numactl", "taskset", "nvidia-smi", "wc", "sort", "uniq",
                 "let", "function", "pip", "pip3", "rsync", "scp", "srun", "mpirun", "mpiexec", "unzip", "gzip",
                 "xargs", "seq", "basename", "dirname", "realpath", "readlink", "id", "whoami", "which", "type",
                 "command", "alias", "source", "srun", "sbatch", "scontrol", "squeue", "sacct", "scancel"}

def launcher_options(words):
  """The options of a launcher line (srun, mpirun: the words before the program; what follows belongs to the
  program: srun -n 4 ./sim -c 8 gives -n 4, the -c 8 is the program's)"""
  prog, args = launched_program(words)
  if prog is None:
    return words[1:]
  end = len(words) - len(args) - 1
  return words[1:end]

MPI_FLAGS = {"oversubscribe", "nooversubscribe", "display-map", "report-bindings", "tag-output", "timestamp-output",
             "v", "q", "quiet", "verbose", "use-hwthread-cpus", "allow-run-as-root", "display-allocation", "noprefix",
             "show-progress", "merge-stderr-to-stdout", "h", "help", "V", "version"}

def launched_program(words):
  """The program a launcher line runs (after its options)"""
  mpi = bool(words) and os.path.basename(words[0]) in ("mpirun", "mpiexec", "mpiexec.hydra", "aprun")
  i = 1
  while i < len(words):
    w = words[i]
    if w.startswith("-") and mpi and not w.startswith("--") and "=" not in w:
      # mpirun's options are single-dash words (-np 4, -host a,b, -x VAR): a value follows unless it is a known flag
      if w[1:] not in MPI_FLAGS and i + 1 < len(words) and not words[i + 1].startswith("-"):
        i += 2
      else:
        i += 1
      continue
    if w.startswith("-"):
      name = w.lstrip("-").split("=")[0]
      if "=" not in w and not (name in FLAG_OPTIONS or len(w) > 2 and w.startswith("-") and not w.startswith("--")) \
         and i + 1 < len(words) and not words[i + 1].startswith("-") and name not in ("exclusive", "overlap", "pty", "u", "l"):
        i += 2
        continue
      i += 1
      continue
    if re.fullmatch(r"[A-Za-z_]\w*=.*", w):
      i += 1
      continue
    return w, words[i + 1:]
  return None, []

SUBMIT_DIR = re.compile(r"^\$(?:SLURM_SUBMIT_DIR\b|\{SLURM_SUBMIT_DIR\})")
# the working directory of a launcher's tasks: srun --chdir/-D, mpirun -wdir/-wd/--wdir (relative: to the script's)
LAUNCHER_DIR_OPTIONS = {"--chdir", "-D", "-wdir", "--wdir", "-wd"}

def launcher_dir(words):
  """The working directory a launcher line gives its tasks (srun --chdir=x, -D x, -Dx; mpirun -wdir x), or None"""
  for i, w in enumerate(words[1:], 1):
    if not w.startswith("-"):
      return None # (the program: its own options are not the launcher's)
    name, eq, value = w.partition("=")
    if name in LAUNCHER_DIR_OPTIONS:
      return value if eq else (words[i + 1] if i + 1 < len(words) else None)
    if w.startswith("-D") and not w.startswith("--") and len(w) > 2:
      return w[2:]
  return None

# a program's argument that names a path (P10): a/b/c, ./in, ../data, ~/x, $SCRATCH/y, input.dat; not options, URLs,
# host:path or gpu:1, globs, lists (a,b), fractions (1/2), node-local places (/tmp, /dev, …: other on the nodes)
NODE_LOCAL = ("/tmp", "/dev", "/proc", "/sys", "/run", "/var/tmp")
OUTPUT_FLAG = re.compile(r"-{1,2}(?:o|(?:out|output|save|write|log|dump|result|dest|chk|checkpoint|ckpt|tmp|scratch|cache)[\w-]*|"
                         r"target|prefix|odir|outdir|wd|work[\w-]*dir|[\w-]*[-_]out)", re.I)
ARGUMENTS_CHECKED = 60 # argument paths probed at most

def path_argument(value):
  """Whether a program's argument looks like the path of a file or directory"""
  if not value or value.startswith("-") or "://" in value or any(ch in value for ch in "*?[]{},:;<>|'\"` \t=@") or is_placeholder(value):
    return False
  if re.fullmatch(r"[\d./]+", value) and not value.startswith(("./", "../")): # (1/2, 0.5, 10/20/30)
    return False
  return "/" in value or value.lower().endswith(DATA_EXT)

def argument_words(args):
  """(flag, value) of a program's arguments: --in=a/b ("--in="), --in a/b ("--in"), positional ones (flag None)"""
  flag = None
  for w in args:
    if w.startswith("-") and len(w) > 1:
      name, eq, value = w.partition("=")
      if eq:
        yield name + "=", value
        flag = None
      else:
        flag = w
      continue
    yield flag, w
    flag = None

def resolve_in(ctx, word, base):
  """word (a path of the script: $VARS, ~) as an absolute path from the directory base; None if not known"""
  if not word or is_placeholder(word):
    return None
  p = ctx.expand(word)
  if p is None:
    return None
  if not os.path.isabs(p):
    if not base:
      return None
    p = os.path.join(base, p)
  return os.path.normpath(p)

def referenced_paths(ctx):
  """[(kind, abspath, line)]: programs run (x), sourced files and redirected inputs (f), cd targets (d). Relative
  paths are resolved from where each line runs: --chdir (or where sbatch runs), then the script's cd and pushd, and a
  launcher's --chdir for its program; after a cd to a place not known (cd -, popd, an unknown $VAR), relative paths
  are not checked until the next cd to a known one"""
  s = ctx.script
  out = []
  made = set()
  home = (ctx.facts.env.get("HOME") if ctx.facts else None) or ctx.env.get("HOME") or ctx.expand("$HOME")
  # without a job, relative paths are relative to where it will be submitted (unknown): the script's directory is a
  # guess until the script changes to an absolute directory
  guessing = ctx.job is None and not ctx.opt("chdir")
  submit_dir = ctx.env.get("SLURM_SUBMIT_DIR") or (ctx.job.rec.get("WorkDir") if ctx.job is not None and not ctx.opt("chdir") else "")
  if not submit_dir and ctx.job is None:
    submit_dir = ctx.script_dir or (os.path.dirname(s.path) if s.path else "")
  cwd = ctx.workdir() or None
  ctx.uncertain, ctx.arguments = set(), {}
  seen = set() # (paths an earlier line names: made or written there, maybe; their arguments are not checked again)
  def known(p):
    return p in seen or p in made or any(p.startswith(d.rstrip("/") + "/") for d in made | seen)
  def add(kind, word, line, base=None):
    base = cwd if base is None else base
    p = resolve_in(ctx, word, base)
    if p:
      out.append((kind, p, line))
      if guessing and not os.path.isabs(ctx.expand(word) or word):
        ctx.uncertain.add(p)
    return p
  for line, t in s.code_lines():
    for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", t):
      try:
        words = shlex.split(part)
      except ValueError:
        continue
      if not words:
        continue
      while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]): # VAR=x program
        words = words[1:]
      if not words:
        continue
      cmd = words[0]
      if cmd == "mkdir":
        for w in words[1:]:
          if not w.startswith("-"):
            p = resolve_in(ctx, w, cwd)
            if p:
              made.add(p)
        continue
      if cmd in ("cd", "pushd"):
        args = [w for w in words[1:] if not w.startswith("-") or w == "-"]
        target = args[0] if args else "~"
        if target == "-" or (cmd == "pushd" and not args):
          cwd = None # (the directory before: not followed)
          continue
        if target in ("~", "$HOME", "${HOME}"):
          cwd, guessing = home or None, False
          continue
        raw = target
        if SUBMIT_DIR.match(target) and ctx.expand(target) is None and submit_dir:
          target = submit_dir + target[SUBMIT_DIR.match(target).end():]
        else:
          guessing = guessing and not os.path.isabs(ctx.expand(target) or target)
        p = resolve_in(ctx, target, cwd)
        if p and p not in made and not any(p.startswith(m + "/") for m in made):
          out.append(("d", p, line))
          if guessing and not os.path.isabs(ctx.expand(raw) or raw):
            ctx.uncertain.add(p)
        cwd = p # (None: a place not known)
        continue
      if cmd == "popd":
        cwd = None
        continue
      if cmd in ("source", ".") and len(words) > 1:
        add("f", words[1], line)
        continue
      base = cwd
      if cmd in LAUNCHERS or cmd == "exec" or cmd == "time" or cmd == "nohup":
        prog, args = launched_program(words) if cmd in LAUNCHERS else (words[1] if len(words) > 1 else None, words[2:])
        launched = launcher_dir(words) if cmd in LAUNCHERS else None
        if launched: # (srun --chdir: its program and arguments from there)
          base = resolve_in(ctx, launched, cwd)
      else:
        prog, args = cmd, words[1:]
      if not prog:
        continue
      checked = None # (the arguments of the program, to check as paths: P10)
      if prog in ("python", "python3") or re.fullmatch(r"python3(\.\d+)?", prog or ""):
        script = next((a for a in args if not a.startswith("-")), None)
        if script and script.endswith(".py"):
          add("f", script, line, base or "")
          if not any(a in ("-c", "-m") for a in args[:args.index(script)]):
            checked = args[args.index(script) + 1:]
      else:
        if "/" in prog and prog not in SKIP_PROGRAMS:
          add("x", prog, line, base or "")
        if (cmd in LAUNCHERS or "/" in prog) and prog not in SKIP_PROGRAMS:
          checked = args
        for m in re.finditer(r"(?<![<>])<\s*([^\s<>&|;]+)", part): # input redirections (the shell's: from the script's directory)
          add("f", m.group(1), line)
      for flag, value in argument_words(checked or []):
        if not path_argument(value) or len(ctx.arguments) >= ARGUMENTS_CHECKED:
          continue
        p = resolve_in(ctx, value, base or "")
        if not p or p.startswith(NODE_LOCAL) or known(p) or p in ctx.arguments:
          continue
        out.append(("a", p, line))
        ctx.arguments[p] = {"word": value, "flag": flag, "output": bool(flag and OUTPUT_FLAG.fullmatch(flag.rstrip("=")))}
        if guessing and not os.path.isabs(ctx.expand(value) or value):
          ctx.uncertain.add(p)
      line_paths = [w.partition("=")[2] if w.startswith("-") else w for w in words[1:]]
      line_paths += re.findall(r"[<>]{1,2}\s*([^\s<>&|;]+)", part)
      for w in line_paths: # (named here: maybe made or written here, for the lines after)
        p = resolve_in(ctx, w, base or "") if w and path_argument(w) else None
        if p and not (base and (p == base or base.startswith(p.rstrip("/") + "/"))): # (not ./ or ..: all below would be known)
          seen.add(p)
  return out

# ---------------------------------------------------------------------------
# relative paths not anchored to a directory (G18)

DATA_EXT = (".dat", ".h5", ".hdf5", ".txt", ".csv", ".json", ".yaml", ".yml", ".in", ".inp", ".cfg", ".conf", ".xml",
            ".nc", ".py", ".sh", ".npy", ".npz", ".bin", ".out", ".log", ".toml", ".ini", ".par", ".input")
ANCHOR_VARS = {"HOME", "SCRATCH", "STORE", "PROJECT", "WORK", "TMPDIR", "1", "2"}

def is_relative_path(word):
  if not word or word.startswith(("-", "/", "$", "~", "%", "http:", "https:")) or "://" in word or "=" in word:
    return False
  return "/" in word or word.lower().endswith(DATA_EXT)

def relative_uses(ctx):
  """[(line, word, kind)] of relative paths the script uses before it anchors itself to a directory (--chdir, or cd to
  an absolute path or a variable like $SCRATCH), and whether it has a cd at all: (uses, anchored_at, has_cd).
  kind: x (program), f (file)."""
  s = ctx.script
  if s is None:
    return [], None, False
  uses, anchored, has_cd = [], ("chdir" if ctx.opt("chdir") else None), False
  for line, t in s.code_lines():
    for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", t):
      try:
        words = shlex.split(part, posix=True)
      except ValueError:
        continue
      while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
        words = words[1:]
      if not words:
        continue
      if words[0] == "cd" and len(words) > 1:
        has_cd = True
        target = words[1]
        m = re.match(r"\$\{?(\w+)\}?", target)
        if target.startswith("/") or (m and m.group(1) in ANCHOR_VARS) or target.startswith("~"):
          anchored = anchored or line
        elif m and m.group(1) not in ("SLURM_SUBMIT_DIR", "PWD", "OLDPWD"):
          value = ctx.expand(target)
          if value and value.startswith("/"):
            anchored = anchored or line
        continue
      if anchored or words[0] in ("mkdir", "echo", "printf", "rm", "touch", "export", "module", "ml", "set"):
        continue
      if words[0] in LAUNCHERS:
        prog, args = launched_program(words)
        words = [prog, *args] if prog else []
      for i, w in enumerate(words):
        value = w.split("=", 1)[1] if w.startswith("--") and "=" in w else w
        if is_relative_path(value):
          uses.append((line, value, "x" if i == 0 else "f"))
      for m in re.finditer(r"[<>]{1,2}\s*([^\s<>&|;]+)", part):
        if is_relative_path(m.group(1)) and m.group(1) not in [u[1] for u in uses]:
          uses.append((line, m.group(1), "f"))
  return uses, anchored, has_cd

def alternative_bases(ctx):
  """The directory relative paths resolve from (where it was submitted) and the other plausible one (the script's)"""
  submit = ctx.job.rec.get("WorkDir") if ctx.job is not None else ""
  return submit or "", ctx.script_dir if ctx.script_dir and ctx.script_dir != submit else ""

# ---------------------------------------------------------------------------
# running the rules

def run_rules(ctx, only=None):
  """Findings of all rules that apply (sorted, muted ones left out)"""
  findings = []
  ignored = ctx.muted | (ctx.script.ignored_rules if ctx.script else set())
  for r in RULES.values():
    if r.id in ignored or (only and r.id[0] not in only and r.id not in only):
      continue
    if not rule_active(r, ctx.facts, ctx): # (about a software stack the cluster does not have)
      continue
    if r.needs in ("script",) and ctx.script is None:
      continue
    if r.needs == "job" and ctx.job is None:
      continue
    if r.needs == "probe" and (ctx.probes is None or ctx.script is None):
      continue
    if r.needs == "quota" and ctx.quota is None:
      continue
    if r.needs == "logs" and not ctx.logs:
      continue
    if r.needs == "replay" and ctx.replay is None:
      continue
    if r.needs == "live" and ctx.live is None:
      continue
    if r.needs == "list" and not ctx.others:
      continue
    if r.needs == "login" and not ctx.login:
      continue
    try:
      for f in r.check(ctx) or ():
        findings.append(f)
    except Exception as e: # a rule must never break the window
      from ..state import log
      log.debug(f"rule {r.id} failed: {e!r}")
  findings.sort(key=Finding.sort_key)
  return findings

def check_script_text(text, path="", facts=None, probes=None, job=None, **kw):
  ctx = make_context(text, path, job=job, facts=facts, probes=probes, **kw)
  return run_rules(ctx), ctx

def check_file(path, with_probe=True, text=None, quota=True, progress=None):
  """Findings of a script file on the cluster of the current tab (worker): the script, the facts, the probe, the quota.
  text: check this text as the content of path (an edited, unsaved version) instead of reading the file. quota=False:
  without the quota (the caller asks for it after: cached_quota, then run_rules). progress(what): the step that starts
  (each may take a while on a slow cluster: said, so that a step that hangs is seen)"""
  from ..cluster.paths import read_text
  say = progress or (lambda what: None)
  if text is None:
    say("reading it")
    text = read_text(path)
  say("the cluster's partitions, accounts and settings")
  facts = gather_facts()
  ctx = make_context(text, path, facts=facts)
  ctx.script_dir = os.path.dirname(path)
  ctx.count_files = True
  if with_probe:
    wanted = interesting_paths(ctx)
    say(f"its paths and programs on the cluster ({len(wanted)})")
    ctx.probes = run_probe([f"{k}:{p}" for k, p in wanted])
    if quota:
      say("the quota where it writes")
      ctx.quota = cached_quota()
  return run_rules(ctx), ctx

_quota, _quota_lock = {}, threading.Lock()

def cached_quota(ttl=300):
  from ..cluster.remote import tab_name
  from ..cluster.storage import quota_info
  key = tab_name()
  with _quota_lock:
    q = _quota.get(key)
    if q and time.time() - q[0] < ttl:
      return q[1]
  try:
    info = quota_info()
  except Exception as e:
    from ..state import log
    log.debug(f"quota for the checks failed: {e!r}")
    return None
  with _quota_lock:
    _quota[key] = (time.time(), info)
  return info

def tail_logs(job, size=65536):
  """{"stdout": text, "stderr": text}: the ends of the job's output files (worker)"""
  from ..cluster.remote import run_many
  paths = {k: p for k, p in (("stdout", job.stdout_path()), ("stderr", job.stderr_path())) if p}
  if not paths:
    return {}
  res = run_many([["tail", "-c", str(size), p] for p in paths.values()])
  return {k: r.stdout for k, r in zip(paths, res) if r.returncode == 0}

def finished_script(job):
  """The batch script of a finished job, if the accounting stored it (sacct --batch-script, Slurm 21.08+)"""
  from ..cluster.remote import run
  out = run(["sacct", "-j", job.jid.split(".")[0], "--batch-script"], quiet=True)
  lines = out.splitlines()
  if len(lines) > 2 and lines[0].startswith("Batch Script for") and set(lines[1]) <= {"-"}:
    text = "\n".join(lines[2:])
    return text if text.strip() and not text.startswith("NONE") else ""
  return ""

def submit_environment(job):
  """The environment the job was submitted with (if the accounting keeps it: AccountingStoreFlags=job_env), or None"""
  from ..cluster.remote import run
  from ..cluster.jobs import sacct_dump, parse_env
  out = run(["sacct", "-j", job.jid, "--env-vars"], quiet=True)
  env = parse_env(sacct_dump(out)) if out.strip() else {}
  return env or None

def check_job(job, full=True, replay=None, live=None):
  """Findings of a job (worker). full: also its log tails and the quota; the script as submitted (queued) or as the
  accounting stored it (finished), else the file it was submitted with."""
  from ..cluster.jobs import batch_script
  from ..cluster.paths import existing_files, read_text
  facts = gather_facts()
  submitted = getattr(job, "script", None) or (batch_script(job.jid) if job.queued else "")
  if not submitted and full and not job.queued:
    submitted = finished_script(job)
  on_disk = None
  submit_line = job.rec.get("SubmitLine") or ""
  _, script_arg = submit_options(submit_line)
  if script_arg:
    for path in existing_files([script_arg], job.rec.get("WorkDir") or ""):
      on_disk = read_text(path)
      break
  text = submitted or on_disk or ""
  ctx = make_context(text or None, "", job=job, facts=facts, on_disk=on_disk, submitted=submitted or None,
                     replay=replay, live=live)
  if script_arg:
    workdir = job.rec.get("WorkDir") or ""
    ctx.script_dir = os.path.dirname(os.path.normpath(script_arg if os.path.isabs(script_arg) else os.path.join(workdir, script_arg)))
  if full and ctx.script is not None:
    ctx.submit_env = submit_environment(job)
  ctx.count_files = full
  ctx.probes = run_probe([f"{k}:{p}" for k, p in interesting_paths(ctx)]) if ctx.script else None
  if full:
    ctx.logs = tail_logs(job) if (job.started or not job.queued) else {}
    ctx.quota = cached_quota()
  return run_rules(ctx), ctx

def check_jobs(jobs):
  """Light checks of the jobs of the list (worker): the script (queued jobs, as fetched by annotate), the record,
  and one probe for all of them; no logs, no quota"""
  if not jobs:
    return
  try:
    facts = gather_facts()
  except Exception as e:
    from ..state import log
    log.debug(f"facts for the checks failed: {e!r}")
    facts = None
  contexts = []
  for job in jobs:
    text = getattr(job, "script", None) or None
    ctx = make_context(text, "", job=job, facts=facts)
    ctx.others = jobs
    _, script_arg = submit_options(job.rec.get("SubmitLine") or "")
    if script_arg:
      wd = job.rec.get("WorkDir") or ""
      ctx.script_dir = os.path.dirname(os.path.normpath(script_arg if os.path.isabs(script_arg) else os.path.join(wd, script_arg)))
    contexts.append(ctx)
  requests = [f"{k}:{p}" for ctx in contexts if ctx.script and ctx.job.queued for k, p in interesting_paths(ctx)]
  probes = run_probe(requests) if requests else {}
  for job, ctx in zip(jobs, contexts):
    if ctx.script and job.queued:
      ctx.probes = probes
    job.checks = run_rules(ctx)

# ---------------------------------------------------------------------------
# the rule catalogue

def rule_matches(r, query):
  """The rule contains every word of the query (its id, title, texts and example; any case)"""
  text = " ".join((r.id, r.title, r.what, r.implies, r.fix, r.example, r.severity, STACKS.get(r.stack, ""))).lower()
  return all(word in text for word in query.lower().split())

def rules_markdown(facts=None, where="", query=""):
  """The catalogue; with the facts of a cluster (where: its name) the rules that do not apply there say so (and the
  rule catalogue window greys them out); query: only the rules that match it"""
  stacks_text = (f"{where or 'This cluster'} has " + (" and ".join(sorted(facts.stacks)) if facts.stacks else
                 "no module system and no uenv that slurp found (all rules apply)") + "." if facts is not None else "")
  out = ["# Rule catalogue",
         "",
         "slurp checks batch scripts and jobs with these rules. Every finding names its rule; this page explains "
         "what the rule looks for, why it matters, and how to fix it.",
         "",
         "**Severity:** ✖ the job fails or wastes a lot · ⚠ probably wrong or wasteful · ℹ a better practice.",
         "",
         "**Muting:** a comment in the script mutes rules for that script: `# slurp: ignore R08 S07`. "
         "To mute a rule everywhere, use *Mute* in a finding, or add its id to `\"checks_muted\"` in slurp's config.json.",
         "",
         "The guides (F1) explain the background: job scripts, arrays, software, filesystems, NUMA placement, GPUs, "
         "failed jobs.",
         "",
         "**Software stacks:** most rules apply everywhere; some only on " + " or on ".join(STACKS.values()) + ". "
         "Module rules also apply in part to a uenv's modules view (--view=modules). " + stacks_text
         + (" The rules that do not apply there are greyed out." if facts is not None else ""),
         ""]
  if facts is not None:
    from ..sites import pack_text
    site = pack_text(facts.site)
    out += [f"**Site pack:** {site}." if site else f"**Site pack:** none for {where or 'this cluster'} (only what slurp finds itself).", ""]
  if query.strip():
    count = sum(rule_matches(r, query) for r in RULES.values())
    out += [f"**Search:** {count} of {len(RULES)} rules match “{query.strip()}”" + ("" if count else " (Esc shows all)") + ".", ""]
  for cat, name in CATEGORIES.items():
    in_category = [r for r in RULES.values() if r.category == cat and (not query.strip() or rule_matches(r, query))]
    if not in_category:
      continue
    out += [f"## {cat} · {name}", ""]
    for r in in_category:
      link = lambda text: re.sub(r"\b([SRBPQENU]\d\d)\b", lambda m: f"[{m.group(1)}](rule:{m.group(1)})"
                                 if m.group(1) in RULES else m.group(1), text)
      applies = "" if r.stack == "any" else f" · only on {STACKS[r.stack]}" + (
        " (and in a uenv's modules view)" if r.stack == "modules" else "")
      inactive = facts is not None and not rule_active(r, facts)
      out += [f"### {r.heading}", "",
              f"{SEVERITY_ICON[r.severity]} {r.severity} · checks: {NEEDS_TEXT.get(r.needs, r.needs)}{applies}", ""]
      if inactive:
        out += [f"*Not active on {where or 'this cluster'}: it has no {r.stack}.*", ""]
      out += [
              f"**What:** {link(r.what)}", "",
              f"**Why it matters:** {link(r.implies)}", "",
              f"**How to fix:** {link(r.fix)}", ""]
      if r.example:
        out += ["```bash", r.example.strip("\n"), "```", ""]
  return "\n".join(out)

NEEDS_TEXT = {"script": "the script", "job": "the job record", "probe": "files on the cluster",
              "quota": "the quota", "logs": "the job's output", "replay": "the job's environment (replayed after a fresh login)",
              "live": "the running job (Placement)", "list": "the job list", "login": "your processes on the login node",
              "facts": "your login files"}

# ---------------------------------------------------------------------------
# Explain: what Slurm makes of each #SBATCH line

def explain_directive(d, ctx):
  """A short explanation of one directive with the values it really means"""
  o, v = d.option, d.value
  if d.ignored:
    return "IGNORED: after the first command"
  if not o:
    return ""
  nodes, tpn, cpt, gpn = ctx.counts()
  p = ctx.partition()
  if o == "time":
    m = parse_time(v)
    return f"time limit {time_text(m)}" + (f" (partition max {time_text(p.max_minutes)})" if p and p.max_minutes else "") if m is not None else "not a time"
  if o in ("mem", "mem-per-cpu", "mem-per-gpu"):
    parsed = parse_mem(v)
    per = {"mem": "per node", "mem-per-cpu": "per CPU", "mem-per-gpu": "per GPU"}[o]
    return f"{mem_text(parsed[0])} {per}" + ("" if parsed[1] else " (no unit: MB!)") if parsed else "not a size"
  if o == "nodes":
    return f"{v} node{'s' if v != '1' else ''}" + (f" of {p.name} ({p.cpus} CPUs, {p.gpus} GPUs, {mem_text(p.mem_mb)} each)" if p else "")
  if o == "ntasks":
    return f"{v} tasks (MPI ranks) in total"
  if o == "ntasks-per-node":
    return f"{v} tasks per node" + (f" × {cpt} CPUs = {to_int(v, 0) * cpt} of {p.cpus} CPUs per node" if p and p.cpus else "")
  if o == "cpus-per-task":
    return f"{v} CPUs (threads) per task: set OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK, srun needs SRUN_CPUS_PER_TASK"
  if o in ("gpus-per-node", "gpus-per-task", "gpus", "gres"):
    return f"{gpn} GPUs per node" + (f" of {p.gpus}" if p else "") if gpn is not None else ""
  if o in ("output", "error", "input"):
    path = ctx.abspath(output_pattern(v, ctx)) if v else None
    return f"→ {path}" + ("  (the directory must exist)" if o != "input" else "") if path else "relative to the submit directory"
  if o == "partition":
    return (f"partition {v}: max {time_text(p.max_minutes)}, {p.cpus} CPUs, {p.gpus} GPUs, {mem_text(p.mem_mb)} per node"
            if p else f"partition {v}")
  if o == "account":
    return f"charged to {v}"
  if o == "array":
    return f"a job array: tasks {v} (each gets $SLURM_ARRAY_TASK_ID); %N after the range limits how many run at once"
  if o == "job-name":
    return f"job name {v} (%x in file names)"
  if o == "exclusive":
    return "whole nodes, charged in full"
  if o == "hint":
    return "nomultithread: one task thread per core" if v == "nomultithread" else ""
  if o == "chdir":
    return "the job starts in " + (ctx.abspath(v) or v)
  if o == "dependency":
    return "starts after: " + v
  if o == "signal":
    return "a signal before the time limit (for checkpoints)"
  if o == "export":
    return "NONE: the job does not get your environment" if (v or "").upper() == "NONE" else "the environment passed to the job"
  if o == "qos":
    return f"quality of service {v}"
  if o == "constraint":
    return f"only nodes with the features {v}"
  if o == "mail-type":
    return f"mails on {v}"
  return ""

def explained_text(ctx):
  """The script with an explanation after each #SBATCH line"""
  if ctx.script is None:
    return ""
  notes = {}
  for d in ctx.script.directives:
    text = explain_directive(d, ctx)
    if text:
      notes.setdefault(d.line, []).append(text)
  width = min(max((len(l) for l in ctx.script.lines), default=0), 60)
  out = []
  for i, line in enumerate(ctx.script.lines, 1):
    if i in notes:
      out.append(f"{line:<{width}}   ⟵ {'; '.join(notes[i])}")
    else:
      out.append(line)
  return "\n".join(out)

def looks_like_job_script(text):
  return bool(re.search(r"^#SBATCH", text, re.M)) or (text.startswith("#!") and bool(re.search(r"^\s*srun\s", text, re.M)))


# ---------------------------------------------------------------------------
# What changed: a failed job against the last run of the same job that worked

def what_changed(job, days=30):
  """A text: the differences of script, command line and request to the last completed job of the same name (worker)"""
  import difflib
  from datetime import datetime, timedelta
  from ..cluster.jobs import sacct, current_user
  name = job.rec.get("JobName") or ""
  try:
    submitted = datetime.fromisoformat(job.rec.get("Submit"))
  except (TypeError, ValueError):
    submitted = datetime.now()
  rows = sacct(["-X", "-u", current_user(), "--name", name, "-s", "CD", "-S", (submitted - timedelta(days=days)).isoformat(timespec="seconds"),
                "-E", submitted.isoformat(timespec="seconds")])
  rows = [r for r in rows if r.get("JobName") == name and r.get("State", "").startswith("COMPLETED")
          and r.get("End", "") < (job.rec.get("Submit") or "9999") and r.get("JobID") != job.jid]
  out = [f"What changed: job {job.jid} ({name}, {job.state}) against the last run that worked", ""]
  if not rows:
    return "\n".join(out + [f"No completed job named {name} in the {days} days before this one."]) + "\n"
  prev = max(rows, key=lambda r: r.get("End", ""))
  out.append(f"Last success: job {prev['JobID']}, ended {prev.get('End', '?').replace('T', ' ')}")
  out.append("")
  fields = [("SubmitLine", "command line"), ("WorkDir", "directory"), ("Partition", "partition"), ("NNodes", "nodes"),
            ("Timelimit", "time limit"), ("ReqMem", "memory"), ("AllocCPUS", "CPUs"), ("Account", "account")]
  diffs = [(label, prev.get(k, ""), job.rec.get(k, "")) for k, label in fields if prev.get(k, "") != job.rec.get(k, "")]
  out.append("Request and command line:")
  out += [f"  {label}: {a or '-'}  →  {b or '-'}" for label, a, b in diffs] or ["  the same"]
  out.append("")
  class Prev:
    jid = prev["JobID"]
  old = finished_script(Prev)
  new = finished_script(job) if not job.queued else (getattr(job, "script", "") or "")
  if not new:
    new = getattr(job, "script", "") or ""
  if old and new:
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"job {prev['JobID']}", f"job {job.jid}", lineterm=""))
    out.append("Batch script:")
    out += [f"  {l}" for l in diff] or ["  the same"]
  else:
    out.append("Batch script: not stored by the accounting (sacct --batch-script) for "
               + " and ".join(x for x, s in ((f"job {prev['JobID']}", old), (f"job {job.jid}", new)) if not s))
  out.append("")
  out.append("Unchanged script and request, but it failed now? Then look at the inputs, the program (rebuilt?), the "
             "modules (changed defaults, E01) and the end of the logs (U05).")
  return "\n".join(out) + "\n"
