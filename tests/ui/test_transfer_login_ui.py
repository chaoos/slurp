"""How a direct route logs in, in the wizard (the route step): the choice (by itself, a temporary key, your login now)
with what the check found, what each means (rrsync or not), the flow picture says it, your login: no transfer job, Log
in now; in the details: Establish connection, Disconnect, Run now logs in first"""
import asyncio
import time

from slurp.state import ClusterSession
from slurp.transfer import model as M
from slurp.transfer.manager import Manager
from slurp.ui.app import Slurp
from slurp.ui.screens.transfers import TransferScreen, TransferWizard


async def until(pilot, cond, t=5):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def direct(app, tmp_path, **kw):
  jedi = ClusterSession("jedi", "jedi")
  app.clusters.append(jedi)
  plan = {"done": True, "steps": {}, "remaining_bytes": 1000, "remaining_files": 2, "recommended": None,
          "routes": {"direct-push": {"ok": False, "why": "u@jedi: Permission denied (publickey,keyboard-interactive).",
                                     "keyable": True, "askable": True, "rrsync": ""}}}
  t = M.Transfer(M.new_id(), "to jedi", {"endpoint": "local", "tab": M.tab_ref(app.clusters[0]), "paths": [str(tmp_path)]},
                 {"endpoint": "tab:jedi", "name": "jedi", "tab": M.tab_ref(jedi), "path": "/scratch/in"},
                 route="direct-push", runs_on="detached", plan=plan, **kw)
  app.transfers.add(t)
  return t


def test_wizard_login_choice(check, tmp_path, monkeypatch):
  asked = []
  monkeypatch.setattr("slurp.ui.screens.transfers.open_login", lambda app, t, then=None: asked.append((t.route, dict(t.options))))
  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 50)) as pilot:
      await pilot.pause(0.5)
      t = direct(app, tmp_path, options={"temp_key": True}) # (as an older transfer kept it: the temporary key)
      app.edit_transfer(t); await pilot.pause(0.6)
      w = app.screen
      w.query_one("#tw-goto-3").press(); await pilot.pause(0.6)
      box = w.query_one("#tw-login-box")
      label = lambda login: str(w.query_one(f".tw-l-{login}").label)
      check("a direct route: how it logs in, under the routes, with the names", box.display and "jedi" in label("you")
            and "logs in to jedi by itself" in label("own") and "✗" in label("own"), [label(x) for x in M.LOGINS])
      check("editing: the login it has (an older transfer's temporary key)", w.chosen_login() == "key", w.chosen_login())
      warning = str(w.query_one("#tw-key-warning").render())
      check("…what it means: made on the cluster, only its public half on the other, never your own keys; no rrsync there: said",
            "never leaves" in warning and "authorized_keys" in warning and "never copies your own keys" in warning
            and "has no rrsync" in warning, warning)
      flow = str(w.query_one("#tw-flow").render())
      check("…the flow says it: the key goes to the other cluster", "push · key" in flow and "temporary key ──▶ jedi" in flow, flow)
      t.plan["routes"]["direct-push"]["rrsync"] = "/usr/bin/rrsync"
      w.plan = dict(t.plan)
      await w.route_changed(); await pilot.pause(0.3)
      warning = str(w.query_one("#tw-key-warning").render())
      check("with rrsync there: the key only writes to the destination", "may only write to /scratch/in" in warning, warning)
      check("…the flow too", "for rsync only in /scratch/in" in str(w.query_one("#tw-flow").render()))
      w.query_one(".tw-l-you").scroll_visible(animate=False); await pilot.pause(0.2)
      await pilot.click(w.query_one(".tw-l-you")); await pilot.pause(0.5)
      check("your login chosen: Log in now; no transfer job (it runs without you)", w.chosen_login() == "you"
            and w.query_one("#tw-login-row").display and not w.query(".tw-o-job"), [b.kind for b in w.query("#tw-runs-on RadioButton")])
      warning = str(w.query_one("#tw-key-warning").render())
      check("…what it means: answered in a terminal, nothing kept, open until unused, who could use it",
            "keeps no password" in warning and "2h unused" in warning and "root on" in warning, warning)
      flow = str(w.query_one("#tw-flow").render())
      check("…the flow says it", "push · your login" in flow and "password, code ──▶ jedi" in flow, flow)
      check("a route that would ask: it can be chosen with your login", w.how_ok()[0], w.how_ok())
      w.query_one("#tw-login-now").press(); await pilot.pause(0.3)
      check("Log in now: for this route and login", asked == [("direct-push", {"login": "you"})], asked)
      save = w.query_one("#tw-store")
      await until(pilot, lambda: not save.disabled)
      save.press(); await pilot.pause(0.5)
      check("saved: your login", t.options.get("login") == "you" and M.login_of(t) == "you" and "temp_key" not in t.options, t.options)
      await pilot.press("q")
  asyncio.run(main())


def test_details_establish(check, tmp_path, monkeypatch):
  asked, ran, closed = [], [], []
  state = [(False, "")]
  def login_state(manager, t, done):
    manager.logins[t.id] = (state[0], time.monotonic())
    done(state[0])
  monkeypatch.setattr(Manager, "login_state", login_state)
  monkeypatch.setattr(Manager, "run_now", lambda manager, t: ran.append(t.id))
  monkeypatch.setattr(Manager, "close_login", lambda manager, t, done=None: (closed.append(t.id), done and done()))
  monkeypatch.setattr("slurp.ui.screens.transfers.open_login", lambda app, t, then=None: asked.append(t.id))
  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 50)) as pilot:
      await pilot.pause(0.5)
      t = direct(app, tmp_path, options={"login": "you"})
      app.open_transfer(t); await pilot.pause(0.8)
      s = app.screen
      route = str(s.query_one("#xd-route").render())
      check("the details: your login not open, Establish connection", isinstance(s, TransferScreen) and s.query_one("#xd-login").display
            and not s.query_one("#xd-logout").display and "not open" in route and "your login" in route, route)
      s.query_one("#xd-login").press(); await pilot.pause(0.3)
      check("Establish connection: the login", asked == [t.id], asked)
      await pilot.press("s"); await pilot.pause(0.4)
      check("Run now while it is not open: the login first (no run yet)", asked == [t.id, t.id] and not ran, (asked, ran))
      state[0] = (True, "login03")
      app.transfers.logins.pop(t.id, None)
      await pilot.pause(1.5)
      route = str(s.query_one("#xd-route").render())
      check("open: on its login node; Disconnect", "open on login03" in route and s.query_one("#xd-logout").display
            and not s.query_one("#xd-login").display, route)
      await pilot.press("s"); await pilot.pause(0.4)
      check("Run now while it is open: the run", ran == [t.id] and len(asked) == 2, (asked, ran))
      s.query_one("#xd-logout").press(); await pilot.pause(0.3)
      check("Disconnect", closed == [t.id])
      await pilot.press("q")
  asyncio.run(main())
