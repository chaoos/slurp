"""Transfers: how one names its tabs, the store (written and read again), a run in slurp that was cut off by the end
of slurp (paused), and the history of runs"""
import types

from slurp.transfer import model as M


def tab(name, destination=None, **kw):
  return types.SimpleNamespace(name=name, destination=destination, ssh=kw.get("ssh"), pre_login=kw.get("pre_login", ""))


def test_tabs(check):
  alps, jedi, local = tab("alps", "daint"), tab("jedi", "jedi", ssh={"host": "jedi.fz-juelich.de"}), tab("Local")
  ref = M.tab_ref(jedi)
  check("a reference: destination, name, the SSH settings", ref == {"destination": "jedi", "name": "jedi", "ssh": {"host": "jedi.fz-juelich.de"}}, ref)
  check("found among the open tabs", M.find_tab([local, alps, jedi], ref) is jedi)
  check("this machine", M.find_tab([alps, local], M.tab_ref(local)) is local)
  check("not open: None", M.find_tab([local, alps], ref) is None)
  twin = tab("alps 2", "daint")
  check("two tabs of one destination: the one of that name", M.find_tab([alps, twin], M.tab_ref(twin)) is twin)
  check("a name for it", M.tab_name(ref) == "jedi" and M.tab_name({"destination": None}) == "Local")
  check("a default name", M.default_name(["/s/run42/"], M.tab_ref(alps), ref) == "run42 alps → jedi"
        and M.default_name(["/s/a", "/s/b"], M.tab_ref(alps), ref) == "a +1 alps → jedi")


def test_store(check, tmp_path):
  store = M.Store(str(tmp_path / "transfers.json"))
  check("empty at first", store.transfers == [])
  t = M.Transfer(M.new_id(), "run42", {"tab": {"destination": "daint", "name": "alps"}, "paths": ["/s/run42"]},
                 {"tab": {"destination": "jedi", "name": "jedi"}, "path": "/p/scratch"}, route="relay")
  t.state, t.run = "running", {"kind": "slurp", "started": 1.0}
  d = M.Transfer(M.new_id(), "job", t.source, t.dest, route="direct-push", runs_on="job")
  d.state, d.run = "queued", {"kind": "job", "job": "123", "folder": "/h/.cache/slurp/transfers/x"}
  cut = M.Transfer(M.new_id(), "cut", t.source, t.dest, route="direct-push", runs_on="detached")
  cut.state, cut.run = "starting", {"kind": "detached", "started": 1.0}
  store.add(t)
  store.add(d)
  store.add(cut)
  again = M.Store(store.path)
  check("all read again", [x.name for x in again.transfers] == ["run42", "job", "cut"])
  r = again.get(t.id)
  check("a run in slurp that slurp's end cut off: paused (Continue goes on)", r.state == "paused" and r.run == {} and "slurp ended" in r.message, r)
  check("a transfer job goes on without slurp: as it was", again.get(d.id).state == "queued" and again.get(d.id).run["job"] == "123")
  check("one that was still starting: paused, in the history", again.get(cut.id).state == "paused" and again.get(cut.id).runs[-1]["result"] == "paused")
  again.remove(again.get(d.id))
  again.remove(again.get(cut.id))
  check("removed", [x.name for x in M.Store(store.path).transfers] == ["run42"])
  (tmp_path / "bad.json").write_text("{not json")
  check("a store that cannot be read: empty (a warning)", M.Store(str(tmp_path / "bad.json")).transfers == [])


def test_history(check):
  t = M.Transfer("x", "n", {}, {})
  t.run, t.progress = {"started": 5.0, "job": "77"}, {"bytes": 10, "files": 2}
  entry = M.record_run(t, "done")
  check("a run in the history", t.runs == [entry] and entry["result"] == "done" and entry["job"] == "77" and entry["bytes"] == 10)
  check("the run under way is gone", t.run == {})
  for _ in range(M.HISTORY + 5):
    M.record_run(t, "failed")
  check("the history is limited", len(t.runs) == M.HISTORY)
