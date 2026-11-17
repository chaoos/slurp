"""slurp in your browser: the text marked goes to the browser (through the session holder), whose Ctrl+C copies it
in the key's own event; in a terminal nothing is sent"""
import asyncio

from textual import events
from textual.widgets.text_area import Selection

from slurp.links import holder
from slurp.ui.app import Slurp
from slurp.ui.screens.dialogs import CopyScreen


async def until(pilot, cond, t=5):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_the_selection_goes_to_the_browser(check, monkeypatch):
  sent = []
  monkeypatch.setattr(holder, "under_holder", lambda: True)
  monkeypatch.setattr(holder, "control", lambda cmd, **kw: sent.append((cmd, kw.get("text"))) or {"ok": True})
  async def main():
    app = Slurp()
    async with app.run_test(size=(120, 40)) as pilot:
      await until(pilot, lambda: app.main is not None)
      app.view_kind = "web"
      app.push_screen(CopyScreen("hello world\nsecond line", "the text")); await pilot.pause(0.2)
      area = app.screen.query_one("#copy-text")
      area.selection = Selection((0, 0), (0, 5))
      ok = await until(pilot, lambda: ("selection", "hello") in sent)
      check("a selection in a text area: to the browser", ok, sent)
      area.selection = Selection((0, 0), (0, 0)); await until(pilot, lambda: sent[-1] == ("selection", ""))
      n = len(sent)
      area.selection = Selection((1, 0), (1, 0)); await pilot.pause(0.2)
      check("then an empty one once (the cursor moving sends nothing more)", sent[-1] == ("selection", "") and len(sent) == n, sent)
      app.screen.get_selected_text = lambda: "marked text"
      app.screen.post_message(events.TextSelected())
      ok = await until(pilot, lambda: sent[-1] == ("selection", "marked text"))
      check("text marked with the mouse: to the browser", ok, sent[-1])
      app.screen.post_message(events.TextSelected())
      ok = await until(pilot, lambda: sent[-2:] == [("selection", "marked text")] * 2)
      check("the same text marked again: sent again (the page forgot it on the click)", ok, sent[-2:])
      app.view_kind = "terminal"; n = len(sent)
      area.selection = Selection((1, 0), (1, 6)); await pilot.pause(0.3)
      check("a terminal as the active view: nothing sent (its own copy works there)", len(sent) == n, sent[n:])
      await pilot.press("escape")
  asyncio.run(main())
