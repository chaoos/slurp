"""Without Slurm (no squeue on the machine of a tab): the job list says so in its middle, once, instead of an error
notification for every Slurm command; r looks again"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp


@pytest.fixture
def no_slurm_path(monkeypatch, tmp_path):
  monkeypatch.setenv("PATH", str(tmp_path)) # (nothing to run there: no squeue, sacct, sacctmgr)
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")


def test_no_slurm_in_the_job_list(check, no_slurm_path):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      for _ in range(100):
        await pilot.pause(0.05)
        if app.main is not None and app.cluster.loader is None and app.cluster.no_slurm:
          break
      note, table = app.main.query_one("#no-slurm"), app.main.query_one("#jobs")
      check("the tab knows: no Slurm", app.cluster.no_slurm and not app.cluster.slurm_found)
      check("the job list shows the message instead of the table", note.display and not table.display)
      text = str(note.render())
      check("the message: what is missing and what to do", "No Slurm on this machine" in text and "squeue" in text
            and "slurp --ssh" in text, text)
      check("the status line says it too", "no Slurm on this machine" in str(app.main.query_one("#status").render()))
      check("no notification about it", not app._notifications, [n.message for n in app._notifications])
      await pilot.press("r")
      for _ in range(60):
        await pilot.pause(0.05)
        if app.cluster.loader is None:
          break
      check("r looks again, still quietly", app.cluster.no_slurm and note.display and not app._notifications,
            [n.message for n in app._notifications])
      await pilot.press("q")
  asyncio.run(main())
