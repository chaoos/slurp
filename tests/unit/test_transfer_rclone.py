"""S3 through rclone, against an S3 server in the test (moto) and the rclone of this machine: remotes from the
environment (no config file, no key on a command line), listing, sizes, Test (a write test for destinations),
copies both ways (idempotent: a run again copies nothing), patterns, the statistics as progress, and the command of
a run on a cluster (its keys file read and deleted, its progress lines as the runner reads rsync's)"""
import os
import shutil
import subprocess

import pytest

from slurp.transfer import endpoints as E, rclone as RC, secrets as S

pytestmark = pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")
moto_server = pytest.importorskip("moto.server")
boto3 = pytest.importorskip("boto3")


@pytest.fixture
def s3():
  server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
  server.start()
  host, port = server.get_host_and_port()
  url = f"http://{host}:{port}"
  import urllib.request
  urllib.request.urlopen(urllib.request.Request(url + "/moto-api/reset", method="POST")).read() # (empty: each test its own)
  client = boto3.client("s3", endpoint_url=url, aws_access_key_id="AK", aws_secret_access_key="SK", region_name="us-east-1")
  client.create_bucket(Bucket="data")
  yield url, client
  server.stop()


def endpoint(url, eid="s1"):
  d = E.EndpointDef(eid, "my bucket", "s3", {"provider": "Other", "endpoint": url, "region": "us-east-1", "bucket": "data"},
                    ["access_key", "secret_key"])
  S.put(S.user(eid, "access_key"), "AK")
  S.put(S.user(eid, "secret_key"), "SK")
  return RC.Endpoint(d)


def tree(tmp_path):
  src = tmp_path / "src" / "run1"
  (src / "sub").mkdir(parents=True)
  (src / "sub" / "a.bin").write_bytes(os.urandom(300_000))
  (src / "b.txt").write_text("hello")
  (tmp_path / "src" / "x.h5").write_bytes(b"1" * 1000)
  (tmp_path / "src" / "y.h5").write_bytes(b"2" * 2000)
  return tmp_path / "src"


def test_remote_and_keys(check, s3):
  url, _ = s3
  e = endpoint(url)
  check("the keys only in the environment", e.env[f"RCLONE_CONFIG_{e.name}_SECRET_ACCESS_KEY"] == "SK")
  check("paths: the bucket's root and keys", e.path("/") == f"{e.name}:data" and e.path("/runs/2026/") == f"{e.name}:data/runs/2026", e.path("/runs/2026/"))
  d = E.EndpointDef("s2", "no keys", "s3", {"bucket": "data"}, ["access_key", "secret_key"])
  try:
    RC.keys_of(d)
    check("no keys in the keyring: said", False)
  except RC.RcloneError as err:
    check("no keys in the keyring: said", "keyring has no access key" in str(err), str(err))
  check("placeholders for Copy as a command", RC.placeholders(d)["secret_key"] == "$AWS_SECRET_ACCESS_KEY")
  check("rclone's version", RC.version_of("rclone v1.60.1-DEV\n- os") == (1, 60, 1) and RC.version() is not None)


def test_list_size_test(check, s3, tmp_path):
  url, client = s3
  e = endpoint(url)
  client.put_object(Bucket="data", Key="runs/a.txt", Body=b"12345")
  client.put_object(Bucket="data", Key="runs/deep/b.txt", Body=b"123")
  entries = RC.lsjson(e.env, e.path("/runs"))
  check("a folder listed", sorted((x["name"], x["dir"], x["size"]) for x in entries) == [("a.txt", False, 5), ("deep", True, 0)], entries)
  check("kinds", RC.kind(e.env, e.path("/runs")) == "dir" and RC.kind(e.env, e.path("/runs/a.txt")) == "file"
        and RC.kind(e.env, e.path("/runs/nothing")) is None and RC.kind(e.env, e.path("/")) == "dir")
  check("size", RC.size(e.env, [e.path("/runs")]) == {"bytes": 8, "files": 2})
  check("Test: listed", "lists 2 entries" in RC.test(e.env, e.path("/runs")))
  words = RC.test(e.env, e.path("/runs"), write=True)
  check("Test as a destination: written and deleted", "can be written" in words and
        not [o["Key"] for o in client.list_objects_v2(Bucket="data").get("Contents", []) if ".slurp-test" in o["Key"]], words)
  bad = RC.Endpoint(E.EndpointDef("s3", "gone", "s3", {**e.settings, "bucket": "gone"}, []), keys={"access_key": "AK", "secret_key": "SK"})
  try:
    RC.test(bad.env, bad.path("/"))
    check("no such bucket: said", False)
  except RC.RcloneError as err:
    check("no such bucket: said", "no such bucket" in str(err).lower(), str(err))


def test_copy_both_ways(check, s3, tmp_path):
  url, client = s3
  e = endpoint(url)
  src = tree(tmp_path)
  sources = [(str(src / "run1"), True), (str(src / "*.h5"), False)]
  stats, rc = [], 0
  for folder, rules, _ in RC.groups(sources):
    result = subprocess.run(["rclone", "--config", "", *RC.copy_argv(folder, e.path("/in"), rules, {})], capture_output=True,
                            text=True, env={**os.environ, **e.env})
    rc |= result.returncode
    stats += [s for s in map(RC.stats_of, result.stderr.splitlines()) if s]
  keys = sorted(o["Key"] for o in client.list_objects_v2(Bucket="data").get("Contents", []))
  check("up: the folder under its name, each match", rc == 0 and keys == ["in/run1/b.txt", "in/run1/sub/a.bin", "in/x.h5", "in/y.h5"], keys)
  check("its statistics", stats and stats[-1]["bytes"] == 300_000 + 5 + 3000 and stats[-1]["files"] == 4, stats[-1:])
  again = subprocess.run(["rclone", "--config", "", *RC.copy_argv(str(src), e.path("/in"), RC.groups(sources)[0][1], {})],
                         capture_output=True, text=True, env={**os.environ, **e.env})
  last = [s for s in map(RC.stats_of, again.stderr.splitlines()) if s][-1]
  check("again: nothing copied (idempotent)", again.returncode == 0 and last["files"] == 0, last)
  down = tmp_path / "down"
  for folder, rules, _ in RC.groups([("/in/run1", True)]):
    subprocess.run(["rclone", "--config", "", *RC.copy_argv(e.path(folder), str(down), rules, {"checksum": True})], check=True,
                   capture_output=True, env={**os.environ, **e.env})
  check("down: the folder under its name", (down / "run1" / "sub" / "a.bin").read_bytes() == (src / "run1" / "sub" / "a.bin").read_bytes())
  check("outcomes", RC.outcome(0) == ("done", "") and RC.outcome(6)[0] == "done-errors" and RC.outcome(7)[0] == "failed"
        and RC.outcome(143, stopped=True)[0] == "paused")


def test_cluster_command(check, s3, tmp_path):
  """The command a run on a cluster runs (here: this machine): the keys read from its file and deleted, rclone's
  statistics as progress lines (as rsync's), its errors on stderr"""
  from slurp.transfer import model as M
  url, client = s3
  e = endpoint(url)
  src = tree(tmp_path)
  folder = tmp_path / "run"
  folder.mkdir()
  t = M.Transfer("t1", "up", {"paths": [str(src / "run1")]}, {"path": "/up"}, options={"transfers": 2})
  (folder / "keys").write_text(RC.keys_file(e.env))
  (folder / "command").write_text(RC.cluster_command(t, "source", e, [], [(str(src / "run1"), True)], "/up"))
  env = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_CONFIG_")}
  result = subprocess.run(["sh", str(folder / "command")], capture_output=True, text=True, env=env, timeout=120)
  lines = result.stdout.splitlines()
  check("ended well", result.returncode == 0, result.stderr[-400:])
  check("the keys file is gone", not (folder / "keys").exists())
  check("progress lines as rsync's", any(l.split()[1].endswith("%") and "MB/s" in l and "xfr#" in l for l in lines if len(l.split()) > 3), lines[-3:])
  keys = sorted(o["Key"] for o in client.list_objects_v2(Bucket="data").get("Contents", []))
  check("copied", keys == ["up/run1/b.txt", "up/run1/sub/a.bin"], keys)
  check("no key in the command", "SK" not in (folder / "command").read_text().replace("SKIP", ""))


def test_cluster_command_pause(check, s3, tmp_path):
  """Pause of a run on a cluster: TERM to its command reaches rclone at once; it ends with 143, the next groups do not run"""
  import signal
  import time
  from slurp.transfer import model as M
  url, client = s3
  e = endpoint(url)
  big = tmp_path / "big"
  big.mkdir()
  (big / "f.bin").write_bytes(os.urandom(6_000_000))
  (tmp_path / "later").mkdir()
  (tmp_path / "later" / "other.bin").write_bytes(b"x") # (another folder: another rclone, after the first)
  folder = tmp_path / "run"
  folder.mkdir()
  t = M.Transfer("t2", "slow", {"paths": []}, {"path": "/slow"}, options={"bwlimit": "0.5"})
  (folder / "keys").write_text(RC.keys_file(e.env))
  (folder / "command").write_text(RC.cluster_command(t, "source", e, [], [(str(big), True), (str(tmp_path / "later" / "other.bin"), False)], "/slow"))
  proc = subprocess.Popen(["sh", str(folder / "command")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
  time.sleep(3)
  started = time.time()
  proc.send_signal(signal.SIGTERM)
  out, err = proc.communicate(timeout=30)
  check("paused at once: 143", proc.returncode == 143 and time.time() - started < 10, (proc.returncode, err[-300:]))
  keys = [o["Key"] for o in client.list_objects_v2(Bucket="data").get("Contents", [])]
  check("the next group did not run", "slow/other.bin" not in keys, keys)
  for _ in range(50): # (rclone ends once it has cleaned up)
    if subprocess.run(["pgrep", "-f", f"rclone --config  copy .*{big}"], capture_output=True).returncode:
      break
    time.sleep(0.1)
  check("no rclone left", subprocess.run(["pgrep", "-f", f"rclone --config  copy .*{big}"], capture_output=True).returncode != 0)


def test_tab_over_ssh(check, monkeypatch):
  """A cluster tab as an rclone remote (rclone here, the cluster without rclone): sftp through slurp's own SSH session
  (its control socket, no new login, never a question); for Copy as a command plain ssh; rclone 1.64 needed"""
  import types
  conn = types.SimpleNamespace(control_path="/tmp/slurp-1-x/control", destination="me@daint", ready=True)
  tab = RC.TabEnd(types.SimpleNamespace(destination="daint", connection=conn))
  ssh = tab.env[f"RCLONE_CONFIG_{tab.name}_SSH"]
  check("sftp, on slurp's session", tab.env[f"RCLONE_CONFIG_{tab.name}_TYPE"] == "sftp"
        and ssh == "ssh -S /tmp/slurp-1-x/control -o ControlMaster=no -o BatchMode=yes me@daint", ssh)
  check("absolute paths", tab.path("/scratch/run1") == f"{tab.name}:/scratch/run1")
  plain = RC.TabEnd(types.SimpleNamespace(destination="daint", connection=conn), placeholder=True)
  check("Copy as a command: plain ssh", plain.env[f"RCLONE_CONFIG_{plain.name}_SSH"] == "ssh daint")
  check("a word with a space: quoted", RC.ssh_value(["ssh", "-S", "/a b/c", 'x"y']) == 'ssh -S "/a b/c" "x""y"')
  monkeypatch.setattr(RC, "version", lambda: (1, 60, 1))
  ok, why = RC.relay_ready()
  check("rclone 1.60 here: not ready, said why", not ok and "1.64 or newer" in why and "1.60.1" in why, why)
  monkeypatch.setattr(RC, "version", lambda: (1, 66, 0))
  check("rclone 1.66: ready", RC.relay_ready() == (True, ""))
