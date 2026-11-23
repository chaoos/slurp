"""Fixtures of the unit tests"""
import os
import shutil
import socket
import subprocess
import time
import types

import pytest

SSHD = shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.exists(p)), None)


def until(cond, seconds=30):
  end = time.time() + seconds
  while time.time() < end:
    value = cond()
    if value:
      return value
    time.sleep(0.2)
  return None


@pytest.fixture
def sshd(tmp_path):
  """An sshd of the test on 127.0.0.1 (keys only, no forced command of its own), its authorized_keys in tmp_path"""
  if not (SSHD and shutil.which("ssh") and shutil.which("rsync")):
    pytest.skip("needs sshd, ssh and rsync")
  d = tmp_path / "sshd"
  d.mkdir()
  subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / "hostkey")], check=True)
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
  (d / "authorized_keys").write_text("")
  (d / "sshd_config").write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d}/hostkey\nAuthorizedKeysFile {d}/authorized_keys\n"
                                 f"PidFile {d}/sshd.pid\nUsePAM no\nStrictModes no\nPasswordAuthentication no\n"
                                 "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\n")
  proc = subprocess.Popen([SSHD, "-D", "-f", str(d / "sshd_config"), "-E", str(d / "sshd.log")])
  try:
    if not until(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0, 10):
      pytest.skip(f"the sshd of the test did not start: {(d / 'sshd.log').read_text()[-300:]}")
    known = f"[127.0.0.1]:{port} {(d / 'hostkey.pub').read_text().split()[0]} {(d / 'hostkey.pub').read_text().split()[1]}\n"
    yield types.SimpleNamespace(port=port, dir=d, known=known, target={"host": "127.0.0.1", "port": str(port)})
  finally:
    proc.kill()
