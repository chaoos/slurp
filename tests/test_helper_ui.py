"""The link window's helper section and the setup window (Textual run_test, no lab)"""
import asyncio
from textual.app import App
from slurp import share as sh, winhelper
from slurp.app import Slurp as SlurpApp
from slurp.screens import links as L


class T(App):
  is_web_flag = False
  view_kind = "terminal"
  on_client = SlurpApp.on_client
  open_on_client = SlurpApp.open_on_client
  deliver_to_client = SlurpApp.deliver_to_client
  opened = []
  def open_url(self, url, new_tab=True): self.opened.append(url)
async def main(check, copied):
  link = sh.Link("drive", "drive: daint /users/x", "daint", lambda: [], relay="ssh-j.com", path="/users/x")
  link.relay_state = "ready"
  app = T()
  async with app.run_test(size=(120, 50)) as pilot:
    app.push_screen(L.LinkScreen(link)); await pilot.pause(1.4)
    scr = app.screen
    check("the helper section is there", bool(scr.query("#lv-helper")))
    await pilot.click("#lv-helper-open"); await pilot.pause(0.3)
    check("Open on my computer in a terminal: copies the link instead (Win+R)", copied and copied[-1][0] == winhelper.link_url(link)
          and "Win+R" in copied[-1][1], copied[-1:])
    await pilot.click("#lv-helper-copy"); await pilot.pause(0.2)
    check("Copy helper link", copied[-1][0].startswith("slurp://drive?"))
    await pilot.click("#lv-helper-setup"); await pilot.pause(0.5)
    hs = app.screen
    check("Set up the helper opens its window", type(hs).__name__ == "HelperSetupScreen")
    check("Download is off in a terminal", hs.query_one("#hs-download").disabled)
    await pilot.click("#hs-copy"); await pilot.pause(0.2)
    check("Copy the setup command", copied[-1][0] == winhelper.setup_command())
    md = hs.query_one("Markdown")
    await pilot.press("escape"); await pilot.pause(0.2)
    check("Escape closes it", type(app.screen).__name__ == "LinkScreen")
    key = sh.Link("shell", "k", "t", lambda: [], auth="key", public_key="ssh-ed25519 AAAA"); key.relay_state = "ready"
    await pilot.press("escape"); await pilot.pause(0.2)
    app.push_screen(L.LinkScreen(key)); await pilot.pause(1.4)
    check("a key link has no helper section", not app.screen.query("#lv-helper"))


def test_helper_ui(check, monkeypatch):
  copied = []
  monkeypatch.setattr(L, "copy_text", lambda app, text, what="": copied.append((text, what)))
  asyncio.run(main(check, copied))
