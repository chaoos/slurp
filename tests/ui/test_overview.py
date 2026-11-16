"""A tab opens on its overview (the default start page); Overview and Jobs are its first subtabs"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.overview import OverviewScreen


@pytest.fixture
def quiet(monkeypatch, tmp_path):
  monkeypatch.setenv("PATH", str(tmp_path)) # (no Slurm here: the overview says so)
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")


def test_overview_first(check, quiet, start_on_overview):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      for _ in range(100):
        await pilot.pause(0.05)
        if app.main is not None and isinstance(app.screen, OverviewScreen) and app.cluster.loader is None:
          break
      check("a tab opens on its overview", isinstance(app.screen, OverviewScreen) and app.cluster.current_view == "overview",
            type(app.screen).__name__)
      tabs = [str(b.label) for b in app.screen.query("#view-tabs Button.view-tab")]
      check("its subtabs: Overview, Jobs", tabs[:2] == ["Overview", "Jobs"], tabs)
      await pilot.press("j"); await pilot.pause(0.3)
      check("j: the job list, which loads in the background meanwhile", app.screen is app.main)
      for _ in range(100):
        await pilot.pause(0.05)
        if app.cluster.loader is None and app.cluster.no_slurm:
          break
      await pilot.click("#view-overview"); await pilot.pause(0.5)
      check("the Overview subtab", isinstance(app.screen, OverviewScreen))
      from rich.console import Console
      import os
      c = Console(width=140, record=True, file=open(os.devnull, "w")); c.print(app.screen.shown); text = c.export_text()
      check("without Slurm it says so, and asks nothing", "No Slurm on this machine" in text and not app.cluster.overview.get("loading"), text)
      check("the menu is there too", bool(app.screen.query("#menu")))
      await pilot.press("f10"); await pilot.pause(0.3)
      check("F10 opens it", type(app.screen).__name__ == "MenuScreen")
      await pilot.press("escape"); await pilot.pause(0.2)
      await pilot.press("q")
  asyncio.run(main())
