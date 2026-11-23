"""The file window (FilePickScreen): Reload (the button, r) lists the directory shown again: a new file is there, one
gone is not, the cursor stays on its file"""
import asyncio

from slurp.ui.app import Slurp
from slurp.ui.screens.files import FilePickScreen


async def until(pilot, cond, t=8):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_reload(check, tmp_path):
  (tmp_path / "a.log").write_text("a")
  (tmp_path / "b.log").write_text("b")
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None)
      picker = FilePickScreen("Choose a file", str(tmp_path / "b.log"), local=True)
      app.push_screen(picker)
      names = lambda: {e["name"] for e in picker.entries}
      ok = await until(pilot, lambda: names() == {"a.log", "b.log"} and (picker.current() or {}).get("name") == "b.log")
      check("listed, the cursor on the file", ok, (names(), picker.current()))
      check("…a Reload button", picker.query_one("#fp-reload").display)
      (tmp_path / "c.log").write_text("c")
      (tmp_path / "a.log").unlink()
      await pilot.click("#fp-reload")
      ok = await until(pilot, lambda: names() == {"b.log", "c.log"})
      check("Reload: the new file there, the one gone not", ok, names())
      check("…the cursor still on its file", (picker.current() or {}).get("name") == "b.log", picker.current())
      (tmp_path / "d.log").write_text("d")
      picker.query_one("#fp-list").focus()
      await pilot.press("r")
      ok = await until(pilot, lambda: "d.log" in names())
      check("r: the same", ok, names())
  asyncio.run(main())
