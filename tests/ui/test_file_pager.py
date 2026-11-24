"""A large log, like less: a window of it is shown (never the whole file), read on while scrolling past its ends;
g / G its start and end, : a line or a percentage, / searches the whole file; the line numbers are the file's"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens import files
from slurp.ui.screens.files import FileScreen, open_path

N = 60000 # lines of the log: "line 000001 ……" (100 bytes each), about 6 MB


@pytest.fixture
def home(monkeypatch, tmp_path):
  home = tmp_path / "home"; home.mkdir()
  with open(home / "big.log", "w") as f:
    for i in range(1, N + 1):
      f.write(f"line {i:06d} " + ("needle " if i == 45678 else "x" * 7) + "." * 81 + "\n")
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


def number_at_cursor(f):
  """(the line number the gutter shows at the cursor, the number written in that line)"""
  a = f.area
  row = a.cursor_location[0]
  if row and not a.document.get_line(row): # (the empty line after the last newline: the one before it)
    row -= 1
  text = a.document.get_line(row)
  return (a.line_number_start + row if a.show_line_numbers else None), (int(text[5:11]) if text.startswith("line ") else None)


def test_a_large_file_like_less(check, home):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      open_path(app, ["Local"], str(home / "big.log"))
      ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and app.screen.file_size is not None and not app.screen.reading)
      f = app.screen; a = f.area
      await pilot.pause(0.3)
      check("opens at its start: one part only", ok and f.start_pos == 0 and f.file_pos <= files.CHUNK and a.text.startswith("line 000001"), f.chunks)
      rows = a.document.line_count
      # scrolling down past the end of what is shown reads on
      a.move_cursor((rows - 1, 0)); a.scroll_end(animate=False)
      ok = await until(pilot, lambda: a.document.line_count > rows and not f.reading)
      check("past the end shown: the next part is read", ok and len(f.chunks) == 2, f.chunks)
      check("the line numbers stay the file's", number_at_cursor(f)[0] == number_at_cursor(f)[1], number_at_cursor(f))
      for _ in range(6): # (on and on: the window stays bounded, the first parts go; each move waits for its part, read
        # in the background: a move while a part is read, or before the read began, reads nothing more)
        pos = f.file_pos
        for _ in range(20 if pos < f.file_size else 0):
          a.move_cursor((a.document.line_count - 1, 0)); a.scroll_end(animate=False)
          if await until(pilot, lambda: f.file_pos > pos and not f.reading, 0.5):
            break
      check("a bounded window: the parts before went", f.file_pos - f.start_pos <= files.WINDOW and f.start_pos > 0, (f.start_pos, f.file_pos))
      check("and the numbers still fit", number_at_cursor(f)[0] == number_at_cursor(f)[1] and f.first_line > 1, (number_at_cursor(f), f.first_line))
      # G: the end, the numbers counted (wc -l of what comes before)
      await pilot.press("G")
      ok = await until(pilot, lambda: a.text.rstrip().endswith("." * 81) and f.file_pos == f.file_size and f.first_line is not None and not f.reading)
      await pilot.pause(0.2)
      last = a.document.get_line(a.document.line_count - 2)
      check("G: the file's end", ok and last.startswith(f"line {N:06d}"), last[:20])
      a.move_cursor((a.document.line_count - 2, 0))
      check("its line numbers counted", number_at_cursor(f) == (N, N), number_at_cursor(f))
      # up past the top shown: the part before
      before = f.start_pos
      a.move_cursor((0, 0)); a.scroll_home(animate=False)
      ok = await until(pilot, lambda: f.start_pos < before and not f.reading)
      await pilot.pause(0.2)
      check("past the top shown: the part before is read, the view stays on the same line", ok and number_at_cursor(f)[0] == number_at_cursor(f)[1], number_at_cursor(f))
      # g: the start
      await pilot.press("g")
      ok = await until(pilot, lambda: f.start_pos == 0 and not f.reading); await pilot.pause(0.2)
      check("g: the file's start", ok and a.cursor_location == (0, 0) and f.first_line == 1)
      # : a line, a percentage
      await pilot.press("colon"); await pilot.pause(0.1)
      f.query_one("#goto").value = "30000"; await pilot.press("enter")
      ok = await until(pilot, lambda: number_at_cursor(f) == (30000, 30000) and not f.reading)
      check(": 30000: that line, numbered", ok, number_at_cursor(f))
      await pilot.press("colon"); await pilot.pause(0.1)
      f.query_one("#goto").value = "75%"; await pilot.press("enter")
      ok = await until(pilot, lambda: number_at_cursor(f)[1] is not None and abs(number_at_cursor(f)[1] - 45000) < 50 and not f.reading)
      check(": 75%: three quarters into the file", ok, number_at_cursor(f))
      # / searches the whole file, not only what is shown
      await pilot.press("g"); await until(pilot, lambda: f.start_pos == 0 and not f.reading); await pilot.pause(0.2)
      await pilot.press("slash"); f.query_one("#search").value = "NEEDLE"; await pilot.press("enter")
      ok = await until(pilot, lambda: number_at_cursor(f)[1] == 45678 and a.selected_text.lower() == "needle" and not f.reading
                       and f.first_line is not None, 15) # (the lines before it are counted in the background)
      check("/ a term far below what is shown: found in the file, shown and selected", ok, (number_at_cursor(f), a.selected_text))
      check("numbered", number_at_cursor(f)[0] == 45678, number_at_cursor(f))
      await pilot.press("G"); await until(pilot, lambda: f.file_pos == f.file_size and not f.reading); await pilot.pause(0.2)
      await pilot.press("N")
      ok = await until(pilot, lambda: number_at_cursor(f)[1] == 45678 and not f.reading, 15)
      check("N from the end: the match above, in the file", ok, number_at_cursor(f))
      await pilot.press("n"); await until(pilot, lambda: not f.reading, 15); await pilot.pause(0.3)
      check("n: none below, said so", number_at_cursor(f)[1] == 45678)
      # less keys
      row = a.cursor_location[0]
      await pilot.press("j"); await pilot.press("j"); await pilot.press("k")
      check("j / k: a line down, up", a.cursor_location[0] == row + 1, (row, a.cursor_location))
      await pilot.press("space")
      check("space: a page down", a.cursor_location[0] > row + 10, a.cursor_location)
      await pilot.press("q")
  asyncio.run(main())


def test_following_a_growing_log_stays_bounded(check, home):
  log = home / "growing.out"
  log.write_text("".join(f"line {i:06d} " + "." * 88 + "\n" for i in range(1, 1001)))
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      open_path(app, ["Jobs", "1"], str(log), follow=True)
      ok = await until(pilot, lambda: isinstance(app.screen, FileScreen) and app.screen.file_size is not None and not app.screen.reading)
      f = app.screen; a = f.area
      n = 1000
      for _ in range(12): # (about 7 MB appended, a part each second)
        with open(log, "a") as out:
          out.write("".join(f"line {i:06d} " + "." * 88 + "\n" for i in range(n + 1, n + 6001)))
        n += 6000
        await until(pilot, lambda: f.file_pos == log.stat().st_size, 15)
      check("followed to the end", f.file_pos == log.stat().st_size, (f.file_pos, log.stat().st_size))
      check("the window stays bounded", f.file_pos - f.start_pos <= files.WINDOW + files.CHUNK and f.start_pos > 0, (f.start_pos, f.file_pos, f.chunks))
      a.move_cursor((a.document.line_count - 2, 0))
      check("the last line, numbered as in the file", ok and number_at_cursor(f) == (n, n), number_at_cursor(f))
      await pilot.press("q")
  asyncio.run(main())
