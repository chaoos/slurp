"""Test shim: the names of all slurp modules as one module (reading finds them in any module, setting changes
them in every module that has them, so that settings like PING_INTERVAL take effect)"""
import importlib, os, sys, types
REPO = os.environ.get("SLURP_REPO") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.environ["SLURP_TEST_WORK"] # (the lab directory: the XDG directories of the tests go there)
if "XDG_STATE_HOME" not in os.environ: # (the log of links) never the user's either
  os.environ["XDG_STATE_HOME"] = os.path.join(WORK, "xdgstate")
if "XDG_CONFIG_HOME" not in os.environ: # never the user's config: an empty one for every test run
  import shutil
  os.environ["XDG_CONFIG_HOME"] = os.path.join(WORK, "xdgtest")
  shutil.rmtree(os.environ["XDG_CONFIG_HOME"], ignore_errors=True)
if "XDG_DATA_HOME" not in os.environ: # (cloudflared) never the user's data either
  os.environ["XDG_DATA_HOME"] = os.path.join(WORK, "xdgdata")
sys.path.insert(0, REPO)
NAMES = ["state", "remote", "jobs", "details", "widgets", "terminal", "screens.dialogs", "screens.base", "screens.files",
         "screens.hdf5", "screens.core", "screens.shell", "screens.job", "screens.main", "screens.connection",
         "screens.clusterinfo", "storage", "screens.storage", "screens.array", "share", "mounts", "screens.links", "checks", "rules", "rulepacks", "environment", "placement", "screens.checks", "screens.script", "screens.placement", "screens.wizard", "fixes", "templates", "guides", "screens.help", "screens.options", "holder", "categories", "screens.categories", "app"]
def _load(n):
  try:
    return importlib.import_module(f"slurp.{n}")
  except ModuleNotFoundError as e: # (an older tree without the newer modules)
    if e.name != f"slurp.{n}":
      raise
    return None
MODS = [m for m in map(_load, NAMES) if m is not None]

class Shim(types.ModuleType):
  def __getattr__(self, name):
    if name == "SlurmTUI":
      name = "Slurp"
    for m in reversed(MODS):
      if name in vars(m):
        return vars(m)[name]
    raise AttributeError(name)
  def __setattr__(self, name, value):
    found = False
    for m in MODS:
      if name in vars(m):
        setattr(m, name, value)
        found = True
    if not found:
      super().__setattr__(name, value)

sys.modules[__name__].__class__ = Shim
