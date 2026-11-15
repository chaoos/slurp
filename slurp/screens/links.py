"""Shells and drives for the user's own terminal: the choice of the Shell buttons, the setup panel of a link and its
view (commands, password, state)"""

import socket
import time
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, RadioButton, RadioSet, Select, Static, Switch, TextArea

from .. import share, winhelper
from ..config import load_config, update_config
from .dialogs import ConfirmScreen, copy_text

# the relay choices of the setup panel: (id, label, what it is in short); the details below when chosen
RELAYS = [
  ("direct", "direct", "no relay: your machine reaches this one itself"),
  ("sshj", "ssh-j.com", "public, no account, the default: ssh jumps through"),
  ("serveo", "serveo.net", "public, no account: the same way as ssh-j.com"),
  ("pinggy", "pinggy.io", "public TCP port, no account; 60 minutes an address"),
  ("sshreach", "sshreach.me", "a service with an account (a trial, then paid)"),
  ("custom", "custom / own relay", "an SSH server both ends log in to (no third party)"),
]

PUBLIC_POINTS = (
  "• The relay cannot read the session: it stays SSH encrypted from your terminal to slurp. Check the host key "
  "fingerprint below when your ssh asks on the first connect.\n"
  "• Anyone who has the command and the password gets {what} as you on the cluster: do not share or post them, "
  "prefer one use and a short expiry.\n"
  "• The relay sees that a connection happens, when, from where and how much data flows; it may go offline at any "
  "time.\n"
  "• Your site's rules may not allow access through third-party relays.\n"
  "A relay is just an SSH host both ends reach: your own (custom) does the same without a third party; direct needs "
  "none, if your machine reaches this one itself.")
PUBLIC_INTRO = {
  "ssh-j": ("{relay}: a PUBLIC relay run by a third party, made for exactly this. No account on either end: slurp "
            "registers this link there under a random name, your ssh jumps through {relay} to that name (ssh -J)."),
  "serveo": ("serveo.net: a PUBLIC relay run by a third party, used like ssh-j.com: slurp registers this link there "
             "under a random name (anonymously: no account, none of your keys are sent), your ssh jumps through "
             "serveo.net to that name (ssh -J)."),
  "pinggy": ("pinggy.io: a PUBLIC TCP tunnel run by a third party (free, no account). It gives slurp's server for this "
             "link a port on the internet; your ssh connects to that host and port itself, no jump. Anyone on the "
             "internet can reach that port and try the password (5 wrong ones lock the link: a scanner may lock it). "
             "A free tunnel lasts 60 minutes, then slurp opens a new one at another address (copy the command, or "
             "open the link in the helper, again). The address names the public IP of this machine's network."),
  "sshreach": ("sshreach.me: a service run by a third party, with an account (a 10-day trial, then paid). Create a "
               "client in its web interface; its page \"manual control\" shows the client's user, the forwarding "
               "server and the port: give them as user@fw1.sshreach.me:10248. slurp logs in there with your key (the "
               "one registered for the client) and forwards that port to itself; your ssh connects to that server "
               "and port, no jump. Whoever reaches that port can try the password."),
}
OWN_NOTE = ("Relay {relay}: a plain OpenSSH jump host that both ends reach, no third party. slurp logs in to it with "
            "your keys (no prompts) and asks it for a free port (it must allow forwarding, the default); your "
            "terminal logs in there too and jumps through it to slurp. Or sshj://host: a relay that works like "
            "ssh-j.com.")

DIRECT_NOTE = ("Direct, no relay: your machine connects to {host} port {port} itself, so it must reach this machine (the "
               "same network, a VPN; no firewall in between). For this link slurp listens on that port on every "
               "interface of this machine (with an address as the host: only on that one); whoever reaches it can "
               "try the password (5 wrong ones lock the link). Host: the name your machine knows this one by (a "
               "fully qualified name, an alias, an address); slurp remembers it, and the port, for this machine.")

def relay_choice(relay):
  """The radio button of a relay setting: (id, the text of its field)"""
  kind, rest = share.parse_relay(relay)
  if kind == share.DIRECT:
    return "direct", ""
  if kind == "ssh-j":
    return ("sshj", "") if rest == share.PUBLIC_RELAY else ("custom", f"sshj://{rest}")
  if kind == "sshreach":
    return "sshreach", rest
  if kind in ("serveo", "pinggy"):
    return kind, ""
  return "custom", rest

def relay_setting():
  return load_config().get("link_relay") or share.DEFAULT_RELAY

def machine():
  """The key of this machine in "link_direct" (a home directory, and so the config, is often shared by many nodes)"""
  return socket.gethostname()

def direct_setting():
  """(host, port) of direct links from this machine: what the relay setting names (direct://host:port), else what
  was stored for this machine, else its fully qualified name and a free port ("")"""
  kind, rest = share.parse_relay(relay_setting())
  if kind == share.DIRECT and rest:
    host, _, port = rest.partition(":")
    return host, port
  entry = (load_config().get("link_direct") or {}).get(machine())
  if isinstance(entry, dict) and entry.get("host"):
    return str(entry["host"]), str(entry.get("port") or "")
  return socket.getfqdn(), ""

def warning_text(relay, kind):
  what = ("a shell" if kind == "shell" else "this slurp itself, with all its tabs, shells and drives" if kind == "web"
          else "your files (read and write, unless read only)")
  try:
    share.check_relay(relay)
  except ValueError as e:
    return Text(str(e), style="red")
  rtype, rest = share.parse_relay(relay)
  if share.is_public(relay):
    return Text(PUBLIC_INTRO[rtype].format(relay=rest) + "\n" + PUBLIC_POINTS.format(what=what), style="yellow")
  if rtype == share.DIRECT:
    host, port = share.direct_address(relay)
    return Text(DIRECT_NOTE.format(host=host, port=port or "(a free one)"), style="yellow")
  return Text(OWN_NOTE.format(relay=f"sshj://{rest}" if rtype == "ssh-j" else relay), style="dim")

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
  #setup-body { height: auto; max-height: 70vh; } /* (the buttons stay in view; the rest scrolls) */
  .setup-row { height: auto; margin-bottom: 1; }
  .setup-row Label { width: 14; padding-top: 1; }
  .setup-row Input, .setup-row Select { width: 1fr; }
  #setup-relays { width: 1fr; height: auto; border: none; padding: 0; }
  #setup-relays RadioButton { width: 1fr; }
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
        chosen, text = relay_choice(relay_setting())
        stored = load_config()
        with Horizontal(classes="setup-row"):
          yield Label("Relay")
          with RadioSet(id="setup-relays"):
            for rid, label, short in RELAYS:
              yield RadioButton(Text.assemble((label.ljust(19), "bold"), (short, "dim")), value=rid == chosen, id=f"setup-relay-{rid}")
        with Horizontal(classes="setup-row setup-for-custom"):
          yield Label("Relay host")
          yield Input(text if chosen == "custom" else str(stored.get("link_relay_custom") or ""), id="setup-relay",
                      placeholder="user@relay.example.org, or sshj://host",
                      tooltip="an SSH server you (and your machine) log in to with a key: host or user@host")
        with Horizontal(classes="setup-row setup-for-sshreach"):
          yield Label("sshreach.me")
          yield Input(text if chosen == "sshreach" else str(stored.get("link_sshreach") or ""), id="setup-sshreach",
                      placeholder="user@fw1.sshreach.me:10248 (from its page: manual control)",
                      tooltip="the client's user, the forwarding server and the port, as sshreach.me shows them")
        host, port = direct_setting()
        with Horizontal(classes="setup-row setup-for-direct"):
          yield Label("Host")
          yield Input(host, id="setup-direct-host", placeholder=socket.getfqdn(),
                      tooltip="the name or address your machine connects to: this machine's fully qualified name, "
                              "or the one it has on your network or VPN")
        with Horizontal(classes="setup-row setup-for-direct"):
          yield Label("Port")
          yield Input(port, id="setup-direct-port", type="integer", placeholder="a free one",
                      tooltip="a fixed port (e.g. one the firewall lets through), or empty for a free one")
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

  def chosen(self):
    """The id of the relay chosen (direct, sshj, serveo, pinggy, sshreach, custom)"""
    pressed = self.query_one("#setup-relays", RadioSet).pressed_button
    return pressed.id.removeprefix("setup-relay-") if pressed is not None else "sshj"

  def relay(self):
    """The relay setting of the link to make (direct: with the host and port of the fields); ValueError if not valid"""
    chosen = self.chosen()
    if chosen == "direct":
      return share.direct_relay(self.query_one("#setup-direct-host", Input).value or socket.getfqdn(),
                                self.query_one("#setup-direct-port", Input).value)
    if chosen == "sshreach":
      relay = "sshreach://" + self.query_one("#setup-sshreach", Input).value.strip().removeprefix("sshreach://")
    elif chosen == "custom":
      relay = self.query_one("#setup-relay", Input).value.strip()
      if not relay:
        raise ValueError("give the relay: user@host of an SSH server both ends log in to (or sshj://host)")
      if share.parse_relay(relay)[0] != "openssh" and not relay.startswith("sshj://"):
        raise ValueError(f"{relay}: that is one of the choices above, pick it there")
    else:
      relay = {"sshj": share.PUBLIC_RELAY, "serveo": share.SERVEO, "pinggy": "pinggy"}[chosen]
    share.check_relay(relay)
    return relay

  @on(RadioSet.Changed, "#setup-relays")
  @on(Input.Changed, "#setup-relay")
  @on(Input.Changed, "#setup-sshreach")
  @on(Input.Changed, "#setup-direct-host")
  @on(Input.Changed, "#setup-direct-port")
  def show_warning(self, event=None):
    if isinstance(event, Input.Changed) and event.input.id == "setup-relay" and event.input.has_focus and event.value.strip():
      # (typing ssh-j.com, direct://host:port ... into the custom field: that choice)
      kind, rest = share.parse_relay(event.value)
      target = relay_choice(event.value)[0] if kind != "openssh" else "custom"
      if target == "direct" and rest:
        host, _, port = rest.partition(":")
        self.query_one("#setup-direct-host", Input).value, self.query_one("#setup-direct-port", Input).value = host, port
      button = self.query_one(f"#setup-relay-{target}", RadioButton)
      if not button.value:
        button.value = True
    chosen = self.chosen()
    for rid in ("direct", "sshreach", "custom"):
      for row in self.query(f".setup-for-{rid}"):
        row.display = chosen == rid
    widget = self.query_one("#setup-warning", Static)
    try:
      relay = self.relay()
    except ValueError as e: # (what the choice is, then what is wrong)
      about = {"sshreach": PUBLIC_INTRO["sshreach"], "custom": OWN_NOTE.format(relay="(user@host)"),
               "direct": DIRECT_NOTE.format(host="(the host)", port="(the port)")}.get(chosen, "")
      widget.update(Text.assemble((about + "\n", "dim") if about else "", (str(e), "red")))
      relay = ""
    else:
      widget.update(warning_text(relay, self.kind))
    widget.styles.border = ("round", "yellow" if relay and share.is_public(relay) else "gray")

  @on(RadioSet.Changed, "#setup-auth")
  def auth_changed(self, event):
    self.query_one("#setup-key", TextArea).display = event.pressed.id == "setup-auth-key"

  @on(Button.Pressed, "#setup-create")
  def create(self, event):
    event.stop()
    try:
      relay = self.relay()
    except ValueError as e:
      self.notify(str(e), severity="error")
      return
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
    # (the relay is a setting: the next link uses it too; what the fields of the others say is kept as well)
    rtype = share.parse_relay(relay)[0]
    if rtype == share.DIRECT: # (the relay "direct"; the host and port for this machine)
      host, port = share.direct_address(relay)
      stored = dict(load_config().get("link_direct") or {})
      stored[machine()] = {"host": host, **({"port": port} if port else {})}
      update_config(link_relay=share.DIRECT, link_direct=stored)
    elif rtype == "sshreach":
      update_config(link_relay=relay, link_sshreach=share.parse_relay(relay)[1])
    elif self.chosen() == "custom":
      update_config(link_relay=relay, link_relay_custom=relay)
    elif relay != relay_setting():
      update_config(link_relay=relay)
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
           "ready": (f"ready, directly at {link.relay_host} port {link.port}" if link.direct else
                     f"ready, at {link.target()[0]} port {link.target()[1]} ({share.parse_relay(link.relay)[1].split('@')[-1].split(':')[0]})" if link.straight else
                     f"ready, via {link.relay_host}" + (f" port {link.relay_port}" if link.relay_port else ""), "green"),
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
  #lv-helper { height: auto; border: round $accent; padding: 0 1; margin-bottom: 1; }
  #lv-helper Horizontal { height: auto; margin-top: 1; }
  #lv-helper Button { margin-right: 1; }
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
    sessions = Text(f"{len(live)} open, {link.logins} logins so far" + (f", {link.failures} wrong passwords" if link.failures else ""))
    if any(self.app.this_view(s) for s in live):
      sessions.append("  ◀ one is this view (the browser you are using)", style="bold")
    grid.add_row("Sessions", sessions)
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
    if winhelper.available(link) is None:
      widgets.append(Vertical(
        Static("On your Windows computer: the slurp helper (one click, then the password)", classes="lv-command-title"),
        Static(f"It {HELPER_DOES[link.kind]}, and keeps it in its tray icon. In slurp's web view, Open on my computer "
               "hands the link to your browser, which starts the helper; from a terminal, copy the link and open it "
               "with Win+R. Set it up once on your computer: Set up the helper.", classes="lv-command-note"),
        Horizontal(Button("Open on my computer", id="lv-helper-open", variant="primary"),
                   Button("Copy helper link", id="lv-helper-copy"),
                   Button("Set up the helper…", id="lv-helper-setup")),
        id="lv-helper"))
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

  @on(Button.Pressed, "#lv-helper-open")
  def helper_open(self, event):
    event.stop()
    url = winhelper.link_url(self.link)
    def done(ok, why):
      if ok:
        self.app.notify("Your browser hands the link to the slurp helper (the first time it asks whether to open it). "
                        "Give the password there: Copy it here.", timeout=10)
      else:
        copy_text(self.app, url, f"the helper link of link {self.link.id} ({why}: open it with Win+R, paste, Enter)")
    self.app.open_on_client(url, done)

  @on(Button.Pressed, "#lv-helper-copy")
  def helper_copy(self, event):
    event.stop()
    copy_text(self.app, winhelper.link_url(self.link), f"the helper link of link {self.link.id} (Win+R, paste, Enter)")

  @on(Button.Pressed, "#lv-helper-setup")
  def helper_setup(self, event):
    event.stop()
    self.app.push_screen(HelperSetupScreen())

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
    message = Text("No more logins with it" + (f", and its {live} open session{'s' if live != 1 else ''} end." if live else "."))
    if any(self.app.this_view(s) for s in self.link.live_sessions):
      message.append("\n\nThe browser you are using now is connected through this link: revoking it ends this very view. "
                     "It goes blank, and you get back to slurp only from a terminal or through a new web link.", style="bold red")
    self.app.push_screen(ConfirmScreen(f"Revoke link {self.link.id}", message, "Revoke"), done)

  @on(Button.Pressed, "#lv-close")
  def action_close(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss()

HELPER_DOES = {"drive": "maps a folder ($HOME, $SCRATCH, $PROJECT, … or one you type; more on other letters) and opens it in Explorer",
               "shell": "opens a terminal window with the shell at once (it asks for the password there)",
               "web": "opens this slurp in your browser (the tunnel runs in the helper, no window to keep open)"}

HELPER_TEXT = """\
One click on **Open on my computer** in a link window, the link's password, and the helper does the rest: it maps
the drive and opens Explorer, opens the web view in your browser, or opens a terminal window with the shell. What is
open stays in its icon in the tray (^ next to the clock): a click on it shows every connection and its state, with
Reconnect, Disconnect and more. It is for Windows 10 and 11.

**Set it up once** on your Windows computer (no admin rights needed):

- **Download the setup** (when slurp is open in your browser): run `slurp-helper-setup.cmd` from your downloads.
  Windows may warn about a file from the internet: *More info → Run anyway*.
- or **Copy the setup command** and paste it into a PowerShell window (Start → PowerShell).

Then the first link asks your browser whether to open slurp links with it: allow it (*always allow* saves the question).
From a terminal (not the browser): **Copy helper link** in the link window, then Win+R, paste, Enter.

**What the setup does:** it puts the helper into `%LOCALAPPDATA%\\slurp-helper`, compiles its small launcher there
(`slurp-open.exe`, from source code in the setup: no program is downloaded), registers `slurp://` links for your
Windows user, starts the helper with Windows (tray menu: *Start with Windows*), adds a start menu entry, and remembers
this slurp's host key (`{fingerprint}`, as `{alias}`): a link of another slurp asks first. Setting it up again
updates it. **Remove it:** tray menu → *Uninstall*.

**It needs** the OpenSSH Client of Windows (an optional feature, there by default on Windows 11: *Settings → Apps →
Optional features*). A drive also needs WinFsp and SSHFS-Win: the helper offers to install them (winget).

**How it connects** (as the commands of the link window do): a tunnel to the relay (no password), then it checks
slurp's host key through it against the one in the link, then logs in with the password you give it: in its window for
a drive or the web view, in the terminal for a shell (ssh asks there). The password is not in the link and not stored:
the helper types it into ssh's prompt for you for the web view, or gives it to `net use` for a drive.
"""

class HelperSetupScreen(ModalScreen):
  """How to set up the slurp helper on Windows: download the setup (web view) or copy the setup command"""
  DEFAULT_CSS = """
  HelperSetupScreen { align: center middle; }
  #hs-dialog { width: 110; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #hs-title { text-style: bold; margin: 1 0 0 0; }
  #hs-body { height: 1fr; }
  #hs-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #hs-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close")]

  def compose(self) -> ComposeResult:
    with Vertical(id="hs-dialog"):
      yield Static("The slurp helper for Windows", id="hs-title")
      with VerticalScroll(id="hs-body"):
        yield Markdown(HELPER_TEXT.replace("{fingerprint}", share.host_fingerprint()).replace("{alias}", share.host_alias()))
      with Horizontal(id="hs-buttons"):
        yield Button("Close", id="hs-close")
        yield Button("Copy the setup command", id="hs-copy")
        yield Button("Download the setup", id="hs-download", variant="primary",
                     tooltip="slurp-helper-setup.cmd, in the browser that shows slurp now")

  def on_mount(self):
    self.query_one("#hs-download", Button).disabled = not self.app.on_client()

  @on(Button.Pressed, "#hs-download")
  def download(self, event):
    event.stop()
    def done(ok, why):
      if ok:
        self.app.notify(f"Your browser downloads {winhelper.SETUP_NAME}: run it (More info → Run anyway, if Windows warns)",
                        timeout=10)
      else:
        self.app.notify(f"no download: {why}. Copy the setup command instead.", severity="warning", timeout=10)
    self.app.deliver_to_client(winhelper.SETUP_NAME, winhelper.setup_file(), done)

  @on(Button.Pressed, "#hs-copy")
  def copy_command(self, event):
    event.stop()
    copy_text(self.app, winhelper.setup_command(), "the setup command of the slurp helper (paste it into PowerShell)")

  @on(Button.Pressed, "#hs-close")
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
