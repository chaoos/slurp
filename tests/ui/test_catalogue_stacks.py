"""The rule catalogue greys out the rules that do not apply on the tab's cluster (a uenv cluster: the module rules)"""
import asyncio

from textual.widgets._markdown import MarkdownH3

from slurp.checks import engine as checks
from slurp.checks.engine import Facts
from slurp.ui.app import Slurp
from slurp.ui.screens.checks import RuleCatalogueScreen


async def until(pilot, cond, t=8):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_inactive_rules_greyed_out(check, monkeypatch):
  monkeypatch.setattr(checks, "cached_facts", lambda tab: Facts(stacks={"uenv"}))
  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 40)) as pilot:
      await until(pilot, lambda: app.main is not None)
      app.action_rule_catalogue("E01")
      ok = await until(pilot, lambda: isinstance(app.screen, RuleCatalogueScreen) and app.screen.query(".-inactive-rule"))
      blocks = list(app.screen.query_one("#doc").document.children)
      heading = lambda rid: next(b for b in blocks if isinstance(b, MarkdownH3) and (b.source or "").startswith(f"### {rid} "))
      after = lambda rid: blocks[blocks.index(heading(rid)) + 2] # (its paragraph after the severity line)
      check("E01 (modules) greyed out, its text too", ok and heading("E01").has_class("-inactive-rule") and after("E01").has_class("-inactive-rule"))
      check("E04 (modules) greyed out", heading("E04").has_class("-inactive-rule"))
      check("E06 (uenv) and S01 (all) not", not heading("E06").has_class("-inactive-rule") and not heading("S01").has_class("-inactive-rule"))
      from textual.widgets import Tree
      nodes = []
      def walk(node):
        for child in node.children:
          nodes.append(child); walk(child)
      walk(app.screen.query_one("#doc").query_one(Tree).root)
      entry = lambda rid: next(n for n in nodes if f" {rid} " in str(n.label))
      check("in the table of contents: E01 dimmed, E06 not", "dim" in str(entry("E01").label.style) and "dim" not in str(entry("E06").label.style),
            (entry("E01").label, entry("E06").label))
      await pilot.press("escape")
  asyncio.run(main())
