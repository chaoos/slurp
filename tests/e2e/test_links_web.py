"""A web link through the lab relay: a real ssh -N -L with the password reaches the web view (a test web server here);
forwarding anywhere else and a shell are refused."""
import asyncio
import os
import shlex

from aiohttp import web

from slurp.links import share as sh
from linkkit import blocking, client_env, free_port, Term, until

async def web_link(check, mgr, events, KNOWN):
  pass # (the test web servers: aiohttp does not run with eager tasks; the holder does not use them either)
  peers = []
  async def page(request):
    peers.append(request.transport.get_extra_info("peername")[1])
    return web.Response(text=f"the web view (token {request.query.get('token')})")
  app = web.Application(); app.add_routes([web.get("/", page)])
  runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
  view_port = site._server.sockets[0].getsockname()[1]
  async def secret(request): return web.Response(text="secret")
  other = web.Application(); other.add_routes([web.get("/", secret)])
  orun = web.AppRunner(other); await orun.setup(); osite = web.TCPSite(orun, "127.0.0.1", 0); await osite.start()
  other_port = osite._server.sockets[0].getsockname()[1]
  link = mgr.create(sh.Link("web", "this slurp (test)", "lab", lambda: [], relay="lab-relay", uses=None,
                            web_port=view_port, web_token="tok123"))
  ok = await until(lambda: link.ready(), 20)
  check("the web link's relay tunnel is up", ok, (link.relay_state, link.relay_error))
  cmds = sh.client_commands(link)
  tunnel = cmds[0][1]
  check("its commands: the tunnel (ssh -L 8000:127.0.0.1:<the web view>), then the URL with the token",
        tunnel.startswith(f"ssh -L 8000:127.0.0.1:{view_port} -J lab-relay -p {link.relay_port}") and
        cmds[1][1] == "http://localhost:8000/?token=tok123", [c[1] for c in cmds[:2]])
  local = free_port()
  argv = shlex.split(tunnel.replace("8000:", f"{local}:", 1))
  argv = [argv[0], "-o", f"UserKnownHostsFile={KNOWN}", "-o", "StrictHostKeyChecking=accept-new", "-o", "ExitOnForwardFailure=yes", *argv[1:]]
  t = Term(argv, client_env(link.password))
  async def get(port, path="/"):
    def fetch():
      import urllib.request
      try:
        return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).read().decode()
      except Exception as e:
        return f"error: {e}"
    return await blocking(fetch)
  body = ""
  for _ in range(60):
    await asyncio.sleep(0.25)
    body = await get(local, "/?token=tok123")
    if body.startswith("the web view"): break
  check("the user's ssh -N -L with the password: the browser reaches the web view", body == "the web view (token tok123)" and link.logins == 1, (body, t.out[-300:]))
  ok = await t.read_until(r"Ctrl-C", 10)
  out = t.out.decode(errors="replace")
  check("after the password, the user's terminal says the tunnel is open, with the URL and how to close it",
        ok and "tunnel to the web view is open" in out and "token=tok123" in out and "Keep this window open" in out, out[-400:])
  websess = [s for s in link.sessions if s.kind == "web"]
  check("the web view sees the browser coming from a port the session recorded (which session is this view)",
        peers and websess and peers[-1] in websess[0].web_ports, (peers, [s.web_ports for s in websess]))
  class FakeApp:
    active_view_port = peers[-1] if peers else 0
  from slurp.ui.app import Slurp as SlurpApp
  check("this_view: the session of the active browser, no other", websess and SlurpApp.this_view(FakeApp(), websess[0])
        and not SlurpApp.this_view(FakeApp(), sh.Session(link, "x")), "")
  check("the session is listed as a web session", any(s.kind == "web" for s in link.sessions) and any(e[0] == "login" for e in events), [s.kind for s in link.sessions])
  # forwarding elsewhere, and a shell, are refused
  local2 = free_port()
  host, port = link.target()
  argv2 = ["ssh", "-N", "-L", f"{local2}:127.0.0.1:{other_port}", "-J", link.jump(), "-p", str(port), "-o", f"HostKeyAlias={sh.host_alias()}",
           "-o", f"UserKnownHostsFile={KNOWN}", f"slurp@{host}"]
  t2 = Term(argv2, client_env(link.password))
  await asyncio.sleep(3)
  body = await get(local2)
  check("forwarding to any other port is refused", "secret" not in body, body)
  t3 = Term(["ssh", "-J", link.jump(), "-p", str(port), "-o", f"HostKeyAlias={sh.host_alias()}", "-o", f"UserKnownHostsFile={KNOWN}", f"slurp@{host}",
             "echo SECRET-$((6*7))"], client_env(link.password))
  await t3.read_until(r"failed|closed|refused", 15)
  code = await blocking(t3.wait, 10)
  check("a command is refused (the link only forwards, its session only shows the notice)", "SECRET-42" not in t3.out.decode(errors="replace")
        and code not in (None, 0), (code, t3.out[-200:]))
  t.send("\x03")
  ok = await t.read_until(r"closing the tunnel", 10)
  code = await blocking(t.wait, 10)
  check("Ctrl-C in the tunnel's window closes it", ok and code is not None, (code, t.out[-200:]))
  mgr.revoke(link)
  ok = await until(lambda: (mgr.tick(), link.relay_state == "closed")[1], 15) # (the app ticks every second)
  body = await get(local, "/?token=tok123")
  check("revoked: the tunnel closes, the web view is out of reach", ok and not body.startswith("the web view"), (link.relay_state, body))
  for term in (t, t2):
    try:
      os.kill(term.pid, 15)
    except OSError:
      pass
  await mgr.stopped(); await runner.cleanup(); await orun.cleanup()


def test_web_link(check, links, link_manager):
  mgr, events = link_manager
  asyncio.run(web_link(check, mgr, events, links.KNOWN))


async def web_link_expires(check, mgr, events, KNOWN):
  """When its lifetime ends, a web link ends: its sessions too (the browser that came in through it is out, a reload
  with the token too), not only its new logins"""
  async def page(request):
    return web.Response(text="the web view")
  app = web.Application(); app.add_routes([web.get("/", page)])
  runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
  view_port = site._server.sockets[0].getsockname()[1]
  link = mgr.create(sh.Link("web", "this slurp (test)", "lab", lambda: [], relay="lab-relay", uses=None,
                            web_port=view_port, web_token="tok456", expiry=12))
  ok = await until(lambda: link.ready(), 20)
  check("the web link is up (12 s to live)", ok, (link.relay_state, link.relay_error))
  local = free_port()
  argv = shlex.split(sh.client_commands(link)[0][1].replace("8000:", f"{local}:", 1))
  argv = [argv[0], "-o", f"UserKnownHostsFile={KNOWN}", "-o", "StrictHostKeyChecking=accept-new", "-o", "ExitOnForwardFailure=yes", *argv[1:]]
  t = Term(argv, client_env(link.password))
  async def get(path="/?token=tok456"):
    def fetch():
      import urllib.request
      try:
        return urllib.request.urlopen(f"http://127.0.0.1:{local}{path}", timeout=5).read().decode()
      except Exception as e:
        return f"error: {e}"
    return await blocking(fetch)
  body = ""
  for _ in range(40):
    await asyncio.sleep(0.25)
    body = await get()
    if body == "the web view": break
  check("before its end: the browser reaches the web view", body == "the web view", (body, t.out[-300:]))
  ok = await until(lambda: (mgr.tick(), link.expired())[1], 20) # (the app ticks every second)
  for _ in range(20):
    mgr.tick(); await asyncio.sleep(0.25)
  body = await get()
  check("expired: the browser that came in through it no longer reaches the web view (a reload with the token neither)",
        ok and body != "the web view", body)
  check("…its sessions ended, it is finished, its tunnel closed", not link.live_sessions and link.finished
        and await until(lambda: (mgr.tick(), link.relay_state == "closed")[1], 15), ([s.ended for s in link.sessions], link.relay_state))
  try:
    os.kill(t.pid, 15)
  except OSError:
    pass
  await mgr.stopped(); await runner.cleanup()


def test_web_link_expires(check, links, link_manager):
  mgr, events = link_manager
  asyncio.run(web_link_expires(check, mgr, events, links.KNOWN))
