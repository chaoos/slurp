"""Is there a newer slurp? PyPI (slurp-ui) is asked at the start, at most once a day (the answer is kept in the cache
directory), in the background, with a short timeout: offline nothing happens. How to update depends on how slurp was
installed: pipx, uv tool, pip in a venv, or a git checkout (pip install -e). Off with "update_check": false in the
config, or SLURP_NO_UPDATE_CHECK=1 (the tests)."""

import json
import os
import re
import shlex
import sys
import time
import urllib.request

PACKAGE = "slurp-ui"
URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_EVERY = 24 * 3600 # seconds between asking PyPI
CHANGELOG = "https://chaoos.github.io/slurp/changelog/"


def cache_file():
  return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "slurp", "latest-version.json")


def version_key(text):
  """A version as something to compare: 0.10.0 > 0.9.2; a pre-release (0.9.0rc1, 0.9.0b2) before its release"""
  m = re.match(r"(\d+(?:\.\d+)*)(.*)", text.strip())
  if not m:
    return ((), 0, text)
  numbers = tuple(int(n) for n in m.group(1).split("."))
  numbers += (0,) * (4 - len(numbers))
  rest = m.group(2).lstrip(".-")
  return (numbers, 0 if rest else 1, rest)


def newer(latest, current):
  return bool(latest) and version_key(latest) > version_key(current)


def enabled():
  if os.environ.get("SLURP_NO_UPDATE_CHECK"):
    return False
  from .config import load_config
  return load_config().get("update_check", True) is not False


def latest_version(timeout=5, now=None):
  """The newest version of slurp on PyPI (not a pre-release), or None (unknown: offline, …); asked at most once a day
  (worker)"""
  now = now or time.time()
  path = cache_file()
  try:
    with open(path) as f:
      kept = json.load(f)
    if now - float(kept.get("checked", 0)) < CHECK_EVERY and kept.get("version"):
      return kept["version"]
  except (OSError, ValueError, TypeError):
    pass
  try:
    request = urllib.request.Request(URL, headers={"Accept": "application/json", "User-Agent": f"slurp/{current()}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
      data = json.load(response)
  except Exception:
    return None
  releases = [v for v, files in (data.get("releases") or {}).items() if files and not re.search(r"[a-zA-Z]", v)]
  version = max(releases, key=version_key) if releases else (data.get("info") or {}).get("version")
  try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
      json.dump({"checked": now, "version": version}, f)
  except OSError:
    pass
  return version


def current():
  from . import __version__
  return __version__


def checkout():
  """The git checkout slurp runs from (pip install -e), or None"""
  import slurp
  here = os.path.dirname(os.path.dirname(os.path.abspath(slurp.__file__))) # (src/)
  for candidate in (os.path.dirname(here), here):
    if os.path.isdir(os.path.join(candidate, ".git")) and os.path.exists(os.path.join(candidate, "pyproject.toml")):
      return candidate
  return None


def how_to_update(prefix=None, executable=None, repo=""):
  """(how slurp was installed, the command that updates it); repo: its git checkout ("": found out, None: none)"""
  prefix, executable = prefix or sys.prefix, executable or sys.executable
  repo = checkout() if repo == "" else repo
  if repo:
    return "a git checkout (pip install -e)", f"git -C {shlex.quote(repo)} pull && {shlex.quote(executable)} -m pip install -e {shlex.quote(repo + '[all]')}"
  path = prefix.replace(os.sep, "/")
  if "/pipx/venvs/" in path:
    return "pipx", f"pipx upgrade {PACKAGE}"
  if "/uv/tools/" in path:
    return "uv tool", f"uv tool upgrade {PACKAGE}"
  return "pip", f"{shlex.quote(executable)} -m pip install -U '{PACKAGE}[all]'"
