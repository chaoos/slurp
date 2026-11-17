"""A web link that ends: the title bar counts down (left of ?); a few minutes before, a warning that can extend it
(its time limit) or explains (its relay's hour); once per deadline"""
import asyncio
import time

from slurp.links import share as sh
from slurp.ui.app import Slurp
from slurp.ui.screens.links import LinkScreen, WebEndingScreen


async def until(pilot, cond, t=5):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def web_link(expiry_left):
  link = sh.Link("web", "this slurp (test)", "Local", lambda: [], relay="ssh-j.com", uses=None, web_port=1, web_token="t" * 24)
  link.created = time.time() - 600
  link.expiry = 600 + expiry_left
  return link


def test_the_countdown_and_the_warning(check):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None)
      timer = app.screen.query_one("#web-timer")
      check("no web link: no timer", not timer.display)
      link = web_link(20 * 60 + 5)
      app.link_manager.links.append(link)
      ok = await until(pilot, lambda: timer.display and str(timer.label)[:6] in ("⏱ 20:0", "⏱ 19:5")) # (20 min: give or take a slow start)
      check("a web link with a time limit: the countdown, left of ?", ok and not timer.has_class("-ending")
            and timer.parent.children.index(timer) == timer.parent.children.index(app.screen.query_one("#view-help")) - 1, str(timer.label))
      await pilot.click("#web-timer")
      ok = await until(pilot, lambda: isinstance(app.screen, LinkScreen))
      check("a click: the link's window", ok and app.screen.link is link)
      await pilot.press("escape"); await pilot.pause(0.2)
      link.expiry -= 16 * 60 # (4 min left)
      ok = await until(pilot, lambda: isinstance(app.screen, WebEndingScreen) and app.screen.query("#we-close"))
      check("5 min before: the warning, Extend offered", ok and app.screen.what == "expiry" and app.screen.query("#we-extend"))
      before = link.expires
      app.screen.query_one("#we-extend").press(); await pilot.pause(0.3) # (not a click: a notification can take it)
      check("Extend: an hour from now", link.expires > before + 50 * 60 and abs(link.expires - time.time() - 3600) < 5, link.expires - time.time())
      ok = await until(pilot, lambda: str(timer.label).startswith("⏱ 59:") or str(timer.label).startswith("⏱ 1:00:"))
      check("the countdown follows; no warning now", ok and not isinstance(app.screen, WebEndingScreen) and not timer.has_class("-ending"), str(timer.label))
      link.expiry = None # (no time limit) …
      link.relay_state, link.relay_until = "ready", time.time() + 3 * 60 # … but the relay's hour ends in 3 min
      ok = await until(pilot, lambda: isinstance(app.screen, WebEndingScreen) and app.screen.query("#we-close"))
      scr = app.screen
      check("the relay ends: the warning says so, nothing to extend", ok and scr.what == "relay" and not scr.query("#we-extend")
            and "new address" in str(scr.query_one("#we-text").render()) and timer.has_class("-ending"))
      scr.query_one("#we-close").press(); await pilot.pause(1.5)
      check("warned once per deadline", not isinstance(app.screen, WebEndingScreen))
      link.revoked = True
      ok = await until(pilot, lambda: not timer.display)
      check("a link that is done (no logins, no sessions): no timer", ok)
      app.link_manager.links.remove(link)
  asyncio.run(main())
