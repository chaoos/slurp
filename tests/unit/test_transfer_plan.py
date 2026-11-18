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
