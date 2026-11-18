"""rsync's command lines, its progress (--info=progress2, name1) and statistics (--stats), and what its exit codes mean.

Every run has the same options, so that running a transfer again only copies what is missing or changed: -rlpt
(recursive, symlinks as symlinks, permissions, times; not owners and groups: their numbers differ between sites),
half-copied files in .slurp-partial (the next run goes on from them), never --delete."""

import re

PARTIAL_DIR = ".slurp-partial"
MIN_PROGRESS = (3, 1, 0) # --info=progress2 and --outbuf (older rsync: only the statistics at the end)

def version_of(text):
  """(3, 2, 7) from `rsync --version` (None if it is not rsync's)"""
  m = re.search(r"rsync\s+version\s+v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
  return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None

def version_text(version):
  return ".".join(map(str, version)) if version else "?"

def options(opts=None, version=(3, 2, 0), dry_run=False):
  """The options of a run (opts: the transfer's options; version: of the rsync that runs it, the client)"""
  opts = opts or {}
  out = ["-rlpt", f"--partial-dir={PARTIAL_DIR}", "--no-h", "--timeout=600"]
  if dry_run:
    out += ["-n", "--stats"]
  elif version is None or tuple(version) >= MIN_PROGRESS:
    out += ["--info=progress2,stats2,name1", "--outbuf=N"]
  else:
    out += ["--stats", "-v"]
  if opts.get("checksum"):
    out.append("--checksum")
  if opts.get("compress"):
    out.append("--compress")
  try:
    limit = float(opts.get("bwlimit") or 0)
  except (TypeError, ValueError):
    limit = 0
  if limit > 0:
    out.append(f"--bwlimit={max(1, int(limit * 1024))}") # (MB/s; rsync takes KiB/s)
  return out

PROGRESS = re.compile(r"^\s*(\d[\d,.']*)\s+(\d{1,3})%\s+([\d.,]+\s*[kKMGTP]?i?B/s)\s+(\d+):(\d\d):(\d\d)"
                      r"(?:\s+\(xfr#(\d+),\s*(?:to-chk|ir-chk|to-check)=(\d+)/(\d+)\))?")
UNITS = {"": 1, "k": 1024, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}

def rate_of(text):
  """Bytes per second from rsync's "20.88MB/s" """
  m = re.match(r"([\d.,]+)\s*([kKMGTP]?)i?B/s", (text or "").strip())
  if not m:
    return 0.0
  try:
    return float(m.group(1).replace(",", ".")) * UNITS[m.group(2)]
  except ValueError:
    return 0.0

def parse_progress(line):
  """A line of --info=progress2: {bytes, percent, rate (B/s), eta (s), xfr, left, checked} (None if it is not one)"""
  m = PROGRESS.match(line or "")
  if not m:
    return None
  out = {"bytes": int(re.sub(r"[,.']", "", m.group(1))), "percent": int(m.group(2)), "rate": rate_of(m.group(3)),
         "eta": int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6))}
  if m.group(7):
    out.update(xfr=int(m.group(7)), left=int(m.group(8)), checked=int(m.group(9)))
  return out

STATS = {
  "files":        r"Number of files:\s*([\d,.]+)",
  "transferred":  r"Number of regular files transferred:\s*([\d,.]+)",
  "total_size":   r"Total file size:\s*([\d,.]+)",
  "to_transfer":  r"Total transferred file size:\s*([\d,.]+)",
  "literal":      r"Literal data:\s*([\d,.]+)",
  "sent":         r"Total bytes sent:\s*([\d,.]+)",
  "received":     r"Total bytes received:\s*([\d,.]+)",
  "created":      r"Number of created files:\s*([\d,.]+)",
}

def parse_stats(text):
  """rsync's --stats: {files, transferred, total_size, to_transfer, literal, sent, received, created} (those there)"""
  out = {}
  for key, pattern in STATS.items():
    m = re.search(pattern, text or "")
    if m:
      out[key] = int(re.sub(r"[,.]", "", m.group(1)))
  return out

ERROR = re.compile(r"^(rsync(:| error| warning)|.*\b(Permission denied|No such file or directory|failed:|"
                   r"No space left|Disk quota exceeded|Read-only file system)\b)")

class Output:
  """What rsync writes, fed as it comes (bytes or text): its progress, the file it copies now, the errors, the
  statistics at the end. Progress lines end with \\r, the others with \\n."""
  MAX_ERRORS = 200

  def __init__(self):
    self.buffer = ""
    self.progress = {}   # the last progress line (parse_progress)
    self.current = ""    # the file named last
    self.files = 0       # names seen (files and directories it updated)
    self.errors = []
    self.error_count = 0
    self.stats_lines = []
    self.in_stats = False
    self.other = []      # (lines it did not know: kept for the log)

  def feed(self, data):
    if isinstance(data, bytes):
      data = data.decode(errors="replace")
    self.buffer += data
    *lines, self.buffer = re.split(r"[\r\n]", self.buffer)
    for line in lines:
      self.line(line)

  def close(self):
    if self.buffer:
      self.line(self.buffer)
      self.buffer = ""

  def line(self, line):
    if not line.strip():
      return
    p = parse_progress(line)
    if p is not None:
      self.progress = p
      return
    text = line.rstrip()
    if text.startswith("Number of files:") or text.startswith("sent ") and " received " in text:
      self.in_stats = True
    if self.in_stats or text.startswith("total size is "):
      self.stats_lines.append(text)
      return
    if ERROR.match(text):
      self.error_count += 1
      if len(self.errors) < self.MAX_ERRORS:
        self.errors.append(text)
      return
    if text in ("sending incremental file list", "receiving incremental file list", "building file list ... done",
                "sending file list", "created directory"):
      return
    if text.startswith(("created directory ", "skipping ", "delta-transmission ")):
      self.other.append(text)
      return
    self.current = text
    self.files += 1

  @property
  def stats(self):
    return parse_stats("\n".join(self.stats_lines))

# rsync's exit codes: what the run came to (paused: it was stopped on purpose; others: as rsync says)
EXIT = {0: "done", 23: "done-errors", 24: "done", 20: "stopped", 30: "timeout", 35: "timeout", 12: "broken",
        255: "broken", 10: "broken", 5: "broken", 1: "failed", 2: "failed", 3: "failed", 11: "failed", 13: "failed"}
EXIT_TEXT = {0: "", 1: "syntax or usage error", 2: "protocol incompatibility", 3: "errors selecting input/output files",
             5: "error starting the client-server protocol", 10: "error in the socket I/O", 11: "error in file I/O",
             12: "error in the rsync protocol data stream (the connection broke, or a login script printed text)",
             13: "errors with program diagnostics", 20: "stopped by a signal", 23: "some files could not be copied",
             24: "some source files vanished during the run", 30: "timeout in sending or receiving data",
             35: "timeout waiting for the daemon", 127: "rsync was not found", 255: "the SSH connection failed"}

def outcome(code, stopped=False):
  """(state, words) of a run that ended with code: stopped on purpose (pause): paused"""
  if stopped and code != 0:
    return "paused", "paused"
  kind = EXIT.get(code, "failed")
  words = EXIT_TEXT.get(code, f"rsync ended with {code}")
  if kind in ("done", "done-errors"):
    return kind, words
  return "failed", f"rsync ended with {code}: {words}" if code is not None else "the run ended without a result"
