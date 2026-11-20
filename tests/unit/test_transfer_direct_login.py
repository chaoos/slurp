"""Transfers: how a direct route logs in to the other cluster, against a real sshd of the test (this machine plays both
clusters): the temporary key kept to the transfer's folder by rrsync (a pull reads, a push writes; outside it, the other
way, a shell: refused), and your login now: a prompt answered in a terminal, the connection that stays open, a run
through it, Disconnect (a run then: refused, said). And what these build: the forced command, the paths, the options"""
import os
import pty
import shutil
import socket
import subprocess
import time
import types

import pytest

from slurp.transfer import model as M, plan as P, runner as RN
from slurp.transfer.host import Host

SSHD = shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.exists(p)), None)
needs_sshd = pytest.mark.skipif(not (SSHD and shutil.which("ssh") and shutil.which("rsync")), reason="needs sshd, ssh and rsync")


def local():
  return Host(types.SimpleNamespace(name="here", destination=None, connection=None))


def until(cond, seconds=30):
  end = time.time() + seconds
  while time.time() < end:
    value = cond()
    if value:
      return value
    time.sleep(0.2)
  return None


def test_what_it_builds(check):
  t = M.Transfer("abc", "x", {"paths": ["/scratch/me/run1", "/scratch/me/out/*.h5", "/scratch/me/cfg/"]}, {"path": "/store/proj/"},
                 route="direct-pull")
  check("a pull: kept to the folder all its sources are in", RN.key_scope(t, "direct-pull") == "/scratch/me")
  check("a push: to the destination", RN.key_scope(t, "direct-push") == "/store/proj")
  check("paths within it (a folder's / kept)", [RN.relative(p, "/scratch/me") for p in t.source["paths"]] == ["run1", "out/*.h5", "cfg/"])
  check("a path that is not absolute: not kept to a folder", RN.key_scope(M.Transfer("a", "x", {"paths": ["~/x"]}, {"path": "/d"}), "direct-pull") is None)
  pull = RN.command(t, "direct-pull", "/f", (3, 2, 7), {"host": "alps"}, key=True, scope="/scratch/me")
  check("…the command of a restricted pull: its sources within the folder", "alps:run1 'alps:out/*.h5' alps:cfg/" in pull, pull)
  push = RN.command(t, "direct-push", "/f", (3, 2, 7), {"host": "alps"}, key=True, scope="/store/proj")
  check("…of a restricted push: into the folder itself", "alps:./" in push, push)
  line = RN.key_line("ssh-ed25519 AAAA", t, 2, "/usr/bin/rrsync", "-ro", "/scratch/me")
  check("the key's line: rrsync, read only, its folder; rsync only, expiring", 'exec /usr/bin/rrsync -ro /scratch/me' in line
        and line.startswith('expiry-time="') and ",restrict," in line and line.endswith(" slurp-transfer-abc"), line)
  check("without rrsync there: any rsync server (as before)", "rrsync" not in RN.key_line("ssh-ed25519 AAAA", t, 2))
  master = RN.ssh_options({"host": "h"}, "/f", master=RN.control_path("abc"))
  check("your login: through its connection (in /tmp/slurp-<uid>), never a new one, never asking",
        "ControlPath=/tmp/slurp-%i/abc" in master and "ControlMaster=no" in master and "BatchMode=yes" in master, master)
  check("the login itself may ask (no BatchMode)", "BatchMode=yes" not in RN.login_args(t, "/f", {"host": "h"}, ""))
  check("a login that would ask: from what ssh said", P.asks("u@h: Permission denied (publickey,keyboard-interactive).")
        and P.asks("Permission denied (publickey,password)") and not P.asks("Permission denied (publickey).")
        and not P.asks("Connection refused"))
  check("the login of a transfer: own, key, you; an older one with temp_key: the key",
        M.login_of(M.Transfer("a", "x", {}, {}, options={"temp_key": True})) == "key"
        and M.login_of(M.Transfer("a", "x", {}, {}, options={"login": "you"})) == "you" and M.login_of(M.Transfer("a", "x", {}, {})) == "own")


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


def run_direct(end, t, sshd, folder, **how):
  """A detached run of a direct route, here (both ends are this machine): its status when it ended"""
  login = RN.preflight(sshd.target, folder, how.get("key", False), how.get("master"))
  RN.prepare(end, t, RN.command(t, t.route, folder, (3, 2, 7), sshd.target, **how), known_hosts=sshd.known, login=login)
  RN.start_detached(end, folder)
  return until(lambda: (s := RN.status(end, folder))["result"] and not s["lock"] and s, 60)


def test_restricted_key(check, tmp_path, sshd):
  end = local()
  rrsync = RN.rrsync_on(end)
  if not rrsync:
    pytest.skip("needs rrsync (rsync's script) here")
  data, outside, got = tmp_path / "data", tmp_path / "outside", tmp_path / "got"
  (data / "run1").mkdir(parents=True)
  (data / "run1" / "a.h5").write_text("a")
  (data / "b.h5").write_text("b")
  outside.mkdir()
  (outside / "secret").write_text("secret")
  got.mkdir()
  t = M.Transfer(M.new_id(), "pull", {"paths": [str(data / "run1"), str(data / "*.h5")]}, {"path": str(got)}, route="direct-pull",
                 runs_on="detached", options={"login": "key"})
  folder = RN.folder_of(end, t)
  os.makedirs(folder, exist_ok=True)
  public = RN.make_key(end, folder, t)
  scope = RN.key_scope(t, "direct-pull")
  check("its folder: where the sources are", scope == str(data), scope)
  (sshd.dir / "authorized_keys").write_text(RN.key_line(public, t, 1, rrsync, "-ro", scope) + "\n")
  status = run_direct(end, t, sshd, folder, key=True, scope=scope)
  check("a pull with the restricted key: done", status and status["result"]["exit"] == 0, status and status["log"][-400:])
  check("…what it had to copy", sorted(os.listdir(got)) == ["b.h5", "run1"], sorted(os.listdir(got)))
  # the same key, asked for something else: refused
  sneaky = M.Transfer(t.id, "x", {"paths": [str(outside / "secret")]}, {"path": str(got)}, route="direct-pull", runs_on="detached")
  status = run_direct(end, sneaky, sshd, folder, key=True, scope=scope)
  check("a file outside its folder: refused", status and status["result"]["exit"] != 0 and not (got / "secret").exists())
  push = M.Transfer(t.id, "x", {"paths": [str(outside / "secret")]}, {"path": str(data)}, route="direct-push", runs_on="detached")
  status = run_direct(end, push, sshd, folder, key=True, scope=str(data))
  check("writing with a key that only reads: refused", status and status["result"]["exit"] != 0 and not (data / "secret").exists())
  shell = subprocess.run(["ssh", *RN.ssh_options(sshd.target, folder, key=True), "-o", "UserKnownHostsFile=" + folder + "/known_hosts",
                          "127.0.0.1", "cat", str(outside / "secret")], capture_output=True, text=True, timeout=30)
  check("a command, not rsync: refused", shell.returncode != 0 and "secret" not in shell.stdout, (shell.returncode, shell.stderr[-200:]))


def test_your_login(check, tmp_path, sshd, monkeypatch):
  """Your login now: ssh asks (here the passphrase of a key: as a password or a code would), you answer in the
  terminal; the connection stays open; a run goes through it without asking; Disconnect; a run then: refused"""
  end = local()
  key = tmp_path / "mykey"
  subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "s3cret", "-f", str(key)], check=True)
  (sshd.dir / "authorized_keys").write_text(key.with_suffix(".pub").read_text())
  # (ssh of the test: that key only, no agent; the user's own ssh setup is not used)
  bin_dir = tmp_path / "bin"
  bin_dir.mkdir()
  (bin_dir / "ssh").write_text(f'#!/bin/sh\nexec {shutil.which("ssh")} -o IdentityFile={key} -o IdentitiesOnly=yes -o IdentityAgent=none "$@"\n')
  (bin_dir / "ssh").chmod(0o755)
  monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
  src, dst = tmp_path / "src", tmp_path / "dst"
  src.mkdir()
  (src / "a.txt").write_text("a")
  dst.mkdir()
  t = M.Transfer(M.new_id(), "push", {"paths": [str(src)]}, {"path": str(dst)}, route="direct-push", runs_on="detached", options={"login": "you"})
  folder = RN.folder_of(end, t)
  master = RN.control_path(t.id)
  try:
    status = run_direct(end, t, sshd, folder, master=master)
    check("not logged in yet: the run is refused at once (it never asks)", status and status["result"].get("login") == "refused", status)
    check("…is it open? no", RN.login_state(end, t, sshd.target, folder) == (False, ""))
    # the terminal: answered as you would
    pid, fd = pty.fork()
    if pid == 0:
      os.execvp("sh", ["sh", "-c", RN.LOGIN_SCRIPT, "sh", *RN.login_args(t, folder, sshd.target, sshd.known)])
    out, answered, stop = b"", False, time.time() + 30
    while time.time() < stop:
      try:
        data = os.read(fd, 1024)
      except OSError:
        break
      if not data:
        break
      out += data
      if b"passphrase" in out and not answered:
        os.write(fd, b"s3cret\n")
        answered = True
    _, code = os.waitpid(pid, 0)
    os.close(fd) # (the terminal closes)
    text = out.decode(errors="replace")
    check("the login asked in the terminal, was answered, and stayed open", answered and os.waitstatus_to_exitcode(code) == 0
          and "connected; it stays open" in text, text[-400:])
    state = RN.login_state(end, t, sshd.target, folder)
    check("…open, on this host", state[0] is True and state[1], state)
    status = run_direct(end, t, sshd, folder, master=master)
    check("a run through it: done, without asking", status and status["result"]["exit"] == 0 and (dst / "src" / "a.txt").exists(),
          status and status["log"][-400:])
    check("the login of the check goes through it too", P.login_works(end, sshd.target, folder, master=master)[0])
    RN.close_login(end, t, sshd.target, folder)
    check("Disconnect: closed", RN.login_state(end, t, sshd.target, folder) == (False, ""))
    status = run_direct(end, t, sshd, folder, master=master)
    check("a run then: refused (it never asks)", status and status["result"].get("login") == "refused", status)
  finally:
    RN.close_login(end, t, sshd.target, folder)
