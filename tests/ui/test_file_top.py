"""A text file opens at its top (a large one: its beginning, G or f its end); one that is followed (a running job's
log) at its end"""
import asyncio
import shutil

import pytest

from slurp.state import TAIL_BYTES
from slurp.ui.app import Slurp
from slurp.ui.screens.files import FileScreen, open_path


@pytest.fixture
def home(monkeypatch, tmp_path):
  home = tmp_path / "home"; home.mkdir()
  (home / "small.txt").write_text("".join(f"line {i}\n" for i in range(500)))
  (home / "running.out").write_text("".join(f"step {i}\n" for i in range(500)))
  line = "x" * 99 + "\n"
  (home / "large.log").write_text("FIRST LINE\n" + line * (TAIL_BYTES // 100 + 2000) + "LAST LINE\n")
  monkeypatch.setenv("HOME", str(home)); monkeypatch.setenv("PATH", "/usr/bin:/bin")
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


def test_a_text_file_opens_at_its_top(check, home):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      loaded = lambda: isinstance(app.screen, FileScreen) and app.screen.file_size is not None
      open_path(app, ["Local"], str(home / "small.txt"))
      await until(pilot, loaded); await pilot.pause(0.2)
      area = app.screen.area
      check("a file: at its top (cursor and view)", area.cursor_location == (0, 0) and area.scroll_y == 0, (area.cursor_location, area.scroll_y))
      open_path(app, ["Local"], str(home / "large.log"))
      await until(pilot, lambda: loaded() and app.screen.path.endswith("large.log")); await pilot.pause(0.2)
      f = app.screen; area = f.area
      status = str(f.query_one("#fstatus").render())
      check("a large file: its beginning, at the top, the status says so", area.text.startswith("FIRST LINE") and "LAST LINE" not in area.text
            and area.cursor_location == (0, 0) and "showing the first" in status and area.text.endswith("\n"), status)
      await pilot.press("G")
      ok = await until(pilot, lambda: "LAST LINE" in f.area.text)
      check("G: its end, at the bottom", ok and f.area.cursor_location[0] == f.area.document.line_count - 1 and not f.head_only)
      app.show_view(None); await pilot.pause(0.2)
      open_path(app, ["Jobs", "1"], str(home / "running.out"), follow=True) # (a running job's log: followed)
      ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and app.screen.following and app.screen.file_size is not None)
      await pilot.pause(0.2)
      area = app.screen.area
      check("followed: at its end", ok and area.cursor_location[0] >= area.document.line_count - 2, area.cursor_location)
      await pilot.press("q")
  asyncio.run(main())
