"""The "ssh" rclone runs for a cluster tab (rclone on this machine, the cluster over slurp's SSH session): rclone calls
it with "-s sftp" (the SFTP server) or with a command (md5sum <path>, …). It runs ssh on the tab's session (its control
socket: no new login) and starts the SFTP server, or the command, behind a marker line: what the cluster's login
scripts print before it (chatty .bashrc files, a ForceCommand) is dropped, where it would break the SFTP greeting
("packet too long"). A cluster without an sftp-server binary: the SFTP subsystem.

  python sftp_ssh.py <control path> <destination> -s sftp | <command …>

Runs as a script, without slurp's package (rclone starts it)."""
import os
import shlex
import subprocess
import sys

MARK = b"--slurp-sftp-7f3a--\n"
SERVERS = ("/usr/lib/ssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/lib/openssh/sftp-server",
           "/usr/lib64/ssh/sftp-server", "/usr/libexec/sftp-server")
NONE = "--slurp-no-sftp-server--"

def remote_sftp():
  """The command that starts the SFTP server behind the marker (none there: says so, for the subsystem)"""
  servers = " ".join(shlex.quote(p) for p in SERVERS)
  return (f'for p in {servers}; do [ -x "$p" ] && {{ printf "%s\\n" {shlex.quote(MARK.decode().strip())}; exec "$p"; }}; done; '
          f'printf "%s\\n" {NONE}; exit 3')

def run(argv, marked):
  """Run ssh; marked: its output from the marker on (before it: dropped); stdin passed on as it is. -> exit code (None:
  no server there)"""
  proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
  out = sys.stdout.buffer
  buf = b""
  if marked:
    while MARK not in buf:
      chunk = os.read(proc.stdout.fileno(), 65536)
      if not chunk:
        proc.wait()
        return None if NONE.encode() in buf else proc.returncode or 1
      buf += chunk
    buf = buf.split(MARK, 1)[1]
  if buf:
    out.write(buf)
    out.flush()
  while True:
    chunk = os.read(proc.stdout.fileno(), 65536)
    if not chunk:
      break
    out.write(chunk)
    out.flush()
  return proc.wait()

def main(args):
  control, destination, rest = args[0], args[1], args[2:]
  ssh = ["ssh", "-S", control, "-o", "ControlMaster=no", "-o", "BatchMode=yes", "-T", destination]
  if rest[:2] == ["-s", "sftp"]:
    code = run([*ssh, remote_sftp()], marked=True)
    if code is None: # (no sftp-server binary: the subsystem, as it is)
      os.execvp("ssh", [*ssh, "-s", "sftp"])
    return code
  return run([*ssh, f'printf "%s\\n" {shlex.quote(MARK.decode().strip())}; ' + " ".join(rest)], marked=True)

if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
