"""A new SSH connection from a site: the clusters of the site packs in a list; one fills in the form (name, host, the
jump host with the user filled in, the key, the command before the login), says what it needs here (found or not)
and what to do once; all of it stays to see and change; Add: those settings. Editing a tab: no such list"""
import asyncio

from slurp.ui.app import Slurp
from slurp.ui.screens.dialogs import AddClusterScreen


async def started(pilot, app):
  for _ in range(100):
    if app.main is not None:
      return
    await pilot.pause(0.05)


def test_from_a_site(check, monkeypatch, tmp_path):
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
      site = dlg.query_one("#cluster-site")
      options = [str(label) for label, _ in site._options]
      check("From a site: the clusters of the packs, with the site's name", "CSCS Alps · Daint" in options and "JSC JEDI · JEDI" in options
            and "JSC JUWELS · JUWELS Booster" in options, options)
      check("…the name is still focused first; no info yet", app.focused is dlg.query_one("#cluster-name") and not dlg.query_one("#cluster-site-info").display)
      site.value = "cscs-alps/daint"; await pilot.pause(0.4)
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
      dlg.query_one("#cluster-site").value = "juelich-jureca/jureca"; await pilot.pause(0.4)
      info = str(dlg.query_one("#cluster-site-info").render())
      check("JURECA: directly, no command before the login, the JuDoor steps", value("host") == "jureca.fz-juelich.de" and value("jump") == ""
            and value("pre-login") == "" and "logs in directly" in info and "JuDoor" in info and "from=" in info, info)
      await pilot.press("escape"); await pilot.pause(0.2)
      app.edit_ssh_settings(app.clusters[0]); await pilot.pause(0.4)
      check("editing a tab: no From a site", not app.screen.query("#cluster-site"))
      await pilot.press("escape"); await pilot.pause(0.2)
      await pilot.press("q")
  asyncio.run(main())


def test_fields_to_fill_in(check, monkeypatch):
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
      dlg.query_one("#cluster-site").value = "cscs-alps/daint"; await pilot.pause(0.4)
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
            and any("Test: fill in the user first" in str(n.message) for n in app._notifications), (pushed, str(hint.render())))
      dlg.query_one("#cluster-full").collapsed = True; await pilot.pause(0.2)
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("Add without it: not added; the settings open again, the user focused", not got and app.screen is dlg
            and not dlg.query_one("#cluster-full").collapsed and app.focused is user)
      user.value = "rgruber"; await pilot.pause(0.2)
      check("filled in: the mark and the hint are gone", not user.has_class("-required") and not user.has_class("-missing")
            and not hint.display and color(user) == plain, (user.classes, color(user)))
      user.value = ""; await pilot.pause(0.2)
      check("emptied again: yellow (red only after Add or Test)", user.has_class("-required") and not user.has_class("-missing") and not hint.display)
      dlg.query_one("#cluster-site").clear(); await pilot.pause(0.3)
      check("(none) chosen: no marks", not user.has_class("-required") and not dlg.query_one("#cluster-site-info").display)
      user.value = "rgruber"
      dlg.query_one("#cluster-site").value = "cscs-alps/daint"; await pilot.pause(0.3)
      check("a user already there: no mark", not user.has_class("-required"))
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("…Add: added", got and got[0]["ssh"]["user"] == "rgruber", got)
      await pilot.press("q")
  asyncio.run(main())
