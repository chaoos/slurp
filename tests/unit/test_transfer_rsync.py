"""Transfers: rsync's options (the same every run: idempotent), its progress and statistics as rsync 3.2 writes
them, and what its exit codes come to"""
import subprocess
import shutil

import pytest

from slurp.transfer import rsync as R

PROGRESS = ("          32768   0%    0.00kB/s    0:00:00  \r"
            "       21495808  71%   19.55MB/s    0:00:00  \r"
            "src/a.bin\n"
            "       30000000  99%   20.89MB/s    0:00:01 (xfr#1, to-chk=3/5)\r"
            "src/sub/b.bin\n"
            "rsync: [sender] send_files failed to open \"/x/secret\": Permission denied (13)\n"
            "       30005003 100%   20.86MB/s    0:00:01 (xfr#3, to-chk=0/5)\n"
            "\nNumber of files: 5 (reg: 3, dir: 2)\nNumber of created files: 5 (reg: 3, dir: 2)\n"
            "Number of regular files transferred: 3\nTotal file size: 30005003 bytes\n"
            "Total transferred file size: 30005003 bytes\nLiteral data: 30005003 bytes\n"
            "Total bytes sent: 30012601\nTotal bytes received: 85\n\n"
            "sent 30012601 bytes  received 85 bytes  12005074.40 bytes/sec\ntotal size is 30005003  speedup is 1.00\n")


def test_options(check):
  base = R.options()
  check("recursive, links, permissions, times; not owners or groups (they differ between sites)",
        base[0] == "-rlpt" and not any(o in ("-a", "-o", "-g") for o in base), base)
  check("half-copied files are kept for the next run", "--partial-dir=.slurp-partial" in base)
  check("never deletes on the destination", not any("delete" in o for o in base))
  check("progress of the whole run, the names, statistics", "--info=progress2,stats2,name1" in base)
  check("an old rsync: no progress2, statistics only", "--info=progress2,stats2,name1" not in R.options(version=(3, 0, 9))
        and "--stats" in R.options(version=(3, 0, 9)))
  dry = R.options(dry_run=True)
  check("the dry run: -n with statistics", "-n" in dry and "--stats" in dry and not any("progress2" in o for o in dry))
  opts = R.options({"checksum": True, "compress": True, "bwlimit": "2.5"})
  check("options: checksums, compression, a limit in MB/s (rsync: KiB/s)",
        "--checksum" in opts and "--compress" in opts and "--bwlimit=2560" in opts, opts)
  check("no limit for nonsense", not any("bwlimit" in o for o in R.options({"bwlimit": "x"})))


def test_versions(check):
  check("rsync's version line", R.version_of("rsync  version 3.2.7  protocol version 31\nCopyright") == (3, 2, 7))
  check("openrsync and others: none", R.version_of("openrsync: protocol version 29") is None)


def test_progress_line(check):
  p = R.parse_progress("       30000000  99%   20.89MB/s    0:01:01 (xfr#1, to-chk=3/5)")
  check("bytes, percent, rate, the time left, files", p == {"bytes": 30000000, "percent": 99, "rate": 20.89 * 1024 ** 2,
                                                            "eta": 61, "xfr": 1, "left": 3, "checked": 5}, p)
  check("with thousands separators (older rsync)", R.parse_progress("  1,234,567  45%  1.00kB/s 0:00:02")["bytes"] == 1234567)
  check("the scan of an incremental recursion (ir-chk)", R.parse_progress("  10  1%  0.00kB/s 0:00:00 (xfr#1, ir-chk=1000/1002)")["left"] == 1000)
  check("a name is not a progress line", R.parse_progress("src/a.bin") is None)


def test_output(check):
  out = R.Output()
  for i in range(0, len(PROGRESS), 7): # (in pieces, as it comes over a channel)
    out.feed(PROGRESS[i:i + 7].encode())
  out.close()
  check("the last progress", out.progress.get("bytes") == 30005003 and out.progress.get("left") == 0, out.progress)
  check("the file named last", out.current == "src/sub/b.bin", out.current)
  check("an error about one file", out.error_count == 1 and "Permission denied" in out.errors[0], out.errors)
  check("the statistics", out.stats == {"files": 5, "transferred": 3, "total_size": 30005003, "to_transfer": 30005003,
                                        "literal": 30005003, "sent": 30012601, "received": 85, "created": 5}, out.stats)


def test_outcome(check):
  check("0: done", R.outcome(0) == ("done", ""))
  check("23: done with errors", R.outcome(23)[0] == "done-errors")
  check("24 (files vanished): done", R.outcome(24)[0] == "done")
  check("stopped on purpose: paused", R.outcome(20, stopped=True) == ("paused", "paused"))
  check("a broken connection: failed, with why", R.outcome(12)[0] == "failed" and "connection" in R.outcome(12)[1])
  check("no result at all", R.outcome(None)[0] == "failed")


@pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")
def test_idempotent(check, tmp_path):
  """Running the same transfer twice: the second run copies nothing, and the dry run says so"""
  src, dst = tmp_path / "src", tmp_path / "dst"
  (src / "sub").mkdir(parents=True)
  (src / "a.bin").write_bytes(b"x" * 100000)
  (src / "sub" / "b.txt").write_text("hello")
  dst.mkdir()
  run = lambda *extra: subprocess.run(["rsync", *R.options(), *extra, str(src), f"{dst}/"], capture_output=True, text=True)
  first = run()
  check("the first run copies", first.returncode == 0 and (dst / "src" / "sub" / "b.txt").read_text() == "hello", first.stderr)
  dry = subprocess.run(["rsync", *R.options(dry_run=True), str(src), f"{dst}/"], capture_output=True, text=True)
  stats = R.parse_stats(dry.stdout)
  check("the dry run after it: nothing left", stats.get("transferred") == 0 and stats.get("to_transfer") == 0, stats)
  (dst / "src" / "extra").write_text("mine")
  second = R.Output()
  second.feed(run().stdout)
  second.close()
  check("the second run copies nothing", second.stats.get("transferred") == 0, second.stats)
  check("and keeps what is only on the destination", (dst / "src" / "extra").exists())
