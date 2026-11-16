"""The settings slurp keeps between runs, in ~/.config/slurp/config.json ($XDG_CONFIG_HOME): the theme, the tabs,
and the files chosen for the progress of jobs"""

import json
import os
import tempfile
import threading

from .state import log

_lock = threading.Lock()

def config_path():
  return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "slurp", "config.json")

def load_config():
  """The stored settings ({} if there are none or the file cannot be read)"""
  try:
    with open(config_path()) as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except FileNotFoundError:
    return {}
  except (OSError, ValueError) as e:
    log.warning(f"cannot read {config_path()}: {e}")
    return {}

def update_config(**changes):
  """Store settings (the others stay); written atomically, errors only logged"""
  with _lock:
    data = load_config()
    data.update(changes)
    path = config_path()
    try:
      os.makedirs(os.path.dirname(path), exist_ok=True)
      fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.")
      with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
      os.replace(tmp, path)
    except OSError as e:
      log.warning(f"cannot write {path}: {e}")
