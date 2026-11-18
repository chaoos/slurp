"""Transfers: the relay with both ends on this machine (real rsync, real channels as processes): it copies, a login
script's text before the marker does not disturb it, a second run copies nothing, a stop keeps the half-copied file and
the next run goes on from it; the check of the command the source's rsync asks for"""
import os
import shutil
import threading
import time
import types

import pytest

from slurp.transfer import relay as RL
from slurp.transfer.host import Host

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")


class Chatty(Host):
  """An end whose login scripts print (on stdout and stderr) before every channel's command"""
  def channel_argv(self, script, *args):
    return ["/bin/sh", "-c", 'echo "Welcome|x|y"; echo "module load: hi" >&2; exec /bin/sh -c "$0" "$@"', script, "sh", *map(str, args)]


def local(cls=Host):
  return cls(types.SimpleNamespace(name="here", destination=None, connection=None))


def tree(root, big=0):
  (root / "sub").mkdir(parents=True)
  (root / "a.txt").write_text("alpha")
  (root / "sub" / "b.bin").write_bytes(os.urandom(200_000))
  if big:
    (root / "big.bin").write_bytes(os.urandom(big))


def test_server_command(check):
  ok = RL.server_command("slurp-relay rsync --server -logDtpre.iLsfxCIvu --partial-dir=.slurp-partial . /p/run42/", "/p/run42")
  check("an rsync server that receives into the destination", ok.startswith("rsync --server -logDtpre") and ok.endswith(" . /p/run42/"), ok)
  check("a path with a space (escaped by rsync)", RL.server_command(r"slurp-relay rsync --server -r . /p/my\ dir/", "/p/my dir").endswith("'/p/my dir/'"))
  real = ("slurp-relay rsync --server -ltpre.iLsfxCIvu --log-format=X --timeout=600 --partial-dir .slurp-partial "
          "--info=NAME,STATS2 . /p/run42/")
  check("as rsync 3.2 sends it: values as the next word", "--partial-dir .slurp-partial" in RL.server_command(real, "/p/run42"))
  for bad, why in (("slurp-relay rm -rf /", "not rsync"), ("slurp-relay rsync --server --sender -r . /p/run42", "a sender"),
                   ("slurp-relay rsync --server -r --log-file=/home/u/.bashrc . /p/run42", "a log file elsewhere"),
                   ("slurp-relay rsync --server -r --partial-dir /etc . /p/run42", "another partial folder"),
                   ("slurp-relay rsync --server -r --delete . /p/run42", "deleting"),
                   ("slurp-relay rsync --server -rT /tmp/x . /p/run42", "a temporary folder"),
                   ("slurp-relay rsync --server -r . /etc", "another destination"),
                   ("slurp-relay rsync --server -r ; rm x . /p/run42", "more than options"),
                   ("slurp-relay rsync --server -r . /p/run42; rm -rf ~", "a command after it")):
    try:
      RL.server_command(bad, "/p/run42")
      check(f"refused: {why}", False, bad)
    except RL.RelayError:
      check(f"refused: {why}", True)


def test_relay_copies(check, tmp_path):
  src, dst = tmp_path / "src", tmp_path / "dst"
  tree(src)
  dst.mkdir()
  relay = RL.run(local(Chatty), local(Chatty), [str(src)], str(dst), timeout=60)
  check("rsync ended well", relay.code == 0, (relay.code, relay.error, relay.output.errors, relay.output.other))
  check("the files are there", (dst / "src" / "a.txt").read_text() == "alpha"
        and (dst / "src" / "sub" / "b.bin").read_bytes() == (src / "sub" / "b.bin").read_bytes())
  check("bytes went through slurp", relay.sent > 200_000, relay.sent)
  check("its progress came", relay.output.progress.get("bytes", 0) >= 200_005, relay.output.progress)
  again = RL.run(local(), local(), [str(src)], str(dst), timeout=60)
  check("a second run: nothing copied", again.code == 0 and again.output.stats.get("transferred") == 0, again.output.stats)
  dry = RL.run(local(), local(), [str(src)], str(dst), dry_run=True, timeout=60)
  check("the dry run through the relay: its statistics", dry.code == 0 and dry.output.stats.get("total_size") == 200_005, dry.output.stats)


def test_relay_destination_missing_rsync(check, tmp_path):
  class NoRsync(Host):
    def channel_argv(self, script, *args):
      return ["/bin/sh", "-c", 'PATH=/nonexistent; export PATH; exec /bin/sh -c "$0" "$@"', script, "sh", *map(str, args)]
  src, dst = tmp_path / "src", tmp_path / "dst"
  tree(src)
  dst.mkdir()
  relay = RL.run(local(), local(NoRsync), [str(src)], str(dst), timeout=60)
  check("it fails", relay.code not in (0, None), relay.code)
  check("and says the server on the destination did not run", "server" in relay.error and "127" in relay.error, relay.error)


def test_relay_stop_and_go_on(check, tmp_path):
  src, dst = tmp_path / "src", tmp_path / "dst"
  tree(src, big=40_000_000)
  dst.mkdir()
  relay = RL.Relay(local(), local(), [str(src)], str(dst), opts={"bwlimit": 8}).start() # (8 MB/s: 5 s for the big file)
  end = time.time() + 20
  while time.time() < end and relay.sent < 8_000_000:
    time.sleep(0.1)
  relay.stop()
  code = relay.wait(30)
  partial = list((dst / "src").rglob(".slurp-partial/*"))
  check("stopped: rsync ended as stopped (20)", code == 20, (code, relay.error, relay.output.errors))
  check("the half-copied file is kept", any(p.name == "big.bin" and p.stat().st_size > 1_000_000 for p in partial), partial)
  done = threading.Event()
  again = RL.Relay(local(), local(), [str(src)], str(dst)).start()
  code = again.wait(60)
  done.set()
  check("the next run ends well", code == 0, (code, again.error, again.output.errors))
  check("the big file is whole", (dst / "src" / "big.bin").read_bytes() == (src / "big.bin").read_bytes())
  check("it went on from the half-copied file (less than the whole file sent)", again.sent < 40_000_000, again.sent)


def test_probe(check, tmp_path):
  (tmp_path / "f.bin").write_bytes(os.urandom(3_000_000))
  rate = RL.probe(local(Chatty), local(Chatty), str(tmp_path), seconds=2)
  check("a speed through slurp", rate is not None and rate > 1_000_000, rate)


def test_relay_pattern(check, tmp_path):
  """A pattern among the sources: its matches are copied (each into the destination, as cp), a name with a space too;
  one that matches nothing: rsync says it is not there"""
  src, dst = tmp_path / "src", tmp_path / "dst"
  (src / "sub").mkdir(parents=True)
  for name in ("a.h5", "my file.h5", "c.txt", "sub/d.h5"):
    (src / name).write_text(name)
  dst.mkdir()
  relay = RL.run(local(Chatty), local(Chatty), [str(src / "*.h5")], str(dst), timeout=60)
  check("done", relay.code == 0, (relay.code, relay.output.errors))
  check("only the matches, into the destination", sorted(p.name for p in dst.iterdir()) == ["a.h5", "my file.h5"], sorted(p.name for p in dst.iterdir()))
  none = RL.run(local(), local(), [str(src / "*.zip")], str(dst), timeout=60)
  check("nothing matches: not there (23)", none.code == 23 and any("*.zip" in e for e in none.output.errors), (none.code, none.output.errors))
