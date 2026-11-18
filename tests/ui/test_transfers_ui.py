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
      check("five steps", [str(b.label) for b in w.query("#tw-steps Button")] == ["1 Source", "2 Destination", "3 Route", "4 When", "5 Summary"])
      check("a new transfer: the steps ahead cannot be jumped to", all(w.query_one(f"#tw-goto-{i}").disabled for i in range(1, 5)))
      w.query_one("#tw-src-path").value = str(src)
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.2)
      check("a path typed: added", w.paths == [str(src)] and not w.query_one("#tw-next").disabled, w.paths)
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      check("step 2: the destination", w.step == 1 and w.query_one("#tw-next").disabled)
      w.query_one("#tw-dst-path").value = str(dst); await pilot.pause(0.2)
      preview = str(w.query_one("#tw-dst-preview").render())
      check("what goes where", f"run42  →  Local:{dst}/run42" in preview, preview)
      await pilot.click("#tw-next"); await pilot.pause(0.1)
      check("step 3: the route", w.step == 2)
      check("…the check, until it is done", await until(pilot, lambda: w.plan.get("done"), 30), w.plan)
      await pilot.pause(0.6)
      steps = str(w.query_one("#tw-check-steps").render())
      check("it says what remains", "500.0 KiB in 2 files to copy" in steps or "488.3 KiB in 2 files" in steps, steps)
      label = lambda route: str(w.query_one(f".tw-r-{route}").label)
      check("the routes as choices, with what the check found: the same filesystem passed, recommended",
            label("same-fs").startswith("✓ same filesystem") and "(recommended)" in label("same-fs") and label("relay").startswith("✓ relay"),
            [label(r) for r in ("same-fs", "direct-push", "direct-pull", "relay")])
      check("a route that failed cannot be chosen, and says why", label("direct-push").startswith("✗") and w.query_one(".tw-r-direct-push").disabled,
            label("direct-push"))
      check("the chosen route passed: Next", not w.query_one("#tw-next").disabled)
      passed = dict(w.plan["routes"]["same-fs"])
      w.plan["routes"]["same-fs"] = {"ok": False, "why": "it did not pass"}
      w.show_plan(); await pilot.pause(0.2)
      check("a chosen route that does not pass: no longer chosen, Next waits and says why",
            w.chosen_route() is None and w.query_one("#tw-next").disabled and w.query_one("#tw-next").tooltip == "choose a route",
            (w.chosen_route(), w.query_one("#tw-next").tooltip))
      w.plan["routes"]["same-fs"] = passed
      w.show_plan(); await pilot.pause(0.3)
      check("…passed again: chosen again (the recommended one)", w.chosen_route() == "same-fs" and not w.query_one("#tw-next").disabled)
      w.plan["routes"]["same-fs"]["runs_on"] = ["detached", "job"] # (as where the site has a transfer partition)
      await w.route_changed(); await pilot.pause(0.3)
      check("the recommended route, where it runs (and a transfer job offered, where there is one)",
            w.chosen_route() == "same-fs" and w.chosen_runs_on() == "detached" and w.query(".tw-o-job"), (w.chosen_route(), w.chosen_runs_on()))
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
      flow = str(w.query_one("#tw-flow").render())
      check("how the data flows: the two tabs as boxes, the same filesystem on the arrow, rsync on the login node",
            flow.count("┌") == 2 and "same filesystem" in flow and "▶" in flow and "(on the login node)" in flow, flow)
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      check("step 4: now", w.when_kind() == "now")
      w.query_one("#tw-when-every").value = True
      w.query_one("#tw-every").value = "1m"; await pilot.pause(0.2)
      check("every 1m: too short, Next waits", w.query_one("#tw-next").disabled)
      w.query_one("#tw-when-now").value = True; await pilot.pause(0.2)
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      summary = str(w.query_one("#tw-summary").render())
      check("step 5: the summary, what happens if slurp closes", "If slurp closes" in summary and "Nothing is deleted" in summary, summary)
      check("a name", w.query_one("#tw-name").value == "run42 Local → Local")
      check("the summary shows the flow", "┌" in str(w.query_one("#tw-summary-flow").render()))
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
      route = str(app.screen.query_one("#xd-route").render())
      check("…and how the data flows", route.count("┌") == 2 and "same filesystem" in route, route)
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
      await until(pilot, lambda: w.plan.get("done"), 30); await pilot.pause(0.6)
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
      await pilot.pause(0.3)
      check("editing: every step can be gone to", all(not e.query_one(f"#tw-goto-{i}").disabled for i in range(5)),
            [e.query_one(f"#tw-goto-{i}").disabled for i in range(5)])
      await pilot.click("#tw-goto-4"); await pilot.pause(0.3)
      check("a click on Summary: that step", e.step == 4 and "only on Run now" in str(e.query_one("#tw-summary").render()))
      await pilot.click("#tw-goto-2"); await pilot.pause(0.4)
      check("a click on Route: its choices as they were", e.step == 2 and e.chosen_route() == t.route and e.chosen_runs_on() == t.runs_on,
            (e.step, e.chosen_route(), e.chosen_runs_on()))
      await pilot.click("#tw-goto-0"); await pilot.pause(0.3)
      e.query_one("#tw-src-path").value = str(src / "sub")
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.3)
      check("another source: the steps after the check wait for a new check", e.step == 0 and not e.query_one("#tw-goto-2").disabled
            and all(e.query_one(f"#tw-goto-{i}").disabled for i in (3, 4)), [e.query_one(f"#tw-goto-{i}").disabled for i in range(5)])
      await pilot.click("#tw-goto-2")
      check("the check: the steps after it can be gone to again", await until(pilot, lambda: e.plan.get("done") and not e.query_one("#tw-goto-4").disabled, 30))
      e.dismiss(None); await pilot.pause(0.2)
      await pilot.press("q")

  asyncio.run(main())


def test_flow_diagram(check):
  """How the data flows: boxes, the arrow says how, under each box what runs there"""
  import types
  from slurp.ui.screens.transfers import flow_diagram
  alps, jedi = types.SimpleNamespace(destination="daint", connection=None), types.SimpleNamespace(destination="jedi", connection=None)
  here = types.SimpleNamespace(destination=None, connection=None)
  relay = flow_diagram("relay", "slurp", alps, jedi, "alps", "jedi").plain
  check("the relay between two clusters: alps → slurp → jedi", relay.count("┌") == 3 and relay.index("alps") < relay.index("slurp") < relay.index("jedi")
        and relay.count("relay") == 2 and "passes the bytes" in relay, relay)
  local = flow_diagram("relay", "slurp", here, jedi, "Local", "jedi").plain
  check("with this machine as an end: no box for slurp", local.count("┌") == 2 and "over slurp's SSH" in local, local)
  push = flow_diagram("direct-push", "job", alps, jedi, "alps", "jedi", "1234").plain
  check("push: rsync on the source logs in to the destination, in its job", "push" in push and "logs in to jedi" in push
        and "transfer job 1234" in push and push.count("┌") == 2, push)
  pull = flow_diagram("direct-pull", "detached", alps, jedi, "alps", "jedi").plain
  check("pull: rsync on the destination logs in to the source", "pull" in pull and "logs in to alps" in pull and "login node" in pull, pull)
  check("the data flows left to right", all("▶" in d and "◀" not in d for d in (relay, push, pull)))
  moving = flow_diagram("relay", "slurp", alps, jedi, "alps", "jedi", moving=2).plain
  check("while it runs: a dot on the arrows", "●" in moving)


def test_comparing_shown(check, tmp_path):
  """A run that compares the files (checksums): the details say so, the list too"""
  import time
  from slurp.transfer import model as M

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      t = M.Transfer(M.new_id(), "sums", {"tab": ref, "paths": ["/x"]}, {"tab": ref, "path": "/y"}, route="same-fs",
                     runs_on="detached", options={"checksum": True},
                     plan={"total_bytes": 10 << 30, "routes": {"same-fs": {"ok": True, "rate": 100 << 20}}})
      t.state, t.run, t.progress = "running", {"kind": "detached", "folder": "/x", "started": time.time() - 30}, {"phase": "comparing"}
      app.transfers.store.transfers.append(t)
      app.open_transfer(t); await pilot.pause(1.2)
      text = str(app.screen.query_one("#xd-current").render())
      check("the details: comparing by contents, how much, how long", "checksums" in text and "10.0 GiB" in text
            and "for 3" in text and "about 1 min expected" in text, text)
      await pilot.click("#view-transfers"); await pilot.pause(1.2)
      table = app.screen.query_one("#xfer-list")
      cell = str(table.get_row_at(0)[4])
      check("the list: comparing", "comparing" in cell, cell)
      app.transfers.store.transfers.remove(t)
      await pilot.press("q")

  asyncio.run(main())


def test_paths_open(check, tmp_path):
  """The paths of a transfer's details are links: a folder opens as a directory view, a text file in its view, a
  binary file as its folder searched for it, a pattern as its folder searched for it, a destination that is not
  there yet as the nearest folder that is; in the tab of the cluster, the Transfers tab left"""
  from slurp.transfer import model as M
  from slurp.ui.screens.files import DirScreen, FileScreen
  data = tmp_path / "data"
  (data / "logs").mkdir(parents=True)
  (data / "logs" / "a.log").write_text("x")
  (data / "notes.txt").write_text("hello\n")
  (data / "blob.bin").write_bytes(bytes(range(256)) * 10)
  dest = tmp_path / "dest"
  dest.mkdir()

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      sources = [str(data / "logs"), str(data / "notes.txt"), str(data / "blob.bin"), str(data / "logs" / "*.log")]
      t = M.Transfer(M.new_id(), "paths", {"tab": ref, "paths": sources}, {"tab": ref, "path": str(dest / "new" / "deeper")})
      app.transfers.add(t)
      app.open_transfer(t); await pilot.pause(0.6)
      text = app.screen.query_one("#xd-paths").render()
      clicks = [span.style.meta.get("@click") for span in getattr(text, "spans", []) if hasattr(span.style, "meta") and span.style.meta.get("@click")]
      check("each path a link", clicks == ["screen.open_end(0)", "screen.open_end(1)", "screen.open_end(2)", "screen.open_end(3)", "screen.open_end(-1)"], clicks)
      async def opened(index):
        app.show_transfers(app.transfer_view(t)); await pilot.pause(0.3)
        app.screen.action_open_end(index)
        await until(pilot, lambda: not app.in_transfers and isinstance(app.screen, (DirScreen, FileScreen)), 10)
        await pilot.pause(0.3)
        return app.screen
      s = await opened(0)
      check("a folder: its directory view, in the cluster's tab", isinstance(s, DirScreen) and s.path == str(data / "logs") and not app.in_transfers,
            type(s).__name__)
      s = await opened(1)
      check("a text file: its view", isinstance(s, FileScreen) and s.path == str(data / "notes.txt"), type(s).__name__)
      s = await opened(2)
      check("a binary file: its folder, searched for it", isinstance(s, DirScreen) and s.path == str(data)
            and s.query_one("#dfilter").value == "blob.bin" and s.query_one("#dsearch").has_class("visible"), (type(s).__name__, getattr(s, "path", "")))
      s = await opened(3)
      check("a pattern: its folder, searched for it", isinstance(s, DirScreen) and s.path == str(data / "logs")
            and s.query_one("#dfilter").value == "*.log", (type(s).__name__, getattr(s, "path", "")))
      s = await opened(-1)
      check("a destination not there yet: the nearest folder that is", isinstance(s, DirScreen) and s.path == str(dest), getattr(s, "path", ""))
      await pilot.press("q")

  asyncio.run(main())
