"""Globus in the Transfers tab: the login (Globus's page in the browser, its code pasted back), a Globus endpoint (a
collection: searched by name or its id, a default folder, Test), and the link of a tab to a collection (which folder
of the tab is which folder of the collection)"""

import time

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ...cluster.remote import in_background
from ...transfer import endpoints as E, globus as G, model as M
from ..widgets import ClickTable
from .dialogs import copy_text

def attempt(work):
  """work() as (result, None) or (None, why): a Globus error is the user's to read, not a crash"""
  def run():
    try:
      return work(), None
    except G.GlobusError as e:
      return None, e.words
    except Exception as e:
      return None, str(e)
  return run

class GlobusLoginScreen(ModalScreen):
  """Log in to Globus: its page in the browser, the code it shows pasted here; the refresh token goes to the keyring.
  Dismissed with True when logged in"""
  DEFAULT_CSS = """
  GlobusLoginScreen { align: center middle; }
  #gl-dialog { width: 100; max-width: 96%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #gl-title { text-style: bold; margin-bottom: 1; }
  .gl-note { color: $text-muted; height: auto; margin-bottom: 1; }
  .gl-row { height: auto; margin-bottom: 1; }
  .gl-row Input { width: 1fr; }
  .gl-row Button { margin-left: 1; }
  #gl-url { height: auto; margin-bottom: 1; }
  #gl-status { height: auto; }
  #gl-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #gl-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, collections=(), why=""):
    super().__init__()
    self.collections, self.why = list(collections), why
    self.login = None
    self.had_client = bool(G.client_id()) # (the app was there already: the login is not "step 2")

  def compose(self) -> ComposeResult:
    with Vertical(id="gl-dialog"):
      yield Static("Log in to Globus", id="gl-title")
      if self.why:
        yield Static(self.why, classes="gl-note")
      # step 1 (once): the Globus app slurp logs in as, its client id
      with Vertical(id="gl-step-client"):
        yield Static(Text.assemble(
          ("Step 1 of 2: the Globus app slurp logs in as\n", "bold"),
          "Globus lets programs log in only as an app registered with it, and slurp has none of its own yet. Register "
          "one (once, a minute; it is free):\n"
          "  1. open app.globus.org/settings/developers (the button below) and log in there;\n"
          "  2. \"Register a thick client or script that will be installed and run by users on their devices\";\n"
          "  3. a project (any, e.g. a new one called slurp), the app's name: slurp; "
          "redirect: https://auth.globus.org/v2/web/auth-code;\n"
          "  4. copy its Client UUID and paste it here."), classes="gl-note")
        with Horizontal(classes="gl-row"):
          yield Button("Open the Globus developers page", id="gl-dev-open", compact=True)
          yield Button("Copy its link", id="gl-dev-copy", compact=True)
        with Horizontal(id="gl-client-row", classes="gl-row"):
          yield Input(G.client_id(), placeholder="the Client UUID, e.g. 01234567-89ab-cdef-0123-456789abcdef", id="gl-client")
          yield Button("Use it", id="gl-client-use", variant="primary", compact=True)
      # step 2: the login
      with Vertical(id="gl-step-login"):
        yield Static(Text.assemble(
          ("Log in\n", "bold"),
          "Open Globus's login page (or copy its link into a browser), log in there with your institution, allow "
          "slurp, and paste the code Globus then shows into the field below. slurp keeps the login in the keyring of "
          "this machine; Globus moves the data, not slurp."), id="gl-login-note", classes="gl-note")
        yield Static(id="gl-url")
        with Horizontal(classes="gl-row"):
          yield Button("Open the login page", id="gl-open", variant="primary", compact=True)
          yield Button("Copy its link", id="gl-copy", compact=True)
          yield Button("Another app…", id="gl-client-change", compact=True, tooltip="the client id of another Globus app")
        with Horizontal(classes="gl-row"):
          yield Input(placeholder="the code Globus shows after the login", id="gl-code", password=True)
      yield Static(id="gl-status")
      with Horizontal(id="gl-buttons"):
        yield Button("Cancel", id="gl-cancel", compact=True)
        yield Button("Log in", id="gl-done", variant="primary", compact=True)

  DEVELOPERS = "https://app.globus.org/settings/developers"

  def on_mount(self):
    self.prepare()

  def prepare(self, change=False):
    """Step 1 while there is no client id (or change: another one), else step 2: the login page of that app"""
    have = bool(G.client_id()) and not change
    q = self.query_one
    q("#gl-step-client").display = not have
    q("#gl-step-login").display = q("#gl-done").display = have
    q("#gl-title", Static).update("Log in to Globus" + ("" if have else ": first, its app"))
    if not have:
      self.login = None
      self.status("")
      q("#gl-client", Input).focus()
      return
    try:
      self.login = G.Login(self.collections)
    except G.GlobusError as e:
      self.status(e.words, "red")
      return
    url = self.login.url()
    q("#gl-login-note", Static).update(Text.assemble(
      ("Step 2 of 2: log in\n" if change or not self.had_client else "Log in\n", "bold"),
      "Open Globus's login page (or copy its link into a browser), log in there with your institution, allow slurp, "
      "and paste the code Globus then shows into the field below. slurp keeps the login in the keyring of this "
      "machine; Globus moves the data, not slurp."))
    q("#gl-url", Static).update(Text.assemble(("The login page: ", "bold"), (url, "underline")))
    consents = len(self.login.collections)
    self.status(f"with the consent for {consents} collection{'s' if consents != 1 else ''} (their data)" if consents else "", "dim")
    q("#gl-open", Button).focus()

  def status(self, words, style=""):
    self.query_one("#gl-status", Static).update(Text(words, style=style))

  @on(Button.Pressed, "#gl-client-use")
  @on(Input.Submitted, "#gl-client")
  def client_set(self, event):
    event.stop()
    value = self.query_one("#gl-client", Input).value.strip()
    if not G.collection_id(value):
      self.status("a Client UUID looks like 01234567-89ab-cdef-0123-456789abcdef", "red")
      return
    G.set_client_id(G.collection_id(value))
    self.prepare()

  @on(Button.Pressed, "#gl-client-change")
  def client_change(self, event):
    event.stop()
    self.had_client = False
    self.prepare(change=True)

  @on(Button.Pressed, "#gl-dev-open")
  def dev_open(self, event):
    event.stop()
    self.app.open_url(self.DEVELOPERS)
    self.status(f"opened in the browser (nothing opened? Copy its link: {self.DEVELOPERS})", "dim")

  @on(Button.Pressed, "#gl-dev-copy")
  def dev_copy(self, event):
    event.stop()
    copy_text(self.app, self.DEVELOPERS, "the link of the Globus developers page")

  @on(Button.Pressed, "#gl-open")
  def open_pressed(self, event):
    event.stop()
    if self.login is not None:
      self.app.open_url(self.login.url())
      self.status("opened in the browser (nothing opened, e.g. over SSH? Copy its link into a browser)", "dim")
      self.query_one("#gl-code", Input).focus()

  @on(Button.Pressed, "#gl-copy")
  def copy_pressed(self, event):
    event.stop()
    if self.login is not None:
      copy_text(self.app, self.login.url(), "the login link")

  @on(Button.Pressed, "#gl-done")
  @on(Input.Submitted, "#gl-code")
  def done_pressed(self, event):
    event.stop()
    code = self.query_one("#gl-code", Input).value.strip()
    if self.login is None or not code:
      self.status("paste the code Globus shows after the login", "yellow")
      return
    self.status("logging in …", "yellow")
    login = self.login
    def finished(result):
      ok, why = result or (None, "?")
      if why:
        self.status(why, "red")
        return
      self.app.notify("logged in to Globus", title="Globus", timeout=5)
      self.dismiss(True)
    in_background(self, attempt(lambda: login.finish(code)), finished, group="gl-login", name="the Globus login", connection=None)

  @on(Button.Pressed, "#gl-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(False)

  def action_cancel(self):
    self.dismiss(False)


class GlobusEndpointScreen(ModalScreen):
  """A Globus endpoint (new or edited: its name, its collection, its default folder), or the link of a tab to a
  collection (tab: the tab's endpoint). Dismissed with {"definition": EndpointDef} / {"link": dict | None (removed)},
  or None"""
  DEFAULT_CSS = """
  GlobusEndpointScreen { align: center middle; }
  #ge-dialog { width: 110; max-width: 96%; height: 92%; border: thick $accent; background: $surface; padding: 1 2; }
  #ge-title { text-style: bold; margin-bottom: 1; height: auto; }
  .ge-note { color: $text-muted; height: auto; }
  .ge-row { height: auto; margin-top: 1; }
  .ge-row Label { width: 22; padding-top: 0; }
  .ge-row Input { width: 1fr; }
  .ge-row Button { margin-left: 1; }
  #ge-results { height: 8; margin-top: 1; }
  #ge-login-text { width: 1fr; height: auto; }
  #ge-chosen, #ge-test-result, #ge-suggest { height: auto; margin-top: 1; }
  #ge-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #ge-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, store, endpoint=None, tab=None, suggestion=""):
    super().__init__()
    self.store, self.endpoint, self.tab = store, endpoint, tab
    d = endpoint.definition if endpoint is not None else None
    self.link = dict(store.links.get(tab.id) or {}) if tab is not None else {}
    settings = d.settings if d is not None else self.link
    self.collection = {"id": settings.get("collection", ""), "name": settings.get("collection_name", ""),
                       "needs_consent": settings.get("needs_consent", False)} if settings.get("collection") else None
    self.suggestion = G.collection_id(suggestion) if suggestion else ""
    self.suggestion_text = suggestion
    self.found = []

  def compose(self) -> ComposeResult:
    d = self.endpoint.definition if self.endpoint is not None else None
    with Vertical(id="ge-dialog"):
      yield Static(f"Link {self.tab.name} to a Globus collection" if self.tab is not None else
                   (f"The Globus endpoint {d.name}" if d else "A new Globus endpoint"), id="ge-title")
      with VerticalScroll():
        yield Static("A Globus collection is a place Globus moves data from and to (a site's storage, a guest "
                     "collection). Globus moves the data between collections itself: not through this machine, and on "
                     "without slurp." + (" Linked to a collection, the tab's transfers to and from other collections "
                                         "can go through Globus." if self.tab is not None else ""), classes="ge-note")
        with Horizontal(classes="ge-row"):
          yield Static(id="ge-login-text")
          yield Button("Log in to Globus", id="ge-login", compact=True)
        if self.tab is None:
          with Horizontal(classes="ge-row"):
            yield Label("Name")
            yield Input(d.name if d else "", placeholder="shown everywhere, e.g. CSCS Globus", id="ge-name")
        with Horizontal(classes="ge-row"):
          yield Label("Find the collection")
          yield Input(placeholder="its name (a search at Globus), or its id", id="ge-search")
          yield Button("Search", id="ge-search-go", compact=True)
        yield ClickTable(id="ge-results", cursor_type="row", zebra_stripes=True)
        yield Static(id="ge-suggest", classes="ge-note")
        yield Static(id="ge-chosen")
        if self.tab is None:
          with Horizontal(classes="ge-row"):
            yield Label("Its folder")
            yield Input((d.settings.get("path") if d else "") or "", placeholder="where Browse… starts (empty: your home there, /~/)", id="ge-path")
        else:
          with Horizontal(classes="ge-row"):
            yield Label(f"A folder on {self.tab.name}")
            yield Input(self.link.get("tab_path", ""), placeholder="e.g. /capstor/scratch/cscs/me (empty: /)", id="ge-tab-path")
          with Horizontal(classes="ge-row"):
            yield Label("…in the collection")
            yield Input(self.link.get("collection_path", ""), placeholder="the same folder as the collection names it (empty: /)", id="ge-coll-path")
          yield Static("The paths of transfers under the folder of the tab are that folder in the collection.", classes="ge-note")
        yield Static(id="ge-test-result")
      with Horizontal(id="ge-buttons"):
        yield Button("Test", id="ge-test", compact=True, tooltip="the collection, and its folder listed")
        if self.tab is not None and self.link:
          yield Button("Remove the link", id="ge-unlink", variant="error", compact=True)
        yield Button("Cancel", id="ge-cancel", compact=True)
        yield Button("Save", id="ge-save", variant="primary", compact=True)

  def on_mount(self):
    table = self.query_one("#ge-results", ClickTable)
    for key, label in (("name", "Collection"), ("type", "Kind"), ("owner", "Owner"), ("id", "Id")):
      table.add_column(label, key=key)
    table.display = False
    if self.suggestion and not self.collection:
      self.query_one("#ge-suggest", Static).update(Text.assemble("The site suggests ", (self.suggestion_text, "bold"), ": Search finds it."))
      self.query_one("#ge-search", Input).value = self.suggestion
    elif self.suggestion_text and not self.collection:
      self.query_one("#ge-suggest", Static).update(f"The site names {self.suggestion_text} (its Globus page).")
    self.show_login()
    self.show_chosen()
    self.query_one("#ge-name" if self.tab is None else "#ge-search", Input).focus()

  def show_login(self):
    ok, why = G.available()
    text = self.query_one("#ge-login-text", Static)
    button = self.query_one("#ge-login", Button)
    if not ok:
      text.update(Text(why, style="red"))
      button.display = False
      return
    logged = G.logged_in()
    text.update(Text("● logged in to Globus", style="green") if logged else Text("○ not logged in to Globus: searching and testing need the login", style="yellow"))
    button.label = "Log in again" if logged else "Log in to Globus"

  def show_chosen(self):
    c = self.collection
    self.query_one("#ge-chosen", Static).update(
      Text.assemble(("Collection: ", "bold"), (c["name"] or c["id"], "bold cyan"), f"  {c['id']}",
                    ("  (needs your consent: the login asks for it)" if c.get("needs_consent") and c["id"] not in G.consented() else "", "yellow"))
      if c else Text("No collection chosen yet: search for it, or paste its id.", style="dim"))

  @on(Button.Pressed, "#ge-login")
  def login_pressed(self, event):
    event.stop()
    wanted = [self.collection["id"]] if self.collection and self.collection.get("needs_consent") else []
    self.app.push_screen(GlobusLoginScreen(wanted), lambda ok: self.show_login() or self.show_chosen())

  @on(Button.Pressed, "#ge-search-go")
  @on(Input.Submitted, "#ge-search")
  def search(self, event):
    event.stop()
    text = self.query_one("#ge-search", Input).value.strip()
    if not text:
      return
    result_box = self.query_one("#ge-test-result", Static)
    result_box.update(Text("asking Globus …", style="yellow"))
    def found(result):
      collections, why = result or (None, "?")
      if why:
        result_box.update(Text(why, style="red"))
        return
      result_box.update("" if collections else Text("nothing found", style="yellow"))
      self.found = collections
      table = self.query_one("#ge-results", ClickTable)
      table.clear()
      for c in collections:
        table.add_row(Text(c["name"], style="bold"), c["type"].replace("_", " "), c.get("owner", ""), c["id"], key=c["id"])
      table.display = bool(collections)
      if len(collections) == 1:
        self.choose(collections[0])
    in_background(self, attempt(lambda: G.search(G.client(), text)), found, group="ge-search", name="the Globus search", connection=None)

  @on(ClickTable.RowSelected, "#ge-results")
  def picked(self, event):
    event.stop()
    c = next((c for c in self.found if c["id"] == event.row_key.value), None)
    if c is not None:
      self.choose(c)

  def choose(self, c):
    self.collection = {"id": c["id"], "name": c["name"], "needs_consent": c.get("needs_consent", False)}
    if self.tab is None:
      name = self.query_one("#ge-name", Input)
      if not name.value.strip():
        name.value = c["name"][:40]
    self.show_chosen()

  def settings(self):
    c = self.collection or {}
    if self.tab is None:
      return {"collection": c.get("id", ""), "collection_name": c.get("name", ""), "needs_consent": c.get("needs_consent", False),
              "path": self.query_one("#ge-path", Input).value.strip()}
    return {"collection": c.get("id", ""), "collection_name": c.get("name", ""), "needs_consent": c.get("needs_consent", False),
            "tab_path": self.query_one("#ge-tab-path", Input).value.strip() or "/",
            "collection_path": self.query_one("#ge-coll-path", Input).value.strip() or "/"}

  @on(Button.Pressed, "#ge-test")
  def test(self, event):
    event.stop()
    s = self.settings()
    box = self.query_one("#ge-test-result", Static)
    if not s["collection"]:
      box.update(Text("choose a collection first", style="yellow"))
      return
    box.update(Text("asking Globus …", style="yellow"))
    folder = s.get("path") or s.get("collection_path") or "/~/"
    def work():
      tc = G.client()
      info = G.collection(tc, s["collection"])
      entries = G.ls(tc, s["collection"], folder)
      return info, len(entries)
    def tested(result):
      value, why = result or (None, "?")
      if why:
        box.update(Text(f"✗ {why}", style="red"))
        return
      info, n = value
      self.collection.update(name=info["name"], needs_consent=info["needs_consent"])
      self.show_chosen()
      box.update(Text(f"✓ {info['name']}: {folder} lists {n} entr{'ies' if n != 1 else 'y'}", style="green"))
    in_background(self, attempt(work), tested, group="ge-test", name="the Globus test", connection=None)

  @on(Button.Pressed, "#ge-save")
  def save(self, event):
    event.stop()
    s = self.settings()
    box = self.query_one("#ge-test-result", Static)
    if not s["collection"]:
      box.update(Text("choose a collection first (search, or its id)", style="yellow"))
      return
    if self.tab is not None:
      self.dismiss({"link": s})
      return
    name = self.query_one("#ge-name", Input).value.strip()
    if not name:
      box.update(Text("give it a name", style="yellow"))
      return
    d = self.endpoint.definition if self.endpoint is not None else None
    definition = E.EndpointDef(d.id if d else M.new_id(), name, "globus", s, [], d.created if d else time.time())
    if any(x.name == name and x.id != definition.id for x in self.store.defs):
      box.update(Text(f"an endpoint is called {name} already", style="yellow"))
      return
    self.dismiss({"definition": definition})

  @on(Button.Pressed, "#ge-unlink")
  def unlink(self, event):
    event.stop()
    self.dismiss({"link": None})

  @on(Button.Pressed, "#ge-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.dismiss(None)

  def action_cancel(self):
    self.dismiss(None)
