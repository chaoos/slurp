"""Globus in the Transfers tab, against a fake Globus (no network; the login token in the tests' keyring in memory):
New endpoint… → Globus: the login (a client id asked for first, Globus's page, the code pasted back), the collection
searched and chosen, saved and tested (connected); a tab linked to a collection; the wizard between two Globus
endpoints (only the Globus route, its options), Add, Run now (the task submitted), Copy as a command"""
import asyncio

import pytest

from slurp.transfer import endpoints as E, globus as G, secrets as S
from slurp.ui.app import Slurp

pytest.importorskip("globus_sdk")

SRC, DST = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
NAMES = {SRC: "CSCS scratch", DST: "JSC data"}


class FakeGlobus:
  def __init__(self):
    self.trees = {SRC: {"/data": [("run1", "dir"), ("a.h5", "file")], "/~": [("data", "dir")], "/": [("data", "dir")]},
                  DST: {"/": [("in", "dir")], "/in": [], "/~": [("in", "dir")]}}
    self.submitted, self.tasks = [], {}

  def get_endpoint(self, cid):
    return {"display_name": NAMES[cid], "entity_type": "GCSv5_guest_collection"}

  def endpoint_search(self, text, limit=20):
    return [{"id": cid, "display_name": name, "entity_type": "GCSv5_guest_collection", "owner_string": "site"}
            for cid, name in NAMES.items() if text.lower() in name.lower()]

  def operation_ls(self, cid, path="/"):
    path = path.rstrip("/") or "/"
    if path not in self.trees[cid]:
      raise G.GlobusError("failed", f"NotFound: {path}")
    return [{"name": n, "type": k, "size": 1, "last_modified": "2026-11-18 10:00:00+00:00"} for n, k in self.trees[cid][path]]

  def submit_transfer(self, data):
    self.submitted.append(dict(data))
    tid = f"task-{len(self.submitted)}"
    self.tasks[tid] = {"status": "ACTIVE", "nice_status": "Queued"}
    return {"task_id": tid}

  def get_task(self, tid):
    return dict(self.tasks[tid])


async def until(pilot, cond, seconds=10):
  for _ in range(int(seconds / 0.1)):
    if cond():
      return True
    await pilot.pause(0.1)
  return cond()


def test_globus_endpoint_and_login(check, monkeypatch):
  from slurp.ui.screens.globus import GlobusEndpointScreen, GlobusLoginScreen
  from slurp.ui.screens.transfers import NewEndpointScreen
  fake = FakeGlobus()
  monkeypatch.delenv("SLURP_GLOBUS_CLIENT_ID", raising=False)
  monkeypatch.setattr(G, "client", lambda: fake)
  monkeypatch.setattr(G.Login, "finish", lambda self, code: S.put(G.TOKEN, f"token-for-{code}"))

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await app.run_action("endpoints"); await pilot.pause(0.5)
      app.screen.query_one("#ep-new").press(); await pilot.pause(0.3)
      check("New endpoint…: Globus can be made here", isinstance(app.screen, NewEndpointScreen)
            and not app.screen.query_one(".ne-globus").disabled)
      app.screen.query_one(".ne-globus").value = True; await pilot.pause(0.1)
      app.screen.query_one("#ne-next").press(); await pilot.pause(0.4)
      check("the Globus form", isinstance(app.screen, GlobusEndpointScreen))
      check("not logged in: said", "not logged in" in str(app.screen.query_one("#ge-login-text").render()))
      app.screen.query_one("#ge-login").press(); await pilot.pause(0.4)
      login = app.screen
      opened = []
      app.open_url = lambda url, **kw: opened.append(url)
      check("step 1: the app's client id first (the login itself not yet shown)", isinstance(login, GlobusLoginScreen)
            and login.query_one("#gl-step-client").display and not login.query_one("#gl-step-login").display
            and not login.query_one("#gl-done").display)
      check("…what to do, and where", "Register a thick client" in str(login.query_one("#gl-step-client Static").render()))
      login.query_one("#gl-dev-open").press(); await pilot.pause(0.2)
      check("…its page opens in the browser", opened == ["https://app.globus.org/settings/developers"], opened)
      login.query_one("#gl-client").value = "not an id"
      login.query_one("#gl-client-use").press(); await pilot.pause(0.2)
      check("a wrong client id: refused", "Client UUID" in str(login.query_one("#gl-status").render()))
      login.query_one("#gl-client").value = "0123abcd-0000-4000-8000-000000000001"
      login.query_one("#gl-client-use").press(); await pilot.pause(0.3)
      url = str(login.query_one("#gl-url").render())
      check("step 2: Globus's login page", login.query_one("#gl-step-login").display and not login.query_one("#gl-step-client").display
            and "auth.globus.org" in url and "0123abcd-0000-4000-8000-000000000001" in url, url)
      login.query_one("#gl-open").press(); await pilot.pause(0.2)
      check("Open the login page: in the browser", opened[-1].startswith("https://auth.globus.org/") and "0123abcd" in opened[-1], opened)
      check("the client id kept", G.client_id() == "0123abcd-0000-4000-8000-000000000001")
      login.query_one("#gl-code").value = "CODE"
      login.query_one("#gl-done").press()
      check("logged in: the token in the keyring", await until(pilot, lambda: G.logged_in() and isinstance(app.screen, GlobusEndpointScreen)))
      check("only in the keyring", S.get(G.TOKEN) == "token-for-CODE")
      form = app.screen
      check("the form says so", "logged in" in str(form.query_one("#ge-login-text").render()))
      form.query_one("#ge-search").value = "jsc"
      form.query_one("#ge-search-go").press()
      check("one found: chosen, and named after it", await until(pilot, lambda: form.collection and form.collection["id"] == DST)
            and form.query_one("#ge-name").value == "JSC data", form.collection)
      form.query_one("#ge-test").press()
      check("Test: the collection and its folder", await until(pilot, lambda: "✓" in str(form.query_one("#ge-test-result").render())),
            str(form.query_one("#ge-test-result").render()))
      form.query_one("#ge-save").press(); await pilot.pause(0.3)
      store = app.transfers.endpoint_store
      d = next((d for d in store.defs if d.kind == "globus"), None)
      check("saved: its collection, no secret", d is not None and d.settings["collection"] == DST and not d.secrets, d)
      check("connected (tested after saving)", await until(pilot, lambda: app.transfers.endpoint_status.get(d.id, ("",))[0] == "connected"),
            app.transfers.endpoint_status)
      await pilot.pause(1.2)
      rows = [[str(c) for c in app.screen.query_one("#ep-list").get_row_at(i)] for i in range(app.screen.query_one("#ep-list").row_count)]
      check("listed as connected", any(r[0] == "JSC data" and r[1] == "Globus" and "connected" in r[3] for r in rows), rows)
      await pilot.press("q")

  asyncio.run(main())


def test_globus_wizard_and_run(check, monkeypatch, tmp_path):
  from slurp.ui.screens.transfers import TransferScreen, TransferWizard
  fake = FakeGlobus()
  monkeypatch.setenv("SLURP_GLOBUS_CLIENT_ID", "0123abcd-0000-4000-8000-000000000001")
  monkeypatch.setattr(G, "client", lambda: fake)
  S.put(G.TOKEN, "a token")

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      store = app.transfers.endpoint_store
      store.add(E.EndpointDef("g1", "CSCS scratch", "globus", {"collection": SRC, "collection_name": "CSCS scratch"}))
      store.add(E.EndpointDef("g2", "JSC data", "globus", {"collection": DST, "collection_name": "JSC data"}))
      await app.run_action("transfer_data"); await pilot.pause(0.5)
      w = app.screen
      check("the wizard", isinstance(w, TransferWizard))
      check("Globus endpoints offered", {"g1", "g2"} <= {v for _, v in w.endpoint_options()}, w.endpoint_options())
      w.query_one("#tw-src-tab").value = "g1"
      check("chosen: tested at once, connected", await until(pilot, lambda: w.live_state("src")[0] == "connected"), w.live_state("src"))
      field = w.query_one("#tw-src-path")
      check("its paths can be typed once it is connected", await until(pilot, lambda: not field.disabled))
      field.value = "/data/run1"
      field.focus(); await pilot.press("enter"); await pilot.pause(0.2)
      check("a path of the collection", w.paths == ["/data/run1"], w.paths)
      check("not counted here (Globus)", "Globus" in str(w.query_one("#tw-src-paths").render()))
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.3)
      w.query_one("#tw-dst-tab").value = "g2"
      await until(pilot, lambda: w.live_state("dst")[0] == "connected" and not w.query_one("#tw-dst-path").disabled)
      w.query_one("#tw-dst-path").value = "/in"; await pilot.pause(0.2)
      check("the destination step complete", w.step_ok(1)[0], w.step_ok(1))
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.3)
      check("options: verify shown, compression and speed limit not", w.query_one("#tw-verify").display
            and not w.query_one("#tw-compress-row").display and not w.query_one("#tw-bwlimit-row").display)
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.3)
      check("the route: Globus, checked and chosen", await until(pilot, lambda: w.chosen_route() == "globus" and w.step_ok(3)[0]),
            (w.chosen_route(), w.plan))
      shown = [b.route for b in w.query("#tw-route RadioButton") if b.display]
      check("only the Globus route", shown == ["globus"], shown)
      check("the picture: not through this machine", "Globus" in str(w.query_one("#tw-flow").render()))
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.4)
      summary = str(w.query_one("#tw-summary").render())
      check("the summary: Globus runs it", "Globus runs it" in summary and "Globus transfer" in summary, summary)
      w.query_one("#tw-go").press(); await pilot.pause(0.6)
      t = next((t for t in app.transfers.transfers), None)
      check("added: a Globus transfer", t is not None and t.route == "globus" and t.runs_on == "globus", t and (t.route, t.runs_on))
      check("its details", isinstance(app.screen, TransferScreen))
      run = app.screen.query_one("#xd-run")
      check("Run now ready (no tab needed)", await until(pilot, lambda: not run.disabled), run.tooltip)
      card = str(app.screen.query_one("#xd-src-end").render())
      check("the source card: Globus, connected", "CSCS scratch" in card and "Globus" in card and "connected" in card, card)
      run.press()
      check("submitted to Globus", await until(pilot, lambda: t.run.get("task") == "task-1"), (t.state, t.message))
      check("its item", fake.submitted and fake.submitted[0]["DATA"][0]["destination_path"] == "/in/run1", fake.submitted)
      text = app.transfers.command_text(t)
      check("Copy as a command: globus transfer", "globus transfer" in text and f"{SRC} {DST}" in text and "--recursive /data/run1 /in/run1" in text, text)
      fake.tasks["task-1"].update(status="SUCCEEDED")
      app.transfers.followed[t.id] = 0
      check("followed to its end", await until(pilot, lambda: t.state == "done"), (t.state, t.message))
      S.delete(G.TOKEN)
      app.screen.refresh_view(); await pilot.pause(0.2)
      check("no login: Run now says so", app.screen.query_one("#xd-run").disabled and "log in to Globus" in str(app.screen.query_one("#xd-run").tooltip))
      await pilot.press("q")

  asyncio.run(main())


def test_link_a_tab(check, monkeypatch):
  """A tab linked to a collection: Edit… of the tab (its site's suggestion), the folder mapping; the list shows it"""
  from slurp.state import ClusterSession
  from slurp.ui.screens.globus import GlobusEndpointScreen
  fake = FakeGlobus()
  monkeypatch.setenv("SLURP_GLOBUS_CLIENT_ID", "0123abcd-0000-4000-8000-000000000001")
  monkeypatch.setattr(G, "client", lambda: fake)
  S.put(G.TOKEN, "a token")

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      app.clusters.append(ClusterSession("jedi", "jedi"))
      await app.run_action("endpoints"); await pilot.pause(0.5)
      e = app.endpoint_named("tab:jedi")
      app.edit_endpoint(e, "tab"); await pilot.pause(0.4)
      form = app.screen
      check("the link form of the tab", isinstance(form, GlobusEndpointScreen) and form.tab is not None)
      form.query_one("#ge-search").value = DST
      form.query_one("#ge-search-go").press()
      await until(pilot, lambda: form.collection is not None)
      form.query_one("#ge-tab-path").value = "/p/scratch/me"
      form.query_one("#ge-coll-path").value = "/"
      form.query_one("#ge-save").press(); await pilot.pause(0.5)
      link = app.transfers.endpoint_store.links.get("tab:jedi")
      check("linked: the collection and the folders", link and link["collection"] == DST and link["tab_path"] == "/p/scratch/me", link)
      await pilot.pause(1.2)
      rows = [[str(c) for c in app.screen.query_one("#ep-list").get_row_at(i)] for i in range(app.screen.query_one("#ep-list").row_count)]
      check("the list shows the link", any(r[0] == "jedi" and "Globus: JSC data" in r[2] for r in rows), rows)
      await pilot.press("q")

  asyncio.run(main())
