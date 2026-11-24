"""The overview: what a tab shows while none of its subtabs is open, at the start; the job list is a subtab like the
others, closed with its ✕ or Ctrl-W (then the overview again)"""
import asyncio
import shutil

import pytest

from slurp.ui.app import Slurp
from slurp.ui.screens.overview import OverviewScreen


@pytest.fixture
def quiet(monkeypatch, tmp_path):
  monkeypatch.setenv("PATH", str(tmp_path)) # (no Slurm here: the overview says so)
  for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    monkeypatch.setenv(var, str(tmp_path / var.lower()))
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")


def test_overview_at_the_start(check, quiet, start_on_overview):
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      for _ in range(100):
        await pilot.pause(0.05)
        if isinstance(app.screen, OverviewScreen) and app.cluster.loader is None and app.cluster.no_slurm:
          break
      await pilot.pause(0.3)
      ov = app.screen
      check("at the start no subtab is open: the overview", isinstance(ov, OverviewScreen) and not app.cluster.view.jobs_open
            and not app.cluster.view.views, type(ov).__name__)
      check("…the job list's subtab is not there", not ov.query_one("#view-jobs").display and not ov.query_one("#view-close-jobs").display)
      bar = ov.query_one("#view-tabs")
      check("the overview: the only subtab, shown, without ✕", ov.query_one("#view-overview").display
            and ov.query_one("#view-overview").variant == "primary" and not bar.query("#view-close-overview"))
      await pilot.press("ctrl+w"); await pilot.pause(0.3)
      check("Ctrl-W does not close it", isinstance(app.screen, OverviewScreen))
      card = ov.query_one("#ov-card")
      middle = ov.query_one("#ov-middle").region
      check("a card in the middle of the window", abs(card.region.x + card.region.width / 2 - (middle.x + middle.width / 2)) <= 2
            and abs(card.region.y + card.region.height / 2 - (middle.y + middle.height / 2)) <= 2, (card.region, middle))
      text = " ".join(str(w.render()) for w in ov.query("#ov-card Static") if w.display)
      check("what it is; here no Slurm, no job list to open", "Local" in text and "this machine" in text and "No Slurm available" in text
            and not ov.query_one("#ov-jobs").display, text)
      check("Quota and Health", ov.query_one("#ov-quota").display and ov.query_one("#ov-info").display)
      check("not many lines: a card, not a report", card.region.height <= 14, card.region)
      app.show_view(None); await pilot.pause(0.4)
      check("opening the job list: a subtab with ✕", app.screen is app.main and app.cluster.view.jobs_open
            and app.main.query_one("#view-jobs").display and app.main.query_one("#view-close-jobs").display)
      tabs = [b for b in app.main.query_one("#view-tabs").children if b.display]
      check("the overview stays, the leftmost subtab", tabs and tabs[0].id == "view-overview" and tabs[1].id == "view-jobs"
            and app.main.query_one("#view-overview").variant == "default", [b.id for b in tabs])
      await pilot.click("#view-overview"); await pilot.pause(0.3)
      check("a click on it: the overview, the job list stays open", isinstance(app.screen, OverviewScreen) and app.cluster.view.jobs_open)
      app.show_view(None); await pilot.pause(0.3)
      await pilot.click("#view-close-jobs"); await pilot.pause(0.4)
      check("its ✕ closes it: the overview again", isinstance(app.screen, OverviewScreen) and not app.cluster.view.jobs_open)
      await pilot.click("#ov-quota"); await pilot.pause(0.5)
      check("Quota: a window over the overview, no subtab", type(app.screen).__name__ == "QuotaScreen" and not app.cluster.view.views
            and isinstance(app.screen_stack[-2], OverviewScreen), type(app.screen).__name__)
      await pilot.press("escape"); await pilot.pause(0.3)
      check("Esc closes it: the overview again", isinstance(app.screen, OverviewScreen))
      await pilot.click("#ov-info"); await pilot.pause(0.5)
      check("Health: a window too", type(app.screen).__name__ == "ClusterInfoScreen" and not app.cluster.view.views, type(app.screen).__name__)
      await pilot.click("#ci-close"); await pilot.pause(0.3)
      check("Close closes it", isinstance(app.screen, OverviewScreen))
      app.show_view(None); await pilot.pause(0.3)
      await pilot.press("ctrl+w"); await pilot.pause(0.4)
      check("Ctrl-W closes the job list too: the overview", isinstance(app.screen, OverviewScreen) and not app.cluster.view.jobs_open,
            type(app.screen).__name__)
      await pilot.press("f10"); await pilot.pause(0.3)
      check("the menu is there too (F10)", type(app.screen).__name__ == "MenuScreen")
      items = app.screen.query_one("#menu-list")
      items.highlighted = items.get_option_index("job_list"); await pilot.press("enter"); await pilot.pause(0.5)
      check("Job list in the menu opens the job list", app.screen is app.main and app.cluster.view.jobs_open, type(app.screen).__name__)
      await pilot.press("q")
  asyncio.run(main())


def test_no_login_at_the_start(check, quiet, start_on_overview):
  """A cluster tab from the last run does not log in at the start: its overview shows Connect (no Disconnect)"""
  from slurp.config import update_config
  update_config(tabs=[{"destination": "far.invalid"}], active=0)
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      for _ in range(60):
        await pilot.pause(0.05)
        if isinstance(app.screen, OverviewScreen):
          break
      await pilot.pause(0.3)
      ov, cluster = app.screen, app.cluster
      check("the cluster tab is shown, on its overview", isinstance(ov, OverviewScreen) and cluster.destination == "far.invalid")
      check("no login at the start", cluster.connection is not None and cluster.connection.state == "new"
            and cluster.login_terminal is None and app.shell is None, cluster.connection and cluster.connection.state)
      check("the overview shows Connect, not Disconnect", ov.query_one("#ov-connect-row").display
            and not ov.query_one("#ov-disconnect-row").display and not ov.query_one("#ov-links").display)
      text = str(ov.query_one("#ov-ssh").render())
      check("…with what happened to the connection", "Not connected to far.invalid" in text and "never connected" in text, text)
      await pilot.press("q")
  asyncio.run(main())
