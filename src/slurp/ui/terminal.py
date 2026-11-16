"""The terminal: a command in a pseudo terminal, emulated with pyte, and the widgets that show it"""

import asyncio
import collections
import fcntl
import functools
import os
import re
import shlex
import struct
import subprocess
import sys
import termios

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Static


from ..state import log

# ---------------------------------------------------------------------------
# terminal window: a command (srun --pty bash) in a pseudo terminal, emulated with pyte

# make the pseudo terminal the controlling terminal of the command (for Ctrl-C, job control), then run it
CTTY = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
ALT_SCREEN_MODES = (47, 1047, 1049)
DECCKM = 1 << 5 # private mode 1 (application cursor keys), pyte stores private modes shifted by 5 bits

TERM_KEYS = {
  "enter": "\r", "tab": "\t", "shift+tab": "\x1b[Z", "backspace": "\x7f", "ctrl+h": "\x08", "escape": "\x1b",
  "insert": "\x1b[2~", "delete": "\x1b[3~", "pageup": "\x1b[5~", "pagedown": "\x1b[6~", "home": "\x1b[H", "end": "\x1b[F",
  "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS", "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~",
  "f8": "\x1b[19~", "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}
TERM_ARROWS = {"up": "A", "down": "B", "right": "C", "left": "D"}

def vt_color(name):
  """pyte color (name or rrggbb) -> rich color"""
  if not name or name == "default":
    return None
  if re.fullmatch(r'[0-9a-fA-F]{6}', name):
    return Color.parse("#" + name)
  bright = name.startswith("bright")
  base = {"brown": "yellow"}.get(name[6:] if bright else name, name[6:] if bright else name)
  try:
    return Color.parse(("bright_" if bright else "") + base)
  except Exception:
    return None

@functools.lru_cache(maxsize=4096)
def vt_style(fg, bg, bold, italics, underscore, strikethrough, reverse):
  return Style(color=vt_color(fg), bgcolor=vt_color(bg), bold=bold, italic=italics, underline=underscore,
               strike=strikethrough, reverse=reverse)

SCROLLBACK = 10000 # lines kept above the screen ("terminal_scrollback" in the config)

def scrollback_lines():
  from ..config import load_config
  try:
    return max(int(load_config().get("terminal_scrollback", SCROLLBACK)), 0)
  except (TypeError, ValueError):
    return SCROLLBACK

def make_vt_screen(cols, rows, reply):
  """pyte screen that answers terminal queries through reply, supports the alternate screen of vim, less, htop, and
  keeps the lines that scroll off its top (the history; not those of the alternate screen)"""
  import collections
  import pyte

  class VTScreen(pyte.Screen):
    saved = None
    history = None
    added = 0 # lines ever added to the history (the history keeps the last ones)

    def index(self):
      top, bottom = self.margins or (0, self.lines - 1)
      if self.cursor.y == bottom and top == 0 and self.saved is None and self.history is not None and self.history.maxlen:
        line = self.buffer[0]
        self.history.append([line[x] for x in range(self.columns)])
        self.added += 1
      super().index()

    def erase_in_display(self, how=0, *args, **kwargs):
      if how == 3 and self.history is not None: # clear: also the scrollback
        self.history.clear()
      super().erase_in_display(how, *args, **kwargs)

    def write_process_input(self, data):
      reply(data.encode())

    def set_mode(self, *modes, **kwargs):
      if kwargs.get("private") and any(m in ALT_SCREEN_MODES for m in modes):
        self.saved = ({y: dict(line) for y, line in self.buffer.items()}, (self.cursor.x, self.cursor.y))
        self.erase_in_display(2)
        modes = tuple(m for m in modes if m not in ALT_SCREEN_MODES)
        if not modes:
          return
      super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
      if kwargs.get("private") and any(m in ALT_SCREEN_MODES for m in modes):
        if self.saved:
          buffer, (x, y) = self.saved
          self.saved = None
          self.buffer.clear()
          for row, line in buffer.items():
            if row < self.lines:
              self.buffer[row].update(line)
          self.cursor.x, self.cursor.y = min(x, self.columns - 1), min(y, self.lines - 1)
          self.dirty.update(range(self.lines))
        modes = tuple(m for m in modes if m not in ALT_SCREEN_MODES)
        if not modes:
          return
      super().reset_mode(*modes, **kwargs)

  screen = VTScreen(cols, rows)
  screen.history = collections.deque(maxlen=scrollback_lines())
  return screen, pyte

SWITCH_KEY = "ctrl+right_square_bracket" # Ctrl-]: the one key that does not go to the shell

class TerminalSession:
  """A command (srun --pty bash) in a pseudo terminal, emulated with pyte. It belongs to the app and is shown in
  the terminal pane of every window, so it keeps running while you navigate on the left."""
  def __init__(self, app, args, title, via=None, login=None):
    self.app, self.args, self.title = app, args, title
    self.via = via     # RemoteSSH: args run on its cluster (no args: the login shell there)
    self.login = login # RemoteSSH: args are the ssh login (control master) of this connection
    self.vt = self.stream = self.proc = self.fd = None
    self.size = None
    self.done, self.code, self.error = False, None, ""

  def ensure(self, cols, rows):
    """Start with, or resize to, the size of the pane"""
    cols, rows = max(cols, 2), max(rows, 2)
    if self.vt is None and not self.done:
      self.start(cols, rows)
    elif self.vt is not None and (cols, rows) != self.size:
      self.size = (cols, rows)
      self.vt.resize(rows, cols)
      self.set_winsize(cols, rows)

  def start(self, cols, rows):
    self.size = (cols, rows)
    try:
      self.vt, pyte = make_vt_screen(cols, rows, self.write)
    except ImportError:
      self.done, self.code, self.error = True, -1, "the shell needs the python package pyte: pip install pyte"
      self.app.shell_changed()
      return
    self.stream = pyte.ByteStream(self.vt)
    master, slave = os.openpty()
    self.fd = master
    self.set_winsize(cols, rows)
    log.debug(f"running command: {shlex.join(self.args)}")
    try:
      if self.login:
        self.proc = self.login.spawn(slave)
      else:
        command = self.via.command_argv(self.args, interactive=True) if self.via else self.args
        self.proc = subprocess.Popen([sys.executable, "-c", CTTY, *command], stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, env=os.environ | {"TERM": "xterm-256color"}, close_fds=True)
    except OSError as e:
      os.close(slave)
      self.stream.feed(f"cannot start {self.args[0]}: {e}\r\n".encode())
      self.finish()
      return
    os.close(slave)
    os.set_blocking(master, False)
    asyncio.get_running_loop().add_reader(master, self.read_output)
    self.app.shell_changed()

  def set_winsize(self, cols, rows):
    if self.fd is not None:
      try:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
      except OSError:
        pass

  def read_output(self):
    try:
      data = os.read(self.fd, 65536)
    except BlockingIOError:
      return
    except OSError: # EIO: the command closed the terminal
      data = b""
    if not data:
      self.finish()
      return
    self.stream.feed(data)
    self.app.refresh_terminal()

  def close_fd(self):
    if self.fd is not None:
      try:
        asyncio.get_running_loop().remove_reader(self.fd)
      except Exception:
        pass
      os.close(self.fd)
      self.fd = None

  def finish(self):
    if self.done:
      return
    self.done = True
    self.close_fd()
    self.code = -1
    if self.proc:
      try:
        self.code = self.proc.wait(timeout=3)
      except subprocess.TimeoutExpired:
        self.proc.kill()
        self.code = self.proc.wait()
    if self.proc and self.app.shell is self and not self.login: # a login stays, so that its errors can be read
      self.app.close_shell()
    else:
      self.app.shell_changed()

  def terminate(self):
    """End the command (the pane is closed or the app quits)"""
    self.close_fd()
    if self.proc and self.proc.poll() is None:
      self.proc.terminate()
      try:
        self.proc.wait(timeout=3)
      except subprocess.TimeoutExpired:
        self.proc.kill()
    self.done = True

  def write(self, data):
    if self.fd is not None and not self.done:
      try:
        os.write(self.fd, data)
      except OSError:
        pass

class TerminalView(Widget, can_focus=True):
  """Shows a shell (its own, in a shell subtab, else the login of the pane); while it has the focus all keys go to
  the shell (in the pane all but Ctrl-] and Ctrl-X). The mouse wheel (and Shift+PgUp/PgDn) scrolls through the
  history, dragging selects text, a right-click on the selection copies it."""
  DEFAULT_CSS = "TerminalView { height: 1fr; }"
  ALLOW_SELECT = False # (its own selection, over the history too)
  WHEEL_LINES = 3

  def __init__(self, session=None):
    super().__init__()
    self.own_session = session
    self.scroll = 0          # lines scrolled back from the bottom
    self.seen_added = 0      # vt.added when the view was last drawn (to keep a scrolled view in place)
    self.selection = None    # ((row, col), (row, col)) in absolute rows (see row_at), anchor first
    self.selecting = False

  # ---- history and selection
  def vt(self):
    s = self.session
    return s.vt if s else None

  def history(self):
    vt = self.vt()
    return vt.history if vt is not None and vt.history is not None else ()

  def row_at(self, y):
    """The absolute row shown at screen row y (stays with its text when it scrolls into the history)"""
    vt = self.vt()
    return vt.added - len(self.history()) + len(self.history()) + y - self.scroll

  def line_of(self, row):
    """The cells of an absolute row: a history line, a screen line, or None"""
    vt = self.vt()
    hist = self.history()
    i = row - (vt.added - len(hist))
    if i < 0:
      return None
    if i < len(hist):
      return hist[i]
    y = i - len(hist)
    if y >= vt.lines:
      return None
    line = vt.buffer[y]
    return [line[x] for x in range(vt.columns)]

  def follow_output(self):
    """Keep a scrolled view on the same lines while new ones arrive"""
    vt = self.vt()
    if vt is None:
      return
    if self.scroll and vt.added > self.seen_added:
      self.scroll = min(self.scroll + vt.added - self.seen_added, len(self.history()))
    self.seen_added = vt.added

  def scroll_by(self, lines):
    vt = self.vt()
    if vt is None:
      return
    self.follow_output()
    self.scroll = max(0, min(self.scroll + lines, len(self.history())))
    self.refresh()

  def selected_text(self):
    if not self.selection:
      return ""
    (r1, c1), (r2, c2) = sorted(self.selection)
    out = []
    for row in range(r1, r2 + 1):
      cells = self.line_of(row)
      if cells is None:
        continue
      start = c1 if row == r1 else 0
      end = c2 + 1 if row == r2 else len(cells)
      out.append("".join(ch.data for ch in cells[start:end]).rstrip())
    return "\n".join(out)

  def in_selection(self, row, col):
    if not self.selection:
      return False
    a, b = sorted(self.selection)
    return a <= (row, col) <= b

  def cell_at(self, event):
    offset = event.get_content_offset(self)
    if offset is None:
      return None
    vt = self.vt()
    y = max(0, min(offset.y, self.size.height - 1))
    x = max(0, min(offset.x, (vt.columns if vt else self.size.width) - 1))
    return self.row_at(y), x

  def on_mouse_scroll_up(self, event):
    event.stop()
    self.wheel(-1)

  def on_mouse_scroll_down(self, event):
    event.stop()
    self.wheel(1)

  def wheel(self, direction):
    vt = self.vt()
    if vt is None:
      return
    if vt.saved is not None: # a full-screen program (less, vim): the wheel sends arrow keys, like other terminals
      s = self.session
      if s and not s.done:
        arrow = ("\x1bO" if DECCKM in vt.mode else "\x1b[") + ("A" if direction < 0 else "B")
        s.write((arrow * self.WHEEL_LINES).encode())
      return
    self.scroll_by(-direction * self.WHEEL_LINES)

  def on_mouse_down(self, event):
    if event.button == 3: # right-click: copy the selection (when it is on it)
      event.stop()
      cell = self.cell_at(event)
      if self.selection and self.selection[0] != self.selection[1] and cell is not None and self.in_selection(*cell):
        text = self.selected_text()
        if text:
          from .screens.dialogs import copy_text
          copy_text(self.app, text, "the selection")
      self.selection = None
      self.refresh()
      return
    if event.button != 1 or self.vt() is None:
      return
    self.focus()
    self.follow_output()
    cell = self.cell_at(event)
    if cell is None:
      return
    self.selection = (cell, cell)
    self.selecting = True
    self.capture_mouse()
    self.refresh()

  def on_mouse_move(self, event):
    if not self.selecting:
      return
    offset = event.get_content_offset(self)
    if offset is not None and offset.y < 0:
      self.scroll_by(1)
    elif offset is not None and offset.y >= self.size.height:
      self.scroll_by(-1)
    cell = self.cell_at(event)
    if cell is not None:
      self.selection = (self.selection[0], cell)
      self.refresh()

  def on_mouse_up(self, event):
    if not self.selecting:
      return
    self.selecting = False
    self.release_mouse()
    if not (self.selection and self.selection[0] != self.selection[1]):
      self.selection = None # (a click without dragging)
    self.refresh() # (a selection stays until it is copied with a right-click, or typing)

  @property
  def session(self):
    return self.own_session or self.app.shell

  def attach(self):
    """The pane was shown or got a new session: start/resize the session with the size of this view"""
    vt = self.vt()
    if vt is None or vt.added < self.seen_added: # (a new session)
      self.scroll, self.selection, self.seen_added = 0, None, vt.added if vt is not None else 0
    if self.session and self.size.width > 0 and self.size.height > 0:
      self.session.ensure(self.size.width, self.size.height)
    self.refresh()

  def on_resize(self, event):
    if self.session and self.display:
      self.session.ensure(event.size.width, event.size.height)
    self.refresh()

  def on_focus(self):
    self.refresh()

  def on_blur(self):
    self.refresh()

  async def on_key(self, event):
    event.stop()
    event.prevent_default()
    key = event.key
    if key in ("shift+pageup", "shift+pagedown"): # scroll the history (not sent to the shell)
      self.scroll_by((self.size.height - 1) * (1 if key == "shift+pageup" else -1))
      return
    if self.scroll or self.selection: # typing goes back to the bottom and ends the selection
      self.scroll, self.selection = 0, None
      self.refresh()
    if self.own_session is None and key == "ctrl+x":
      self.app.close_shell()
      return
    if key == SWITCH_KEY: # the one key that does not go to the shell: back to the job list (or the other side)
      if self.own_session is None:
        self.app.action_switch_side()
      else:
        self.app.show_view(None)
      return
    s = self.session
    if s is None or s.vt is None and not s.done:
      return
    if s.done:
      if key == "enter":
        if self.own_session is not None:
          self.screen.restart()
        else:
          self.app.restart_shell()
      return
    if key in TERM_ARROWS:
      data = ("\x1bO" if DECCKM in s.vt.mode else "\x1b[") + TERM_ARROWS[key]
    elif key.startswith(("ctrl+", "shift+", "alt+")) and key.split("+")[-1] in TERM_ARROWS:
      mod = {"shift": 2, "alt": 3, "ctrl": 5}[key.split("+")[0]]
      data = f"\x1b[1;{mod}{TERM_ARROWS[key.split('+')[-1]]}"
    elif key in TERM_KEYS:
      data = TERM_KEYS[key]
    elif event.character:
      data = event.character
    elif key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
      data = chr(ord(key[5]) & 0x1f)
    elif key.startswith("alt+") and len(key) == 5:
      data = "\x1b" + key[4]
    else:
      return
    s.write(data.encode())

  def on_paste(self, event):
    s = self.session
    if s is None or s.vt is None:
      return
    text = event.text.replace("\r\n", "\r").replace("\n", "\r")
    if (2004 << 5) in s.vt.mode: # bracketed paste
      text = f"\x1b[200~{text}\x1b[201~"
    s.write(text.encode())

  def render_line(self, y):
    width = self.size.width
    s = self.session
    vt = s.vt if s else None
    base = self.rich_style
    if vt is None or y >= vt.lines:
      if s and s.error and y == 0:
        return Strip([Segment(s.error, base + Style(color="red"))]).adjust_cell_length(width, base)
      return Strip.blank(width, base)
    if y == 0:
      self.follow_output()
    row = self.row_at(y)
    if self.scroll:
      cells = self.line_of(row) or []
      line = {x: c for x, c in enumerate(cells)}
      line = collections.defaultdict(lambda: vt.default_char, line)
    else:
      line = vt.buffer[y]
    cursor_x = vt.cursor.x if (not self.scroll and y == vt.cursor.y and self.has_focus and not vt.cursor.hidden and not s.done) else -1
    selected = self.selection is not None and self.selection[0] != self.selection[1]
    segments, text, style = [], "", None
    for x in range(min(vt.columns, width)):
      ch = line[x]
      if ch.data == "": # second cell of a wide character
        continue
      st = base + vt_style(ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.strikethrough,
                           bool(ch.reverse) ^ (x == cursor_x) ^ bool(selected and self.in_selection(row, x)))
      if st != style:
        if text:
          segments.append(Segment(text, style))
        text, style = "", st
      text += ch.data
    if text:
      segments.append(Segment(text, style))
    strip = Strip(segments).adjust_cell_length(width, base)
    mark = ""
    if y == 0 and self.selection and self.selection[0] != self.selection[1] and not self.selecting:
      mark = " selected · right-click on it copies · click elsewhere: none "
    elif y == 0 and self.scroll:
      mark = f" ↑ {self.scroll} of {len(self.history())} lines back · wheel, Shift+PgUp/PgDn · typing returns "
    if mark:
      if len(mark) < width:
        strip = Strip([*strip.crop(0, width - len(mark)), Segment(mark, base + Style(reverse=True, bold=True))])
    return strip

class TerminalPane(Vertical):
  """Right side of every window: title bar with a close button and the shell"""
  def compose(self) -> ComposeResult:
    with Horizontal(classes="tbar"):
      yield Static(classes="ttitle")
      yield Button("ⓘ", classes="tinfo", compact=True, tooltip="copy, paste, keys, closing")
      yield Button("✕", classes="tclose", compact=True, tooltip="close the shell (Ctrl-X)")
    yield TerminalView()

  def on_mount(self):
    self.watch(self.app, "shell_version", self.sync)
    self.watch(self.app, "pane_width", self.set_width)

  def sync(self, _=None):
    s = self.app.shell
    self.display = s is not None
    for splitter in self.parent.query(Splitter):
      splitter.display = s is not None
    if s is None:
      return
    if s.done:
      status = f"ended (exit code {s.code}) · Enter: new shell · ✕ or Ctrl-X: close"
    else:
      status = "Ctrl-] switches between the sides"
    self.query_one(".ttitle", Static).update(f"{s.title} · {status}")
    self.call_after_refresh(self.query_one(TerminalView).attach)

  def set_width(self, width):
    self.styles.width = width if width else "50%"

  @on(Button.Pressed, ".tinfo")
  def info_pressed(self, event):
    event.stop()
    if self.app.shell is not None:
      self.app.show_terminal_help(self.app.shell, pane=True, then=lambda: self.query_one(TerminalView).focus())

  @on(Button.Pressed, ".tclose")
  def close_pressed(self, event):
    event.stop()
    self.app.close_shell()

class Splitter(Widget):
  """Vertical bar between the window and the terminal pane, drag it with the mouse to resize"""
  def __init__(self):
    super().__init__()
    self.dragging = False

  def render(self):
    return ""

  def on_mouse_down(self, event):
    self.dragging = True
    self.capture_mouse()
    self.add_class("-dragging")
    event.stop()

  def on_mouse_move(self, event):
    if self.dragging:
      total = self.screen.size.width
      self.app.pane_width = max(20, min(total - event.screen_x - 1, total - 20))

  def on_mouse_up(self, event):
    if self.dragging:
      self.dragging = False
      self.release_mouse()
      self.remove_class("-dragging")
