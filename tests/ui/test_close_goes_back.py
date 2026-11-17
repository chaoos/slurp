"""Closing the tab or subtab shown goes back to the one shown before it (not to a neighbour, not to the job list)"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.files import DirScreen, open_path


@pytest.fixture
def home(monkeypatch, tmp_path):
  home = tmp_path / "home"
  for name in ("a", "b", "c"):
    (home / name).mkdir(parents=True)
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.setenv("PATH", "/usr/bin:/bin")
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")
  return home


async def until(pilot, cond, t=10):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_closing_goes_back_to_the_one_shown_before(check, home):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      views = {}
      for name in ("a", "b", "c"):
        open_path(app, ["Local"], str(home / name))
        await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.path == str(home / name))
        views[name] = app.cluster.views[-1]
      shown = lambda: next((n for n, v in views.items() if app.current_mode == v.mode), app.current_mode)

      app.show_view(views["b"]); await pilot.pause(0.1)
      app.show_view(views["a"]); await pilot.pause(0.1)
      app.close_view(views["a"]); await pilot.pause(0.3)
      check("a subtab closed: the one shown before it (b, not the last one, c)", shown() == "b", shown())

      app.show_view(None); await pilot.pause(0.1) # the job list
      app.show_view(views["c"]); await pilot.pause(0.1)
      app.show_view(views["b"]); await pilot.pause(0.1)
      app.close_view(views["b"]); await pilot.pause(0.3)
      check("again: c (shown before b), not the job list", shown() == "c", shown())
      app.close_view(views["c"]); await pilot.pause(0.3)
      check("and then the job list (shown before c)", app.current_mode == "_default", app.current_mode)

      app.add_cluster(""); await pilot.pause(0.3)
      app.add_cluster(""); await pilot.pause(0.3)
      first, second, third = app.clusters
      check("three tabs, the last one added shown", app.cluster is third)
      app.select_cluster(0); await pilot.pause(0.3)
      app.close_cluster(first); await pilot.pause(0.3)
      check("the tab shown closed: the one shown before it (the third), not its neighbour", app.cluster is third
            and app.clusters == [second, third], [c.name for c in app.clusters])
      app.select_cluster(0); await pilot.pause(0.3)
      app.close_cluster(third); await pilot.pause(0.3)
      check("a tab not shown closed: the one shown stays", app.cluster is second and app.clusters == [second])
      await pilot.press("q")
  asyncio.run(main())
