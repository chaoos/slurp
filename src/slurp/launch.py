"""How slurp starts itself again: the session holder, the app under it, the app of --serve (see holder.py, cli.py)"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # the directory this slurp package is in
BOOTSTRAP = "import sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = 'slurp'; from slurp.cli import main; main()"


def slurp_command(*args):
  """The command that starts this same slurp again (the holder, the app under it, the app of --serve), installed or
  not: the package is taken from where this one is (-P: never from the current directory); no PYTHONPATH, which
  the shells and jobs started from slurp would inherit."""
  return [sys.executable, "-P", "-c", BOOTSTRAP, ROOT, *args]
