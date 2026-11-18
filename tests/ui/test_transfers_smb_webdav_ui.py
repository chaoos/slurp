"""SMB and WebDAV in the Transfers tab: New endpoint… offers both; the WebDAV form against a WebDAV server in the test
(rclone serve webdav): Test as a destination, Save (the password to the keyring only), connected, a transfer from this
machine into it (rclone here) that runs; the SMB form: its fields, what is required, the password to the keyring"""
import asyncio
import shutil
import socket
import subprocess
import time

import pytest

from slurp.transfer import secrets as S
from slurp.ui.app import Slurp

pytestmark = pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")


def free_port():
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


@pytest.fixture
def webdav(tmp_path):
  root, port = tmp_path / "davroot", free_port()
  root.mkdir()
  proc = subprocess.Popen(["rclone", "--config", "", "serve", "webdav", str(root), "--addr", f"127.0.0.1:{port}", "--user", "dav",
                           "--pass", "pw456"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  end = time.time() + 15
  while time.time() < end:
    try:
      socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
      break
    except OSError:
      time.sleep(0.2)
  yield port, root
  proc.kill()


async def until(pilot, cond, seconds=15):
  for _ in range(int(seconds / 0.1)):
    if cond():
      return True
    await pilot.pause(0.1)
  return cond()


async def new_endpoint(app, pilot, kind):
  from slurp.ui.screens.transfers import NewEndpointScreen
  app.screen.query_one("#ep-new").press(); await pilot.pause(0.3)
  chooser = app.screen
  ok = isinstance(chooser, NewEndpointScreen) and not chooser.query_one(f".ne-{kind}").disabled
  chooser.query_one(f".ne-{kind}").value = True; await pilot.pause(0.1)
  chooser.query_one("#ne-next").press(); await pilot.pause(0.4)
  return ok, app.screen


def test_webdav_and_smb_forms(check, webdav, tmp_path):
  from slurp.ui.screens.rclone_form import RcloneEndpointScreen
  from slurp.ui.screens.transfers import TransferScreen
  port, root = webdav
  src = tmp_path / "run1"
  src.mkdir()
  (src / "a.txt").write_text("hello")

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await app.run_action("endpoints"); await pilot.pause(0.5)
      store = app.transfers.endpoint_store

      ok, form = await new_endpoint(app, pilot, "smb")
      check("SMB can be made here", ok)
      check("the SMB form: server, share, user, domain, port, password", isinstance(form, RcloneEndpointScreen) and form.kind == "smb"
            and all(form.query(f"#rc-{k}") for k in ("host", "share", "user", "domain", "port", "password")))
      form.query_one("#rc-name").value = "files"
      form.query_one("#rc-host").value = "files.example.org"
      form.query_one("#rc-save").press(); await pilot.pause(0.2)
      check("…the share is required", "the share" in str(form.query_one("#rc-result").render()), str(form.query_one("#rc-result").render()))
      form.query_one("#rc-share").value = "data"
      form.query_one("#rc-user").value = "me"
      form.query_one("#rc-save").press(); await pilot.pause(0.2)
      check("…and the password", "the password" in str(form.query_one("#rc-result").render()))
      form.query_one("#rc-password").value = "s3cret"
      form.query_one("#rc-save").press(); await pilot.pause(0.5)
      smb = next((d for d in store.defs if d.kind == "smb"), None)
      check("saved: its settings, the password only in the keyring", smb is not None and smb.settings == {"host": "files.example.org", "share": "data", "user": "me"}
            and S.get(S.user(smb.id, "password")) == "s3cret" and "s3cret" not in open(store.path).read(), smb)

      ok, form = await new_endpoint(app, pilot, "webdav")
      check("WebDAV can be made here; its form", ok and isinstance(form, RcloneEndpointScreen) and form.kind == "webdav")
      for wid, value in (("name", "dav"), ("url", f"http://127.0.0.1:{port}"), ("user", "dav"), ("password", "pw456")):
        form.query_one(f"#rc-{wid}").value = value
      form.query_one("#rc-write").value = True
      form.query_one("#rc-test").press()
      check("Test as a destination: listed and written", await until(pilot, lambda: "can be written" in str(form.query_one("#rc-result").render())),
            str(form.query_one("#rc-result").render()))
      form.query_one("#rc-save").press(); await pilot.pause(0.4)
      dav = next((d for d in store.defs if d.kind == "webdav"), None)
      check("saved", dav is not None and dav.secrets == ["password"] and S.get(S.user(dav.id, "password")) == "pw456", dav)
      check("connected (tested after saving)", await until(pilot, lambda: app.transfers.endpoint_status.get(dav.id, ("",))[0] == "connected"),
            app.transfers.endpoint_status.get(dav.id))

      await app.run_action("transfer_data"); await pilot.pause(0.5)
      w = app.screen
      field = w.query_one("#tw-src-path")
      await until(pilot, lambda: not field.disabled)
      field.value = str(src)
      field.focus(); await pilot.press("enter"); await pilot.pause(0.2)
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.3)
      w.query_one("#tw-dst-tab").value = dav.id
      await until(pilot, lambda: w.live_state("dst")[0] == "connected" and not w.query_one("#tw-dst-path").disabled)
      w.query_one("#tw-dst-path").value = "in"; await pilot.pause(0.2)
      for _ in range(3): # (options, route, summary)
        await until(pilot, lambda: not w.query_one("#tw-next").disabled, 20)
        w.query_one("#tw-next").press(); await pilot.pause(0.4)
      check("the route: rclone on this machine", w.chosen_route() == "rclone-here", w.chosen_route())
      w.query_one("#tw-go").press(); await pilot.pause(0.6)
      t = next(iter(app.transfers.transfers), None)
      check("its details", isinstance(app.screen, TransferScreen) and t is not None)
      run = app.screen.query_one("#xd-run")
      await until(pilot, lambda: not run.disabled)
      run.press()
      check("done: in the WebDAV folder", await until(pilot, lambda: t.state == "done", 30)
            and (root / "in" / "run1" / "a.txt").read_text() == "hello", (t.state, t.message))
      await pilot.press("q")

  asyncio.run(main())


def test_new_folder_on_webdav(check, webdav):
  """New folder… in the picker of a WebDAV endpoint: made with rclone, then into it"""
  from slurp.transfer import endpoints as E, rclone as RC
  from slurp.ui.screens.transfers import PickPathsScreen
  port, root = webdav
  d = E.EndpointDef("w2", "dav", "webdav", {"url": f"http://127.0.0.1:{port}", "user": "dav"}, ["password"])
  e = RC.Endpoint(d, {"password": "pw456"})

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      app.push_screen(PickPathsScreen(None, "/", multi=False, lister=lambda p: RC.lsjson(e.env, e.path(p)), name="dav",
                                      maker=lambda p: RC.check(RC.run(["mkdir", e.path(p)], e.env))))
      await pilot.pause(0.6)
      picker = app.screen
      picker.query_one("#pp-new").press(); await pilot.pause(0.2)
      picker.query_one("#pp-new-name").value = "made"
      picker.query_one("#pp-new-create").press()
      check("made on the WebDAV server, and into it", await until(pilot, lambda: picker.path == "/made", 15) and (root / "made").is_dir(),
            (picker.path, str(picker.query_one("#pp-status").render())))
      await pilot.press("q")

  asyncio.run(main())


def shown(widget):
  """The text a Static shows (a Rich table too)"""
  from rich.console import Console
  console = Console(width=200, record=True, file=open("/dev/null", "w"))
  console.print(widget.content)
  return console.export_text()


def test_endpoint_window(check, webdav):
  """Enter on an endpoint in the Endpoints: its window. WebDAV: its settings, its secret only as there or not (never
  its value), its connection (rclone here, the last test), Test: the course, connected; Disconnect; Copy address. This
  machine (a tab): its connection window"""
  from slurp.transfer import endpoints as E
  from slurp.ui.screens.connection import ConnectionScreen
  from slurp.ui.screens.endpoint import EndpointScreen
  port, root = webdav

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      store = app.transfers.endpoint_store
      store.add(E.EndpointDef("w9", "dav", "webdav", {"url": f"http://127.0.0.1:{port}", "user": "dav"}, ["password"]))
      S.put(S.user("w9", "password"), "pw456")
      await app.run_action("endpoints"); await pilot.pause(0.6)
      screen = app.screen
      table = screen.query_one("#ep-list")
      rows = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
      table.move_cursor(row=rows.index("w9")); await pilot.pause(0.2)
      table.focus(); await pilot.press("enter"); await pilot.pause(0.6)
      w = app.screen
      check("Enter: its window", isinstance(w, EndpointScreen), type(w).__name__)
      settings = shown(w.query_one("#epd-settings"))
      check("its settings: the URL, the user; the password there, its value not", f"http://127.0.0.1:{port}" in settings and "dav" in settings
            and "password ✓" in settings and "pw456" not in settings, settings)
      connection = shown(w.query_one("#epd-connection"))
      check("its connection: rclone here", "rclone" in connection and "on this machine" in connection, connection)
      await pilot.press("t")
      check("Test: connected, in the course, the last test", await until(pilot, lambda: "connected" in shown(w.query_one("#epd-course"))
                                                                       and "took" in shown(w.query_one("#epd-connection"))),
            (shown(w.query_one("#epd-course")), shown(w.query_one("#epd-connection"))))
      check("…its border green", w.query_one("#epd-dialog").has_class("-good"))
      w.query_one("#epd-disconnect").press(); await pilot.pause(0.3)
      check("Disconnect: not connected", app.transfers.endpoint_status["w9"][0] == "not connected" and "disconnected" in shown(w.query_one("#epd-course")))
      copied = []
      import slurp.ui.screens.dialogs as D
      original = D.copy_text
      D.copy_text = lambda app_, text, what="": copied.append(text)
      try:
        w.query_one("#epd-copy").press(); await pilot.pause(0.2)
      finally:
        D.copy_text = original
      check("Copy address: its URL", copied == [f"http://127.0.0.1:{port}/"], copied)
      await pilot.press("escape"); await pilot.pause(0.3)
      table = app.screen.query_one("#ep-list")
      rows = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
      table.move_cursor(row=rows.index("local")); await pilot.pause(0.2)
      table.focus(); await pilot.press("enter"); await pilot.pause(0.6)
      check("this machine: its connection window", isinstance(app.screen, ConnectionScreen), type(app.screen).__name__)
      await pilot.press("escape"); await pilot.pause(0.3)
      await pilot.press("q")

  asyncio.run(main())
