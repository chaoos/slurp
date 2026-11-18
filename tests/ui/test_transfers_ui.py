"""The Transfers tab and the wizard, with both ends this machine: Menu → Transfer data… opens the wizard; its six steps
(source, destination, the check, how, when, summary) and Go; the transfer runs, the Transfers tab appears (left of the
tabs) with the list and a subtab for the transfer; cluster keys do nothing there; Ctrl-W closes a transfer's subtab, not
the list; its subtabs are there again after a new start; deleting the last transfer takes the tab away"""
import asyncio
import os
import shutil

import pytest
from textual.widgets import RadioButton

from slurp.ui.app import Slurp
from slurp.ui.screens.transfers import TransferScreen, TransfersScreen, TransferWizard

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")


async def until(pilot, cond, seconds=20):
  for _ in range(int(seconds / 0.1)):
    if cond():
      return True
    await pilot.pause(0.1)
  return cond()


def files(tmp_path):
  src, dst = tmp_path / "data" / "run42", tmp_path / "dest"
  (src / "sub").mkdir(parents=True)
  (src / "sub" / "a.bin").write_bytes(os.urandom(500_000))
  (src / "b.txt").write_text("hello")
  dst.mkdir()
  return src, dst


def test_wizard_and_tab(check, tmp_path):
  src, dst = files(tmp_path)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      check("no transfers: no Transfers tab", not app.screen.query("#transfers-tab"))
      from slurp.ui.screens.menu import ITEMS
      check("the menu has Transfer data… (T)", any(i and i[0] == "transfer_data" and i[2] == "T" for i in ITEMS))
      await pilot.press("T"); await pilot.pause(0.4)
      w = app.screen
      check("T: the wizard", isinstance(w, TransferWizard))
      check("step 1: Next waits for a source", w.query_one("#tw-next").disabled)
      w.query_one("#tw-src-path").value = str(src)
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.2)
      check("a path typed: added", w.paths == [str(src)] and not w.query_one("#tw-next").disabled, w.paths)
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      check("step 2: the destination", w.step == 1 and w.query_one("#tw-next").disabled)
      w.query_one("#tw-dst-path").value = str(dst); await pilot.pause(0.2)
      preview = str(w.query_one("#tw-dst-preview").render())
      check("what goes where", f"run42  →  Local:{dst}/run42" in preview, preview)
      await pilot.click("#tw-next")
      check("step 3: the check, until it is done", await until(pilot, lambda: w.plan.get("done"), 30), w.plan)
      steps = str(w.query_one("#tw-check-steps").render())
      check("it says what remains", "500.0 KiB in 2 files to copy" in steps or "488.3 KiB in 2 files" in steps, steps)
      routes = str(w.query_one("#tw-routes").render())
      check("the routes, the same filesystem recommended", "same filesystem" in routes and "(recommended)" in routes, routes)
      w.plan["routes"]["same-fs"]["runs_on"] = ["detached", "job"] # (as where the site has a transfer partition)
      await pilot.click("#tw-next"); await pilot.pause(0.4)
      check("step 4: the recommended route, where it runs (a transfer job, where there is one)",
            w.chosen_route() == "same-fs" and w.chosen_runs_on() == "job", (w.chosen_route(), w.chosen_runs_on()))
      def pressed(sel):
        return [(b.kind if sel == "#tw-runs-on" else b.route) for b in w.query(f"{sel} RadioButton").results(RadioButton) if b.value]
      await pilot.click(w.query_one(".tw-o-detached")); await pilot.pause(0.3)
      check("a click on another choice: only that one is pressed", pressed("#tw-runs-on") == ["detached"], pressed("#tw-runs-on"))
      await pilot.click(w.query_one(".tw-o-job")); await pilot.pause(0.3)
      check("…and back", pressed("#tw-runs-on") == ["job"], pressed("#tw-runs-on"))
      await pilot.click(w.query_one(".tw-r-relay")); await pilot.pause(0.4)
      check("another route: only it, and its choices (in slurp)", pressed("#tw-route") == ["relay"] and pressed("#tw-runs-on") == ["slurp"],
            (pressed("#tw-route"), pressed("#tw-runs-on")))
      await pilot.click(w.query_one(".tw-r-same-fs")); await pilot.pause(0.4)
      check("the route back: one choice pressed", pressed("#tw-route") == ["same-fs"] and len(pressed("#tw-runs-on")) == 1,
            (pressed("#tw-route"), pressed("#tw-runs-on")))
      await pilot.click(w.query_one(".tw-o-detached")); await pilot.pause(0.3)
      check("and it can still be changed", pressed("#tw-runs-on") == ["detached"] and w.chosen_runs_on() == "detached", pressed("#tw-runs-on"))
      check("no temporary key for it", not w.query_one("#tw-key").display)
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      check("step 5: now", w.when_kind() == "now")
      w.query_one("#tw-when-every").value = True
      w.query_one("#tw-every").value = "1m"; await pilot.pause(0.2)
      check("every 1m: too short, Next waits", w.query_one("#tw-next").disabled)
      w.query_one("#tw-when-now").value = True; await pilot.pause(0.2)
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      summary = str(w.query_one("#tw-summary").render())
      check("step 6: the summary, what happens if slurp closes", "If slurp closes" in summary and "Nothing is deleted" in summary, summary)
      check("a name", w.query_one("#tw-name").value == "run42 Local → Local")
      w.query_one("#tw-name").value = "my run"
      await pilot.click("#tw-go")
      check("Go: its subtab in the Transfers tab", await until(pilot, lambda: isinstance(app.screen, TransferScreen)) and app.in_transfers)
      t = app.transfers.transfers[0]
      check("it ran", await until(pilot, lambda: t.state == "done", 30), (t.state, t.message))
      check("the files are there", (dst / "run42" / "sub" / "a.bin").read_bytes() == (src / "sub" / "a.bin").read_bytes())
      await pilot.pause(0.5)
      tabs = [b.id for b in app.screen.query_one("#cluster-tabs").query("Button")]
      check("the Transfers tab: left of the tabs", tabs[:2] == ["transfers-tab", "cluster-tab-0"], tabs)
      bar = app.screen.query_one("#view-tabs")
      check("its subtabs: the list and the transfer (not the overview, not the jobs)",
            bar.query_one("#view-transfers").display and not bar.query_one("#view-overview").display and any((b.id or "").startswith("xfer-tab-") for b in bar.query("Button")))
      head = str(app.screen.query_one("#xd-head").render())
      check("the details: its name and state", "my run" in head and "done" in head, head)
      await pilot.press("t"); await pilot.pause(0.3)
      check("a cluster's key does nothing here (t: a shell)", isinstance(app.screen, TransferScreen) and app.shell is None)
      await pilot.click("#view-transfers"); await pilot.pause(0.3)
      check("the list", isinstance(app.screen, TransfersScreen) and app.screen.query_one("#xfer-list").row_count == 1)
      await pilot.press("ctrl+w"); await pilot.pause(0.3)
      check("Ctrl-W on the list: it stays", isinstance(app.screen, TransfersScreen))
      await pilot.click("#cluster-tab-0"); await pilot.pause(0.3)
      check("a cluster tab: shown, the Transfers tab no longer", not app.in_transfers and not isinstance(app.screen, (TransfersScreen, TransferScreen)))
      await pilot.click("#transfers-tab"); await pilot.pause(0.3)
      check("back: the list (shown last)", isinstance(app.screen, TransfersScreen))
      app.open_transfer(t); await pilot.pause(0.3)
      app.save_tabs()
      await pilot.press("q")

  async def again():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      check("a new start: the Transfers tab, shown as before, with the transfer's subtab",
            await until(pilot, lambda: app.in_transfers and isinstance(app.screen, TransferScreen)), type(app.screen).__name__)
      await pilot.press("ctrl+w"); await pilot.pause(0.3)
      check("Ctrl-W closes the transfer's subtab: the list", isinstance(app.screen, TransfersScreen) and not app.transfer_views)
      t = app.transfers.transfers[0]
      app.delete_transfer(t); await pilot.pause(0.4)
      check("deleting the last transfer: no tab", not app.in_transfers and not app.transfers.transfers
            and not any(b.display for b in app.screen.query("#transfers-tab")))
      await pilot.press("q")

  asyncio.run(main())
  asyncio.run(again())


def test_never(check, tmp_path):
  """When: never by itself: Go reads Add, no Save without starting; Add adds it, and it runs only on Run now"""
  src, dst = files(tmp_path)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await pilot.press("T"); await pilot.pause(0.4)
      w = app.screen
      w.query_one("#tw-src-path").value = str(src)
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.2)
      w.query_one("#tw-next").press(); await pilot.pause(0.2)
      w.query_one("#tw-dst-path").value = str(dst); await pilot.pause(0.2)
      w.query_one("#tw-next").press()
      await until(pilot, lambda: w.plan.get("done"), 30)
      w.query_one("#tw-next").press(); await pilot.pause(0.4)
      w.query_one("#tw-next").press(); await pilot.pause(0.2)
      w.query_one("#tw-when-never").value = True; await pilot.pause(0.2)
      note = str(w.query_one("#tw-when-note").render())
      check("never: it says when it runs", "only when you click Run now" in note, note)
      check("no login, no missed runs for it", not w.query_one("#tw-login").display and not w.query_one("#tw-missed").display)
      w.query_one("#tw-next").press(); await pilot.pause(0.2)
      go = w.query_one("#tw-go")
      check("the last step: Add instead of Go, no Save without starting", str(go.label) == "Add" and go.display
            and not w.query_one("#tw-save").display, (str(go.label), w.query_one("#tw-save").display))
      check("the summary says so", "only on Run now" in str(w.query_one("#tw-summary").render()))
      go.press()
      check("Add: its subtab", await until(pilot, lambda: isinstance(app.screen, TransferScreen)))
      t = app.transfers.transfers[0]
      await pilot.pause(1.5)
      check("it does not run", t.state == "new" and not t.runs and not os.path.exists(dst / "run42"), (t.state, t.runs))
      check("its subtab offers Run now", app.screen.query_one("#xd-run").display)
      await pilot.click("#xd-run")
      check("Run now: it runs", await until(pilot, lambda: t.state == "done", 30), (t.state, t.message))
      app.edit_transfer(t); await pilot.pause(0.4)
      e = app.screen
      check("edited: never is chosen", e.query_one("#tw-when-never").value)
      e.dismiss(None); await pilot.pause(0.2)
      await pilot.press("q")

  asyncio.run(main())
