"""A file opened from a directory opens in a subtab of its own (the directory stays in its subtab), once: opened
again, its subtab is shown; a subdirectory opens where the directory is (← goes back)"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.files import DirScreen, FileScreen


@pytest.fixture
def home(monkeypatch, tmp_path):
  home = tmp_path / "home"
  (home / "sub").mkdir(parents=True)
  (home / "notes.txt").write_text("hello\n")
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


def test_a_file_from_a_directory_opens_in_a_subtab(check, home):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      app.action_login_dir()
      ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.entries)
      check("the login directory: a subtab", ok and len(app.cluster.views) == 1, [v.title for v in app.cluster.views])
      directory, dir_view = app.screen, app.cluster.views[0]
      table = directory.query_one("#dir"); table.focus()
      table.move_cursor(row=table.get_row_index("notes.txt")); await pilot.press("enter")
      ok = await until(pilot, lambda: isinstance(app.screen, FileScreen))
      views = [v.title for v in app.cluster.views]
      check("a file: a subtab of its own, shown", ok and views == [dir_view.title, "notes.txt"]
            and app.current_mode == app.cluster.views[1].mode, (views, app.current_mode))
      app.show_view(dir_view); await pilot.pause(0.3)
      check("the directory stays in its subtab", app.screen is directory)
      table.move_cursor(row=table.get_row_index("notes.txt")); await pilot.press("enter")
      ok = await until(pilot, lambda: isinstance(app.screen, FileScreen))
      check("opened again: its subtab, not another", ok and len(app.cluster.views) == 2, [v.title for v in app.cluster.views])
      app.show_view(dir_view); await pilot.pause(0.3)
      table.move_cursor(row=table.get_row_index("sub")); await pilot.press("enter")
      ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen is not directory)
      check("a subdirectory: where the directory is (no subtab)", ok and len(app.cluster.views) == 2
            and app.current_mode == dir_view.mode, [v.title for v in app.cluster.views])
      await pilot.press("q")
  asyncio.run(main())
