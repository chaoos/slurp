"""A newer slurp: said at the start, ⬆ in the title bar opens how to update (the command to copy, start again); Skip
this version"""
import asyncio

from slurp import update
from slurp.config import load_config
from slurp.ui.app import Slurp
from slurp.ui.screens.dialogs import UpdateScreen


async def until(pilot, cond, t=5):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_a_newer_version(check, monkeypatch):
  copied = []
  monkeypatch.setattr("slurp.ui.screens.dialogs.copy_text", lambda app, text, what="": copied.append(text))
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.screen.query("#update")) # (the first window there)
      button = app.screen.query_one("#update")
      check("no newer version known: nothing", not button.display)
      app.version_found("0.0.1")
      await pilot.pause(1.2)
      check("an older or the same version: nothing", app.new_version is None and not button.display)
      app.version_found("99.0.0")
      # (and laid out: just shown, its region is still empty, at the left: a click there hit the menu, on a busy runner)
      ok = await until(pilot, lambda: button.display and str(button.label) == "⬆ slurp 99.0.0" and button.region.width > 0)
      notes = [str(n.message) for n in app._notifications]
      check("a newer one: said, and in the title bar", ok and any("slurp 99.0.0 is available" in n for n in notes), notes)
      await pilot.click("#update")
      ok = await until(pilot, lambda: isinstance(app.screen, UpdateScreen))
      scr = app.screen
      text = " ".join(str(w.render()) for w in scr.query("Static"))
      how, command = update.how_to_update()
      check("how to update: the version, what changed, how it is installed, the command, start again", ok and "slurp 99.0.0 is available" in text
            and update.CHANGELOG in text and how in text and command in text and "quit it (q" in text, text[:300])
      scr.query_one("#up-copy").press(); await pilot.pause(0.2)
      check("Copy the command", copied == [command], copied)
      scr.query_one("#up-skip").press(); await pilot.pause(1.2)
      check("Skip this version: no reminder (kept), gone from the title bar", load_config().get("update_skip") == "99.0.0"
            and app.new_version is None and not button.display)
      app.version_found("99.0.0"); await pilot.pause(0.3)
      check("…also not at the next start", app.new_version is None)
      app.version_found("99.0.1"); await pilot.pause(0.3)
      check("a newer one than the skipped: said again", app.new_version == "99.0.1")
  asyncio.run(main())
