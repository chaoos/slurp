"""A window that loads in the background stays usable: Textual's own `loading` of a widget covers it with a loading
indicator that takes every click (the whole screen, for a screen), so slurp's screens keep their state elsewhere"""
import asyncio
import pathlib
import re
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.storage import QuotaScreen


@pytest.fixture
def slow_quota(monkeypatch, tmp_path):
  bin_dir = tmp_path / "bin"
  bin_dir.mkdir()
  for name in ("lfs", "quota"): # (answer after a while: the quota is loading meanwhile)
    (bin_dir / name).write_text("#!/bin/sh\nsleep 3; exit 1\n")
    (bin_dir / name).chmod(0o755)
  monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")


def test_quota_loading_keeps_the_screen_usable(check, slow_quota):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      for _ in range(100):
        await pilot.pause(0.05)
        if app.main is not None and app.cluster.loader is None:
          break
      app.action_quota(); await pilot.pause(0.5)
      screen = app.screen
      check("Quota is loading", isinstance(screen, QuotaScreen) and screen.fetching)
      check("…without Textual's loading indicator over it", not screen.query("LoadingIndicator"),
            [type(w).__name__ for w in screen.query("LoadingIndicator")])
      await pilot.click("#quota-close"); await pilot.pause(0.4)
      check("…its Close can be clicked meanwhile", app.screen is app.main, type(app.screen).__name__)
      await pilot.press("q")
  asyncio.run(main())


def test_no_screen_sets_textuals_loading():
  """(the attribute of Textual's widgets: setting it covers the widget; slurp's screens say fetching)"""
  ui = pathlib.Path(__file__).resolve().parents[2] / "src" / "slurp" / "ui"
  found = [f"{path.name}:{n}" for path in ui.rglob("*.py") for n, line in enumerate(path.read_text().splitlines(), 1)
           if re.search(r"\bself\.loading\b\s*(=|,)", line) or re.search(r"\bself\.loading\s*,", line)]
  assert not found, found
