"""Transfers through Globus, against a fake Globus (no network): secrets only in the keyring (one in memory here),
the scope of the login, paths of linked tabs, the items of a transfer (patterns, folders), the task states, and the
manager: submit, follow, done, Pause (cancel) and Continue (a new task), followed again after a restart, the check,
Copy as a command"""
import queue
import time
import types

import pytest

from slurp.transfer import endpoints as E, globus as G, manager as MG, model as M, secrets as S

pytest.importorskip("globus_sdk")

SRC, DST = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


class FakeGlobus:
  """A TransferClient: two collections, a folder tree each, tasks that move along as they are looked at"""
  def __init__(self):
    self.trees = {SRC: {"/data": [("run1", "dir"), ("a.h5", "file"), ("b.h5", "file"), ("notes.txt", "file")], "/": [("data", "dir")]},
                  DST: {"/": [("in", "dir")], "/in": []}}
    self.tasks, self.submitted, self.canceled = {}, [], []

  def get_endpoint(self, cid):
    if cid not in self.trees:
      raise G.GlobusError("failed", "no such collection")
    return {"display_name": "source coll" if cid == SRC else "dest coll", "entity_type": "GCSv5_mapped_collection"}

  def operation_ls(self, cid, path="/"):
    path = path.rstrip("/") or "/"
    if path not in self.trees[cid]:
      raise G.GlobusError("failed", f"NotFound: {path}")
    return [{"name": n, "type": k, "size": 10, "last_modified": "2026-11-18 10:00:00+00:00"} for n, k in self.trees[cid][path]]

  def endpoint_search(self, text, limit=20):
    return [{"id": SRC, "display_name": "source coll", "entity_type": "GCSv5_mapped_collection"},
            {"id": "x", "display_name": "an endpoint", "entity_type": "GCSv5_endpoint"}]

  def submit_transfer(self, data):
    tid = f"task-{len(self.submitted) + 1}"
    self.submitted.append(dict(data))
    self.tasks[tid] = {"status": "ACTIVE", "nice_status": "Queued", "bytes_transferred": 0, "files": 3, "files_transferred": 0,
                       "subtasks_total": 3, "subtasks_succeeded": 0, "effective_bytes_per_second": 0}
    return {"task_id": tid}

  def get_task(self, tid):
    return dict(self.tasks[tid])

  def cancel_task(self, tid):
    self.canceled.append(tid)
    self.tasks[tid].update(status="FAILED", fatal_error={"code": "CANCELED", "description": "canceled"})
    return {"code": "Canceled"}

  def task_skipped_errors(self, tid):
    return [{"source_path": "/data/b.h5", "error_code": "PERMISSION_DENIED", "error_details": "no"}]


def test_secrets_only_in_the_keyring(check):
  check("a keyring here (the tests': in memory)", S.available()[0], S.available())
  S.put(S.user("e1", "password"), "hunter2")
  check("stored and read back", S.get(S.user("e1", "password")) == "hunter2")
  d = E.EndpointDef("e1", "share", "smb", {}, ["password"])
  check("nothing missing", S.missing(d) == [])
  S.delete(S.user("e1", "password"))
  check("deleted: missing", S.missing(d) == ["password"])
  import keyring
  from keyring.backends import fail
  keyring.set_keyring(fail.Keyring())
  ok, why = S.available()
  check("no keyring: said so", not ok and "keyring" in why, why)
  try:
    S.put("x", "y")
    check("no keyring: refused", False)
  except RuntimeError:
    check("no keyring: refused", True)


def test_scopes_and_paths(check):
  check("transfer only", G.scopes() == G.TRANSFER_SCOPE)
  s = G.scopes([DST, SRC])
  check("with the consents", s.startswith(G.TRANSFER_SCOPE + "[*https://auth.globus.org/scopes/1111") and f"*https://auth.globus.org/scopes/{DST}/data_access]" in s, s)
  check("an id in a URL", G.collection_id(f"https://app.globus.org/file-manager?origin_id={SRC.upper()}") == SRC)
  check("no id", G.collection_id("CSCS collection") == "")
  link = {"tab_path": "/capstor/scratch/me", "collection_path": "/scratch/me"}
  check("a path under the linked folder", G.to_collection(link, "/capstor/scratch/me/run1/out") == "/scratch/me/run1/out")
  check("the linked folder itself", G.to_collection(link, "/capstor/scratch/me/") == "/scratch/me")
  check("outside it: None", G.to_collection(link, "/home/me/x") is None)
  check("and back", G.from_collection(link, "/scratch/me/run1") == "/capstor/scratch/me/run1")
  check("the whole tab", G.to_collection({"tab_path": "/", "collection_path": "/"}, "/a/b") == "/a/b")


def test_items_and_states(check):
  tc = FakeGlobus()
  found = G.items(tc, SRC, ["/data/run1", "/data/*.h5", "/data/notes.txt"], "/in/")
  check("a folder recursive, each match, a file", found == [("/data/run1", "/in/run1", True), ("/data/a.h5", "/in/a.h5", False),
                                                           ("/data/b.h5", "/in/b.h5", False), ("/data/notes.txt", "/in/notes.txt", False)], found)
  for paths, words in ((["/data/*.nc"], "nothing matches"), (["/data/gone"], "not there"), (["/nowhere/x"], "not there")):
    try:
      G.items(tc, SRC, paths, "/in")
      check(f"refused: {paths}", False)
    except G.GlobusError as e:
      check(f"refused: {paths}", words in e.words, e.words)
  check("search: collections only", [c["id"] for c in G.search(tc, "coll")] == [SRC])
  check("a mapped collection needs a consent", G.collection(tc, SRC)["needs_consent"])
  states = [({"status": "ACTIVE", "nice_status": "Queued"}, "queued"), ({"status": "ACTIVE", "nice_status": "OK", "bytes_transferred": 5}, "running"),
            ({"status": "INACTIVE", "nice_status": "ConsentRequired"}, "waiting"), ({"status": "SUCCEEDED"}, "done"),
            ({"status": "SUCCEEDED", "subtasks_skipped_errors": 2}, "done-errors"),
            ({"status": "FAILED", "fatal_error": {"code": "CANCELED", "description": "canceled"}}, "canceled"),
            ({"status": "FAILED", "fatal_error": {"code": "PERMISSION_DENIED", "description": "no access"}}, "failed")]
  for doc, state in states:
    check(f"{doc['status']} {doc.get('nice_status', '')}: {state}", G.state_of(doc)[0] == state, G.state_of(doc))
  p = G.progress_of({"bytes_transferred": 100, "subtasks_total": 4, "subtasks_succeeded": 1, "files_transferred": 1, "effective_bytes_per_second": 50})
  check("progress", p["bytes"] == 100 and p["percent"] == 25.0 and p["rate"] == 50.0 and p["files"] == 1, p)


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


def until(app, manager, cond, seconds=10):
  end = time.time() + seconds
  while time.time() < end:
    app.pump()
    manager.tick()
    if cond():
      return True
    time.sleep(0.05)
  app.pump()
  return cond()


def setup(tmp_path, monkeypatch, fake):
  monkeypatch.setattr(G, "client", lambda: fake)
  store = E.EndpointStore(str(tmp_path / "endpoints.json"))
  store.add(E.EndpointDef("g1", "CSCS", "globus", {"collection": SRC, "collection_name": "source coll"}))
  store.links["tab:jedi"] = {"collection": DST, "collection_name": "dest coll", "tab_path": "/p/scratch", "collection_path": "/"}
  store.save()
  return store


def test_manager_runs_a_task(check, tmp_path, monkeypatch):
  fake = FakeGlobus()
  store = setup(tmp_path, monkeypatch, fake)
  app = App()
  m = MG.Manager(app, M.Store(str(tmp_path / "transfers.json")), store)
  t = M.Transfer(M.new_id(), "to jedi", {"endpoint": "g1", "name": "CSCS", "paths": ["/data/run1", "/data/*.h5"]},
                 {"endpoint": "tab:jedi", "name": "jedi", "tab": {"destination": "jedi", "name": "jedi"}, "path": "/p/scratch/in"},
                 route="globus", runs_on="globus")
  m.add(t)
  check("a Globus transfer needs no tab", m.not_ready(t) == [])
  plan = m.globus_check(t, lambda p: None)
  check("the check: passed, its items", plan["routes"]["globus"]["ok"] and len(plan["globus_items"]) == 3, plan)
  t.plan = plan
  text = m.command_text(t)
  check("the command: globus transfer --batch, a folder recursive", f"--batch - {SRC} {DST}" in text and "--recursive /data/run1 /in/run1" in text
        and "--verify-checksum" in text and "--sync-level mtime" in text, text)
  m.run_now(t)
  check("submitted", until(app, m, lambda: t.run.get("task") == "task-1") and t.state == "queued", (t.state, t.message))
  sub = fake.submitted[0]
  check("its items, mapped into the linked collection", [(i["source_path"], i["destination_path"], i.get("recursive") is True) for i in sub["DATA"]]
        == [("/data/run1", "/in/run1", True), ("/data/a.h5", "/in/a.h5", False), ("/data/b.h5", "/in/b.h5", False)], sub["DATA"])
  check("sync level mtime, checksums verified", sub["sync_level"] in ("mtime", 2) and sub["verify_checksum"] is True, sub)
  fake.tasks["task-1"].update(nice_status="OK", bytes_transferred=500, files_transferred=1, subtasks_succeeded=1, effective_bytes_per_second=100)
  m.followed[t.id] = 0
  check("running, with its progress", until(app, m, lambda: t.state == "running") and t.progress.get("bytes") == 500, (t.state, t.progress))

  # a restart: the task is followed again
  m2 = MG.Manager(app, M.Store(str(tmp_path / "transfers.json")), store)
  t2 = m2.get(t.id)
  check("after a restart: still under way, followed", t2.state in M.ACTIVE and t2.run.get("task") == "task-1" and t2.id in m2.followed, (t2.state, t2.run))
  fake.tasks["task-1"].update(status="SUCCEEDED", subtasks_skipped_errors=1)
  check("done (with an error)", until(app, m2, lambda: t2.state == "done-errors"), (t2.state, t2.message))
  check("its errors", t2.errors and "b.h5" in t2.errors[0], t2.errors)
  check("a run in the history", t2.runs and t2.runs[-1]["result"] == "done-errors")


def test_pause_and_continue(check, tmp_path, monkeypatch):
  fake = FakeGlobus()
  store = setup(tmp_path, monkeypatch, fake)
  app = App()
  m = MG.Manager(app, M.Store(str(tmp_path / "transfers.json")), store)
  t = M.Transfer(M.new_id(), "pause", {"endpoint": "g1", "name": "CSCS", "paths": ["/data/notes.txt"]},
                 {"endpoint": "tab:jedi", "name": "jedi", "tab": {"destination": "jedi"}, "path": "/p/scratch/in"},
                 route="globus", runs_on="globus", options={"checksum": True, "no_verify": True})
  t.plan = {"routes": {"globus": {"ok": True}}}
  m.add(t)
  m.run_now(t)
  until(app, m, lambda: t.run.get("task"))
  check("checksums: sync level checksum, no verify", fake.submitted[0]["sync_level"] in ("checksum", 3) and fake.submitted[0]["verify_checksum"] is False)
  m.pause(t)
  check("pause: the task is canceled", until(app, m, lambda: t.state == "paused") and fake.canceled == ["task-1"], (t.state, fake.canceled))
  check("paused mid-run: Continue", m.paused_mid_run(t))
  m.resume(t)
  check("Continue: a new task", until(app, m, lambda: t.run.get("task") == "task-2"), t.run)
  fake.tasks["task-2"]["status"] = "FAILED"
  fake.tasks["task-2"]["fatal_error"] = {"code": "PERMISSION_DENIED", "description": "permission denied on /in"}
  m.followed[t.id] = 0
  check("failed, and why", until(app, m, lambda: t.state == "failed") and "permission denied" in t.message, t.message)


def test_not_logged_in(check, tmp_path):
  store = E.EndpointStore(str(tmp_path / "endpoints.json"))
  d = E.EndpointDef("g1", "CSCS", "globus", {"collection": SRC})
  store.add(d)
  state, words = E.default_state(d)
  check("not logged in: not set up", state == "not set up", (state, words))
  try:
    G.client()
    check("no client without a login", False)
  except G.GlobusError as e:
    check("no client without a login", e.state == "not set up", e.words)
  check("globus only (a Globus endpoint)", E.globus_only(store, {"endpoint": "g1"}, {"endpoint": "local"}))
  check("not possible: this machine has no collection", not E.globus_possible(store, {"endpoint": "g1"}, {"endpoint": "local"}))
