"""An endpoint rclone reaches: S3 (AWS or S3-compatible: Ceph, MinIO, …), an SMB share (Windows, Samba), WebDAV
(Nextcloud, ownCloud, SharePoint, …): its settings, and its secrets (keys, a password, a token), which go to the
keyring only (never to a file of slurp); Test lists it, and with "as a destination" also writes a marker and deletes it"""

import time

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from ...cluster.remote import in_background
from ...transfer import endpoints as E, model as M, rclone as RC, secrets as S

# per kind: its settings (key, label, placeholder or a list to choose from, required) and its secrets (key, label, required)
FIELDS = {
  "s3": [("provider", "Provider", RC.PROVIDERS, False), ("endpoint", "Endpoint URL", "https://… (empty: AWS)", False),
         ("region", "Region", "e.g. eu-central-1 (may be empty for others than AWS)", False), ("bucket", "Bucket", "its name", True),
         ("path", "Its folder", "where Browse… starts, e.g. /runs (empty: the bucket's root)", False)],
  "smb": [("host", "Server", "e.g. files.example.org", True), ("share", "Share", "its name, e.g. data", True),
          ("user", "User", "your user there", True), ("domain", "Domain", "(empty: none, or the server's)", False),
          ("port", "Port", "445", False), ("path", "Its folder", "where Browse… starts (empty: the share's root)", False)],
  "webdav": [("url", "URL", "https://…, e.g. https://cloud.example.org/remote.php/dav/files/me", True),
             ("vendor", "Vendor", ["other", "nextcloud", "owncloud", "sharepoint", "sharepoint-ntlm"], False),
             ("user", "User", "(empty: with a token, or none)", False), ("path", "Its folder", "where Browse… starts (empty: the URL)", False)],
}
SECRET_FIELDS = {
  "s3": [("access_key", "Access key", True), ("secret_key", "Secret key", True), ("session_token", "Session token", False)],
  "smb": [("password", "Password", True)],
  "webdav": [("password", "Password", False), ("bearer_token", "Token", False)],
}
TITLES = {"s3": "S3 endpoint", "smb": "SMB share", "webdav": "WebDAV endpoint"}
NOTES = {
  "s3": "A bucket of AWS S3 or of an S3-compatible store (Ceph, MinIO, a site's object store).",
  "smb": "A share of a Windows or Samba file server (SMB 2 and 3), reached from where rclone runs.",
  "webdav": "A WebDAV folder: Nextcloud, ownCloud, SharePoint, a site's file service.",
}
DEFAULTS = {"provider": "Other", "vendor": "other"}

class RcloneEndpointScreen(ModalScreen):
  """New or Edit… of an S3, SMB or WebDAV endpoint: dismissed with {"definition": EndpointDef, "keys": {name: value}}
  (keys: only those typed: an empty field keeps the one in the keyring), or None"""
  DEFAULT_CSS = """
  RcloneEndpointScreen { align: center middle; }
  #rc-dialog { width: 100; max-width: 96%; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #rc-title { text-style: bold; margin-bottom: 1; height: auto; }
  .rc-note { color: $text-muted; height: auto; }
  .rc-row { height: auto; margin-top: 1; }
  .rc-row Label { width: 18; }
  .rc-row Input, .rc-row Select { width: 1fr; }
  #rc-result { height: auto; margin-top: 1; }
  #rc-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #rc-buttons Button, #rc-buttons Checkbox { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, store, kind="s3", endpoint=None):
    super().__init__()
    self.store, self.endpoint = store, endpoint
    self.definition = endpoint.definition if endpoint is not None else None
    self.kind = self.definition.kind if self.definition is not None else kind

  def compose(self) -> ComposeResult:
    d = self.definition
    s = d.settings if d else {}
    have = set(d.secrets) - set(S.missing(d)) if d else set()
    with Vertical(id="rc-dialog"):
      yield Static(f"The {TITLES[self.kind]} {d.name}" if d else f"A new {TITLES[self.kind]}", id="rc-title")
      with VerticalScroll():
        yield Static(NOTES[self.kind] + " rclone moves the data, on this machine or on a cluster. Its secrets are kept "
                     "only in the keyring of this machine.", classes="rc-note")
        with Horizontal(classes="rc-row"):
          yield Label("Name")
          yield Input(d.name if d else "", placeholder="shown everywhere", id="rc-name")
        for key, label, hint, _required in FIELDS[self.kind]:
          with Horizontal(classes="rc-row"):
            yield Label(label)
            if isinstance(hint, list):
              yield Select([(v, v) for v in hint], value=s.get(key) or DEFAULTS.get(key, hint[0]), allow_blank=False, id=f"rc-{key}", compact=True)
            else:
              yield Input(str(s.get(key, "")), placeholder=hint, id=f"rc-{key}")
        for name, label, required in SECRET_FIELDS[self.kind]:
          with Horizontal(classes="rc-row"):
            yield Label(label)
            hint = "(in the keyring: empty keeps it)" if name in have else ("" if required else "(optional)")
            yield Input(password=True, placeholder=hint, id=f"rc-{name}")
        yield Static(id="rc-result")
      with Horizontal(id="rc-buttons"):
        yield Checkbox("as a destination (also write)", id="rc-write", compact=True,
                       tooltip="Test also writes a marker (.slurp-test-…) and deletes it")
        yield Button("Test", id="rc-test", compact=True)
        yield Button("Cancel", id="rc-cancel", compact=True)
        yield Button("Save", id="rc-save", variant="primary", compact=True)

  def on_mount(self):
    ok, why = RC.available()
    if not ok:
      self.say(why, "red")
    self.query_one("#rc-name", Input).focus()

  def say(self, words, style=""):
    self.query_one("#rc-result", Static).update(Text(words, style=style))

  def settings(self):
    out = {}
    for key, _label, hint, _required in FIELDS[self.kind]:
      widget = self.query_one(f"#rc-{key}")
      value = str(widget.value).strip() if not isinstance(hint, list) else str(widget.value)
      if key == "path":
        value = "/" + value.strip("/") if value.strip("/") else ""
      if value:
        out[key] = value
    return out

  def missing_setting(self, s):
    return next((label.lower() for key, label, _hint, required in FIELDS[self.kind] if required and not s.get(key)), "")

  def typed_keys(self):
    return {name: self.query_one(f"#rc-{name}", Input).value.strip() for name, _, _ in SECRET_FIELDS[self.kind]
            if self.query_one(f"#rc-{name}", Input).value.strip()}

  def keys(self):
    """The secrets to test with: those typed, else those in the keyring"""
    out = {}
    if self.definition is not None:
      out = {n: S.get(S.user(self.definition.id, n)) for n in self.definition.secrets}
    out.update(self.typed_keys())
    return {k: v for k, v in out.items() if v}

  def missing_secret(self, have):
    return next((label.lower() for name, label, required in SECRET_FIELDS[self.kind] if required and name not in have), "")

  @on(Button.Pressed, "#rc-test")
  def test(self, event):
    event.stop()
    s, keys = self.settings(), self.keys()
    missing = self.missing_setting(s) or self.missing_secret(set(keys))
    if missing:
      self.say(f"the {missing} first", "yellow")
      return
    write = self.query_one("#rc-write", Checkbox).value
    self.say("asking it …", "yellow")
    d = E.EndpointDef("test", "test", self.kind, s, list(keys))
    def work():
      try:
        e = RC.Endpoint(d, keys)
        return True, RC.test(e.env, e.path(s.get("path", "/")), write=write)
      except RC.RcloneError as err:
        return False, str(err)
    def tested(result):
      ok, words = result or (False, "?")
      self.say(("✓ " if ok else "✗ ") + words, "green" if ok else "red")
    in_background(self, work, tested, group="rc-test", name="the test", connection=None)

  @on(Button.Pressed, "#rc-save")
  def save(self, event):
    event.stop()
    s = self.settings()
    name = self.query_one("#rc-name", Input).value.strip()
    if not name:
      self.say("give it a name", "yellow")
      return
    missing = self.missing_setting(s)
    if missing:
      self.say(f"the {missing}", "yellow")
      return
    if any(x.name == name and (self.definition is None or x.id != self.definition.id) for x in self.store.defs):
      self.say(f"an endpoint is called {name} already", "yellow")
      return
    keys = self.typed_keys()
    have = set(self.definition.secrets) - set(S.missing(self.definition)) if self.definition else set()
    missing = self.missing_secret(set(keys) | have)
    if missing:
      self.say(f"the {missing} (it goes to the keyring)", "yellow")
      return
    ok, why = S.available()
    if not ok and keys:
      self.say(why, "red")
      return
    d = self.definition
    names = [n for n, _, _ in SECRET_FIELDS[self.kind]]
    secrets = [n for n in names if n in set(keys) | have]
    definition = E.EndpointDef(d.id if d else M.new_id(), name, self.kind, s, secrets, d.created if d else time.time())
    self.dismiss({"definition": definition, "keys": keys})

  @on(Button.Pressed, "#rc-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  def action_cancel(self):
    self.dismiss(None)
