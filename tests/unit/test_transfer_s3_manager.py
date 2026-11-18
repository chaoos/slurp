"""Transfers with S3 (rclone on this machine, an S3 server in the test): the check (rclone, what the sources hold, the
destination listed and written), a run with its progress, Pause and Continue (what is there is skipped), a run again
copies nothing, Verify, Copy as a command (the keys as placeholders), the keys only in the keyring"""
import os
import queue
import shutil
import time
import types

import pytest

from slurp.transfer import endpoints as E, manager as MG, model as M, secrets as S

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


class App:
  def __init__(self):
    self.clusters = [types.SimpleNamespace(name="Local", destination=None, connection=None, ssh=None, pre_login="")]
    self.calls, self.notes = queue.Queue(), []

  def call_from_thread(self, fn, *args):
    self.calls.put((fn, args))

  def notify(self, message, title="", severity="information", timeout=None):
    self.notes.append((title, message, severity))

  def pump(self):
    while True:
      try:
        fn, args = self.calls.get_nowait()
      except queue.Empty:
        return
      fn(*args)


def until(app, m, cond, seconds=30):
  end = time.time() + seconds
  while time.time() < end:
    app.pump()
    m.tick()
    if cond():
      return True
    time.sleep(0.05)
  app.pump()
  return cond()


def keys(client):
  return sorted(o["Key"] for o in client.list_objects_v2(Bucket="data").get("Contents", []))


def setup(tmp_path, url):
  store = E.EndpointStore(str(tmp_path / "endpoints.json"))
  store.add(E.EndpointDef("s1", "my bucket", "s3", {"provider": "Other", "endpoint": url, "region": "us-east-1", "bucket": "data"},
                          ["access_key", "secret_key"]))
  S.put(S.user("s1", "access_key"), "AK")
  S.put(S.user("s1", "secret_key"), "SK")
  src = tmp_path / "src" / "run1"
  (src / "sub").mkdir(parents=True)
  (src / "sub" / "big.bin").write_bytes(os.urandom(4_000_000))
  (src / "b.txt").write_text("hello")
  return store, src


def test_up_pause_continue_verify(check, s3, tmp_path):
  url, client = s3
  store, src = setup(tmp_path, url)
  app = App()
  m = MG.Manager(app, M.Store(str(tmp_path / "transfers.json")), store)
  check("this machine and S3: rclone here", E.rclone_routes(store, {"endpoint": "local"}, {"endpoint": "s1"}) == ["rclone-here"])
  check("a tab and S3: rclone on the cluster", E.rclone_routes(store, {"endpoint": "tab:jedi"}, {"endpoint": "s1"}) == ["rclone-cluster"])
  t = M.Transfer(M.new_id(), "up", {"endpoint": "local", "paths": [str(src)]}, {"endpoint": "s1", "name": "my bucket", "path": "/in"},
                 route="rclone-here", runs_on="slurp", options={"bwlimit": "1"})
  m.add(t)
  plan = m.rclone_check(t, lambda p: None)
  check("the check: passed, what the sources hold, the destination written", plan["routes"]["rclone-here"]["ok"]
        and plan["total_bytes"] == 4_000_005 and "can be written" in plan["steps"]["rclone"]["text"], plan)
  t.plan = plan
  text = m.command_text(t)
  check("Copy as a command: rclone copy, the keys as placeholders", "rclone --config '' copy" in text and "SK" not in text.replace("SKIP", "")
        and "=$AWS_SECRET_ACCESS_KEY" in text and "export RCLONE_CONFIG_" in text, text)
  m.run_now(t)
  check("running, with its progress", until(app, m, lambda: t.state == "running" and t.progress.get("bytes", 0) > 0), (t.state, t.progress))
  m.pause(t)
  check("paused", until(app, m, lambda: t.state == "paused"), (t.state, t.message))
  check("paused mid-run: Continue", m.paused_mid_run(t))
  t.options = {}
  m.resume(t)
  check("Continue: done", until(app, m, lambda: t.state == "done", 60), (t.state, t.message))
  check("all of it there", keys(client) == ["in/run1/b.txt", "in/run1/sub/big.bin"], keys(client))
  m.run_now(t)
  check("a run again: done, nothing copied", until(app, m, lambda: t.state == "done" and len(t.runs) == 3, 30) and t.runs[-1]["files"] == 0, t.runs[-1:])
  results = []
  m.verify(t, results.append)
  check("Verify: the same", until(app, m, lambda: results, 30) and results[0][0], results)
  client.put_object(Bucket="data", Key="in/run1/b.txt", Body=b"changed")
  results.clear()
  m.verify(t, results.append)
  check("Verify: a difference found", until(app, m, lambda: results, 30) and not results[0][0] and "1 difference" in results[0][1], results)


def test_down_and_errors(check, s3, tmp_path):
  url, client = s3
  store, src = setup(tmp_path, url)
  client.put_object(Bucket="data", Key="runs/a.h5", Body=b"1" * 100)
  client.put_object(Bucket="data", Key="runs/b.h5", Body=b"2" * 200)
  client.put_object(Bucket="data", Key="runs/c.txt", Body=b"3")
  app = App()
  m = MG.Manager(app, M.Store(str(tmp_path / "transfers.json")), store)
  down = tmp_path / "down"
  t = M.Transfer(M.new_id(), "down", {"endpoint": "s1", "paths": ["/runs/*.h5"]}, {"endpoint": "local", "path": str(down)},
                 route="rclone-here", runs_on="slurp")
  t.plan = {"routes": {"rclone-here": {"ok": True}}}
  m.add(t)
  m.run_now(t)
  check("down: each match", until(app, m, lambda: t.state == "done") and sorted(os.listdir(down)) == ["a.h5", "b.h5"],
        (t.state, t.message, os.listdir(down) if down.exists() else None))
  S.delete(S.user("s1", "secret_key"))
  check("a key missing: not set up, said", E.default_state(store.get("s1"))[0] == "not set up")
  plan = m.rclone_check(t, lambda p: None)
  check("the check says so", not plan["routes"]["rclone-here"]["ok"] and "keyring has no secret key" in plan["routes"]["rclone-here"]["why"], plan)
