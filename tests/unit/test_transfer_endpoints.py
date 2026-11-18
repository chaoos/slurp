"""Transfers: endpoints: this machine (built in), the remote tabs (with their status from their login), the defined
ones (stored without secrets), deleting one that transfers use (refused, named), the ends of older transfers"""
import types

from slurp.transfer import endpoints as E, model as M


def tab(name, destination=None, state=None, ready=False, latency=None, wanted=True):
  connection = None if state is None else types.SimpleNamespace(state=state, ready=ready, latency=latency, wanted=wanted)
  return types.SimpleNamespace(name=name, destination=destination, connection=connection, ssh=None, pre_login="")


class App:
  def __init__(self, clusters):
    self.clusters = clusters
  def login_visible(self, cluster):
    return False


def test_listing(check, tmp_path):
  store = E.EndpointStore(str(tmp_path / "e.json"))
  store.add(E.EndpointDef("b1", "my-bucket", "s3", {"region": "eu", "bucket": "b", "prefix": "runs/"}, ["access_key", "secret_key"]))
  app = App([tab("Local"), tab("alps", "daint", "ready", ready=True, latency=0.038), tab("jedi", "jedi", "new"),
             tab("eiger", "eiger", "connecting")])
  found = E.listing(app, store)
  check("this machine first, the remote tabs, the defined ones",
        [(e.id, e.kind) for e in found] == [("local", "local"), ("tab:daint", "tab"), ("tab:jedi", "tab"), ("tab:eiger", "tab"), ("b1", "s3")],
        [(e.id, e.kind) for e in found])
  by = {e.id: e for e in found}
  check("this machine: named as the local tab, always connected", by["local"].name == "Local" and by["local"].connected)
  check("a tab logged in: connected, its round trip", by["tab:daint"].connected and by["tab:daint"].words == "38 ms", by["tab:daint"])
  check("a tab not logged in: needs login", by["tab:jedi"].state == "not connected" and by["tab:jedi"].words == "needs login")
  check("a tab logging in: connecting", by["tab:eiger"].state == "connecting")
  check("a defined one: where it points; without its keys in the keyring: not set up", by["b1"].where == "eu · b/runs"
        and by["b1"].state == "not set up" and "keyring" in by["b1"].words, (by["b1"].where, by["b1"].state, by["b1"].words))
  import json
  saved = json.load(open(store.path))["endpoints"][0]
  check("stored with the names of its secrets, not their values", saved["secrets"] == ["access_key", "secret_key"]
        and not set(saved["settings"]) & set(E.SECRETS["s3"]), saved)


def test_resolve_and_refs(check, tmp_path):
  store = E.EndpointStore(str(tmp_path / "e.json"))
  alps = tab("alps", "daint", "ready", ready=True)
  app = App([alps])
  check("this machine without a local tab: its stand-in", E.resolve(app, store, {"endpoint": "local"}).cluster.destination is None)
  ref = E.ref_of(E.resolve(app, store, {"endpoint": "tab:daint"}), paths=["/x"])
  check("a tab's end keeps its tab reference and its name", ref["tab"]["destination"] == "daint" and ref["name"] == "alps" and ref["paths"] == ["/x"], ref)
  app.clusters = []
  gone = E.resolve(app, store, ref)
  check("its tab closed: tab not open, its name kept", gone.state == "tab not open" and gone.name == "alps", gone)
  check("an older transfer's end: its endpoint from its tab", E.ref_id({"tab": {"destination": "daint"}}) == "tab:daint"
        and E.ref_id({"tab": {"destination": None}}) == "local")
  check("a deleted endpoint: said", E.resolve(app, store, {"endpoint": "nope"}).state == "failed")


def test_delete_refused(check, tmp_path):
  store = E.EndpointStore(str(tmp_path / "e.json"))
  store.add(E.EndpointDef("b1", "my-bucket", "s3"))
  try:
    store.add(E.EndpointDef("b2", "my-bucket", "s3"))
    check("two endpoints of one name: refused", False)
  except ValueError:
    check("two endpoints of one name: refused", True)
  users = [M.Transfer(M.new_id(), f"t{i}", {"endpoint": "local", "paths": []}, {"endpoint": "b1", "path": "/"}) for i in range(7)]
  try:
    store.remove("b1", users)
    check("used by transfers: refused", False)
  except E.InUse as e:
    check("used by transfers: refused, named (five, and how many more)", "t0, t1, t2, t3, t4 and 2 more" in str(e), str(e))
  check("still there", store.get("b1") is not None and E.EndpointStore(store.path).get("b1") is not None)
  store.remove("b1", [])
  check("unused: deleted", E.EndpointStore(store.path).get("b1") is None)
