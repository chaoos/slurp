"""SMB and WebDAV through rclone, against servers in the test (an SMB server of impacket, rclone serve webdav): the
password given to rclone obscured (in its environment, never on a command line), listing, Test with the write test,
copies through the manager, Verify, Copy as a command (the password a placeholder, obscured by the command itself).
WebDAV: both ways, a run again copies nothing. SMB: up into the share's root (what impacket's server does right: it
takes no files in folders, keeps coarser times, answers reads badly; Samba and Windows do not have these limits)"""
import os
import queue
import shutil
import socket
import subprocess
import sys
import time
import types

import pytest

from slurp.transfer import endpoints as E, manager as MG, model as M, rclone as RC, secrets as S

pytestmark = pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")

SMB_SERVER = r"""
import sys
from impacket import ntlm, smbserver
s = smbserver.SimpleSMBServer(listenAddress="127.0.0.1", listenPort=int(sys.argv[1]))
s.addShare("DATA", sys.argv[2], "")
s.setSMB2Support(True)
s.addCredential("user", 1000, "aad3b435b51404eeaad3b435b51404ee", ntlm.compute_nthash("pw123").hex())
s.setSMBChallenge("")
s.start()
"""


def free_port():
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def wait_port(port, seconds=15):
  end = time.time() + seconds
  while time.time() < end:
    try:
      socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
      return True
    except OSError:
      time.sleep(0.2)
  return False


@pytest.fixture
def smb(tmp_path):
  pytest.importorskip("impacket")
  share, port = tmp_path / "smbshare", free_port()
  share.mkdir()
  proc = subprocess.Popen([sys.executable, "-c", SMB_SERVER, str(port), str(share)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  assert wait_port(port), "the SMB server did not start"
  yield port, share
  proc.kill()


@pytest.fixture
def webdav(tmp_path):
  root, port = tmp_path / "davroot", free_port()
  root.mkdir()
  proc = subprocess.Popen(["rclone", "--config", "", "serve", "webdav", str(root), "--addr", f"127.0.0.1:{port}", "--user", "dav",
                           "--pass", "pw456"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  assert wait_port(port), "rclone serve webdav did not start"
  yield port, root
  proc.kill()


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


def tree(tmp_path):
  src = tmp_path / "src" / "run1"
  (src / "sub").mkdir(parents=True)
  (src / "sub" / "a.bin").write_bytes(os.urandom(200_000))
  (src / "b.txt").write_text("hello")
  return src


def round_trip(check, tmp_path, store, eid, where_files):
  """Up into the endpoint, a run again, Verify, down again"""
  app = App()
  m = MG.Manager(app, M.Store(str(tmp_path / "transfers.json")), store)
  src = tree(tmp_path)
  up = M.Transfer(M.new_id(), "up", {"endpoint": "local", "paths": [str(src)]}, {"endpoint": eid, "path": "/in"},
                  route="rclone-here", runs_on="slurp")
  m.add(up)
  plan = m.rclone_check(up, lambda p: None)
  check(f"{eid}: the check: passed, the destination written", plan["routes"]["rclone-here"]["ok"] and "can be written" in plan["steps"]["rclone"]["text"], plan)
  up.plan = plan
  m.run_now(up)
  check(f"{eid}: up: done", until(app, m, lambda: up.state == "done"), (up.state, up.message))
  target = where_files / "in" / "run1" / "sub" / "a.bin"
  check(f"{eid}: the files there", target.exists() and target.read_bytes() == (src / "sub" / "a.bin").read_bytes(), (up.runs[-1:], sorted(str(p.relative_to(where_files)) for p in where_files.rglob("*"))))
  m.run_now(up)
  check(f"{eid}: again: nothing copied", until(app, m, lambda: up.state == "done" and len(up.runs) == 2) and up.runs[-1]["files"] == 0, up.runs[-1:])
  results = []
  m.verify(up, results.append)
  check(f"{eid}: Verify: the same", until(app, m, lambda: results) and results[0][0], results)
  down_dir = tmp_path / "down"
  down = M.Transfer(M.new_id(), "down", {"endpoint": eid, "paths": ["/in/run1"]}, {"endpoint": "local", "path": str(down_dir)},
                    route="rclone-here", runs_on="slurp")
  down.plan = {"routes": {"rclone-here": {"ok": True}}}
  m.add(down)
  m.run_now(down)
  check(f"{eid}: down: done", until(app, m, lambda: down.state == "done") and (down_dir / "run1" / "b.txt").read_text() == "hello",
        (down.state, down.message))
  return m, up


def test_smb(check, smb, tmp_path):
  port, share = smb
  store = E.EndpointStore(str(tmp_path / "endpoints.json"))
  d = E.EndpointDef("m1", "the share", "smb", {"host": "127.0.0.1", "port": str(port), "share": "DATA", "user": "user"}, ["password"])
  store.add(d)
  check("no password in the keyring: not set up", E.default_state(d)[0] == "not set up")
  S.put(S.user("m1", "password"), "pw123")
  check("with it: not connected (yet)", E.default_state(d)[0] == "not connected")
  e = RC.Endpoint(d)
  check("the password obscured for rclone", e.env[f"RCLONE_CONFIG_{e.name}_PASS"] not in ("", "pw123") and e.path("/x") == f"{e.name}:DATA/x")
  check("Test as a destination", "can be written" in RC.test(e.env, e.path("/"), write=True))
  check("where, for people", RC.address(d, "/in/a") == "//127.0.0.1/DATA/in/a" and d.where() == "//127.0.0.1/DATA as user", d.where())
  # (impacket's SMB server takes rclone's files at the share's root, not in folders under it (Samba and Windows do):
  # here files at the root, both ways)
  app = App()
  m = MG.Manager(app, M.Store(str(tmp_path / "transfers.json")), store)
  src = tree(tmp_path)
  (src / "c.txt").write_text("more")
  up = M.Transfer(M.new_id(), "up", {"endpoint": "local", "paths": [str(src / "*.txt")]}, {"endpoint": "m1", "path": "/"},
                  route="rclone-here", runs_on="slurp")
  m.add(up)
  plan = m.rclone_check(up, lambda p: None)
  check("the check: passed, the share written", plan["routes"]["rclone-here"]["ok"] and "can be written" in plan["steps"]["rclone"]["text"], plan)
  up.plan = plan
  m.run_now(up)
  check("up: done, the files there", until(app, m, lambda: up.state == "done") and (share / "b.txt").read_text() == "hello"
        and (share / "c.txt").read_text() == "more", (up.state, up.message))
  # (impacket's server keeps coarser file times (a run again would copy again) and answers rclone's reads badly: a run
  # again and a copy down are not checked against it; WebDAV below checks both)
  results = []
  m.verify(up, results.append)
  check("Verify: the same", until(app, m, lambda: results) and results[0][0], results)
  check("no marker of the write test left", not list(share.glob(".slurp-test*")))
  text = m.command_text(up)
  check("Copy as a command: the password a placeholder, obscured by the command", "pw123" not in text
        and 'rclone obscure -)' in text and "$PASSWORD" in text, text)
  S.put(S.user("m1", "password"), "wrong")
  try:
    RC.test(RC.Endpoint(d).env, RC.Endpoint(d).path("/"))
    check("a wrong password: said", False)
  except RC.RcloneError as err:
    check("a wrong password: said", str(err), str(err))


def test_webdav(check, webdav, tmp_path):
  port, root = webdav
  store = E.EndpointStore(str(tmp_path / "endpoints.json"))
  d = E.EndpointDef("w1", "dav", "webdav", {"url": f"http://127.0.0.1:{port}", "vendor": "other", "user": "dav"}, ["password"])
  store.add(d)
  S.put(S.user("w1", "password"), "pw456")
  e = RC.Endpoint(d)
  check("paths under the URL", e.path("/a/b") == f"{e.name}:a/b" and RC.address(d, "/a") == f"http://127.0.0.1:{port}/a")
  check("Test as a destination", "can be written" in RC.test(e.env, e.path("/"), write=True))
  round_trip(check, tmp_path, store, "w1", root)


def test_make_folder_webdav(check, tmp_path):
  """New folder… on WebDAV: made and written; on a server that only reads: said, not made"""
  root, port = tmp_path / "ro", free_port()
  root.mkdir()
  proc = subprocess.Popen(["rclone", "--config", "", "serve", "webdav", str(root), "--addr", f"127.0.0.1:{port}", "--read-only"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  try:
    assert wait_port(port), "rclone serve webdav did not start"
    d = E.EndpointDef("w3", "ro", "webdav", {"url": f"http://127.0.0.1:{port}"}, [])
    try:
      RC.make_folder(RC.Endpoint(d, {}), "/new")
      check("read only: said", False)
    except RC.RcloneError as err:
      check("read only: said, not made", str(err) and not (root / "new").exists(), str(err))
  finally:
    proc.kill()


def test_make_folder_webdav_written(check, webdav):
  port, root = webdav
  d = E.EndpointDef("w4", "dav", "webdav", {"url": f"http://127.0.0.1:{port}", "user": "dav"}, ["password"])
  note = RC.make_folder(RC.Endpoint(d, {"password": "pw456"}), "/new")
  check("made, written, no marker left, no note", (root / "new").is_dir() and not list((root / "new").iterdir()) and note == "", note)
