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
      check("five steps (no When)", [str(b.label) for b in w.query("#tw-steps Button")] == ["1 Source", "2 Destination", "3 Options", "4 Route", "5 Summary"])
      check("a new transfer: the steps ahead cannot be jumped to", all(w.query_one(f"#tw-goto-{i}").disabled for i in range(1, 5)))
      check("the source: an endpoint, this machine (its tab is Local)", w.src_id == "local" and w.query_one("#tw-src-tab").value == "local")
      check("…its state: connected", "connected" in str(w.query_one("#tw-src-state").render()) and not w.query_one("#tw-src-connect").display)
      w.query_one("#tw-src-path").value = str(src)
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.2)
      check("a path typed: added", w.paths == [str(src)] and not w.query_one("#tw-next").disabled, w.paths)
      counted = lambda: "files" in str(w.query_one("#tw-src-paths").render())
      check("…and what it holds: its size, files, folders", await until(pilot, counted, 15)
            and "488.3 KiB · 2 files · 2 folders" in str(w.query_one("#tw-src-paths").render()), str(w.query_one("#tw-src-paths").render()))
      w.query_one("#tw-src-path").value = str(src / "b.txt")
      await pilot.click("#tw-src-path"); await pilot.press("enter")
      await until(pilot, lambda: "in all" in str(w.query_one("#tw-src-paths").render()) and "counting" not in str(w.query_one("#tw-src-paths").render()), 15)
      shown = str(w.query_one("#tw-src-paths").render())
      check("two sources: each, and in all", "5 Bytes · 1 file" in shown and "in all" in shown and "3 files" in shown, shown)
      await pilot.click("#tw-src-remove"); await pilot.pause(0.3)
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      check("step 2: the destination", w.step == 1 and w.query_one("#tw-next").disabled)
      w.query_one("#tw-dst-path").value = str(dst); await pilot.pause(0.2)
      preview = str(w.query_one("#tw-dst-preview").render())
      check("what goes where", f"run42  →  Local:{dst}/run42" in preview, preview)
      await pilot.click("#tw-next"); await pilot.pause(0.3)
      check("step 3: the options", w.step == 2 and w.query_one("#tw-step-2").query("#tw-checksum")
            and w.query_one("#tw-step-2").query("#tw-bwlimit"))
      check("…compression said to count only on the relay", "only on the relay" in str(w.query_one("#tw-compress-note").render()))
      w.query_one("#tw-bwlimit").value = "fast"; await pilot.pause(0.2)
      check("a speed limit that is not a number: Next waits", w.query_one("#tw-next").disabled and "speed limit" in str(w.query_one("#tw-next").tooltip))
      w.query_one("#tw-bwlimit").value = ""; await pilot.pause(0.2)
      await pilot.click("#tw-next"); await pilot.pause(0.1)
      check("step 4: the route", w.step == 3)
      check("…the check, until it is done", await until(pilot, lambda: w.plan.get("done"), 30), w.plan)
      await pilot.pause(0.6)
      steps = str(w.query_one("#tw-check-steps").render())
      check("it says what remains", "500.0 KiB in 2 files to copy" in steps or "488.3 KiB in 2 files" in steps, steps)
      label = lambda route: str(w.query_one(f".tw-r-{route}").label)
      check("the routes as choices, with what the check found: the same filesystem passed, recommended",
            label("same-fs").startswith("✓ same filesystem") and "(recommended)" in label("same-fs") and label("relay").startswith("✓ relay"),
            [label(r) for r in ("same-fs", "direct-push", "direct-pull", "relay")])
      check("a route that failed says why, and can be chosen (Run now checks first)", label("direct-push").startswith("✗")
            and not w.query_one(".tw-r-direct-push").disabled, label("direct-push"))
      check("the recommended route chosen: Next", w.chosen_route() == "same-fs" and not w.query_one("#tw-next").disabled)
      await pilot.click(w.query_one(".tw-r-direct-push")); await pilot.pause(0.4)
      check("the failed one: Next too", w.chosen_route() == "direct-push" and not w.query_one("#tw-next").disabled,
            (w.chosen_route(), w.query_one("#tw-next").tooltip))
      await pilot.click(w.query_one(".tw-r-same-fs")); await pilot.pause(0.4)
      await w.route_changed(); await pilot.pause(0.2)
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
      check("…the bandwidth measured under the arrow, the estimate below", "/s" in flow.splitlines()[2] and "⏱ about" in flow
            and "in 2 files" in flow, flow)
      w.query_one("#tw-checksum").value = True; await pilot.pause(0.3)
      flow = str(w.query_one("#tw-flow").render())
      check("checksums: the estimate says so", "with checksums" in flow, flow)
      w.query_one("#tw-checksum").value = False; await pilot.pause(0.2)
      check("the route step has no options (they have their own step)", not any(
        w.query_one("#tw-step-3").query(sel) for sel in ("#tw-checksum", "#tw-compress", "#tw-bwlimit")))
      await pilot.click("#tw-next"); await pilot.pause(0.2)
      summary = str(w.query_one("#tw-summary").render())
      check("step 5: the summary, what happens if slurp closes", "If slurp closes" in summary and "Nothing is deleted" in summary, summary)
      check("a name", w.query_one("#tw-name").value == "run42 Local → Local")
      check("the summary shows the flow", "┌" in str(w.query_one("#tw-summary-flow").render()))
      w.query_one("#tw-name").value = "my run"
      check("the last button: Add (no Go, no Save without starting)", str(w.query_one("#tw-go").label) == "Add"
            and not w.query("#tw-save"))
      await pilot.click("#tw-go")
      check("Add: its subtab in the Transfers tab", await until(pilot, lambda: isinstance(app.screen, TransferScreen)) and app.in_transfers)
      t = app.transfers.transfers[0]
      check("added, not started", t.state == "new" and not t.runs and t.source["endpoint"] == "local" and t.dest["endpoint"] == "local",
            (t.state, t.source, t.dest))
      app.screen.query_one("#xd-run").press()
      check("Run now: it ran", await until(pilot, lambda: t.state == "done", 30), (t.state, t.message))
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
      row = [str(c) for c in app.screen.query_one("#xfer-list").get_row_at(0)][1:] # (after the button)
      check("…with the source and the destination, each with its tab and path", row[1].endswith(f"Local:{src}") and row[2].endswith(f"Local:{dst}")
            or "/…/" in row[1], row[:3])
      note = str(app.screen.query_one("#xfer-note").render())
      check("…the full paths below", f"Local:{src}  →  Local:{dst}" in note, note)
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


def test_add_and_edit(check, tmp_path):
  """Add adds it (it does not run); Run now runs it; editing: every step a click away, Cancel and Save; a changed
  source: Save at once (Run now checks first); nothing starts"""
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
      w.query_one("#tw-next").press(); await pilot.pause(0.2) # (the options)
      w.query_one("#tw-next").press() # (the route)
      await until(pilot, lambda: w.plan.get("done"), 30); await pilot.pause(0.6)
      w.query_one("#tw-next").press(); await pilot.pause(0.2)
      summary = str(w.query_one("#tw-summary").render())
      check("the summary: it runs on Run now, Copy as a command for cron", "when you click Run now" in summary and "Copy as a command" in summary, summary)
      w.query_one("#tw-go").press()
      check("Add: its subtab", await until(pilot, lambda: isinstance(app.screen, TransferScreen)))
      t = app.transfers.transfers[0]
      await pilot.pause(1.5)
      check("it does not run", t.state == "new" and not t.runs and not os.path.exists(dst / "run42"), (t.state, t.runs))
      check("its subtab offers Run now", app.screen.query_one("#xd-run").display)
      app.screen.query_one("#xd-run").press()
      check("Run now: it runs", await until(pilot, lambda: t.state == "done", 30), (t.state, t.message))
      app.edit_transfer(t); await pilot.pause(0.7)
      e = app.screen
      buttons = [str(b.label) for b in e.query("#tw-buttons Button") if b.display]
      check("editing: the buttons are Cancel and Save", buttons == ["Cancel", "Save"], buttons)
      check("editing: every step can be gone to", all(not e.query_one(f"#tw-goto-{i}").disabled for i in range(5)),
            [e.query_one(f"#tw-goto-{i}").disabled for i in range(5)])
      await pilot.click("#tw-goto-4"); await pilot.pause(0.3)
      check("a click on Summary: that step", e.step == 4 and "Run now" in str(e.query_one("#tw-summary").render()))
      await pilot.click("#tw-goto-3"); await pilot.pause(0.4)
      check("a click on Route: its choices as they were", e.step == 3 and e.chosen_route() == t.route and e.chosen_runs_on() == t.runs_on,
            (e.step, e.chosen_route(), e.chosen_runs_on()))
      await pilot.click("#tw-goto-0"); await pilot.pause(0.3)
      e.query_one("#tw-src-path").value = str(src / "sub")
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.3)
      store = e.query_one("#tw-store")
      check("another source: Save at once (Run now checks first)", not store.disabled, (store.disabled, store.tooltip))
      store.press(); await pilot.pause(0.5)
      check("Save: the changes kept, nothing started", t.source["paths"] == [str(src), str(src / "sub")] and len(t.runs) == 1
            and t.state == "done", (t.state, t.source, len(t.runs)))
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
  for kind, label in (("s3", "S3"), ("smb", "SMB share"), ("webdav", "WebDAV")):
    up = flow_diagram("rclone-here", "slurp", here, None, "Local", "files", dst_kind=kind).plain
    down = flow_diagram("rclone-cluster", "job", None, alps, "files", "alps", src_kind=kind).plain
    check(f"{kind}: its end labelled {label}", label in up and label in down and (kind == "s3" or "S3" not in up + down), (up, down))


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
      cell = str(table.get_row_at(0)[[c.value for c in table.columns].index("progress")])
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
      clicks = []
      for card in ("#xd-src-end", "#xd-dst-end"):
        text = app.screen.query_one(card).render()
        clicks += [span.style.meta.get("@click") for span in getattr(text, "spans", []) if hasattr(span.style, "meta") and span.style.meta.get("@click")]
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


def test_endpoint_cards(check, tmp_path):
  """The details show the source and the destination as cards with their state; Run now waits for every endpoint
  (its tooltip names the missing one); Copy as a command gives the rsync line"""
  from slurp.transfer import model as M

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      gone = {"endpoint": "tab:jedi", "name": "jedi", "tab": {"destination": "jedi", "name": "jedi"}, "path": "/scratch/in"}
      t = M.Transfer(M.new_id(), "to jedi", {"endpoint": "local", "tab": ref, "paths": [str(tmp_path)]}, gone)
      app.transfers.add(t)
      app.open_transfer(t); await pilot.pause(0.6)
      dst = str(app.screen.query_one("#xd-dst-end").render())
      check("the destination card: its name and state", "jedi" in dst and "tab not open" in dst, dst)
      button = app.screen.query_one("#xd-dst-connect")
      check("its button opens the tab", button.display and "Open its tab" in str(button.label), str(button.label))
      check("the source card has no button (this machine)", not app.screen.query_one("#xd-src-connect").display)
      run = app.screen.query_one("#xd-run")
      check("Run now waits for jedi", run.disabled and "jedi" in str(run.tooltip), (run.disabled, run.tooltip))
      local = M.Transfer(M.new_id(), "here", {"endpoint": "local", "tab": ref, "paths": [str(tmp_path / "a b")]},
                         {"endpoint": "local", "tab": ref, "path": str(tmp_path / "out")}, route="local")
      text = app.transfers.command_text(local)
      check("the command: rsync with the quoted paths", "rsync " in text and f"'{tmp_path}/a b'" in text and f"{tmp_path}/out/" in text, text)
      app.transfers.add(local)
      app.open_transfer(local); await pilot.pause(0.6)
      check("Run now: ready", not app.screen.query_one("#xd-run").disabled)
      await pilot.press("q")

  asyncio.run(main())


def test_runs(check, tmp_path):
  """The details have a Runs field: how many times it ran; a click: the list of runs (the last first); a click on one:
  its details (times, what it copied, rsync's statistics, its events); Esc goes back"""
  from slurp.transfer import model as M
  from slurp.ui.screens.transfers import TransferRunScreen, TransferRunsScreen
  src, dst = files(tmp_path)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      t = M.Transfer(M.new_id(), "twice", {"tab": ref, "paths": [str(src)]}, {"tab": ref, "path": str(dst)}, route="relay", runs_on="slurp")
      app.transfers.add(t)
      app.open_transfer(t); await pilot.pause(0.4)
      field = app.screen.query_one("#xd-runs")
      check("no runs yet: said, nothing to open", str(field.label) == "never ran yet" and field.disabled, str(field.label))
      for n in (1, 2):
        app.transfers.start(t)
        check(f"run {n} done", await until(pilot, lambda: len(t.runs) == n and t.state == "done", 30), (t.state, t.message))
      await pilot.pause(1.2)
      label = str(app.screen.query_one("#xd-runs").label)
      check("the field: ran 2 times, how they ended, the last", label.startswith("ran 2 times (2 done) · last:"), label)
      await pilot.click("#xd-runs"); await pilot.pause(0.5)
      runs = app.screen
      table = runs.query_one("#xr-list") if isinstance(runs, TransferRunsScreen) else None
      check("a click: the list of runs", table is not None and table.row_count == 2, type(runs).__name__)
      first_row = [str(c) for c in table.get_row_at(0)] if table is not None else []
      check("…the last first, with its number, result, what it copied", first_row[:1] == ["2"] and first_row[4] == "done", first_row)
      await pilot.press("enter"); await pilot.pause(0.5)
      run = app.screen
      check("Enter on a run: its details", isinstance(run, TransferRunScreen))
      if isinstance(run, TransferRunScreen):
        head, facts = str(run.query_one("#xu-head").render()), str(run.query_one("#xu-facts").render())
        stats, events = str(run.query_one("#xu-stats").render()), str(run.query_one("#xu-events").render())
        check("its head: run 2 of the transfer, done", "Run 2 of twice" in head and "done" in head, head)
        check("its facts: times, how long, the route", "Started" in facts and "Took" in facts and "relay through slurp" in facts, facts)
        check("rsync's statistics: the second run copied nothing", "files copied" in stats and "files copied              0" in stats, stats)
        check("its events", "run started" in events, events)
      await pilot.press("escape"); await pilot.pause(0.3)
      check("Esc: the list again", isinstance(app.screen, TransferRunsScreen))
      await pilot.press("escape"); await pilot.pause(0.3)
      check("Esc: the transfer's details again", type(app.screen).__name__ == "TransferScreen")
      check("the counter is kept", M.Store(app.transfers.store.path).get(t.id).run_count == 2)
      await pilot.press("q")

  asyncio.run(main())


def test_sync_and_recheck(check, tmp_path):
  """The details say whether both sides are in sync (from the last check or the last run that ended well), and Check
  again checks the transfer as it is set up"""
  from slurp.transfer import model as M
  src, dst = files(tmp_path)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      t = M.Transfer(M.new_id(), "sync", {"tab": ref, "paths": [str(src)]}, {"tab": ref, "path": str(dst)}, route="relay", runs_on="slurp")
      app.transfers.add(t)
      app.open_transfer(t); await pilot.pause(1.2)
      sync = lambda: str(app.screen.query_one("#xd-sync").render())
      check("never checked nor run: not known", "in sync?" in sync() and "not checked yet" in sync(), sync())
      app.screen.query_one("#xd-recheck").press(); await pilot.pause(0.1)
      check("Check again: it checks", await until(pilot, lambda: t.id not in app.transfers.checking and t.plan.get("done"), 30))
      await pilot.pause(1.2)
      check("…not in sync: what is to copy", "● not in sync" in sync() and "in 2 files to copy" in sync(), sync())
      check("the check's results in the details", "files to copy" in str(app.screen.query_one("#xd-check").render()))
      app.transfers.start(t)
      await until(pilot, lambda: t.state == "done", 30); await pilot.pause(1.2)
      check("after a run that ended well: in sync, as of that run", "● in sync" in sync() and "the last run" in sync(), sync())
      (src / "new.txt").write_text("more")
      app.screen.query_one("#xd-recheck").press()
      await until(pilot, lambda: t.id not in app.transfers.checking, 30); await pilot.pause(1.2)
      check("a new file on the source, Check again: not in sync, 1 file", "● not in sync" in sync() and "in 1 file to copy" in sync(), sync())
      check("in its log", any("checked:" in words for _, words in t.log))
      await pilot.press("q")

  asyncio.run(main())


def test_direct_job_checked_on_a_node(check, tmp_path, monkeypatch):
  """A direct route as a transfer job: the login is checked from a node of the partition before Next (refused there:
  Next refuses, and says why; no node free: Next, with a note); no temporary key for a route that logs in by itself"""
  src, dst = files(tmp_path)
  answers = []
  def probe(manager, t, route, partition, account, done):
    answers.append((route, partition, account))
    done((False, f"a node of {partition} cannot log in to jureca by itself: Permission denied (publickey)."))
  from slurp.transfer.manager import Manager
  monkeypatch.setattr(Manager, "probe_node", probe)
  monkeypatch.setattr(Manager, "test_job", lambda manager, t, route, done: done((True, "Job 1 to start at 2026-11-18T14:00:00")))

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await pilot.press("T"); await pilot.pause(0.3)
      w = app.screen
      w.query_one("#tw-src-path").value = str(src)
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.2)
      w.query_one("#tw-next").press(); await pilot.pause(0.2)
      w.query_one("#tw-dst-path").value = str(dst); await pilot.pause(0.2)
      w.query_one("#tw-next").press(); await pilot.pause(0.2) # (the options)
      w.query_one("#tw-next").press() # (the route)
      await until(pilot, lambda: w.plan.get("done"), 30); await pilot.pause(0.5)
      # (as on two clusters: the direct route passed from the login node, the site has a transfer partition)
      w.plan["routes"]["direct-push"] = {"ok": True, "rate": 100 << 20, "estimate": [1, 2], "runs_on": ["detached", "job"]}
      w.show_plan(); await pilot.pause(0.2)
      await pilot.click(w.query_one(".tw-r-direct-push")); await pilot.pause(0.4)
      check("a route that logs in by itself: no temporary key offered", not w.query_one("#tw-key").display and not w.query_one("#tw-key").value)
      await pilot.click(w.query_one(".tw-o-job")); await pilot.pause(0.3)
      w.query_one("#tw-partition").value = "xfer"; await pilot.pause(1.5)
      check("as a job: the login checked from a node of the partition", answers and answers[-1][:2] == ("direct-push", "xfer"), answers)
      node = str(w.query_one("#tw-node-check").render())
      check("refused there: said (Run now checks again; it may be added so)", "✗" in node and "Permission denied" in node
            and not w.query_one("#tw-next").disabled, node)
      await pilot.click(w.query_one(".tw-o-detached")); await pilot.pause(0.3)
      check("detached on the login node (where the check logged in): Next", not w.query_one("#tw-next").disabled, w.query_one("#tw-next").tooltip)
      w.dismiss(None); await pilot.pause(0.2)
      await pilot.press("q")

  asyncio.run(main())


def test_short_path(check):
  from slurp.ui.screens.transfers import short_path
  long = "/very/long/directory/path/with/many/subdirs/and/more/stuff/going/on/in/its/path"
  short = short_path(long, 30)
  check("a long path: its start and its end, … between, at slashes", short.startswith("/very/long/") and short.endswith("/in/its/path")
        and "/…/" in short and len(short) <= 30, short)
  check("a short one: as it is", short_path("/scratch/run42", 30) == "/scratch/run42")
  name = "/s/" + "x" * 60 + ".h5"
  check("one long name: its end", short_path(name, 30) == "…" + name[-29:] and len(short_path(name, 30)) == 30, short_path(name, 30))


def test_status_and_continue(check, tmp_path):
  """The details show the state in big; Continue only for a paused transfer: mid-run Continue, between runs Resume
  ; not for one that ended"""
  from slurp.transfer import model as M

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      t = M.Transfer(M.new_id(), "st", {"tab": ref, "paths": ["/x"]}, {"tab": ref, "path": "/y"}, route="relay", runs_on="slurp")
      t.state, t.runs, t.message = "done", [{"started": 1.0, "ended": 2.0, "result": "done"}], ""
      app.transfers.add(t)
      app.open_transfer(t); await pilot.pause(1.2)
      status = lambda: str(app.screen.query_one("#xd-status").render())
      resume = lambda: app.screen.query_one("#xd-resume")
      check("done: its state in big", "DONE" in status(), status())
      check("…no Continue (Run now instead)", not resume().display and app.screen.query_one("#xd-run").display)
      t.state, t.message = "paused", "paused"
      t.runs.append({"started": 3.0, "ended": 4.0, "result": "paused"})
      await pilot.pause(1.2)
      check("paused: said, Continue", "PAUSED" in status() and "Continue goes on" in status() and resume().display and "Continue" in str(resume().label),
            (status(), str(resume().label)))
      t.state, t.message = "waiting", "waiting for jedi (not connected)"
      await pilot.pause(1.2)
      check("waiting: in big, why", "WAITING" in status() and "jedi" in status(), status())
      await pilot.press("q")

  asyncio.run(main())


def test_job_tested(check, tmp_path, monkeypatch):
  """As a transfer job: the account picked from the user's accounts, the job script tested (sbatch --test-only) at once
  and when its settings change; refused: Next refuses with Slurm's error"""
  src, dst = files(tmp_path)
  tests = []
  def test_job(manager, t, route, done):
    tests.append(dict(t.options))
    done((True, "Job 1 to start at 2026-11-18T14:00:00 in partition xfer") if t.options.get("account") == "g1" else
         (False, "error: Batch job submission failed: Invalid account or account/partition combination specified"))
  from slurp.transfer.manager import Manager
  monkeypatch.setattr(Manager, "test_job", test_job)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      app.clusters[0].accounts = ["g1", "g2"]
      await pilot.press("T"); await pilot.pause(0.3)
      w = app.screen
      w.query_one("#tw-src-path").value = str(src)
      await pilot.click("#tw-src-path"); await pilot.press("enter"); await pilot.pause(0.2)
      w.query_one("#tw-next").press(); await pilot.pause(0.2)
      w.query_one("#tw-dst-path").value = str(dst); await pilot.pause(0.2)
      w.query_one("#tw-next").press(); await pilot.pause(0.2) # (the options)
      w.query_one("#tw-next").press() # (the route)
      await until(pilot, lambda: w.plan.get("done"), 30); await pilot.pause(0.5)
      w.plan["routes"]["same-fs"]["runs_on"] = ["detached", "job"]
      await w.route_changed(); await pilot.pause(0.2)
      w.query_one("#tw-partition").value = "xfer"
      w.query_one(".tw-o-job").value = True; w.runs_changed(); await pilot.pause(1.5)
      picks = [v for _, v in w.query_one("#tw-account-list")._options if isinstance(v, str)]
      check("the accounts: a list to choose from (no field to type)", picks == ["g1", "g2"] and w.query_one("#tw-account-list").display
            and not w.query_one("#tw-account-list").disabled and not w.query_one("#tw-account").display, picks)
      check("no account: Next asks for one", w.query_one("#tw-next").disabled and "account" in str(w.query_one("#tw-next").tooltip),
            w.query_one("#tw-next").tooltip)
      w.query_one("#tw-account-list").value = "g2"; await pilot.pause(1.5)
      check("an account picked: in the field, the job tested", w.query_one("#tw-account").value == "g2" and tests and tests[-1].get("account") == "g2", tests)
      test = str(w.query_one("#tw-job-test").render())
      check("refused: Slurm's error, said (a warning: Run now submits and says it again)", "✗ sbatch --test-only" in test
            and "Invalid account" in test and not w.query_one("#tw-next").disabled, test)
      w.query_one("#tw-account-list").value = "g1"; await pilot.pause(1.5)
      test = str(w.query_one("#tw-job-test").render())
      check("another account: tested again, accepted: when and where", "✓ sbatch --test-only" in test and "to start at" in test
            and not w.query_one("#tw-next").disabled, (test, w.query_one("#tw-next").tooltip))
      app.clusters[0].accounts = [] # (none could be found there: a field to type one)
      w.query_one("#tw-account").value = ""
      w.fill_accounts(app.clusters[0]); await pilot.pause(0.2)
      check("no accounts known: the field instead", w.query_one("#tw-account").display and not w.query_one("#tw-account-list").display)
      w.dismiss(None); await pilot.pause(0.2)
      await pilot.press("q")

  asyncio.run(main())


def test_pattern_source(check, tmp_path):
  """A pattern as a source in the wizard: typed, or chosen in Browse… (the list shows its matches): what it matches,
  where they go"""
  from slurp.ui.screens.transfers import PickPathsScreen
  data = tmp_path / "data"
  data.mkdir()
  for name in ("a.h5", "b.h5", "c.txt"):
    (data / name).write_text("xx")

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await pilot.press("T"); await pilot.pause(0.3)
      w = app.screen
      w.query_one("#tw-src-browse").press(); await pilot.pause(0.3)
      picker = app.screen
      check("Browse…: the picker", isinstance(picker, PickPathsScreen))
      picker.query_one("#pp-path").value = str(data)
      await pilot.click("#pp-path"); await pilot.press("enter")
      await until(pilot, lambda: picker.entries, 10)
      picker.query_one("#pp-pattern").value = "*.h5"; await pilot.pause(0.3)
      table = picker.query_one("#pp-list")
      check("a pattern: the list shows its matches", table.row_count == 2 and "2 matches" in str(picker.query_one("#pp-status").render()),
            str(picker.query_one("#pp-status").render()))
      picker.query_one("#pp-choose-pattern").press(); await pilot.pause(0.5)
      check("Choose the pattern: a source", w.paths == [str(data / "*.h5")], w.paths)
      check("…what it matches", await until(pilot, lambda: "2 matches · 4 Bytes · 2 files" in str(w.query_one("#tw-src-paths").render()), 10),
            str(w.query_one("#tw-src-paths").render()))
      w.query_one("#tw-src-path").value = str(data / "*.zip")
      await pilot.click("#tw-src-path"); await pilot.press("enter")
      check("a pattern that matches nothing: said", await until(pilot, lambda: "nothing matches" in str(w.query_one("#tw-src-paths").render()), 10))
      w.query_one("#tw-dst-path").value = str(tmp_path / "dest"); await pilot.pause(0.3)
      preview = str(w.query_one("#tw-dst-preview").render())
      check("where they go: each match into the destination", f"*.h5 (each match)  →  Local:{tmp_path / 'dest'}/" in preview, preview)
      w.dismiss(None); await pilot.pause(0.2)
      await pilot.press("q")

  asyncio.run(main())


def test_list_keeps_scrolling(check, tmp_path):
  """The list is refreshed every second: scrolled down, it stays there (values change in place), also when a transfer
  comes (built anew, scrolled back), and the cursor stays on its transfer"""
  from slurp.transfer import model as M

  async def main():
    app = Slurp()
    async with app.run_test(size=(140, 30)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      made = []
      for i in range(60):
        t = M.Transfer(M.new_id(), f"t{i:02d}", {"tab": ref, "paths": [f"/data/run{i}"]}, {"tab": ref, "path": "/dest"})
        app.transfers.store.transfers.append(t)
        made.append(t)
      app.transfers.changed(None)
      app.show_transfers(); await pilot.pause(1.2)
      table = app.screen.query_one("#xfer-list")
      table.move_cursor(row=2); await pilot.pause(0.2)
      table.scroll_to(y=30, animate=False); await pilot.pause(0.3) # (as the mouse wheel: the cursor stays where it is)
      y = table.scroll_y
      check("scrolled down", y > 10, y)
      made[40].state, made[40].progress = "running", {"percent": 30.0, "rate": 1024 ** 2, "eta": 60}
      await pilot.pause(1.5)
      check("a value changed, refreshed: the scrolling and the cursor stay", table.scroll_y == y and table.cursor_row == 2
            and "30%" in str(table.get_row_at(40)[[c.value for c in table.columns].index("progress")]), (table.scroll_y, y, table.cursor_row))
      app.transfers.store.transfers.insert(0, M.Transfer(M.new_id(), "new", {"tab": ref, "paths": ["/x"]}, {"tab": ref, "path": "/y"}))
      app.transfers.changed(None); await pilot.pause(1.5)
      check("a transfer came: scrolled where it was, the cursor on the same transfer", table.scroll_y == y and table.cursor_row == 3,
            (table.scroll_y, y, table.cursor_row))
      await pilot.press("q")

  asyncio.run(main())


def test_pick_destination_folder(check, tmp_path):
  """Browse… of the destination: its folders are listed (a picker without a pattern field), Enter goes into one,
  Choose this folder takes it"""
  from slurp.ui.screens.transfers import PickPathsScreen
  (tmp_path / "dest" / "inner").mkdir(parents=True)
  (tmp_path / "dest" / "file.txt").write_text("x")

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await pilot.press("T"); await pilot.pause(0.3)
      w = app.screen
      w.query_one("#tw-dst-path").value = str(tmp_path / "dest"); await pilot.pause(0.2)
      w.query_one("#tw-dst-browse").press(); await pilot.pause(0.3)
      p = app.screen
      check("the picker of the destination", isinstance(p, PickPathsScreen) and not p.multi)
      check("its folders listed", await until(pilot, lambda: p.query_one("#pp-list").row_count == 3, 10),
            (p.query_one("#pp-list").row_count, str(p.query_one("#pp-status").render())))
      p.query_one("#pp-here").press(); await pilot.pause(0.3)
      check("Choose this folder: the destination", w.query_one("#tw-dst-path").value == str(tmp_path / "dest"))
      w.dismiss(None); await pilot.pause(0.2)
      await pilot.press("q")

  asyncio.run(main())


def test_endpoints_subtab(check, tmp_path):
  """Menu → Endpoints…: the Endpoints subtab of the Transfers tab: this machine, the tabs (with their state), how many
  transfers use each; a tab cannot be deleted there; New endpoint… (SSH: the + dialog)"""
  from slurp.transfer import model as M
  from slurp.ui.screens.transfers import EndpointsScreen, NewEndpointScreen
  from slurp.ui.screens.dialogs import AddClusterScreen

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      from slurp.ui.screens.menu import ITEMS
      check("the menu has Endpoints…", any(i and i[0] == "endpoints" for i in ITEMS))
      await app.run_action("endpoints"); await pilot.pause(0.6)
      check("Endpoints…: the subtab, in the Transfers tab (there, though no transfer exists)",
            isinstance(app.screen, EndpointsScreen) and app.in_transfers and app.screen.query("#transfers-tab"))
      table = app.screen.query_one("#ep-list")
      first = [str(c) for c in table.get_row_at(0)]
      check("this machine: Local, always connected", first[0] == "Local" and first[1] == "this machine" and "connected" in first[3], first)
      ref = M.tab_ref(app.clusters[0])
      app.transfers.add(M.Transfer(M.new_id(), "uses it", {"endpoint": "local", "tab": ref, "paths": ["/x"]},
                                   {"endpoint": "local", "tab": ref, "path": "/y"}))
      await pilot.pause(1.3)
      check("how many transfers use it", str(table.get_row_at(0)[4]) == "1 transfer", [str(c) for c in table.get_row_at(0)])
      app.screen.action_delete(); await pilot.pause(0.3)
      check("this machine cannot be deleted: said", any("as long as they are open" in str(n.message) for n in app._notifications))
      bar = app.screen.query_one("#view-tabs")
      check("the subtabs: All transfers and Endpoints", bar.query("#view-transfers") and bar.query_one("#view-endpoints").variant == "primary")
      app.screen.query_one("#ep-new").press(); await pilot.pause(0.3)
      check("New endpoint…: which kind", isinstance(app.screen, NewEndpointScreen))
      app.screen.query_one("#ne-next").press(); await pilot.pause(0.4)
      check("SSH: the + dialog (a tab)", isinstance(app.screen, AddClusterScreen))
      app.screen.dismiss(None); await pilot.pause(0.2)
      app.save_tabs()
      await pilot.press("q")

  async def again():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      check("a new start: the Endpoints subtab shown as before", await until(pilot, lambda: isinstance(app.screen, EndpointsScreen)),
            type(app.screen).__name__)
      await pilot.press("q")

  asyncio.run(main())
  asyncio.run(again())


def test_edit_offline(check, tmp_path):
  """Editing a transfer whose endpoint is not connected: its sources and destination stay as they are (their fields
  wait for the login), the options, the route and where it runs can be changed, the check is not run again (what the
  last one found stays), Save keeps it"""
  from slurp.state import ClusterSession
  from slurp.transfer import model as M
  from slurp.ui.screens.transfers import TransferWizard

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      jedi = ClusterSession("jedi", "jedi")
      app.clusters.append(jedi)
      ref = M.tab_ref(app.clusters[0])
      plan = {"done": True, "steps": {}, "routes": {"direct-push": {"ok": True, "rate": 1e6}}, "recommended": "direct-push",
              "remaining_bytes": 1000, "remaining_files": 2}
      t = M.Transfer(M.new_id(), "to jedi", {"endpoint": "local", "tab": ref, "paths": [str(tmp_path)]},
                     {"endpoint": "tab:jedi", "name": "jedi", "tab": M.tab_ref(jedi), "path": "/scratch/in"},
                     route="direct-push", runs_on="job", options={"partition": "xfer", "time": "2:00:00"}, plan=plan)
      app.transfers.add(t)
      app.edit_transfer(t); await pilot.pause(0.6)
      w = app.screen
      check("the wizard, editing", isinstance(w, TransferWizard) and w.editing is t)
      check("the destination as it was: fine though jedi is not connected", w.step_ok(1)[0], w.step_ok(1))
      check("its path cannot be changed now", w.query_one("#tw-dst-path").disabled and w.query_one("#tw-dst-browse").disabled)
      w.query_one("#tw-goto-2").press(); await pilot.pause(0.3)
      w.query_one("#tw-checksum").value = True; await pilot.pause(0.2)
      w.query_one("#tw-goto-3").press(); await pilot.pause(0.6)
      check("the route as it was", w.chosen_route() == "direct-push" and w.chosen_runs_on() == "job", (w.chosen_route(), w.chosen_runs_on()))
      check("not checked again: said", "Not checked again" in str(w.query_one("#tw-check-steps").render()) and w.checking is None,
            str(w.query_one("#tw-check-steps").render()))
      save = w.query_one("#tw-store")
      check("Save can", await until(pilot, lambda: not save.disabled, 5), save.tooltip)
      save.press(); await pilot.pause(0.5)
      check("saved: the option changed, the route, the plan kept", t.options.get("checksum") and t.route == "direct-push"
            and t.runs_on == "job" and t.plan.get("remaining_bytes") == 1000, (t.options, t.route, t.runs_on, t.plan))
      await pilot.press("q")

  asyncio.run(main())


def test_list_play_pause(check, tmp_path):
  """The list: a small first column with a button on each row: ▶ starts it (Run now), then ‖ pauses it, ▶ again
  continues it; a click on it does not open the details (elsewhere on the row: they open); Space does the same; its
  header does not sort"""
  from slurp.transfer import model as M
  from slurp.ui.screens.transfers import TransferScreen, TransfersScreen
  src, dst = files(tmp_path)
  (src / "big.bin").write_bytes(os.urandom(4_000_000))

  def cell(app):
    table = app.screen.query_one("#xfer-list")
    return str(table.get_row_at(0)[0])

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      t = M.Transfer(M.new_id(), "slow", {"tab": ref, "paths": [str(src)]}, {"tab": ref, "path": str(dst)}, route="relay", runs_on="slurp",
                     options={"bwlimit": 1})
      app.transfers.add(t)
      app.show_transfers(); await pilot.pause(0.6)
      table = app.screen.query_one("#xfer-list")
      check("the first column: the button, ▶", [c.value for c in table.columns][0] == "act" and cell(app) in ("▶", "\U000f040c"), cell(app))
      check("…a small one", table.columns[next(iter(table.columns))].get_render_width(table) <= 3)
      await pilot.click("#xfer-list", offset=(1, 1)); await pilot.pause(0.3)
      check("a click on ▶: started, the list stays", isinstance(app.screen, TransfersScreen)
            and await until(pilot, lambda: t.state == "running", 30), (type(app.screen).__name__, t.state, t.message))
      await until(pilot, lambda: cell(app) not in ("▶", "\U000f040c"), 5)
      check("…now ‖", cell(app) in ("‖", "\U000f03e5"), cell(app))
      await pilot.click("#xfer-list", offset=(1, 1))
      check("a click on ‖: paused", await until(pilot, lambda: t.state == "paused", 30), (t.state, t.message))
      await until(pilot, lambda: cell(app) in ("▶", "\U000f040c"), 5)
      check("…▶ again", cell(app) in ("▶", "\U000f040c"), cell(app))
      table.focus(); await pilot.press("space")
      check("Space on a paused one: it continues", await until(pilot, lambda: t.state == "running", 30), t.state)
      await pilot.press("space")
      check("Space again: paused", await until(pilot, lambda: t.state == "paused", 30), t.state)
      await pilot.click("#xfer-list", offset=(8, 1)); await pilot.pause(0.5)
      check("a click elsewhere on the row: its details", isinstance(app.screen, TransferScreen))
      await pilot.press("q")

  asyncio.run(main())


def test_list_sorted(check, tmp_path):
  """The list of transfers sorted by a click on a column's header (▲), the other way by another click (▼); by state:
  the running ones first; kept for the next start"""
  from textual.widgets import DataTable
  from slurp.transfer import model as M

  def names(app):
    table = app.screen.query_one("#xfer-list")
    return [str(table.get_row_at(i)[1]) for i in range(table.row_count)]

  async def header(app, pilot, key):
    table = app.screen.query_one("#xfer-list")
    column = next(k for k in table.columns if k.value == key)
    index = [k.value for k in table.columns].index(key)
    table.post_message(DataTable.HeaderSelected(table, column, index, table.columns[column].label))
    await pilot.pause(0.4)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      for name, state in (("beta", "done"), ("alpha", "new"), ("gamma", "running")):
        t = M.Transfer(M.new_id(), name, {"endpoint": "local", "tab": ref, "paths": ["/x"]}, {"endpoint": "local", "tab": ref, "path": "/y"})
        t.state = state
        app.transfers.store.transfers.append(t)
      app.transfers.changed(None)
      app.show_transfers(); await pilot.pause(0.6)
      check("first: in the order they were made", names(app) == ["beta", "alpha", "gamma"], names(app))
      await header(app, pilot, "name")
      table = app.screen.query_one("#xfer-list")
      label = lambda key: str(next(c.label for k, c in table.columns.items() if k.value == key))
      check("a click on Name: by name, ▲", names(app) == ["alpha", "beta", "gamma"] and label("name").endswith("▲"), (names(app), label("name")))
      await header(app, pilot, "name")
      check("again: the other way, ▼", names(app) == ["gamma", "beta", "alpha"] and label("name").endswith("▼"), names(app))
      await header(app, pilot, "state")
      check("by state: the running one first; the mark moves", names(app) == ["gamma", "beta", "alpha"] and label("state").endswith("▲")
            and not label("name").endswith(("▲", "▼")), (names(app), label("state"), label("name")))
      app.transfers.store.save()
      await pilot.press("q")

  async def again():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      app.show_transfers(); await pilot.pause(0.6)
      check("a new start: sorted as before", names(app) == ["gamma", "beta", "alpha"], names(app))
      await pilot.press("q")

  asyncio.run(main())
  asyncio.run(again())


def test_done_is_100(check):
  """A transfer that is done shows 100 %: in the list and its details, also when its run did not know its total"""
  from slurp.transfer import model as M
  from slurp.ui.screens.transfers import shown_percent

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      ref = M.tab_ref(app.clusters[0])
      t = M.Transfer(M.new_id(), "nothing left", {"endpoint": "local", "tab": ref, "paths": ["/x"]}, {"endpoint": "local", "tab": ref, "path": "/y"})
      t.state, t.progress = "done", {"percent": 37.0, "bytes": 0} # (as a run that found nothing to copy, or an older one)
      app.transfers.add(t)
      check("done: 100 %", shown_percent(t) == 100.0)
      app.show_transfers(); await pilot.pause(0.6)
      table = app.screen.query_one("#xfer-list")
      cell = table.get_row_at(0)[[c.value for c in table.columns].index("progress")]
      from rich.progress_bar import ProgressBar as RichProgressBar
      check("in the list: the job list's bar, full", isinstance(cell, RichProgressBar) and cell.completed == 100 and cell.width == 8, cell)
      app.open_transfer(t); await pilot.pause(0.6)
      details_bar = app.screen.query_one("#xd-bar")
      check("in its details: the job window's bar, full", details_bar.display and details_bar.percentage == 1.0, details_bar.percentage)
      t.state = "paused"
      check("paused: as far as it got", shown_percent(t) == 37.0)
      await pilot.press("q")

  asyncio.run(main())


def test_reload(check):
  """The views of the Transfers tab reload as the others: the countdown in the title bar, Reload (r): asked again now
  (the runs, the endpoints): all of them in the list, the transfer's own in its details"""
  from slurp.transfer import model as M
  from slurp.ui.screens.transfers import EndpointsScreen, TransferScreen, TransfersScreen

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      calls = []
      original = app.transfers.reload
      app.transfers.reload = lambda transfers=None: (calls.append(transfers), original(transfers))
      ref = M.tab_ref(app.clusters[0])
      t = M.Transfer(M.new_id(), "one", {"endpoint": "local", "tab": ref, "paths": ["/x"]}, {"endpoint": "local", "tab": ref, "path": "/y"})
      app.transfers.add(t)
      app.show_transfers(); await pilot.pause(0.8)
      timer = app.screen.query_one("#refresh-timer")
      check("the list: the countdown in the title bar", isinstance(app.screen, TransfersScreen) and timer.display and "⟳" in str(timer.label),
            str(timer.label))
      app.screen.query_one("#xfer-reload").press(); await pilot.pause(0.3)
      check("Reload: all asked again", calls and calls[-1] is None, calls)
      app.open_transfer(t); await pilot.pause(0.8)
      check("its details: the countdown too", isinstance(app.screen, TransferScreen) and app.screen.query_one("#refresh-timer").display)
      calls.clear()
      await pilot.press("r"); await pilot.pause(0.3)
      check("r: this transfer asked again", calls and calls[-1] == [t], calls)
      app.screen.query_one("#xd-reload").press(); await pilot.pause(0.3)
      check("…and its Reload button", len(calls) == 2, calls)
      await app.run_action("endpoints"); await pilot.pause(0.6)
      calls.clear()
      app.screen.query_one("#ep-reload").press(); await pilot.pause(0.3)
      check("the endpoints: the countdown, Reload", isinstance(app.screen, EndpointsScreen) and app.screen.query_one("#refresh-timer").display
            and calls == [None], calls)
      await pilot.press("q")

  asyncio.run(main())


def test_new_folder(check, tmp_path):
  """The destination's picker: New folder… makes a folder in the one shown and goes into it (Choose this folder takes
  it); a name with / is refused; one that is there: into it"""
  from slurp.ui.screens.transfers import PickPathsScreen
  (tmp_path / "old").mkdir()

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      chosen = []
      app.push_screen(PickPathsScreen(app.clusters[0], str(tmp_path), multi=False), chosen.append)
      await pilot.pause(0.6)
      picker = app.screen
      check("New folder… in the destination's picker", picker.query("#pp-new") and not picker.query_one("#pp-new-row").has_class("-open"))
      picker.query_one("#pp-new").press(); await pilot.pause(0.2)
      check("…its name asked", picker.query_one("#pp-new-row").has_class("-open") and picker.focused is picker.query_one("#pp-new-name"))
      picker.query_one("#pp-new-name").value = "a/b"
      picker.query_one("#pp-new-create").press(); await pilot.pause(0.3)
      check("a name with / refused", "no /" in str(picker.query_one("#pp-status").render()) and not (tmp_path / "a").exists())
      picker.query_one("#pp-new-name").value = "results"
      await pilot.press("enter")
      check("created, and into it", await until(pilot, lambda: picker.path == str(tmp_path / "results"), 10)
            and (tmp_path / "results").is_dir() and not picker.query_one("#pp-new-row").has_class("-open"), picker.path)
      picker.action_up(); await until(pilot, lambda: picker.path == str(tmp_path), 10)
      picker.query_one("#pp-new").press(); await pilot.pause(0.2)
      picker.query_one("#pp-new-name").value = "old"
      picker.query_one("#pp-new-create").press()
      check("one that is there: into it", await until(pilot, lambda: picker.path == str(tmp_path / "old"), 10), picker.path)
      picker.query_one("#pp-here").press(); await pilot.pause(0.3)
      check("Choose this folder: it", chosen == [[str(tmp_path / "old")]], chosen)
      await pilot.press("q")

  asyncio.run(main())


def test_picker_reload(check, tmp_path):
  """Reload in both pickers (the source's and the destination's): the folder shown listed again, new entries in it,
  gone ones out; the marks kept where their entries still are"""
  from slurp.ui.screens.transfers import PickPathsScreen
  for n in ("a", "b", "c"):
    (tmp_path / n).write_text(n)

  def names(picker):
    return {e["name"] for e in picker.entries}

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      app.push_screen(PickPathsScreen(app.clusters[0], str(tmp_path), multi=True))
      picker = app.screen
      await until(pilot, lambda: names(picker) == {"a", "b", "c"}, 10)
      check("a Reload button in the source's picker", bool(picker.query("#pp-reload")))
      picker.marked = {"a", "b"}
      (tmp_path / "b").unlink()
      (tmp_path / "d").write_text("d")
      picker.query_one("#pp-reload").press()
      check("reloaded: the new one in, the gone one out", await until(pilot, lambda: names(picker) == {"a", "c", "d"}, 10), names(picker))
      check("the marks kept where still there", picker.marked == {"a"}, picker.marked)
      await pilot.press("escape"); await pilot.pause(0.3)

      app.push_screen(PickPathsScreen(app.clusters[0], str(tmp_path), multi=False))
      picker = app.screen
      await until(pilot, lambda: "d" in names(picker), 10)
      check("a Reload button in the destination's picker", bool(picker.query("#pp-reload")))
      (tmp_path / "e").mkdir()
      picker.query_one("#pp-list").focus()
      await pilot.press("r")
      check("r reloads", await until(pilot, lambda: "e" in names(picker), 10), names(picker))
      await pilot.press("escape"); await pilot.pause(0.3)
      await pilot.press("q")

  asyncio.run(main())


def test_new_folder_checked(check, tmp_path):
  """New folder…: a folder that cannot be written: said (in the picker and as a note), not gone into; a place that
  keeps no empty folders (S3): the new folder shown in its folder all the same, with the maker's note"""
  from slurp.ui.screens.transfers import PickPathsScreen
  ro = tmp_path / "ro"
  ro.mkdir()
  (ro / "x").mkdir()
  (ro / "x").chmod(0o500)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      app.push_screen(PickPathsScreen(app.clusters[0], str(ro), multi=False))
      await pilot.pause(0.6)
      picker = app.screen
      notes = []
      app.notify = lambda message, **kw: notes.append((message, kw.get("severity")))
      picker.query_one("#pp-new").press(); await pilot.pause(0.2)
      picker.query_one("#pp-new-name").value = "x"
      picker.query_one("#pp-new-create").press(); await pilot.pause(0.2)
      check("there already: into it", await until(pilot, lambda: picker.path == str(ro / "x"), 10), picker.path)
      picker.query_one("#pp-new").press(); await pilot.pause(0.2)
      picker.query_one("#pp-new-name").value = "y"
      picker.query_one("#pp-new-create").press()
      check("in a folder that cannot be written: said", await until(pilot, lambda: "not created" in str(picker.query_one("#pp-status").render()), 10)
            and any(sev == "error" for _, sev in notes), (str(picker.query_one("#pp-status").render()), notes))
      check("…and not gone into", picker.path == str(ro / "x"))
      await pilot.press("escape"); await pilot.pause(0.3)

      listed = {"/": []} # (a place where an empty folder is not listed)
      app.push_screen(PickPathsScreen(None, "/", multi=False, lister=lambda p: list(listed.get(p, [])), name="bucket",
                                      maker=lambda p: "S3 keeps no empty folders"))
      await pilot.pause(0.6)
      picker = app.screen
      picker.query_one("#pp-new").press(); await pilot.pause(0.2)
      picker.query_one("#pp-new-name").value = "new"
      picker.query_one("#pp-new-create").press()
      check("made: into it, the note said", await until(pilot, lambda: picker.path == "/new", 10)
            and any("no empty folders" in m and "can be written" in m for m, _ in notes), (picker.path, notes))
      picker.action_up()
      check("…shown in its folder, though not listed", await until(pilot, lambda: picker.path == "/" and any(e["name"] == "new" for e in picker.entries), 10),
            picker.entries)
      await pilot.press("escape"); await pilot.pause(0.3)
      await pilot.press("q")

  try:
    asyncio.run(main())
  finally:
    (ro / "x").chmod(0o700)


def test_source_with_braces(check, tmp_path):
  """A source typed with braces: one line, counted over its paths (how many are there), the preview says how many;
  too many: refused"""
  for i in (1, 2):
    (tmp_path / f"run_{i}").mkdir()
    (tmp_path / f"run_{i}" / "out.h5").write_bytes(b"x" * 100)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await pilot.press("T"); await pilot.pause(0.4)
      w = app.screen
      field = w.query_one("#tw-src-path")
      field.value = f"{tmp_path}/run_{{1..3}}"
      field.focus(); await pilot.press("enter"); await pilot.pause(0.3)
      check("one source, as typed", w.paths == [f"{tmp_path}/run_{{1..3}}"], w.paths)
      check("counted over its paths: 2 of 3 there", await until(pilot, lambda: "2 of 3 paths there" in str(w.query_one("#tw-src-paths").render()), 10)
            and "200 Bytes" in str(w.query_one("#tw-src-paths").render()), str(w.query_one("#tw-src-paths").render()))
      w.dest_path = str(tmp_path / "dest"); w.refresh_preview(); await pilot.pause(0.1)
      check("the preview: each of its paths", "each of its 3 paths" in str(w.query_one("#tw-dst-preview").render()))
      field.value = f"{tmp_path}/{{1..20000}}"
      field.focus(); await pilot.press("enter"); await pilot.pause(0.3)
      check("too many: refused, said", len(w.paths) == 1 and any("10,000" in str(n.message) for n in app._notifications))
      await pilot.press("q")

  asyncio.run(main())


def test_edit_ssh_endpoint(check):
  """Endpoints: Edit… of a cluster tab: its SSH settings (the + dialog, filled in); its Globus link is a button of its
  own; this machine has neither"""
  from slurp.state import ClusterSession
  from slurp.ui.screens.dialogs import AddClusterScreen
  from slurp.ui.screens.globus import GlobusEndpointScreen
  from slurp.ui.screens.transfers import EndpointsScreen

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      app.clusters.append(ClusterSession("jedi", "jedi"))
      await app.run_action("endpoints"); await pilot.pause(0.6)
      screen = app.screen
      table = screen.query_one("#ep-list")
      table.move_cursor(row=table.get_row_index("tab:jedi")); await pilot.pause(0.3)
      check("a cluster tab: Edit… and Globus link…", isinstance(screen, EndpointsScreen) and not screen.query_one("#ep-edit").disabled
            and screen.query_one("#ep-globus").display)
      await pilot.press("e"); await pilot.pause(0.4)
      check("Edit…: its SSH settings, filled in", isinstance(app.screen, AddClusterScreen)
            and app.screen.query_one("#cluster-destination").value == "jedi", type(app.screen).__name__)
      app.screen.dismiss(None); await pilot.pause(0.3)
      await pilot.press("g"); await pilot.pause(0.4)
      check("Globus link…: the Globus form of the tab", isinstance(app.screen, GlobusEndpointScreen) and app.screen.tab is not None,
            type(app.screen).__name__)
      app.screen.dismiss(None); await pilot.pause(0.3)
      table.move_cursor(row=table.get_row_index("local")); await pilot.pause(0.3)
      check("this machine: no Edit…, no Globus link", screen.query_one("#ep-edit").disabled and not screen.query_one("#ep-globus").display)
      await pilot.press("q")

  asyncio.run(main())
