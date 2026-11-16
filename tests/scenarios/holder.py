"""The session holder end to end, in pseudo terminals (isolated: own runtime, state and config directories)"""
import asyncio, json, os, re, select, shutil, signal, subprocess, sys, time
HERE = os.environ.get("SLURP_TEST_WORK") or sys.exit("SLURP_TEST_WORK: the lab directory (see tests/lab)"); T = os.path.join(HERE, "holdertest")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); PY = sys.executable
shutil.rmtree(T, ignore_errors=True); os.makedirs(T)
ENV = os.environ | {"XDG_RUNTIME_DIR": os.path.join(T, "run"), "XDG_STATE_HOME": os.path.join(T, "state"),
                    "XDG_CONFIG_HOME": os.path.join(T, "config"), "XDG_DATA_HOME": os.path.join(T, "data"), "XDG_CACHE_HOME": os.path.join(T, "cache"), "TERM": "xterm-256color", "COLORTERM": "truecolor",
                    "PATH": os.path.join(HERE, "sshlab", "fakebin") + ":" + os.environ["PATH"]}
for k in ("SLURP_INNER", "SLURP_HOLDER"): ENV.pop(k, None)
os.makedirs(ENV["XDG_RUNTIME_DIR"], mode=0o700)
sys.path.insert(0, REPO)
import pyte
results = []
def check(name, cond, info=""):
  results.append(bool(cond)); print(f"{'PASS' if cond else 'FAIL'}  {name}  {'' if cond else info}", flush=True)

class Term:
  """A terminal (pyte) with slurp in it"""
  def __init__(self, cols, rows, args=()):
    import fcntl, struct, termios
    self.master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    self.proc = subprocess.Popen([PY, os.path.join(REPO, "slurp"), *args], stdin=slave, stdout=slave, stderr=slave,
                                 env=ENV, start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    self.screen = pyte.Screen(cols, rows); self.stream = pyte.ByteStream(self.screen)
    self.raw = b""
  def pump(self, t=0.1):
    end = time.time() + t
    while time.time() < end:
      r, _, _ = select.select([self.master], [], [], 0.05)
      if r:
        try:
          data = os.read(self.master, 65536)
        except OSError:
          return
        self.raw += data
        self.stream.feed(data)
  def text(self):
    return "\n".join(self.screen.display)
  def wait(self, pattern, t=20):
    end = time.time() + t
    while time.time() < end:
      self.pump(0.2)
      if re.search(pattern, self.text()):
        return True
    return False
  def send(self, data):
    os.write(self.master, data.encode() if isinstance(data, str) else data)

os.makedirs(os.path.join(ENV["XDG_CONFIG_HOME"], "slurp"), exist_ok=True)
open(os.path.join(ENV["XDG_CONFIG_HOME"], "slurp", "config.json"), "w").write('{"theme": "nord", "theme_web": "gruvbox"}')
NORD, GRUVBOX = b"48;2;46;52;64", b"48;2;40;40;40" # (their backgrounds)
sock = subprocess.run([PY, "-c", "import slurp.holder as h; print(h.socket_path())"], cwd=REPO, env=ENV, capture_output=True, text=True).stdout.strip()
print("socket", sock)
a = Term(120, 35)
ok = a.wait(r"Jobs", 40)
check("slurp starts in the terminal (through the session holder)", ok and os.path.exists(sock), a.text()[-400:])
holder = subprocess.run(["pgrep", "-f", "--", f"--holder {sock}"], capture_output=True, text=True).stdout.split()
check("a holder process runs it, detached", len(holder) == 1, holder)
b = Term(100, 30)
ok = b.wait(r"in use in another view", 20)
check("a second terminal attaches and is locked: who is active, Take over, Watch, Detach",
      ok and "the terminal on" in b.text() and "Take over" in b.text() and "Watch: off" in b.text() and "120×35" in b.text(), b.text())
b.send("\r"); ok = b.wait(r"Take over from", 5)
check("Take over asks first", ok, b.text())
b.send("n"); ok = b.wait(r"Watch: off", 5) and not re.search("Take over from", b.text())
check("No: still locked", ok)
b.send("\r"); b.wait(r"Take over from", 5); b.send("y")
ok = b.wait(r"Jobs", 10) and a.wait(r"taken over by", 10)
check("Yes: the second view is active (slurp drawn there), the first one locked, saying by whom", ok, (b.text()[-300:], a.text()))
b.pump(1.5)
check("the app takes the new view's size", "Jobs" in b.text() and len(b.text().splitlines()) == 30)
a.send("w"); ok = a.wait(r"Watching .* \(read only\)", 5)
check("Watch shows the screen live, read only, with a bar", ok and "Jobs" in a.text(), a.text()[:300])
a.send("x"); a.pump(0.5)
check("keys of a watching view do not reach the app", "Watching" in a.text())
a.send("\r"); a.wait(r"Take over from", 5); a.send("y")
ok = a.wait(r"Jobs", 10) and b.wait(r"taken over by", 10) and "Watching" not in a.text()
check("taking it back: the first view active again, the second locked", ok, (a.text()[:200], b.text()))
b.send("d"); b.pump(1.0)
code = b.proc.wait(5)
check("Detach: the second terminal's slurp command ends, saying it goes on running", code == 0 and "detached" in b.text(), b.text()[-200:])
# the terminal goes away: slurp goes on, and attaches again
a.proc.send_signal(signal.SIGHUP); os.close(a.master); time.sleep(1.5)
check("closing the terminal leaves slurp running", os.path.exists(sock) and subprocess.run(["pgrep", "-f", "--", f"--holder {sock}"], capture_output=True).returncode == 0)
c = Term(110, 32)
ok = c.wait(r"Jobs", 15)
check("slurp again attaches to it, active at once (no other view)", ok and "in use" not in c.text(), c.text()[-300:])
# the web view, through the app's request
env = ENV | {"SLURP_HOLDER": sock}
r = subprocess.run([PY, "-c", "import json, slurp.holder as h; print(json.dumps(h.control('web_start')))"], cwd=REPO, env=env, capture_output=True, text=True)
web = json.loads(r.stdout or "{}")
check("the web view starts when asked (127.0.0.1, a random port, a token)", web.get("port") and web.get("token"), r.stderr[-300:])
async def browser():
  import aiohttp
  base = f"http://127.0.0.1:{web['port']}"
  async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
    async with s.get(base + "/") as resp:
      check("without the token: refused", resp.status == 403)
    async with s.get(base + f"/?token={web['token']}") as resp:
      page = await resp.text()
      check("with the token: textual-serve's page, pointing to this holder's websocket", resp.status == 200 and "textual-terminal" in page and "/ws" in page)
    async with s.get(base + "/static/js/textual.js") as resp:
      check("its script is served", resp.status == 200)
    async with s.ws_connect(base + "/ws?width=90&height=28") as ws:
      seen = b""
      async def collect(t, pattern):
        nonlocal seen
        end = time.time() + t
        while time.time() < end:
          try:
            m = await ws.receive(timeout=0.5)
          except asyncio.TimeoutError:
            continue
          if m.type == aiohttp.WSMsgType.BINARY:
            seen += m.data
            if re.search(pattern, seen):
              return True
        return False
      ok = await collect(10, rb"in use in another view")
      check("the browser attaches as a view, locked while the terminal is active", ok)
      await ws.send_str(json.dumps(["stdin", "\r"])); await collect(5, rb"Take over from")
      await ws.send_str(json.dumps(["stdin", "y"]))
      ok = await collect(10, rb"Jobs")
      check("the browser takes over: slurp drawn in it", ok and c.wait(r"taken over by Python|taken over by .*web", 10), c.text())
      ok = await collect(10, GRUVBOX)
      check("the browser being active, slurp switches to the browser's theme (gruvbox)", ok)
      c.raw = b"" 
      await ws.send_str(json.dumps(["ping", "x"]))
      pong = False
      end = time.time() + 5
      while time.time() < end and not pong:
        try:
          m = await ws.receive(timeout=0.5)
        except asyncio.TimeoutError:
          continue
        pong = m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data)[0] == "pong"
      check("ping / pong as textual-serve's page expects", pong)
    await asyncio.sleep(0.5)
    async with s.get(base + "/") as resp:
      check("a second visit without the token (the cookie of the first one kept): refused", resp.status == 403)
    try:
      async with s.ws_connect(base + "/ws?width=90&height=28") as ws2:
        reused = True
    except aiohttp.WSServerHandshakeError:
      reused = False
    check("the page's ticket opens one websocket only: its cookie, used again, is refused", not reused)
    async with s.get(base + "/download/x") as resp:
      check("a download with a used ticket: refused", resp.status == 403)
  async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), cookies={"slurp_token": web["token"]}) as s2:
    async with s2.get(base + "/") as resp:
      check("the token as a cookie (as an older slurp kept it) opens no page", resp.status == 403)
    try:
      async with s2.ws_connect(base + "/ws") as ws3:
        took = True
    except aiohttp.WSServerHandshakeError:
      took = False
    check("nor a websocket", not took)
  async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
    ok = c.wait(r"Jobs", 10) and "in use" not in c.text()
    check("the browser goes away: the terminal becomes active by itself", ok, c.text()[:300])
    end = time.time() + 10
    while time.time() < end and NORD not in c.raw[-400000:]:
      c.pump(0.2)
    check("…and slurp switches back to the terminal's theme (nord)", NORD in c.raw[-400000:])
asyncio.run(browser())
r = subprocess.run([PY, "-c", "import json, slurp.holder as h; print(json.dumps(h.control('status')))"], cwd=REPO, env=env, capture_output=True, text=True)
st = json.loads(r.stdout or "{}")
check("status: the views and the web port", len(st.get("views", [])) == 1 and st.get("web"), st)
r = subprocess.run([PY, "-c", f"import json, slurp, slurp.holder as h; print(json.dumps([slurp.__version__, h.control('status', path={sock!r})]))"],
                   cwd=REPO, env=ENV, capture_output=True, text=True)
version, st2 = json.loads(r.stdout or '["", {}]')
check("status: the holder's version, asked by a new slurp by its socket (as before attaching)", version and (st2 or {}).get("version") == version, (version, st2, r.stderr[-200:]))
# quitting ends everything
c.send("q"); c.pump(2)
code = c.proc.wait(10)
time.sleep(1)
check("q quits slurp: the view's command ends, the holder too, the socket is gone", code == 0 and not os.path.exists(sock)
      and subprocess.run(["pgrep", "-f", "--", f"--holder {sock}"], capture_output=True).returncode != 0, (code, c.text()[-200:]))
d = Term(100, 30, ["--no-holder"])
ok = d.wait(r"Jobs", 30)
check("--no-holder runs it directly (no socket, no holder)", ok and not os.path.exists(sock))
d.send("q"); end = time.time() + 10
while d.proc.poll() is None and time.time() < end: # (reading on: a full pty would block its last output)
  d.pump(0.2)
check("--no-holder: q quits it", d.proc.poll() == 0, (d.proc.poll(), d.text()[-200:]))
print(f"\n{sum(results)}/{len(results)} passed")
for pid in subprocess.run(["pgrep", "-f", "--", f"--holder {sock}"], capture_output=True, text=True).stdout.split():
  os.kill(int(pid), signal.SIGTERM)
