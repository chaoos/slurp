"""Transfers: the runner of runs on a cluster, here on this machine (a detached run of the same-filesystem route):
its progress.json, its result, the lock against a second run, a pause and going on; the job script; the remote shell
of direct routes without what login scripts print; the forced command of the temporary key"""
import json
import os
import shutil
import subprocess
import time
import types

import pytest

from slurp.transfer import model as M, runner as RN
from slurp.transfer.host import Host

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")


def local():
  return Host(types.SimpleNamespace(name="here", destination=None, connection=None))


def transfer(src, dst, **options):
  return M.Transfer(M.new_id(), "t", {"tab": {"destination": None}, "paths": [str(src)]}, {"tab": {"destination": None}, "path": str(dst)},
                    route="same-fs", runs_on="detached", options=options)


def until(cond, seconds=30):
  end = time.time() + seconds
  while time.time() < end:
    value = cond()
    if value:
      return value
    time.sleep(0.2)
  return cond()


def test_detached_run(check, tmp_path):
  src, dst = tmp_path / "src", tmp_path / "dst"
  (src / "sub").mkdir(parents=True)
  (src / "a.txt").write_text("alpha")
  (src / "sub" / "big.bin").write_bytes(os.urandom(30_000_000))
  dst.mkdir()
  end = local()
  t = transfer(src, dst, bwlimit=10)
  folder = RN.prepare(end, t, RN.command(t, "same-fs", None, (3, 2, 7)))
  check("the folder is the cache's", folder.startswith(os.environ["XDG_CACHE_HOME"]) and oct(os.stat(folder).st_mode)[-3:] == "700", folder)
  RN.start_detached(end, folder)
  running = until(lambda: (s := RN.status(end, folder))["lock"] and (s["progress"] or {}).get("phase") == "copying" and s)
  check("it runs: its lock, alive, progress", bool(running) and running["alive"], running)
  second = subprocess.run(["sh", f"{folder}/run.sh", folder], capture_output=True, text=True)
  check("a second run of the same transfer does not start", second.returncode == 75 and "runs already" in second.stderr, second.stderr)
  check("pause: TERM to the runner", RN.stop(end, RN.status(end, folder)["lock"]))
  paused = until(lambda: RN.status(end, folder)["result"])
  check("it ended as stopped (20), the lock is gone", paused and paused["exit"] == 20 and not RN.status(end, folder)["lock"], paused)
  check("the half-copied file is kept", any(p.name == "big.bin" for p in (dst / "src").rglob(".slurp-partial/*")))
  RN.start_detached(end, RN.prepare(end, t, RN.command(t, "same-fs", None, (3, 2, 7))))
  done = until(lambda: RN.status(end, folder)["result"] if RN.status(end, folder)["result"] and not RN.status(end, folder)["lock"] else None, 60)
  check("continued: it ended well", done and done["exit"] == 0, done)
  check("the files are whole", (dst / "src" / "sub" / "big.bin").read_bytes() == (src / "sub" / "big.bin").read_bytes())
  last = RN.status(end, folder)
  p = RN.progress_of(last["progress"])
  check("its progress at the end", p.get("phase") == "ended" and p.get("bytes", 0) > 0 and p.get("files", 0) >= 1, p)
  check("the log has the statistics", "Number of regular files transferred" in last["log"], last["log"][-500:])


def test_job_script(check):
  t = M.Transfer("abc", "run42", {"paths": ["/s/x"]}, {"path": "/d"}, options={"partition": "xfer", "time": "12:00:00", "account": "g1"})
  script = RN.job_script(t, "/h/.cache/slurp/transfers/abc")
  for line in ("#SBATCH --job-name=slurp-xfer-abc", "#SBATCH --partition=xfer", "#SBATCH --time=12:00:00",
               "#SBATCH --account=g1", "#SBATCH --output=/h/.cache/slurp/transfers/abc/job-%j.out",
               "exec sh /h/.cache/slurp/transfers/abc/run.sh /h/.cache/slurp/transfers/abc"):
    check(f"the job script: {line}", line in script.splitlines(), script)


def test_direct_command(check):
  t = M.Transfer("abc", "x", {"paths": ["/s/run 42"]}, {"path": "/d/"}, route="direct-push")
  text = RN.command(t, "direct-push", "/f", (3, 2, 7), {"host": "jedi.example", "port": 2222, "user": "u"}, key=True)
  check("push: to host:dest/", "'jedi.example:/d/'" in text or "jedi.example:/d/" in text, text)
  check("never asks, the host key as known, the key of the transfer", all(w in text for w in ("BatchMode=yes", "StrictHostKeyChecking=yes", "/f/key", "-p 2222", "-l u")), text)
  check("the remote side prints the marker first", RN.DIRECT_MARK in text)
  pull = RN.command(t, "direct-pull", "/f", (3, 2, 7), {"host": "alps.example"})
  check("pull: from host:source", "'alps.example:/s/run 42'" in pull, pull)


def test_rsh_skips_login_text(check, tmp_path):
  """The remote shell of direct routes passes only what comes after the marker"""
  fake = tmp_path / "bin"
  fake.mkdir()
  (fake / "ssh").write_text('#!/bin/sh\necho "Welcome to the cluster"; echo "module: x"; printf "\\n%s\\n" ' + RN.DIRECT_MARK + '; printf "PROTOCOL\\001\\002"\n')
  (fake / "ssh").chmod(0o755)
  (tmp_path / "rsh").write_text(RN.RSH)
  out = subprocess.run(["sh", str(tmp_path / "rsh"), "host", "cmd"], capture_output=True, env=os.environ | {"PATH": f"{fake}:{os.environ['PATH']}"})
  check("only the protocol", out.stdout == b"PROTOCOL\x01\x02", out.stdout)


def test_forced_command(check, tmp_path):
  """The temporary key runs exactly an rsync server, nothing else"""
  fake = tmp_path / "bin"
  fake.mkdir()
  (fake / "rsync").write_text('#!/bin/sh\necho "RSYNC $*"\n')
  (fake / "rsync").chmod(0o755)
  def run(original):
    return subprocess.run(["sh", "-c", RN.FORCED_PROGRAM], capture_output=True, text=True,
                          env=os.environ | {"PATH": f"{fake}:{os.environ['PATH']}", "SSH_ORIGINAL_COMMAND": original})
  good = run("printf '\\n" + RN.DIRECT_MARK + "\\n'; exec rsync --server -logDtpre.iLsfxCIvu . /d/my\\ dir/")
  check("the command of a direct route: rsync runs", good.returncode == 0 and "RSYNC --server -logDtpre.iLsfxCIvu . /d/my dir/" in good.stdout, good)
  for bad in ("rm -rf ~", "printf '\\n" + RN.DIRECT_MARK + "\\n'; exec rsync --server . /d; rm -rf ~",
              "rm -rf ~; printf '\\n" + RN.DIRECT_MARK + "\\n'; exec rsync --server . /d",
              "printf '\\n" + RN.DIRECT_MARK + "\\n'; exec rsync --server . $(touch /tmp/x)"):
    result = run(bad)
    check(f"refused: {bad[:50]}", result.returncode == 1 and "RSYNC" not in result.stdout and "only runs rsync" in result.stderr, result)
  line = RN.key_line("ssh-ed25519 AAAAC3x", types.SimpleNamespace(id="abc"), 6)
  check("the line: expires, restricted, the forced command, our comment",
        line.startswith('expiry-time="') and ',restrict,command="' in line and line.endswith(" ssh-ed25519 AAAAC3x slurp-transfer-abc"), line)


def test_keys_script(check, tmp_path, monkeypatch):
  monkeypatch.setenv("HOME", str(tmp_path))
  (tmp_path / ".ssh").mkdir()
  keys = tmp_path / ".ssh" / "authorized_keys"
  keys.write_text("ssh-ed25519 MINE me@laptop\n")
  t = types.SimpleNamespace(id="abc")
  RN.install_key(local(), "ssh-ed25519 K1 slurp-transfer-abc", t)
  RN.install_key(local(), "ssh-ed25519 K2 slurp-transfer-abc", t)
  check("installed once (the new line replaces the old), the others stay", keys.read_text() == "ssh-ed25519 MINE me@laptop\nssh-ed25519 K2 slurp-transfer-abc\n", keys.read_text())
  RN.remove_key(local(), t)
  check("removed: as before", keys.read_text() == "ssh-ed25519 MINE me@laptop\n", keys.read_text())


def test_parse_status(check):
  s = RN.parse_status('--progress\n{"phase": "copying", "bytes": 5}\n\n--result\n\n--alive\nalive\n--lock\n12 login1 \n--job\nRUNNING None\n--log\nx\ny')
  check("the parts", s["progress"] == {"phase": "copying", "bytes": 5} and s["result"] is None and s["alive"]
        and s["lock"] == "12 login1" and s["job"] == "RUNNING None" and s["log"] == "x\ny", s)
  check("json that is cut: none", RN.parse_status('--progress\n{"pha\n--result\n')["progress"] is None)
  check("progress as a transfer's", RN.progress_of({"bytes": 10, "rate": "1.00MB/s", "eta": "0:01:05", "names": 3, "checked": 9, "left": 2})
        == {"bytes": 10, "percent": 0, "rate": 1024 ** 2, "files": 3, "current": "", "phase": "", "updated": None, "rsync_eta": 65, "checked": 9, "left": 2})
  check("json", json.loads(json.dumps(RN.progress_of({}))) == {})


def test_preflight_refused(check, tmp_path, monkeypatch):
  """A direct route: the run logs in first where it runs; refused there, it ends at once and says what ssh said"""
  fake = tmp_path / "bin"
  fake.mkdir()
  (fake / "ssh").write_text('#!/bin/sh\necho "user@jureca: Permission denied (publickey)." >&2\nexit 255\n')
  (fake / "ssh").chmod(0o755)
  monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
  src, dst = tmp_path / "src", tmp_path / "dst"
  src.mkdir()
  (src / "a.txt").write_text("x")
  dst.mkdir()
  end = local()
  t = M.Transfer(M.new_id(), "t", {"paths": [str(src)]}, {"path": str(dst)}, route="direct-push", runs_on="detached")
  target = {"host": "jureca.example", "user": "u"}
  folder = RN.folder_of(end, t)
  RN.prepare(end, t, RN.command(t, "direct-push", folder, (3, 2, 7), target), known_hosts="", login=RN.preflight(target, folder))
  RN.start_detached(end, folder)
  status = until(lambda: (s := RN.status(end, folder))["result"] and not s["lock"] and s)
  check("it ended at once: no login", status and status["result"]["exit"] == 255 and status["result"].get("login") == "refused", status)
  check("the log says where and what ssh said", status and "cannot log in to the other cluster" in status["log"]
        and "Permission denied (publickey)" in status["log"], status and status["log"][-300:])
  check("rsync did not run", not list(dst.iterdir()))
  plain = RN.prepare(end, t, RN.command(t, "same-fs", None, (3, 2, 7)))
  check("another route afterwards: no login first (the file is gone)", not os.path.exists(os.path.join(plain, "preflight")))


def test_job_test_only(check, tmp_path, monkeypatch):
  """sbatch --test-only of a transfer job's script: when and where it would start, or Slurm's error"""
  fake = tmp_path / "bin"
  fake.mkdir()
  (fake / "sbatch").write_text('#!/bin/sh\ngrep -q "account=bad" "$2" && { echo "sbatch: error: Batch job submission failed: Invalid account or account/partition combination specified" >&2; exit 1; }\n'
                               'echo "sbatch: Job 4242 to start at 2026-11-18T14:00:00 using 1 processors on nodes nid0001 in partition xfer" >&2\n')
  (fake / "sbatch").chmod(0o755)
  monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
  t = M.Transfer("abc", "x", {"paths": ["/s"]}, {"path": "/d"}, options={"partition": "xfer", "time": "01:00:00", "account": "g1"})
  ok, words = RN.test_job(local(), t, str(tmp_path))
  check("accepted: when and where", ok and words.startswith("Job 4242 to start at 2026-11-18T14:00:00") and "partition xfer" in words, (ok, words))
  check("its test script is gone", not (tmp_path / "job-test.sh").exists())
  t.options["account"] = "bad"
  ok, words = RN.test_job(local(), t, str(tmp_path))
  check("refused: Slurm's error", not ok and "Invalid account" in words, (ok, words))


def test_detached_pattern(check, tmp_path):
  src, dst = tmp_path / "src", tmp_path / "dst"
  (src / "sub").mkdir(parents=True)
  for name in ("a.h5", "b.txt", "sub/c.h5"):
    (src / name).write_text(name)
  dst.mkdir()
  end = local()
  t = M.Transfer(M.new_id(), "t", {"paths": [str(src / "*.h5"), str(src / "sub")]}, {"path": str(dst)}, route="same-fs", runs_on="detached")
  folder = RN.prepare(end, t, RN.command(t, "same-fs", None, (3, 2, 7)))
  RN.start_detached(end, folder)
  done = until(lambda: (s := RN.status(end, folder))["result"] and not s["lock"] and s)
  check("done", done and done["result"]["exit"] == 0, done)
  check("the matches and the folder", sorted(p.name for p in dst.iterdir()) == ["a.h5", "sub"], sorted(p.name for p in dst.iterdir()))
