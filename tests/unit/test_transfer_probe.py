"""What an endpoint's server says of itself (its window's Server part), against servers in the test: WebDAV (rclone
serve webdav; a Nextcloud-like server: its status.php), SMB (impacket: the SMB2 negotiate), S3 (moto); a server that
is not there: said, no error"""
import http.server
import json
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from slurp.transfer import endpoints as E, probe as PR


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


@pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")
def test_webdav(check, tmp_path):
  port = free_port()
  proc = subprocess.Popen(["rclone", "--config", "", "serve", "webdav", str(tmp_path), "--addr", f"127.0.0.1:{port}", "--user", "dav",
                           "--pass", "pw"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  try:
    assert wait_port(port)
    rows = dict(PR.server_info(E.EndpointDef("a", "dav", "webdav", {"url": f"http://127.0.0.1:{port}", "user": "dav"}, ["password"]),
                               {"password": "pw"}))
    check("its software, its WebDAV classes", rows.get("Server", "").startswith("rclone/") and "1" in rows.get("WebDAV classes", ""), rows)
  finally:
    proc.kill()


def test_nextcloud(check):
  """A Nextcloud (as one answers): its Server header, and product and version from its status.php"""
  class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
      pass

    def do_OPTIONS(self):
      self.send_response(401)
      self.send_header("Server", "Apache")
      self.send_header("DAV", "1, 3, extended-mkcol")
      self.end_headers()

    def do_GET(self):
      body = json.dumps({"installed": True, "maintenance": False, "versionstring": "29.0.4", "productname": "Nextcloud"}).encode()
      self.send_response(200 if self.path == "/status.php" else 404)
      self.end_headers()
      self.wfile.write(body)
  server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    url = f"http://127.0.0.1:{server.server_port}/remote.php/dav/files/me"
    rows = dict(PR.server_info(E.EndpointDef("n", "nc", "webdav", {"url": url, "vendor": "nextcloud", "user": "me"}, ["password"]),
                               {"password": "x"}))
    check("Nextcloud 29.0.4, from status.php", rows.get("Software") == "Nextcloud 29.0.4", rows)
    check("…its headers, also when it refuses (401)", rows.get("Server") == "Apache" and "extended-mkcol" in rows.get("WebDAV classes", "")
          and rows.get("Answer") == "HTTP 401 to OPTIONS", rows)
  finally:
    server.shutdown()


SMB_SERVER = r"""
import sys
from impacket import smbserver
s = smbserver.SimpleSMBServer(listenAddress="127.0.0.1", listenPort=int(sys.argv[1]))
s.addShare("DATA", sys.argv[2], "")
s.setSMB2Support(True)
s.start()
"""


def test_smb(check, tmp_path):
  pytest.importorskip("impacket")
  port = free_port()
  proc = subprocess.Popen([sys.executable, "-c", SMB_SERVER, str(port), str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  try:
    assert wait_port(port)
    n = PR.smb_negotiate("127.0.0.1", port)
    check("the SMB2 negotiate: a dialect, signing, NTLM", n["dialect"].startswith("SMB 2") and n["signing"] in ("enabled", "required")
          and "NTLM" in n["mechs"], n)
    rows = dict(PR.server_info(E.EndpointDef("s", "smb", "smb", {"host": "127.0.0.1", "port": str(port), "share": "DATA"}, ["password"]),
                               {"password": "x"}))
    check("its rows", rows.get("Protocol", "").startswith("SMB 2") and rows.get("Logins") == "NTLM", rows)
  finally:
    proc.kill()


def test_s3(check):
  moto_server = pytest.importorskip("moto.server")
  server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
  server.start()
  try:
    host, port = server.get_host_and_port()
    rows = dict(PR.s3({"provider": "Other", "endpoint": f"http://{host}:{port}", "bucket": "data"}))
    check("its Server header (moto: Werkzeug), what answered", "Werkzeug" in rows.get("Server", "") and "unsigned HEAD" in rows.get("Answer", ""), rows)
  finally:
    server.stop()


def test_not_there(check):
  port = free_port() # (nothing listens)
  rows = dict(PR.server_info(E.EndpointDef("x", "smb", "smb", {"host": "127.0.0.1", "port": str(port), "share": "D"}, ["password"]), {}))
  check("SMB not there: said", "no SMB2 answer" in rows.get("Server", ""), rows)
  rows = dict(PR.webdav({"url": f"http://127.0.0.1:{port}"}, {}))
  check("WebDAV not there: said", "no answer" in rows.get("Server", ""), rows)
