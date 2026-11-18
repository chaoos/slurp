"""The test lab: a fake Slurm cluster on this machine, reached over a real SSH connection.

``build(work)`` lays out a working directory for the lab scenarios (tests/scenarios/):

  work/sshlab/   an OpenSSH server on 127.0.0.1 (a free port) that runs every session as this user with the fake
                 Slurm commands first on its PATH; the client config (Host lab, Host lab-relay), the keys, and the
                 ``ssh`` wrapper the tests put first on their PATH (it adds the lab config and logs every call)
  work/share/    the keys and the askpass of the "user's own terminal" in the link tests
  work/clipbin/  fake clipboard tools (xclip, pbcopy) that write to clipboard.txt

The login key of ``lab`` has the passphrase ``pw123`` (the tests type it into slurp's login prompt); the relay
key has none. Nothing here touches the user's own ~/.ssh or config.
"""

import getpass
import os
import shutil
import signal
import socket
import stat
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PASSPHRASE = "pw123"
SLURM_COMMANDS = ("sacct", "sacctmgr", "sbatch", "scancel", "scontrol", "sinfo", "squeue", "sstat")
OTHER_COMMANDS = ("srun", "lfs", "quota", "stat")
# like chatty login scripts: text before the shell, with | separators slurp must not mistake for its own output
JUNK = "Welcome to the lab cluster|x|y\nmodule load: something|a|b|c|d|e|f|g|h|i|j|k|l|m\n"
SESSION_DELAY = "0.3" # seconds every session waits (slow login scripts)


def find_sshd():
  return shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.exists(p)), None)


def missing_tools():
  """What the lab needs from this machine and does not find"""
  need = {"sshd": find_sshd(), "ssh": shutil.which("ssh"), "ssh-keygen": shutil.which("ssh-keygen")}
  return [name for name, path in need.items() if not path]


def free_port():
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def write(path, text, mode=0o644):
  with open(path, "w") as f:
    f.write(text)
  os.chmod(path, mode)


def keygen(path, passphrase="", comment="slurp-lab"):
  subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-C", comment, "-f", path], check=True)


class Lab:
  def __init__(self, work):
    self.work = os.path.abspath(work)
    self.dir = os.path.join(self.work, "sshlab")
    self.share = os.path.join(self.work, "share")
    self.clipbin = os.path.join(self.work, "clipbin")
    self.port = None
    self.proc = None

  # ---- the files
  def build(self):
    d, user = self.dir, getpass.getuser()
    for sub in (d, os.path.join(d, "bin"), os.path.join(d, "fakebin"), os.path.join(d, "scratch"), self.share, self.clipbin):
      os.makedirs(sub, exist_ok=True)
    self.port = free_port()
    keygen(os.path.join(d, "hostkey"), comment="slurp-lab-host")
    keygen(os.path.join(d, "userkey"), PASSPHRASE, comment="slurp-lab-user")
    for name in ("relaykey", "clientkey", "otherkey"):
      keygen(os.path.join(self.share, name), comment=f"slurp-lab-{name}")
    write(os.path.join(d, "authorized_keys"), "".join(open(p).read() for p in (os.path.join(d, "userkey.pub"), os.path.join(self.share, "relaykey.pub"))))
    host_pub = " ".join(open(os.path.join(d, "hostkey.pub")).read().split()[:2])
    write(os.path.join(d, "known_hosts"), f"[127.0.0.1]:{self.port} {host_pub}\n")

    # the fake cluster: Slurm commands (one script, by the name it is called with), srun, quota tools, a slow stat
    shutil.copy(os.path.join(HERE, "fakeslurm.py"), os.path.join(d, "fakeslurm.py"))
    os.chmod(os.path.join(d, "fakeslurm.py"), 0o755)
    for name in SLURM_COMMANDS:
      os.symlink(os.path.join(d, "fakeslurm.py"), os.path.join(d, "fakebin", name))
    for name in OTHER_COMMANDS: # (copies: they find the lab from their own path)
      shutil.copy(os.path.join(HERE, "bin", name), os.path.join(d, "fakebin", name))
      os.chmod(os.path.join(d, "fakebin", name), 0o755)
    write(os.path.join(d, "delay"), "0")
    write(os.path.join(d, "session_delay"), SESSION_DELAY)
    write(os.path.join(d, "junk"), JUNK)
    write(os.path.join(d, "labrc"), 'PS1="lab:\\\\w (cluster) "\n')
    write(os.path.join(d, "force.sh"), f"""#!/bin/bash
# ForceCommand of the lab sshd: every session pays the delay in session_delay (slow login scripts), prints the
# text in the file junk (if any) like chatty login scripts, interactive logins get a prompt that is not recognised
d=$(cat {d}/session_delay 2>/dev/null || echo 0)
sleep "$d"
[ -f {d}/junk ] && cat {d}/junk
if [ -z "$SSH_ORIGINAL_COMMAND" ]; then
  exec bash --rcfile {d}/labrc -i
fi
exec bash -c "$SSH_ORIGINAL_COMMAND"
""", 0o755)

    write(os.path.join(d, "sshd_config"), f"""Port {self.port}
ListenAddress 127.0.0.1
HostKey {d}/hostkey
AuthorizedKeysFile {d}/authorized_keys
PidFile {d}/sshd.pid
UsePAM no
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowTcpForwarding yes
X11Forwarding no
AllowAgentForwarding no
AcceptEnv FAKE_*
SetEnv PATH={d}/fakebin:/usr/local/bin:/usr/bin:/bin SCRATCH={d}/scratch XDG_CACHE_HOME={d}/cache
Subsystem sftp internal-sftp
ForceCommand {d}/force.sh
""")
    common = f"""  HostName 127.0.0.1
  Port {self.port}
  User {user}
  IdentitiesOnly yes
  UserKnownHostsFile {d}/known_hosts
  StrictHostKeyChecking accept-new
"""
    write(os.path.join(d, "ssh_config"), f"""Host lab
{common}  IdentityFile {d}/userkey
  ForwardX11 yes
  ForwardAgent yes
  ServerAliveInterval 5

Host lab-relay
{common}  IdentityFile {self.share}/relaykey
""")
    write(os.path.join(d, "bin", "ssh"), f"""#!/bin/sh
# test wrapper: real ssh with the lab config, every call logged
printf '%s %s\\n' "$(date +%s.%N)" "$*" >> {d}/ssh_calls.log
exec {shutil.which("ssh")} -F {d}/ssh_config "$@"
""", 0o755)
    write(os.path.join(d, "askpass"), f"#!/bin/sh\necho {PASSPHRASE}\n", 0o755)
    write(os.path.join(self.share, "askpass.sh"), '#!/bin/sh\n# answers the password prompt of a link\ncat "$SHARE_PW_FILE"\n', 0o755)
    for tool in ("xclip", "pbcopy"):
      write(os.path.join(self.clipbin, tool), f'#!/bin/sh\ncat > "{self.clipbin}/clipboard.txt"\n', 0o755)
    for key in ("hostkey", "userkey"):
      os.chmod(os.path.join(d, key), stat.S_IRUSR | stat.S_IWUSR)
    return self

  # ---- the server
  def start(self, timeout=10):
    sshd = find_sshd()
    self.proc = subprocess.Popen([sshd, "-D", "-f", os.path.join(self.dir, "sshd_config"), "-E", os.path.join(self.dir, "sshd.log")])
    end = time.time() + timeout
    while time.time() < end:
      if self.proc.poll() is not None:
        raise RuntimeError(f"the lab sshd ended ({self.proc.returncode}): {self.log_tail()}")
      try:
        with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
          return self
      except OSError:
        time.sleep(0.1)
    raise RuntimeError(f"the lab sshd does not answer on port {self.port}: {self.log_tail()}")

  def log_tail(self, n=20):
    try:
      return "".join(open(os.path.join(self.dir, "sshd.log")).readlines()[-n:])
    except OSError:
      return ""

  def stop(self):
    for root, dirs, _ in os.walk(self.work): # (a scenario that failed half way may leave sshfs mounts)
      for name in dirs:
        path = os.path.join(root, name)
        if os.path.ismount(path):
          subprocess.run(["fusermount", "-u", "-z", path], capture_output=True)
    if self.proc and self.proc.poll() is None:
      self.proc.send_signal(signal.SIGTERM)
      try:
        self.proc.wait(5)
      except subprocess.TimeoutExpired:
        self.proc.kill()

  def env(self):
    """The environment of a scenario process"""
    return os.environ | {"SLURP_TEST_WORK": self.work}

  def __enter__(self):
    return self.build().start()

  def __exit__(self, *exc):
    self.stop()
