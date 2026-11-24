"""What an endpoint's server is (for its window): its software, its version, its protocol, its space, as far as it says
so without changing anything. WebDAV: its HTTP headers (Server, DAV) and Nextcloud's or ownCloud's status.php; S3: the
headers of an unsigned request (Server, the bucket's region); SMB: an SMB2 negotiate (the dialect, signing,
encryption, the logins it offers, a hint of Samba); rclone about: its space (where the server tells); Globus: what
Globus knows of the collection. Each: [(label, value)]; a probe that fails: a row that says so (never raises)"""
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import rclone as RC

TIMEOUT = 8
HEADERS = ("Server", "DAV", "X-Powered-By", "MS-Author-Via", "X-Sabre-Version", "x-amz-bucket-region", "x-amz-id-2")

def human(n):
  for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
    if abs(n) < 1024 or unit == "PiB":
      return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
    n /= 1024

def http_headers(url, method="OPTIONS", user="", password=""):
  """(status, {header: value}) of a request (an answer that refuses, 401 or 403, has its headers too)"""
  request = urllib.request.Request(url, method=method, headers={"User-Agent": "slurp"})
  if user:
    import base64
    request.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
  try:
    with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
      return answer.status, dict(answer.headers.items())
  except urllib.error.HTTPError as e:
    return e.code, dict(e.headers.items())

def header_rows(headers):
  lower = {k.lower(): v for k, v in headers.items()}
  return [(name if name != "DAV" else "WebDAV classes", lower[name.lower()]) for name in HEADERS if lower.get(name.lower())]

def webdav(settings, keys):
  url = settings.get("url", "")
  rows = []
  try:
    status, headers = http_headers(url, "OPTIONS", settings.get("user", ""), keys.get("password", ""))
    rows += header_rows(headers) or [("Server", "says nothing of itself (no Server header)")]
    rows.append(("Answer", f"HTTP {status} to OPTIONS"))
  except Exception as e:
    rows.append(("Server", f"no answer: {e}"))
    return rows
  if "/remote.php" in url or settings.get("vendor") in ("nextcloud", "owncloud"): # (Nextcloud, ownCloud: their status)
    base = url.split("/remote.php")[0].rstrip("/")
    try:
      with urllib.request.urlopen(urllib.request.Request(base + "/status.php", headers={"User-Agent": "slurp"}), timeout=TIMEOUT) as answer:
        s = json.loads(answer.read().decode() or "{}")
      rows.append(("Software", f"{s.get('productname') or 'Nextcloud/ownCloud'} {s.get('versionstring') or s.get('version') or ''}".strip()
                   + (" (maintenance)" if s.get("maintenance") else "")))
    except Exception:
      pass
  return rows

def s3(settings):
  endpoint = (settings.get("endpoint") or "https://s3.amazonaws.com").rstrip("/")
  bucket = settings.get("bucket", "")
  rows = [("Provider", settings.get("provider") or ("AWS" if not settings.get("endpoint") else "Other"))]
  try:
    status, headers = http_headers(f"{endpoint}/{urllib.parse.quote(bucket)}", "HEAD")
    found = header_rows(headers)
    software = next((v for k, v in found if k == "Server"), "")
    rows += [(k, v) for k, v in found if k not in ("x-amz-id-2",)] or [("Server", "says nothing of itself")]
    hint = ("AWS S3" if software == "AmazonS3" else "MinIO" if "minio" in software.lower() or any(k.lower().startswith("x-minio") for k in headers)
            else "Ceph (RADOS Gateway)" if "ceph" in software.lower() or "rgw" in software.lower() else "")
    if hint:
      rows.append(("Software", hint))
    rows.append(("Answer", f"HTTP {status} to an unsigned HEAD of the bucket"))
  except Exception as e:
    rows.append(("Server", f"no answer: {e}"))
  return rows

# ---- SMB2 negotiate (the first message of every SMB2 connection: no login)
DIALECTS = {0x0202: "SMB 2.0.2", 0x0210: "SMB 2.1", 0x0300: "SMB 3.0", 0x0302: "SMB 3.0.2", 0x0311: "SMB 3.1.1"}
CAPS = {0x01: "DFS", 0x02: "leasing", 0x04: "large MTU", 0x08: "multichannel", 0x10: "persistent handles",
        0x20: "directory leasing", 0x40: "encryption"}
MECHS = {bytes.fromhex("2a864886f712010202"): "Kerberos", bytes.fromhex("2a864882f712010202"): "Kerberos (MS)",
         bytes.fromhex("2b06010401823702020a"): "NTLM", bytes.fromhex("2b06010401823702021e"): "NEGOEX"}

def smb_negotiate(host, port=445):
  """The SMB2 negotiate answer of a server: {dialect, signing, caps, mechs, samba, time, max_read, max_write}"""
  dialects = [0x0202, 0x0210, 0x0300, 0x0302]
  header = struct.pack("<4sHHIHHIIQIIQ16s", b"\xfeSMB", 64, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, b"\0" * 16)
  body = struct.pack("<HHHHI16sQ", 36, len(dialects), 1, 0, 0x7F, os.urandom(16), 0) + b"".join(struct.pack("<H", d) for d in dialects)
  message = header + body
  with socket.create_connection((host, int(port)), timeout=TIMEOUT) as s:
    s.sendall(struct.pack(">I", len(message)) + message)
    def read(n):
      data = b""
      while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
          raise ConnectionError("the server closed the connection")
        data += chunk
      return data
    length = struct.unpack(">I", read(4))[0] & 0xFFFFFF
    answer = read(length)
  if answer[:4] != b"\xfeSMB":
    raise ConnectionError("no SMB2 answer (SMB1 only?)")
  status = struct.unpack("<I", answer[8:12])[0]
  if status:
    raise ConnectionError(f"the negotiate was refused (status 0x{status:08x})")
  shape = "<HHHH16sIIIIQQHH" # (its fixed part: the dialect, the server's GUID, capabilities, sizes, its clock, the security blob)
  b = answer[64:64 + struct.calcsize(shape)]
  (_, mode, dialect, _, _guid, caps, _, max_read, max_write, systime, _, sec_off, sec_len) = struct.unpack(shape, b)
  blob = answer[sec_off:sec_off + sec_len]
  when = datetime(1601, 1, 1, tzinfo=timezone.utc).timestamp() + systime / 1e7 if systime else None
  return {"dialect": DIALECTS.get(dialect, f"0x{dialect:04x}"), "signing": "required" if mode & 2 else "enabled" if mode & 1 else "off",
          "caps": [w for bit, w in CAPS.items() if caps & bit], "mechs": [w for oid, w in MECHS.items() if oid in blob],
          "samba": b"not_defined_in_RFC4178@please_ignore" in blob, "time": when, "max_read": max_read, "max_write": max_write}

def smb(settings):
  rows = []
  try:
    n = smb_negotiate(settings.get("host", ""), settings.get("port") or 445)
  except Exception as e:
    return [("Server", f"no SMB2 answer: {e}")]
  rows.append(("Protocol", n["dialect"] + " (the newest both speak, of up to 3.0.2 offered)"))
  rows.append(("Signing", n["signing"]))
  rows.append(("Offers", ", ".join(n["caps"]) or "none of the SMB3 extras"))
  if n["mechs"]:
    rows.append(("Logins", ", ".join(n["mechs"])))
  if n["samba"]:
    rows.append(("Software", "Samba (its negotiate says so)"))
  rows.append(("Largest read", human(n["max_read"])))
  if n["time"]:
    skew = n["time"] - time.time()
    rows.append(("Its clock", f"{datetime.fromtimestamp(n['time']):%Y-%m-%d %H:%M:%S}" + (f" ({skew:+.0f} s from this machine's)" if abs(skew) > 5 else "")))
  return rows

def space(definition, keys):
  """rclone about: its total, used and free space (where the server tells)"""
  try:
    e = RC.Endpoint(definition, keys)
    result = RC.run(["about", "--json", e.path("/")], e.env, timeout=30)
  except Exception:
    return []
  if result.returncode:
    return []
  try:
    data = json.loads(result.stdout or "{}")
  except ValueError:
    return []
  parts = [f"{human(data[k])} {k}" for k in ("total", "used", "free") if isinstance(data.get(k), int)]
  return [("Space", ", ".join(parts))] if parts else []

def globus(settings):
  from . import globus as G
  cid = settings.get("collection")
  if not cid:
    return [("Collection", "none chosen yet")]
  try:
    doc = G.call(G.client().get_endpoint, cid)
  except Exception as e:
    return [("Collection", f"Globus did not say: {getattr(e, 'words', None) or e}")]
  get = doc.get if hasattr(doc, "get") else (lambda k, d=None: doc[k] if k in doc else d)
  rows = []
  for key, label in (("entity_type", "Kind"), ("owner_string", "Owner"), ("organization", "Organization"), ("department", "Department"),
                     ("contact_email", "Contact"), ("gcs_version", "GCS version"), ("host_endpoint_display_name", "Its endpoint"),
                     ("storage_type", "Storage"), ("high_assurance", "High assurance"), ("https_server", "HTTPS"),
                     ("tlsftp_server", "GridFTP"), ("description", "Description")):
    value = get(key)
    if value not in (None, "", [], False):
      text = str(value).replace("GCSv5_mapped_collection", "a mapped collection of Globus Connect Server 5").replace(
        "GCSv5_guest_collection", "a guest collection of Globus Connect Server 5").replace("GCP_mapped_collection", "Globus Connect Personal")
      rows.append((label, text if value is not True else "yes"))
  return rows

def server_info(definition, keys=None):
  """What can be found out of an endpoint's server: [(label, value)]"""
  s = definition.settings
  if keys is None and definition.kind in RC.KINDS:
    try:
      keys = RC.keys_of(definition)
    except Exception:
      keys = {}
  if definition.kind == "webdav":
    rows = webdav(s, keys or {})
  elif definition.kind == "s3":
    rows = s3(s)
  elif definition.kind == "smb":
    rows = smb(s)
  elif definition.kind == "globus":
    return globus(s)
  else:
    return []
  return rows + space(definition, keys)
