"""The subtabs of a tab are kept for the next start: closing slurp and starting it again (once the tab is connected
and its job list loaded) opens them again, in their order, and shows the one shown before; shells are not kept"""
import asyncio
import shutil

import pytest

from slurp.config import load_config
from slurp.ui.app import Slurp
from slurp.ui.screens.checks import RuleCatalogueScreen
from slurp.ui.screens.files import DirScreen, FileScreen, open_path
from slurp.ui.screens.overview import OverviewScreen


@pytest.fixture
def home(monkeypatch, tmp_path):
  home = tmp_path / "home"; (home / "runs").mkdir(parents=True)
  (home / "runs" / "out.log").write_text("".join(f"step {i}/10\n" for i in range(10)))
  monkeypatch.setenv("HOME", str(home)); monkeypatch.setenv("PATH", "/usr/bin:/bin")
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")
  return home


async def until(pilot, cond, t=10):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def titles(app):
  return [v.title for v in app.cluster.views]


def test_subtabs_open_again_at_the_next_start(check, home):
  async def first():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and (app.cluster.loaded or app.cluster.no_slurm) and app.cluster.loader is None)
      open_path(app, ["Local"], str(home / "runs"))
      await until(pilot, lambda: isinstance(app.screen, DirScreen))
      app.action_rule_catalogue("B05")
      await until(pilot, lambda: isinstance(app.screen, RuleCatalogueScreen))
      open_path(app, ["Local"], str(home / "runs" / "out.log"))
      await until(pilot, lambda: isinstance(app.screen, FileScreen) and app.screen.file_size is not None)
      app.show_view(None); await pilot.pause(0.2) # (the job list open)
      app.show_view(app.cluster.views[2]); await pilot.pause(0.3) # (the file shown)
      return titles(app)
  before = asyncio.run(first())
  kept = (load_config().get("tabs") or [{}])[0].get("views") or {}
  check("kept with the tab: the directory, the rule catalogue, the file; which one was shown; the job list open",
        [v["type"] for v in kept.get("views", [])] == ["path", "rules", "path"] and kept.get("shown") == 2
        and kept.get("jobs_open") is True and kept["views"][1]["rule"] == "B05", kept)

  async def second():
    from slurp.ui import restore
    real_ready = restore.ready
    restore.ready = lambda app, cluster: False # (not connected yet)
    app = Slurp()
    try:
      async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.main is not None)
        await pilot.pause(0.2)
        check("at once at the start: the subtabs as placeholders, in their order (titled as they were)", [v.title for v in app.cluster.views] == before[:3]
              and all(v.pending for v in app.cluster.views), [(v.title, v.pending) for v in app.cluster.views])
        buttons = [b for b in app.screen.query("#view-tabs Button.view-tab") if b.display and b.id != "view-overview"]
        check("…in the bar, muted", [str(b.label).split()[-1] for b in buttons][-3:] == [before[0].split()[-1][:40], "catalogue", "out.log"]
              and all(b.has_class("-pending") for b in buttons[-3:]), [str(b.label) for b in buttons])
        app.show_view(None); await pilot.pause(0.2)
        app.show_view(app.cluster.views[2]); await pilot.pause(0.3)
        notes = [str(n.message) for n in app._notifications]
        check("a click before it is connected: connect first, the overview", isinstance(app.screen, OverviewScreen)
              and any("connect first" in n for n in notes), (type(app.screen).__name__, notes[-2:]))
        restore.ready = real_ready # (connected now)
        app.restore_views()
        ok = await until(pilot, lambda: not any(v.pending for v in app.cluster.views) and not app.cluster.restoring, 15)
        await pilot.pause(0.3)
        check("connected: each opened in its place, in their order", ok and titles(app) == before[:3], (titles(app), before))
        buttons = [str(b.label).split()[-1] for b in app.screen.query("#view-tabs Button.view-tab") if b.display and b.id != "view-overview"]
        check("…the bar in that order too, none muted", buttons[-3:] == [before[0].split()[-1][:40], "catalogue", "out.log"]
              and not app.screen.query("#view-tabs Button.-pending"), buttons)
        check("the one shown before is shown (the overview was)", isinstance(app.screen, FileScreen) and app.screen.path.endswith("out.log"),
              type(app.screen).__name__)
        check("the job list open again too", app.cluster.jobs_open)
        app.close_view(app.cluster.views[0]); await pilot.pause(0.3)
        return titles(app)
    finally:
      restore.ready = real_ready
  after = asyncio.run(second())
  kept = (load_config().get("tabs") or [{}])[0].get("views") or {}
  check("a subtab closed: kept so", [v["type"] for v in kept.get("views", [])] == ["rules", "path"] and len(after) == 2, kept)


def test_what_is_kept(check):
  """A shell is not kept (it ended with slurp); a subtab without a window yet neither"""
  import types
  from slurp.state import View
  from slurp.ui.restore import view_spec
  app = types.SimpleNamespace(_screen_stacks={})
  shell = View(1, "shell", "shell", session=types.SimpleNamespace(done=False))
  check("a shell: not kept", view_spec(app, shell) is None)
  check("no window: nothing", view_spec(app, View(2, "x", "window")) is None)


def test_the_start_with_placeholders_on_the_overview(check, home, start_on_overview):
  """slurp starts on the overview with kept subtabs not open yet (the default start page): it starts, shows the
  overview, and never switches to a placeholder"""
  from slurp.config import update_config
  update_config(tabs=[{"destination": None, "views": {"views": [{"type": "rules", "rule": "", "title": "Rule catalogue", "icon": "rules"},
                                                                {"type": "path", "path": str(home / "runs"), "title": "runs/", "icon": "folder"}],
                                                      "shown": 1, "jobs_open": False}}], active=0)
  from slurp.ui import restore
  real_ready = restore.ready
  restore.ready = lambda app, cluster: False
  async def main():
    app = Slurp()
    try:
      async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.main is not None); await pilot.pause(0.5)
        check("it starts: the overview, the placeholders in the bar", isinstance(app.screen, OverviewScreen)
              and [v.title for v in app.cluster.views] == ["Rule catalogue", "runs/"] and all(v.pending for v in app.cluster.views),
              type(app.screen).__name__)
        app.show_view(None); await pilot.pause(0.2)
        await pilot.press("ctrl+w"); await pilot.pause(0.3)
        check("closing the job list: the overview again, not a placeholder", isinstance(app.screen, OverviewScreen), type(app.screen).__name__)
        # (connected, with the job list of the last run kept, not loaded again, not open: that is enough)
        app.cluster.loaded, app.cluster.from_cache, app.cluster.jobs_open = True, True, False
        restore.ready = real_ready
        app.restore_views()
        ok = await until(pilot, lambda: not any(v.pending for v in app.cluster.views) and not app.cluster.restoring, 10)
        await pilot.pause(0.5) # (the one shown before is being shown)
        check("then opened, and the one shown before shown", ok and [v.title for v in app.cluster.views][0] == "Rule catalogue"
              and isinstance(app.screen, DirScreen), type(app.screen).__name__)
    finally:
      restore.ready = real_ready
  asyncio.run(main())
