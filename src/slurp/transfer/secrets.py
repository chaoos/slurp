"""Secrets of endpoints (keys, passwords, tokens): only in the OS keyring (Secret Service, Keychain, Windows Credential
Manager), never in a file of slurp, a log, a command line or a message.

Each is under the service "slurp", as "endpoint:<id>:<name>"; the Globus login (one per user, for every collection) as
"globus:refresh_token". Without a working keyring the kinds that need secrets cannot be made (available() says why)."""

SERVICE = "slurp"
NO_KEYRING = "needs a keyring (Secret Service, Keychain or Windows Credential Manager): there is none here"

def _keyring():
  try:
    import keyring
  except ImportError:
    return None
  return keyring

def available():
  """(True, "") where a keyring works; else (False, why)"""
  keyring = _keyring()
  if keyring is None:
    return False, NO_KEYRING + " (the Python package keyring is not installed: pip install \"slurp[transfers]\")"
  try:
    backend = keyring.get_keyring()
  except Exception as e: # (a broken backend configuration)
    return False, f"{NO_KEYRING} ({e})"
  name = f"{type(backend).__module__}.{type(backend).__name__}"
  if name.startswith(("keyring.backends.fail", "keyring.backends.null")) or getattr(backend, "priority", 1) <= 0:
    return False, NO_KEYRING
  return True, ""

def user(eid, name):
  return f"endpoint:{eid}:{name}"

def get(username):
  """A secret (None: not there, or no keyring)"""
  keyring = _keyring()
  if keyring is None:
    return None
  try:
    return keyring.get_password(SERVICE, username)
  except Exception: # (a locked or broken keyring: as if it were not there; the reason is not the secret's business)
    return None

def put(username, value):
  """Store a secret (raises RuntimeError with the reason if the keyring refuses)"""
  ok, why = available()
  if not ok:
    raise RuntimeError(why)
  try:
    _keyring().set_password(SERVICE, username, value)
  except Exception as e:
    raise RuntimeError(f"the keyring refused it: {type(e).__name__}") from None

def delete(username):
  keyring = _keyring()
  if keyring is None:
    return
  try:
    keyring.delete_password(SERVICE, username)
  except Exception: # (not there)
    pass

def missing(definition):
  """The names of the secrets an endpoint needs that the keyring does not have"""
  return [n for n in definition.secrets if get(user(definition.id, n)) is None]
