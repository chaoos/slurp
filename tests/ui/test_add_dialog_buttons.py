"""The add dialog keeps Test, Cancel and Add in view, also on a small terminal with the full settings unfolded and
much in them"""
import asyncio

from slurp.ui.app import Slurp
from slurp.ui.screens.dialogs import AddClusterScreen


def test_the_buttons_stay_in_view(check):
  async def main():
    app = Slurp()
    async with app.run_test(size=(100, 24)) as pilot:
      for _ in range(100):
        if app.main is not None:
          break
        await pilot.pause(0.05)
      app.push_screen(AddClusterScreen()); await pilot.pause(0.3)
      dlg = app.screen
      dlg.query_one("#cluster-full").collapsed = False
      dlg.query_one("#ssh-options").load_text("\n".join(f"SetEnv X{i}=1" for i in range(40)))
      await pilot.pause(0.5)
      screen = app.screen.region
      for button in ("#cluster-cancel", "#cluster-check-button", "#cluster-confirm"):
        region = dlg.query_one(button).region
        check(f"{button} in view", region.height > 0 and region.y + region.height <= screen.height, (region, screen))
  asyncio.run(main())
