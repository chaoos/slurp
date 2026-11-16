"""The quota window, and the scratch window: the oldest files of the user in $SCRATCH, which the cluster may delete
after some time without asking, and touching them"""

import re
import time
from datetime import datetime

import humanize
from rich.progress_bar import ProgressBar as RichProgressBar
from rich.table import Table
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ...cluster.remote import in_background
from ..widgets import ClickTable
from .base import SplitScreen
from ...cluster.storage import oldest_files, quota_info, scratch_dir, touch_files
from .dialogs import ConfirmScreen

def not_connected(cluster):
  """Why the cluster of a tab cannot be asked now (None: it can)"""
  c = cluster.connection
  if not cluster.destination:
    return None if cluster.name else "no tab"
  if c is None or not c.ready:
    return f"Not connected to {cluster.destination}: Connect in the overview or the job list, then Reload."
  return None

def usage_bar(used, limit):
  """[bar, 'used of limit (x%)'] with a colour by how full it is"""
  if not limit:
    return "", Text("no limit", style="dim")
  share = used / limit
  style = "red" if share >= 0.9 else "yellow" if share >= 0.75 else "green"
  return RichProgressBar(total=limit, completed=min(used, limit), width=16, complete_style=style, finished_style="red"), \
         Text(f"{share:.0%}", style=style)

class QuotaScreen(ModalScreen):
  """Quotas of the user and the user's groups on the filesystems of the cluster of a tab (a window; loads in the
  background)"""
  DEFAULT_CSS = """
  QuotaScreen { align: center middle; }
  #quota-dialog { width: 130; max-width: 98%; height: 80%; border: thick $accent; background: $surface; padding: 0 1; }
  #quota-title { text-style: bold; height: auto; margin-bottom: 1; }
  #quota-body { height: 1fr; }
  #quota-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #quota-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("r", "reload", "Reload")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster, self.info, self.fetching = cluster, None, False

  def compose(self):
    with Vertical(id="quota-dialog"):
      yield Static(id="quota-title")
      with VerticalScroll(id="quota-body"):
        yield Static(id="quota-info")
      with Horizontal(id="quota-buttons"):
        yield Button("Reload", id="quota-reload", variant="primary", compact=True)
        yield Button("Close", id="quota-close", compact=True)

  def on_mount(self):
    self.action_reload()

  @on(Button.Pressed, "#quota-close")
  def action_close(self):
    self.dismiss()

  def set_title(self):
    state = "loading …" if self.fetching else f"as of {self.info['time']:%H:%M:%S}" if self.info else ""
    self.query_one("#quota-title", Static).update(Text(f"Quota on {self.cluster.name or '-'} · {state}"))

  @on(Button.Pressed, "#quota-reload")
  def action_reload(self):
    if self.fetching:
      return
    why = not_connected(self.cluster)
    if why:
      self.query_one("#quota-info", Static).update(Text(why, style="bold yellow"))
      self.set_title()
      return
    self.fetching = True
    self.set_title()
    self.query_one("#quota-info", Static).update(Text("asking the filesystems (in the background) …", style="dim"))
    def loaded(info):
      self.fetching = False
      if info is not None:
        self.info = info
      self.show()
    in_background(self, quota_info, loaded, group="quota", connection=self.cluster.connection if self.cluster.destination else None)

  def show(self):
    self.set_title()
    info = self.info
    if info is None:
      return
    table = Table(box=None, padding=(0, 1), header_style="bold", expand=False)
    for col, justify in (("filesystem", "left"), ("of", "left"), ("space used", "right"), ("limit", "right"), ("", "left"),
                         ("", "right"), ("files used", "right"), ("limit", "right"), ("", "left"), ("", "right")):
      table.add_column(col, justify=justify)
    for e in info["entries"]:
      kb_limit = e["kb_hard"] or e["kb_soft"]
      files_limit = e["files_hard"] or e["files_soft"]
      space_bar, space_pct = usage_bar(e["kb"], kb_limit)
      files_bar, files_pct = usage_bar(e["files"], files_limit)
      soft = lambda soft, hard, fmt: f" (soft {fmt(soft)})" if soft and hard and soft != hard else ""
      gb = lambda kb: humanize.naturalsize(kb * 1024, binary=True)
      table.add_row(Text(e["filesystem"], style="bold red" if e["over"] else ""), f"{e['kind']} {e['name']}",
                    gb(e["kb"]), (gb(kb_limit) if kb_limit else "-") + soft(e["kb_soft"], e["kb_hard"], gb), space_bar, space_pct,
                    f"{e['files']:,}", (f"{files_limit:,}" if files_limit else "-") + soft(e["files_soft"], e["files_hard"], lambda n: f"{n:,}"),
                    files_bar, files_pct)
    parts = [table] if info["entries"] else []
    if info["dirs"]:
      parts.append(Text("\n" + "  ".join(f"${k} = {v}" for k, v in info["dirs"].items()), style="dim"))
    if info["site"]:
      parts.append(Text("\nOutput of the site's quota command:\n", style="bold"))
      parts.append(Text(info["site"]))
    for err in info["errors"]:
      parts.append(Text(err, style="red"))
    from rich.console import Group
    self.shown = Group(*parts)
    self.query_one("#quota-info", Static).update(self.shown)


DAY = 86400

class ScratchScreen(SplitScreen):
  """The oldest files of the user in the scratch directory (searched in the background), to see what an automatic
  cleanup of the cluster may delete soon, and to touch them"""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back"), Binding("space", "toggle", "Select"),
              Binding("A", "select_all", "Select all"), Binding("r", "search", "Search again")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster = cluster
    self.result, self.worker, self.selected = None, None, set()
    self.crumbs = ["old files in scratch"]

  def compose_left(self):
    with Horizontal(id="scratch-bar"):
      yield Label("directory")
      yield Input(id="scratch-dir", placeholder="$SCRATCH", compact=True)
      yield Label("by")
      yield Select([("last access", "atime"), ("last modification", "mtime")], value="atime", allow_blank=False, id="scratch-by", compact=True)
      yield Label("older than")
      yield Input("30", id="scratch-days", type="integer", compact=True)
      yield Label("days")
      yield Button("Search", id="scratch-search", variant="primary", compact=True)
    with Horizontal(id="scratch-actions"):
      yield Input(id="scratch-exclude", placeholder="exclude paths matching (regex, e.g. /keep/|\\.git/)",
                  tooltip="files whose path matches are left out of the list and of every touch (Enter: apply)", compact=True)
      yield Button("Touch selected", id="touch-selected", compact=True)
      yield Button("Touch all listed", id="touch-listed", compact=True)
      yield Button("Touch all my files there", id="touch-all", variant="warning", compact=True)
    yield Static(id="scratch-status")
    yield ClickTable(id="scratch-files", cursor_type="row", zebra_stripes=True)

  def on_mount(self):
    self.sub_title = f"old files in scratch · {self.cluster.name}"
    table = self.query_one("#scratch-files", ClickTable)
    for key, label in (("mark", "✓"), ("atime", "Last access"), ("mtime", "Last modified"), ("age", "Age"),
                       ("size", "Size"), ("path", "Path")):
      table.add_column(label, key=key)
    self.status("looking for the scratch directory …")
    in_background(self, scratch_dir, self.got_dir, group="scratch-dir", connection=self.connection())

  def connection(self):
    return self.cluster.connection if self.cluster.destination else None

  def got_dir(self, directory):
    self.query_one("#scratch-dir", Input).value = directory or ""
    if directory:
      self.action_search()
    else:
      self.status("$SCRATCH is not set on this cluster: type the directory and press Search")

  def status(self, text, style=""):
    self.query_one("#scratch-status", Static).update(Text(text, style=style))

  def days(self):
    try:
      return max(0, int(self.query_one("#scratch-days", Input).value))
    except ValueError:
      return 30

  @on(Button.Pressed, "#scratch-search")
  def search_pressed(self, event):
    event.stop()
    if self.worker is not None:
      self.worker.abort.set()
      self.worker = None
      self.query_one("#scratch-search", Button).label = "Search"
      self.status("search aborted", "yellow")
    else:
      self.action_search()

  def action_search(self):
    directory = self.query_one("#scratch-dir", Input).value.strip()
    if not directory or self.worker is not None:
      return
    by = self.query_one("#scratch-by", Select).value
    started = time.monotonic()
    self.status(f"searching the files of the user in {directory} (in the background, it can take minutes) …", "yellow")
    self.query_one("#scratch-search", Button).label = "Abort"
    def found(result):
      if self.worker is not worker:
        return
      self.worker = None
      self.query_one("#scratch-search", Button).label = "Search"
      if result is not None:
        result["seconds"] = time.monotonic() - started
        self.result = result
        self.selected.clear()
        self.show()
    worker = self.worker = in_background(self, lambda: oldest_files(directory, by), found, group="scratch-search",
                                         connection=self.connection())

  def exclude(self):
    """The compiled exclude regex (None: none or invalid)"""
    text = self.query_one("#scratch-exclude", Input).value.strip()
    if not text:
      return None
    try:
      return re.compile(text)
    except re.error as e:
      self.notify(f"invalid exclude regex: {e}", severity="error")
      return None

  def visible(self):
    """Indices of the listed files that are not excluded"""
    if not self.result:
      return []
    rx = self.exclude()
    return [i for i, f in enumerate(self.result["files"]) if rx is None or not rx.search(f["path"])]

  @on(Input.Submitted, "#scratch-exclude")
  def exclude_changed(self, event):
    self.selected &= set(self.visible())
    self.show()

  @on(Input.Submitted, "#scratch-days")
  @on(Select.Changed, "#scratch-by")
  def options_changed(self, event):
    if isinstance(event, Select.Changed):
      if self.result is not None and self.result["by"] != event.value:
        self.action_search()
    else:
      self.show()

  def show(self):
    r = self.result
    table = self.query_one("#scratch-files", ClickTable)
    table.clear()
    if r is None:
      return
    if r["error"]:
      self.status(f"search failed: {r['error']}", "red")
      return
    now, limit = time.time(), self.days() * DAY
    old = near = 0
    shown = self.visible()
    for i in shown:
      f = r["files"][i]
      age = now - f[r["by"]]
      is_old, is_near = age > limit, limit * 0.8 < age <= limit # near: within the last fifth of the limit
      old += is_old
      near += is_near
      style = "red" if is_old else "yellow" if is_near else ""
      table.add_row("✓" if i in self.selected else "", datetime.fromtimestamp(f["atime"]).strftime("%Y-%m-%d %H:%M"),
                    datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M"), Text(f"{age / DAY:.0f} d", style=style),
                    humanize.naturalsize(f["size"]), Text(f["path"], style=style), key=str(i))
    n, size = r["total"]
    what = "access" if r["by"] == "atime" else "modification"
    listed = f"the {len(r['files'])} with the oldest {what} are listed" if n > len(r["files"]) else "all listed"
    excluded = len(r["files"]) - len(shown)
    self.status(f"{n} files ({humanize.naturalsize(size)}) of the user in {r['directory']}, {listed}"
                + (f", {excluded} of them excluded" if excluded else "") + "; "
                f"{old}{'+' if n > len(r['files']) and old == len(r['files']) else ''} not {'used' if r['by'] == 'atime' else 'changed'} "
                f"for more than {self.days()} days (red), which an automatic cleanup may delete, {near} close to it (yellow) · "
                f"searched in {r.get('seconds', 0):.0f} s at {r['time']:%H:%M}", "bold red" if old else "yellow" if near else "green")

  @on(DataTable.RowSelected, "#scratch-files")
  def row_selected(self, event):
    event.stop()
    self.query_one("#scratch-files", ClickTable).take_clicked_column()
    self.toggle(int(event.row_key.value))

  def toggle(self, i):
    self.selected.symmetric_difference_update({i})
    self.query_one("#scratch-files", ClickTable).update_cell(str(i), "mark", "✓" if i in self.selected else "")

  def action_toggle(self):
    table = self.query_one("#scratch-files", ClickTable)
    if table.row_count:
      self.toggle(int(table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value))

  def action_select_all(self):
    if self.result:
      everything = set(self.visible())
      self.selected = set() if self.selected == everything else everything
      self.show()

  @on(Button.Pressed, "#touch-selected, #touch-listed, #touch-all")
  def touch_pressed(self, event):
    event.stop()
    r = self.result
    if r is None or r["error"]:
      self.notify("search first", severity="warning")
      return
    which = event.button.id
    if which == "touch-selected" and not self.selected:
      self.notify("select files first (click a row or Space; A selects all)", severity="warning")
      return
    shown = self.visible()
    paths = [] if which == "touch-all" else [r["files"][i]["path"] for i in sorted(self.selected & set(shown))] \
            if which == "touch-selected" else [r["files"][i]["path"] for i in shown]
    what = "atime" if r["by"] == "atime" else "both"
    exclude = self.query_one("#scratch-exclude", Input).value.strip() if self.exclude() else ""
    count = f"all {r['total'][0]} files of the user in {r['directory']}" + (f" except those matching {exclude}" if exclude else "") \
            if which == "touch-all" else f"{len(paths)} files"
    message = (f"Touch {count}: set their {'access time' if what == 'atime' else 'access and modification times'} to now, so "
               "that an automatic cleanup that looks at it does not delete them yet.\n\nNote: many sites do not allow keeping "
               "files in scratch this way (their usage policy may count it as circumventing the cleanup); scratch is not "
               "meant for keeping data. Move what you need to keep to a project or home filesystem.")
    self.app.push_screen(ConfirmScreen("Touch files?", message, "Touch", variant="warning"),
                         lambda ok: self.touch(paths, what, r["directory"] if which == "touch-all" else None, exclude) if ok else None)

  def touch(self, paths, what, directory, exclude=""):
    self.status("touching …", "yellow")
    def done(result):
      if result is None:
        return
      count, error = result
      self.notify(f"touched {'all files' if count is None else f'{count} files'}" + (f" (errors: {error})" if error else ""),
                  severity="warning" if error else "information")
      self.action_search()
    in_background(self, lambda: touch_files(paths, what, directory, exclude), done, group="touch", connection=self.connection())

  def action_back(self):
    self.pop_back()
