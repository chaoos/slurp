"""Shared test setup.

* Every test runs with its own empty XDG directories: no test reads or writes the user's slurp config, cache, state
  or data (slurp reads $XDG_* when it needs a path, so setting them before each test is enough).
* ``check(name, cond, info="")``: a soft assertion. The test goes on after a failed check and fails at the end,
  listing every check that failed (the tests walk through a UI step by step: one failure should not hide the rest).
* ``lab``: the fake Slurm cluster behind a real sshd (tests/lab), built once per session. Tests that need it are
  marked ``lab`` and are skipped when sshd/ssh/ssh-keygen are missing, unless SLURP_REQUIRE_LAB=1 (CI), where that
  is an error instead.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests")) # (the lab package)

from lab import Lab, missing_tools # noqa: E402

XDG = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path_factory, monkeypatch):
  base = tmp_path_factory.mktemp("xdg")
  for name in XDG:
    monkeypatch.setenv(name, str(base / name.lower()))


class Checks:
  def __init__(self):
    self.count = 0
    self.failed = []

  def __call__(self, name, cond, info=""):
    self.count += 1
    if not cond:
      self.failed.append(f"{name}  {info}" if info != "" else name)
    return bool(cond)


@pytest.fixture
def check():
  return Checks()


@pytest.hookimpl(hookwrapper=True)
def pytest_pyfunc_call(pyfuncitem):
  outcome = yield
  checks = pyfuncitem.funcargs.get("check")
  if isinstance(checks, Checks) and checks.failed and outcome.excinfo is None:
    lines = "\n".join(f"  FAIL  {f}" for f in checks.failed)
    outcome.force_exception(pytest.fail.Exception(f"{len(checks.failed)} of {checks.count} checks failed:\n{lines}", pytrace=False))


def pytest_collection_modifyitems(config, items):
  missing = missing_tools()
  if not missing or os.environ.get("SLURP_REQUIRE_LAB") == "1":
    return
  skip = pytest.mark.skip(reason=f"the lab needs {', '.join(missing)}")
  for item in items:
    if "lab" in item.keywords:
      item.add_marker(skip)


@pytest.fixture(scope="session")
def lab(tmp_path_factory):
  missing = missing_tools()
  if missing:
    pytest.fail(f"the lab needs {', '.join(missing)} (SLURP_REQUIRE_LAB=1)")
  lab = Lab(tmp_path_factory.mktemp("lab")).build().start()
  yield lab
  lab.stop()
