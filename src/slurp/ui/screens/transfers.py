"""Transfers: the top-level tab Transfers (its list, a subtab for each transfer shown), the wizard that sets one up
(source, destination, check, how, when, summary: Go), and a window that picks paths on any tab"""

import asyncio
import os
import time
from datetime import datetime

import humanize
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, ContentSwitcher, Input, Label, RadioButton, RadioSet, Select, Static

from ...cluster.listing import listing
from ...cluster.remote import HEALTH_STYLES, connection_health, in_background
from ...transfer import model as M, plan as P, schedule as S
from ...transfer.manager import client_side, site_of
from .. import icons
from ..widgets import ClickTable, TimePlot
from .base import SplitScreen
from .dialogs import ConfirmScreen
from .files import expand_path, home_of, with_places

STATE_STYLES = {"running": "bold green", "starting": "green", "queued": "cyan", "waiting": "yellow", "pausing": "yellow",
                "paused": "yellow", "scheduled": "cyan", "done": "green", "done-errors": "bold yellow", "failed": "bold red",
                "checking": "cyan", "new": "dim", "interrupted": "yellow"}
STATE_ICONS = {"running": "running", "starting": "running", "queued": "pending", "waiting": "pending", "pausing": "suspended",
               "paused": "suspended", "scheduled": "pending", "done": "completed", "done-errors": "completed",
               "failed": "failed", "new": "pending", "checking": "pending"}
ROUTE_SHORT = {"same-fs": "same filesystem", "direct-push": "direct", "direct-pull": "direct (pull)", "relay": "relay"}
RUNS_SHORT = {"slurp": "in slurp", "detached": "login node", "job": "transfer job"}

def state_text(t):
  return Text(icons.label(STATE_ICONS.get(t.state, ""), M.STATES.get(t.state, t.state)), style=STATE_STYLES.get(t.state, ""))

def bar(percent, width=12):
  """A bar of text: ███████░░░  70%"""
  if percent is None:
    return Text("")
  filled = int(round(width * max(0.0, min(100.0, percent)) / 100))
  return Text.assemble(("█" * filled, "green"), ("░" * (width - filled), "dim"), f" {percent:3.0f}%")

def human(n):
  return humanize.naturalsize(n or 0, binary=True)

def rate_text(rate):
  return f"{human(rate)}/s" if rate else ""

def route_text(t):
  return f"{ROUTE_SHORT.get(t.route, t.route)} · {RUNS_SHORT.get(t.runs_on, t.runs_on)}"

def dot(cluster):
  """● connected, ◌ connecting, ○ not connected (this machine: ⌂; a closed tab: ✕)"""
  if cluster is None:
    return Text("✕", style="red")
  if not cluster.destination:
    return Text("⌂", style="green")
  c = cluster.connection
  if c is not None and c.ready:
    return Text("●", style=HEALTH_STYLES[connection_health(c)[0]])
  return Text("◌" if c is not None and c.state == "connecting" else "○", style="dim")

RUNS_WHERE = {"slurp": "", "detached": "on the login node", "job": "as a transfer job"}

def flow_diagram(route, runs_on, src, dst, src_name, dst_name, job="", moving=None):
  """How the data flows, as boxes and arrows: the source, slurp (the relay between two clusters), the destination; on
  each arrow how (push, pull, relay, the same filesystem), under each box what runs there. moving: a frame number
  while it runs (a dot moves along the arrows)"""
  import textwrap
  from rich.cells import cell_len
  where = f"transfer job {job}" if job and runs_on == "job" else RUNS_WHERE.get(runs_on, "")
  def rsync(extra=""):
    return "rsync" + (f" {extra}" if extra else "") + (f" ({where})" if where else "")
  two_clusters = src is not None and dst is not None and src.destination and dst.destination
  if route == "relay" and two_clusters:
    boxes = [(src, src_name, "rsync (sends)"), (None, "slurp", "passes the bytes"), (dst, dst_name, "rsync server (receives)")]
    arrows = ["relay", "relay"]
  elif route == "relay":
    boxes = [(src, src_name, "rsync (sends)"), (dst, dst_name, "rsync server (receives)")]
    arrows = ["over slurp's SSH"]
  elif route == "direct-push":
    boxes = [(src, src_name, rsync(f"logs in to {dst_name}")), (dst, dst_name, "rsync server (receives)")]
    arrows = ["push"]
  elif route == "direct-pull":
    boxes = [(src, src_name, "rsync server (sends)"), (dst, dst_name, rsync(f"logs in to {src_name}"))]
    arrows = ["pull"]
  else: # (same-fs)
    boxes = [(src, src_name, rsync("folder to folder")), (dst, dst_name, "the same filesystem: no network")]
    arrows = ["same filesystem"]
  cells = []
  for cluster, name, caption in boxes:
    label = Text.assemble(dot(cluster), " ", (name, "bold")) if cluster is not None or name != "slurp" else Text("⇄ slurp", style="bold cyan")
    inner = cell_len(label.plain) + 2
    caption = textwrap.wrap(caption, max(22, inner + 2)) # (a few short lines: the boxes stay close)
    width = max([inner + 2, *map(cell_len, caption)])
    cells.append((label, inner, width, caption, cluster is None and name == "slurp"))
  rows = max(len(c[3]) for c in cells)
  arrow_widths = [max(cell_len(a) + 4, 10) for a in arrows]
  lines = [Text() for _ in range(3 + rows)]
  for i, (label, inner, width, caption, relay_box) in enumerate(cells):
    pad = width - inner - 2
    left, right = pad // 2, pad - pad // 2
    style = "cyan" if relay_box else ""
    lines[0].append(" " * left + "┌" + "─" * inner + "┐" + " " * right, style=style)
    lines[1].append(" " * left + "│ ", style=style)
    lines[1].append_text(label)
    lines[1].append(" │" + " " * right, style=style)
    lines[2].append(" " * left + "└" + "─" * inner + "┘" + " " * right, style=style)
    for r in range(rows):
      words = caption[r] if r < len(caption) else ""
      cpad = width - cell_len(words)
      lines[3 + r].append(" " * (cpad // 2) + words + " " * (cpad - cpad // 2), style="dim")
    if i < len(arrows):
      w = arrow_widths[i]
      name = arrows[i]
      npad = w - cell_len(name)
      lines[0].append(" " * (npad // 2) + name + " " * (npad - npad // 2), style="italic")
      shaft = ["─"] * (w - 3)
      if moving is not None and shaft:
        shaft[moving % len(shaft)] = "●"
      lines[1].append(" " + "".join(shaft) + "▶ ", style="bold green" if moving is not None else "")
      for line in lines[2:]:
        line.append(" " * w)
  for line in lines:
    line.rstrip()
  return Text("\n").join(lines)

def if_slurp_closes(t, client_name="", host="", pack=None):
  """What happens to a transfer when slurp ends (the details and the wizard say it)"""
  pack = pack or {}
  if t.runs_on == "slurp":
    from ...links import holder
    terminal = " A closed terminal does not matter: slurp keeps running in its session holder." if holder.under_holder() else ""
    return ("It runs through slurp: when slurp quits, the run stops and shows paused (slurp ended); Continue at the "
            "next start picks it up where it stopped." + terminal)
  if t.runs_on == "detached":
    limits = f" Note: {pack['login_node_limits']}" if pack.get("login_node_limits") else (
      " Some sites end long processes on login nodes; a transfer job is safer there.")
    return (f"It runs on {client_name}{' (' + host + ')' if host else ''}, detached from slurp: it goes on when slurp "
            "ends, and slurp shows its progress again at the next start." + limits)
  resubmit = (" When its time limit ends, slurp submits the next job once it sees that (slurp must run then; "
              "else at its next start)." if t.options.get("resubmit") else
              " When its time limit ends, the transfer stops (Continue submits a new job).")
  return f"It runs as a Slurm job on {client_name}: it goes on when slurp ends, and slurp shows it again at the next start." + resubmit

class PickPathsScreen(ModalScreen):
  """Pick paths on a tab: a directory listing (Enter or a click: into a folder), Space marks entries (several:
  multi), Choose. Dismissed with [paths] (the marked ones, or the folder shown), or None"""
  DEFAULT_CSS = """
  PickPathsScreen { align: center middle; }
  #pp-dialog { width: 110; max-width: 96%; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #pp-title { text-style: bold; height: auto; }
  #pp-bar { height: auto; margin-top: 1; }
  #pp-bar Button { margin-right: 1; }
  #pp-path { width: 1fr; }
  #pp-places { height: auto; margin-top: 1; }
  #pp-places Button { margin-right: 1; }
  #pp-list { height: 1fr; margin-top: 1; }
  #pp-status { height: auto; color: $text-muted; }
  #pp-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #pp-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("space", "mark", "Mark"), Binding("backspace", "up", "Up", show=False)]

  def __init__(self, cluster, start="", multi=True, title=""):
    super().__init__()
    self.cluster, self.multi = cluster, multi
    self.path = start or home_of(cluster) or "/"
    self.entries, self.marked = [], set()
    self.title_text = title or (f"Choose what to copy from {cluster.name}" if multi else f"Choose the destination folder on {cluster.name}")

  def compose(self) -> ComposeResult:
    with Vertical(id="pp-dialog"):
      yield Static(self.title_text, id="pp-title", markup=False)
      with Horizontal(id="pp-bar"):
        yield Button("↑ Up", id="pp-up", compact=True, tooltip="the folder above (Backspace)")
        yield Input(self.path, id="pp-path", tooltip="a folder: Enter goes there (~: your home)")
      yield Horizontal(id="pp-places")
      yield ClickTable(id="pp-list", cursor_type="row", zebra_stripes=True)
      yield Static(id="pp-status")
      with Horizontal(id="pp-buttons"):
        yield Button("Cancel", id="pp-cancel", compact=True)
        if self.multi:
          yield Button("Choose the marked", id="pp-marked", compact=True, disabled=True)
        yield Button("Choose this folder", id="pp-here", variant="primary", compact=True)

  def on_mount(self):
    table = self.query_one("#pp-list", ClickTable)
    for key, label in (("mark", " "), ("name", "Name"), ("size", "Size"), ("mtime", "Modified")):
      table.add_column(label, key=key)
    with_places(self, self.cluster, self.show_places)
    self.go(self.path)
    table.focus()

  def show_places(self, places):
    bar_ = self.query_one("#pp-places", Horizontal)
    bar_.remove_children()
    buttons = []
    for label, path in places:
      button = Button(label, classes="pp-place", compact=True, tooltip=path)
      button.place = path
      buttons.append(button)
    bar_.mount(*buttons)

  def go(self, path):
    self.query_one("#pp-status", Static).update(Text(f"listing {path} …", style="dim"))
    def work():
      try:
        return path, listing(path, 20000)[1], ""
      except OSError as e:
        return None, None, str(e)
    def listed(result):
      if result is None:
        return
      where, entries, error = result
      if where is None:
        self.query_one("#pp-status", Static).update(Text(error, style="red"))
        return
      self.path, self.entries, self.marked = where, entries, set()
      self.query_one("#pp-path", Input).value = where
      self.populate()
    in_background(self, work, listed, group="pp-list", name="listing the folder",
                  connection=self.cluster.connection if self.cluster.destination else None)

  def populate(self):
    table = self.query_one("#pp-list", ClickTable)
    table.clear()
    if self.path != "/":
      table.add_row("", Text("..", style="bold"), "", "", key="..")
    for e in sorted(self.entries, key=lambda e: (not e["dir"], e["name"].lower())):
      table.add_row("✓" if e["name"] in self.marked else "", Text(e["name"] + ("/" if e["dir"] else ""), style="bold blue" if e["dir"] else ""),
                    "" if e["dir"] else human(e["size"]), datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M"), key=e["name"])
    hint = "Space marks, Enter: into a folder" if self.multi else "Enter: into a folder"
    self.query_one("#pp-status", Static).update(Text(f"{self.path} · {len(self.marked)} marked · {hint}" if self.multi else f"{self.path} · {hint}", style="dim"))
    for b in self.query("#pp-marked").results(Button):
      b.disabled = not self.marked

  def full(self, name):
    return os.path.join(self.path, name) if self.path != "/" else "/" + name

  @on(ClickTable.RowSelected, "#pp-list")
  def selected(self, event):
    event.stop()
    name = event.row_key.value
    if name == "..":
      self.action_up()
      return
    entry = next((e for e in self.entries if e["name"] == name), None)
    if entry and entry["dir"]:
      self.go(self.full(name))
    elif entry and self.multi:
      self.toggle(name)

  def toggle(self, name):
    if name in ("..", None):
      return
    self.marked ^= {name}
    table = self.query_one("#pp-list", ClickTable)
    row = table.cursor_row
    self.populate()
    table.move_cursor(row=min(row + 1, table.row_count - 1))

  def action_mark(self):
    if not self.multi:
      return
    table = self.query_one("#pp-list", ClickTable)
    if table.row_count:
      self.toggle(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)

  def action_up(self):
    if self.path != "/":
      self.go(os.path.dirname(self.path.rstrip("/")) or "/")

  @on(Input.Submitted, "#pp-path")
  def typed(self, event):
    event.stop()
    self.go(expand_path(event.value, home_of(self.cluster) or "/"))

  @on(Button.Pressed)
  def pressed(self, event):
    bid = event.button.id or ""
    if event.button.has_class("pp-place"):
      event.stop()
      self.go(event.button.place)
    elif bid == "pp-up":
      event.stop()
      self.action_up()
    elif bid == "pp-cancel":
      event.stop()
      self.dismiss(None)
    elif bid == "pp-marked":
      event.stop()
      self.dismiss([self.full(n) for n in sorted(self.marked)])
    elif bid == "pp-here":
      event.stop()
      self.dismiss([self.path])

  def action_cancel(self):
    self.dismiss(None)

def select(radio_set, button):
  """Press one button of a radio set from code: the others off, and the set knows which one is pressed. (Setting its
  value alone is not enough while a RadioSet.Changed is handled: the set then hears nothing of it, keeps the button it
  had, and two stay pressed.)"""
  for b in radio_set.query(RadioButton).results(RadioButton):
    if b is not button and b.value:
      b.value = False
  button.value = True
  radio_set._pressed_button = button

STEP_STATE = {"running": ("…", "yellow"), "ok": ("✓", "green"), "bad": ("✗", "red"), "warn": ("!", "yellow"), "info": ("ⓘ", "dim")}
STEP_ORDER = ["facts", "paths", "same-fs", "remaining", "relay", "direct-push", "direct-pull"]

def plan_text(plan, spinner=""):
  """The steps of a check, as they came"""
  text = Text()
  for name in [n for n in STEP_ORDER if n in plan.get("steps", {})]:
    step = plan["steps"][name]
    mark, style = STEP_STATE.get(step["state"], ("·", ""))
    if step["state"] == "running" and spinner:
      mark = spinner
    text.append(f" {mark} ", style=style)
    text.append(step["text"] + "\n")
  if plan.get("error"):
    text.append(f" ✗ {plan['error']}\n", style="red")
  for w in plan.get("warnings", []):
    text.append(f" ⚠ {w}\n", style="yellow")
  return text

def routes_text(plan):
  """The routes: possible or not, speed, estimate"""
  text = Text()
  routes = plan.get("routes", {})
  for route in ("same-fs", "direct-push", "direct-pull", "relay"):
    info = routes.get(route)
    if info is None:
      continue
    rec = route == plan.get("recommended")
    est = info.get("estimate")
    line = f" {'●' if rec else '○'} {M.ROUTES[route]:28}"
    if info.get("ok"):
      text.append(line, style="bold" if rec else "")
      text.append(f" ✓ {rate_text(info.get('rate')) or 'speed ?':>12}  ")
      text.append(f"{P.duration(est[0])} – {P.duration(est[1])}" if est else "no estimate", style="bold" if rec else "")
      text.append("  (recommended)\n" if rec else "\n", style="green")
    else:
      text.append(line, style="dim")
      text.append(f" ✗ {info.get('why', '')[:90]}" + ("  (the temporary key could)" if info.get("keyable") else "") + "\n", style="dim")
  return text

def globus_text(plan):
  g = plan.get("globus")
  if not g:
    return Text("")
  return Text("ⓘ " + g["text"], style="bold cyan" if g.get("strong") else "cyan")

class TransferWizard(ModalScreen):
  """Set up a transfer: 1 source, 2 destination, 3 check, 4 how, 5 when, 6 summary. Dismissed with
  {"transfer": Transfer, "go": bool} or None. editing: a transfer whose settings it changes (its id stays)"""
  STEPS = ["Source", "Destination", "Check", "How", "When", "Summary"]
  DEFAULT_CSS = """
  TransferWizard { align: center middle; }
  #tw-dialog { width: 120; max-width: 98%; height: 94%; border: thick $accent; background: $surface; padding: 0 2; }
  #tw-title { text-style: bold; margin: 1 0 0 0; height: auto; }
  #tw-steps { height: 1; margin-bottom: 1; }
  #tw-steps Button { margin-right: 1; }
  #tw-steps Button:disabled { color: $text-disabled; }
  #tw-switch { height: 1fr; }
  #tw-switch > VerticalScroll { height: 1fr; }
  .tw-row { height: auto; margin-top: 1; }
  .tw-row Label { padding: 0 1 0 0; width: auto; }
  .tw-row Button { margin-right: 1; }
  .tw-note { color: $text-muted; height: auto; }
  .tw-warn { color: $warning; height: auto; }
  #tw-src-tab, #tw-dst-tab { width: 40; }
  #tw-src-paths { height: auto; max-height: 12; border: round $panel; padding: 0 1; }
  #tw-src-path, #tw-dst-path { width: 1fr; }
  #tw-check-steps, #tw-routes, #tw-globus { height: auto; }
  #tw-routes, #tw-globus { margin-top: 1; }
  #tw-route, #tw-runs-on, #tw-when { height: auto; width: 1fr; }
  #tw-job { height: auto; border: round $panel; padding: 0 1; margin-top: 1; }
  #tw-partition, #tw-time, #tw-account { width: 24; margin-right: 2; }
  #tw-bwlimit, #tw-every, #tw-daily, #tw-job-id { width: 16; margin-right: 2; }
  #tw-at { width: 24; margin-right: 2; }
  #tw-key-warning { height: auto; color: $warning; border: round $warning; padding: 0 1; display: none; }
  #tw-name { width: 1fr; }
  #tw-summary { height: auto; }
  #tw-flow, #tw-summary-flow { height: auto; margin: 1 0; }
  #tw-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #tw-buttons Button { margin-left: 1; }
  #tw-missed { width: 40; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, app, editing=None, source_cluster=None):
    super().__init__()
    self.editing = editing
    self.step = 0
    self.plan = dict(editing.plan) if editing else {}
    self.checking = None # (the stop event of a check under way)
    self.checked_for = None # (what the plan was made for: source, paths, destination)
    clusters = app.clusters
    self.tabs = list(clusters)
    if editing:
      self.src, self.dst = app.transfers.ends(editing)
      self.paths, self.dest_path = list(editing.source.get("paths", [])), editing.dest.get("path", "")
    else:
      self.src = source_cluster if source_cluster in clusters else (clusters[0] if clusters else None)
      self.dst = next((c for c in clusters if c is not self.src), self.src)
      self.paths, self.dest_path = [], ""
    self.tid = editing.id if editing else M.new_id()
    self.rebuilding = asyncio.Lock() # (the choices of step 4 are rebuilt one at a time)

  # ---- the window
  def compose(self) -> ComposeResult:
    with Vertical(id="tw-dialog"):
      yield Static("Edit the transfer" if self.editing else "Transfer data", id="tw-title")
      with Horizontal(id="tw-steps"):
        for i, name in enumerate(self.STEPS):
          yield Button(f"{i + 1} {name}", id=f"tw-goto-{i}", compact=True)
      with ContentSwitcher(id="tw-switch", initial="tw-step-0"):
        with VerticalScroll(id="tw-step-0"):
          yield Static("What to copy: a tab, and one or more files or folders there. Each is copied into the destination "
                       "folder under its own name.", classes="tw-note")
          with Horizontal(classes="tw-row"):
            yield Label("From")
            yield Select(self.tab_options(), id="tw-src-tab", allow_blank=False, value=self.tab_value(self.src), compact=True)
          yield Static(id="tw-src-state", classes="tw-note")
          yield Static(id="tw-src-paths")
          with Horizontal(classes="tw-row"):
            yield Input(placeholder="a path (Enter adds it; ~ and relative paths are in your home)", id="tw-src-path")
            yield Button("Browse…", id="tw-src-browse", compact=True)
            yield Button("Remove the last", id="tw-src-remove", compact=True)
        with VerticalScroll(id="tw-step-1"):
          yield Static("Where to: a tab, and the folder there the sources are copied into (created if it is not there).", classes="tw-note")
          with Horizontal(classes="tw-row"):
            yield Label("To")
            yield Select(self.tab_options(), id="tw-dst-tab", allow_blank=False, value=self.tab_value(self.dst), compact=True)
          yield Static(id="tw-dst-state", classes="tw-note")
          with Horizontal(classes="tw-row"):
            yield Input(self.dest_path, placeholder="the destination folder", id="tw-dst-path")
            yield Button("Browse…", id="tw-dst-browse", compact=True)
          yield Horizontal(id="tw-dst-places", classes="tw-row")
          yield Static(id="tw-dst-preview", classes="tw-note")
        with VerticalScroll(id="tw-step-2"):
          yield Static("slurp looks at both sides (it changes nothing): what remains to copy, the room on the destination, "
                       "which routes work and how fast.", classes="tw-note")
          yield Static(id="tw-check-steps")
          yield Static(id="tw-routes")
          yield Static(id="tw-globus")
          with Horizontal(classes="tw-row"):
            yield Button(icons.label("reload", "Check again"), id="tw-recheck", compact=True)
        with VerticalScroll(id="tw-step-3"):
          yield Label("Route")
          yield RadioSet(id="tw-route")
          yield Static(id="tw-flow")
          yield Static(id="tw-route-note", classes="tw-note")
          yield Label("Runs")
          yield RadioSet(id="tw-runs-on")
          with Vertical(id="tw-job"):
            yield Static("The transfer job", classes="tw-note")
            with Horizontal(classes="tw-row"):
              yield Label("partition")
              yield Input(id="tw-partition", compact=True)
              yield Label("time limit")
              yield Input("24:00:00", id="tw-time", compact=True)
              yield Label("account")
              yield Input(id="tw-account", placeholder="(default)", compact=True)
            yield Checkbox("submit the next job when the time limit ends", value=True, id="tw-resubmit", compact=True)
          with Horizontal(classes="tw-row"):
            yield Checkbox("checksums (slower; finds changes with the same size and time)", id="tw-checksum", compact=True)
          with Horizontal(classes="tw-row"):
            yield Checkbox("compress (helps on the relay over slow links)", id="tw-compress", compact=True)
          with Horizontal(classes="tw-row"):
            yield Label("limit to")
            yield Input(placeholder="MB/s (none)", id="tw-bwlimit", compact=True)
          with Horizontal(classes="tw-row"):
            yield Checkbox("Install a temporary key", id="tw-key", compact=True)
          yield Static(id="tw-key-warning")
        with VerticalScroll(id="tw-step-4"):
          with RadioSet(id="tw-when"):
            yield RadioButton("now", value=True, id="tw-when-now")
            yield RadioButton("once, at a time", id="tw-when-at")
            yield RadioButton("repeatedly, every …", id="tw-when-every")
            yield RadioButton("daily, at …", id="tw-when-daily")
            yield RadioButton("after a job ends", id="tw-when-after")
            yield RadioButton("never by itself: only when I click Run now", id="tw-when-never")
          with Horizontal(classes="tw-row"):
            yield Label("at")
            yield Input(placeholder="14:00, 2026-11-20 14:00, +2h", id="tw-at", compact=True)
            yield Label("every")
            yield Input("6h", placeholder="6h, 1d, 30m", id="tw-every", compact=True)
            yield Label("daily at")
            yield Input("02:00", id="tw-daily", compact=True)
            yield Label("job")
            yield Input(placeholder="job ID", id="tw-job-id", compact=True)
          yield Checkbox("log in to the tabs by itself when it is time (a login that needs you waits, and says so)",
                         value=True, id="tw-login", compact=True)
          with Horizontal(classes="tw-row"):
            yield Label("a run that was missed")
            yield Select([("runs once, as soon as it can", "run"), ("is skipped", "skip")], value="run", allow_blank=False,
                         id="tw-missed", compact=True)
          yield Static("A repeated transfer copies only what changed since the run before: it keeps the destination in "
                       "step with the source (nothing is deleted there).", classes="tw-note")
          yield Static(id="tw-when-note", classes="tw-warn")
        with VerticalScroll(id="tw-step-5"):
          with Horizontal(classes="tw-row"):
            yield Label("Name")
            yield Input(id="tw-name")
          yield Static(id="tw-summary-flow")
          yield Static(id="tw-summary")
      with Horizontal(id="tw-buttons"):
        yield Button("Cancel", id="tw-cancel", compact=True)
        yield Button("← Back", id="tw-back", compact=True)
        yield Button("Next →", id="tw-next", variant="primary", compact=True)
        yield Button("Save without starting", id="tw-save", compact=True)
        yield Button("Go", id="tw-go", variant="success", compact=True)

  def tab_options(self):
    return [(f"{c.name}{'' if self.ready(c) else ' (not connected)'}", str(i)) for i, c in enumerate(self.tabs)]

  def tab_value(self, cluster):
    return str(self.tabs.index(cluster)) if cluster in self.tabs else (str(0) if self.tabs else Select.BLANK)

  def on_mount(self):
    if self.editing:
      self.prefill(self.editing)
      if self.plan.get("done"): # (its check is for its sources and destination: the steps after it can be gone to)
        self.checked_for = (id(self.src), tuple(self.paths), id(self.dst), self.dest_path)
        self.call_later(self.fill_how)
    self.show_step()
    self.refresh_sources()
    self.refresh_dest()
    self.set_interval(0.5, self.tick)

  def prefill(self, t):
    q = self.query_one
    q("#tw-name", Input).value = t.name
    o = t.options
    q("#tw-checksum", Checkbox).value = bool(o.get("checksum"))
    q("#tw-compress", Checkbox).value = bool(o.get("compress"))
    q("#tw-bwlimit", Input).value = str(o.get("bwlimit") or "")
    q("#tw-key", Checkbox).value = bool(o.get("temp_key"))
    q("#tw-partition", Input).value = o.get("partition", "")
    q("#tw-time", Input).value = o.get("time", "24:00:00")
    q("#tw-account", Input).value = o.get("account", "")
    q("#tw-resubmit", Checkbox).value = o.get("resubmit", True)
    s = t.schedule or {}
    which = {"now": "now", "at": "at", "every": "every", "daily": "daily", "after_job": "after", "never": "never"}.get(s.get("kind", "now"), "now")
    q(f"#tw-when-{which}", RadioButton).value = True
    if s.get("at"):
      q("#tw-at", Input).value = datetime.fromtimestamp(s["at"]).strftime("%Y-%m-%d %H:%M")
    q("#tw-every", Input).value = s.get("every", "6h")
    q("#tw-daily", Input).value = s.get("daily", "02:00")
    q("#tw-job-id", Input).value = str(s.get("job", ""))
    q("#tw-login", Checkbox).value = s.get("login", True)
    q("#tw-missed", Select).value = s.get("missed", "run")

  def tick(self):
    if self.step == 2 and self.checking is not None:
      self.show_plan()
    if self.step in (0, 1):
      self.refresh_states()

  # ---- the steps
  def show_step(self):
    self.query_one("#tw-switch", ContentSwitcher).current = f"tw-step-{self.step}"
    q = self.query_one
    q("#tw-back", Button).display = self.step > 0
    q("#tw-next", Button).display = self.step < len(self.STEPS) - 1
    last = self.step == len(self.STEPS) - 1
    never = self.when_kind() == "never"
    q("#tw-go", Button).display = last
    q("#tw-save", Button).display = last and not never # (Add saves it already)
    go = "Save" if never and self.editing else "Add" if never else "Go"
    if str(q("#tw-go", Button).label) != go:
      q("#tw-go", Button).label = go
    q("#tw-go", Button).tooltip = ("add it to the transfers; it runs when you click Run now" if never else
                                   "start it now, or by its schedule")
    if self.step == 2:
      self.start_check()
    if self.step == 3:
      self.call_later(self.fill_how)
    if self.step == 4:
      self.when_changed()
    if self.step == 5:
      self.fill_summary()
    self.update_next()

  def reachable(self, step):
    """A step can be gone to when every step before it is complete (editing: all of them, at once)"""
    return step == self.step or all(self.step_ok(i)[0] for i in range(step))

  def update_steps(self):
    """The steps at the top: the one shown, those that can be gone to (a click), the others not yet"""
    for i in range(len(self.STEPS)):
      b = self.query_one(f"#tw-goto-{i}", Button)
      b.variant = "primary" if i == self.step else "default"
      b.disabled = not self.reachable(i)
      b.tooltip = None if i == self.step else ("go to this step" if not b.disabled else
                                                f"first: {next(why for j in range(i) for ok, why in [self.step_ok(j)] if not ok) or 'the steps before'}")

  @on(Button.Pressed, "#tw-steps Button")
  async def goto_pressed(self, event):
    event.stop()
    step = int(event.button.id.rsplit("-", 1)[1])
    if step == self.step or not self.reachable(step):
      return
    if step > 3 and not self.query("#tw-route RadioButton"): # (its choices are needed by the steps after it)
      await self.fill_how()
    self.step = step
    self.show_step()

  def update_next(self):
    if not self.is_mounted:
      return
    self.update_steps()
    ok, why = self.step_ok()
    nxt = self.query_one("#tw-next", Button)
    nxt.disabled = not ok
    nxt.tooltip = why or None
    self.query_one("#tw-go", Button).disabled = self.step == 5 and not (self.how_ok()[0] and self.when_ok()[0])

  def plan_current(self):
    """Is the plan of the check for the sources and the destination chosen now?"""
    return self.checked_for == (id(self.src), tuple(self.paths), id(self.dst), self.dest_path)

  def step_ok(self, step=None):
    step = self.step if step is None else step
    if step == 0:
      if self.src is None:
        return False, "no tab"
      if not self.ready(self.src):
        return False, f"connect {self.src.name} first"
      return bool(self.paths), "" if self.paths else "add what to copy"
    if step == 1:
      if self.dst is None or not self.ready(self.dst):
        return False, f"connect {self.dst.name if self.dst else 'the tab'} first"
      return bool(self.dest_path.strip()), "" if self.dest_path.strip() else "the destination folder"
    if step == 2:
      plan = self.plan
      if not plan.get("done"):
        return False, "the check is not done"
      if step != self.step and not self.plan_current():
        return False, "check again (the source or the destination changed)"
      if plan.get("steps", {}).get("paths", {}).get("state") == "bad" or plan.get("steps", {}).get("facts", {}).get("state") == "bad":
        return False, "fix that first (Back)"
      usable = [r for r, i in plan.get("routes", {}).items() if i.get("ok") or i.get("keyable")]
      return bool(usable), "" if usable else "no route works"
    if step == 3:
      return self.how_ok()
    if step == 4:
      return self.when_ok()
    return True, ""

  @staticmethod
  def ready(cluster):
    return cluster is not None and (not cluster.destination or (cluster.connection is not None and cluster.connection.ready))

  @on(Button.Pressed, "#tw-next")
  def next_pressed(self, event):
    event.stop()
    if self.step_ok()[0] and self.step < len(self.STEPS) - 1:
      self.step += 1
      self.show_step()

  @on(Button.Pressed, "#tw-back")
  def back_pressed(self, event):
    event.stop()
    if self.step > 0:
      self.step -= 1
      self.show_step()

  @on(Button.Pressed, "#tw-cancel")
  def cancel_pressed(self, event):
    event.stop()
    self.action_cancel()

  def action_cancel(self):
    if self.checking is not None:
      self.checking.set()
    self.dismiss(None)

  # ---- 1: the source
  @on(Select.Changed, "#tw-src-tab")
  def src_changed(self, event):
    event.stop()
    cluster = self.tabs[int(event.value)] if event.value not in (None, Select.BLANK) else None
    if cluster is not self.src:
      self.src, self.paths = cluster, []
      self.refresh_sources()

  def refresh_states(self):
    if not self.is_mounted:
      return
    for sel, cluster in (("#tw-src-state", self.src), ("#tw-dst-state", self.dst)):
      if cluster is None:
        continue
      self.query_one(sel, Static).update(Text.assemble(dot(cluster), " ", (
        "connected" if self.ready(cluster) else "not connected: connect it first (its tab's overview: Connect)")))
    self.update_next()

  def refresh_sources(self):
    text = Text()
    for p in self.paths:
      text.append(f"  {p}\n")
    self.query_one("#tw-src-paths", Static).update(text if self.paths else Text("  nothing yet: Browse… or type a path", style="dim"))
    self.refresh_states()
    self.refresh_preview()

  @on(Input.Submitted, "#tw-src-path")
  def src_typed(self, event):
    event.stop()
    if event.value.strip() and self.src is not None:
      path = expand_path(event.value, home_of(self.src) or "/")
      if path not in self.paths:
        self.paths.append(path)
      event.input.value = ""
      self.refresh_sources()

  @on(Button.Pressed, "#tw-src-browse")
  def src_browse(self, event):
    event.stop()
    if not self.ready(self.src):
      self.app.notify(f"connect {self.src.name if self.src else 'the tab'} first", severity="warning")
      return
    def chosen(paths):
      for p in paths or []:
        if p not in self.paths:
          self.paths.append(p)
      self.refresh_sources()
    self.app.push_screen(PickPathsScreen(self.src, os.path.dirname(self.paths[-1]) if self.paths else "", multi=True), chosen)

  @on(Button.Pressed, "#tw-src-remove")
  def src_remove(self, event):
    event.stop()
    if self.paths:
      self.paths.pop()
      self.refresh_sources()

  # ---- 2: the destination
  @on(Select.Changed, "#tw-dst-tab")
  def dst_changed(self, event):
    event.stop()
    cluster = self.tabs[int(event.value)] if event.value not in (None, Select.BLANK) else None
    if cluster is not self.dst:
      self.dst = cluster
      self.refresh_dest()

  def refresh_dest(self):
    self.refresh_states()
    places_bar = self.query_one("#tw-dst-places", Horizontal)
    places_bar.remove_children()
    if self.ready(self.dst):
      def places(found):
        places_bar.remove_children()
        buttons = []
        for label, path in found:
          b = Button(label, classes="tw-place", compact=True, tooltip=path)
          b.place = path
          buttons.append(b)
        places_bar.mount(*buttons)
      with_places(self, self.dst, places)
    self.refresh_preview()

  @on(Input.Changed, "#tw-dst-path")
  def dst_typed(self, event):
    event.stop()
    self.dest_path = expand_path(event.value, home_of(self.dst) or "/") if event.value.strip() and self.dst else ""
    self.refresh_preview()
    self.update_next()

  @on(Button.Pressed, "#tw-dst-browse")
  def dst_browse(self, event):
    event.stop()
    if not self.ready(self.dst):
      self.app.notify(f"connect {self.dst.name if self.dst else 'the tab'} first", severity="warning")
      return
    self.app.push_screen(PickPathsScreen(self.dst, self.dest_path, multi=False),
                         lambda paths: self.set_dest(paths[0]) if paths else None)

  def set_dest(self, path):
    self.query_one("#tw-dst-path", Input).value = path

  def refresh_preview(self):
    if not self.is_mounted:
      return
    preview = self.query_one("#tw-dst-preview", Static)
    if not self.paths or not self.dest_path or self.dst is None:
      preview.update("")
      return
    lines = [f"  {os.path.basename(p.rstrip('/'))}  →  {self.dst.name}:{os.path.join(self.dest_path, os.path.basename(p.rstrip('/')))}"
             for p in self.paths[:6]]
    preview.update(Text("\n".join(["What goes where:", *lines, *(["  …"] if len(self.paths) > 6 else [])])))

  # ---- 3: the check
  def draft(self):
    """A transfer of what the wizard says so far (its id stays)"""
    base = self.editing
    t = M.Transfer(self.tid, base.name if base else M.default_name(self.paths, M.tab_ref(self.src), M.tab_ref(self.dst)),
                   {"tab": M.tab_ref(self.src), "paths": list(self.paths)}, {"tab": M.tab_ref(self.dst), "path": self.dest_path})
    if base:
      for key in ("runs", "log", "created", "progress", "state", "message", "errors"):
        setattr(t, key, getattr(base, key))
    return t

  def start_check(self, force=False):
    key = (id(self.src), tuple(self.paths), id(self.dst), self.dest_path)
    if self.checking is not None or (key == self.checked_for and self.plan.get("done") and not force):
      self.show_plan()
      return
    self.checked_for = key
    self.plan = {"steps": {}, "routes": {}}
    def report(plan):
      if self.checking is not None:
        self.plan = plan
    def done(plan):
      self.plan, self.checking = plan, None
      self.show_plan()
    self.checking = self.app.transfers.check(self.draft(), report, done)
    self.show_plan()

  def show_plan(self):
    if not self.is_mounted:
      return
    spinner = self.app.spinner() if self.checking is not None else ""
    steps = plan_text(self.plan, spinner)
    if self.checking is not None and not self.plan.get("steps"):
      steps = Text(f" {spinner} starting the check …", style="yellow")
    self.query_one("#tw-check-steps", Static).update(steps)
    routes = routes_text(self.plan) if self.plan.get("done") else Text("")
    if self.plan.get("done") and self.plan.get("remaining_bytes") == 0:
      routes.append("\n Nothing to copy: the destination has all of it already (a run would only check).", style="green")
    self.query_one("#tw-routes", Static).update(routes)
    self.query_one("#tw-globus", Static).update(globus_text(self.plan))
    self.update_next()

  @on(Button.Pressed, "#tw-recheck")
  def recheck(self, event):
    event.stop()
    self.start_check(force=True)

  # ---- 4: how
  async def fill_how(self):
    async with self.rebuilding:
      await self.fill_routes()
    await self.route_changed()

  async def fill_routes(self):
    routes = self.plan.get("routes", {})
    current = self.chosen_route() or (self.editing.route if self.editing else None) or self.plan.get("recommended")
    rs = self.query_one("#tw-route", RadioSet)
    await rs.remove_children()
    buttons = []
    for route in ("same-fs", "direct-push", "direct-pull", "relay"):
      info = routes.get(route)
      if info is None:
        continue
      usable = bool(info.get("ok") or info.get("keyable"))
      est = info.get("estimate")
      words = M.ROUTES[route] + (f"  ({P.duration(est[0])} – {P.duration(est[1])})" if est else "")
      if not info.get("ok") and info.get("keyable"):
        words += "  (with the temporary key)"
      if not usable:
        words += f"  ✗ {info.get('why', '')[:60]}"
      b = RadioButton(words, classes=f"tw-r-{route}", disabled=not usable)
      b.route, b.usable = route, usable
      buttons.append(b)
    await rs.mount(*buttons)
    chosen = next((b for b in buttons if b.route == current and b.usable), next((b for b in buttons if b.usable), None))
    if chosen is not None:
      select(rs, chosen)

  def chosen_route(self):
    for b in self.query("#tw-route RadioButton").results(RadioButton):
      if b.value:
        return b.route
    return None

  @on(RadioSet.Changed, "#tw-route")
  async def route_set(self, event):
    event.stop()
    await self.route_changed()

  async def route_changed(self):
    async with self.rebuilding:
      await self.fill_runs()
    self.runs_changed()
    self.key_changed()

  async def fill_runs(self):
    route = self.chosen_route()
    info = self.plan.get("routes", {}).get(route or "", {})
    runs = info.get("runs_on") or (["slurp"] if route == "relay" else ["detached"])
    current = self.chosen_runs_on() or (self.editing.runs_on if self.editing else None)
    if current not in runs:
      current = "job" if "job" in runs else runs[0]
    rs = self.query_one("#tw-runs-on", RadioSet)
    await rs.remove_children()
    labels = {"slurp": "in slurp (stops when slurp quits; Continue goes on)",
              "detached": "detached on the cluster's login node (goes on without slurp)",
              "job": "as a transfer job (Slurm; goes on without slurp)"}
    buttons = []
    for kind in runs:
      b = RadioButton(labels[kind], classes=f"tw-o-{kind}")
      b.kind = kind
      buttons.append(b)
    await rs.mount(*buttons)
    for b in buttons:
      if b.kind == current:
        select(rs, b)
    note = ""
    if route == "relay":
      note = ("The bytes pass through this machine (slurp's): its connections to both clusters set the speed."
              if all(c is not None and c.destination for c in (self.src, self.dst)) else
              "One end is this machine: this is a direct copy over slurp's SSH session.")
    elif route in ("direct-push", "direct-pull"):
      client = self.src if route == "direct-push" else self.dst
      note = f"rsync runs on {client.name if client else '?'} and logs in to the other cluster by itself."
    elif route == "same-fs":
      note = f"Both see the same filesystem: rsync runs on {self.src.name if self.src else '?'}, no network."
    self.query_one("#tw-route-note", Static).update(note)
    key = self.query_one("#tw-key", Checkbox)
    key.display = route in ("direct-push", "direct-pull")
    if route in ("direct-push", "direct-pull") and not info.get("ok") and info.get("keyable"):
      key.value = True

  def chosen_runs_on(self):
    for b in self.query("#tw-runs-on RadioButton").results(RadioButton):
      if b.value:
        return b.kind
    return None

  @on(RadioSet.Changed, "#tw-runs-on")
  def runs_set(self, event):
    event.stop()
    self.runs_changed()

  def show_flow(self):
    route = self.chosen_route()
    flow = self.query_one("#tw-flow", Static)
    flow.update(flow_diagram(route, self.chosen_runs_on(), self.src, self.dst, self.src.name if self.src else "?",
                             self.dst.name if self.dst else "?") if route else "")

  def runs_changed(self):
    self.show_flow()
    job = self.chosen_runs_on() == "job"
    self.query_one("#tw-job", Vertical).display = job
    route = self.chosen_route()
    client = self.dst if route == "direct-pull" else self.src
    part = self.query_one("#tw-partition", Input)
    if job and not part.value:
      part.value = str(site_of(client).get("transfer_partition") or "")
    self.query_one("#tw-compress", Checkbox).display = route == "relay"
    self.update_next()

  @on(Checkbox.Changed, "#tw-key")
  def key_toggled(self, event):
    event.stop()
    self.key_changed()

  def key_changed(self):
    route = self.chosen_route()
    on_ = self.query_one("#tw-key", Checkbox).value and route in ("direct-push", "direct-pull")
    warning = self.query_one("#tw-key-warning", Static)
    warning.display = on_
    if on_:
      client, server = (self.src, self.dst) if route == "direct-push" else (self.dst, self.src)
      repeated = self.query_one("#tw-when-every", RadioButton).value or self.query_one("#tw-when-daily", RadioButton).value
      warning.update(f"⚠ This adds a line to {server.name}:~/.ssh/authorized_keys for a key that slurp makes on "
                     f"{client.name} (in the run's folder there, readable only by you). The key may only run an rsync "
                     "server (no shell, no forwarding) and expires after some hours; slurp removes the line when the run "
                     f"ends. Anyone who can read your files on {client.name} could use it until then. Some sites forbid "
                     "this, or overwrite the file (keys managed elsewhere, certificates only)."
                     + (" A repeated transfer installs it anew for each run." if repeated else ""))
    self.update_next()

  def how_ok(self):
    route = self.chosen_route()
    if route is None:
      return False, "choose a route"
    info = self.plan.get("routes", {}).get(route, {})
    if not info.get("ok") and not (info.get("keyable") and self.query_one("#tw-key", Checkbox).value):
      return False, "this route needs the temporary key"
    if self.chosen_runs_on() is None:
      return False, "where it runs"
    if self.chosen_runs_on() == "job" and not self.query_one("#tw-time", Input).value.strip():
      return False, "the time limit of the job"
    bw = self.query_one("#tw-bwlimit", Input).value.strip()
    if bw:
      try:
        float(bw)
      except ValueError:
        return False, "the limit: a number of MB/s"
    return True, ""

  # ---- 5: when
  @on(RadioSet.Changed, "#tw-when")
  def when_set(self, event):
    event.stop()
    self.when_changed()

  @on(Input.Changed, "#tw-at, #tw-every, #tw-daily, #tw-job-id")
  def when_typed(self, event):
    event.stop()
    self.when_changed()

  def when_kind(self):
    for kind, sel in (("now", "now"), ("at", "at"), ("every", "every"), ("daily", "daily"), ("after_job", "after"), ("never", "never")):
      if self.query_one(f"#tw-when-{sel}", RadioButton).value:
        return kind
    return "now"

  def schedule(self):
    q = self.query_one
    kind = self.when_kind()
    s = {"kind": kind}
    if kind not in ("now", "never"):
      s.update(login=q("#tw-login", Checkbox).value, missed=q("#tw-missed", Select).value or "run")
    if kind == "at":
      s["at"] = S.parse_at(q("#tw-at", Input).value)
    elif kind == "every":
      s["every"] = q("#tw-every", Input).value.strip()
    elif kind == "daily":
      s["daily"] = q("#tw-daily", Input).value.strip()
    elif kind == "after_job":
      s["job"] = q("#tw-job-id", Input).value.strip()
      s["job_tab"] = M.tab_ref(self.src) if self.src else None
    return s

  def when_ok(self):
    s = self.schedule()
    kind = s["kind"]
    if kind == "at" and not s.get("at"):
      return False, "when: 14:00, 2026-11-20 14:00 or +2h"
    if kind == "at" and s["at"] < time.time() - 60:
      return False, "that time is past"
    if kind == "every" and not S.every_seconds(s.get("every")):
      return False, "every: 30m, 6h, 1d (at least 5 minutes)"
    if kind == "daily" and not S.daily_time(s.get("daily")):
      return False, "daily at: 02:00"
    if kind == "after_job" and not s.get("job", "").replace("_", "").isdigit():
      return False, "the job ID"
    return True, ""

  def when_changed(self):
    if not self.is_mounted:
      return
    kind = self.when_kind()
    q = self.query_one
    for sel, k in (("#tw-at", "at"), ("#tw-every", "every"), ("#tw-daily", "daily"), ("#tw-job-id", "after_job")):
      q(sel, Input).disabled = kind != k
    q("#tw-login", Checkbox).display = q("#tw-missed", Select).display = kind not in ("now", "never")
    ok, why = self.when_ok()
    note = why if not ok else ""
    if ok and kind == "never":
      note = "It is added to the transfers and runs only when you click Run now (in its subtab or the list: s)."
    elif ok and kind != "now" and self.chosen_runs_on() == "slurp":
      note = "It runs through slurp: slurp must be running then (it may run in its session holder, without a terminal)."
    if ok and kind == "at" and self.chosen_runs_on() == "job":
      note = "The transfer job is submitted when you press Go, to start at that time (slurp need not run then)."
    q("#tw-when-note", Static).update(note)
    self.update_next()

  # ---- 6: the summary
  def options(self):
    q = self.query_one
    route = self.chosen_route()
    o = {"checksum": q("#tw-checksum", Checkbox).value, "compress": q("#tw-compress", Checkbox).value and route == "relay",
         "bwlimit": q("#tw-bwlimit", Input).value.strip(),
         "temp_key": q("#tw-key", Checkbox).value and route in ("direct-push", "direct-pull")}
    if self.chosen_runs_on() == "job":
      o.update(partition=q("#tw-partition", Input).value.strip(), time=q("#tw-time", Input).value.strip(),
               account=q("#tw-account", Input).value.strip(), resubmit=q("#tw-resubmit", Checkbox).value)
    return {k: v for k, v in o.items() if v not in ("", None, False)}

  def build(self):
    t = self.draft()
    t.name = self.query_one("#tw-name", Input).value.strip() or t.name
    t.route, t.runs_on = self.chosen_route(), self.chosen_runs_on()
    t.options, t.schedule, t.plan = self.options(), self.schedule(), dict(self.plan)
    return t

  def fill_summary(self):
    name = self.query_one("#tw-name", Input)
    if not name.value:
      name.value = self.editing.name if self.editing else M.default_name(self.paths, M.tab_ref(self.src), M.tab_ref(self.dst))
    t = self.build()
    info = self.plan.get("routes", {}).get(t.route, {})
    est = info.get("estimate")
    client = self.dst if t.route == "direct-pull" else self.src
    self.query_one("#tw-summary-flow", Static).update(
      flow_diagram(t.route, t.runs_on, self.src, self.dst, self.src.name, self.dst.name))
    text = Text()
    def row(label, value, style=""):
      text.append(f"{label:10}", style="bold")
      text.append(f"{value}\n", style=style)
    row("From", f"{self.src.name}  " + ", ".join(self.paths))
    row("To", f"{self.dst.name}  {self.dest_path}")
    if self.plan.get("remaining_bytes") is not None:
      row("Moves", f"{human(self.plan['remaining_bytes'])} in {self.plan.get('remaining_files', '?')} files"
                   + (f" (of {human(self.plan.get('total_bytes'))})" if self.plan.get("total_bytes") else ""))
    runs = M.RUNS_ON[t.runs_on]
    if t.runs_on == "job":
      runs += (f" ({t.options.get('partition') or 'the default partition'}, {t.options.get('time')}"
               + (", then the next one" if t.options.get("resubmit") else "") + ")")
    row("Route", f"{M.ROUTES[t.route]}, {runs}")
    row("Estimate", (f"{P.duration(est[0])} – {P.duration(est[1])}" if est else "?") + (" (plus the time it waits in the queue)" if t.runs_on == "job" else ""))
    row("When", S.describe(t.schedule))
    opts = [w for k, w in (("checksum", "checksums"), ("compress", "compressed"), ("temp_key", "temporary key")) if t.options.get(k)]
    if t.options.get("bwlimit"):
      opts.append(f"at most {t.options['bwlimit']} MB/s")
    row("Options", ", ".join(opts) or "—")
    text.append("\nIf slurp closes: ", style="bold")
    text.append(if_slurp_closes(t, client.name if client else "", "", site_of(client)) + "\n")
    for w in self.plan.get("warnings", []):
      text.append(f"⚠ {w}\n", style="yellow")
    text.append("\nNothing is deleted on the destination; running it again copies only what is missing or changed.\n", style="dim")
    g = globus_text(self.plan)
    if g.plain:
      text.append("\n")
      text.append_text(g)
    self.query_one("#tw-summary", Static).update(text)

  @on(Input.Changed, "#tw-name")
  def name_typed(self, event):
    event.stop()

  @on(Button.Pressed, "#tw-go")
  def go_pressed(self, event):
    event.stop()
    if self.how_ok()[0] and self.when_ok()[0]:
      self.dismiss({"transfer": self.build(), "go": True})

  @on(Button.Pressed, "#tw-save")
  def save_pressed(self, event):
    event.stop()
    if self.how_ok()[0] and self.when_ok()[0]:
      self.dismiss({"transfer": self.build(), "go": False})

  @on(Button.Pressed)
  def place_pressed(self, event):
    if event.button.has_class("tw-place"):
      event.stop()
      self.set_dest(event.button.place)

class TransfersBase(SplitScreen):
  """(the screens of the Transfers tab: its subtab bar shows its own subtabs)"""
  TRANSFERS = True

  @property
  def manager(self):
    return self.app.transfers

  def on_mount(self):
    super().on_mount()
    self.manager.listeners.append(self.changed)
    self.set_interval(1, self.refresh_view)
    self.call_after_refresh(self.refresh_view)

  def on_unmount(self):
    if self.changed in self.manager.listeners:
      self.manager.listeners.remove(self.changed)

  def on_screen_resume(self):
    self.refresh_view()

  def changed(self, t=None):
    if self.is_mounted and self.app.screen is self:
      self.refresh_view()

  def refresh_view(self):
    pass

  def confirm_pause(self, t):
    """Pause; a transfer job is cancelled for it: said first"""
    if t.runs_on == "job" and t.run.get("job") and t.active:
      ends = self.manager.ends(t)
      client = ends[1] if client_side(t.route) == "dest" else ends[0]
      self.app.push_screen(ConfirmScreen(
        "Pause the transfer job?",
        f"Pausing cancels Slurm job {t.run['job']} on {client.name if client else '?'}. It loses its place in the queue "
        "and the time it used. Files already copied stay.\n\nContinue submits a new job, which waits in the queue again.",
        "Pause", variant="warning"), lambda ok: self.manager.pause(t) if ok else None)
      return
    self.manager.pause(t)

  @staticmethod
  def can_continue(t):
    return t.state in ("paused", "failed", "done", "done-errors", "new")

  def run_now(self, t):
    if not t.active and t.state != "waiting":
      self.manager.start(t)

  def edit(self, t):
    if t.active:
      self.app.notify("pause it first: a running transfer cannot be changed", title=t.name, severity="warning")
      return
    self.app.edit_transfer(t)

  def delete(self, t):
    if t.active:
      self.app.notify("pause it first", title=t.name, severity="warning")
      return
    self.app.push_screen(ConfirmScreen("Delete the transfer?", f"{t.name}: its settings and its history go away. The files "
                                       "on both sides stay as they are.", "Delete"),
                         lambda ok: self.app.delete_transfer(t) if ok else None)

COLUMNS = [("name", "Name"), ("ends", "From → To"), ("how", "Route"), ("state", "State"), ("progress", "Progress"),
           ("rate", "Rate"), ("left", "Left"), ("when", "Next / last run")]

class TransfersScreen(TransfersBase):
  """The list of transfers: the first subtab of the Transfers tab"""
  DEFAULT_CSS = """
  #xfer-toolbar { height: auto; padding: 0 1; }
  #xfer-toolbar Button { margin-right: 1; }
  #xfer-summary { width: 1fr; color: $text-muted; padding: 0 1; }
  #xfer-list { height: 1fr; }
  #xfer-note { height: auto; max-height: 4; padding: 0 1; background: $panel; }
  """
  BINDINGS = [Binding("n", "new", "New transfer"), Binding("p", "pause", "Pause"), Binding("c", "resume", "Continue"),
              Binding("s", "run_now", "Run now"), Binding("e", "edit", "Edit"), Binding("delete", "delete", "Delete")]
  icon_name = "transfer"

  def compose_left(self):
    with Horizontal(id="xfer-toolbar"):
      yield Button(icons.label("new_script", "New transfer"), id="xfer-new", variant="primary", compact=True, tooltip="set up a transfer (n)")
      yield Static(id="xfer-summary")
    yield ClickTable(id="xfer-list", cursor_type="row", zebra_stripes=True)
    yield Static(id="xfer-note")

  def on_mount(self):
    table = self.query_one("#xfer-list", ClickTable)
    for key, label in COLUMNS:
      table.add_column(label, key=key)
    super().on_mount()
    table.focus()

  def refresh_view(self):
    if not self.is_mounted:
      return
    table = self.query_one("#xfer-list", ClickTable)
    transfers = self.manager.transfers
    selected = self.current()
    keep = table.cursor_row
    table.clear()
    for t in transfers:
      src, dst = self.manager.ends(t)
      p = t.progress
      shown = p.get("percent") if t.state in ("running", "pausing", "paused", "done", "done-errors", "failed", "waiting") else None
      last = t.runs[-1] if t.runs else None
      when = (S.when_text(t.next_run) if t.next_run else
              (f"{S.when_text(last['ended'])} {M.STATES.get(last['result'], last['result'])}" if last else ""))
      table.add_row(Text(t.name, style="bold"), Text.assemble(dot(src), f" {t.source_name} → {t.dest_name} ", dot(dst)),
                    route_text(t), state_text(t), bar(shown) if shown is not None else "",
                    rate_text(p.get("rate")) if t.state == "running" else "",
                    P.duration(p.get("eta")) if t.state == "running" and p.get("eta") else "", when, key=t.id)
    if transfers:
      row = next((i for i, t in enumerate(transfers) if selected is not None and t.id == selected.id), keep)
      table.move_cursor(row=min(max(row, 0), len(transfers) - 1))
    running = sum(t.state == "running" for t in transfers)
    self.query_one("#xfer-summary", Static).update(
      f"{len(transfers)} transfer{'s' if len(transfers) != 1 else ''}" + (f", {running} running" if running else "")
      + " · Enter: its details · p pause, c continue, s run now, e edit, Del delete")
    self.show_note()

  def show_note(self):
    t = self.current()
    note = self.query_one("#xfer-note", Static)
    if t is None:
      note.update(Text("No transfers. New transfer sets one up: from one tab to another, checked first, with an estimate.", style="dim"))
      return
    text = Text.assemble((t.name, "bold"), ": ", state_text(t))
    if t.message:
      text.append(f" · {t.message}")
    if t.schedule and t.schedule.get("kind", "now") not in ("now", "never"):
      text.append(f" · {S.describe(t.schedule, t.next_run)}", style="cyan")
    note.update(text)

  def current(self):
    table = self.query_one("#xfer-list", ClickTable)
    if not table.row_count:
      return None
    try:
      key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
    except Exception:
      return None
    return self.manager.get(key)

  @on(ClickTable.RowHighlighted, "#xfer-list")
  def highlighted(self, event):
    self.show_note()

  @on(ClickTable.RowSelected, "#xfer-list")
  def selected(self, event):
    event.stop()
    t = self.manager.get(event.row_key.value)
    if t is not None:
      self.app.open_transfer(t)

  @on(Button.Pressed, "#xfer-new")
  def new_pressed(self, event):
    event.stop()
    self.action_new()

  def action_new(self):
    self.app.action_transfer_data()

  def action_pause(self):
    t = self.current()
    if t is not None:
      self.confirm_pause(t)

  def action_resume(self):
    t = self.current()
    if t is not None and self.can_continue(t):
      self.manager.resume(t)

  def action_run_now(self):
    t = self.current()
    if t is not None:
      self.run_now(t)

  def action_edit(self):
    t = self.current()
    if t is not None:
      self.edit(t)

  def action_delete(self):
    t = self.current()
    if t is not None:
      self.delete(t)

class TransferScreen(TransfersBase):
  """A transfer: its state, route, progress (a bar, the rate over time), what happens if slurp closes, its schedule,
  the errors of its last run, its runs, its check and its log"""
  DEFAULT_CSS = """
  #xd-body { height: 1fr; padding: 0 1; }
  #xd-body Static { height: auto; }
  #xd-actions { height: auto; margin: 1 0; }
  #xd-actions Button { margin-right: 1; }
  #xd-rate { height: 6; margin-top: 1; }
  #xd-closes { color: $text-muted; }
  """
  BINDINGS = [Binding("p", "pause", "Pause"), Binding("c", "resume", "Continue"), Binding("s", "run_now", "Run now"),
              Binding("e", "edit", "Edit")]
  icon_name = "transfer"

  def __init__(self, tid):
    super().__init__()
    self.tid = tid

  @property
  def transfer(self):
    return self.manager.get(self.tid)

  def compose_left(self):
    with VerticalScroll(id="xd-body"):
      yield Static(id="xd-head")
      yield Static(id="xd-route")
      yield Static(id="xd-paths")
      with Horizontal(id="xd-actions"):
        yield Button(icons.label("suspended", "Pause"), id="xd-pause", variant="warning", compact=True, tooltip="stop the run; Continue goes on (p)")
        yield Button(icons.label("running", "Continue"), id="xd-resume", variant="primary", compact=True, tooltip="a new run: it goes on where it stopped (c)")
        yield Button(icons.label("reload", "Run now"), id="xd-run", compact=True, tooltip="a run now, besides the schedule (s)")
        yield Button(icons.label("edit", "Edit…"), id="xd-edit", compact=True, tooltip="its settings, in the wizard (e)")
        yield Button(icons.label("jobs", "Its job"), id="xd-job", compact=True, tooltip="the job window of its transfer job")
        yield Button(icons.label("connect", "Connect"), id="xd-connect", compact=True, tooltip="log in to the tab it waits for")
        yield Button(icons.label("cancel_job", "Delete"), id="xd-delete", variant="error", compact=True, tooltip="its settings and history (not the files)")
      yield Static(id="xd-progress")
      yield Static(id="xd-current")
      yield Static("Rate", classes="section")
      yield TimePlot(color="green", fmt=lambda v: human(v) + "/s", id="xd-rate")
      yield Static("If slurp closes", classes="section")
      yield Static(id="xd-closes")
      yield Static("Schedule", classes="section")
      yield Static(id="xd-schedule")
      yield Static("Errors of the last run", classes="section", id="xd-errors-title")
      yield Static(id="xd-errors")
      yield Static("Runs", classes="section")
      yield Static(id="xd-runs")
      yield Static("The check", classes="section")
      yield Static(id="xd-check")
      yield Static("Log", classes="section")
      yield Static(id="xd-log")

  def on_mount(self):
    super().on_mount()
    self.manager.shown.add(self.tid)

  def on_unmount(self):
    super().on_unmount()
    self.manager.shown.discard(self.tid)

  def refresh_view(self):
    if not self.is_mounted:
      return
    t = self.transfer
    q = self.query_one
    if t is None:
      q("#xd-head", Static).update(Text("This transfer was deleted.", style="dim"))
      q("#xd-actions", Horizontal).display = False
      return
    src, dst = self.manager.ends(t)
    head = Text.assemble((t.name, "bold"), "  ", state_text(t))
    if t.message:
      head.append(f" · {t.message}")
    q("#xd-head", Static).update(head)
    self.frame = getattr(self, "frame", 0) + 1
    route = flow_diagram(t.route, t.runs_on, src, dst, t.source_name, t.dest_name, t.run.get("job", ""),
                         moving=self.frame if t.state == "running" else None)
    route.append(f"\n{M.ROUTES.get(t.route, t.route)}, {M.RUNS_ON.get(t.runs_on, t.runs_on)}", style="dim")
    if t.run.get("job"):
      route.append(f", job {t.run['job']}", style="dim")
    q("#xd-route", Static).update(route)
    q("#xd-paths", Static).update(Text("\n".join([f"from {t.source_name}: {p}" for p in t.source.get("paths", [])]
                                                 + [f"into {t.dest_name}: {t.dest.get('path', '')}"]), style="dim"))
    q("#xd-pause", Button).display = t.active or t.state in ("scheduled", "waiting")
    q("#xd-resume", Button).display = self.can_continue(t)
    q("#xd-run", Button).display = not t.active and t.state != "waiting"
    q("#xd-job", Button).display = bool(t.run.get("job") or (t.runs and t.runs[-1].get("job")))
    missing = [c for c in self.manager.not_ready(t) if c is not None]
    connect = q("#xd-connect", Button)
    connect.display = bool(missing)
    if missing and str(connect.label) != icons.label("connect", f"Connect {missing[0].name}"):
      connect.label = icons.label("connect", f"Connect {missing[0].name}")
    p = t.progress
    lines = Text()
    if p.get("percent") is not None or p.get("bytes"):
      lines.append_text(bar(p.get("percent"), 40))
      total = p.get("total")
      lines.append(f"   {human(p.get('bytes'))}" + (f" of {human(total)}" if total else ""))
      if t.state == "running":
        if p.get("rate"):
          lines.append(f" · {rate_text(p['rate'])}")
        if p.get("transport") and t.route == "relay":
          lines.append(f" (through slurp {rate_text(p['transport'])})", style="dim")
        if p.get("eta"):
          lines.append(f" · {P.duration(p['eta'])} left", style="bold")
      files = p.get("files") or 0
      if files or p.get("files_total"):
        lines.append(f" · {files} files" + (f" of {p['files_total']}" if p.get("files_total") else ""))
      if p.get("errors"):
        lines.append(f" · {p['errors']} errors", style="yellow")
    elif t.plan.get("remaining_bytes") is not None:
      lines.append(f"To copy: {human(t.plan['remaining_bytes'])} in {t.plan.get('remaining_files', '?')} files (at the check)", style="dim")
    q("#xd-progress", Static).update(lines)
    q("#xd-current", Static).update(Text(f"now: {p['current']}", style="dim") if t.state == "running" and p.get("current") else "")
    q("#xd-rate", TimePlot).set_data(rates(p.get("history") or []))
    client = dst if client_side(t.route) == "dest" else src
    q("#xd-closes", Static).update(if_slurp_closes(t, client.name if client else "", t.run.get("host", ""), site_of(client)))
    sched = S.describe(t.schedule, t.next_run)
    if t.schedule and t.schedule.get("kind", "now") not in ("now", "never"):
      sched += ("; logs in by itself" if t.schedule.get("login", True) else "; does not log in by itself")
      sched += "; a missed run " + ("is skipped" if t.schedule.get("missed") == "skip" else "runs once")
    q("#xd-schedule", Static).update(sched)
    errors = t.errors or []
    q("#xd-errors-title", Static).display = q("#xd-errors", Static).display = bool(errors)
    q("#xd-errors", Static).update(Text("\n".join(errors[-30:]), style="yellow"))
    runs = Text()
    for r in reversed(t.runs[-15:]):
      runs.append(f"{datetime.fromtimestamp(r['started']).strftime('%Y-%m-%d %H:%M')} – {datetime.fromtimestamp(r['ended']).strftime('%H:%M')}  ")
      runs.append(f"{M.STATES.get(r['result'], r['result']):17}", style=STATE_STYLES.get(r["result"], ""))
      runs.append(f" {human(r.get('bytes')):>10}  {ROUTE_SHORT.get(r.get('route'), r.get('route', ''))}, {RUNS_SHORT.get(r.get('runs_on'), '')}"
                  + (f", job {r['job']}" if r.get("job") else "") + (f"  {r['message'][:80]}" if r.get("message") else "") + "\n")
    q("#xd-runs", Static).update(runs if t.runs else Text("none yet", style="dim"))
    check = plan_text(t.plan) if t.plan.get("steps") else Text("no check", style="dim")
    if t.plan.get("routes"):
      check.append_text(routes_text(t.plan))
    q("#xd-check", Static).update(check)
    q("#xd-log", Static).update(Text("\n".join(f"{datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S')}  {text}" for ts, text in t.log[-25:]), style="dim"))

  @on(Button.Pressed)
  def pressed(self, event):
    t = self.transfer
    bid = event.button.id or ""
    if t is None or not bid.startswith("xd-"):
      return
    event.stop()
    if bid == "xd-pause":
      self.confirm_pause(t)
    elif bid == "xd-resume":
      self.manager.resume(t)
    elif bid == "xd-run":
      self.run_now(t)
    elif bid == "xd-edit":
      self.edit(t)
    elif bid == "xd-delete":
      self.delete(t)
    elif bid == "xd-connect":
      missing = [c for c in self.manager.not_ready(t) if c is not None]
      if missing:
        self.app.connect_cluster(missing[0])
    elif bid == "xd-job":
      self.app.open_transfer_job(t)

  def action_pause(self):
    if self.transfer is not None:
      self.confirm_pause(self.transfer)

  def action_resume(self):
    if self.transfer is not None and self.can_continue(self.transfer):
      self.manager.resume(self.transfer)

  def action_run_now(self):
    if self.transfer is not None:
      self.run_now(self.transfer)

  def action_edit(self):
    if self.transfer is not None:
      self.edit(self.transfer)

def rates(history):
  """[(time, bytes per second)] from the progress history ([[time, bytes]])"""
  out = []
  for (t0, b0), (t1, b1) in zip(history, history[1:]):
    if t1 > t0:
      out.append((t1, max(b1 - b0, 0) / (t1 - t0)))
  return out
