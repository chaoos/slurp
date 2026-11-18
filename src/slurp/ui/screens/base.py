"""The frame of every window: the header (with the menu), tabs, subtabs, the window, the login pane, the footer"""


from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static
from .. import icons


from ..widgets import ConnectionStatus
from ..terminal import Splitter, TerminalPane
from .dialogs import AddClusterScreen

class SplitScreen(Screen):
  """Window with its content on the left and, while a shell is open, the terminal pane on the right"""
  SHOW_BACK = False
  SHOW_NOTABS = False # the hint shown when no tab is open (the job list)

  def compose(self) -> ComposeResult:
    with Horizontal(id="topbar"): # (the top line: the menu, the title of slurp and of the window, its help)
      yield Button("☰ Menu", id="menu", compact=True,
                   tooltip="job list, health, quota, shell, drive, interactive job, categories, web, options, … (F10)")
      if self.SHOW_BACK:
        yield Button("←", id="back", compact=True, tooltip="Back")
      yield Header()
      yield Button("", id="update", compact=True, tooltip="a newer slurp is there: how to update")
      yield Button("", id="refresh-timer", compact=True,
                   tooltip="the next automatic refresh of this view (a click: automatic refreshing off, or on again)")
      yield Button("", id="web-timer", compact=True,
                   tooltip="a web link ends soon: when it takes no more logins, or its relay ends (click: the link)")
      yield Button("?", id="view-help", compact=True, tooltip="about this window: what it shows, what it can do, its keys (?)")
    with Horizontal(id="cluster-tabs"):
      if self.app.transfers_tab_wanted(): # (the Transfers tab: there while transfers or endpoints exist)
        yield Button(self.app.transfers_tab_label(), id="transfers-tab", classes="transfers-tab", compact=True,
                     variant="primary" if self.app.in_transfers else "default", tooltip="the transfers of data between the tabs")
      for index, cluster in enumerate(self.app.clusters):
        variant = "primary" if index == self.app.cluster_index and not self.app.in_transfers else "default"
        yield Button(self.app.tab_label(cluster), id=f"cluster-tab-{index}", classes="cluster-tab", variant=variant, compact=True)
        yield Button("✕", id=f"cluster-close-{index}", classes="cluster-close", compact=True, tooltip="close the tab")
      yield Button("+", id="cluster-add", tooltip="Add a local or SSH cluster", compact=True)
      yield Button("? slurp", id="about", compact=True, tooltip="about slurp: how it works, its version, the machine it runs on")
    with Horizontal(id="view-tabs"):
      yield Button(icons.label("cluster", "Overview"), id="view-overview", classes="view-tab", compact=True,
                   tooltip="the cluster at a glance: the first subtab of every tab, always there")
      yield Button(icons.label("jobs", "Jobs"), id="view-jobs", classes="view-tab", compact=True, tooltip="the job list")
      yield Button("✕", id="view-close-jobs", classes="view-close", compact=True, tooltip="close the job list (Ctrl-W)")
    if self.SHOW_NOTABS:
      yield Static("No tabs. The + button above adds a connection (blank for this machine, or an SSH destination).", id="notabs")
    with Horizontal(classes="split"):
      with Vertical(classes="left"):
        yield from self.compose_left()
      yield Splitter()
      yield TerminalPane()
    with Horizontal(id="bottombar"):
      yield Footer()
      yield Static(id="loadstatus")
      yield ConnectionStatus(id="connstatus")

  def on_mount(self):
    self.call_after_refresh(self.app.refresh_status)

  @on(Button.Pressed)
  def back_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "cluster-add":
      event.stop()
      self.app.push_screen(AddClusterScreen(), self.app.add_cluster)
      return
    if event.button.id == "transfers-tab":
      event.stop()
      self.app.show_transfers()
      return
    if event.button.id in ("view-transfers", "view-endpoints") or (event.button.id or "").startswith(("xfer-tab-", "xfer-close-")):
      event.stop()
      self.app.transfer_subtab_pressed(event.button.id)
      return
    if event.button.id and event.button.id.startswith("cluster-tab-"):
      event.stop()
      self.app.select_cluster(int(event.button.id.rsplit("-", 1)[1]))
      return
    if event.button.id and event.button.id.startswith("cluster-close-"):
      event.stop()
      self.app.confirm_close_cluster(int(event.button.id.rsplit("-", 1)[1]))
      return
    if event.button.id == "about":
      event.stop()
      self.app.action_about()
      return
    if event.button.id == "refresh-timer":
      event.stop()
      self.app.toggle_auto_refresh()
      return
    if event.button.id == "update":
      event.stop()
      self.app.show_update()
      return
    if event.button.id == "web-timer":
      event.stop()
      self.app.show_web_deadline()
      return
    if event.button.id == "view-help":
      event.stop()
      self.app.action_view_help()
      return
    if event.button.id == "view-overview":
      event.stop()
      self.app.show_overview()
      return
    if event.button.id == "view-jobs":
      event.stop()
      self.app.show_view(None)
      return
    if event.button.id == "view-close-jobs":
      event.stop()
      self.app.close_jobs()
      return
    if event.button.id and event.button.id.startswith(("view-tab-", "view-close-")):
      event.stop()
      vid = int(event.button.id.rsplit("-", 1)[1])
      view = next((v for v in self.app.cluster.views if v.id == vid), None)
      if view is not None:
        (self.app.show_view if event.button.id.startswith("view-tab-") else self.app.request_close_view)(view)
      return
    if event.button.id != "back":
      return
    event.stop()
    self.action_back()

  def compose_left(self):
    return []

  def pop_back(self):
    if len(self.app.screen_stack) <= 1: # the first window of a subtab: back to the job list, or the overview (the subtab stays)
      self.app.show_home(views=False)
      return
    self.app.pop_screen()
    # pop_screen completes after this key action; queue a full redraw behind it.
    self.app.call_later(self.app.refresh, layout=True)
