"""Globus: collections as endpoints, and transfers between them that Globus runs (the data does not pass this machine).

- One Globus login per user (not per endpoint): its refresh token is only in the keyring (secrets.py, as
  "globus:refresh_token"). The login is the native-app flow: slurp shows Globus's login page (a URL), the user logs in
  there and pastes the code it shows back into slurp.
- The client id of the Globus app slurp logs in as: $SLURP_GLOBUS_CLIENT_ID, else "globus_client_id" in slurp's
  config, else the one built in (CLIENT_ID; empty: the login asks for one, registered at app.globus.org → Settings →
  Developers as a native app / thick client).
- A mapped collection of Globus Connect Server 5 needs a consent of its own (its data_access scope): the login asks for
  those of every collection slurp knows (consented(); a new one: Log in again).
- A tab can be linked to a collection (endpoints.EndpointStore.links): a path on the tab maps to a path in it.
- A transfer is a Globus task: submitted, then followed (also after slurp restarts: its task id is kept like a job's);
  Pause cancels the task (users cannot pause tasks), Continue submits a new one (its sync level skips what is there).

globus_sdk is optional (the "transfers" extra): without it available() says so. Every call here blocks (threads)."""

import fnmatch
import os
import posixpath
import re

from ..config import load_config, update_config
from ..state import log
from . import secrets as S

CLIENT_ID = "" # (slurp's own registered native app, once there is one)
TOKEN = "globus:refresh_token"
TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
NO_CLIENT_ID = ("slurp needs the client id of a Globus app to log in: register one at app.globus.org → Settings → "
                "Developers (a native app, \"thick client\"), and paste its Client UUID here")

class GlobusError(Exception):
  """What went wrong, as an endpoint state: "not set up" (no login), "expired" (log in again), "needs consent" (a
  collection: log in again with its consent), "failed" (why)"""
  def __init__(self, state, words):
    super().__init__(words)
    self.state, self.words = state, words

def sdk():
  try:
    import globus_sdk
  except ImportError:
    return None
  return globus_sdk

def client_id():
  return (os.environ.get("SLURP_GLOBUS_CLIENT_ID") or load_config().get("globus_client_id") or CLIENT_ID).strip()

def set_client_id(value):
  update_config(globus_client_id=value.strip())

def available():
  """(True, "") if Globus can be used here; else (False, why)"""
  if sdk() is None:
    return False, "needs the Python package globus-sdk (pip install \"slurp[transfers]\")"
  return S.available()

def logged_in():
  return S.get(TOKEN) is not None

def consented():
  """The collections whose consent (data_access) the login has asked for"""
  return list((load_config().get("globus") or {}).get("consented") or [])

def collection_id(text):
  """A collection's id in what the user typed or a rule pack names (an id, or a URL with one); "" if none"""
  m = UUID.search(text or "")
  return m.group(0).lower() if m else ""

def scopes(collections=()):
  """The scope of the login: Transfer, with the data_access consent of the collections that need one"""
  deps = " ".join(f"*https://auth.globus.org/scopes/{c}/data_access" for c in sorted(set(collections)))
  return TRANSFER_SCOPE + (f"[{deps}]" if deps else "")

# ---- the login
class Login:
  """The native-app login: url() is Globus's login page; finish(code) stores the refresh token in the keyring"""
  def __init__(self, collections=()):
    g = sdk()
    if g is None:
      raise GlobusError("failed", available()[1])
    if not client_id():
      raise GlobusError("not set up", NO_CLIENT_ID)
    self.collections = sorted(set(collections) | set(consented()))
    self.client = g.NativeAppAuthClient(client_id())
    self.client.oauth2_start_flow(requested_scopes=scopes(self.collections), refresh_tokens=True,
                                  prefill_named_grant="slurp")

  def url(self):
    return self.client.oauth2_get_authorize_url()

  def finish(self, code):
    try:
      tokens = self.client.oauth2_exchange_code_for_tokens(code.strip())
    except Exception as e:
      raise GlobusError("failed", f"Globus did not take the code: {why(e)}") from None
    data = tokens.by_resource_server.get("transfer.api.globus.org") or {}
    if not data.get("refresh_token"):
      raise GlobusError("failed", "Globus gave no refresh token")
    S.put(TOKEN, data["refresh_token"])
    update_config(globus={**(load_config().get("globus") or {}), "consented": self.collections})

def logout():
  """Forget the login (the token is revoked at Globus, where it can be)"""
  token = S.get(TOKEN)
  g = sdk()
  if token and g is not None and client_id():
    try:
      g.NativeAppAuthClient(client_id()).oauth2_revoke_token(token)
    except Exception as e:
      log.debug(f"globus: revoking the token: {why(e)}")
  S.delete(TOKEN)
  update_config(globus={**(load_config().get("globus") or {}), "consented": []})

def why(e):
  """A Globus error in a few words (never a token)"""
  message = getattr(e, "message", None) or str(e)
  code = getattr(e, "code", None)
  return f"{message} ({code})" if code and code not in str(message) else str(message)

def client():
  """A TransferClient of the login (GlobusError: not logged in, or no SDK)"""
  g = sdk()
  if g is None:
    raise GlobusError("failed", available()[1])
  token = S.get(TOKEN)
  if not token:
    raise GlobusError("not set up", "log in to Globus")
  if not client_id():
    raise GlobusError("not set up", NO_CLIENT_ID)
  def refreshed(response): # (a rotated refresh token: kept)
    new = (response.by_resource_server.get("transfer.api.globus.org") or {}).get("refresh_token")
    if new and new != token:
      S.put(TOKEN, new)
  authorizer = g.RefreshTokenAuthorizer(token, g.NativeAppAuthClient(client_id()), on_refresh=refreshed)
  return g.TransferClient(authorizer=authorizer)

def call(fn, *args, **kwargs):
  """A call to Globus; its errors as GlobusError (the state the endpoint is in)"""
  try:
    return fn(*args, **kwargs)
  except GlobusError:
    raise
  except Exception as e:
    code = str(getattr(e, "code", "") or "")
    text = why(e)
    info = getattr(e, "info", None)
    consent = getattr(info, "consent_required", None) if info is not None else None
    if code == "ConsentRequired" or (consent is not None and bool(consent)):
      raise GlobusError("needs consent", "the collection needs your consent: Log in to Globus again") from None
    if "invalid_grant" in text or code in ("AuthenticationFailed",) or getattr(e, "http_status", None) == 401:
      raise GlobusError("expired", "the Globus login expired: Log in again") from None
    raise GlobusError("failed", text) from None

# ---- collections
def collection(tc, cid):
  """{id, name, type, needs_consent, owner}: what Globus says of a collection"""
  doc = call(tc.get_endpoint, cid)
  get = doc.get if hasattr(doc, "get") else (lambda k, d=None: doc[k] if k in doc else d)
  kind = get("entity_type") or ""
  return {"id": cid, "name": get("display_name") or get("canonical_name") or cid, "type": kind,
          "needs_consent": kind == "GCSv5_mapped_collection", "owner": get("owner_string") or "",
          "domain": get("tlsftp_server") or get("gcs_manager_url") or ""}

def search(tc, text, limit=20):
  """Collections that match a text (an id: that one)"""
  cid = collection_id(text)
  if cid:
    return [collection(tc, cid)]
  found = call(tc.endpoint_search, text, limit=limit)
  out = []
  for doc in found:
    if doc.get("entity_type") == "GCSv5_endpoint": # (an endpoint, not a collection: nothing to copy from)
      continue
    out.append({"id": doc["id"], "name": doc.get("display_name") or doc.get("canonical_name") or doc["id"],
                "type": doc.get("entity_type") or "", "owner": doc.get("owner_string") or "",
                "needs_consent": doc.get("entity_type") == "GCSv5_mapped_collection",
                "description": (doc.get("description") or "")[:120]})
  return out

def ls(tc, cid, path):
  """A folder of a collection, as cluster.listing gives it: [{name, dir, size, mtime}]"""
  from datetime import datetime
  out = []
  for e in call(tc.operation_ls, cid, path=path or "/~/"):
    mtime = 0.0
    try:
      mtime = datetime.fromisoformat(str(e.get("last_modified") or "").replace(" ", "T")).timestamp()
    except ValueError:
      pass
    out.append({"name": e["name"], "dir": e.get("type") == "dir", "size": e.get("size") or 0, "mtime": mtime,
                "link": e.get("type") == "link" or bool(e.get("link_target"))})
  return out

def kind_of(tc, cid, path):
  """"dir", "file" or None (not there): the entry of its folder"""
  folder, name = posixpath.split(path.rstrip("/") or "/")
  if not name: # (the root)
    return "dir"
  try:
    entries = ls(tc, cid, folder or "/")
  except GlobusError as e:
    if e.state == "failed" and ("NotFound" in e.words or "No such" in e.words or "not found" in e.words.lower()):
      return None
    raise
  entry = next((e for e in entries if e["name"] == name), None)
  return None if entry is None else "dir" if entry["dir"] else "file"

def items(tc, cid, sources, dest):
  """[(source path, destination path, recursive)] of a transfer: each source (each match of a pattern) into the
  destination folder under its own name; raises GlobusError for a source that is not there"""
  out = []
  dest = dest.rstrip("/") or "/"
  for src in sources:
    folder, name = posixpath.split(src.rstrip("/"))
    if any(ch in name for ch in "*?["): # (a pattern: its matches, now)
      matches = [e for e in ls(tc, cid, folder or "/") if fnmatch.fnmatchcase(e["name"], name)]
      if not matches:
        raise GlobusError("failed", f"nothing matches {src}")
      out += [(posixpath.join(folder, e["name"]), posixpath.join(dest, e["name"]), e["dir"]) for e in matches]
      continue
    kind = kind_of(tc, cid, src)
    if kind is None:
      raise GlobusError("failed", f"{src} is not there")
    out.append((src.rstrip("/") or "/", posixpath.join(dest, name) if name else dest, kind == "dir"))
  return out

# ---- tabs linked to collections
def to_collection(link, path):
  """The path in the collection of a path on the tab (link: {tab_path, collection_path}); None outside of it"""
  tab_base = (link.get("tab_path") or "/").rstrip("/") or "/"
  coll_base = (link.get("collection_path") or "/").rstrip("/") or "/"
  path = posixpath.normpath(path)
  if tab_base != "/" and path != tab_base and not path.startswith(tab_base + "/"):
    return None
  rest = path[len(tab_base):].lstrip("/") if tab_base != "/" else path.lstrip("/")
  return posixpath.join(coll_base, rest) if rest else coll_base

def from_collection(link, path):
  """The path on the tab of a path in the collection (the other way)"""
  return to_collection({"tab_path": link.get("collection_path"), "collection_path": link.get("tab_path")}, path)

# ---- tasks
def submit(tc, t, src_cid, dst_cid, paths, dest):
  """Submit a transfer (t: its name and options; paths and dest: in the collections): the task id"""
  g = sdk()
  found = items(tc, src_cid, paths, dest)
  label = re.sub(r"[^\w ,.\-]", " ", f"slurp {t.name}")[:120]
  data = g.TransferData(src_cid, dst_cid, label=label, sync_level="checksum" if t.options.get("checksum") else "mtime",
                        verify_checksum=not t.options.get("no_verify"), preserve_timestamp=True,
                        notify_on_succeeded=False, notify_on_failed=False, notify_on_inactive=False)
  for src, dst, recursive in found:
    if recursive:
      data.add_item(src, dst, recursive=True)
    else:
      data.add_item(src, dst)
  result = call(tc.submit_transfer, data)
  return result["task_id"]

def task(tc, task_id):
  """What Globus says of a task, as a dict"""
  doc = call(tc.get_task, task_id)
  keys = ("status", "nice_status", "bytes_transferred", "bytes_checksummed", "files", "files_transferred",
          "files_skipped", "directories", "subtasks_total", "subtasks_succeeded", "subtasks_pending", "subtasks_failed",
          "subtasks_canceled", "subtasks_expired", "subtasks_skipped_errors", "effective_bytes_per_second", "faults",
          "fatal_error", "completion_time", "request_time", "canceled_by_admin", "is_paused", "nice_status_short_description")
  return {k: doc.get(k) for k in keys}

def state_of(doc):
  """(transfer state, words) of a task: queued, running, waiting (inactive: Globus waits, e.g. for a login or a
  consent), done, done-errors (files skipped), failed (why), canceled"""
  status = doc.get("status")
  nice = doc.get("nice_status_short_description") or doc.get("nice_status") or ""
  if status == "SUCCEEDED":
    skipped = doc.get("subtasks_skipped_errors") or 0
    return ("done-errors", f"{skipped} file{'s' if skipped != 1 else ''} skipped (errors)") if skipped else ("done", "")
  if status == "FAILED":
    fatal = doc.get("fatal_error") or {}
    code = (fatal.get("code") if isinstance(fatal, dict) else "") or ""
    if code == "CANCELED" or doc.get("subtasks_canceled"):
      return "canceled", (fatal.get("description") if isinstance(fatal, dict) else "") or "canceled"
    return "failed", ((fatal.get("description") if isinstance(fatal, dict) else str(fatal)) or nice or "the task failed")
  if status == "INACTIVE":
    return "waiting", f"Globus waits: {nice or 'inactive'} (a login or a consent may be missing)"
  if doc.get("is_paused"):
    return "waiting", "paused by an administrator of a collection"
  if status == "ACTIVE" and not doc.get("bytes_transferred") and not doc.get("files_transferred") and nice.lower() in ("queued", ""):
    return "queued", "queued at Globus"
  if status == "ACTIVE":
    return "running", (nice if nice and nice.lower() not in ("ok", "active", "queued") else "")
  return "running", nice

def progress_of(doc):
  """The progress of a task, as the UI shows a run's"""
  total, done = doc.get("subtasks_total") or 0, sum(doc.get(k) or 0 for k in ("subtasks_succeeded", "subtasks_skipped_errors"))
  return {"bytes": doc.get("bytes_transferred") or 0, "rate": float(doc.get("effective_bytes_per_second") or 0),
          "percent": 100.0 * done / total if total else None, "files": doc.get("files_transferred") or 0,
          "files_total": doc.get("files"), "skipped": doc.get("files_skipped") or 0, "faults": doc.get("faults") or 0}

def cancel(tc, task_id):
  result = call(tc.cancel_task, task_id)
  return result.get("code") if hasattr(result, "get") else ""

def task_errors(tc, task_id, limit=50):
  """What went wrong with single files (the skipped ones)"""
  try:
    found = call(tc.task_skipped_errors, task_id)
  except GlobusError:
    return []
  out = []
  for e in found:
    out.append(f"{e.get('source_path') or e.get('destination_path') or '?'}: {e.get('error_code') or ''} {e.get('error_details') or ''}".strip())
    if len(out) >= limit:
      break
  return out
