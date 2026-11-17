"""The check of an SSH connection in a terminal, as the login of a tab goes: what needs no login (ssh -G, the keys,
the host's name, port and greeting), then the command before the login if there is one, then the login itself with
ssh -v (its prompts in the terminal: a host key, a passphrase, a password, a one-time code; its verbose log to a
file), then the verdict and the findings of that log. Run by the check window of the add dialog and of the
connection window:  python -m slurp.cluster.sshcheck '<json: destination, ssh settings, pre_login>'"""

import json
import os
import subprocess
import sys
import tempfile
import time

from rich.console import Console
from rich.text import Text

from . import sshsetup

DONE = "slurp-check: logged in"


def main(argv=None):
  args = json.loads((argv or sys.argv[1:])[0])
  destination, settings, pre_login = args.get("destination") or "", sshsetup.clean(args.get("ssh")), args.get("pre_login") or ""
  out = Console(force_terminal=True, highlight=False, soft_wrap=True)
  out.print(Text("1. what needs no login", style="bold"))
  start = time.monotonic()
  result = sshsetup.CheckResult()
  found = sshsetup.precheck(destination, settings, result)
  shown = len(result.findings)
  for line in sshsetup.check_text(result, log_lines=0).plain.splitlines()[1:]:
    out.print(Text(line, style="red" if line.startswith("✖") else "yellow" if line.startswith("!") else "green" if line.startswith("✔") else ""))
  if found is None:
    result.verdict, result.seconds = "error", time.monotonic() - start
    finish(out, result, shown)
    return 1
  dest, _host = found
  if pre_login:
    out.print(Text(f"\n2. the command before the login: {pre_login}", style="bold"))
    code = subprocess.run(["/bin/sh", "-c", pre_login]).returncode
    out.print(Text(f"it ended with {code}" + (" (the login follows all the same)" if code else ""), style="yellow" if code else "dim"))
  out.print(Text(f"\n{3 if pre_login else 2}. the login (as slurp's, here you answer what it asks)", style="bold"))
  with tempfile.NamedTemporaryFile(prefix="slurp-sshcheck-", suffix=".log", delete=False) as f:
    log_path = f.name
  try:
    argv = ["ssh", "-v", "-E", log_path, "-T", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ConnectTimeout=15",
            *sshsetup.ssh_args(settings), dest, f'echo "{DONE} as $(id -un) on $(hostname), home $HOME"']
    code = subprocess.run(argv).returncode
    log = open(log_path, errors="replace").read()
  finally:
    try:
      os.unlink(log_path)
    except OSError:
      pass
  result.log = log
  sshsetup.evaluate(log, code, result, batch=False)
  result.seconds = time.monotonic() - start
  finish(out, result, shown)
  return 0 if result.verdict != "error" else 1


def finish(out, result, shown):
  """The verdict, the findings not shown yet (of the login), the end of ssh's verbose log"""
  out.print()
  rest = sshsetup.CheckResult(verdict=result.verdict, summary=result.summary, findings=result.findings[shown:],
                              log=result.log, seconds=result.seconds)
  out.print(sshsetup.check_text(rest, log_lines=40))


if __name__ == "__main__":
  sys.exit(main())
