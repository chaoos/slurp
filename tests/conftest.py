"""Shared test setup (tests/unit: no Textual app, no cluster; tests/ui: Textual windows, no cluster; tests/e2e: slurp
against a fake Slurm cluster over real SSH, see tests/e2e/conftest.py).

* The tests use the installed slurp, which must be this checkout's (pip install -e ".[all,dev]"): the run stops if it
  is another copy (a second checkout or worktree; PYTHONPATH=src picks this one).
* Every test of unit/ and ui/ runs with its own empty XDG directories: no test reads or writes the user's slurp
  config, cache, state or data (slurp reads $XDG_* when it needs a path, so setting them before each test is enough).
  The e2e tests set up their own (a session of the app keeps its settings from one test to the next).
* ``check(name, cond, info="")``: a soft assertion. The test goes on after a failed check and fails at the end,
  listing every check that failed (the tests walk through a UI step by step: one failure should not hide the rest).
"""

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XDG = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")


def pytest_configure(config):
  """The tests run against the installed slurp (as users get it); it must be this checkout's (pip install -e .). slurp
  does not ask PyPI for a newer version in the tests (test_update.py asks a fake one)"""
  os.environ["SLURP_NO_UPDATE_CHECK"] = "1"
  os.environ["SLURP_REFRESH_SECONDS"] = "100000" # (no automatic refresh in the middle of a test; its tests set their own)
  import slurp
  here = os.path.realpath(os.path.join(ROOT, "src", "slurp"))
  got = os.path.dirname(os.path.realpath(slurp.__file__))
  if got != here:
    pytest.exit(f"slurp is imported from {got}, not from this checkout ({here}): install it with pip install -e "
                f"\".[all,dev]\" (or run with PYTHONPATH={os.path.join(ROOT, 'src')})", returncode=4)


@pytest.fixture(autouse=True)
def isolated_xdg(request, tmp_path_factory, monkeypatch):
  if request.node.get_closest_marker("lab"):
    return
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
