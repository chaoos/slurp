"""Endpoints: what the user picks as the source or the destination of a transfer.

- This machine: built in, once (id "local"), named as the local tab ("Local"), always connected.
- Cluster tabs: every open tab with an SSH destination is an endpoint (id "tab:<destination>"); its status follows the
  tab's login. They are not stored: a transfer keeps its tab reference (model.tab_ref) to find it, or to open it again.
- Defined endpoints (S3, Globus, SMB, WebDAV): created in the Transfers tab, stored in $XDG_STATE_HOME/slurp/endpoints.json
  without their secrets; the secrets are only in the OS keyring (secrets.py).

Their status is of the moment (not stored): not set up (a secret is missing), not connected, connecting, needs you (a
tab's login waits for input), connected, failed (why), expired (a token), tab not open."""

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field

from ..state import log
from . import model as M

LOCAL = "local"
KINDS = {"local": "this machine", "tab": "cluster tab", "s3": "S3", "globus": "Globus", "smb": "SMB", "webdav": "WebDAV"}
SECRETS = {"s3": ["access_key", "secret_key", "session_token"], "globus": ["refresh_token"], "smb": ["password"],
           "webdav": ["password"]}
OPTIONAL_SECRETS = {"session_token"} # (an S3 session token: only for temporary keys)
DEFINED = ("s3", "globus", "smb", "webdav") # (the kinds the user defines)

def tab_id(cluster):
  """The endpoint id of a tab (this machine: local)"""
  return f"tab:{cluster.destination}" if cluster.destination else LOCAL

def ref_id(ref):
  """The endpoint id of a transfer's end ({"endpoint", "tab", …}); one of an older transfer: from its tab"""
  if ref.get("endpoint"):
    return ref["endpoint"]
  tab = ref.get("tab") or {}
  return f"tab:{tab['destination']}" if tab.get("destination") else LOCAL

def is_tab(eid):
  return eid == LOCAL or eid.startswith("tab:")

@dataclass
class EndpointDef:
  """An endpoint the user defined (not a tab, not this machine)"""
  id: str
  name: str
  kind: str
  settings: dict = field(default_factory=dict) # per kind; never a secret
  secrets: list = field(default_factory=list)  # the names of its secrets in the keyring
  created: float = field(default_factory=time.time)

  def where(self):
    """Where it points, in a few words"""
    s = self.settings
    if self.kind == "s3":
      return " · ".join(x for x in (s.get("region", ""), (s.get("bucket", "") + ("/" + s["prefix"].strip("/") if s.get("prefix") else ""))) if x)
    if self.kind == "globus":
      return s.get("collection_name") or s.get("collection", "")
    if self.kind == "smb":
      return f"//{s.get('server', '')}/{s.get('share', '')}"
    if self.kind == "webdav":
      return s.get("url", "")
    return ""

def store_path():
  return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "slurp", "endpoints.json")

class InUse(Exception):
  """An endpoint that transfers use cannot be deleted"""
  def __init__(self, names):
    self.names = names
    shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    super().__init__(f"transfers use it: {shown}")

class EndpointStore:
  """The defined endpoints, and what is linked to tabs (a Globus collection), written atomically"""
  def __init__(self, path=None):
    self.path = path or store_path()
    self.lock = threading.Lock()
    self.defs = []
    self.links = {} # tab id -> {"collection", "collection_name", "tab_path", "collection_path"}
    self.load()

  def load(self):
    try:
      with open(self.path) as f:
        data = json.load(f)
    except FileNotFoundError:
      data = {}
    except (OSError, ValueError) as e:
      log.warning(f"cannot read {self.path}: {e}")
      data = {}
    self.defs = []
    for item in data.get("endpoints", []) if isinstance(data, dict) else []:
      try:
        self.defs.append(EndpointDef(**{k: v for k, v in item.items() if k in EndpointDef.__dataclass_fields__}))
      except TypeError as e:
        log.warning(f"an endpoint in {self.path} cannot be read: {e}")
    self.links = dict(data.get("links") or {}) if isinstance(data, dict) else {}

  def save(self):
    with self.lock:
      data = {"endpoints": [asdict(d) for d in self.defs], "links": self.links}
      try:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".endpoints.")
        with os.fdopen(fd, "w") as f:
          json.dump(data, f, indent=1)
        os.replace(tmp, self.path)
      except OSError as e:
        log.warning(f"cannot write {self.path}: {e}")

  def get(self, eid):
    return next((d for d in self.defs if d.id == eid), None)

  def add(self, definition):
    if any(d.name == definition.name and d.id != definition.id for d in self.defs):
      raise ValueError(f"an endpoint is called {definition.name} already")
    old = self.get(definition.id)
    if old is not None:
      self.defs[self.defs.index(old)] = definition
    else:
      self.defs.append(definition)
    self.save()

  def remove(self, eid, transfers=()):
    """Delete an endpoint (refused while transfers use it: InUse names them)"""
    users = [t.name for t in transfers if eid in (ref_id(t.source), ref_id(t.dest))]
    if users:
      raise InUse(users)
    d = self.get(eid)
    if d is not None:
      self.defs.remove(d)
      self.save()
    return d

# ---- the endpoints of the moment: tabs, this machine, the defined ones, with their status
@dataclass
class Endpoint:
  id: str
  name: str
  kind: str
  where: str = ""
  state: str = "not connected"   # not set up, not connected, connecting, needs you, connected, failed, expired, tab not open
  words: str = ""                # the state in a few words (why it failed, its round trip, …)
  cluster: object = None         # a tab (or this machine's stand-in)
  definition: object = None      # an EndpointDef

  @property
  def connected(self):
    return self.state == "connected"

  @property
  def is_tab(self):
    return self.kind in ("tab", "local")

def this_machine(clusters):
  """This machine: the local tab when one is open, else a stand-in for it (always there)"""
  from ..state import ClusterSession
  return next((c for c in clusters if not c.destination), None) or ClusterSession("Local", None)

def tab_state(cluster, app=None):
  """(state, words) of a tab, as its overview and its dot say"""
  if cluster is None:
    return "tab not open", "its tab is not open"
  if not cluster.destination:
    return "connected", "this machine"
  c = cluster.connection
  if c is not None and c.ready:
    return "connected", f"{c.latency * 1000:.0f} ms" if c.latency is not None else "connected"
  if c is None or c.state in ("new", "closed") or (c.state == "broken" and not c.wanted):
    return "not connected", "needs login"
  if c.state == "connecting" and app is not None and app.login_visible(cluster):
    return "needs you", "its login waits for you (in its tab)"
  return "connecting", "connecting" if c.state == "connecting" else "reconnecting"

def listing(app, store, statuses=None):
  """All endpoints for the list and the wizard: this machine, the remote tabs, then the defined ones"""
  statuses = statuses or {}
  clusters = app.clusters
  out = [Endpoint(LOCAL, this_machine(clusters).name, "local", "this machine", "connected", "always", cluster=this_machine(clusters))]
  for c in clusters:
    if c.destination:
      state, words = tab_state(c, app)
      out.append(Endpoint(tab_id(c), c.name, "tab", c.destination, state, words, cluster=c))
  for d in sorted(store.defs, key=lambda d: d.name.lower()):
    state, words = statuses.get(d.id, ("not connected", ""))
    out.append(Endpoint(d.id, d.name, d.kind, d.where(), state, words, definition=d))
  return out

def resolve(app, store, ref, statuses=None):
  """The endpoint of a transfer's end; a tab that is not open: one in the state "tab not open" (with its name)"""
  eid = ref_id(ref)
  if eid == LOCAL:
    m = this_machine(app.clusters)
    return Endpoint(LOCAL, m.name, "local", "this machine", "connected", "always", cluster=m)
  if eid.startswith("tab:"):
    cluster = M.find_tab(app.clusters, ref.get("tab") or {"destination": eid[4:]})
    state, words = tab_state(cluster, app)
    name = cluster.name if cluster is not None else M.tab_name(ref.get("tab") or {"destination": eid[4:]})
    return Endpoint(eid, name, "tab", eid[4:], state, words, cluster=cluster)
  d = store.get(eid)
  if d is None:
    return Endpoint(eid, "(deleted)", "?", "", "failed", "this endpoint was deleted")
  state, words = (statuses or {}).get(eid, ("not connected", ""))
  return Endpoint(d.id, d.name, d.kind, d.where(), state, words, definition=d)

def ref_of(endpoint, **more):
  """A transfer's end for an endpoint (a tab: with its tab reference, to find it again)"""
  ref = {"endpoint": endpoint.id, "name": endpoint.name, **more}
  if endpoint.kind == "tab" and endpoint.cluster is not None:
    ref["tab"] = M.tab_ref(endpoint.cluster)
  elif endpoint.kind == "local":
    ref["tab"] = {"destination": None, "name": endpoint.name}
  return ref
