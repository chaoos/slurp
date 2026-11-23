"""A new SSH connection from a site: the clusters of the site packs in a list; one fills in the form (name, host, the
jump host with the user filled in, the key, the command before the login), says what it needs here (found or not)
and what to do once; all of it stays to see and change; Add: those settings. Editing a tab: no such list"""
import asyncio

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.dialogs import AddClusterScreen, SiteClusterScreen


@pytest.fixture
def home(monkeypatch, tmp_path):
  """A home of the test with ~/.ssh/cscs-key (the key the CSCS clusters name: it must be there)"""
  ssh = tmp_path / "home" / ".ssh"
  ssh.mkdir(parents=True)
  (ssh / "cscs-key").write_text("PRIVATE KEY CONTENT")
  (ssh / "cscs-key.pub").write_text("ssh-ed25519 AAAA")
  monkeypatch.setenv("HOME", str(tmp_path / "home"))
  return tmp_path / "home"


async def started(pilot, app):
  for _ in range(100):
    if app.main is not None:
      return
    await pilot.pause(0.05)


def test_from_a_site(check, monkeypatch, tmp_path, home):
  bin_dir = tmp_path / "bin" # (here: cscs-key not installed; a PATH without it)
  bin_dir.mkdir()
  monkeypatch.setenv("PATH", str(bin_dir))
  monkeypatch.setattr(Slurp, "activate_cluster", lambda app, index, **kw: setattr(app, "cluster_index", index))
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 50)) as pilot:
      await started(pilot, app)
      got = []
      app.push_screen(AddClusterScreen(), got.append); await pilot.pause(0.4)
      dlg = app.screen
      check("From a site: none yet, Choose… (no Clear); the name is still focused first; no info yet",
            "none" in str(dlg.query_one("#cluster-site").render()) and dlg.query_one("#cluster-site-choose").display
            and not dlg.query_one("#cluster-site-clear").display
            and app.focused is dlg.query_one("#cluster-name") and not dlg.query_one("#cluster-site-info").display)
      await pilot.click("#cluster-site-choose"); await pilot.pause(0.4)
      win = app.screen
      check("Choose…: the search window, the search focused, all the clusters of the packs",
            isinstance(win, SiteClusterScreen) and app.focused is win.query_one("#sc-search")
            and {"cscs-alps/daint", "juelich-jedi/jedi", "juelich-juwels/juwels-booster", "lumi/lumi", "cineca-leonardo/leonardo"}
            <= {c["id"] for c in win.shown}, [c["id"] for c in getattr(win, "shown", [])])
      await pilot.press(*"jülich")
      await pilot.pause(0.2)
      ids = [c["id"] for c in win.shown]
      check("a search: by the site (jülich, as juelich or julich too)", ids and all(i.startswith("juelich-") for i in ids) and len(ids) == 5, ids)
      win.query_one("#sc-search").value = "booster juwels"; await pilot.pause(0.2)
      check("…every word must match, in any order", [c["id"] for c in win.shown] == ["juelich-juwels/juwels-booster"], [c["id"] for c in win.shown])
      win.query_one("#sc-search").value = "alps.cscs"; await pilot.pause(0.2)
      check("…by the host", {c["id"] for c in win.shown} == {"cscs-alps/daint", "cscs-alps/santis", "cscs-alps/clariden", "cscs-alps/eiger"})
      win.query_one("#sc-search").value = "nothing-like-it"; await pilot.pause(0.2)
      check("…nothing: said, Choose off", not win.shown and win.query_one("#sc-ok").disabled and "no cluster matches" in str(win.query_one("#sc-info").render()))
      win.query_one("#sc-search").value = "cscs"; await pilot.pause(0.2)
      first = win.current()["id"]
      await pilot.press("down"); await pilot.pause(0.1)
      second = win.current()["id"]
      info = str(win.query_one("#sc-info").render())
      check("↓ while typing: the next one, how it logs in below", first != second and second.startswith("cscs-alps/")
            and "through the jump host ela.cscs.ch" in info, (first, second))
      win.query_one("#sc-search").value = "daint"; await pilot.pause(0.2)
      await pilot.press("enter"); await pilot.pause(0.4)
      check("Enter: taken, back in the form, said there with Clear", app.screen is dlg and "Daint" in str(dlg.query_one("#cluster-site").render())
            and dlg.query_one("#cluster-site-clear").display)
      value = lambda key: dlg.query_one(f"#ssh-{key}").value
      check("Daint: the form filled in (the full settings open), the user asked", dlg.query_one("#cluster-name").value == "daint"
            and value("host") == "daint.alps.cscs.ch" and value("jump") == "ela.cscs.ch" and value("identities") == "~/.ssh/cscs-key"
            and value("pre-login") == "cscs-key sign --headless" and value("pre-login-fallback") is True
            and not dlg.query_one("#cluster-full").collapsed and app.focused is dlg.query_one("#ssh-user")
            and dlg.query_one("#ssh-user").placeholder == "your CSCS username", [value(k) for k in ("host", "jump", "identities")])
      info = str(dlg.query_one("#cluster-site-info").render())
      check("…what it is, the pack's version, what is missing here and where to get it, what to do once",
            dlg.query_one("#cluster-site-info").display and "CSCS Alps" in info and "site pack 2026." in info
            and "through the jump host ela.cscs.ch" in info and "✖ cscs-key is not installed here" in info
            and "docs.cscs.ch/access/ssh" in info and "ssh-keygen -t ed25519 -f ~/.ssh/cscs-key" in info, info)
      dlg.query_one("#ssh-user").value = "rgruber"; await pilot.pause(0.2)
      check("the user: the jump host logs in as that user too", value("jump") == "rgruber@ela.cscs.ch", value("jump"))
      dlg.query_one("#ssh-user").value = "rg"; await pilot.pause(0.2)
      check("…and follows it", value("jump") == "rg@ela.cscs.ch", value("jump"))
      dlg.query_one("#ssh-jump").value = "rg@ela2.cscs.ch"; await pilot.pause(0.1)
      dlg.query_one("#ssh-user").value = "rgruber"; await pilot.pause(0.2)
      check("…not when you changed the jump host yourself", value("jump") == "rg@ela2.cscs.ch", value("jump"))
      dlg.query_one("#ssh-jump").value = "rgruber@ela.cscs.ch"
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("Add: what the form shows (nothing else of the pack)", got == [{"ssh": {"name": "daint", "user": "rgruber", "host": "daint.alps.cscs.ch",
            "jump": "rgruber@ela.cscs.ch", "identities": ["~/.ssh/cscs-key"]}, "pre_login": "cscs-key sign --headless",
            "pre_login_fallback": True}], got)
      app.push_screen(AddClusterScreen(), got.append); await pilot.pause(0.4)
      dlg = app.screen
      dlg.site_chosen("juelich-jureca/jureca"); await pilot.pause(0.4)
      info = str(dlg.query_one("#cluster-site-info").render())
      check("JURECA: directly, no command before the login, the JuDoor steps", value("host") == "jureca.fz-juelich.de" and value("jump") == ""
            and value("pre-login") == "" and "logs in directly" in info and "JuDoor" in info and "from=" in info, info)
      await pilot.press("escape"); await pilot.pause(0.2)
      app.edit_ssh_settings(app.clusters[0]); await pilot.pause(0.4)
      check("editing a tab: no From a site", not app.screen.query("#cluster-site"))
      await pilot.press("escape"); await pilot.pause(0.2)
      await pilot.press("q")
  asyncio.run(main())


def test_fields_to_fill_in(check, monkeypatch, home):
  monkeypatch.setattr(Slurp, "activate_cluster", lambda app, index, **kw: setattr(app, "cluster_index", index))
  pushed = []
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 50)) as pilot:
      await started(pilot, app)
      got = []
      app.push_screen(AddClusterScreen(), got.append); await pilot.pause(0.4)
      dlg = app.screen
      user, hint = dlg.query_one("#ssh-user"), dlg.query_one("#ssh-user-hint")
      from textual.color import Color
      color = lambda w: w.styles.border_left[1]
      theme = lambda name: Color.parse({"warning": "ansi_yellow", "error": "ansi_red"}[name]) # (the terminal's, in every theme)
      plain = color(user)
      dlg.site_chosen("cscs-alps/daint"); await pilot.pause(0.4)
      check("a site's cluster: the user (required by its pack) has a yellow border, no hint yet", user.has_class("-required")
            and not user.has_class("-missing") and color(user) == theme("warning") and not hint.display,
            (user.classes, color(user)))
      check("…the other fields: as usual", not dlg.query_one("#ssh-host").has_class("-required") and not dlg.query_one("#ssh-jump").has_class("-required"))
      check("…as high as without it (a bar at each end, no lines above and below), its text where it was",
            user.region.height == 1 and dlg.query_one("#ssh-user-row").region.height == 1 and user.region.x == dlg.query_one("#ssh-host").region.x
            and user.styles.border_left[1] == theme("warning") and user.styles.border_right[1] == theme("warning"), (user.region, dlg.query_one("#ssh-host").region))
      monkeypatch.setattr(app, "push_screen", lambda screen, *a, **k: pushed.append(type(screen).__name__))
      dlg.query_one("#cluster-check-button").press(); await pilot.pause(0.3)
      check("Test without it: no check; red, the hint below it, said", not pushed and user.has_class("-missing")
            and color(user) == theme("error") and user.region.height == 1 and hint.display
            and str(hint.render()) == "↑ fill in your CSCS username: CSCS Alps needs it"
            and any("Test: fill in the user (marked red). Test anyway: press it again" in str(n.message) for n in app._notifications), (pushed, str(hint.render())))
      buttons = lambda: [(str(b.label), b.variant) for b in (dlg.query_one("#cluster-check-button"), dlg.query_one("#cluster-confirm"))]
      check("…the buttons: Test anyway, Add anyway, red", buttons() == [("Test anyway", "error"), ("Add anyway", "error")], buttons())
      dlg.query_one("#cluster-full").collapsed = True; await pilot.pause(0.2)
      dlg.query_one("#cluster-check-button").press(); await pilot.pause(0.3)
      check("Test anyway: the check (the command before the login may make what is missing)", pushed == ["SshCheckScreen"], pushed)
      user.value = "rgruber"; await pilot.pause(0.2)
      check("filled in: the mark and the hint are gone, the buttons as they were", not user.has_class("-required") and not user.has_class("-missing")
            and not hint.display and color(user) == plain and buttons() == [("Test", "default"), ("Add", "primary")], (user.classes, color(user), buttons()))
      user.value = ""; await pilot.pause(0.2)
      check("emptied again: yellow (red only after Add or Test)", user.has_class("-required") and not user.has_class("-missing") and not hint.display)
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("Add without it: not added yet; the settings open again, the user focused; Add anyway", not got and app.screen is dlg
            and not dlg.query_one("#cluster-full").collapsed and app.focused is user and buttons()[1] == ("Add anyway", "error"))
      dlg.query_one("#cluster-site-clear").press(); await pilot.pause(0.3)
      check("Clear: no site, no marks, the buttons as they were", not user.has_class("-required") and not dlg.query_one("#cluster-site-info").display
            and buttons() == [("Test", "default"), ("Add", "primary")], buttons())
      user.value = "rgruber"
      dlg.site_chosen("cscs-alps/daint"); await pilot.pause(0.3)
      check("a user already there: no mark", not user.has_class("-required"))
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("…Add: added", got and got[0]["ssh"]["user"] == "rgruber", got)
      await pilot.press("q")
  asyncio.run(main())


def test_identity_files(check, monkeypatch, home):
  """An identity file should be on this machine: one that is not there (or a directory, or the .pub half) is red at
  once, said below it; Add and Test say so first, then Add anyway. Browse…: a private key of this machine, its
  content not shown"""
  monkeypatch.setattr(Slurp, "activate_cluster", lambda app, index, **kw: setattr(app, "cluster_index", index))
  ssh = home / ".ssh"
  read = []
  import slurp.ui.screens.files as F
  monkeypatch.setattr(F, "fs_read", lambda *a, **k: read.append(a) or b"")
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 50)) as pilot:
      await started(pilot, app)
      got = []
      app.push_screen(AddClusterScreen(), got.append); await pilot.pause(0.4)
      dlg = app.screen
      dlg.query_one("#cluster-full").collapsed = False
      dlg.query_one("#ssh-host").value = "h.example"
      field, hint = dlg.query_one("#ssh-identities"), dlg.query_one("#ssh-identities-hint")
      field.value = "~/.ssh/nokey"; await pilot.pause(0.3)
      check("an identity file that is not there: red at once (no site needed), one line high, said below it",
            field.has_class("-missing") and field.region.height == 1 and hint.display
            and str(hint.render()) == "↑ ~/.ssh/nokey does not exist (here, on this machine)", str(hint.render()))
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("…Add: not yet, said; Add anyway", not got and any("~/.ssh/nokey does not exist" in str(n.message) for n in app._notifications)
            and str(dlg.query_one("#cluster-confirm").label) == "Add anyway")
      field.value = "~/.ssh/cscs-key.pub"; await pilot.pause(0.3)
      check("the .pub half: red, the private key named", field.has_class("-missing") and "choose the private key (~/.ssh/cscs-key)" in str(hint.render()))
      field.value = "~/.ssh"; await pilot.pause(0.3)
      check("a directory: red", field.has_class("-missing") and "is a directory" in str(hint.render()))
      field.value = "~/.ssh/cscs-key"; await pilot.pause(0.3)
      check("there: no mark", not field.has_class("-missing") and not field.has_class("-required") and not hint.display)
      field.value = "~/.ssh/nokey"; await pilot.pause(0.2)
      dlg.query_one("#ssh-identities-browse").press(); await pilot.pause(0.8)
      picker = app.screen
      check("Browse…: this machine's files, in ~/.ssh", isinstance(picker, F.FilePickScreen) and picker.local and picker.path == str(ssh), getattr(picker, "path", None))
      names = [str(r[0]) for r in (picker.query_one("#fp-list").get_row_at(i) for i in range(picker.query_one("#fp-list").row_count))]
      check("…its files listed", "cscs-key" in names and "cscs-key.pub" in names, names)
      picker.query_one("#fp-list").move_cursor(row=names.index("cscs-key")); await pilot.pause(0.4)
      box = str(picker.query_one("#fp-preview").render())
      check("…the key's content: not read, not shown", "PRIVATE" not in box and "not shown" in box and not read, (box, read))
      picker.query_one("#fp-ok").press(); await pilot.pause(0.4)
      check("chosen: it replaces the file that was not there (~ for home); the mark goes", app.screen is dlg and field.value == "~/.ssh/cscs-key"
            and not field.has_class("-missing"), field.value)
      dlg.query_one("#ssh-identities-browse").press(); await pilot.pause(0.8)
      app.screen.query_one("#fp-list").move_cursor(row=names.index("cscs-key.pub")); await pilot.pause(0.2)
      app.screen.query_one("#fp-ok").press(); await pilot.pause(0.4)
      check("another: added (more keys: commas); the .pub: red", field.value == "~/.ssh/cscs-key, ~/.ssh/cscs-key.pub" and field.has_class("-missing"), field.value)
      field.value = "~/.ssh/later-key"; await pilot.pause(0.2)
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("a key that is not there yet (the command before the login may make it): Add, then Add anyway",
            not got and str(dlg.query_one("#cluster-confirm").label) == "Add anyway")
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("…added, with that key", got and got[0]["ssh"]["identities"] == ["~/.ssh/later-key"], got)
      await pilot.press("q")
  asyncio.run(main())


def test_the_user_in_the_command_before_the_login(check, monkeypatch, home, tmp_path):
  import os
  rules = os.path.join(os.environ["XDG_CONFIG_HOME"], "slurp", "rules")
  os.makedirs(rules, exist_ok=True)
  with open(os.path.join(rules, "proxy.toml"), "w") as f:
    f.write('name = "Proxy site"\nmatch = ["proxy*"]\n[login]\nrequired = ["user"]\npre_login = "sshproxy.sh -u {user}"\n'
            '[[clusters]]\nname = "proxy1"\nhost = "login.proxy.example"\n')
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 50)) as pilot:
      await started(pilot, app)
      app.push_screen(AddClusterScreen()); await pilot.pause(0.4)
      dlg = app.screen
      dlg.site_chosen("proxy/proxy1"); await pilot.pause(0.3)
      pre = dlg.query_one("#ssh-pre-login")
      check("a command with {user}: shown so, until the user is there", pre.value == "sshproxy.sh -u {user}", pre.value)
      dlg.query_one("#ssh-user").value = "me"; await pilot.pause(0.2)
      check("…the user filled in: in the command", pre.value == "sshproxy.sh -u me", pre.value)
      dlg.query_one("#ssh-user").value = "me2"; await pilot.pause(0.2)
      check("…it follows the user", pre.value == "sshproxy.sh -u me2", pre.value)
      pre.value = "sshproxy.sh -u me2 -c other"; await pilot.pause(0.1)
      dlg.query_one("#ssh-user").value = "me3"; await pilot.pause(0.2)
      check("…not when you changed it yourself", pre.value == "sshproxy.sh -u me2 -c other", pre.value)
  asyncio.run(main())
