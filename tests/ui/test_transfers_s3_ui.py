"""S3 in the Transfers tab, against an S3 server in the test (moto) and the rclone of this machine: New endpoint… →
S3: the form (Test as a destination: listed and written), Save (the keys to the keyring only), connected; the wizard
from this machine into the bucket (the rclone route, files at a time), Add, Run now, Verify; Copy as a command
without the keys; deleting the endpoint (refused while used; then its keys go too)"""
import asyncio
import json
import os
import shutil

import pytest

from slurp.transfer import secrets as S
from slurp.ui.app import Slurp

pytestmark = pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")
moto_server = pytest.importorskip("moto.server")
boto3 = pytest.importorskip("boto3")


@pytest.fixture
def s3():
  import urllib.request
  server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
  server.start()
  host, port = server.get_host_and_port()
  url = f"http://{host}:{port}"
  urllib.request.urlopen(urllib.request.Request(url + "/moto-api/reset", method="POST")).read()
  client = boto3.client("s3", endpoint_url=url, aws_access_key_id="AK", aws_secret_access_key="SK", region_name="us-east-1")
  client.create_bucket(Bucket="data")
  yield url, client
  server.stop()


async def until(pilot, cond, seconds=15):
  for _ in range(int(seconds / 0.1)):
    if cond():
      return True
    await pilot.pause(0.1)
  return cond()


def test_s3_endpoint_and_transfer(check, s3, tmp_path):
  from slurp.ui.screens.s3 import S3EndpointScreen
  from slurp.ui.screens.transfers import NewEndpointScreen, TransferScreen, TransferWizard
  url, client = s3
  src = tmp_path / "run1"
  (src / "sub").mkdir(parents=True)
  (src / "sub" / "a.bin").write_bytes(os.urandom(200_000))
  (src / "b.txt").write_text("hello")

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      await app.run_action("endpoints"); await pilot.pause(0.5)
      app.screen.query_one("#ep-new").press(); await pilot.pause(0.3)
      check("New endpoint…: S3 can be made here", isinstance(app.screen, NewEndpointScreen) and not app.screen.query_one(".ne-s3").disabled)
      app.screen.query_one(".ne-s3").value = True; await pilot.pause(0.1)
      app.screen.query_one("#ne-next").press(); await pilot.pause(0.4)
      form = app.screen
      check("the S3 form", isinstance(form, S3EndpointScreen))
      for wid, value in (("name", "my bucket"), ("endpoint", url), ("region", "us-east-1"), ("bucket", "data"),
                         ("access_key", "AK"), ("secret_key", "SK")):
        form.query_one(f"#s3-{wid}").value = value
      form.query_one("#s3-write").value = True
      form.query_one("#s3-test").press()
      check("Test as a destination: listed and written", await until(pilot, lambda: "can be written" in str(form.query_one("#s3-result").render())),
            str(form.query_one("#s3-result").render()))
      form.query_one("#s3-save").press(); await pilot.pause(0.4)
      store = app.transfers.endpoint_store
      d = next((d for d in store.defs if d.kind == "s3"), None)
      check("saved", d is not None and d.settings["bucket"] == "data" and d.secrets == ["access_key", "secret_key"], d)
      check("the keys only in the keyring", S.get(S.user(d.id, "secret_key")) == "SK"
            and "SK" not in open(store.path).read().replace("SKIP", ""), json.load(open(store.path)))
      check("connected (tested after saving)", await until(pilot, lambda: app.transfers.endpoint_status.get(d.id, ("",))[0] == "connected"),
            app.transfers.endpoint_status.get(d.id))

      await app.run_action("transfer_data"); await pilot.pause(0.5)
      w = app.screen
      check("the wizard offers it", isinstance(w, TransferWizard) and d.id in {v for _, v in w.endpoint_options()})
      field = w.query_one("#tw-src-path")
      await until(pilot, lambda: not field.disabled)
      field.value = str(src)
      field.focus(); await pilot.press("enter"); await pilot.pause(0.2)
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.3)
      w.query_one("#tw-dst-tab").value = d.id
      await until(pilot, lambda: w.live_state("dst")[0] == "connected" and not w.query_one("#tw-dst-path").disabled)
      w.query_one("#tw-dst-path").value = "in"; await pilot.pause(0.2)
      check("a path in the bucket", w.dest_path == "/in", w.dest_path)
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.3)
      check("options: files at a time, not compression", w.query_one("#tw-transfers-row").display and not w.query_one("#tw-compress-row").display)
      w.query_one("#tw-transfers").value = "2"
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.3)
      check("the route: rclone on this machine, checked and chosen", await until(pilot, lambda: w.chosen_route() == "rclone-here" and w.step_ok(3)[0]),
            (w.chosen_route(), w.plan))
      shown = [b.route for b in w.query("#tw-route RadioButton") if b.display]
      check("only rclone", shown == ["rclone-here"], shown)
      check("the check: what it holds, the bucket written", "can be written" in str(w.query_one("#tw-check-steps").render()),
            str(w.query_one("#tw-check-steps").render()))
      check("no keys question (it runs here)", not w.query_one("#tw-secrets").display)
      await until(pilot, lambda: not w.query_one("#tw-next").disabled)
      w.query_one("#tw-next").press(); await pilot.pause(0.4)
      w.query_one("#tw-go").press(); await pilot.pause(0.6)
      t = next(iter(app.transfers.transfers), None)
      check("added", t is not None and t.route == "rclone-here" and t.options.get("transfers") == 2, t and (t.route, t.options))
      check("its details", isinstance(app.screen, TransferScreen))
      run = app.screen.query_one("#xd-run")
      check("Run now ready", await until(pilot, lambda: not run.disabled), run.tooltip)
      run.press()
      check("done", await until(pilot, lambda: t.state == "done", 30), (t.state, t.message))
      found = sorted(o["Key"] for o in client.list_objects_v2(Bucket="data").get("Contents", []))
      check("in the bucket", found == ["in/run1/b.txt", "in/run1/sub/a.bin"], found)
      verify = app.screen.query_one("#xd-verify")
      check("Verify there", verify.display)
      await until(pilot, lambda: not verify.disabled)
      verify.press()
      check("Verify: the same", await until(pilot, lambda: any("verified: everything" in e[1] for e in t.log), 30), t.log[-2:])
      text = app.transfers.command_text(t)
      check("Copy as a command: no key in it", "SK" not in text.replace("SKIP", "") and "$AWS_SECRET_ACCESS_KEY" in text, text)

      await app.run_action("endpoints"); await pilot.pause(0.5)
      app.delete_endpoint(app.endpoint_named(d.id)); await pilot.pause(0.2)
      check("deleting it while used: refused", store.get(d.id) is not None)
      app.transfers.delete(t)
      app.delete_endpoint(app.endpoint_named(d.id)); await pilot.pause(0.2)
      check("deleted: its keys gone from the keyring", store.get(d.id) is None and S.get(S.user(d.id, "secret_key")) is None)
      await pilot.press("q")

  asyncio.run(main())
