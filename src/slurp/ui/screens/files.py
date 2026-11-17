"""Files and directories: opening a path in the fitting window, the directory browser, the text viewer"""

import codecs
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

import humanize
from rich.text import Text
from textual import on
from textual.app import SuspendNotSupported
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input, MarkdownViewer, Static, TextArea
from textual.widgets.text_area import Selection


from ...state import TAIL_BYTES
from ...cluster.remote import (
  cluster_work, conn, fs_getsize, fs_isdir, fs_isfile, fs_read, fs_scandir, in_background, start_worker, tab_name
)
from ...cluster.paths import is_binary, resolve_path
from .. import icons
from ..widgets import clean, ClickTable, crumb_title, PathArea
from .base import SplitScreen

@dataclass
class FileView:
  """A specialized viewer selected by inspecting a file, before the text fallback."""
  name: str
  matches: object
  open: object

FILE_VIEWS = []

def register_file_view(name, matches, open_view):
  FILE_VIEWS.append(FileView(name, matches, open_view))

def open_path(app, crumbs, path, bases=(), env=None, follow=False):
  """Open a directory or dispatch a file to its first matching specialized view. What the path is (and what the
  view needs) is found out in a worker: on a remote cluster that takes round trips, which must not block the UI.
  A view's open() runs in the worker and returns what to do on the UI thread."""
  def work():
    if fs_isdir(path):
      return lambda: app.open_window(DirScreen(crumbs, path, bases=bases, env=env))
    if not fs_isfile(path):
      return lambda: app.notify(f"{path} does not exist (yet)", severity="warning")
    for view in FILE_VIEWS:
      if view.matches(path):
        return view.open(app, crumbs, path, bases, env)
    if is_binary(path):
      return lambda: app.notify(f"{path} is a binary file", severity="warning")
    more_bases = list(dict.fromkeys([*bases, os.path.dirname(path)]))
    return lambda: app.open_window(FileScreen([*crumbs, os.path.basename(path)], path=path, follow=follow,
                                              bases=more_bases, env=env))
  in_background(app, work, lambda show: show() if show else None, group="open", name=f"opening {path}")

def open_resolved(screen, word, crumbs, bases, env, quiet=True):
  """Open the file or directory a word of a text names (looked up in the background)"""
  def opened(path):
    if path:
      open_path(screen.app, crumbs, path, bases, env)
    elif not quiet:
      screen.notify(f"no existing file or directory named {word}", severity="warning")
  in_background(screen, lambda: resolve_path(word, bases, env) or "", opened, group="resolve")

def is_markdown(path):
  return os.path.splitext(path)[1].lower() in {".md", ".markdown", ".mdown", ".mdx"}

def open_markdown(app, crumbs, path, bases=(), env=None):
  """(runs in a worker)"""
  try:
    markdown = fs_read(path, size=TAIL_BYTES).decode("utf-8", errors="replace") # the first TAIL_BYTES at most
  except OSError as e:
    msg = f"cannot read {path}: {e}" # (e is gone after the except block)
    return lambda: app.notify(msg, severity="error")
  return lambda: app.open_window(MarkdownScreen(crumbs, path, markdown, bases=bases, env=env))

class MarkdownScreen(SplitScreen):
  """Render a Markdown file with Textual's viewer and table of contents."""

  @property
  def icon_name(self): # (of its subtab)
    return icons.file_kind(os.path.basename(self.path))
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back"), Binding("r", "reload", "Reload")]

  def __init__(self, crumbs, path, markdown, bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.markdown = markdown
    self.bases, self.env = list(bases), env

  def compose_left(self):
    yield MarkdownViewer(self.markdown, id="markdown-viewer", show_table_of_contents=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#markdown-viewer", MarkdownViewer).focus()

  def action_reload(self):
    def read():
      try:
        return fs_read(self.path, size=TAIL_BYTES).decode("utf-8", errors="replace")
      except OSError as e:
        self.app.call_from_thread(self.notify, f"cannot read {self.path}: {e}", title=tab_name(), severity="error")
        return None
    def show(markdown):
      if markdown is not None:
        self.markdown = markdown
        self.query_one("#markdown-viewer", MarkdownViewer).update(markdown)
    in_background(self, read, show, group="reload")

  def action_back(self):
    self.pop_back()

DIR_COLUMNS = [("name", "Name"), ("size", "Size"), ("mtime", "Modified")]
DIR_LIMIT = 20000 # entries listed at most

class DirScreen(SplitScreen):
  """Directory browser: click (or Enter) a subdirectory to enter it, a file to open it"""
  SHOW_BACK = True
  icon_name = "folder" # (of its subtab)
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("backspace,u", "parent", "Parent"),
    Binding("slash", "filter", "Filter"),
    Binding("f", "follow", "Follow"),
    Binding("r", "reload", "Reload"),
    Binding("full_stop", "hidden", "Hidden files"),
  ]

  def __init__(self, crumbs, path, bases=(), env=None, pick=None):
    super().__init__()
    self.path = os.path.normpath(path)
    self.pick = pick # pick mode: a file is chosen (pick(path)) instead of opened; directories open in place
    self.base_crumbs = list(crumbs)
    self.crumbs = [*crumbs, self.path.rstrip("/") + "/"]
    self.bases, self.env = list(bases), env # passed on to the files opened from here (e.g. the job's WorkDir)
    self.entries = []
    self.pattern = ""
    self.show_hidden = True
    self.following = False
    self.sort_column, self.sort_reverse = "name", False

  def compose_left(self):
    yield Static(id="dstatus")
    yield ClickTable(id="dir", cursor_type="row", zebra_stripes=True)
    yield Input(id="dfilter", placeholder="filter names (Enter: apply, Esc: clear)")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    table = self.query_one("#dir", ClickTable)
    for key, label in DIR_COLUMNS:
      table.add_column(label, key=key)
    self.timer = self.set_interval(2.0, self.action_reload, pause=True)
    self.query_one("#dstatus", Static).update(f"{self.path} · listing …")
    self.action_reload()
    table.focus()

  def scan(self):
    entries = []
    try:
      with fs_scandir(self.path) as it:
        for i, de in enumerate(it):
          if i >= DIR_LIMIT:
            break
          try:
            st = de.stat()
            isdir = de.is_dir()
          except OSError:
            st, isdir = None, False
          entries.append({"name": de.name, "dir": isdir, "size": st.st_size if st and not isdir else None,
                          "mtime": st.st_mtime if st else 0, "link": de.is_symlink()})
    except OSError as e:
      self.app.call_from_thread(self.notify, f"cannot list {self.path}: {e}", title=tab_name(), severity="error")
    return entries

  scanning = False

  def action_reload(self):
    """List the directory in the background (also every 2 s while following, one listing at a time)"""
    if self.scanning:
      return
    self.scanning = True
    def listed(entries):
      self.scanning = False
      if entries is not None:
        self.entries = entries
        self.populate()
    in_background(self, self.scan, listed, group="scan")

  def populate(self):
    table = self.query_one("#dir", ClickTable)
    current = None
    if table.row_count:
      try:
        current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
      except Exception:
        pass
    table.clear()
    if self.path != "/":
      table.add_row(Text(icons.label("folder", "../"), style="bold"), "", "", key="..")
    keys = {"name": lambda e: e["name"].lower(), "size": lambda e: e["size"] or 0, "mtime": lambda e: e["mtime"]}
    shown = [e for e in self.entries if (self.show_hidden or not e["name"].startswith("."))
             and self.pattern.lower() in e["name"].lower()]
    shown.sort(key=keys[self.sort_column], reverse=self.sort_reverse)
    shown.sort(key=lambda e: not e["dir"]) # directories first
    for e in shown:
      style = "bold blue" if e["dir"] else ""
      if e["name"].startswith("."):
        style += " dim"
      name = Text(icons.label(icons.file_kind(e["name"], e["dir"]), e["name"] + ("/" if e["dir"] else "") + (" @" if e["link"] else "")),
                  style=style.strip())
      size = humanize.naturalsize(e["size"]) if e["size"] is not None else ""
      mtime = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M:%S") if e["mtime"] else ""
      table.add_row(name, size, mtime, key=e["name"])
    if current is not None:
      try:
        table.move_cursor(row=table.get_row_index(current))
      except Exception:
        pass
    status = f"{self.path} · {len(shown)} of {len(self.entries)} entries"
    if self.pick is not None:
      status = "choose a file (Enter or click) · Esc: cancel · " + status
    if len(self.entries) >= DIR_LIMIT:
      status += f" (only the first {DIR_LIMIT} are listed)"
    if self.pattern:
      status += f" · filter '{self.pattern}'"
    status += " · following (f)" if self.following else ""
    self.query_one("#dstatus", Static).update(status)

  def open(self, name):
    path = os.path.dirname(self.path) if name == ".." else os.path.join(self.path, name)
    if self.pick is not None:
      entry = next((e for e in self.entries if e["name"] == name), None)
      if name == ".." or entry and entry["dir"]:
        self.path = os.path.normpath(path)
        self.crumbs = [*self.base_crumbs, self.path.rstrip("/") + "/"]
        self.sub_title = crumb_title(self.crumbs)
        self.action_reload()
      else:
        pick, self.pick = self.pick, None
        self.app.pop_screen()
        pick(path)
      return
    open_path(self.app, self.crumbs, path, bases=self.bases, env=self.env)

  @on(DataTable.RowSelected, "#dir")
  def selected(self, event):
    event.stop()
    self.query_one("#dir", ClickTable).take_clicked_column()
    if event.row_key.value is not None:
      self.open(event.row_key.value)

  @on(DataTable.HeaderSelected, "#dir")
  def sort_by(self, event):
    event.stop()
    col = event.column_key.value
    if col == self.sort_column:
      self.sort_reverse = not self.sort_reverse
    else:
      self.sort_column, self.sort_reverse = col, col != "name"
    self.populate()

  def action_back(self):
    search = self.query_one("#dfilter", Input)
    if search.has_class("visible"):
      search.remove_class("visible")
      self.pattern = ""
      self.populate()
      self.query_one("#dir").focus()
    else:
      self.pop_back()

  def action_parent(self):
    if self.path != "/":
      self.open("..")

  def action_filter(self):
    search = self.query_one("#dfilter", Input)
    search.add_class("visible")
    search.value = self.pattern
    search.focus()

  @on(Input.Changed, "#dfilter")
  def filter_changed(self, event):
    self.pattern = event.value
    self.populate()

  @on(Input.Submitted, "#dfilter")
  def filter_submitted(self, event):
    event.input.remove_class("visible")
    self.query_one("#dir").focus()

  def action_follow(self):
    self.following = not self.following
    (self.timer.resume if self.following else self.timer.pause)()
    self.populate()

  def action_hidden(self):
    self.show_hidden = not self.show_hidden
    self.populate()

class FileScreen(SplitScreen):
  """Read-only viewer for a file (with tail -f style following) or for text (optionally re-run by a refresher)"""
  SHOW_BACK = True

  @property
  def icon_name(self): # (of its subtab)
    return icons.file_kind(os.path.basename(self.path)) if self.path else "file"
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("enter", "open_path", "Open path"),
    Binding("f", "follow", "Follow"),
    Binding("slash", "search", "Search"),
    Binding("n", "find(True)", "Next", show=False),
    Binding("N", "find(False)", "Prev", show=False),
    Binding("g", "top", "Top"),
    Binding("G", "bottom", "Bottom"),
    Binding("r", "reload", "Reload"),
    Binding("w", "watch", "Watch"),
    Binding("A", "load_more", "Earlier part", show=False),
    Binding("l", "less", "less"),
  ]

  def __init__(self, crumbs, path=None, text=None, refresher=None, follow=False, bases=(), env=None):
    super().__init__()
    self.crumbs, self.path, self.text, self.refresher = crumbs, path, text, refresher
    self.bases, self.env = list(bases), env # to resolve relative paths in the text
    self.following = follow and path is not None
    self.watching = False
    self.start_pos = 0 # where in the file the text shown starts (only the end of a large file is read)
    self.file_pos = 0
    self.truncated = False
    self.term = ""
    self.decoder = None
    self.timer = None

  def compose_left(self):
    yield Static(id="fstatus")
    area = PathArea(id="text", read_only=True, show_line_numbers=True, soft_wrap=False, highlight_cursor_line=False)
    yield area
    yield Input(id="search", placeholder="search (Enter: find, Esc: cancel)")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.load()
    self.timer = self.set_interval(1.0, self.poll, pause=not self.following)
    self.rerun_timer = self.set_interval(5.0, self.rerun, pause=True)
    self.query_one("#text").focus()

  def check_action(self, action, parameters):
    if action == "follow" or action == "load_more":
      return self.path is not None
    if action == "watch":
      return self.refresher is not None
    return True

  @property
  def area(self):
    return self.query_one("#text", TextArea)

  file_size = None # as last read

  def update_status(self):
    parts = []
    if self.path:
      if self.file_size is None:
        parts.append(f"{self.path}  (not readable)" if self.decoder else f"{self.path} · loading …")
      else:
        parts.append(f"{self.path}  {humanize.naturalsize(self.file_size)}")
      if self.truncated:
        parts.append(f"showing the last {humanize.naturalsize((self.file_size or 0) - self.start_pos)} "
                     f"(A: the {humanize.naturalsize(TAIL_BYTES)} before)")
      parts.append("following" if self.following else "not following (f)")
    if self.refresher:
      parts.append("watching every 5 s" if self.watching else "w: watch, r: run again")
    self.query_one("#fstatus", Static).update(" · ".join(parts))

  def load(self):
    """Show the text, or read the file (its end if it is large) in the background"""
    if self.path is None:
      self.area.load_text(clean(self.text or ""))
      self.update_status()
      return
    def read():
      try:
        size = fs_getsize(self.path)
        truncated = size > TAIL_BYTES
        offset = size - TAIL_BYTES if truncated else 0
        data = fs_read(self.path, offset, size - offset)
        return size, truncated, offset + len(data), data
      except OSError as e:
        return None, False, 0, f"cannot read {self.path}: {e}\n".encode()
    in_background(self, read, self.file_read, group="load")
    self.update_status()

  def file_read(self, result):
    if result is None:
      return
    area = self.area
    self.file_size, self.truncated, self.file_pos, data = result
    self.start_pos = self.file_pos - len(data)
    self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    if self.truncated:
      first, newline, data = data.partition(b"\n") # drop the partial first line
      self.start_pos += len(first) + len(newline)
    area.load_text(clean(self.decoder.decode(data)))
    area.history.clear()
    self.action_bottom()
    self.update_status()

  polling = False

  def poll(self):
    """Append what was written to the file since the last poll. Read in a worker: on a remote cluster it takes
    round trips, which must not block the UI."""
    if self.polling:
      return
    self.polling, pos = True, self.file_pos
    def read():
      size, data = None, b""
      try:
        size = fs_getsize(self.path)
        if size > pos:
          data = fs_read(self.path, pos, TAIL_BYTES)
      except OSError:
        pass
      finally:
        self.app.call_from_thread(self.append, pos, size, data)
    start_worker(self, read, "poll", exclusive=False)

  def append(self, pos, size, data):
    self.polling = False
    if size is None or pos != self.file_pos or not self.is_mounted or self.decoder is None: # unreadable, reloaded
      return
    self.file_size = size
    if size < pos: # truncated or replaced
      self.load()
      return
    if not data:
      return
    self.file_pos += len(data)
    area = self.area
    at_end = area.scroll_y >= area.max_scroll_y - 1
    area.insert(clean(self.decoder.decode(data)), area.document.end, maintain_selection_offset=True)
    area.history.clear()
    if at_end:
      area.scroll_end(animate=False)
    self.update_status()

  def action_back(self):
    search = self.query_one("#search", Input)
    if search.has_class("visible"):
      search.remove_class("visible")
      self.area.focus()
    else:
      self.pop_back()

  @on(PathArea.PathClicked)
  def path_clicked(self, event):
    open_resolved(self, event.word, self.crumbs, self.bases, self.env)

  def action_open_path(self):
    word = self.area.word_at_cursor()
    if word:
      open_resolved(self, word, self.crumbs, self.bases, self.env, quiet=False)
    else:
      self.notify("no file or directory name at the cursor", severity="warning")

  def action_follow(self):
    self.following = not self.following
    if self.following:
      self.poll()
      self.action_bottom()
      self.timer.resume()
    else:
      self.timer.pause()
    self.update_status()

  def action_top(self):
    self.area.move_cursor((0, 0))
    self.area.scroll_home(animate=False)

  def action_bottom(self):
    area = self.area
    area.move_cursor((area.document.line_count - 1, 0))
    area.scroll_end(animate=False)

  def action_load_more(self):
    """Read the TAIL_BYTES before the text shown (in the background) and put them in front: a large file is never read
    as a whole, only the parts asked for"""
    if not self.path or self.start_pos <= 0:
      self.notify("the whole file is shown")
      return
    end = self.start_pos
    start = max(0, end - TAIL_BYTES)
    def read():
      return fs_read(self.path, start, end - start)
    def got(data):
      if data is None or self.start_pos != end:
        return
      new_start = start
      if start > 0:
        first, newline, data = data.partition(b"\n") # a partial line: it starts in the part before
        new_start += len(first) + len(newline)
      text = clean(data.decode("utf-8", errors="replace"))
      area = self.area
      row = area.cursor_location[0]
      area.insert(text, (0, 0))
      area.history.clear()
      added = text.count("\n")
      area.move_cursor((row + added, 0))
      area.scroll_to(y=added, animate=False)
      self.start_pos, self.truncated = new_start, new_start > 0
      self.update_status()
    in_background(self, read, got, group="load-more")

  def action_reload(self):
    if self.refresher:
      self.rerun()
    else:
      self.load()

  def action_watch(self):
    self.watching = not self.watching
    (self.rerun_timer.resume if self.watching else self.rerun_timer.pause)()
    self.update_status()

  @cluster_work("rerun")
  def rerun(self):
    text = self.refresher()
    self.app.call_from_thread(self.show_text, text)

  def show_text(self, text):
    self.text = text
    self.load()

  def action_search(self):
    search = self.query_one("#search", Input)
    search.add_class("visible")
    search.value = self.term
    search.focus()

  @on(Input.Submitted, "#search")
  def search_submitted(self, event):
    event.input.remove_class("visible")
    self.term = event.value
    self.area.focus()
    self.action_find(True)

  def action_find(self, forward):
    if not self.term:
      self.action_search()
      return
    area, term = self.area, self.term.lower()
    doc = area.document
    n = doc.line_count
    sel = area.selection
    row, col = (max(sel.start, sel.end) if forward else min(sel.start, sel.end))
    for i in range(n + 1):
      r = (row + i) % n if forward else (row - i) % n
      line = doc.get_line(r).lower()
      if forward:
        c = line.find(term, col if i == 0 else 0)
      else:
        c = line.rfind(term, 0, col if i == 0 else len(line))
        c = -1 if (i == 0 and c >= col) else c
      if c >= 0:
        area.selection = Selection((r, c), (r, c + len(term)))
        area.scroll_cursor_visible(center=True)
        return
    self.notify(f"'{self.term}' not found", severity="warning")

  def action_less(self):
    path = self.path
    if path is None:
      import tempfile
      with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write(self.text or "")
        path = tmp.name
    try:
      with self.app.suspend():
        args = ["less", "-R", "+F" if self.following else "+G", path]
        subprocess.run(conn().command_argv(args, interactive=True) if conn() else args)
    except SuspendNotSupported:
      self.notify("cannot suspend the terminal here", severity="error")
    finally:
      if path != self.path:
        os.unlink(path)
    if self.path:
      self.poll()
register_file_view("Markdown", is_markdown, open_markdown)
