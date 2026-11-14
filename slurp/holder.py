"""The session holder: one running slurp, several views onto it.

`slurp` does not run the app in the terminal it is started from. It starts (or finds) a holder process, which runs the
app in a pseudo terminal, and attaches to it as a view. Other views can attach: another terminal (`slurp` again), or a
browser (Remote web access: the holder serves textual-serve's page, a terminal, on 127.0.0.1, reached through a web
link). Like tmux, the app keeps running when a terminal goes away, and `slurp` attaches to it again.

Exactly one view is active: its keys and mouse go to the app, the screen has its size. The others are locked: the
holder draws a lock screen over them (who is active, since when, idle for how long, its size, whether it is still
connected) with Take over (after an "are you sure?"), an optional live read-only view of the screen (Watch), and Detach.
The holder keeps a copy of the screen (pyte) and the terminal modes the app switched on, so a view that becomes active
gets exactly what is on the screen; if its size differs, the app draws itself again at the new size.

Views talk to the holder over a Unix socket (terminal views, and the app's control requests) or a websocket (browsers),
in frames: one type byte (D data, M meta: JSON), four bytes of length, the payload. Browsers use textual-serve's
protocol: binary messages of terminal output to them, ["stdin", text], ["resize", {width, height}], ["ping", x] from them.

The holder does not import the app (it is long-lived and small); it needs pyte, and aiohttp and jinja2 (installed with
textual-serve) for the web view."""

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import termios
import time

log = logging.getLogger("slurp.holder")

PACKAGE = os.path.dirname(os.path.abspath(__file__))
CTTY = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
RESET_TERMINAL = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?1004l\x1b[?2004l\x1b[<u\x1b[0m"
                  "\x1b[?25h\x1b[?1049l")
ALT_MODES = (47, 1047, 1049)

# ---------------------------------------------------------------------------
# frames

def frame(kind, payload):
  if isinstance(payload, (dict, list)):
    payload = json.dumps(payload).encode()
  elif isinstance(payload, str):
    payload = payload.encode()
  return kind + struct.pack(">I", len(payload)) + payload

async def read_frame(reader):
  """(type, payload) or None at the end"""
  try:
    head = await reader.readexactly(5)
    payload = await reader.readexactly(struct.unpack(">I", head[1:])[0])
  except (asyncio.IncompleteReadError, ConnectionError):
    return None
  return head[:1], payload

# ---------------------------------------------------------------------------
# where the holder listens

SOCKET_PATH_MAX = 100 # (a Unix socket path may have about 108 bytes)

def runtime_dir():
  """$XDG_RUNTIME_DIR/slurp; a short directory in /tmp, unique for the runtime directory, where that is too long"""
  import hashlib
  base = os.environ.get("XDG_RUNTIME_DIR")
  path = os.path.join(base, "slurp") if base and os.path.isdir(base) else f"/tmp/slurp-{os.getuid()}"
  if len(path) + len("/s4194304.sock") > SOCKET_PATH_MAX:
    path = f"/tmp/slurp-{os.getuid()}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"
  os.makedirs(path, mode=0o700, exist_ok=True)
  os.chmod(path, 0o700)
  return path

def socket_path(name="default"):
  return os.path.join(runtime_dir(), f"{name}.sock")

def alive(path):
  """A holder answers at path (a socket left by a holder that died is removed)"""
  s = socket.socket(socket.AF_UNIX)
  s.settimeout(2)
  try:
    s.connect(path)
    return True
  except FileNotFoundError:
    return False
  except ConnectionRefusedError: # (nothing listens: a holder that died left it)
    try:
      os.unlink(path)
    except OSError:
      pass
    return False
  except OSError:
    return False
  finally:
    s.close()

# ---------------------------------------------------------------------------
# the copy of the screen

def make_shadow(cols, rows):
  """A pyte screen of what the app shows (it answers nothing: the active view's terminal answers), with the alternate
  screen and the private modes the app set (for a view that becomes active)"""
  import pyte

  class Shadow(pyte.Screen):
    def __init__(self, columns, lines):
      super().__init__(columns, lines)
      self.private = {}   # mode -> on
      self.saved = None   # the normal screen while the alternate one is shown
      self.kitty = []     # the stack of kitty keyboard flags

    def write_process_input(self, data):
      pass

    def set_mode(self, *modes, **kwargs):
      if kwargs.get("private"):
        for m in modes:
          self.private[m] = True
        if any(m in ALT_MODES for m in modes) and self.saved is None:
          self.saved = ({y: dict(line) for y, line in self.buffer.items()}, (self.cursor.x, self.cursor.y))
          self.erase_in_display(2)
        modes = tuple(m for m in modes if m not in ALT_MODES)
        if not modes:
          return
      super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
      if kwargs.get("private"):
        for m in modes:
          self.private[m] = False
        if any(m in ALT_MODES for m in modes) and self.saved is not None:
          buffer, (x, y) = self.saved
          self.saved = None
          self.buffer.clear()
          for row, line in buffer.items():
            if row < self.lines:
              self.buffer[row].update(line)
          self.cursor.x, self.cursor.y = min(x, self.columns - 1), min(y, self.lines - 1)
        modes = tuple(m for m in modes if m not in ALT_MODES)
        if not modes:
          return
      super().reset_mode(*modes, **kwargs)

  screen = Shadow(cols, rows)
  return screen, pyte.ByteStream(screen)

COLORS = {"black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7}

def sgr_color(name, base):
  if not name or name == "default":
    return None
  if re.fullmatch(r"[0-9a-fA-F]{6}", name):
    return f"{base + 8};2;{int(name[0:2], 16)};{int(name[2:4], 16)};{int(name[4:6], 16)}"
  bright = name.startswith("bright")
  index = COLORS.get(name[6:] if bright else name)
  if index is None:
    return None
  return str(base + 60 + index if bright else base + index)

def sgr(ch):
  codes = ["0"]
  if ch.bold:
    codes.append("1")
  if ch.italics:
    codes.append("3")
  if ch.underscore:
    codes.append("4")
  if ch.reverse:
    codes.append("7")
  if ch.strikethrough:
    codes.append("9")
  for value, base in ((ch.fg, 30), (ch.bg, 40)):
    c = sgr_color(value, base)
    if c:
      codes.append(c)
  return ";".join(codes)

def render(screen, cols, rows, top=0):
  """The screen as terminal output, clipped to cols × rows, drawn from row top"""
  out = []
  for y in range(min(rows - top, screen.lines)):
    line = screen.buffer[y]
    out.append(f"\x1b[{y + 1 + top};1H\x1b[0m\x1b[2K")
    last = None
    for x in range(min(cols, screen.columns)):
      ch = line[x]
      if ch.data == "":
        continue
      s = sgr(ch)
      if s != last:
        out.append(f"\x1b[{s}m")
        last = s
      out.append(ch.data)
  out.append("\x1b[0m")
  return "".join(out)

def replay(screen, cols, rows):
  """What a view that becomes active needs: the modes the app set, then the screen, then the cursor"""
  out = ["\x1b[0m"]
  if screen.saved is not None:
    out.append("\x1b[?1049h")
  out.append("\x1b[2J")
  for m, on in screen.private.items():
    if m not in ALT_MODES:
      out.append(f"\x1b[?{m}{'h' if on else 'l'}")
  if screen.kitty:
    out.append(f"\x1b[>{screen.kitty[-1]}u")
  out.append(render(screen, cols, rows))
  out.append(f"\x1b[{min(screen.cursor.y, rows - 1) + 1};{min(screen.cursor.x, cols - 1) + 1}H")
  return "".join(out)

KITTY_PUSH = re.compile(rb"\x1b\[>(\d+)u")
KITTY_POP = re.compile(rb"\x1b\[<(\d*)u")

# ---------------------------------------------------------------------------
# views

def clock(t):
  return time.strftime("%H:%M", time.localtime(t))

def ago(seconds):
  seconds = int(max(seconds, 0))
  if seconds < 60:
    return f"{seconds} s"
  if seconds < 3600:
    return f"{seconds // 60} min"
  return f"{seconds // 3600} h {seconds % 3600 // 60} min"

class View:
  """An attached terminal or browser"""
  def __init__(self, holder, kind, label, send, close, width, height):
    self.holder, self.kind, self.label = holder, kind, label
    self.send, self.close = send, close
    self.width, self.height = max(width, 20), max(height, 5)
    self.since = self.last_seen = time.time()
    self.last_input = None
    self.active = False
    self.confirm = False
    self.watch = False
    self.note = ""
    self.buttons = [] # (row, first col, last col, action)

  def size_text(self):
    return f"{self.width}×{self.height}"

class Holder:
  def __init__(self, path, args, cols=100, rows=30):
    self.path, self.args = path, args
    self.cols, self.rows = cols, rows
    self.views = []
    self.active = None
    self.screen, self.stream = make_shadow(cols, rows)
    self.master = None
    self.proc = None
    self.web = None       # (runner, port)
    self.web_token = ""
    self.info = ""        # what the app says about its web links (for the lock screens)
    self.dirty = False
    self.done = asyncio.Event()
    self.code = None
    self.started = time.time()

  # ---- the app in its pseudo terminal
  def start_app(self):
    master, slave = os.openpty()
    self.master = master
    self.set_size(self.cols, self.rows)
    env = os.environ | {"SLURP_INNER": "1", "SLURP_HOLDER": self.path}
    env.pop("SLURP_HOLDER_SIZE", None)
    self.proc = subprocess.Popen([sys.executable, "-c", CTTY, sys.executable, PACKAGE, *self.args], stdin=slave,
                                 stdout=slave, stderr=slave, start_new_session=True, env=env, close_fds=True)
    os.close(slave)
    os.set_blocking(master, False)
    asyncio.get_running_loop().add_reader(master, self.from_app)

  def set_size(self, cols, rows):
    import fcntl
    self.cols, self.rows = cols, rows
    fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    self.screen.resize(rows, cols)

  def from_app(self):
    try:
      data = os.read(self.master, 65536)
    except BlockingIOError:
      return
    except OSError:
      data = b""
    if not data:
      asyncio.get_running_loop().remove_reader(self.master)
      return
    self.stream.feed(data)
    for m in KITTY_PUSH.finditer(data):
      self.screen.kitty.append(m.group(1).decode())
    for m in KITTY_POP.finditer(data):
      n = int(m.group(1) or 1)
      del self.screen.kitty[-n:]
    if self.active is not None:
      self.active.send(data)
    self.dirty = True

  def to_app(self, data):
    try:
      os.write(self.master, data)
    except OSError:
      pass

  async def wait_app(self):
    self.code = await asyncio.get_running_loop().run_in_executor(None, self.proc.wait)
    log.info(f"the app ended ({self.code})")
    for view in list(self.views):
      try:
        view.send(b"\x1b[0m")
        view.close(self.code)
      except Exception:
        pass
    await self.web_stop()
    self.done.set()

  # ---- views
  def attach(self, view):
    self.views.append(view)
    log.info(f"attached: {view.label} ({view.size_text()})")
    if self.active is None:
      self.activate(view)
    else:
      self.draw_lock(view)
    self.refresh_locks()

  def detach(self, view):
    if view not in self.views:
      return
    self.views.remove(view)
    log.info(f"detached: {view.label}")
    if view is self.active:
      self.active = None
      if self.views: # the most recently attached remaining view takes over by itself
        self.activate(max(self.views, key=lambda v: v.since), note=f"{view.label} went away at {clock(time.time())}")
    self.refresh_locks()

  def activate(self, view, note=""):
    old = self.active
    if old is not None and old is not view:
      old.active = False
      old.note = f"taken over by {view.label} at {clock(time.time())}"
      old.confirm = old.watch = False
      self.draw_lock(old)
    view.active, view.confirm, view.watch = True, False, False
    view.note = note
    view.active_since = time.time()
    self.active = view
    view.send(replay(self.screen, view.width, view.height).encode())
    if (view.width, view.height) != (self.cols, self.rows):
      self.set_size(view.width, view.height) # (the app draws itself again at this size)
    log.info(f"active: {view.label}")
    self.refresh_locks()

  def resize(self, view, width, height):
    view.width, view.height = max(width, 20), max(height, 5)
    if view is self.active:
      self.set_size(view.width, view.height)
    else:
      self.draw_lock(view)

  def input(self, view, data):
    view.last_seen = time.time()
    if view is self.active:
      view.last_input = time.time()
      self.to_app(data)
      return
    self.lock_input(view, data)

  # ---- the lock screen
  def refresh_locks(self):
    for view in self.views:
      if view is not self.active:
        self.draw_lock(view)

  def draw_lock(self, view):
    now = time.time()
    w, h = view.width, view.height
    view.buttons = []
    out = ["\x1b[?1049h\x1b[?1003l\x1b[?1002l\x1b[?1000h\x1b[?1006h\x1b[?25l\x1b[0m\x1b[2J"]
    if view.watch and not view.confirm:
      out.append(render(self.screen, w, h, top=1))
      bar = " Watching (read only) · Enter: take over · w: stop watching · d: detach "
      if self.active is not None:
        bar = f" Watching {self.active.label} (read only) · Enter: take over · w: stop · d: detach "
      out.append(f"\x1b[1;1H\x1b[0;7;1m{bar[:w].ljust(w)}\x1b[0m")
      view.buttons.append((1, 1, w, "take"))
      view.send("".join(out).encode())
      return
    a = self.active
    lines = [("slurp is in use in another view", "1")]
    if a is None:
      lines.append(("no view is active now", ""))
    else:
      idle = f"idle {ago(now - a.last_input)}" if a.last_input else "no input yet"
      seen = now - a.last_seen
      lines += [(f"by {a.label}", ""),
                (f"since {clock(getattr(a, 'active_since', a.since))} · {idle} · {a.size_text()} · "
                 + ("connected" if seen < 20 else f"no sign of it for {ago(seen)}"), "2")]
    lines.append(("", ""))
    lines.append((f"this view: {view.label}, {view.size_text()}", "2"))
    if view.note:
      lines.append((view.note, "33"))
    if self.info:
      lines.append(("", ""))
      lines += [(line, "2") for line in self.info.splitlines()[:4]]
    lines.append(("", ""))
    if view.confirm:
      lines.append((f"Take over from {a.label if a else 'the other view'}? It is locked then (and can take over back).", "1"))
      buttons = [("[ Yes, take over ]", "yes", " Enter/y"), ("[ No ]", "no", " Esc/n")]
    else:
      buttons = [("[ Take over ]", "take", " Enter"), (f"[ Watch: {'on' if view.watch else 'off'} ]", "watch", " w"),
                 ("[ Detach ]", "detach", " d")]
    top = max((h - len(lines) - 2) // 2, 1)
    for i, (text, style) in enumerate(lines):
      text = text[:w - 2]
      out.append(f"\x1b[{top + i};{max((w - len(text)) // 2, 1) + 1}H\x1b[0{';' + style if style else ''}m{text}\x1b[0m")
    row = top + len(lines) + 1
    total = sum(len(b) + len(k) + 4 for b, _, k in buttons)
    col = max((w - total) // 2, 1) + 1
    for label, action, keys in buttons:
      out.append(f"\x1b[{row};{col}H\x1b[0;7;1m{label}\x1b[0;2m{keys}\x1b[0m")
      view.buttons.append((row, col, col + len(label) - 1, action))
      col += len(label) + len(keys) + 4
    view.send("".join(out).encode())

  def lock_input(self, view, data):
    actions = []
    for m in re.finditer(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", data): # SGR mouse: a press of the left button
      b, x, y, kind = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
      if kind == b"M" and b == 0:
        for row, first, last, action in view.buttons:
          if y == row and first <= x <= last:
            actions.append(action)
    keys = re.sub(rb"\x1b\[<[\d;]+[Mm]", b"", data)
    if keys in (b"\r", b"\n", b"\x1b[13u", b"t", b"y"):
      actions.append("yes" if view.confirm else "take")
    elif keys in (b"\x1b", b"\x1b[27u", b"n"):
      actions.append("no")
    elif keys == b"w":
      actions.append("watch")
    elif keys == b"d":
      actions.append("detach")
    for action in actions[:1]:
      if action == "take":
        view.confirm = True
      elif action == "yes" and view.confirm:
        self.activate(view)
        return
      elif action == "no":
        view.confirm = False
      elif action == "watch":
        view.watch = not view.watch
      elif action == "detach":
        view.send(RESET_TERMINAL.encode())
        view.close(None)
        self.detach(view)
        return
      self.draw_lock(view)

  async def ticker(self):
    """Once a second: the lock screens (idle times), the watching views (when the screen changed)"""
    while not self.done.is_set():
      await asyncio.sleep(1)
      dirty, self.dirty = self.dirty, False
      for view in list(self.views):
        if view is not self.active and (not view.watch or dirty or view.confirm):
          self.draw_lock(view)

  # ---- the Unix socket: terminal views and the app's requests
  async def serve_socket(self, reader, writer):
    first = await read_frame(reader)
    if first is None or first[0] != b"M":
      writer.close()
      return
    hello = json.loads(first[1])
    if hello.get("type") == "control":
      reply = await self.control(hello)
      writer.write(frame(b"M", reply))
      await writer.drain()
      writer.close()
      return
    def send(data):
      if not writer.is_closing():
        writer.write(frame(b"D", data))
    def close(code):
      if not writer.is_closing():
        writer.write(frame(b"M", {"type": "exit", "code": code}))
        writer.close()
    host = hello.get("host") or "?"
    tty = hello.get("tty") or ""
    view = View(self, "terminal", f"the terminal on {host}" + (f" ({tty})" if tty else ""), send, close,
                hello.get("width", 80), hello.get("height", 24))
    self.attach(view)
    try:
      while True:
        f = await read_frame(reader)
        if f is None:
          break
        kind, payload = f
        if kind == b"D":
          self.input(view, payload)
        else:
          meta = json.loads(payload)
          if meta.get("type") == "resize":
            self.resize(view, meta.get("width", view.width), meta.get("height", view.height))
          elif meta.get("type") == "ping":
            view.last_seen = time.time()
    finally:
      self.detach(view)
      if not writer.is_closing():
        writer.close()

  async def control(self, request):
    cmd = request.get("cmd")
    if cmd == "web_start":
      port = await self.web_start()
      return {"port": port, "token": self.web_token}
    if cmd == "web_stop":
      await self.web_stop()
      return {"ok": True}
    if cmd == "info":
      self.info = request.get("text", "")
      self.refresh_locks()
      return {"ok": True}
    if cmd == "status":
      now = time.time()
      return {"views": [{"label": v.label, "kind": v.kind, "active": v is self.active, "since": v.since,
                         "idle": now - v.last_input if v.last_input else None} for v in self.views],
              "web": self.web[1] if self.web else None, "since": self.started}
    return {"error": f"unknown request {cmd!r}"}

  # ---- the web view (textual-serve's page, this terminal)
  async def web_start(self):
    if self.web is not None:
      return self.web[1]
    from aiohttp import web, WSMsgType
    import jinja2
    import textual_serve
    serve_dir = os.path.dirname(textual_serve.__file__)
    templates = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(serve_dir, "templates")), autoescape=True)
    self.web_token = secrets.token_urlsafe(18)

    async def index(request):
      if not secrets.compare_digest(request.query.get("token", ""), self.web_token) and \
         not secrets.compare_digest(request.cookies.get("slurp_token", ""), self.web_token):
        return web.Response(status=403, text="slurp: this needs the link's URL (with its token)")
      host = request.host
      page = templates.get_template("app_index.html").render(
        font_size=int(request.query.get("fontsize", "16") or 16), app_websocket_url=f"ws://{host}/ws",
        config={"static": {"url": f"http://{host}/static/"}}, application={"name": "slurp"}, ping_url="")
      response = web.Response(text=page, content_type="text/html")
      response.set_cookie("slurp_token", self.web_token, httponly=True, samesite="Strict")
      return response

    async def websocket(request):
      if not secrets.compare_digest(request.cookies.get("slurp_token", ""), self.web_token):
        return web.Response(status=403)
      ws = web.WebSocketResponse(heartbeat=15)
      await ws.prepare(request)
      queue = asyncio.Queue()
      def send(data):
        queue.put_nowait(data)
      def close(code):
        queue.put_nowait(None)
      async def writer():
        while True:
          data = await queue.get()
          if data is None:
            await ws.close()
            return
          try:
            await ws.send_bytes(data)
          except Exception:
            return
      task = asyncio.create_task(writer())
      agent = request.headers.get("User-Agent", "")
      browser = next((b for b in ("Firefox", "Edg", "Chrome", "Safari") if b in agent), "a browser").replace("Edg", "Edge")
      view = View(self, "web", f"{browser} (web)", send, close,
                  int(request.query.get("width", "100") or 100), int(request.query.get("height", "30") or 30))
      self.attach(view)
      try:
        async for message in ws:
          if message.type != WSMsgType.TEXT:
            continue
          try:
            envelope = json.loads(message.data)
          except ValueError:
            continue
          kind = envelope[0] if envelope else None
          if kind == "stdin":
            self.input(view, envelope[1].encode())
          elif kind == "resize":
            self.resize(view, int(envelope[1].get("width", 80)), int(envelope[1].get("height", 24)))
          elif kind == "ping":
            view.last_seen = time.time()
            await ws.send_str(json.dumps(["pong", envelope[1]]))
      finally:
        self.detach(view)
        task.cancel()
      return ws

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/ws", websocket),
                    web.static("/static", os.path.join(serve_dir, "static"))])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    self.web = (runner, port)
    log.info(f"web view on 127.0.0.1:{port}")
    return port

  async def web_stop(self):
    if self.web is None:
      return
    runner, port = self.web
    self.web = None
    for view in [v for v in self.views if v.kind == "web"]:
      view.close(None)
      self.detach(view)
    await runner.cleanup()
    log.info(f"web view on port {port} stopped")

  # ---- the holder's life
  async def run(self):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGHUP, signal.SIGINT):
      loop.add_signal_handler(sig, lambda: None) # (it outlives the terminal it was started from)
    loop.add_signal_handler(signal.SIGTERM, lambda: self.proc and self.proc.terminate())
    server = await asyncio.start_unix_server(self.serve_socket, path=self.path)
    os.chmod(self.path, 0o600)
    self.start_app()
    ticker = asyncio.create_task(self.ticker())
    await self.wait_app()
    ticker.cancel()
    server.close()
    try:
      os.unlink(self.path)
    except OSError:
      pass
    return self.code or 0

def run_holder(path, args):
  """The holder process (started by start_holder, detached from the terminal)"""
  logdir = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "slurp")
  os.makedirs(logdir, exist_ok=True)
  logging.basicConfig(filename=os.path.join(logdir, "holder.log"), level=logging.INFO,
                      format="%(asctime)s %(levelname)s %(message)s")
  size = os.environ.get("SLURP_HOLDER_SIZE", "100x30")
  try:
    cols, rows = (int(v) for v in size.split("x"))
  except ValueError:
    cols, rows = 100, 30
  holder = Holder(path, args, cols, rows)
  log.info(f"holder started: {path}, slurp {' '.join(args)}")
  try:
    return asyncio.run(holder.run())
  except Exception:
    log.exception("the holder failed")
    raise

# ---------------------------------------------------------------------------
# starting, attaching, and the app's requests

def start_holder(path, args):
  """Start a holder (detached: it outlives this terminal) and wait until it listens. -> True if it does."""
  try:
    cols, rows = os.get_terminal_size()
  except OSError:
    cols, rows = 100, 30
  logdir = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "slurp")
  os.makedirs(logdir, exist_ok=True)
  with open(os.path.join(logdir, "holder.err"), "ab") as err:
    subprocess.Popen([sys.executable, PACKAGE, "--holder", path, *args], stdin=subprocess.DEVNULL, stdout=err,
                     stderr=err, start_new_session=True, env=os.environ | {"SLURP_HOLDER_SIZE": f"{cols}x{rows}"})
  deadline = time.time() + 10
  while time.time() < deadline:
    if os.path.exists(path) and alive(path):
      return True
    time.sleep(0.05)
  return False

def attach(path):
  """This terminal as a view of the holder at path, until the app ends or the view detaches -> exit code"""
  import tty
  code = [0]
  message = [""]

  async def main():
    reader, writer = await asyncio.open_unix_connection(path)
    cols, rows = os.get_terminal_size()
    try:
      tty_name = os.ttyname(0)
    except OSError:
      tty_name = ""
    writer.write(frame(b"M", {"type": "hello", "kind": "terminal", "width": cols, "height": rows,
                              "host": socket.gethostname(), "tty": tty_name}))
    loop = asyncio.get_running_loop()
    ended = asyncio.Event()

    def stdin_ready():
      try:
        data = os.read(0, 65536)
      except OSError:
        data = b""
      if not data:
        ended.set()
        return
      writer.write(frame(b"D", data))

    def resized():
      try:
        c, r = os.get_terminal_size()
      except OSError:
        return
      writer.write(frame(b"M", {"type": "resize", "width": c, "height": r}))

    async def ping():
      while True:
        await asyncio.sleep(5)
        writer.write(frame(b"M", {"type": "ping"}))

    async def output():
      while True:
        f = await read_frame(reader)
        if f is None:
          message[0] = "slurp: the session holder went away"
          break
        kind, payload = f
        if kind == b"D":
          view = memoryview(payload)
          while view:
            try:
              n = os.write(1, view)
            except BlockingIOError:
              await asyncio.sleep(0.01)
              continue
            view = view[n:]
        else:
          meta = json.loads(payload)
          if meta.get("type") == "exit":
            if meta.get("code") is None:
              message[0] = "slurp: detached (it goes on running; slurp attaches to it again)"
            else:
              code[0] = meta.get("code") or 0
            break
      ended.set()

    loop.add_reader(0, stdin_ready)
    loop.add_signal_handler(signal.SIGWINCH, resized)
    tasks = [asyncio.create_task(output()), asyncio.create_task(ping())]
    await ended.wait()
    loop.remove_reader(0)
    for t in tasks:
      t.cancel()
    writer.close()

  old = termios.tcgetattr(0)
  try:
    tty.setraw(0)
    asyncio.run(main())
  finally:
    termios.tcsetattr(0, termios.TCSADRAIN, old)
    os.write(1, RESET_TERMINAL.encode())
    if message[0]:
      print(message[0], file=sys.stderr)
  return code[0]

def control(cmd, timeout=5, **kw):
  """A request of the app to its holder -> the reply (dict); None if slurp does not run under a holder"""
  path = os.environ.get("SLURP_HOLDER")
  if not path:
    return None
  s = socket.socket(socket.AF_UNIX)
  s.settimeout(timeout)
  try:
    s.connect(path)
    s.sendall(frame(b"M", {"type": "control", "cmd": cmd, **kw}))
    data = b""
    while len(data) < 5 or len(data) < 5 + struct.unpack(">I", data[1:5])[0]:
      chunk = s.recv(65536)
      if not chunk:
        break
      data += chunk
    return json.loads(data[5:5 + struct.unpack(">I", data[1:5])[0]]) if len(data) >= 5 else None
  except (OSError, ValueError):
    return None
  finally:
    s.close()

def under_holder():
  return bool(os.environ.get("SLURP_HOLDER"))
