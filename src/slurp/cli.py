"""The command line: slurp [options] runs the app (under the session holder, see links/holder.py), slurp check FILE...
checks batch scripts without it, slurp --holder is the holder process itself. The `slurp` command and
`python -m slurp` both come here."""

import argparse
import os
import shlex
import sys
import time

from .launch import slurp_command


class VersionAction(argparse.Action):
  """--version: slurp's version and the site packs it reads (with their versions), as lines"""
  def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help="show the version of slurp "
               "and of its site packs, and exit"):
    super().__init__(option_strings, dest, nargs=0, default=default, help=help)

  def __call__(self, parser, namespace, values, option_string=None):
    from . import VERSION_TEXT
    from .sites import packs_table
    print("\n".join([f"slurp {VERSION_TEXT}", *packs_table()]))
    parser.exit()


def check_main(argv):
  """slurp check FILE...: the checks of batch scripts as text, on the machine it runs on (e.g. a login node).
  Exit code 1 if a script has errors, 2 if a file cannot be read."""
  parser = argparse.ArgumentParser(prog="slurp check", description="Check Slurm batch scripts (the rules of slurp's "
                                   "rule catalogue: slurp check --rules lists them).")
  parser.add_argument("--version", action=VersionAction)
  parser.add_argument("files", nargs="*", metavar="FILE")
  parser.add_argument("--no-probe", action="store_true", help="only the script itself: no paths, programs or quota")
  parser.add_argument("--rules", action="store_true", help="print the rule catalogue (Markdown) and exit")
  parser.add_argument("--all", action="store_true", help="also the hints (ℹ), not only errors and warnings")
  args = parser.parse_args(argv)
  from .checks import engine as ch
  if args.rules:
    print(ch.rules_markdown())
    return 0
  if not args.files:
    parser.error("give one or more batch scripts")
  color = sys.stdout.isatty()
  paint = {"error": "\x1b[1;31m", "warning": "\x1b[33m", "info": "\x1b[36m"}
  worst = 0
  for path in args.files:
    path = os.path.abspath(path)
    if not os.path.isfile(path):
      print(f"{path}: cannot read it", file=sys.stderr)
      worst = max(worst, 2)
      continue
    findings, ctx = ch.check_file(path, with_probe=not args.no_probe)
    shown = [f for f in findings if args.all or f.severity != "info"]
    c = ch.counts(findings)
    print(f"{path}: {c['error']} errors, {c['warning']} warnings, {c['info']} hints" + ("" if args.all else " (--all shows the hints)"))
    for f in shown:
      where = f"{path}:{f.line}" if f.line else path
      icon = f"{paint[f.severity]}{f.icon} {f.rule.id}\x1b[0m" if color else f"{f.icon} {f.rule.id}"
      print(f"  {where}: {icon} {f.evidence}")
      print(f"      fix: {f.fix or f.rule.fix}")
      if f.snippet:
        for line in f.snippet.splitlines():
          print(f"      | {line}")
    if c["error"]:
      worst = max(worst, 1)
  return worst

def install_font():
  """Copy slurp's icon font where this user's fonts are; the terminals take its icons from it (their font stays)"""
  import shutil
  import subprocess
  from .ui.icons import FONT
  if sys.platform == "win32":
    os.startfile(FONT) # (the font viewer, with Install)
    print(f"opened {FONT}: Install there, then restart the terminal (all its windows)")
    return 0
  from .ui.icons import RESTART, font_folder
  folder = font_folder()
  try:
    os.makedirs(folder, exist_ok=True)
    shutil.copyfile(FONT, os.path.join(folder, os.path.basename(FONT)))
  except OSError as e:
    print(f"slurp: cannot install the icon font in {folder}: {e}", file=sys.stderr)
    return 1
  if shutil.which("fc-cache"):
    subprocess.run(["fc-cache", "-f", folder], check=False)
  print(f"installed {os.path.basename(FONT)} in {folder}: {RESTART} to see the icons (a new window is not enough "
        "for GNOME Terminal or Konsole: one process keeps the old font for all their windows). It is the terminal's "
        "machine that needs the font: over SSH, install it where you sit.")
  return 0

def main():
  if sys.argv[1:2] == ["check"]:
    sys.exit(check_main(sys.argv[2:]))
  if sys.argv[1:2] == ["--holder"]: # (the session holder process, started by slurp itself)
    from .links import holder
    sys.exit(holder.run_holder(sys.argv[2], sys.argv[3:]))
  parser = argparse.ArgumentParser(prog="slurp", description="Interactive terminal UI for Slurm jobs, locally or on "
                                   "clusters over SSH (one tab per cluster, added with +). "
                                   "'slurp check FILE...' checks batch scripts without the UI.")
  from . import VERSION_TEXT, __version__
  parser.add_argument("--version", action=VersionAction)
  parser.add_argument("--log", metavar="FILE", help="write a debug log (all slurm commands that are run) to FILE")
  parser.add_argument("--serve", action="store_true", help="serve the app in a browser with textual-serve")
  parser.add_argument("--host", default="127.0.0.1", help="web server bind address for --serve (default: 127.0.0.1)")
  parser.add_argument("--port", type=int, default=8000, help="web server port for --serve (default: 8000)")
  parser.add_argument("--new", action="store_true", help="start a new, separate slurp instead of attaching to the "
                      "running one")
  parser.add_argument("--no-holder", action="store_true", help="run directly in this terminal, without the session "
                      "holder (no remote web access, ends with the terminal)")
  parser.add_argument("--install-font", action="store_true", help="install slurp's icon font for this user, so that "
                      "the terminals of this machine show its icons (Linux, macOS; Windows: opens the font to install)")
  args = parser.parse_args()

  if args.install_font:
    sys.exit(install_font())

  if args.serve:
    try:
      from textual_serve.server import Server
    except ImportError:
      parser.error("--serve requires textual-serve; install it with: pip install textual-serve")
    command = slurp_command()
    if args.log:
      command += ["--log", args.log]
    Server(shlex.join(command), host=args.host, port=args.port, title="slurp").serve()
    return

  if not os.environ.get("SLURP_INNER") and not args.no_holder and sys.stdin.isatty() and sys.stdout.isatty():
    # the session holder runs the app; this terminal is a view of it (see links/holder.py)
    from .links import holder
    app_args = [a for a in sys.argv[1:] if a not in ("--new", "--no-holder")]
    path = holder.socket_path(f"s{os.getpid()}" if args.new else "default")
    if holder.alive(path):
      status = holder.control("status", path=path) or {}
      running = status.get("version") or "older than 0.8.0"
      if running != __version__: # (the running slurp keeps its code: this one's changes need a restart of it)
        print(f"slurp {VERSION_TEXT}: the slurp running already is version {running}. Attaching to it; to use "
              f"{__version__}, quit it (q, or Quit in its menu) and start slurp again.", file=sys.stderr)
        time.sleep(2.5)
      if app_args:
        print("slurp is running already: attaching to it (its options stay as they were; + adds a tab, "
              "--new starts a separate one)", file=sys.stderr)
        time.sleep(1.5)
      sys.exit(holder.attach(path))
    if holder.start_holder(path, app_args):
      sys.exit(holder.attach(path))
    print("slurp: the session holder did not start (see ~/.local/state/slurp/holder.err); running directly",
          file=sys.stderr)

  from . import state
  from .ui.app import Slurp
  state.setup_logging(args.log)
  app = Slurp()
  try:
    app.run()
  finally:
    app.shutdown()
