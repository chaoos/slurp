"""The slurp helper for Linux (slurp/links/linux) with real links through the lab relay: installed by its setup into a
home of its own, then a drive (sshfs), a web view (ssh -L) and a shell (in a pseudo terminal), each opened the way the
browser opens a slurp:// link (--batch: no dialogs; the password on stdin); and what it refuses."""
import asyncio
import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request

from aiohttp import web

from slurp.links import linux
from slurp.links import share as sh
from slurp.links.windows import winhelper
from linkkit import blocking, Term, until


def install(root):
  """The setup, as the user runs it (sh slurp-helper-setup.sh), in a home of its own -> (helper, env)"""
  home = os.path.join(root, "home")
  shutil.rmtree(root, ignore_errors=True)
  os.makedirs(home)
  env = os.environ | {"HOME": home, "XDG_DATA_HOME": os.path.join(home, ".local/share"), "XDG_STATE_HOME": os.path.join(home, ".local/state"),
                      "XDG_CONFIG_HOME": os.path.join(home, ".config")}
  for var in ("DISPLAY", "WAYLAND_DISPLAY", "SLURP_HELPER_TERMINAL"):
    env.pop(var, None)
  setup = os.path.join(root, linux.SETUP_NAME)
  with open(setup, "wb") as f:
    f.write(linux.setup_file())
  r = subprocess.run(["sh", setup], env=env, capture_output=True, text=True, timeout=60)
  return os.path.join(home, ".local/share/slurp-helper/slurp-helper"), env, r

def connections(env):
  conns = os.path.join(env["XDG_STATE_HOME"], "slurp-helper", "connections")
  out = []
  for cid in sorted(os.listdir(conns)) if os.path.isdir(conns) else []:
    try:
      out.append(json.load(open(os.path.join(conns, cid, "state.json"))))
    except (OSError, ValueError):
      pass
  return out

async def helper_links(check, mgr, S):
  root = os.path.join(S, "linhelper")
  helper, env, r = await blocking(install, root)
  check("the setup installs the helper, registers slurp:// and trusts this slurp", r.returncode == 0 and os.access(helper, os.X_OK)
        and "x-scheme-handler/slurp" in open(os.path.join(env["XDG_DATA_HOME"], "applications/slurp-helper.desktop")).read()
        and sh.host_fingerprint() in open(os.path.join(env["XDG_DATA_HOME"], "slurp-helper/trusted.txt")).read(), r.stdout + r.stderr)
  check("the setup command is one line, the same setup", "\n" not in linux.setup_command() and linux.setup_command().endswith("| sh"))
  version = subprocess.run([helper, "--version"], capture_output=True, text=True, env=env).stdout
  from slurp import __version__
  check("the helper has slurp's version", version.strip() == f"slurp helper {__version__}", version)

  def open_link(url, password, *extra):
    return subprocess.Popen([helper, url, "--batch", "--password-stdin", *extra], env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

  # ---- a drive: the folder given, sshfs through the relay, then stop unmounts it
  d = os.path.join(S, "linhome"); shutil.rmtree(d, ignore_errors=True); os.makedirs(os.path.join(d, "sub"))
  open(os.path.join(d, "hello.txt"), "w").write("hi from the cluster\n")
  sftp = next(p for p in sh.SFTP_SERVERS if os.path.exists(p))
  link = mgr.create(sh.Link("drive", "drive (linux helper)", "lab", lambda: [sftp, "-d", d], relay="lab-relay", uses=None, path=d,
                            dirs=[("HOME", d)]))
  ok = await until(lambda: link.ready(), 20)
  check("the drive link's relay tunnel is up", ok, (link.relay_state, link.relay_error))
  url = winhelper.link_url(link)
  p = open_link(url, link.password)
  p.stdin.write(link.password + "\n"); p.stdin.close()
  mnt = os.path.join(env["HOME"], "slurp", "drive_linux_helper", "HOME")
  ok = await until(lambda: any(c["state"] == "connected" for c in connections(env)) or p.poll() is not None, 60)
  check("a drive: mounted at ~/slurp/<link>/HOME", ok and mounted(mnt), (p.poll(), p.poll() is not None and p.stderr.read()[-400:]))
  if mounted(mnt):
    text = await blocking(lambda: open(os.path.join(mnt, "hello.txt")).read())
    check("files are read through it", text == "hi from the cluster\n", text)
    conns = connections(env)
    check("it is an open connection, with its mount", len(conns) == 1 and conns[0]["kind"] == "drive" and conns[0]["mount"] == mnt
          and conns[0]["state"] == "connected", conns)
    listed = subprocess.run([helper, "list"], capture_output=True, text=True, env=env).stdout
    check("slurp-helper list shows it", "Drive: drive (linux helper)" in listed and mnt in listed, listed)
    s = link.live_sessions[0] if link.live_sessions else None
    check("slurp sees its session", s is not None and s.kind == "drive" and s.path == d, s)
    stopped = subprocess.run([helper, "stop", "all"], capture_output=True, text=True, env=env)
    ok = await until(lambda: p.poll() is not None and not mounted(mnt), 20)
    check("stop: unmounted, the helper ends, nothing left", ok and stopped.returncode == 0 and not os.path.exists(mnt)
          and not connections(env), (stopped.stdout, p.poll()))
    check("…and the session at slurp ends", await until(lambda: not link.live_sessions, 15))
  else:
    p.kill()

  p = open_link(url, "wrong")
  p.stdin.write("wrong\n"); p.stdin.close()
  code = await blocking(p.wait, 60)
  err = p.stderr.read()
  check("a wrong password: no mount, it says why", code == 1 and "wrong password" in err and not os.path.exists(mnt), (code, err[-300:]))

  # ---- a web view: ssh -L with the password, the local URL answers
  async def page(request):
    return web.Response(text=f"the web view (token {request.query.get('token')})")
  app = web.Application(); app.add_routes([web.get("/", page)])
  runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
  view_port = site._server.sockets[0].getsockname()[1]
  wlink = mgr.create(sh.Link("web", "this slurp (linux helper)", "lab", lambda: [], relay="lab-relay", uses=None,
                             web_port=view_port, web_token="tok123tok123"))
  await until(lambda: wlink.ready(), 20)
  p = open_link(winhelper.link_url(wlink), wlink.password)
  p.stdin.write(wlink.password + "\n"); p.stdin.close()
  ok = await until(lambda: any(c.get("url") and c["state"] == "connected" for c in connections(env)) or p.poll() is not None, 60)
  conn = next((c for c in connections(env) if c.get("url")), None)
  body = ""
  if ok and conn:
    body = await blocking(lambda: urllib.request.urlopen(conn["url"], timeout=5).read().decode())
  check("a web view: its local URL (with the token) reaches the web view", body == "the web view (token tok123tok123)",
        (body, conn, p.poll() is not None and p.stderr.read()[-300:]))
  subprocess.run([helper, "stop", "all"], capture_output=True, env=env)
  check("stop closes it", await until(lambda: p.poll() is not None, 15))
  await runner.cleanup()

  # ---- a shell: in a terminal (here a pseudo terminal), ssh asks for the password there
  slink = mgr.create(sh.Link("shell", "shell (linux helper)", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"],
                             relay="lab-relay", uses=None))
  await until(lambda: slink.ready(), 20)
  t = Term([helper, "_shell", winhelper.link_url(slink)], env)
  ok = await t.read_until(r"assword:", 30)
  check("a shell: the helper connects and checks the host key, then ssh asks for the password in the terminal",
        ok and b"host key checked" in t.out, t.out[-400:])
  t.send(slink.password + "\r")
  ok = await t.read_until(r"\$ ", 20)
  check("…the password: a shell on slurp's side", ok and slink.logins >= 1, t.out[-300:])
  t.send("echo $((6*7))\r")
  check("commands run", await t.read_until(r"42", 10), t.out[-200:])
  t.send("exit\r")
  code = await blocking(t.wait, 15)
  check("exit ends it, nothing left", code == 0 and not connections(env), (code, t.out[-200:]))

  # ---- refused: another slurp's link, a wrong host key, a broken link
  open(os.path.join(env["XDG_DATA_HOME"], "slurp-helper/trusted.txt"), "w").close()
  p = open_link(url, "x"); p.stdin.close()
  code, err = await blocking(p.wait, 30), p.stderr.read()
  check("a slurp it does not know (and nobody to ask): refused", code == 1 and "not trusted" in err, (code, err[-300:]))
  other = "SHA256:" + "A" * 43
  head, query = url.split("?", 1)
  q = dict(urllib.parse.parse_qsl(query)) | {"fp": other, "a": linux_alias(other)}
  forged = head + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote, safe="")
  p = open_link(forged, link.password, "--yes"); p.stdin.write(link.password + "\n"); p.stdin.close()
  code, err = await blocking(p.wait, 60), p.stderr.read()
  check("a host key that is not the link's: WRONG HOST KEY, no login", code == 1 and "WRONG HOST KEY" in err and forged != url, (code, err[-300:]))
  p = open_link("slurp://drive?v=1&id=zz", "x"); p.stdin.close()
  code, err = await blocking(p.wait, 30), p.stderr.read()
  check("a broken link: refused, it says which value", code == 2 and "no valid 'id'" in err, (code, err))

  r = subprocess.run([helper, "uninstall"], capture_output=True, text=True, env=env)
  check("uninstall removes it", r.returncode == 0 and not os.path.exists(helper)
        and not os.path.exists(os.path.join(env["XDG_DATA_HOME"], "applications/slurp-helper.desktop")))
  mgr.shutdown() # (here, while the loop runs: the relays' processes end on it)
  await until(lambda: all(lk.tunnel is None or lk.tunnel.returncode is not None for lk in mgr.links), 10)

def mounted(path):
  """(from /proc/self/mounts: a stat of the mount would wait for slurp, whose loop is the one that would wait)"""
  return any(line.split()[1] == path for line in open("/proc/self/mounts"))

def linux_alias(fp):
  import hashlib
  return "slurp-" + hashlib.sha256(fp.encode()).hexdigest()[:8]


def test_linux_helper(check, links, link_manager):
  if not shutil.which("sshfs") or not os.path.exists("/dev/fuse"):
    import pytest
    pytest.skip("sshfs and FUSE are needed")
  mgr, _ = link_manager
  asyncio.run(helper_links(check, mgr, links.S))
