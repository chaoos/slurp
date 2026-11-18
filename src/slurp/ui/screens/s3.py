"""An S3 endpoint (AWS or S3-compatible: Ceph, MinIO, …): its provider, endpoint URL, region, bucket and folder, and
its keys, which go to the keyring only (never to a file of slurp); Test lists it, and with "as a destination" also
writes a marker object and deletes it"""

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

KEY_FIELDS = (("access_key", "Access key"), ("secret_key", "Secret key"), ("session_token", "Session token"))

class S3EndpointScreen(ModalScreen):
  """New or Edit… of an S3 endpoint: dismissed with {"definition": EndpointDef, "keys": {name: value}} (keys: only
  those typed: an empty field keeps the one in the keyring), or None"""
  DEFAULT_CSS = """
  S3EndpointScreen { align: center middle; }
  #s3-dialog { width: 100; max-width: 96%; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #s3-title { text-style: bold; margin-bottom: 1; height: auto; }
  .s3-note { color: $text-muted; height: auto; }
  .s3-row { height: auto; margin-top: 1; }
  .s3-row Label { width: 18; }
  .s3-row Input, .s3-row Select { width: 1fr; }
  #s3-result { height: auto; margin-top: 1; }
  #s3-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #s3-buttons Button, #s3-buttons Checkbox { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, store, endpoint=None):
    super().__init__()
    self.store, self.endpoint = store, endpoint
    self.definition = endpoint.definition if endpoint is not None else None

  def compose(self) -> ComposeResult:
    d = self.definition
    s = d.settings if d else {}
    have = set(d.secrets) - set(S.missing(d)) if d else set()
    with Vertical(id="s3-dialog"):
      yield Static(f"The S3 endpoint {d.name}" if d else "A new S3 endpoint", id="s3-title")
      with VerticalScroll():
        yield Static("A bucket of AWS S3 or of an S3-compatible store (Ceph, MinIO, a site's object store). rclone moves "
                     "the data, on this machine or on a cluster. Its keys are kept only in the keyring of this machine.",
                     classes="s3-note")
        with Horizontal(classes="s3-row"):
          yield Label("Name")
          yield Input(d.name if d else "", placeholder="shown everywhere, e.g. my-bucket", id="s3-name")
        with Horizontal(classes="s3-row"):
          yield Label("Provider")
          yield Select([(p, p) for p in RC.PROVIDERS], value=s.get("provider") or "Other", allow_blank=False, id="s3-provider", compact=True)
        with Horizontal(classes="s3-row"):
          yield Label("Endpoint URL")
          yield Input(s.get("endpoint", ""), placeholder="https://… (empty: AWS)", id="s3-endpoint")
        with Horizontal(classes="s3-row"):
          yield Label("Region")
          yield Input(s.get("region", ""), placeholder="e.g. eu-central-1 (may be empty for others than AWS)", id="s3-region")
        with Horizontal(classes="s3-row"):
          yield Label("Bucket")
          yield Input(s.get("bucket", ""), placeholder="its name", id="s3-bucket")
        with Horizontal(classes="s3-row"):
          yield Label("Its folder")
          yield Input(s.get("path", ""), placeholder="where Browse… starts, e.g. /runs (empty: the bucket's root)", id="s3-path")
        for name, label in KEY_FIELDS:
          with Horizontal(classes="s3-row"):
            yield Label(label)
            hint = "(in the keyring: empty keeps it)" if name in have else ("(optional: temporary keys only)" if name == "session_token" else "")
            yield Input(password=True, placeholder=hint, id=f"s3-{name}")
        yield Static(id="s3-result")
      with Horizontal(id="s3-buttons"):
        yield Checkbox("as a destination (also write)", id="s3-write", compact=True,
                       tooltip="Test also writes a marker object (.slurp-test-…) and deletes it")
        yield Button("Test", id="s3-test", compact=True)
        yield Button("Cancel", id="s3-cancel", compact=True)
        yield Button("Save", id="s3-save", variant="primary", compact=True)

  def on_mount(self):
    ok, why = RC.available()
    if not ok:
      self.say(why, "red")
    self.query_one("#s3-name", Input).focus()

  def say(self, words, style=""):
    self.query_one("#s3-result", Static).update(Text(words, style=style))

  def settings(self):
    v = lambda wid: self.query_one(f"#s3-{wid}", Input).value.strip()
    path = v("path")
    return {"provider": str(self.query_one("#s3-provider", Select).value), "endpoint": v("endpoint"), "region": v("region"),
            "bucket": v("bucket"), **({"path": "/" + path.strip("/")} if path.strip("/") else {})}

  def typed_keys(self):
    return {name: self.query_one(f"#s3-{name}", Input).value.strip() for name, _ in KEY_FIELDS if self.query_one(f"#s3-{name}", Input).value.strip()}

  def keys(self):
    """The keys to test with: those typed, else those in the keyring"""
    out = {}
    if self.definition is not None:
      out = {n: S.get(S.user(self.definition.id, n)) for n in self.definition.secrets}
    out.update(self.typed_keys())
    return {k: v for k, v in out.items() if v}

  @on(Button.Pressed, "#s3-test")
  def test(self, event):
    event.stop()
    s, keys = self.settings(), self.keys()
    if not s["bucket"]:
      self.say("the bucket first", "yellow")
      return
    if not keys.get("access_key") or not keys.get("secret_key"):
      self.say("the access key and the secret key first", "yellow")
      return
    write = self.query_one("#s3-write", Checkbox).value
    self.say("asking it …", "yellow")
    d = E.EndpointDef("test", "test", "s3", s, [])
    def work():
      try:
        e = RC.Endpoint(d, keys)
        return True, RC.test(e.env, e.path(s.get("path", "/")), write=write)
      except RC.RcloneError as err:
        return False, str(err)
    def tested(result):
      ok, words = result or (False, "?")
      self.say(("✓ " if ok else "✗ ") + words, "green" if ok else "red")
    in_background(self, work, tested, group="s3-test", name="the S3 test", connection=None)

  @on(Button.Pressed, "#s3-save")
  def save(self, event):
    event.stop()
    s = self.settings()
    name = self.query_one("#s3-name", Input).value.strip()
    if not name:
      self.say("give it a name", "yellow")
      return
    if not s["bucket"]:
      self.say("the bucket", "yellow")
      return
    if any(x.name == name and (self.definition is None or x.id != self.definition.id) for x in self.store.defs):
      self.say(f"an endpoint is called {name} already", "yellow")
      return
    keys = self.typed_keys()
    have = set(self.definition.secrets) - set(S.missing(self.definition)) if self.definition else set()
    if not ({"access_key", "secret_key"} <= set(keys) | have):
      self.say("the access key and the secret key (they go to the keyring)", "yellow")
      return
    ok, why = S.available()
    if not ok:
      self.say(why, "red")
      return
    d = self.definition
    secrets = sorted((set(keys) | have) & {"access_key", "secret_key", "session_token"})
    definition = E.EndpointDef(d.id if d else M.new_id(), name, "s3", s, secrets, d.created if d else time.time())
    self.dismiss({"definition": definition, "keys": keys})

  @on(Button.Pressed, "#s3-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  def action_cancel(self):
    self.dismiss(None)
