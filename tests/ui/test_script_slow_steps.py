"""The script window: the script's text is shown as soon as it is read, its findings (the script, its paths) next; a
slow step after them (the quota where it writes, e.g. lfs quota that hangs; the environment) does not hold them back,
and says which step runs; Reload shows the new text at once"""
import asyncio
import threading

from textual.widgets import TextArea

from slurp.checks import engine as checks
from slurp.ui.app import Slurp
from slurp.ui.screens.checks import FindingsView
from slurp.ui.screens.script import ScriptScreen


async def until(pilot, cond, t=10):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_a_slow_step_does_not_hold_the_script_back(check, monkeypatch, tmp_path):
  script = tmp_path / "job.sh"
  script.write_text("#!/bin/bash\n#SBATCH --time=01:00:00\nsrun ./sim\n")
  release, asked = threading.Event(), threading.Event()
  def hanging_quota(ttl=300):
    asked.set()
    release.wait(30)
    return None
  monkeypatch.setattr(checks, "cached_quota", hanging_quota)
  monkeypatch.setattr("slurp.checks.environment.replay_environment", lambda ctx: {})
  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await until(pilot, lambda: app.main is not None)
      screen = ScriptScreen(["jobs"], str(script))
      app.push_screen(screen)
      def area():
        found = screen.query("#script-text").results(TextArea)
        return next((a.text for a in found), "")
      def summary():
        found = list(screen.query("#sc-findings").results(FindingsView))
        return str(found[0].query_one(".fv-summary").render()) if found else ""
      ok = await until(pilot, lambda: "srun ./sim" in area())
      check("the text: shown", ok, area())
      ok = await until(pilot, lambda: asked.is_set() and screen.first_shown)
      check("…the findings of the script and its paths too, while the quota is still asked", ok and screen.ctx is not None, summary())
      ok = await until(pilot, lambda: "checking the quota where it writes" in summary(), 3)
      check("…which step runs: said", ok, summary())
      script.write_text("#!/bin/bash\n#SBATCH --time=01:00:00\nsrun ./sim2\n")
      await pilot.click("#sc-reload")
      ok = await until(pilot, lambda: "srun ./sim2" in area(), 5)
      check("Reload: the new text at once (the quota still hangs)", ok, area())
      release.set()
      ok = await until(pilot, lambda: "follow" not in summary() and "checking" not in summary())
      check("the quota there: the findings complete", ok, summary())
  asyncio.run(main())
