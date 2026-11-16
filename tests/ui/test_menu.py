"""The job list's menu (☰ Menu, F10) instead of a row of buttons; no Textual command palette (nor its ⭘ in the header)"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.menu import ITEMS, MenuScreen


@pytest.fixture
def quiet(monkeypatch, tmp_path):
  monkeypatch.setenv("PATH", str(tmp_path)) # (no Slurm, no ssh: only the Local tab, quietly)
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")


def test_menu(check, quiet):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      for _ in range(100):
        await pilot.pause(0.05)
        if app.main is not None and app.cluster.loader is None:
          break
      toolbar = [b.id for b in app.main.query("#toolbar Button")]
      check("the toolbar: Menu and Reload, the other buttons are in the menu", toolbar == ["menu", "reload"], toolbar)
      check("every item of the menu is an action of the app", all(hasattr(app, f"action_{a}") for a, _, _ in filter(None, ITEMS)))
      await pilot.press("f10"); await pilot.pause(0.3)
      check("F10 opens the menu", isinstance(app.screen, MenuScreen))
      button = app.main.query_one("#menu")
      listed = app.screen.query_one("#menu-list")
      check("…under its button", listed.region.x == button.region.x and listed.region.y == button.region.y + 1, (listed.region, button.region))
      await pilot.press("escape"); await pilot.pause(0.2)
      check("Esc closes it", app.screen is app.main)
      await pilot.click("#menu"); await pilot.pause(0.3)
      check("a click on ☰ Menu opens it", isinstance(app.screen, MenuScreen))
      await pilot.click(offset=(130, 35)); await pilot.pause(0.2)
      check("a click elsewhere closes it", app.screen is app.main)
      await pilot.press("f10"); await pilot.pause(0.3)
      items = app.screen.query_one("#menu-list")
      items.highlighted = items.get_option_index("options"); await pilot.press("enter"); await pilot.pause(0.5)
      check("Enter on an item does it (Options)", type(app.screen).__name__ == "OptionsScreen", type(app.screen).__name__)
      await pilot.press("escape"); await pilot.pause(0.2)
      check("no command palette", not app.ENABLE_COMMAND_PALETTE)
      await pilot.press("ctrl+p"); await pilot.pause(0.3)
      check("Ctrl+P opens nothing", type(app.screen).__name__ != "CommandPalette", type(app.screen).__name__)
      icons = list(app.screen.query("HeaderIcon"))
      check("the header's ⭘ is not shown", all(not icon.display for icon in icons), icons)
      await pilot.press("q")
  asyncio.run(main())
