"""Transfers: the check before a transfer with both ends on this machine (the same filesystem, so: that route), what
remains (a dry run), the room on the destination; the estimates, the warnings and the Globus hint"""
import os
import shutil
import types

import pytest

from slurp.transfer import model as M, plan as P
from slurp.transfer.endpoint import Endpoint

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")


def local():
  return Endpoint(types.SimpleNamespace(name="here", destination=None, connection=None))


def test_check_here(check, tmp_path):
  src, dst = tmp_path / "src", tmp_path / "dst"
  (src / "sub").mkdir(parents=True)
  (src / "a.bin").write_bytes(os.urandom(3_000_000))
  (src / "sub" / "b.txt").write_text("x")
  dst.mkdir()
  cluster = types.SimpleNamespace(name="here", destination=None)
  t = M.Transfer(M.new_id(), "t", {"tab": {}, "paths": [str(src)]}, {"tab": {}, "path": str(dst / "new")})
  reports = []
  plan = P.check(local(), local(), t, cluster, cluster, report=lambda p: reports.append(dict(p["steps"])))
  check("it reported step by step", len(reports) > 5)
  check("done", plan["done"] and plan["steps"]["paths"]["state"] == "ok", plan["steps"])
  check("the destination is created in its folder", plan["dest"]["ancestor"] == str(dst) and plan["dest"]["writable"], plan["dest"])
  check("the same filesystem: found", plan["routes"]["same-fs"]["ok"], plan["routes"])
  check("and no marker left behind", not [p for p in dst.iterdir() if p.name.startswith(".slurp-probe")])
  check("what remains: all of it", plan["remaining_bytes"] == 3_000_001 and plan["remaining_files"] == 2, plan)
  check("free space known", plan["dest"]["free"] and plan["dest"]["free"] > 0)
  check("recommended: the same filesystem", plan["recommended"] == "same-fs", plan["recommended"])
  check("an estimate for it", plan["routes"]["same-fs"]["estimate"] is not None, plan["routes"]["same-fs"])
  check("it runs on the cluster (or in slurp for the relay)", plan["routes"]["same-fs"]["runs_on"] == ["detached"]
        and plan["routes"]["relay"]["runs_on"] == ["slurp"])
  check("this machine is no end for direct routes", not plan["routes"]["direct-push"]["ok"])
  missing = M.Transfer(M.new_id(), "t", {"tab": {}, "paths": [str(tmp_path / "nope")]}, {"tab": {}, "path": str(dst)})
  bad = P.check(local(), local(), missing, cluster, cluster)
  check("a source that is not there: said", bad["steps"]["paths"]["state"] == "bad" and "not there" in bad["steps"]["paths"]["text"], bad["steps"])


def test_room_and_estimates(check):
  dest = {"path": "/scratch/u/x", "free": 10 << 30, "quota": {"used": 95 << 30, "limit": 100 << 30, "files": 900, "files_limit": 1000}}
  warnings = P.room_warnings(dest, 6 << 30, 200, {"scratch_purge_days": 30})
  check("too big for the quota", any("may not fit" in w for w in warnings), warnings)
  check("too many files", any("too many files" in w for w in warnings), warnings)
  check("scratch is purged", any("30 days" in w for w in warnings), warnings)
  check("fits: no warning", P.room_warnings({"path": "/p", "free": 10 << 30}, 1 << 30, 10) == [])
  low, high = P.estimate("relay", 100 << 20, 0, 10 << 20)
  check("an estimate as a range", 7 < low < 9 and 15 < high < 17, (low, high))
  check("no rate: no estimate", P.estimate("relay", 1, 1, None) is None)
  check("durations", P.duration(30) == "30 s" and P.duration(3000) == "50 min" and P.duration(7300) == "2 h 01 min" and P.duration(3 * 86400) == "3 d 0 h")


def test_globus(check):
  both = P.globus_hint({"globus": "CSCS"}, {"globus": "JSC"}, 2 << 40, 10)
  check("both sites: named, strong for a large transfer", both["strong"] and "CSCS" in both["text"] and "JSC" in both["text"])
  check("a small one: not strong", not P.globus_hint({"globus": "a"}, {"globus": "b"}, 1 << 20, 10)["strong"])
  check("large, no site knows it: still suggested", P.globus_hint({}, {}, 2 << 40, 10)["strong"])
  check("small, none: nothing", P.globus_hint({}, {}, 1 << 20, 60) is None)


def test_known_hosts(check, tmp_path):
  kh = tmp_path / "known_hosts"
  kh.write_text("[127.0.0.1]:2222 ssh-ed25519 AAAAkey\nother ssh-ed25519 BBBB\n")
  check("the lines of the host, with its port", P.known_hosts("127.0.0.1", 2222, [str(kh)]).strip() == "[127.0.0.1]:2222 ssh-ed25519 AAAAkey")
  check("none: empty", P.known_hosts("nohost", 22, [str(kh)]) == "")


def test_checksum_estimate(check):
  plain = P.estimate("same-fs", 0, 0, 100 << 20, total_bytes=10 << 30)
  summed = P.estimate("same-fs", 0, 0, 100 << 20, total_bytes=10 << 30, checksum=True)
  check("nothing to copy: next to no time", plain[1] < 1, plain)
  check("with checksums: all of it is read (10 GiB at 100 MiB/s: about 100 s)", 80 < summed[0] < 85 and 160 < summed[1] < 170, summed)
  check("the time of checksums", P.checksum_seconds(10 << 30, 100 << 20) == 102.4 and P.checksum_seconds(None, 1) is None)


def test_login_from_a_node(check, tmp_path, monkeypatch):
  """The login of a direct route from a node of a partition (srun): works, refused, or no node free (not checked)"""
  fake = tmp_path / "bin"
  fake.mkdir()
  (fake / "srun").write_text('#!/bin/sh\nif [ -f "$SRUN_BUSY" ]; then echo "srun: error: Unable to allocate resources: Immediate execution impossible" >&2; exit 1; fi\n'
                             'while [ "${1#-}" != "$1" ]; do shift; done\nexec "$@"\n')
  (fake / "ssh").write_text('#!/bin/sh\nif [ -f "$SSH_REFUSE" ]; then echo "u@jureca: Permission denied (publickey)." >&2; exit 255; fi\n'
                            'echo "Welcome"; printf "\\nSLURP-DIRECT rsync  version 3.2.7  protocol version 31\\n"\n')
  for f in ("srun", "ssh"):
    (fake / f).chmod(0o755)
  monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
  monkeypatch.setenv("SRUN_BUSY", str(tmp_path / "busy"))
  monkeypatch.setenv("SSH_REFUSE", str(tmp_path / "refuse"))
  target = {"host": "jureca", "port": "22", "user": "u", "jump": "", "known_hosts": ""}
  ok, words = P.login_works_on(local(), target, str(tmp_path), "xfer")
  check("it logs in from a node", ok is True and "a node of xfer logs in to jureca" in words, (ok, words))
  (tmp_path / "refuse").write_text("")
  ok, words = P.login_works_on(local(), target, str(tmp_path), "xfer")
  check("refused from the node: said, with what ssh said", ok is False and "Permission denied (publickey)" in words, (ok, words))
  (tmp_path / "busy").write_text("")
  ok, words = P.login_works_on(local(), target, str(tmp_path), "xfer")
  check("no node free: not checked (None)", ok is None and "not checked" in words, (ok, words))


def test_count(check, tmp_path):
  """What the sources hold: bytes of the files, files, folders, links; a path that is not there"""
  (tmp_path / "a" / "b").mkdir(parents=True)
  (tmp_path / "a" / "x").write_bytes(b"1" * 1000)
  (tmp_path / "a" / "b" / "y").write_bytes(b"2" * 500)
  (tmp_path / "a" / "l").symlink_to("x")
  found = P.count(local(), [str(tmp_path / "a"), str(tmp_path / "a" / "x"), str(tmp_path / "nope")])
  check("a folder: its files, folders, links, bytes", found[str(tmp_path / "a")] == {"files": 2, "dirs": 2, "links": 1, "bytes": 1500}, found)
  check("a file", found[str(tmp_path / "a" / "x")] == {"files": 1, "dirs": 0, "links": 0, "bytes": 1000})
  check("not there: None", found[str(tmp_path / "nope")] is None)


def test_patterns(check, tmp_path):
  src = tmp_path / "src"
  src.mkdir()
  for name in ("a.h5", "b.h5", "c.txt"):
    (src / name).write_text("xx")
  found = P.sources(local(), [str(src / "*.h5"), str(src / "*.zip")])
  check("a pattern that matches: there; one that does not: not", found[0]["exists"] and not found[1]["exists"], found)
  counted = P.count(local(), [str(src / "*.h5"), str(src / "*.zip")])
  check("counted: its matches and what they hold", counted[str(src / "*.h5")] == {"files": 2, "dirs": 0, "links": 0, "bytes": 4, "matches": 2}
        and counted[str(src / "*.zip")] is None, counted)
