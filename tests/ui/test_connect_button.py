"""A tab of a machine slurp is not connected to: switching to it does not log in; its job list shows Connect in its
middle (no jobs from before), the status line says so"""
import asyncio
import shutil

import pytest

from slurp.state import ClusterSession
from slurp.ui.app import Slurp


@pytest.fixture
def no_slurm_path(monkeypatch, tmp_path):
  monkeypatch.setenv("PATH", str(tmp_path)) # (no ssh, no Slurm: nothing may be started)
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
  monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
  monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")


def test_connect_in_the_job_list(check, no_slurm_path):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      for _ in range(100):
        await pilot.pause(0.05)
        if app.main is not None and app.cluster.loader is None:
          break
      app.clusters.append(ClusterSession("far.invalid", "far.invalid"))
      app.refresh_cluster_tabs()
      app.select_cluster(1); await pilot.pause(0.3)
      cluster = app.cluster
      panel, table = app.main.query_one("#connect-panel"), app.main.query_one("#jobs")
      check("switching to the tab does not log in", cluster.connection is not None and cluster.connection.state == "new"
            and cluster.login_terminal is None, cluster.connection and cluster.connection.state)
      note = str(app.main.query_one("#connect-note").render())
      check("Connect in the middle of the job list, instead of the table", panel.display and panel.has_class("-center")
            and not table.display and app.main.query_one("#connect").display and "Not connected to far.invalid" in note, note)
      check("the status line: not connected", "not connected" in str(app.connection_text(cluster)), app.connection_text(cluster))
      app.select_cluster(0); await pilot.pause(0.3)
      check("back on the local tab (no Slurm here): its own message, no Connect", not panel.display
            and app.main.query_one("#no-slurm").display and not table.display)
      await pilot.press("q")
  asyncio.run(main())
