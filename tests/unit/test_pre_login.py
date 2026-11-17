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
