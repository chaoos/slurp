"""The quota window"""

import os
import time
from datetime import datetime

import humanize
from rich.progress_bar import ProgressBar as RichProgressBar
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ..state import *
from ..remote import *
from ..storage import *
from ..widgets import *
from .base import *
from .dialogs import ConfirmScreen

def not_connected(cluster):
  """Why the cluster of a tab cannot be asked now (None: it can)"""
  c = cluster.connection
  if not cluster.destination:
    return None if cluster.name else "no tab"
  if c is None or not c.ready:
    return f"No connection to {cluster.destination}. Reconnect (in the connection window, c) and Reload."
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
  """Quotas of the user and the user's groups on the filesystems of the cluster of a tab"""
  DEFAULT_CSS = """
  QuotaScreen { align: center middle; }
  #quota-dialog { width: 140; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #quota-title { text-style: bold; height: auto; margin-bottom: 1; }
  #quota-body { height: 1fr; }
  #quota-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #quota-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("r", "reload", "Reload")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster, self.info, self.loading = cluster, None, False

  def compose(self) -> ComposeResult:
    with Vertical(id="quota-dialog"):
      yield Static(id="quota-title")
      with VerticalScroll(id="quota-body"):
        yield Static(id="quota-info")
      with Horizontal(id="quota-buttons"):
        yield Button("Reload", id="quota-reload", variant="primary")
        yield Button("Close", id="quota-close")

  def on_mount(self):
    self.action_reload()

  def set_title(self):
    state = "loading …" if self.loading else f"as of {self.info['time']:%H:%M:%S}" if self.info else ""
    self.query_one("#quota-title", Static).update(Text(f"Quota on {self.cluster.name or '-'} · {state}"))

  @on(Button.Pressed, "#quota-reload")
  def action_reload(self):
    if self.loading:
      return
    why = not_connected(self.cluster)
    if why:
      self.query_one("#quota-info", Static).update(Text(why, style="bold yellow"))
      self.set_title()
      return
    self.loading = True
    self.set_title()
    def loaded(info):
      self.loading = False
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

  @on(Button.Pressed, "#quota-close")
  def action_close(self):
    self.dismiss()
