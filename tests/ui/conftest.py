"""The windows of the app without a cluster: a tab opens on its job list here (the tests are about it; the overview,
the default start page, has its own test)"""
import pytest


@pytest.fixture(autouse=True)
def start_on_the_job_list(request, monkeypatch):
  if "start_on_overview" in request.fixturenames:
    return
  from slurp.ui.app import Slurp
  monkeypatch.setattr(Slurp, "start_page", lambda self: "_default")


@pytest.fixture
def start_on_overview():
  """(a test that wants the default start page: the overview)"""


@pytest.fixture(autouse=True)
def no_tab_left_active(monkeypatch):
  """The app sets the SSH session of its active tab module-wide (remote.ssh_connection): a test with a cluster tab must
  not leave it to the tests after it (their commands would wait for its Connect)"""
  from slurp.cluster import remote
  monkeypatch.setattr(remote, "ssh_connection", remote.ssh_connection)
