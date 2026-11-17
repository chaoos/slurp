"""The rule catalogue's search: only the rules that match, while typing; Esc shows all again"""
import asyncio

from textual.widgets._markdown import MarkdownH3

from slurp.checks import engine as checks
from slurp.ui.app import Slurp
from slurp.ui.screens.checks import RuleCatalogueScreen


async def until(pilot, cond, t=8):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_search_the_rule_catalogue(check, monkeypatch):
  monkeypatch.setattr(checks, "cached_facts", lambda tab: None)
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None)
      app.action_rule_catalogue()
      await until(pilot, lambda: isinstance(app.screen, RuleCatalogueScreen))
      screen = app.screen
      shown = lambda: [(b.source or "").split()[1] for b in screen.query_one("#doc").document.children
                       if isinstance(b, MarkdownH3)] if screen.query("#doc") else []
      await until(pilot, lambda: len(shown()) == len(checks.RULES))
      await pilot.press("slash")
      check("/ goes to the search", app.focused is screen.query_one("#rc-search"))
      await pilot.press(*"uenv pulled")
      ok = await until(pilot, lambda: shown() == ["E05"])
      check("words: only the rules with all of them (uenv pulled: E05)", ok, shown())
      screen.query_one("#rc-search").value = ""; await pilot.press(*"memory")
      ok = await until(pilot, lambda: shown() and "R01" in shown() and len(shown()) < len(checks.RULES))
      check("memory: R01 among a few", ok, shown())
      summary = "".join((b.source or "") for b in screen.query_one("#doc").document.children)
      check("it says how many match", f"of {len(checks.RULES)} rules match" in summary)
      screen.query_one("#rc-search").value = ""; await pilot.press(*"zzzz")
      ok = await until(pilot, lambda: shown() == [] and "0 of" in "".join((b.source or "") for b in screen.query_one("#doc").document.children))
      check("nothing matches: said so", ok)
      await pilot.press("escape")
      ok = await until(pilot, lambda: len(shown()) == len(checks.RULES))
      check("Esc: the search cleared, all rules again, the window stays", ok and app.screen is screen
            and screen.query_one("#rc-search").value == "")
      screen.query_one("#rc-search").value = "uenv pulled"; screen.show_matches()
      await until(pilot, lambda: shown() == ["E05"])
      app.action_rule_catalogue("R01")
      ok = await until(pilot, lambda: len(shown()) == len(checks.RULES))
      check("a rule the search hides asked for (a finding's link): the search goes, all rules", ok and screen.query_one("#rc-search").value == "")
      await pilot.press("escape")
  asyncio.run(main())
