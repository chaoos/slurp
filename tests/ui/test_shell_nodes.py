"""The shell dialog of a job: its nodes as radio buttons (the one chosen first, else the first), with the keyboard;
dismissed with (where, node). Without nodes (the login node's shell) as before: where"""
import asyncio

from slurp.ui.app import Slurp
from slurp.ui.screens.links import ShellChoiceScreen


async def until(pilot, cond, t=5):
  for _ in range(int(t / 0.05)):
    if cond():
      return True
    await pilot.pause(0.05)
  return False


def test_the_node_of_a_shell(check):
  async def main():
    app = Slurp()
    async with app.run_test(size=(120, 40)) as pilot:
      await until(pilot, lambda: app.main is not None)
      got = []
      app.push_screen(ShellChoiceScreen("Shell on a node of job 7", nodes=["nid001", "nid002", "nid003"], node="nid002"), got.append)
      await pilot.pause(0.3)
      scr = app.screen
      check("the nodes, the one chosen first pressed, the list has the focus", [str(b.label) for b in scr.query("RadioButton")]
            == ["nid001", "nid002", "nid003"] and scr.node == "nid002" and app.focused is scr.query_one("#choice-nodes"))
      await pilot.press("down", "space"); await pilot.pause(0.2) # (the next one)
      check("the keys choose another one", scr.node == "nid003", scr.node)
      await pilot.click("#choice-slurp"); await pilot.pause(0.2)
      check("In slurp: (where, node)", got == [("slurp", "nid003")], got)
      app.push_screen(ShellChoiceScreen("Shell on daint"), got.append); await pilot.pause(0.3)
      check("without nodes: no list", not app.screen.query("RadioButton"))
      await pilot.click("#choice-slurp"); await pilot.pause(0.2)
      check("…and just where", got[-1] == "slurp", got)
  asyncio.run(main())
