"""Logging, settings shared by the modules, and the data of the tabs and subtabs"""

import logging
import re
from dataclasses import dataclass, field





log = logging.getLogger("slurp")

TAIL_BYTES = 1024*1024 # files larger than this are shown from their end (more on request, in parts this size)
PROGRESS_TAIL_BYTES = 64*1024
PROGRESS_TAIL_LINES = 30
PROGRESS_PATTERN = re.compile(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)")
PROGRESS_PERCENT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?%") # M% (0 to 100, integer or not), where no n/N is
STEP_NAME = "slurp"  # job name of the steps that run commands on the nodes
OLD_STEP_NAMES = (STEP_NAME, "slurm_tui") # also of earlier versions
PRESETS = {
  "nvidia-smi": "nvidia-smi",
  "GPU csv":    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv",
  "free":       "free -h",
  "top":        "top -bn1 | head -30",
  "uptime":     "uptime",
  "ps":         "ps -u $USER -o pid,pcpu,rss,etime,cmd",
}

# ---------------------------------------------------------------------------
# logging: never write to the terminal, collect warnings to show them in the UI

class Collector(logging.Handler):
  """The warnings, with the tab of the thread that logs each; tab_of() names that tab (cluster/remote.py sets it: it knows the
  connections, this module is below it)"""
  def __init__(self):
    super().__init__(logging.WARNING)
    self.messages = []
    self.tab_of = lambda: "Local"
  def emit(self, record):
    self.messages.append((self.tab_of(), record.getMessage()))
  def drain(self):
    msgs, self.messages = self.messages, []
    return msgs

collector = Collector()

@dataclass
class ClusterSession:
  """A tab: the local machine or an SSH destination, with its own connection, shell and job list"""
  name: str
  destination: str | None = None
  ssh: dict | None = None              # full SSH settings (sshsetup: user, host, port, jump, identities, options, name)
  pre_login: str = ""                  # a command run before every login, in the login's terminal (e.g. signing a key)
  connection: object | None = None
  terminal: object | None = None       # shell in the pane on the right while this tab is active
  login_terminal: object | None = None # the ssh login (control master) of the connection
  jobs: dict = field(default_factory=dict)
  since: object | None = None          # finished-since time of the loaded list
  summary: str = ""                    # status line of the loaded list
  loaded: bool = False
  listed: bool = False                 # the list of the current load is shown, the files of its jobs are being read
  drive: object | None = None          # mounts.LocalDrive: the login directory as a drive on this machine
  places: list | None = None           # [(label, path)]: home, $SCRATCH, $PROJECT, … on the cluster (None: not looked up)
  loader: object | None = None         # worker loading the job list
  accounts: list | None = None         # Slurm accounts of the user (sacctmgr), None: not asked yet
  since_value: str = "1d"              # finished jobs listed: within this time (e.g. 1d, 3d12h, 2w, 1mo), "off": none
  collapse_arrays: bool = True        # big arrays as one row in the job list (else every task)
  expanded: set = field(default_factory=set) # collapsed array rows that are open ("array id|state")
  scope: str = "me"                    # whose jobs are listed: "me", "all" (all accounts) or an account
  views: list = field(default_factory=list) # the subtabs besides the job list
  current_view: str = "_default"       # mode shown ("_default": the job list, "overview": no subtab open, a view's mode)
  view_history: list = field(default_factory=list) # the modes shown, the last one most recently (closing goes back)
  opened: bool = False                 # shown once (the first time: the overview, or the job list with start_page jobs)
  jobs_open: bool = False              # the job list is open as a subtab (closed: the overview, when nothing else is open)
  overview: dict = field(default_factory=dict) # what the overview loaded of its cluster (screens/overview.py)
  load_started: float = 0.0
  updated: object | None = None        # when the list was loaded
  show_login: bool = False             # the login is shown in the pane (until it is done)
  attempts: int = 0                    # logins since the session was last ready
  ready_generation: int | None = None  # session generation the app has seen ready
  broken_at: float | None = None       # when the session broke (or the last reconnect started)
  categories: object | None = None     # categories.Store of its Slurm cluster (None: not read yet)
  slurm_cluster: str | None = None     # Slurm's name of the cluster (ClusterName; the categories are kept by it)
  marked: set = field(default_factory=set) # rows of the job list marked (m): job ids, "group|<array>" for an array
  from_cache: bool = False             # the job list is the one kept from an earlier load (jobcache.py), not loaded now
  slurm_found: bool = False            # squeue was found on the machine of the tab (it is not looked for again)
  no_slurm: bool = False               # the last load found no Slurm there: the job list says so
  pending_categories: list = field(default_factory=list) # changes to the categories made while the cluster could not
                                                         # be reached: [op, args...], written to it with the next load

@dataclass
class View:
  """A subtab of a tab: a job window, a file or directory, a shell. It is a Textual mode, with its own window stack."""
  id: int
  title: str
  kind: str                   # "job", "window" or "shell"
  key: str = ""               # identifies what it shows (e.g. the job), to show it again instead of opening it twice
  session: object | None = None # TerminalSession of a shell
  icon: str = ""              # the concept of its icon (ui/icons.py): shell, folder, file, script, …

  @property
  def mode(self):
    return f"view-{self.id}"

def is_local_destination(destination):
  return destination.lower() in {"localhost", "127.0.0.1", "::1"}

def setup_logging(logfile):
  for name in ("slurp", "asyncssh"): # asyncssh: the logger of links
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel((logging.INFO if name == "asyncssh" else logging.DEBUG) if logfile else logging.WARNING)
    lg.addHandler(collector)
    if logfile:
      fh = logging.FileHandler(logfile)
      fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(funcName)s] %(message)s"))
      lg.addHandler(fh)
