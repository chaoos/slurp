"""The connection window of a tab"""

import os
import re
import shlex
import subprocess
import time
from datetime import datetime

import humanize
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static


from ...cluster import remote # (remote.PING_*: the settings of the heartbeat, read where they are set)
from ...cluster.remote import connection_health, HEALTH_STYLES, in_background
from ..widgets import ClickTable, open_clicked_url, url_text
from ...links import share
from .links import LinkScreen, link_state, drive_state_text
from .dialogs import ConfirmScreen

def process_tree(pid):
  """pid and its descendants (e.g. the ssh -W of a ProxyJump, which holds the TCP connection)"""
  children = {}
  for entry in os.listdir("/proc"):
    if entry.isdigit():
      try:
        with open(f"/proc/{entry}/stat") as f:
          ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(entry))
      except (OSError, ValueError, IndexError):
        pass
  pids, todo = [], [pid]
  while todo:
    p = todo.pop()
    pids.append(p)
    todo += children.get(p, [])
  return pids

def tcp_connections(pids):
  """TCP sockets of the processes, from ss: [{"local", "peer", "pid", "bytes_sent", "bytes_received", "rtt", ...}]"""
  try:
    out = subprocess.run(["ss", "-tinpH"], capture_output=True, text=True, timeout=5).stdout
  except (OSError, subprocess.TimeoutExpired):
    return None
  conns, header = [], None
  for line in out.splitlines():
    if not line[:1].isspace():
      header = line
      continue
    if header is None:
      continue
    pid = next((p for p in pids if f"pid={p}," in header), None)
    if pid is None:
      continue
    fields = header.split()
    info = {"local": fields[3] if len(fields) > 4 else "?", "peer": fields[4] if len(fields) > 4 else "?", "pid": pid,
            "state": fields[0] if fields else "?"}
    for key, pattern in [("bytes_sent", r"bytes_sent:(\d+)"), ("bytes_received", r"bytes_received:(\d+)"),
                         ("rtt", r"\brtt:([\d.]+)"), ("retrans", r"retrans:\d+/(\d+)"), ("lastrcv", r"lastrcv:(\d+)"),
                         ("lastsnd", r"lastsnd:(\d+)")]:
      m = re.search(pattern, line)
      info[key] = float(m.group(1)) if m else None
    conns.append(info)
  return conns

def ssh_config(destination):
  """What OpenSSH makes of a destination (ssh -G: no connection is made)"""
  try:
    out = subprocess.run(["ssh", "-G", destination], capture_output=True, text=True, timeout=5).stdout
  except (OSError, subprocess.TimeoutExpired):
    return {}
  conf = {}
  for line in out.splitlines():
    key, _, value = line.partition(" ")
    conf.setdefault(key, value)
  return conf

def ago(seconds):
  if seconds is None:
    return "-"
  seconds = int(seconds)
  if seconds < 60:
    return f"{seconds} s"
  if seconds < 3600:
    return f"{seconds // 60} min {seconds % 60} s"
  return f"{seconds // 3600} h {seconds // 60 % 60} min"

class ConnectionScreen(ModalScreen):
  """The SSH session of a tab: state, the course of a (re)connect, traffic and diagnostics; reconnect/disconnect"""
  DEFAULT_CSS = """
  ConnectionScreen { align: center middle; }
  #conn-dialog { width: 110; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #conn-title { text-style: bold; height: 1; margin-bottom: 1; }
  #conn-dialog.-good { border: thick $success; }
  #conn-dialog.-warn { border: thick $warning; }
  #conn-dialog.-bad { border: thick $error; }
  #conn-body { height: 1fr; }
  #conn-links-title, #conn-drive-title { text-style: bold underline; height: auto; }
  #conn-drive { height: auto; }
  #conn-drive-buttons { height: auto; margin-bottom: 1; }
  #conn-drive-buttons Button { margin-right: 1; }
  #conn-links { height: auto; max-height: 14; margin-bottom: 1; }
  #conn-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #conn-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("R", "reconnect", "Reconnect")]

  def __init__(self, cluster):
    super().__init__()
    self.cluster = cluster
    self.config, self.tcp, self.tcp_time = None, None, 0.0

  def compose(self) -> ComposeResult:
    with Vertical(id="conn-dialog"):
      yield Static(id="conn-title")
      with VerticalScroll(id="conn-body"):
        if self.cluster.destination:
          yield Static("Drive on this machine", id="conn-drive-title")
          yield Static(id="conn-drive")
          with Horizontal(id="conn-drive-buttons"):
            yield Button("Mount…", id="conn-drive-mount", variant="primary", tooltip="choose the path and mount (Drive, F)", compact=True)
            yield Button("Unmount", id="conn-drive-unmount", compact=True)
            yield Button("Remount", id="conn-drive-remount", tooltip="unmount and mount again (e.g. when it does not answer)", compact=True)
            yield Button("Force unmount", id="conn-drive-force", variant="error",
                         tooltip="also when files are in use (lazy: it goes when they are closed)", compact=True)
        yield Static(id="conn-links-title")
        yield ClickTable(id="conn-links", cursor_type="row", zebra_stripes=True)
        yield Static(id="conn-info")
      with Horizontal(id="conn-buttons"):
        if self.cluster.destination:
          yield Button("Reconnect", id="conn-reconnect", variant="primary", tooltip="end the session and log in again (R)", compact=True)
          yield Button("Disconnect", id="conn-disconnect", variant="error", compact=True)
        yield Button("Close", id="conn-close", compact=True)

  def show_title(self):
    text = Text(f"Connection of the tab {self.cluster.name}" + (f" · ssh {self.cluster.destination}" if self.cluster.destination else ""))
    level, words = connection_health(self.cluster.connection if self.cluster.destination else None)
    text.append(f" · ● {words}", style=HEALTH_STYLES[level])
    self.query_one("#conn-title", Static).update(text)
    dialog = self.query_one("#conn-dialog")
    for other in HEALTH_STYLES:
      dialog.set_class(other == level, f"-{other}")

  def show_drive(self):
    """The login directory as a drive on this machine: state, details, the buttons that fit"""
    if not self.cluster.destination:
      return
    drive = self.app.drive_of(self.cluster)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    grid.add_row("State", drive_state_text(drive))
    grid.add_row("Path", drive.path + (" (read only)" if drive.read_only else ""))
    if drive.source:
      grid.add_row("Files", f"{drive.source}, through the session of this tab (sshfs"
                   + (f", {drive.server}" if drive.server else ", the cluster's sftp") + ")")
    if drive.proc is not None:
      grid.add_row("sshfs", f"pid {drive.proc.pid}, started {datetime.fromtimestamp(drive.started):%H:%M:%S}"
                   + (", reconnects by itself when the session is back" if drive.state == "broken" else ""))
    if drive.messages and drive.state in ("failed", "broken", "mounting"):
      grid.add_row("Messages", Text("\n".join(drive.messages[-3:]), style="dim"))
    self.query_one("#conn-drive", Static).update(grid)
    state = drive.state
    ours = drive.proc is not None
    self.query_one("#conn-drive-mount", Button).display = state in ("unmounted", "failed")
    self.query_one("#conn-drive-unmount", Button).display = state in ("mounted", "mounting", "broken", "reconnecting", "foreign")
    self.query_one("#conn-drive-remount", Button).display = ours and state in ("mounted", "broken")
    self.query_one("#conn-drive-force", Button).display = state in ("mounted", "broken", "foreign")

  @on(Button.Pressed, "#conn-drive-mount")
  def drive_mount_pressed(self, event):
    event.stop()
    self.app.action_cluster_drive()

  @on(Button.Pressed, "#conn-drive-unmount")
  def drive_unmount_pressed(self, event):
    event.stop()
    self.app.unmount_drive(self.cluster)

  @on(Button.Pressed, "#conn-drive-remount")
  def drive_remount_pressed(self, event):
    event.stop()
    self.app.remount_drive(self.cluster)

  @on(Button.Pressed, "#conn-drive-force")
  def drive_force_pressed(self, event):
    event.stop()
    drive = self.app.drive_of(self.cluster)
    def done(yes):
      if yes:
        self.app.unmount_drive(self.cluster, lazy=True)
    self.app.push_screen(ConfirmScreen("Force unmount", f"Unmount {drive.path} even if files are in use there? "
                                       "Programs using them get errors; it is gone when they close them.", "Force unmount"), done)

  links_shown = None

  def show_links(self):
    """The links of this tab (shells and drives for the user's own terminal) and their sessions: a click on a link
    shows it (commands, password, Revoke), on a session offers to disconnect it"""
    links = [l for l in self.app.link_manager.links if l.tab == self.cluster.name and not (l.finished and l.relay_state == "closed"
                                                                                          and time.time() - l.created > 3600)]
    title = self.query_one("#conn-links-title", Static)
    table = self.query_one("#conn-links", ClickTable)
    table.display = bool(links)
    title.update(Text("Links: shells and drives from your own terminal (click one)", style="bold underline") if links else
                 Text("Links: none. Shell → From your own terminal, or Drive, makes one.", style="dim"))
    if not links:
      return
    now = time.time()
    rows = []
    def health(level, words):
      return Text(f"{'●' if level else '○'} {words}", style=HEALTH_STYLES[level] if level else "dim")
    for link in links:
      relay, logins = link_state(link, now)
      what = f"{link.kind}: {link.tab}" if link.kind == "drive" else f"{link.kind}: {link.title}" # (a drive's folders: its sessions)
      rows.append((f"link|{link.id}", (Text.assemble(link.id, (" ◀ this view", "bold green") if link.cloudflare and self.app.this_view_link(link) else ""),
                                       health(*share.link_health(link)),
                                       what + (" (read only)" if link.read_only else ""), relay, logins,
                                       "the address opens it (no logins)" if link.cloudflare else
                                       f"{len(link.live_sessions)} open, {link.logins} logins" + (f", {link.failures} wrong" if link.failures else ""))))
      sessions = [s for s in link.sessions if s.kind]
      if link.kind == "drive":
        # one row per folder: its newest session; the ones before it are its reconnects (sshfs, the helper reconnect)
        groups = {}
        for s in sessions:
          groups.setdefault(s.path or f"?{s.id}", []).append(s) # (a folder not known yet: a row of its own)
        shown = [(group[-1], group[:-1]) for group in groups.values()]
      else:
        shown = [(s, []) for s in sessions]
      for s, before in shown:
        duration = int((s.ended or now) - s.started)
        here = s.ended is None and self.app.this_view(s)
        level, words = share.session_health(s)
        recent = [b for b in before[1:] + [s] if now - b.started < share.UNSTABLE] if before else []
        if s.ended is None and recent and level == "good":
          level, words = "warn", f"unstable: reconnected {len(recent)}× in {share.UNSTABLE // 60} min"
        state = f"{'open' if s.ended is None else 'ended'} · {duration // 60}:{duration % 60:02d}"
        rows.append((f"session|{link.id}|{s.id}", (
          Text.assemble(f"  └ {s.id}", (" ◀ this view", "bold green") if here else ""),
          health(level, words),
          Text.assemble(f"{s.path or '(folder not known yet)'} · {state}" if link.kind == "drive" else f"{s.kind} {state}",
                        (f" · reconnected {len(before)}×", "yellow") if before else ""),
          Text(f"{s.client or 'client ?'}" + (f", key {s.key[:20]}…" if s.key else ""), style="" if s.ended is None else "dim"),
          Text(f"{humanize.naturalsize(s.bytes_in)} in, {humanize.naturalsize(s.bytes_out)} out" + (f", {s.term} {s.size[0]}×{s.size[1]}" if s.term else ""),
               style="" if s.ended is None else "dim"),
          Text("click: disconnect", style="red") if s.ended is None else "")))
    signature = [(k, tuple(str(v) for v in values)) for k, values in rows]
    if signature == self.links_shown:
      return
    old_keys = [k for k, _ in (self.links_shown or [])]
    self.links_shown = signature
    if not table.columns:
      for label in ("Link", "Health", "What", "Relay / client", "Logins / traffic", "Sessions"):
        table.add_column(label, key=label)
    if old_keys == [k for k, _ in rows] and table.row_count == len(rows):
      # the same rows, new values (traffic, durations): update the cells, the view (scrolling, cursor) stays as it is
      columns = [c.key for c in table.ordered_columns]
      for key, values in rows:
        for column, value in zip(columns, values):
          table.update_cell(key, column, value, update_width=True)
      return
    # rows came or went: build anew, but keep where the table was scrolled to (it is wide: often scrolled sideways)
    x, y = table.scroll_x, table.scroll_y
    cursor = table.cursor_row
    table.clear()
    for key, values in rows:
      table.add_row(*values, key=key)
    if 0 <= cursor < table.row_count:
      table.move_cursor(row=cursor, scroll=False)
    table.call_after_refresh(table.scroll_to, x, y, animate=False)

  @on(DataTable.RowSelected, "#conn-links")
  def link_selected(self, event):
    event.stop()
    kind, link_id, *rest = event.row_key.value.split("|")
    link = next((l for l in self.app.link_manager.links if l.id == link_id), None)
    if link is None:
      return
    if kind == "link":
      self.app.push_screen(LinkScreen(link))
      return
    session = next((s for s in link.sessions if s.id == rest[0]), None)
    if session is None or session.ended is not None:
      return
    def done(yes):
      if yes:
        self.app.link_manager.disconnect(session)
    message = Text(f"End the {session.kind} of link {link.id} ({link.title}) from {session.client or 'a client'}?"
                   + (" A mounted drive fails until it is unmounted." if session.kind == "drive" else ""))
    if self.app.this_view(session):
      message.append("\n\nThis is the session of the browser you are using now: disconnecting it ends this very view. "
                     "It loses slurp until you log in through a web link again (this one, if it takes more logins) or use a terminal.", style="bold red")
    self.app.push_screen(ConfirmScreen(f"Disconnect session {session.id}", message, "Disconnect"), done)

  def on_mount(self):
    self.show_title()
    if self.cluster.destination:
      dest = self.cluster.destination
      in_background(self, lambda: ssh_config(dest), self.got_config, group="ssh-config", connection=None)
    self.set_interval(0.5, self.show)
    self.show()

  def got_config(self, config):
    self.config = config or {}
    self.show()

  def poll_tcp(self):
    """the TCP connection of the ssh process, from ss (every 2 s, in the background)"""
    c = self.cluster.connection
    proc = c.proc if c else None
    if proc is None or proc.poll() is not None or time.monotonic() - self.tcp_time < 2:
      return
    self.tcp_time = time.monotonic()
    pid = proc.pid
    def got(conns):
      self.tcp = conns
    in_background(self, lambda: tcp_connections(process_tree(pid)), got, group="tcp", connection=None)

  def status(self):
    """The course of the current (re)connect, in words"""
    cluster, c = self.cluster, self.cluster.connection
    if c is None or c.state == "new":
      return Text("not connected", style="dim")
    now = time.monotonic()
    if c.state == "ready":
      waiting = c.responding()
      if waiting is not None and waiting > remote.PING_SLOW:
        return Text(f"connected, but no answer to the heartbeat for {waiting:.0f} s "
                    f"(given up after {remote.PING_DEAD} s)", style="bold red")
      return Text(f"connected since {c.ready_at:%Y-%m-%d %H:%M:%S} ({ago((datetime.now() - c.ready_at).total_seconds())})",
                  style="bold green")
    if c.state == "connecting":
      t = cluster.login_terminal
      printed = t is not None and t.vt is not None and any(line.strip() for line in t.vt.display)
      text = Text(f"connecting for {now - c.connecting_since:.0f} s (attempt {max(cluster.attempts, 1)})", style="bold yellow")
      if printed:
        text.append(" · the login may wait for input: answer it in the pane on the right (close this window)",
                    style="yellow")
      return text.append_text(self.site_status()) if cluster.attempts > 1 else text
    if c.state == "closed":
      return Text("disconnected (closed) · Reconnect logs in again", style="bold")
    delay = self.app.RECONNECT_DELAYS[min(cluster.attempts, len(self.app.RECONNECT_DELAYS) - 1)]
    left = max(0, delay - (now - (cluster.broken_at or now)))
    text = Text(f"failed: {c.error or 'the session ended'}" + (f" (ssh exit code {c.exit_code})" if c.exit_code is not None else ""),
                style="bold red")
    if c.wanted:
      text.append(f" · next attempt in {left:.0f} s" if left >= 1 else " · reconnecting", style="red")
    return text.append_text(self.site_status())

  def site_status(self):
    """When the login fails: the cluster may be down; its site's status page (a link), if a rule pack knows it"""
    from ...checks.rulepacks import status_page
    url = status_page(self.cluster.name, self.cluster.destination or "")
    text = Text("\nThe cluster may be down or in maintenance: ", style="yellow")
    if url:
      text.append("check its status page ", style="yellow")
      text.append_text(url_text(url))
      text.append(" (a click opens it)", style="dim")
    else:
      text.append("check the status page of the site (its web pages, or the news of its support)", style="yellow")
    return text

  def on_click(self, event):
    if open_clicked_url(self.app, event): # (the status page in the window's text)
      event.stop()

  def show(self):
    if not self.is_mounted:
      return
    self.show_title()
    self.show_drive()
    self.show_links()
    cluster, c = self.cluster, self.cluster.connection
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    def section(title):
      grid.add_row("", "")
      grid.add_row(Text(title, style="bold underline"), "")
    if not cluster.destination:
      grid.add_row("Machine", f"local ({os.uname().nodename}), commands run directly, no SSH")
      self.query_one("#conn-info", Static).update(grid)
      return
    self.poll_tcp()
    grid.add_row("Status", self.status())
    if c is not None:
      grid.add_row("Logged in as", f"{c.user or '-'} on {c.hostname or '-'}" + (f" (home {c.home})" if c.home else ""))
      if c.groups:
        grid.add_row("Groups", ", ".join(c.groups))
      grid.add_row("Session", f"number {c.generation}" + (f", started {c.started_at:%H:%M:%S}" if c.started_at else "")
                   + (f", reconnect attempts {cluster.attempts}" if cluster.attempts > 1 else ""))
    conf = self.config
    if conf is None:
      grid.add_row("Destination", f"{cluster.destination} …")
    elif conf:
      target = f"{conf.get('user', '?')}@{conf.get('hostname', '?')}:{conf.get('port', '?')}"
      grid.add_row("Destination", f"{cluster.destination} → {target}")
      for key, label in [("proxyjump", "Proxy jump"), ("identityfile", "Identity file"),
                         ("serveraliveinterval", "Server alive"), ("connecttimeout", "Connect timeout")]:
        value = conf.get(key)
        if value and value != "none":
          if key == "serveraliveinterval":
            value = f"every {value} s, gives up after {conf.get('serveralivecountmax', '?')} missed"
          grid.add_row(label, value)
    if c is not None:
      section("Command channel")
      now = time.monotonic()
      grid.add_row("Traffic", f"{humanize.naturalsize(c.bytes_sent)} sent, {humanize.naturalsize(c.bytes_received)} received, "
                   f"{c.commands} commands (all sessions)")
      grid.add_row("Waiting", f"{len(c.pending)} commands" + (f", last answer {ago(now - c.last_answer)} ago" if c.last_answer else ""))
      if c.latencies:
        grid.add_row("Heartbeat", f"round trip {c.latencies[-1] * 1000:.0f} ms (average {sum(c.latencies) / len(c.latencies) * 1000:.0f} ms, "
                     f"max {max(c.latencies) * 1000:.0f} ms of {len(c.latencies)}), every {remote.PING_INTERVAL} s when idle")
      else:
        grid.add_row("Heartbeat", f"none yet (sent after {remote.PING_INTERVAL} s without an answer from the cluster)")
      section("SSH process")
      proc = c.proc
      if proc is None:
        grid.add_row("Process", "-")
      else:
        code = proc.poll()
        grid.add_row("Process", f"pid {proc.pid}, " + ("running" if code is None else f"ended with exit code {code}"))
      grid.add_row("Control socket", c.control_path)
      if c.argv:
        grid.add_row("Command", shlex.join(c.argv))
      if self.tcp is None:
        grid.add_row("TCP", "…" if proc is not None and proc.poll() is None else "-")
      elif not self.tcp:
        grid.add_row("TCP", "no TCP connection of the ssh process found")
      for conn in self.tcp or []:
        details = [f"{conn['local']} → {conn['peer']} ({conn['state']})"]
        if conn["bytes_sent"] is not None:
          details.append(f"{humanize.naturalsize(conn['bytes_sent'])} sent, {humanize.naturalsize(conn['bytes_received'] or 0)} received "
                         "(the whole connection, shells included)")
        if conn["rtt"] is not None:
          details.append(f"rtt {conn['rtt']:.1f} ms" + (f", {conn['retrans']:.0f} retransmissions" if conn["retrans"] else "")
                         + (f", last data received {conn['lastrcv'] / 1000:.1f} s ago" if conn["lastrcv"] is not None else ""))
        grid.add_row("TCP", "\n".join(details))
      t = cluster.login_terminal
      lines = [line.rstrip() for line in t.vt.display if line.strip()] if t is not None and t.vt is not None else []
      if lines:
        section("Login output")
        for line in lines[-10:]:
          grid.add_row("", Text(line))
      section("Events")
      for when, text in list(c.events)[-25:][::-1]:
        style = "red" if any(w in text for w in ("ended", "no answer", "failed")) else "green" if "connected as" in text else ""
        grid.add_row(f"{when:%H:%M:%S}", Text(text, style=style))
    self.query_one("#conn-info", Static).update(grid)
    self.shown = grid

  @on(Button.Pressed, "#conn-reconnect")
  def action_reconnect(self):
    """Log in again: the window closes and the login comes up in the pane, with the focus"""
    if self.cluster.destination:
      self.app.reconnect(self.cluster)
      self.dismiss()

  @on(Button.Pressed, "#conn-disconnect")
  def disconnect_pressed(self):
    self.app.disconnect(self.cluster)
    self.show()

  @on(Button.Pressed, "#conn-close")
  def action_close(self):
    self.dismiss()
