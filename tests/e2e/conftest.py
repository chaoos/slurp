"""The end-to-end tests: slurp against the lab (lab/: a fake Slurm cluster behind a real sshd on this machine, built
once per session in pytest's temporary directory), with real ssh, sshfs and pseudo terminals.

Every test here is marked ``lab``. On Python before 3.12, or without sshd/ssh/ssh-keygen, they are skipped, unless SLURP_REQUIRE_LAB=1 (as in the
CI), where that is an error. Their logs, screenshots and the like go to <basetemp>/lab0/out/ (pytest --basetemp=DIR
keeps them where you want)."""

import os
import sys

import pytest

from lab import Lab, missing_tools

HERE = os.path.dirname(os.path.abspath(__file__))


def pytest_collection_modifyitems(config, items):
  missing = missing_tools()
  skip = pytest.mark.skip(reason=f"the lab needs {', '.join(missing)}") if missing and os.environ.get("SLURP_REQUIRE_LAB") != "1" else None
  if sys.version_info < (3, 12): # (they run the app's loop with eager tasks, as Textual does from 3.12 on)
    skip = pytest.mark.skip(reason="the e2e tests need Python 3.12 or newer (asyncio.eager_task_factory)")
  for item in items:
    if str(item.path).startswith(HERE + os.sep):
      item.add_marker(pytest.mark.lab)
      if skip:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def lab(tmp_path_factory):
  missing = missing_tools()
  if missing:
    pytest.fail(f"the lab needs {', '.join(missing)} (SLURP_REQUIRE_LAB=1)")
  lab = Lab(tmp_path_factory.mktemp("lab")).build().start()
  yield lab
  lab.stop()


@pytest.fixture(scope="session")
def out_dir(lab):
  """Where the e2e tests put what is worth keeping (logs, screenshots): <lab>/out/"""
  path = os.path.join(lab.work, "out")
  os.makedirs(path, exist_ok=True)
  return path


@pytest.fixture(scope="module")
def ui(request, lab, out_dir):
  """run(scenario, check): a UI scenario (ui_scenarios.t_*) in a logged-in app of this test module (started with the
  first; a new one after a scenario that raised or did not end, which may have left the app half way)"""
  import ui_scenarios
  name = request.module.__name__.removeprefix("test_")
  current, count = None, 0

  def run(scenario, check):
    nonlocal current, count
    if current is None or current.broken:
      if current is not None:
        current.close()
        current = None
      count += 1
      current = ui_scenarios.UISession(lab.work, os.path.join(lab.work, "home", f"{name}-{count}"),
                                       os.path.join(out_dir, name)).start()
    current.run(scenario, check)
    assert check.count, f"{scenario.__name__} made no checks"

  yield run
  if current is not None:
    current.close()


@pytest.fixture(scope="module")
def links(lab):
  """The link tests' setup: the lab's ssh first on PATH (the relay "lab-relay"), the link settings and their log in
  the lab directory; linkkit's paths. -> linkkit"""
  import shutil
  import linkkit
  linkkit.configure(lab.work)
  with pytest.MonkeyPatch.context() as mp:
    mp.setenv("PATH", os.path.join(linkkit.LAB, "bin") + os.pathsep + os.environ["PATH"])
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
      path = os.path.join(linkkit.S, var.lower())
      shutil.rmtree(path, ignore_errors=True)
      mp.setenv(var, path)
    yield linkkit


@pytest.fixture
def link_manager(links):
  """A ShareManager for one test, and the events it reported ([(kind, link id, message)])"""
  from slurp.links import share as sh
  events = []
  mgr = sh.ShareManager(lambda kind, link, session, msg: events.append((kind, link.id, msg)))
  yield mgr, events
  mgr.shutdown()
