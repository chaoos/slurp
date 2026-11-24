"""Search in the directory view: this directory as you type (a part of names, or a glob), or with Subdirectories in the
background: results come in batches, the window stays usable meanwhile, Esc stops the search, then shows all again"""
import asyncio
import os
import shutil
import time

import pytest

from slurp.cluster import listing
from slurp.ui.app import Slurp
from slurp.ui.screens.files import DirScreen, FileScreen, open_path


@pytest.fixture
def home(monkeypatch, tmp_path):
  home = tmp_path / "home"
  for i in range(12):
    (home / f"run{i:02d}" / "out").mkdir(parents=True)
    (home / f"run{i:02d}" / "out" / f"result{i}.txt").write_text("x")
  (home / "notes.txt").write_text("x"); (home / "data.h5").write_text("x")
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


def test_search_in_the_directory_view(check, home, monkeypatch):
  real = listing.search_batch
  def slow(dirs, text): # (a slow cluster: every round trip takes a while)
    time.sleep(0.4)
    return real(dirs, text)
  monkeypatch.setattr(listing, "SEARCH_BATCH", 4)
  async def main():
    app = Slurp()
    async with app.run_test(size=(150, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      open_path(app, ["Local"], str(home))
      await until(pilot, lambda: isinstance(app.screen, DirScreen) and app.screen.entries)
      d = app.screen; table = d.query_one("#dir")
      rows = lambda: [str(table.coordinate_to_cell_key((i, 0)).row_key.value) for i in range(table.row_count)]
      await pilot.press("slash", *"*.txt")
      ok = await until(pilot, lambda: rows() == ["..", "notes.txt"])
      check("this directory, as you type: a glob (*.txt)", ok, rows())
      d.query_one("#dfilter").value = ""; await pilot.press(*"run0")
      ok = await until(pilot, lambda: rows()[1:] and all(r.startswith("run0") for r in rows()[1:]) and len(rows()) == 11)
      check("a part of the names (run0)", ok, rows())
      monkeypatch.setattr(listing, "search_batch", slow)
      d.query_one("#dfilter").value = ""; await pilot.press(*"result")
      await pilot.press("ctrl+r")
      ok = await until(pilot, lambda: d.searching is not None and d.results)
      check("Subdirectories (Ctrl+R): searched in the background, results come while it runs", ok and d.searching is not None,
            (d.searching, len(d.results or [])))
      status = str(d.query_one("#dstatus").render())
      check("the status says it searches, and how far", "searching" in status and "Esc stops" in status, status)
      table.focus(); before = table.cursor_row
      t0 = time.monotonic(); await pilot.press("down")
      check("meanwhile the window answers at once (the cursor moves)", table.cursor_row != before or table.row_count < 2
            and time.monotonic() - t0 < 0.3, (before, table.cursor_row))
      await pilot.press("escape")
      n = len(d.results)
      check("Esc stops the search, its results stay", d.searching is None and d.results is not None)
      await pilot.pause(1.0)
      check("nothing comes in after", len(d.results) == n, (n, len(d.results)))
      monkeypatch.setattr(listing, "search_batch", real)
      d.query_one("#drecursive").value = False; d.query_one("#drecursive").value = True
      ok = await until(pilot, lambda: d.searching is None and d.results is not None and len(d.results) == 12)
      check("all of them, with their paths below here", ok and sorted(e["name"] for e in d.results)[0] == "run00/out/result0.txt",
            sorted(e["name"] for e in (d.results or []))[:2])
      check("no ../ among the results", ".." not in rows())
      table.focus(); table.move_cursor(row=rows().index("run03/out/result3.txt")); await pilot.press("enter")
      ok = await until(pilot, lambda: isinstance(app.screen, FileScreen))
      check("a result opens (a file in its subtab)", ok and app.screen.path == os.path.join(str(home), "run03/out/result3.txt"))
      app.show_view(app.cluster.view.views[0]); await pilot.pause(0.3)
      await pilot.press("escape")
      ok = await until(pilot, lambda: d.results is None and ".." in rows() and len(rows()) == 15)
      check("Esc again: no search, the listing as it was", ok and not d.query_one("#dsearch").has_class("visible"), rows()[:4])
      await pilot.press("q")
  asyncio.run(main())
