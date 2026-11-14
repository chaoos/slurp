"""Shells and drives for the user's own terminal: the choice of the Shell buttons, the setup panel of a link and its
view (commands, password, state)"""

import time
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Select, Static, Switch, TextArea

from .. import share
from ..config import load_config, update_config
from .dialogs import ConfirmScreen, copy_text

PUBLIC_WARNING = (
  "This link goes through {relay}, a PUBLIC relay run by a third party.\n"
  "• The relay cannot read the session: it stays SSH encrypted from your terminal to slurp. Check the host key "
  "fingerprint below when your ssh asks on the first connect.\n"
  "• Anyone who has the command and the password gets {what} as you on the cluster: do not share or post them, "
  "prefer one use and a short expiry.\n"
  "• The relay sees that a connection happens, when, and how much data flows; it may go offline at any time.\n"
  "• Your site's rules may not allow access through third-party relays.\n"
  "Your own relay avoids this: any SSH server you can log in to with a key, set below or as \"link_relay\" in "
  "~/.config/slurp/config.json.")
OWN_NOTE = ("Own relay {relay}: an OpenSSH jump host. slurp logs in to it with your keys (no prompts) and asks it "
            "for a free port; your terminal jumps through it to slurp.")

def relay_setting():
  return load_config().get("link_relay") or share.DEFAULT_RELAY

def warning_text(relay, kind):
  what = ("a shell" if kind == "shell" else "this slurp itself, with all its tabs, shells and drives" if kind == "web"
          else "your files (read and write, unless read only)")
  if share.is_public(relay):
    return Text(PUBLIC_WARNING.format(relay=share.parse_relay(relay)[1], what=what), style="yellow")
  return Text(OWN_NOTE.format(relay=relay), style="dim")

class ShellChoiceScreen(ModalScreen):
  """Where a shell opens: in slurp, or in the user's own terminal (a link). Asked every time."""
  DEFAULT_CSS = """
  ShellChoiceScreen { align: center middle; }
  #choice-dialog { width: 76; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #choice-title { text-style: bold; margin-bottom: 1; }
  #choice-text { height: auto; margin-bottom: 1; }
  #choice-buttons { height: auto; align-horizontal: right; }
  #choice-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, title):
    super().__init__()
    self.title_text = title

  def compose(self) -> ComposeResult:
    missing = share.available()
    with Vertical(id="choice-dialog"):
      yield Static(self.title_text, id="choice-title", markup=False)
      yield Static("In slurp: a subtab with the shell, as before.\nFrom your own terminal: slurp makes a one-time "
                   "link (an ssh command and a password) for your terminal or ssh client on your own machine "
                   "(OpenSSH, PuTTY, Windows Terminal, …), wherever it is." + (f"\n\n(Own terminal: {missing})" if missing else ""),
                   id="choice-text", markup=False)
      with Horizontal(id="choice-buttons"):
        yield Button("Cancel", id="choice-cancel")
        yield Button("From your own terminal…", id="choice-own", disabled=bool(missing))
        yield Button("In slurp", id="choice-slurp", variant="primary")

  def on_mount(self):
    self.query_one("#choice-slurp", Button).focus()

  @on(Button.Pressed, "#choice-slurp")
  def slurp_pressed(self, event):
    event.stop()
    self.dismiss("slurp")

  @on(Button.Pressed, "#choice-own")
  def own_pressed(self, event):
    event.stop()
    self.dismiss("own")

  @on(Button.Pressed, "#choice-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

class LinkSetupScreen(ModalScreen):
  """The setup panel of a link: relay (with its warning), uses, expiry, password or key; for a drive read only.
  Dismissed with the link it started (or None)."""
  DEFAULT_CSS = """
  LinkSetupScreen { align: center middle; }
  #setup-dialog { width: 100; max-width: 98%; height: auto; max-height: 95%; border: thick $accent; background: $surface; padding: 0 1; }
  #setup-title { text-style: bold; margin: 1 0; }
  #setup-body { height: auto; max-height: 1fr; }
  .setup-row { height: auto; margin-bottom: 1; }
  .setup-row Label { width: 14; padding-top: 1; }
  .setup-row Input, .setup-row Select { width: 1fr; }
  #setup-warning { height: auto; margin-bottom: 1; padding: 0 1; border: round $warning; }
  #setup-key { height: 5; display: none; }
  #setup-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #setup-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, kind, title, tab, argv, path="", read_only_argv=None, web=None):
    """kind "shell", "drive" or "web"; argv: () -> argv of a session; a drive: read_only_argv the same read only
    (None: not possible here); a web link: web = (port, token) of the web view"""
    super().__init__()
    self.kind, self.what, self.tab, self.argv, self.path, self.read_only_argv = kind, title, tab, argv, path, read_only_argv
    self.web = web

  def compose(self) -> ComposeResult:
    with Vertical(id="setup-dialog"):
      yield Static({"shell": "Shell from your own terminal: ", "web": "Remote web access: "}.get(self.kind, "Drive for your own machine: ")
                   + self.what, id="setup-title", markup=False)
      with VerticalScroll(id="setup-body"):
        if self.kind == "web":
          yield Static("This slurp in your browser, from anywhere: an ssh tunnel through the relay (only to the web "
                       "view, nothing else), then a URL. How it goes, once the link is made:\n"
                       "  1. On your machine, paste the ssh command of the link into a terminal (cmd or PowerShell on "
                       "Windows) and give the link's password.\n"
                       "  2. It says that the tunnel is open and shows the URL. Keep that window open: it carries the "
                       "browser's connection (Ctrl-C there ends it).\n"
                       "  3. Open the URL in your browser. It shows this same slurp: one view is active at a time, so "
                       "while this one is, the browser shows a lock screen with Take over.", markup=False, classes="setup-row")
        if self.kind == "drive":
          yield Static(f"Your files on the cluster as a drive (SFTP): sshfs on Linux and macOS, SSHFS-Win on Windows, "
                       f"or WinSCP, Cyberduck, file managers. It starts in {self.path}.", markup=False, classes="setup-row")
          with Horizontal(classes="setup-row"):
            yield Label("Read only")
            yield Switch(value=False, id="setup-ro", disabled=self.read_only_argv is None,
                         tooltip=None if self.read_only_argv else "the cluster's sftp-server was not found: not possible")
        with Horizontal(classes="setup-row"):
          yield Label("Relay")
          yield Input(relay_setting(), id="setup-relay", tooltip="ssh-j.com (public) or your own SSH jump host (user@host)")
        yield Static(id="setup-warning")
        with Horizontal(classes="setup-row"):
          yield Label("Logins")
          yield Select([("once", 1), ("any number until it expires", 0)], value=1 if self.kind == "shell" else 0,
                       allow_blank=False, id="setup-uses")
        with Horizontal(classes="setup-row"):
          yield Label("Expires after")
          yield Select([(label, -1 if s is None else s) for label, s in share.EXPIRY_CHOICES], value=share.DEFAULT_EXPIRY,
                       allow_blank=False, id="setup-expiry")
        with Horizontal(classes="setup-row"):
          yield Label("Login with")
          with RadioSet(id="setup-auth"):
            yield RadioButton("a password (made by slurp)", value=True, id="setup-auth-password")
            yield RadioButton("my public key (paste it below)", id="setup-auth-key")
        yield TextArea("", id="setup-key")
      with Horizontal(id="setup-buttons"):
        yield Button("Cancel", id="setup-cancel")
        yield Button("Create link", id="setup-create", variant="primary")

  def on_mount(self):
    self.show_warning()
    self.query_one("#setup-create", Button).focus()

  @on(Input.Changed, "#setup-relay")
  def show_warning(self, event=None):
    relay = self.query_one("#setup-relay", Input).value.strip() or share.DEFAULT_RELAY
    widget = self.query_one("#setup-warning", Static)
    widget.update(warning_text(relay, self.kind))
    widget.styles.border = ("round", "yellow" if share.is_public(relay) else "gray")

  @on(RadioSet.Changed, "#setup-auth")
  def auth_changed(self, event):
    self.query_one("#setup-key", TextArea).display = event.pressed.id == "setup-auth-key"

  @on(Button.Pressed, "#setup-create")
  def create(self, event):
    event.stop()
    relay = self.query_one("#setup-relay", Input).value.strip() or share.DEFAULT_RELAY
    uses = self.query_one("#setup-uses", Select).value
    expiry = self.query_one("#setup-expiry", Select).value
    by_key = self.query_one("#setup-auth-key", RadioButton).value
    key = self.query_one("#setup-key", TextArea).text.strip()
    if by_key:
      try:
        share.asyncssh.import_public_key(key)
      except Exception:
        self.notify("paste your public key (one line, e.g. ssh-ed25519 AAAA… from ~/.ssh/id_ed25519.pub)", severity="error")
        return
    read_only = self.kind == "drive" and self.query_one("#setup-ro", Switch).value
    link = share.Link(self.kind, self.what, self.tab, self.read_only_argv if read_only else self.argv, relay=relay,
                      uses=uses or None, expiry=None if expiry == -1 else expiry, auth="key" if by_key else "password",
                      public_key=key if by_key else "", path=self.path, read_only=read_only,
                      web_port=self.web[0] if self.web else 0, web_token=self.web[1] if self.web else "")
    if relay != relay_setting():
      update_config(link_relay=relay) # (the relay is a setting: the next link uses it too)
    try:
      self.app.link_manager.create(link)
    except Exception as e:
      self.notify(f"cannot create the link: {e}", severity="error")
      return
    self.dismiss(link)

  @on(Button.Pressed, "#setup-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

def link_state(link, now=None):
  """The state of a link in words, for its view and the connection window"""
  now = now or time.time()
  relay = {"starting": ("starting", "yellow"), "connecting": (f"connecting to the relay {link.relay_host}", "yellow"),
           "ready": (f"ready, via {link.relay_host}" + (f" port {link.relay_port}" if link.relay_port else ""), "green"),
           "broken": (f"relay lost: {link.relay_error} (trying again)", "red"), "closed": ("closed", "dim")}[link.relay_state]
  if link.open(now):
    left = "once" if link.uses == 1 else f"{link.uses - link.logins} more logins" if link.uses else "any number of logins"
    if link.expires is not None:
      rest = int(link.expires - now)
      left += f", for {rest // 60}:{rest % 60:02d} more (until {datetime.fromtimestamp(link.expires):%H:%M})"
    else:
      left += ", until slurp quits"
    logins = Text(f"takes logins: {left}", style="green")
  else:
    logins = Text(f"takes no more logins: {link.why_closed()}", style="yellow" if link.live_sessions else "dim")
  return Text(relay[0], style=relay[1]), logins

class LinkScreen(ModalScreen):
  """A link: its state, the commands for the user's terminal with copy buttons, the password, the host key; New
  password, Extend, Revoke"""
  DEFAULT_CSS = """
  LinkScreen { align: center middle; }
  #lv-dialog { width: 110; max-width: 98%; height: 92%; border: thick $accent; background: $surface; padding: 0 1; }
  #lv-title { text-style: bold; margin: 1 0 0 0; }
  #lv-body { height: 1fr; }
  #lv-state { height: auto; margin: 1 0; }
  #lv-warning { height: auto; padding: 0 1; border: round $warning; margin-bottom: 1; }
  .lv-command { height: auto; margin-bottom: 1; }
  .lv-command-title { text-style: bold; }
  .lv-command-text { background: $boost; padding: 0 1; width: 1fr; }
  .lv-command-note { color: $text-muted; }
  .lv-command Horizontal { height: auto; }
  .lv-command Button { min-width: 10; margin-left: 1; }
  #lv-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #lv-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close")]

  def __init__(self, link):
    super().__init__()
    self.link = link
    self.shown_commands = None

  def compose(self) -> ComposeResult:
    link = self.link
    with Vertical(id="lv-dialog"):
      yield Static(f"Link {link.id}: {link.kind} · {link.title}" + (" · read only" if link.read_only else ""),
                   id="lv-title", markup=False)
      with VerticalScroll(id="lv-body"):
        yield Static(id="lv-state")
        if share.is_public(link.relay):
          yield Static(warning_text(link.relay, link.kind), id="lv-warning")
        yield Vertical(id="lv-commands")
      with Horizontal(id="lv-buttons"):
        if link.auth == "password":
          yield Button("New password", id="lv-password", tooltip="the old one stops working")
        yield Button("Extend 1 h", id="lv-extend")
        yield Button("Revoke", id="lv-revoke", variant="error", tooltip="no more logins, and its sessions end")
        yield Button("Close", id="lv-close", variant="primary", tooltip="the link stays (see the connection window)")

  def on_mount(self):
    self.refresh_view()
    self.set_interval(1, self.refresh_view)

  def refresh_view(self):
    if not self.is_mounted:
      return
    link = self.link
    relay, logins = link_state(link)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    grid.add_row("Relay", relay)
    grid.add_row("Logins", logins)
    if link.auth == "password":
      grid.add_row("Password", Text(link.password, style="bold"))
    else:
      grid.add_row("Login", "with your public key only")
    grid.add_row("Host key", f"{share.host_fingerprint()}  (your ssh shows it on the first connect, as {share.host_alias()})")
    live = link.live_sessions
    grid.add_row("Sessions", f"{len(live)} open, {link.logins} logins so far" + (f", {link.failures} wrong passwords" if link.failures else ""))
    self.query_one("#lv-state", Static).update(grid)
    if link.auth == "password":
      self.query_one("#lv-password", Button).display = link.open()
    self.query_one("#lv-revoke", Button).disabled = link.finished
    # the commands, once the relay is ready (an own relay gives its port only then)
    key = (link.ready(), link.relay_port, link.password)
    if key == self.shown_commands:
      return
    self.shown_commands = key
    box = self.query_one("#lv-commands", Vertical)
    box.remove_children()
    if not link.ready():
      box.mount(Static(Text("The commands appear when the relay is ready …", style="yellow")))
      return
    widgets = []
    if link.auth == "password":
      widgets.append(Vertical(Static("Password", classes="lv-command-title"),
                              Horizontal(Static(link.password, classes="lv-command-text", markup=False),
                                         Button("Copy", classes="lv-copy", name="password")),
                              classes="lv-command"))
    for i, (title, text, note) in enumerate(share.client_commands(link)):
      shown = text if len(text) <= 400 else f"{text[:90]} … ({len(text)} characters: use Copy)"
      widgets.append(Vertical(Static(title, classes="lv-command-title", markup=False),
                              Horizontal(Static(shown, classes="lv-command-text", markup=False),
                                         Button("Copy", classes="lv-copy", name=str(i))),
                              Static(note, classes="lv-command-note", markup=False), classes="lv-command"))
    box.mount_all(widgets)

  @on(Button.Pressed, ".lv-copy")
  def copy_pressed(self, event):
    event.stop()
    if event.button.name == "password":
      copy_text(self.app, self.link.password, f"the password of link {self.link.id}")
    else:
      title, text, _ = share.client_commands(self.link)[int(event.button.name)]
      copy_text(self.app, text, title)

  @on(Button.Pressed, "#lv-password")
  def new_password(self, event):
    event.stop()
    self.app.link_manager.new_password(self.link)
    self.refresh_view()

  @on(Button.Pressed, "#lv-extend")
  def extend(self, event):
    event.stop()
    self.app.link_manager.extend(self.link, 3600)
    self.refresh_view()

  @on(Button.Pressed, "#lv-revoke")
  def revoke(self, event):
    event.stop()
    def done(yes):
      if yes:
        self.app.link_manager.revoke(self.link)
        self.refresh_view()
    live = len(self.link.live_sessions)
    self.app.push_screen(ConfirmScreen(f"Revoke link {self.link.id}", "No more logins with it"
                                       + (f", and its {live} open session{'s' if live != 1 else ''} end." if live else "."), "Revoke"), done)

  @on(Button.Pressed, "#lv-close")
  def action_close(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss()

class DriveScreen(ModalScreen):
  """Drive: where the login directory of the tab goes. On this machine (where slurp runs: sshfs through the tab's
  session, at a path remembered per tab) or on the user's own machine (a link). Dismissed with "link" for the
  latter."""
  DEFAULT_CSS = """
  DriveScreen { align: center middle; }
  #drive-dialog { width: 96; max-width: 98%; height: auto; max-height: 95%; border: thick $accent; background: $surface; padding: 0 1; }
  #drive-title { text-style: bold; margin: 1 0; }
  .drive-section { text-style: bold underline; margin-top: 1; }
  .drive-text { height: auto; margin-bottom: 1; }
  .drive-row { height: auto; margin-bottom: 1; }
  .drive-row Label { width: 12; padding-top: 1; }
  .drive-row Input { width: 1fr; }
  #drive-state { height: auto; margin-bottom: 1; }
  .drive-buttons { height: auto; align-horizontal: right; margin-bottom: 1; }
  .drive-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close")]

  def __init__(self, cluster, home, local_reason=None):
    super().__init__()
    self.cluster, self.home, self.local_reason = cluster, home, local_reason

  def compose(self) -> ComposeResult:
    import socket
    from .. import mounts
    drive = self.app.drive_of(self.cluster)
    with Vertical(id="drive-dialog"):
      yield Static(f"Drive: {self.cluster.name} {self.home}", id="drive-title", markup=False)
      yield Static(f"On this machine ({socket.gethostname()}, where slurp runs)", classes="drive-section", markup=False)
      yield Static("sshfs through the session of this tab (no login); slurp shows its state in the connection window "
                   "and unmounts it when it quits." + (f"\nNot possible: {self.local_reason}" if self.local_reason else ""),
                   classes="drive-text", markup=False)
      with Horizontal(classes="drive-row"):
        yield Label("Path")
        yield Input(drive.path, id="drive-path", disabled=bool(self.local_reason), tooltip="an empty or new directory; remembered for this tab")
      with Horizontal(classes="drive-row"):
        yield Label("Read only")
        yield Switch(value=drive.read_only, id="drive-ro", disabled=bool(self.local_reason))
      yield Static(id="drive-state")
      with Horizontal(classes="drive-buttons"):
        yield Button("Unmount", id="drive-unmount", variant="error")
        yield Button("Mount here", id="drive-mount", variant="primary", disabled=bool(self.local_reason))
      yield Static("On your own machine", classes="drive-section")
      yield Static("A link: sshfs on Linux and macOS, SSHFS-Win on Windows, WinSCP, file managers; wherever your "
                   "machine is (through a relay).", classes="drive-text", markup=False)
      with Horizontal(classes="drive-buttons"):
        yield Button("Close", id="drive-close")
        yield Button("Make a link…", id="drive-link", disabled=share.available() is not None,
                     tooltip=share.available())

  def on_mount(self):
    self.show_state()
    self.set_interval(0.5, self.show_state)

  def show_state(self):
    if not self.is_mounted:
      return
    drive = self.app.drive_of(self.cluster)
    text = drive_state_text(drive)
    self.query_one("#drive-state", Static).update(text)
    ours = drive.state in ("mounting", "mounted", "broken", "unmounting", "reconnecting")
    self.query_one("#drive-unmount", Button).display = ours
    self.query_one("#drive-mount", Button).display = not ours

  @on(Button.Pressed, "#drive-mount")
  def mount_pressed(self, event):
    event.stop()
    path = self.query_one("#drive-path", Input).value.strip()
    if not path:
      self.notify("give a path", severity="error")
      return
    self.app.mount_drive(self.cluster, path, self.query_one("#drive-ro", Switch).value)

  @on(Button.Pressed, "#drive-unmount")
  def unmount_pressed(self, event):
    event.stop()
    self.app.unmount_drive(self.cluster)

  @on(Button.Pressed, "#drive-link")
  def link_pressed(self, event):
    event.stop()
    self.dismiss("link")

  @on(Button.Pressed, "#drive-close")
  def action_close(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

def drive_state_text(drive):
  """The state of a drive on this machine in words"""
  import humanize
  now = time.time()
  if drive.state == "mounted":
    since = f" since {datetime.fromtimestamp(drive.mounted_at):%H:%M:%S}" if drive.mounted_at else ""
    return Text(f"mounted at {drive.path}{since}" + (" (read only)" if drive.read_only else ""), style="bold green")
  if drive.state == "mounting":
    return Text(f"connecting … {now - drive.started:.0f} s ({drive.path})", style="yellow")
  if drive.state == "broken":
    return Text(f"mounted at {drive.path}, but the files do not answer (connection lost? sshfs reconnects when the "
                "tab is connected again; or Remount)", style="bold red")
  if drive.state == "unmounting":
    return Text("unmounting …", style="yellow")
  if drive.state == "reconnecting":
    c = drive.cluster.connection
    waiting = "waiting for the session of the tab" if c is None or not c.ready else f"next attempt in {max(0, drive.retry_at - now):.0f} s"
    return Text(f"connection lost: mounted again when the tab is connected ({waiting}"
                + (f", attempt {drive.attempts}" if drive.attempts else "") + ")", style="bold yellow")
  if drive.state == "failed":
    return Text(f"failed: {drive.error}", style="red")
  if drive.state == "foreign":
    source, kind = drive.foreign or ("?", "?")
    return Text(f"{drive.path}: mounted, but not by slurp ({source}, {kind})", style="yellow")
  return Text(f"not mounted (path {drive.path})", style="dim")
