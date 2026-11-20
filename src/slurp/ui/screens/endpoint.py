"""The window of an endpoint that is no tab (S3, SMB, WebDAV, Globus), as the connection window of a tab: where it
points, its settings (its secrets: only whether the keyring has them), its connection (its state, its last test, what
reaches it), the transfers that use it, and the course of its tests; Test, Edit…, Disconnect, Copy address"""
import threading
import time
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ...transfer import endpoints as E, model as M, rclone as RC, secrets as S
from .. import icons

# (the settings of a kind, as its form names them; the others: their names)
LABELS = {
  "s3": [("provider", "Provider"), ("endpoint", "Endpoint URL"), ("region", "Region"), ("bucket", "Bucket"), ("path", "Its folder")],
  "smb": [("host", "Server"), ("port", "Port"), ("share", "Share"), ("user", "User"), ("domain", "Domain"), ("path", "Its folder")],
  "webdav": [("url", "URL"), ("vendor", "Vendor"), ("user", "User"), ("path", "Its folder")],
  "globus": [("collection_name", "Collection"), ("collection", "Its id"), ("path", "Its folder")],
}
SECRET_LABELS = {"access_key": "access key", "secret_key": "secret key", "session_token": "session token", "password": "password",
                 "bearer_token": "token", "refresh_token": "Globus login"}
LEVELS = {"connected": "good", "connecting": "warn", "needs you": "warn", "not connected": "warn"} # (the others: bad)
STYLES = {"good": "green", "warn": "yellow", "bad": "red"}

def level_of(state):
  return LEVELS.get(state, "bad")

def address_of(d):
  """Where it is, to copy: s3://bucket/…, //server/share/…, the URL; a Globus collection: its id and folder"""
  if d.kind in RC.KINDS:
    return RC.address(d, d.settings.get("path") or "/")
  if d.kind == "globus":
    return f"globus:{d.settings.get('collection', '')}{d.settings.get('path') or '/~/'}"
  return ""

class EndpointScreen(ModalScreen):
  """An endpoint (no tab): its window, refreshed every second"""
  DEFAULT_CSS = """
  EndpointScreen { align: center middle; }
  #epd-dialog { width: 110; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #epd-dialog.-good { border: thick $success; }
  #epd-dialog.-warn { border: thick $warning; }
  #epd-dialog.-bad { border: thick $error; }
  #epd-title { text-style: bold; height: 1; margin-bottom: 1; }
  #epd-body { height: 1fr; }
  .epd-head { text-style: bold underline; height: auto; }
  .epd-part { height: auto; margin-bottom: 1; }
  #epd-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #epd-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("t", "test", "Test"), Binding("e", "edit", "Edit")]

  def __init__(self, eid):
    super().__init__()
    self.eid = eid
    self.rclone = None # (rclone's version here: asked once, when it opens)
    self.server = None # (what its server says of itself: [(label, value)]; None: being asked)
    self.server_for = None # (the last test it was asked after)

  def compose(self) -> ComposeResult:
    with Vertical(id="epd-dialog"):
      yield Static(id="epd-title")
      with VerticalScroll(id="epd-body"):
        for part, head in (("settings", "Settings"), ("server", "Server"), ("connection", "Connection"), ("used", "Used by"),
                           ("course", "Course of its tests")):
          yield Static(head, classes="epd-head")
          yield Static(id=f"epd-{part}", classes="epd-part")
      with Horizontal(id="epd-buttons"):
        yield Button("Copy address", id="epd-copy", compact=True, tooltip="where it is: s3://…, //server/share, the URL, the collection")
        yield Button(icons.label("check", "Test"), id="epd-test", variant="primary", compact=True, tooltip="does it answer? (t)")
        yield Button(icons.label("edit", "Edit…"), id="epd-edit", compact=True, tooltip="its settings and secrets (e)")
        yield Button(icons.label("disconnect", "Disconnect"), id="epd-disconnect", variant="error", compact=True)
        yield Button("Close", id="epd-close", compact=True)

  def on_mount(self):
    e = self.endpoint()
    if e is not None and e.kind in RC.KINDS:
      self.rclone = RC.version() or ()
    self.ask_server()
    self.show()

  def ask_server(self):
    """What its server says of itself (software, version, protocol, space), in a thread; again after each test"""
    e = self.endpoint()
    if e is None or e.definition is None:
      return
    self.server, self.server_for = None, self.manager.endpoint_tested.get(self.eid)
    from ...transfer import probe
    def work():
      try:
        rows = probe.server_info(e.definition)
      except Exception as err:
        rows = [("Server", f"not asked: {err}")]
      self.app.call_from_thread(self.got_server, rows)
    threading.Thread(target=work, daemon=True, name=f"server info {e.name}").start()

  def got_server(self, rows):
    if self.is_attached:
      self.server = rows
      self.show()
    self.set_interval(1, self.show)

  @property
  def manager(self):
    return self.app.transfers

  def endpoint(self):
    m = self.manager
    return next((e for e in E.listing(self.app, m.endpoint_store, m.endpoint_status) if e.id == self.eid), None)

  def show(self):
    e = self.endpoint()
    if e is None or e.definition is None: # (deleted meanwhile)
      self.dismiss()
      return
    d = e.definition
    level = level_of(e.state)
    title = Text(f"Endpoint {e.name} · {E.KINDS.get(e.kind, e.kind)}" + (f" · {e.where}" if e.where else ""))
    title.append(f" · ● {e.state}", style=STYLES[level])
    self.query_one("#epd-title", Static).update(title)
    dialog = self.query_one("#epd-dialog")
    for other in STYLES:
      dialog.set_class(other == level, f"-{other}")
    self.query_one("#epd-settings", Static).update(self.settings_grid(d))
    if self.server is not None and self.manager.endpoint_tested.get(self.eid) != self.server_for: # (tested again: asked again)
      self.ask_server()
    self.query_one("#epd-server", Static).update(self.server_grid())
    self.query_one("#epd-connection", Static).update(self.connection_grid(e))
    self.query_one("#epd-used", Static).update(self.used_text())
    self.query_one("#epd-course", Static).update(self.course_grid())
    self.query_one("#epd-disconnect", Button).disabled = not e.connected

  @staticmethod
  def grid():
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bold", no_wrap=True)
    g.add_column()
    return g

  def settings_grid(self, d):
    g = self.grid()
    g.add_row("Kind", E.KINDS.get(d.kind, d.kind))
    address = address_of(d)
    if address:
      g.add_row("Address", address)
    known = LABELS.get(d.kind, [])
    for key, label in known + [(k, k) for k in d.settings if k not in dict(known) and k not in ("needs_consent",)]:
      value = d.settings.get(key)
      if value not in (None, ""):
        g.add_row(label, str(value))
    if d.kind == "globus" and d.settings.get("needs_consent"):
      g.add_row("Consent", "its data needs your consent (asked at the Globus login)")
    if d.secrets: # (only whether the keyring has them: never a value)
      have = Text()
      for i, name in enumerate(d.secrets):
        there = bool(S.get(S.user(d.id, name))) if S.available()[0] else False
        have.append(("" if not i else ", ") + SECRET_LABELS.get(name, name) + " ")
        have.append("✓" if there else "✗ missing", style="green" if there else "red")
      have.append(" (in the keyring)", style="dim")
      g.add_row("Secrets", have)
    return g

  def server_grid(self):
    if self.server is None:
      return Text("asking it …", style="yellow")
    if not self.server:
      return Text("it says nothing of itself", style="dim")
    g = self.grid()
    for label, value in self.server:
      g.add_row(label, str(value))
    return g

  def connection_grid(self, e):
    from .transfers import endpoint_state_text
    g = self.grid()
    g.add_row("State", endpoint_state_text(e))
    tested = self.manager.endpoint_tested.get(e.id)
    if tested:
      when, took = tested
      g.add_row("Last test", f"{datetime.fromtimestamp(when):%H:%M:%S} ({int(time.time() - when) // 60} min ago), took {took:.1f} s")
    else:
      g.add_row("Last test", Text("not tested in this session yet (Test, t)", style="dim"))
    if e.kind in RC.KINDS:
      have = self.rclone
      g.add_row("Reached by", f"rclone {'.'.join(map(str, have))} on this machine" if have else Text("rclone: not installed here", style="red"))
      g.add_row("", Text(f"as the remote {RC.remote_name(e.id)}: its settings and secrets in rclone's environment, no config file, "
                         "nothing on a command line", style="dim"))
      g.add_row("Keyring", S.available()[1] or "available")
    elif e.kind == "globus":
      from ...transfer import globus as G
      ok, why = G.available()
      g.add_row("Reached by", "Globus (its service moves the data; this machine only asks it)" if ok else Text(why, style="red"))
      g.add_row("Globus login", Text("logged in", style="green") if ok and G.logged_in() else Text("not logged in (Test logs in)", style="yellow"))
      cid = e.definition.settings.get("collection")
      if cid and ok and G.logged_in() and e.definition.settings.get("needs_consent"):
        g.add_row("Consent", "given" if cid in G.consented() else Text("not given yet: Test logs in again, with it", style="yellow"))
    return g

  def used_text(self):
    users = [t for t in self.manager.transfers if self.eid in (E.ref_id(t.source), E.ref_id(t.dest))]
    if not users:
      return Text("no transfer uses it", style="dim")
    g = self.grid()
    for t in users:
      side = "from it" if E.ref_id(t.source) == self.eid else "to it"
      g.add_row(t.name, Text.assemble(f"{side} · ", M.STATES.get(t.state, t.state)))
    return g

  def course_grid(self):
    events = self.manager.endpoint_events.get(self.eid, [])
    if not events:
      return Text("no tests yet in this session", style="dim")
    g = Table.grid(padding=(0, 2))
    g.add_column(no_wrap=True, style="dim")
    g.add_column()
    for when, text, state in events[-30:]:
      g.add_row(f"{datetime.fromtimestamp(when):%H:%M:%S}", Text(text, style=STYLES[level_of(state)] if state != "connecting" else ""))
    return g

  # ---- the buttons
  @on(Button.Pressed, "#epd-test")
  def action_test(self):
    e = self.endpoint()
    if e is not None:
      self.app.test_endpoint(e)
      self.show()

  @on(Button.Pressed, "#epd-edit")
  def action_edit(self):
    e = self.endpoint()
    if e is not None:
      self.app.edit_endpoint(e, e.kind)

  @on(Button.Pressed, "#epd-disconnect")
  def disconnect_pressed(self):
    if self.endpoint() is not None:
      self.manager.endpoint_status[self.eid] = ("not connected", "")
      self.manager.endpoint_event(self.eid, "disconnected (tested again when used)", "not connected")
      self.show()

  @on(Button.Pressed, "#epd-copy")
  def copy_pressed(self):
    e = self.endpoint()
    if e is not None and address_of(e.definition):
      from .dialogs import copy_text
      copy_text(self.app, address_of(e.definition), "the address")

  @on(Button.Pressed, "#epd-close")
  def action_close(self):
    self.dismiss()
