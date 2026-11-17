"""The subtabs of a tab are kept for the next start: closing slurp and starting it again (once the tab is connected
and its job list loaded) opens them again, in their order, and shows the one shown before; shells are not kept"""
import asyncio
import shutil

import pytest

from slurp.config import load_config
from slurp.ui.app import Slurp
from slurp.ui.screens.checks import RuleCatalogueScreen
from slurp.ui.screens.files import DirScreen, FileScreen, open_path


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
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      ok = await until(pilot, lambda: len(app.cluster.views) == 3 and not app.cluster.restoring, 15)
      await pilot.pause(0.3)
      check("the next start: the subtabs again, in their order", ok and titles(app) == before[:3], (titles(app), before))
      check("the one shown before is shown", isinstance(app.screen, FileScreen) and app.screen.path.endswith("out.log"),
            type(app.screen).__name__)
      check("the job list open again too, the overview first", app.cluster.jobs_open
            and [b.id for b in app.screen.query("#view-tabs Button.view-tab") if b.display][:2] == ["view-overview", "view-jobs"])
      check("opened once: nothing left to open", app.cluster.pending_views is None)
      app.close_view(app.cluster.views[0]); await pilot.pause(0.3)
      return titles(app)
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
