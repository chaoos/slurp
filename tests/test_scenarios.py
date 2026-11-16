"""The lab scenarios (tests/scenarios/*.py): slurp against the fake cluster over real SSH, real sshfs and real ssh
clients in pseudo terminals. Each runs in its own process (they start servers, change the environment and the
event loop, and leave subprocesses to clean up) and prints one line per check, ``PASS name`` or ``FAIL name info``.

Their output, logs and screenshots end up in <lab>/out/<scenario>/ (pytest's temporary directory; --basetemp sets
where, the CI keeps it when a run fails).
"""

import os
import re
import subprocess
import sys

import pytest

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")

# name: (script, time limit in s, environment). "tui" is one long session through the whole UI; the drive (sshfs
# mounted from the UI) has a session of its own (after the others in one session, it does not mount: to be found out).
SCENARIOS = {
  "tui": ("tui", 3600, {"SKIP": "t_drive"}),
  "tui-drive": ("tui", 600, {"ONLY": "t_drive"}),
  "share": ("share", 300, {}),
  "weblink": ("weblink", 200, {}),
  "winhelper": ("winhelper", 300, {}),
  "holder": ("holder", 300, {}),
}


@pytest.mark.lab
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name, lab):
  script, limit, extra = SCENARIOS[name]
  out = os.path.join(lab.work, "out", name)
  os.makedirs(out, exist_ok=True)
  env = lab.env() | {"SLURP_TEST_OUT": out, "PYTHONUNBUFFERED": "1"}
  if "ONLY" in os.environ or "SKIP" in os.environ: # (set by hand: they decide, for the UI scenarios)
    extra = {}
  env |= extra
  for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
    env.pop(var, None) # (each scenario puts its own in the lab directory)
  # the home of the scenario: ~/mnt/lab and the like go here, not into the user's home
  env["HOME"] = os.path.join(lab.work, "home", name)
  os.makedirs(env["HOME"], exist_ok=True)
  log = os.path.join(out, "output.txt")
  with open(log, "w") as f:
    try:
      proc = subprocess.run([sys.executable, "-u", os.path.join(HERE, f"{script}.py")], env=env, cwd=lab.work,
                            stdout=f, stderr=subprocess.STDOUT, timeout=limit)
      code = proc.returncode
    except subprocess.TimeoutExpired:
      code = "timeout"
  text = open(log, errors="replace").read()
  passed = len(re.findall(r"^PASS\b", text, re.M))
  failed = re.findall(r"^FAIL\b.*", text, re.M)
  summary = f"{name}: {passed} passed, {len(failed)} failed, exit {code} (output: {log})"
  print(summary)
  if failed or code != 0 or not passed:
    detail = "\n".join(failed) if failed else "\n".join(text.splitlines()[-40:])
    pytest.fail(f"{summary}\n{detail}", pytrace=False)
