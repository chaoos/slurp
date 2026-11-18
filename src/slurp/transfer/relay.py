"""The relay: rsync runs on the source and copies to an rsync server on the destination; its connection to that
server goes through slurp, over a channel of each tab's SSH session (no new login, the clusters need not reach each
other, no port is opened anywhere).

On the source, rsync's "remote shell" is a small helper: it writes the command rsync wants to run on the other side
(rsync --server …) as one line, then carries rsync's protocol over the channel's input and output. slurp checks that
line (an rsync server that receives into the destination, nothing else), starts it on the destination in a channel of
its own, and passes the bytes between the two channels. What login scripts print before is skipped: each side writes
a marker line first, and only what follows it counts."""

import re
import secrets
import shlex
import threading
import time

from ..state import log
from . import rsync as R

MARK = "SLURP-RELAY"
HEADER_LIMIT = 1 << 20 # bytes read before the marker and the command at most (login scripts that print a lot)

# The source's channel ($1: the token; then rsync's arguments, the remote side last as slurp-relay:<destination>/).
# fd 3 and 4 are the channel's input and output (the protocol), rsync's own output goes to stderr (its progress).
SOURCE_SCRIPT = (
  'h=$(mktemp "${TMPDIR:-/tmp}/slurp-relay.XXXXXX") || exit 1; '
  # (the helper: rsync → slurp in the background, slurp → rsync in front; when the server's side ends, the other
  # direction is ended too, so that rsync sees the end of its connection: its socket is one for both directions)
  "printf '%s\\n' 'shift; printf \"%s\\n\" \"$*\" >&4; exec 5<&0; cat <&5 >&4 3<&- & c=$!; exec 5<&-; "
  "cat <&3 4>&-; kill $c 2>/dev/null; exit 0' >\"$h\"; "
  "exec 3<&0 4>&1 1>&2; "
  f"printf '\\n{MARK} %s %s\\n' \"$1\" $$ >&4; shift; "
  "p=; trap 'kill -TERM $p 2>/dev/null' TERM HUP INT; "
  'rsync --rsh="sh $h" "$@" & p=$!; '
  "wait $p; r=$?; "
  "while [ $r -gt 128 ] && kill -0 $p 2>/dev/null; do wait $p; r=$?; done; "
  "if [ $r -gt 128 ]; then wait $p; q=$?; [ $q -ne 127 ] && r=$q; fi; "
  'rm -f "$h"; exit $r')

# The destination's channel ($1: the token, $2: the command, checked): the marker, then the rsync server
DEST_SCRIPT = f"printf '\\n{MARK} %s\\n' \"$1\"; eval \"exec $2\""

class RelayError(Exception):
  pass

# options of the server that take their value as the next word (rsync passes them so), and what the value may be
SERVER_VALUES = {"--log-format": None, "--timeout": r"\d+", "--info": None, "--debug": None, "--bwlimit": r"[\d.]+[kKmMgG]?",
                 "--compress-level": r"\d+", "--partial-dir": re.escape(R.PARTIAL_DIR), "--checksum-choice": None,
                 "--compress-choice": None, "--modify-window": r"-?\d+", "--outbuf": r"[NLB]"}
# options that would make the server write or delete elsewhere, or read files there: never
SERVER_REFUSED = ("--log-file", "--temp-dir", "--backup", "--suffix", "--compare-dest", "--copy-dest", "--link-dest",
                  "--files-from", "--write-batch", "--read-batch", "--only-write-batch", "--delete", "--remove-source-files",
                  "--rsync-path", "--exclude-from", "--include-from", "--sender", "--daemon", "--config")

def server_command(header, dest_path):
  """The command the source's rsync wants to run on the destination, checked: an rsync server that receives into
  dest_path (its options, the "." and the destination; the partial folder only ours, nothing that writes or deletes
  elsewhere). -> the command to run there (quoted anew)"""
  try:
    words = shlex.split(header)
  except ValueError as e:
    raise RelayError(f"the command of the source's rsync cannot be read: {e}") from None
  if words and words[0] == "slurp-relay": # (the host name rsync gives its remote shell)
    words = words[1:]
  if len(words) < 4 or words[0] != "rsync" or words[1] != "--server":
    raise RelayError(f"the source's rsync asked for something else than an rsync server: {header[:200]}")
  *options, dot, target = words[2:]
  if dot != ".":
    raise RelayError(f"unexpected arguments for the rsync server: {header[:200]}")
  i = 0
  while i < len(options):
    word = options[i]
    name, _, value = word.partition("=")
    if not word.startswith("-") or any(name == r or name.startswith(r) for r in SERVER_REFUSED):
      raise RelayError(f"the source's rsync asked for an option the relay does not pass on: {word[:100]}")
    if name in SERVER_VALUES:
      if not _:
        i += 1
        if i >= len(options):
          raise RelayError(f"{name} without its value")
        value = options[i]
      pattern = SERVER_VALUES[name]
      if pattern is not None and not re.fullmatch(pattern, value):
        raise RelayError(f"unexpected value for {name}: {value[:100]}")
    elif not word.startswith("--") and len(word) > 1 and "T" in word.split(".")[0]: # (-T: a temporary folder)
      raise RelayError(f"the source's rsync asked for an option the relay does not pass on: {word[:100]}")
    i += 1
  if target.rstrip("/") != dest_path.rstrip("/"):
    raise RelayError(f"the rsync server would receive into {target}, not into {dest_path}")
  return shlex.join(["rsync", "--server", *options, ".", target])

def read_marker(stream, token, with_line=False):
  """Read a channel's output up to its marker line (MARK token [pid]); with_line: and the line after it (the
  command). -> (pid or "", the line, bytes already read after them)"""
  buffer = b""
  marker = f"{MARK} {token}".encode()
  while True:
    at = buffer.find(marker)
    if at >= 0:
      end = buffer.find(b"\n", at)
      if end >= 0:
        words = buffer[at:end].decode(errors="replace").split()
        pid = words[2] if len(words) > 2 else ""
        rest = buffer[end + 1:]
        if not with_line:
          return pid, "", rest
        line_end = rest.find(b"\n")
        if line_end >= 0:
          return pid, rest[:line_end].decode(errors="replace"), rest[line_end + 1:]
    if len(buffer) > HEADER_LIMIT:
      raise RelayError("no marker from the channel (a login script that prints without end?)")
    chunk = stream.read1(1 << 16) if hasattr(stream, "read1") else stream.read(1 << 16)
    if not chunk:
      raise EOFError("the channel ended before its marker")
    buffer += chunk

class Relay:
  """One rsync run through slurp (or its dry run): start(), then wait() for rsync's exit code; stop() ends it the
  way a pause does (rsync keeps the half-copied file); output: the parsed output of rsync (its progress)"""
  def __init__(self, source, dest, paths, dest_path, opts=None, version=(3, 2, 0), dry_run=False):
    self.source, self.dest = source, dest
    self.paths, self.dest_path = list(paths), dest_path
    self.opts, self.version, self.dry_run = opts or {}, version, dry_run
    self.token = secrets.token_hex(8)
    self.output = R.Output()
    self.a = self.b = None
    self.pid = ""
    self.stopped = False
    self.error = ""          # what went wrong in slurp's part (the marker, the command, the destination's server)
    self.dest_stderr = b""
    self.sent = self.received = 0 # bytes passed from the source to the destination, and back
    self.started = None
    self.code = None
    self.lock = threading.Lock()
    self.threads = []
    self.ready = threading.Event() # (the destination's server runs: bytes flow)

  def args(self):
    return [*R.options(self.opts, self.version, self.dry_run), "--", *self.paths, f"slurp-relay:{self.dest_path.rstrip('/')}/"]

  def start(self):
    self.started = time.monotonic()
    self.a = self.source.popen(SOURCE_SCRIPT, self.token, *self.args())
    self.thread(self.read_errors, "relay stderr")
    self.thread(self.connect, "relay connect")
    return self

  def thread(self, target, name):
    t = threading.Thread(target=target, daemon=True, name=name)
    t.start()
    self.threads.append(t)

  def read_errors(self):
    stream = self.a.stderr
    while True:
      chunk = stream.read1(1 << 16) if hasattr(stream, "read1") else stream.read(1 << 16)
      if not chunk:
        break
      with self.lock:
        self.output.feed(chunk)
    with self.lock:
      self.output.close()

  def connect(self):
    try:
      self.pid, header, rest = read_marker(self.a.stdout, self.token, with_line=True)
      command = server_command(header, self.dest_path)
      self.b = self.dest.popen(DEST_SCRIPT, self.token, command)
      self.thread(self.read_dest_errors, "relay dest stderr")
      _, _, back = read_marker(self.b.stdout, self.token)
    except Exception as e: # (the source's rsync gets EOF and ends)
      self.error = self.error or str(e)
      log.debug(f"relay: {e!r}")
      self.close(self.a.stdin)
      if self.b is not None:
        self.close(self.b.stdin)
      return
    self.ready.set()
    if rest:
      self.write(self.b.stdin, rest, "sent")
    if back:
      self.write(self.a.stdin, back, "received")
    self.thread(lambda: self.pipe(self.a.stdout, self.b.stdin, "sent"), "relay →")
    self.pipe(self.b.stdout, self.a.stdin, "received")

  def read_dest_errors(self):
    try:
      data = self.b.stderr.read()
    except (OSError, ValueError):
      data = b""
    self.dest_stderr = data[-4000:]

  def write(self, stream, data, counter):
    try:
      stream.write(data)
      stream.flush()
    except (OSError, ValueError):
      return False
    setattr(self, counter, getattr(self, counter) + len(data))
    return True

  def pipe(self, source, target, counter):
    while True:
      try:
        chunk = source.read1(1 << 16) if hasattr(source, "read1") else source.read(1 << 16)
      except (OSError, ValueError):
        chunk = b""
      if not chunk or not self.write(target, chunk, counter):
        break
    self.close(target)

  @staticmethod
  def close(stream):
    try:
      stream.close()
    except (OSError, ValueError):
      pass

  def stop(self):
    """End the run as a pause does: rsync gets TERM (it keeps the file it copies in .slurp-partial)"""
    self.stopped = True
    if self.pid:
      try:
        self.source.kill(self.pid, "TERM")
      except Exception as e:
        log.debug(f"relay: stopping {self.pid}: {e!r}")
    elif self.a is not None and self.a.poll() is None:
      self.a.terminate()

  def poll(self):
    return None if self.a is None else self.a.poll()

  def wait(self, timeout=None):
    """rsync's exit code (None if it did not end within timeout)"""
    try:
      code = self.a.wait(timeout)
    except Exception:
      return None
    if self.b is not None:
      try:
        self.b.wait(10)
      except Exception:
        self.b.kill()
    for t in self.threads:
      t.join(2)
    if self.b is not None and self.b.returncode not in (0, None) and not self.error and code != 0:
      said = self.dest_stderr.decode(errors="replace").strip().splitlines()
      self.error = f"the rsync server on {self.dest.name} ended with {self.b.returncode}" + (f": {said[-1][:200]}" if said else "")
    self.code = code
    return code

  @property
  def rate(self):
    """Bytes per second passed so far (both ways)"""
    if not self.started:
      return 0.0
    return (self.sent + self.received) / max(time.monotonic() - self.started, 1e-3)

def run(source, dest, paths, dest_path, opts=None, version=(3, 2, 0), dry_run=False, timeout=None):
  """A whole run through the relay (for the dry run of the check): -> the Relay, ended"""
  relay = Relay(source, dest, paths, dest_path, opts, version, dry_run).start()
  if relay.wait(timeout) is None:
    relay.stop()
    relay.wait(30)
  return relay

# ---- a sample of data from the source to the destination through slurp: the speed of the relay
PROBE_SOURCE = (f"printf '\\n{MARK} %s\\n' \"$1\"; f=$(find \"$2\" -type f -size +1M 2>/dev/null | head -n 1); "
                'if [ -n "$f" ]; then exec head -c "$3" "$f"; else exec head -c "$3" /dev/urandom; fi')
PROBE_DEST = f"printf '\\n{MARK} %s\\n' \"$1\"; exec cat >/dev/null"

def probe(source, dest, path, seconds=6.0, limit=64 * 1024 * 1024):
  """Bytes per second from the source (a file of path, else random data) through slurp to the destination, None if
  that does not work. The time to start the channels does not count."""
  token = secrets.token_hex(8)
  a = b = None
  try:
    a = source.popen(PROBE_SOURCE, token, path, limit)
    b = dest.popen(PROBE_DEST, token)
    _, _, first = read_marker(a.stdout, token)
    read_marker(b.stdout, token)
    moved, start = 0, time.monotonic()
    if first:
      b.stdin.write(first)
      moved += len(first)
    while time.monotonic() - start < seconds:
      chunk = a.stdout.read1(1 << 16)
      if not chunk:
        break
      b.stdin.write(chunk)
      moved += len(chunk)
    b.stdin.flush()
    elapsed = time.monotonic() - start
    return moved / elapsed if moved > (1 << 20) and elapsed > 0 else None
  except Exception as e:
    log.debug(f"relay probe: {e!r}")
    return None
  finally:
    for p in (a, b):
      if p is not None and p.poll() is None:
        p.kill()
      if p is not None:
        try:
          p.wait(5)
        except Exception:
          pass
