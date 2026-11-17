"""A file opened from a directory opens in a subtab of its own (the directory stays in its subtab), once: opened
again, its subtab is shown; a subdirectory opens where the directory is (← goes back)"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.files import DirScreen, FileScreen, OpenDirScreen


@pytest.fixture
def home(monkeypatch, tmp_path):
  home = tmp_path / "home"
  (home / "sub").mkdir(parents=True)
  (home / "notes.txt").write_text("hello\n")
  (tmp_path / "scratch" / "run1").mkdir(parents=True)
  (tmp_path / "project").mkdir()
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.setenv("SCRATCH", str(tmp_path / "scratch"))
  monkeypatch.setenv("PROJECT", str(tmp_path / "project"))
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
      await until(pilot, lambda: isinstance(app.screen, OpenDirScreen)); await pilot.press("enter")
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


def test_open_a_directory_asks_which_and_opens_a_new_subtab_each_time(check, home, tmp_path):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      app.action_login_dir()
      ok = await until(pilot, lambda: isinstance(app.screen, OpenDirScreen) and len(app.screen.places) == 3)
      dialog = app.screen
      check("the dialog: home (first, highlighted), then $SCRATCH and $PROJECT", ok and dialog.places == [
            ("Home", str(home)), ("Scratch", str(tmp_path / "scratch")), ("Project", str(tmp_path / "project"))]
            and dialog.query_one("#opendir-places").highlighted == 0, dialog.places)
      await pilot.press("down", "enter")
      ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.entries)
      check("a place: its directory in a subtab", ok and app.screen.path == str(tmp_path / "scratch")
            and len(app.cluster.views) == 1, getattr(app.screen, "path", None))
      first = app.screen
      check("the path is shown in an input, the places as buttons", first.query_one("#dpath").value == first.path
            and [str(b.label) for b in first.query("#dplaces Button")] == ["Home", "Scratch", "Project"])

      app.action_login_dir()
      await until(pilot, lambda: isinstance(app.screen, OpenDirScreen))
      await pilot.press("tab"); await pilot.press(*"~/sub"); await pilot.press("enter")
      ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen is not first)
      check("a typed path (~/sub), and again: another subtab", ok and app.screen.path == str(home / "sub")
            and len(app.cluster.views) == 2, [v.title for v in app.cluster.views])
      second = app.screen

      app.action_login_dir()
      await until(pilot, lambda: isinstance(app.screen, OpenDirScreen)); await pilot.press("escape")
      await pilot.pause(0.2)
      check("Esc: nothing opens", app.screen is second and len(app.cluster.views) == 2)

      path = second.query_one("#dpath"); path.focus(); path.value = str(tmp_path / "scratch" / "run1")
      await pilot.press("enter")
      ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.path.endswith("run1"))
      check("the path edited, Enter: there, in the same subtab", ok and len(app.cluster.views) == 2)
      third = app.screen
      await until(pilot, lambda: len(third.query("#dplaces Button")) == 3)
      await pilot.click("#dplace-2")
      ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.path == str(tmp_path / "project"))
      check("a place button: there", ok and len(app.cluster.views) == 2)
      await pilot.press("escape"); await pilot.pause(0.2)
      check("Esc comes back", app.screen is third)
      path = third.query_one("#dpath"); path.focus(); path.value = "/somewhere/else"
      await pilot.press("escape")
      check("Esc while editing the path: as it was", path.value == third.path and app.screen is third)
      await pilot.press("q")
  asyncio.run(main())


def test_permissions_owners_acls_and_what_the_user_cannot_do(check, home):
  import os
  if os.geteuid() == 0:
    pytest.skip("root may read and write everything")
  (home / "secret").write_text("x"); os.chmod(home / "secret", 0o000)
  (home / "readonly").write_text("x"); os.chmod(home / "readonly", 0o444)
  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      app.action_login_dir()
      await until(pilot, lambda: isinstance(app.screen, OpenDirScreen)); await pilot.press("enter")
      ok = await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.entries)
      table = app.screen.query_one("#dir")
      row = lambda name: [str(c) for c in table.get_row(name)]
      check("the columns", ok and [str(c.label) for c in table.columns.values()]
            == ["Name", "Size", "Modified", "Permissions", "Owner", "You", "ACL"])
      check("permissions and owner as ls -l", row("readonly")[3] == "-r--r--r--" and row("readonly")[4].count(":") == 1,
            row("readonly"))
      check("a file the user cannot read: no read (and no write)", row("secret")[5] == "no read no write", row("secret"))
      check("a read-only file: no write", row("readonly")[5] == "no write", row("readonly"))
      check("a file of the user: nothing", row("notes.txt")[5] == "", row("notes.txt"))
      status = str(app.screen.query_one("#dstatus").render())
      check("the directory itself in the status line", "drwx" in status and "read-only" not in status, status)
      await pilot.press("q")
  asyncio.run(main())
  os.chmod(home / "secret", 0o600)
