"""slurp in your browser: Ctrl+C copies the text marked in slurp. The holder's real page in headless Chromium (driven
over the DevTools protocol, whose key events are real ones): the app's selection arrives on the websocket, Ctrl+C puts
it on the clipboard, also without the Clipboard API (plain HTTP to another host); a click forgets it."""
import asyncio
import os
import shutil
import subprocess
import tempfile

import aiohttp
import pytest

from slurp.links import holder

BROWSER = next((shutil.which(b) for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable") if shutil.which(b)), None)


async def browser_part(check):
  h = holder.Holder("/nonexistent", [])
  master, slave = os.openpty()
  h.master = master
  port = await h.web_start()
  snap = "/snap/" in os.path.realpath(BROWSER) or BROWSER.startswith("/snap/")
  profile = tempfile.mkdtemp(prefix="slurp-test-profile-", dir=os.path.expanduser("~") if snap else None) # (a snap: not /tmp)
  proc = subprocess.Popen([BROWSER, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
                           "--no-sandbox", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  try:
    debug_port = None
    for _ in range(150):
      try:
        debug_port = int(open(os.path.join(profile, "DevToolsActivePort")).readline())
        break
      except (OSError, ValueError):
        await asyncio.sleep(0.1)
    if debug_port is None:
      pytest.skip(f"{BROWSER} did not start its DevTools endpoint")
    async with aiohttp.ClientSession() as s:
      pages = []
      for _ in range(50):
        try:
          async with s.get(f"http://127.0.0.1:{debug_port}/json") as r:
            pages = [p for p in await r.json() if p["type"] == "page"]
        except aiohttp.ClientError:
          pass
        if pages:
          break
        await asyncio.sleep(0.1)
      async with s.get(f"http://127.0.0.1:{debug_port}/json/version") as r:
        browser_ws = await s.ws_connect((await r.json())["webSocketDebuggerUrl"])
      ws = await s.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=0)
      ids = iter(range(1, 10**6))
      async def cdp(method, socket=ws, **params):
        i = next(ids)
        await socket.send_json({"id": i, "method": method, "params": params})
        while True:
          m = await socket.receive_json(timeout=15)
          if m.get("id") == i:
            return m.get("result", m.get("error"))
      async def ctrl_c():
        for kind in ("keyDown", "keyUp"):
          await cdp("Input.dispatchKeyEvent", type=kind, modifiers=2, key="c", code="KeyC", windowsVirtualKeyCode=67, nativeVirtualKeyCode=67)
        await asyncio.sleep(0.3)
      async def clipboard():
        got = await cdp("Runtime.evaluate", expression="window.realClipboard.readText()", awaitPromise=True)
        return (got or {}).get("result", {}).get("value")
      origin = f"http://127.0.0.1:{port}"
      await cdp("Browser.grantPermissions", socket=browser_ws, origin=origin, permissions=["clipboardReadWrite", "clipboardSanitizedWrite"])
      await cdp("Page.navigate", url=f"{origin}/?token={h.web_token}")
      for _ in range(100):
        if h.active is not None and h.active.kind == "web":
          break
        await asyncio.sleep(0.1)
      check("the page connected: the browser is the active view", h.active is not None and h.active.kind == "web")
      await asyncio.sleep(0.5)
      # (the test reads the clipboard with the API; the page goes without it, as over plain HTTP to another host)
      await cdp("Runtime.evaluate", expression="window.realClipboard = navigator.clipboard; "
                "Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true}); 1")
      await h.control({"cmd": "selection", "text": "marked in slurp\nsecond line"}); await asyncio.sleep(0.3)
      await ctrl_c()
      got = await clipboard()
      check("Ctrl+C copies the text marked (without the Clipboard API too)", got == "marked in slurp\nsecond line", got)
      await h.control({"cmd": "selection", "text": "another"}); await asyncio.sleep(0.3)
      await ctrl_c()
      check("another selection: that one", await clipboard() == "another")
      await cdp("Runtime.evaluate", expression="window.realClipboard.writeText('before')", awaitPromise=True)
      for kind in ("mousePressed", "mouseReleased"):
        await cdp("Input.dispatchMouseEvent", type=kind, x=40, y=40, button="left", clickCount=1)
      await ctrl_c()
      check("after a click (a new selection starts) Ctrl+C copies nothing old", await clipboard() == "before")
      await ws.close(); await browser_ws.close()
  finally:
    proc.terminate()
    try:
      proc.wait(5)
    except subprocess.TimeoutExpired:
      proc.kill()
    await h.web_stop()
    os.close(master); os.close(slave)
    shutil.rmtree(profile, ignore_errors=True)


@pytest.mark.skipif(BROWSER is None, reason="needs Chromium or Chrome")
def test_ctrl_c_copies_the_selection_in_the_browser(check):
  asyncio.run(browser_part(check))
