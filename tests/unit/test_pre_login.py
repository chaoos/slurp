"""The command before the login runs in the login's terminal (input and output there); Ctrl-C ends the command, not
what follows: the login starts after it all the same"""
import os
import pty
import select
import subprocess
import sys
import time

from slurp.cluster.remote import PRE_LOGIN


def read_until(fd, text, timeout=10):
  out, end = b"", time.time() + timeout
  while time.time() < end and text.encode() not in out:
    if select.select([fd], [], [], 0.1)[0]:
      try:
        out += os.read(fd, 4096)
      except OSError:
        break
  return out.decode(errors="replace")


def run(command, keys=b""):
  master, slave = pty.openpty()
  proc = subprocess.Popen([sys.executable, "-c", PRE_LOGIN, command, "sh", "-c", "echo LOGIN-STARTS >&2"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=slave, start_new_session=True)
  os.close(slave)
  return proc, master


def test_the_command_then_the_login(check):
  proc, fd = run("printf 'code: '; read c; echo got $c")
  out = read_until(fd, "code: ")
  check("the command, said, asks in the terminal", "(run before the login)" in out and "code: " in out, out)
  os.write(fd, b"42\n")
  out = read_until(fd, "LOGIN-STARTS")
  check("its answer, it ends, then the login", "got 42" in out and "the SSH login follows" in out and "LOGIN-STARTS" in out, out)
  proc.wait(5)


def test_ctrl_c_ends_the_command_not_the_login(check):
  proc, fd = run("echo waiting; sleep 30")
  read_until(fd, "waiting")
  time.sleep(0.3)
  os.write(fd, b"\x03") # Ctrl-C
  out = read_until(fd, "LOGIN-STARTS")
  check("Ctrl-C: the command ends (with its exit code), the login starts", "ended with" in out and "LOGIN-STARTS" in out, out)
  proc.wait(5)


def test_exports_reach_the_login(check):
  """What the command exports (eval $(ssh-agent): SSH_AUTH_SOCK, …) is in the environment of the login; said"""
  master, slave = pty.openpty()
  proc = subprocess.Popen([sys.executable, "-c", PRE_LOGIN, "export SLURP_T1=one; SLURP_T2=two; export SLURP_T2",
                           "sh", "-c", 'echo "LOGIN SEES $SLURP_T1 $SLURP_T2" >&2'],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=slave, start_new_session=True)
  os.close(slave)
  out = read_until(master, "LOGIN SEES")
  out += read_until(master, "\n", 2)
  proc.wait(timeout=10)
  check("the login sees what it exported", "LOGIN SEES one two" in out, out)
  check("said", "it set SLURP_T1, SLURP_T2 for the login" in out, out)
  check("no file left", not any(n.startswith("slurp-env-") for n in os.listdir(os.environ.get("TMPDIR") or "/tmp")
                                if os.path.getmtime(os.path.join(os.environ.get("TMPDIR") or "/tmp", n)) > time.time() - 30))


def test_only_when_the_login_fails(check, tmp_path):
  """Run before login only when a login without it fails: first a login without it that asks nothing (BatchMode); it
  fails: the next one runs the command; once logged in, the next login tries without it again"""
  from slurp.cluster.remote import RemoteSSH
  marker = tmp_path / "ran"
  c = RemoteSSH("nowhere.invalid", ["-o", "ProxyCommand=false"], f"touch {marker}", pre_login_fallback=True)
  def attempt():
    master, slave = pty.openpty()
    c.spawn(slave)
    os.close(slave)
    end = time.time() + 15
    while time.time() < end and c.state == "connecting":
      read_until(master, "\0", 0.2)
    os.close(master)
  try:
    attempt()
    check("first: without the command, asking nothing", "BatchMode=yes" in c.argv and not marker.exists(), c.argv)
    check("it failed: the next login runs it, at once", c.state == "broken" and c.pre_login_due and c.retry_now, (c.state, c.pre_login_due))
    attempt()
    check("then: the command, and a login that may ask", marker.exists() and "BatchMode=yes" not in c.argv, c.argv)
    check("no second retry at once", not c.retry_now)
    c.pre_login_due = False # (as after a login that worked)
    marker.unlink()
    attempt()
    check("logged in once: the next login without it first again", not marker.exists() and "BatchMode=yes" in c.argv)
    plain = RemoteSSH("nowhere.invalid", ["-o", "ProxyCommand=false"], f"touch {marker}")
    c = plain
    attempt()
    check("without the box: the command before every login", marker.exists() and "BatchMode=yes" not in c.argv and not c.retry_now)
  finally:
    c.close()
