"""Files and directories: opening a path in the fitting window, the directory browser, the text viewer"""

import codecs
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

import humanize
from rich.text import Text
from textual import on
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, MarkdownViewer, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection


from ...state import TAIL_BYTES
from ...cluster.remote import (
  cluster_work, conn, fs_getsize, fs_isdir, fs_isfile, fs_read, in_background, start_worker, tab_name
)
from ...cluster.paths import is_binary, resolve_path
from ...cluster.storage import drive_dirs
from ...cluster.listing import can_read, can_write, listing, matches_name, mode_text, search, short_acl
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

def open_path(app, crumbs, path, bases=(), env=None, follow=False, here=False):
  """Open a directory or dispatch a file to its first matching specialized view, in a subtab of its own (one per
  path: open already, its subtab is shown). here: a directory opens where it is opened (a folder entered in a
  directory view: ← goes back); a file still gets a subtab. What the path is (and what the view needs) is found
  out in a worker: on a remote cluster that takes round trips, which must not block the UI. A view's open() runs in
  the worker and returns what to do on the UI thread."""
  def work():
    if fs_isdir(path):
      show = lambda: app.open_window(DirScreen(crumbs, path, bases=bases, env=env))
      return show if here else (lambda: app.in_new_subtab(show, key=f"path {os.path.normpath(path)}"))
    show = file_view()
    return (lambda: app.in_new_subtab(show, key=f"path {os.path.normpath(path)}")) if show is not None else None

  def file_view():
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

PLACE_LABELS = {"HOME": "Home", "SCRATCH": "Scratch", "PROJECT": "Project", "STORE": "Store", "WORK": "Work"}

def home_of(cluster):
  """The login directory of a tab ("" while its session is not ready)"""
  if cluster.destination:
    return cluster.connection.home if cluster.connection and cluster.connection.ready else ""
  return os.path.expanduser("~")

def with_places(node, cluster, then):
  """then(places) with the places of a tab: [(label, path)] of its home, $SCRATCH, $PROJECT, $STORE and $WORK (those
  set on the cluster), looked up in the background the first time (then kept with the tab)"""
  home = home_of(cluster)
  if cluster.places is not None or not home:
    then(cluster.places or ([("Home", home)] if home else []))
    return
  def found(dirs):
    if dirs:
      cluster.places = [(PLACE_LABELS.get(var, var.title()), path) for var, path in dirs]
    then(cluster.places or [("Home", home)])
  in_background(node, lambda: drive_dirs(home), found, group="places", name="looking up the directories",
                connection=cluster.connection if cluster.destination else None)

def expand_path(text, home):
  """A path typed in: ~ and ~/… are in home, a relative path is relative to home"""
  text = text.strip()
  if text == "~" or text.startswith("~/"):
    text = home + text[1:]
  elif not text.startswith("/"):
    text = os.path.join(home, text)
  return os.path.normpath(text)

class OpenDirScreen(ModalScreen[str | None]):
  """Which directory to open (dismissed with its path, or None): the places of the tab (home first, the default) or
  a path typed in"""
  DEFAULT_CSS = """
  OpenDirScreen { align: center middle; }
  #opendir-dialog { width: 80; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #opendir-title { text-style: bold; margin-bottom: 1; }
  #opendir-places { height: auto; max-height: 10; margin-bottom: 1; }
  #opendir-path { width: 1fr; }
  #opendir-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #opendir-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster = cluster
    self.home = home_of(cluster)
    self.places = []

  def compose(self) -> ComposeResult:
    with Vertical(id="opendir-dialog"):
      yield Static(f"Open a directory on {self.cluster.name}", id="opendir-title", markup=False)
      yield OptionList(id="opendir-places")
      yield Input(placeholder="or a path (~/… and relative paths are in your home)", id="opendir-path")
      with Horizontal(id="opendir-buttons"):
        yield Button("Cancel", id="opendir-cancel", compact=True)
        yield Button("Open", id="opendir-open", variant="primary", compact=True)

  def on_mount(self):
    self.show_places([("Home", self.home)], looking=True)
    self.query_one("#opendir-places", OptionList).focus()
    with_places(self, self.cluster, self.show_places)

  def show_places(self, places, looking=False):
    self.places = places
    options = self.query_one("#opendir-places", OptionList)
    highlighted = options.highlighted or 0
    width = max((len(label) for label, _ in places), default=0) + 2
    options.clear_options()
    for i, (label, path) in enumerate(places):
      options.add_option(Option(Text.assemble((label.ljust(width), "bold"), (path, "dim")), id=str(i)))
    if looking:
      options.add_option(Option(Text("looking up $SCRATCH, $PROJECT, … ", style="dim italic"), disabled=True))
    options.highlighted = min(highlighted, len(places) - 1) if places else None

  @on(OptionList.OptionSelected, "#opendir-places")
  def place_chosen(self, event):
    event.stop()
    self.dismiss(self.places[int(event.option.id)][1])

  @on(Button.Pressed, "#opendir-open")
  @on(Input.Submitted, "#opendir-path")
  def open_pressed(self, event):
    event.stop()
    typed = self.query_one("#opendir-path", Input).value.strip()
    if typed:
      self.dismiss(expand_path(typed, self.home))
      return
    highlighted = self.query_one("#opendir-places", OptionList).highlighted
    if highlighted is not None and highlighted < len(self.places):
      self.dismiss(self.places[highlighted][1])

  @on(Button.Pressed, "#opendir-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  def action_cancel(self):
    self.dismiss(None)

DIR_COLUMNS = [("name", "Name"), ("size", "Size"), ("mtime", "Modified"), ("mode", "Permissions"), ("owner", "Owner"),
               ("you", "You"), ("acl", "ACL")]

def access_text(entry):
  """What the user cannot do with an entry: "no read" (red), "no write" (yellow); empty if both"""
  marks = []
  if not can_read(entry):
    marks.append(("no read", "bold red"))
  if not can_write(entry):
    marks.append(("no write", "yellow"))
  return Text(" ").join(Text(text, style=style) for text, style in marks)
DIR_LIMIT = 20000 # entries listed at most
SEARCH_LIMIT = 5000 # matches of a search in the subdirectories at most

class DirScreen(SplitScreen):
  """Directory browser: click (or Enter) a subdirectory to enter it, a file to open it"""
  SHOW_BACK = True
  icon_name = "folder" # (of its subtab)
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("backspace,u", "parent", "Parent"),
    Binding("slash", "filter", "Search"),
    Binding("ctrl+r", "recursive", "Subdirectories", show=False, priority=True),
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
    self.this = None # the directory itself (its permissions, what the user may do)
    self.results = None   # the search's matches in the subdirectories (None: no such search; the listing is shown)
    self.searching = None # (directories looked at, still to look at) while a search runs
    self.search_gen = 0   # (results of an older search are dropped)
    self.search_timer = None
    self.show_hidden = True
    self.following = False
    self.sort_column, self.sort_reverse = "name", False
    self.cluster, self.places = None, []

  def compose_left(self):
    with Horizontal(id="dbar"):
      yield Input(self.path, id="dpath", tooltip="The directory: edit and Enter to go there (~ is your home)")
      yield Horizontal(id="dplaces")
    yield Static(id="dstatus")
    yield ClickTable(id="dir", cursor_type="row", zebra_stripes=True)
    with Horizontal(id="dsearch"):
      yield Input(id="dfilter", placeholder="search names: a part of them, or *.out, run_? … (Esc: all again)")
      yield Checkbox("Subdirectories", id="drecursive", compact=True,
                     tooltip="search the subdirectories too, in the background (Ctrl+R)")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    table = self.query_one("#dir", ClickTable)
    for key, label in DIR_COLUMNS:
      table.add_column(label, key=key)
    self.timer = self.set_interval(2.0, self.action_reload, pause=True)
    self.query_one("#dstatus", Static).update("listing …")
    self.action_reload()
    table.focus()
    self.cluster = self.app.cluster if self.app.clusters else None
    if self.cluster is not None:
      with_places(self, self.cluster, self.show_places)

  def show_places(self, places):
    """The places of the tab as buttons next to the path (home, scratch, project, …)"""
    self.places = places
    bar = self.query_one("#dplaces", Horizontal)
    bar.remove_children()
    bar.mount_all(Button(label, id=f"dplace-{i}", compact=True, tooltip=path) for i, (label, path) in enumerate(places))

  @on(Button.Pressed, "#dplaces Button")
  def place_pressed(self, event):
    event.stop()
    self.go(self.places[int(event.button.id.removeprefix("dplace-"))][1])

  @on(Input.Submitted, "#dpath")
  def path_submitted(self, event):
    event.stop()
    home = home_of(self.cluster) if self.cluster is not None else ""
    path = expand_path(event.value, home or "/")
    event.input.value = self.path
    self.query_one("#dir").focus()
    if path != self.path:
      self.go(path)

  def go(self, path):
    """To another directory (from the path or a place): here (← comes back), in place while picking a file; a file
    opens in a subtab"""
    if self.pick is None:
      open_path(self.app, self.base_crumbs, path, bases=self.bases, env=self.env, here=True)
      return
    def went(isdir):
      if isdir:
        self.enter(path)
      elif isdir is not None:
        self.notify(f"{path} is not a directory", severity="warning")
    in_background(self, lambda: fs_isdir(path), went, group="go")

  def enter(self, path):
    """Show another directory in this view (picking a file)"""
    self.path = os.path.normpath(path)
    self.crumbs = [*self.base_crumbs, self.path.rstrip("/") + "/"]
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#dpath", Input).value = self.path
    self.action_reload()

  def scan(self):
    """(the directory, its entries) with permissions, owners, ACLs and what the user may do (cluster/listing.py)"""
    try:
      return listing(self.path, DIR_LIMIT)
    except OSError as e:
      self.app.call_from_thread(self.notify, f"cannot list {self.path}: {e}", title=tab_name(), severity="error")
      return None, []

  scanning = False

  def action_reload(self):
    """List the directory in the background (also every 2 s while following, one listing at a time)"""
    if self.scanning:
      return
    self.scanning = True
    def listed(result):
      self.scanning = False
      if result is not None:
        self.this, self.entries = result
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
    if self.path != "/" and self.results is None:
      table.add_row(Text(icons.label("folder", "../"), style="bold"), "", "", "", "", "", "", key="..")
    keys = {"name": lambda e: e["name"].lower(), "size": lambda e: e["size"] or 0, "mtime": lambda e: e["mtime"],
            "mode": mode_text, "owner": lambda e: (e["owner"], e["group"]),
            "you": lambda e: (can_read(e), can_write(e)), "acl": lambda e: short_acl(e["acl"])}
    shown = self.shown()
    shown.sort(key=keys[self.sort_column], reverse=self.sort_reverse)
    shown.sort(key=lambda e: not e["dir"]) # directories first
    for e in shown:
      self.add_entry(table, e)
    if current is not None:
      try:
        table.move_cursor(row=table.get_row_index(current))
      except Exception:
        pass
    self.show_status(len(shown))

  def shown(self):
    """The entries shown: the search's matches in the subdirectories, or the listing (filtered by the search)"""
    if self.results is not None:
      return [e for e in self.results if self.show_hidden or not os.path.basename(e["name"]).startswith(".")]
    text = self.query_one("#dfilter", Input).value if self.query_one("#dsearch").has_class("visible") else ""
    return [e for e in self.entries if (self.show_hidden or not e["name"].startswith(".")) and (not text.strip() or matches_name(e["name"], text))]

  def add_entry(self, table, e):
    style = "bold blue" if e["dir"] else ""
    if os.path.basename(e["name"]).startswith(".") or not can_read(e):
      style += " dim"
    name = Text(icons.label(icons.file_kind(os.path.basename(e["name"]), e["dir"]), e["name"] + ("/" if e["dir"] else "") + (" @" if e["link"] else "")),
                style=style.strip())
    size = humanize.naturalsize(e["size"]) if e["size"] is not None else ""
    mtime = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M:%S") if e["mtime"] else ""
    table.add_row(name, size, mtime, Text(mode_text(e), style="bold" if e["acl"] else ""),
                  f"{e['owner']}:{e['group']}", access_text(e), Text(short_acl(e["acl"]), style="cyan"), key=e["name"])

  def show_status(self, count=None):
    if count is None:
      count = len(self.shown())
    text = self.query_one("#dfilter", Input).value.strip()
    if self.results is not None:
      status = Text(f"{count} found for '{text}' here and in the subdirectories")
      if self.searching is not None:
        done, left = self.searching
        status.append(f" · searching: {done} directories looked at, {left} to go (Esc stops)", style="bold yellow")
      elif len(self.results) >= SEARCH_LIMIT:
        status.append(f" · stopped at {SEARCH_LIMIT}: say more precisely what you look for", style="yellow")
      if self.pick is not None:
        status = Text("choose a file (Enter or click) · Esc: cancel · ") + status
      self.query_one("#dstatus", Static).update(status)
      return
    status = Text(f"{count} of {len(self.entries)} entries")
    if self.this is not None:
      status.append(f" · {mode_text(self.this)} {self.this['owner']}:{self.this['group']}")
      if self.this["acl"]:
        status.append(f" · ACL {short_acl(self.this['acl'])}", style="cyan")
      if not can_read(self.this):
        status.append(" · you cannot list this directory", style="bold red")
      elif not can_write(self.this):
        status.append(" · read-only for you", style="yellow")
    if self.pick is not None:
      status = Text("choose a file (Enter or click) · Esc: cancel · ") + status
    if len(self.entries) >= DIR_LIMIT:
      status += f" (only the first {DIR_LIMIT} are listed)"
    if text:
      status += f" · search '{text}'"
    status += " · following (f)" if self.following else ""
    self.query_one("#dstatus", Static).update(status)

  def open(self, name):
    path = os.path.dirname(self.path) if name == ".." else os.path.join(self.path, name)
    if self.pick is not None:
      entry = next((e for e in (self.results if self.results is not None else self.entries) if e["name"] == name), None)
      if name == ".." or entry and entry["dir"]:
        self.enter(path)
      else:
        pick, self.pick = self.pick, None
        self.app.pop_screen()
        pick(path)
      return
    open_path(self.app, self.crumbs, path, bases=self.bases, env=self.env, here=True)

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
    bar, path = self.query_one("#dsearch"), self.query_one("#dpath", Input)
    if path.has_focus: # (Esc while editing the path: as it was)
      path.value = self.path
      self.query_one("#dir").focus()
    elif self.searching is not None: # (first Esc: the search stops, its results stay)
      self.stop_search()
      self.show_status()
    elif bar.has_class("visible"): # (then: no search, the listing again)
      bar.remove_class("visible")
      self.query_one("#dfilter", Input).value = ""
      self.results = None
      self.populate()
      self.query_one("#dir").focus()
    else:
      self.pop_back()

  def action_parent(self):
    if self.path != "/":
      self.open("..")

  def action_filter(self):
    self.query_one("#dsearch").add_class("visible")
    self.query_one("#dfilter", Input).focus()

  def action_recursive(self):
    if self.query_one("#dsearch").has_class("visible"):
      box = self.query_one("#drecursive", Checkbox)
      box.value = not box.value

  def recursive(self):
    return self.query_one("#drecursive", Checkbox).value

  @on(Input.Changed, "#dfilter")
  def filter_changed(self, event):
    if not self.recursive(): # (this directory: filtered as you type)
      self.populate()
      return
    if self.search_timer is not None: # (the subdirectories: searched when the typing pauses)
      self.search_timer.stop()
    self.search_timer = self.set_timer(0.5, self.start_search)

  @on(Input.Submitted, "#dfilter")
  def filter_submitted(self, event):
    if self.recursive() and (self.results is None or self.search_text != event.value.strip()):
      self.start_search()
    self.query_one("#dir").focus()

  @on(Checkbox.Changed, "#drecursive")
  def recursive_changed(self, event):
    if event.value:
      self.start_search()
    else:
      self.stop_search()
      self.results = None
      self.populate()

  search_text = ""

  def start_search(self):
    """Search the subdirectories for the names in the search field, in the background: the matches come in batches
    (each a round trip on a cluster), the table and the status follow; a new search, Esc or leaving stops it"""
    if self.search_timer is not None:
      self.search_timer.stop()
      self.search_timer = None
    self.stop_search()
    text = self.query_one("#dfilter", Input).value.strip()
    self.search_text = text
    if not text or not self.recursive():
      self.results = None
      self.populate()
      return
    self.results, self.searching = [], (0, 1)
    self.populate()
    gen, root = self.search_gen, self.path
    def found(matches, done, left):
      self.app.call_from_thread(self.search_found, gen, matches, done, left)
    def body():
      search(root, text, found, SEARCH_LIMIT)
      self.app.call_from_thread(self.search_done, gen)
    start_worker(self, body, "search", exclusive=True, name=f"searching {root}")

  def search_found(self, gen, matches, done, left):
    if gen != self.search_gen or self.results is None:
      return
    table = self.query_one("#dir", ClickTable)
    for e in matches:
      e["name"] = os.path.relpath(e["name"], self.path)
      self.results.append(e)
      if self.show_hidden or not os.path.basename(e["name"]).startswith("."):
        self.add_entry(table, e) # (added as they come; sorted when the search is done)
    self.searching = (done, left)
    self.show_status()

  def search_done(self, gen):
    if gen != self.search_gen:
      return
    self.searching = None
    self.populate()

  def stop_search(self):
    """The search running stops (its results stay)"""
    self.search_gen += 1
    for w in list(self.app.workers):
      if w.node is self and w.group == "search":
        getattr(w, "abort", None) and w.abort.set()
        w.cancel()
    self.searching = None

  def on_unmount(self):
    self.stop_search()

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
