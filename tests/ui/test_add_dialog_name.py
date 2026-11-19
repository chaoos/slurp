"""A new SSH connection, as the endpoints' forms: its name first (the tab's name; empty: the destination), then the
destination; Test, Cancel, Add in that order; a name another tab has: refused; the name kept for the next start;
editing a tab: its name shown, a new one renames the tab"""
import asyncio

from slurp.config import load_config
from slurp.ui.app import Slurp
from slurp.ui.screens.dialogs import AddClusterScreen


def test_name(check, monkeypatch):
  monkeypatch.setattr(Slurp, "activate_cluster", lambda app, index, **kw: setattr(app, "cluster_index", index)) # (no login in the test)
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 45)) as pilot:
      for _ in range(100):
        if app.main is not None:
          break
        await pilot.pause(0.05)
      got = []
      app.push_screen(AddClusterScreen(), got.append); await pilot.pause(0.4)
      dlg = app.screen
      check("titled as the endpoints' forms; the name first, focused", str(dlg.query_one("#cluster-title").render()) == "A new SSH connection"
            and app.focused is dlg.query_one("#cluster-name"))
      labels = [str(b.label) for b in dlg.query("#cluster-actions Button")]
      check("Test, Cancel, Add", labels == ["Test", "Cancel", "Add"], labels)
      dlg.query_one("#cluster-name").value = app.clusters[0].name
      dlg.query_one("#cluster-destination").value = "jedi.example"
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("a name another tab has: refused (the dialog stays)", app.screen is dlg and not got
            and any("is called" in str(n.message) for n in app._notifications))
      dlg.query_one("#cluster-name").value = "JEDI booster"
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.3)
      check("Add: the destination and the name", got == [{"destination": "jedi.example", "name": "JEDI booster"}], got)
      app.add_cluster(got[0]); await pilot.pause(0.3)
      tab = app.clusters[-1]
      check("the tab: that name, that destination", tab.name == "JEDI booster" and tab.destination == "jedi.example", (tab.name, tab.destination))
      saved = [t for t in load_config().get("tabs", []) if t.get("destination") == "jedi.example"]
      check("kept for the next start", saved and saved[0].get("name") == "JEDI booster", saved)
      app.add_cluster("eiger.example"); await pilot.pause(0.3)
      check("no name: the destination", app.clusters[-1].name == "eiger.example")
      app.edit_ssh_settings(tab); await pilot.pause(0.4)
      dlg = app.screen
      check("editing: its name shown; Save", dlg.query_one("#cluster-name").value == "JEDI booster"
            and str(dlg.query_one("#cluster-confirm").label) == "Save" and str(dlg.query_one("#cluster-title").render()) == "The SSH connection JEDI booster")
      dlg.query_one("#cluster-name").value = "booster"
      dlg.query_one("#cluster-confirm").press(); await pilot.pause(0.4)
      check("a new name renames the tab", tab.name == "booster" and tab.destination == "jedi.example", tab.name)
      app.edit_ssh_settings(tab); await pilot.pause(0.4)
      app.screen.query_one("#cluster-name").value = ""
      app.screen.query_one("#cluster-confirm").press(); await pilot.pause(0.4)
      check("emptied: the destination again", tab.name == "jedi.example", tab.name)
  asyncio.run(main())
