"""The job list refreshes by itself every refresh_seconds; the title bar counts down; due but unable to start (a load
runs): it says why, not 0:00; a click switches it off (kept), and on again; views that do not refresh have no countdown"""
import asyncio
import shutil
import time

import pytest

from slurp.config import load_config
from slurp.ui.app import Slurp
from slurp.ui.screens.main import MainScreen


async def until(pilot, cond, t=8):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_the_job_list_refreshes_and_counts_down(check, monkeypatch):
  if shutil.which("squeue"):
    pytest.skip("squeue is on PATH")
  async def main():
    app = Slurp()
    monkeypatch.setattr(app, "refresh_seconds", lambda: 3)
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None and app.cluster.loader is None)
      app.show_view(None); await pilot.pause(0.5)
      reloads = []
      monkeypatch.setattr(app, "reload_cluster", lambda cluster: (reloads.append(time.time()), setattr(cluster, "updated", __import__("datetime").datetime.now())))
      app.cluster.loaded = True
      timer = app.screen.query_one("#refresh-timer")
      ok = await until(pilot, lambda: isinstance(app.screen, MainScreen) and timer.display and str(timer.label).startswith("⟳ 0:0"))
      check("the job list: the countdown in the title bar", ok, str(timer.label))
      ok = await until(pilot, lambda: reloads, 6)
      ok = ok and await until(pilot, lambda: str(timer.label) in ("⟳ 0:03", "⟳ 0:02", "⟳ 0:01"), 3)
      check("at 0: it reloads (and counts from that load again)", ok, (reloads, str(timer.label)))
      busy = object()
      app.cluster.loader = busy # (a load runs when the refresh is due: it waits, and says so)
      ok = await until(pilot, lambda: str(timer.label) == "⟳ loading", 6)
      check("due while a load runs: ⟳ loading, not 0:00", ok, str(timer.label))
      app.cluster.loader = None
      ok = await until(pilot, lambda: str(timer.label) not in ("⟳ loading", "⟳ 0:00"), 3)
      check("…the load done: it counts down again", ok, str(timer.label))
      await pilot.click("#refresh-timer"); await pilot.pause(0.3)
      n = len(reloads)
      ok = await until(pilot, lambda: str(timer.label) == "⟳ off", 3)
      await pilot.pause(4)
      check("a click: off (kept for the next start), no more reloads", ok and load_config().get("auto_refresh") is False and len(reloads) == n, (str(timer.label), len(reloads), n))
      await pilot.click("#refresh-timer"); await pilot.pause(0.3)
      ok = await until(pilot, lambda: len(reloads) > n, 6)
      check("a click again: on, it reloads again", ok and load_config().get("auto_refresh") is True)
      app.cluster.view.jobs_open = False; app.show_home(); await pilot.pause(0.6)
      check("the overview (no automatic refresh): no countdown", not any(t.display for t in app.screen.query("#refresh-timer")))
  asyncio.run(main())
