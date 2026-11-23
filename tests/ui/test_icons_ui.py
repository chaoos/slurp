"""The icons in the app, with the default set (slurp's icon font): the tab, its subtab, the buttons, the menu; and
none when switched off"""
import asyncio
import shutil

import pytest

from slurp.config import update_config
from slurp.resources import icons
from slurp.ui.app import Slurp
from slurp.ui.screens.menu import MenuScreen


@pytest.fixture
def home(monkeypatch, tmp_path):
  monkeypatch.setenv("PATH", "/usr/bin:/bin")
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")
  yield
  icons.set_style(None)


async def labels(app, pilot):
  for _ in range(100):
    await pilot.pause(0.05)
    if app.main is not None and app.cluster.loader is None:
      break
  app.show_view(None); await pilot.pause(0.3)
  screen = app.screen
  found = {"tab": str(screen.query_one("#cluster-tab-0").label), "jobs": str(screen.query_one("#view-jobs").label),
           "reload": str(screen.query_one("#reload").label), "reload_height": screen.query_one("#reload").region.height,
           "menu": []}
  app.push_screen(MenuScreen()); await pilot.pause(0.3)
  found["menu"] = [str(o.prompt) for o in app.screen.query_one("#menu-list")._options if o is not None]
  return found


def test_default_icons(check, home):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      found = await labels(app, pilot)
      check("the default set: slurp's icon font", icons.style() == "nerd")
      check("this machine's tab: its icon", found["tab"].startswith(icons.icon("local") + " Local"), found["tab"])
      check("the Jobs subtab", found["jobs"] == icons.icon("jobs") + " Jobs", found["jobs"])
      check("Reload", found["reload"] == icons.icon("reload") + " Reload", found["reload"])
      check("…on one line (not wrapped for the icon)", found["reload_height"] == 1, found["reload_height"])
      check("each item of the menu", found["menu"] and all(len(p) > 2 and 0xF0000 <= ord(p[0]) <= 0xF1FFF and p[1] == " "
                                                            for p in found["menu"]), found["menu"][:3])
      await pilot.press("escape"); await pilot.press("q")
  asyncio.run(main())


def test_no_icons(check, home):
  update_config(icons="none")
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      found = await labels(app, pilot)
      check("the tab: its name", found["tab"] == "Local", found["tab"])
      check("the buttons: their words", found["jobs"] == "Jobs" and found["reload"] == "Reload", found)
      check("the menu: its words", found["menu"][0].startswith("Job list"), found["menu"][:2])
      await pilot.press("escape"); await pilot.press("q")
  asyncio.run(main())
