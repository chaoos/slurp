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
