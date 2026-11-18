"""Transfers: the top-level tab Transfers (its list, a subtab for each transfer shown), the wizard that sets one up
(source, destination, check, how, when, summary: Go), and a window that picks paths on any tab"""

import asyncio
import os
import time
from datetime import datetime

import humanize
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, ContentSwitcher, Input, Label, RadioButton, RadioSet, Select, Static

from ...cluster.listing import listing
from ...cluster.remote import HEALTH_STYLES, connection_health, in_background
from ...transfer import model as M, plan as P, rsync as R, schedule as S
from ...transfer.endpoint import Endpoint
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

def flow_diagram(route, runs_on, src, dst, src_name, dst_name, job="", moving=None, rate=None, estimate=None, measuring=False):
  """How the data flows, as boxes and arrows: the source, slurp (the relay between two clusters), the destination; on
  each arrow how (push, pull, relay, the same filesystem), under it the bandwidth measured (rate; measuring: not yet),
  under each box what runs there, and below all the estimate (a line of words). moving: a frame number while it runs
  (a dot moves along the arrows)"""
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
  speed = f"{rate_text(rate)}" if rate else "measuring …" if measuring else ""
  arrow_widths = [max(cell_len(a) + 4, cell_len(speed) + 4, 10) for a in arrows]
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
      spad = w - cell_len(speed)
      lines[2].append(" " * (spad // 2) + speed + " " * (spad - spad // 2), style="bold cyan" if rate else "dim")
      for line in lines[3:]:
        line.append(" " * w)
  for line in lines:
    line.rstrip()
  if estimate:
    lines.append(Text.assemble(("⏱ ", "bold"), (estimate, "bold")))
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
  multi), a pattern (*.h5: the listing shows its matches), Choose. Dismissed with [paths] (the marked ones, the folder
  shown, or the pattern in it), or None"""
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
  #pp-pattern-row { height: auto; margin-top: 1; }
  #pp-pattern { width: 1fr; margin-right: 1; }
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
      if self.multi:
        with Horizontal(id="pp-pattern-row"):
          yield Input(placeholder="or a pattern in this folder: *.h5, run_??.out, step_[0-9]*.log (the list shows its matches)",
                      id="pp-pattern", tooltip="its matches are copied, each into the destination folder (as cp does)")
          yield Button("Choose the pattern", id="pp-choose-pattern", compact=True, disabled=True)
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
    pattern = self.pattern()
    if self.path != "/" and not pattern:
      table.add_row("", Text("..", style="bold"), "", "", key="..")
    import fnmatch
    shown = [e for e in self.entries if not pattern or fnmatch.fnmatchcase(e["name"], pattern)]
    for e in sorted(shown, key=lambda e: (not e["dir"], e["name"].lower())):
      table.add_row("✓" if e["name"] in self.marked else "", Text(e["name"] + ("/" if e["dir"] else ""), style="bold blue" if e["dir"] else ""),
                    "" if e["dir"] else human(e["size"]), datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M"), key=e["name"])
    hint = "Space marks, Enter: into a folder" if self.multi else "Enter: into a folder"
    status = f"{self.path} · {len(self.marked)} marked · {hint}" if self.multi else f"{self.path} · {hint}"
    if pattern:
      status = f"{self.path}/{pattern}: {len(shown)} match{'es' if len(shown) != 1 else ''} (shown)"
    self.query_one("#pp-status", Static).update(Text(status, style="dim"))
    for b in self.query("#pp-marked").results(Button):
      b.disabled = not self.marked
    for b in self.query("#pp-choose-pattern").results(Button):
      b.disabled = not pattern

  def pattern(self):
    """The pattern typed ("" when none, or in a picker without one: the destination's)"""
    found = list(self.query("#pp-pattern").results(Input))
    text = found[0].value.strip() if found else ""
    return text if text and "/" not in text else ""

  @on(Input.Changed, "#pp-pattern")
  def pattern_typed(self, event):
    event.stop()
    self.populate()

  @on(Input.Submitted, "#pp-pattern")
  def pattern_submitted(self, event):
    event.stop()
    if self.pattern():
      self.dismiss([self.full(self.pattern())])

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
    elif bid == "pp-choose-pattern" and self.pattern():
      event.stop()
      self.dismiss([self.full(self.pattern())])

  def action_cancel(self):
    self.dismiss(None)

def select(radio_set, button):
  """Press one button of a radio set from code (None: none): the others off, and the set knows which one is pressed.
  The set is told directly, its buttons' messages held back: while a RadioSet.Changed is handled it would hear nothing
  of them (two stayed pressed), and a button turned off it turns on again (a radio set never has none). The caller
  does what follows from the choice."""
  with radio_set.prevent(RadioButton.Changed):
    for b in radio_set.query(RadioButton).results(RadioButton):
      if b is not button and b.value:
        b.value = False
    if button is not None:
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
  """Set up a transfer: 1 source, 2 destination, 3 route (the check, the choice of what it found, where it runs),
  4 options (checksums, compression, a speed limit), 5 when, 6 summary. Dismissed with
  {"transfer": Transfer, "go": bool} or None. editing: a transfer whose settings it changes (its id stays)"""
  STEPS = ["Source", "Destination", "Route", "Options", "When", "Summary"]
  ROUTE_ORDER = ("same-fs", "direct-push", "direct-pull", "relay")
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
  #tw-check-steps, #tw-globus { height: auto; }
  #tw-globus { margin-top: 1; }
  #tw-route, #tw-runs-on, #tw-when { height: auto; width: 1fr; }
  #tw-job { height: auto; border: round $panel; padding: 0 1; margin-top: 1; }
  #tw-partition, #tw-time, #tw-account { width: 24; margin-right: 2; }
  #tw-account-list { width: 20; margin-right: 2; }
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
    self.sizes = {}       # (id(tab), path) -> {"bytes", "files", "dirs", "links"} | None (not there) | "counting" | "failed"
    self.node_checks = {} # (route, partition, account) -> ("running" | True | False | None, words): the login from a node
    self.job_tests = {}   # (route, partition, time, account) -> ("running" | True | False, words): sbatch --test-only
    self.node_timer = None

  # ---- the window
  def compose(self) -> ComposeResult:
    with Vertical(id="tw-dialog"):
      yield Static("Edit the transfer" if self.editing else "Transfer data", id="tw-title")
      with Horizontal(id="tw-steps"):
        for i, name in enumerate(self.STEPS):
          yield Button(f"{i + 1} {name}", id=f"tw-goto-{i}", compact=True)
      with ContentSwitcher(id="tw-switch", initial="tw-step-0"):
        with VerticalScroll(id="tw-step-0"):
          yield Static("What to copy: a tab, and one or more files or folders there, or patterns (/path/to/dir/*.h5: each "
                       "match). Each is copied into the destination folder under its own name.", classes="tw-note")
          with Horizontal(classes="tw-row"):
            yield Label("From")
            yield Select(self.tab_options(), id="tw-src-tab", allow_blank=False, value=self.tab_value(self.src), compact=True)
          yield Static(id="tw-src-state", classes="tw-note")
          yield Static(id="tw-src-paths")
          with Horizontal(classes="tw-row"):
            yield Input(placeholder="a path or a pattern (*.h5): Enter adds it; ~ and relative paths are in your home", id="tw-src-path")
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
          yield Static("slurp checks both sides (it changes nothing): what remains to copy, the room on the destination, "
                       "and each route, how fast it is. Choose one that passed.", classes="tw-note")
          yield Static(id="tw-check-steps")
          yield Label("Route")
          with RadioSet(id="tw-route"):
            for route in self.ROUTE_ORDER:
              b = RadioButton(f"… {M.ROUTES[route]}", classes=f"tw-r-{route}", disabled=True)
              b.route = route
              yield b
          yield Static(id="tw-globus")
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
              yield Select([], prompt="pick", id="tw-account-list", compact=True)
            yield Checkbox("submit the next job when the time limit ends", value=True, id="tw-resubmit", compact=True)
            yield Static(id="tw-job-test")
            yield Static(id="tw-node-check")
          with Horizontal(classes="tw-row"):
            yield Checkbox("Install a temporary key", id="tw-key", compact=True)
          yield Static(id="tw-key-warning")
          with Horizontal(classes="tw-row"):
            yield Button(icons.label("reload", "Check again"), id="tw-recheck", compact=True)
        with VerticalScroll(id="tw-step-3"):
          yield Static("How rsync copies: the same at every run.", classes="tw-note")
          with Horizontal(classes="tw-row"):
            yield Checkbox("checksums: compare the contents (every file is read on both sides, at every run, also when "
                           "nothing changed: much slower)", id="tw-checksum", compact=True)
          yield Static(id="tw-checksum-note", classes="tw-warn")
          with Horizontal(classes="tw-row"):
            yield Checkbox("compress (helps on the relay over slow links)", id="tw-compress", compact=True)
          yield Static(id="tw-compress-note", classes="tw-note")
          with Horizontal(classes="tw-row"):
            yield Label("limit the speed to")
            yield Input(placeholder="MB/s (none)", id="tw-bwlimit", compact=True)
            yield Label("MB/s (empty: no limit)")
          yield Static(id="tw-options-flow", classes="tw-note")
        with VerticalScroll(id="tw-step-4"):
          with RadioSet(id="tw-when"):
            yield RadioButton("never by itself: only when I click Run now", value=True, id="tw-when-never") # (the default)
            yield RadioButton("now", id="tw-when-now")
            yield RadioButton("once, at a time", id="tw-when-at")
            yield RadioButton("repeatedly, every …", id="tw-when-every")
            yield RadioButton("daily, at …", id="tw-when-daily")
            yield RadioButton("after a job ends", id="tw-when-after")
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
        yield Button("Save", id="tw-store", variant="primary", compact=True) # (editing: Cancel and Save only)

  def tab_options(self):
    return [(f"{c.name}{'' if self.ready(c) else ' (not connected)'}", str(i)) for i, c in enumerate(self.tabs)]

  def tab_value(self, cluster):
    return str(self.tabs.index(cluster)) if cluster in self.tabs else (str(0) if self.tabs else Select.BLANK)

  def on_mount(self):
    if self.editing:
      self.prefill(self.editing)
      if self.plan.get("done"): # (its check is for its sources and destination: the steps after it can be gone to)
        self.checked_for = (id(self.src), tuple(self.paths), id(self.dst), self.dest_path)
        self.refresh_routes()
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
    if self.step == 2 and any(v[0] == "running" for v in self.node_checks.values()):
      self.show_node_check()
    if self.step == 2 and any(v[0] == "running" for v in self.job_tests.values()):
      self.show_job_test()
    if self.step in (0, 1):
      self.refresh_states()
    if self.step == 0 and "counting" in self.sizes.values():
      self.show_sources()
    if self.step == 0 and self.paths and self.ready(self.src) and any((id(self.src), p) not in self.sizes for p in self.paths):
      self.refresh_sources() # (its tab connected meanwhile: counted now)

  # ---- the steps
  def show_step(self):
    self.query_one("#tw-switch", ContentSwitcher).current = f"tw-step-{self.step}"
    q = self.query_one
    editing = self.editing is not None # (editing: every step a click away, and Cancel and Save)
    q("#tw-back", Button).display = self.step > 0 and not editing
    q("#tw-next", Button).display = self.step < len(self.STEPS) - 1 and not editing
    last = self.step == len(self.STEPS) - 1
    never = self.when_kind() == "never"
    q("#tw-go", Button).display = last and not editing
    q("#tw-save", Button).display = last and not never and not editing # (Add saves it already)
    q("#tw-store", Button).display = editing
    go = "Save" if never and self.editing else "Add" if never else "Go"
    if str(q("#tw-go", Button).label) != go:
      q("#tw-go", Button).label = go
    q("#tw-go", Button).tooltip = ("add it to the transfers; it runs when you click Run now" if never else
                                   "start it now, or by its schedule")
    if self.step == 2:
      self.start_check()
    if self.step == 3:
      self.show_options()
    if self.step == 4:
      self.when_changed()
    if self.step == 5:
      self.fill_summary()
    self.update_next()

  def reachable(self, step):
    """A step can be gone to when every step before it is complete; editing: every step"""
    return self.editing is not None or step == self.step or all(self.step_ok(i)[0] for i in range(step))

  def incomplete(self):
    """The first step that is not complete: "3 Route: why" ("" when all are)"""
    for i in range(len(self.STEPS) - 1):
      ok, why = self.step_ok(i)
      if not ok:
        return f"{i + 1} {self.STEPS[i]}: {why}"
    return ""

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
    self.query_one("#tw-go", Button).disabled = self.step == len(self.STEPS) - 1 and not (self.how_ok()[0] and self.options_ok()[0] and self.when_ok()[0])
    if self.editing is not None:
      store = self.query_one("#tw-store", Button)
      missing = self.incomplete()
      store.disabled = bool(missing)
      store.tooltip = f"first: {missing}" if missing else "keep the changes (its schedule goes on; nothing starts now)"

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
      # (the check of both sides, and of the route chosen: Next as soon as these passed, the other routes may go on)
      plan, steps = self.plan, self.plan.get("steps", {})
      if not plan.get("done") and (self.checking is None or step != self.step):
        return False, "check first"
      if not self.plan_current():
        return False, "check again (the source or the destination changed)"
      if steps.get("paths", {}).get("state") == "bad" or steps.get("facts", {}).get("state") == "bad":
        return False, "fix that first (Back)"
      if any(steps.get(name, {}).get("state", "running") == "running" for name in ("facts", "paths", "remaining")) and not plan.get("done"):
        return False, "the check of both sides is not done"
      route = self.chosen_route()
      if route is None:
        usable = [r for r, i in plan.get("routes", {}).items() if i.get("ok") or i.get("keyable")]
        return False, "choose a route" if usable or not plan.get("done") else "no route works"
      info = plan.get("routes", {}).get(route)
      if info is None:
        return False, f"{M.ROUTES[route]}: its check is not done"
      return self.how_ok()
    if step == 3:
      return self.options_ok()
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
    self.count_sources()
    self.show_sources()
    self.refresh_states()
    self.refresh_preview()

  def count_sources(self):
    """What the sources hold (size, files, folders), counted on their tab in the background (each path once)"""
    cluster = self.src
    if cluster is None or not self.ready(cluster):
      return
    wanted = [p for p in self.paths if (id(cluster), p) not in self.sizes]
    if not wanted:
      return
    for p in wanted:
      self.sizes[(id(cluster), p)] = "counting"
    def done(found):
      for p in wanted:
        self.sizes[(id(cluster), p)] = (found or {}).get(p, "failed") if found is not None else "failed"
      self.show_sources()
    in_background(self, lambda: P.count(cluster and Endpoint(cluster), wanted), done, group=f"tw-count-{len(self.sizes)}",
                  name="counting the sources", connection=cluster.connection if cluster.destination else None)

  def show_sources(self):
    """The sources with what each holds, and in all"""
    if not self.is_mounted:
      return
    if not self.paths:
      self.query_one("#tw-src-paths", Static).update(Text("  nothing yet: Browse… or type a path", style="dim"))
      return
    from rich.cells import cell_len
    width = min(max(cell_len(p) for p in self.paths), 70)
    text, total, known = Text(), {"bytes": 0, "files": 0, "dirs": 0, "links": 0}, True
    def what(c):
      words = (f"{c['matches']:,} match{'es' if c['matches'] != 1 else ''} · " if "matches" in c else "") + (
        f"{human(c['bytes'])} · {c['files']:,} file{'s' if c['files'] != 1 else ''}")
      if c["dirs"] > 1 or (c["dirs"] == 1 and c["files"] != 1):
        words += f" · {c['dirs']:,} folder{'s' if c['dirs'] != 1 else ''}"
      if c["links"]:
        words += f" · {c['links']:,} link{'s' if c['links'] != 1 else ''}"
      return words
    for p in self.paths:
      c = self.sizes.get((id(self.src), p))
      text.append(f"  {short_path(p, width):{width}}  ")
      if c == "counting":
        text.append(f"{self.app.spinner()} counting …", style="yellow")
        known = False
      elif c is None:
        text.append("nothing matches" if R.is_pattern(p) else "not there", style="red")
      elif c == "failed":
        text.append("not counted", style="dim")
        known = False
      else:
        text.append(what(c), style="cyan")
        for k in total:
          total[k] += c[k]
      text.append("\n")
    if len(self.paths) > 1 or not known:
      text.append(f"  {'in all':{width}}  ", style="bold")
      text.append(what(total) + ("" if known else " so far"), style="bold cyan" if known else "dim")
    self.query_one("#tw-src-paths", Static).update(text)

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
    lines = [(f"  {os.path.basename(p)} (each match)  →  {self.dst.name}:{self.dest_path.rstrip('/')}/" if R.is_pattern(p) else
              f"  {os.path.basename(p.rstrip('/'))}  →  {self.dst.name}:{os.path.join(self.dest_path, os.path.basename(p.rstrip('/')))}")
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
    general = dict(self.plan, steps={k: v for k, v in self.plan.get("steps", {}).items() if k in ("facts", "paths", "remaining")})
    steps = plan_text(general, spinner) # (the routes: on their choices)
    if self.checking is not None and not self.plan.get("steps"):
      steps = Text(f" {spinner} starting the check …", style="yellow")
    if self.plan.get("done") and self.plan.get("remaining_bytes") == 0:
      steps.append(" Nothing to copy: the destination has all of it already (a run would only compare).\n", style="green")
    self.query_one("#tw-check-steps", Static).update(steps)
    self.query_one("#tw-globus", Static).update(globus_text(self.plan))
    self.refresh_routes()
    if self.step == 2:
      self.show_flow() # (the bandwidth and the estimate, as the check measures them)
    self.update_next()

  def refresh_routes(self):
    """The choices of the route as the check found them: passed (its speed, its estimate), the temporary key could,
    failed (why), not checked yet; the recommended one chosen when the check is done and none is"""
    plan = self.plan
    routes, checking, spinner = plan.get("routes", {}), self.checking is not None, self.app.spinner()
    rs = self.query_one("#tw-route", RadioSet)
    buttons = {b.route: b for b in rs.query(RadioButton).results(RadioButton)}
    for route, b in buttons.items():
      info = routes.get(route)
      name = M.ROUTES[route]
      if info is None:
        words, usable = (f"{spinner} {name}: checking …" if checking else f"· {name}: not checked"), False
      elif info.get("ok"):
        est = info.get("estimate")
        words = f"✓ {name}" + (f"  {rate_text(info.get('rate'))}" if info.get("rate") else "") + (
          f"  {P.duration(est[0])} – {P.duration(est[1])}" if est else "") + ("  (recommended)" if route == plan.get("recommended") else "")
        usable = True
      elif info.get("keyable"):
        words, usable = f"! {name}: logs in only with the temporary key (below) · {info.get('why', '')[:60]}", True
      else:
        words, usable = f"✗ {name}: {info.get('why', '')[:80]}", False
      if str(b.label) != words:
        b.label = words
      b.disabled = not usable
      b.usable = usable
    chosen = self.chosen_route()
    if chosen is not None and not buttons[chosen].usable and (routes.get(chosen) is not None or not checking):
      select(rs, None) # (it did not pass: no longer chosen)
      chosen = None
    if chosen is None:
      wanted = (self.editing.route if self.editing else None) if not plan.get("done") else (
        self.editing.route if self.editing and buttons.get(self.editing.route) and buttons[self.editing.route].usable else plan.get("recommended"))
      if wanted in buttons and buttons[wanted].usable:
        select(rs, buttons[wanted])
        self.call_later(self.route_changed)

  @on(Button.Pressed, "#tw-recheck")
  def recheck(self, event):
    event.stop()
    self.start_check(force=True)

  # ---- 3: the route (and how it runs)
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
    client = self.dst if route == "direct-pull" else self.src
    runs = ["slurp"] if route == "relay" else ["detached"] + (["job"] if P.partition_of(client, site_of) else [])
    runs = info.get("runs_on") or runs
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
    # (only for a direct route that cannot log in by itself: one that can needs none, and its key would be the only one)
    needs_key = route in ("direct-push", "direct-pull") and not info.get("ok") and info.get("keyable")
    if needs_key:
      key.value = True
    elif key.value and route in ("direct-push", "direct-pull") and info.get("ok"):
      key.value = False
    key.display = bool(needs_key)

  def chosen_runs_on(self):
    for b in self.query("#tw-runs-on RadioButton").results(RadioButton):
      if b.value:
        return b.kind
    return None

  @on(RadioSet.Changed, "#tw-runs-on")
  def runs_set(self, event):
    event.stop()
    self.runs_changed()

  def estimate_words(self, route):
    """The estimate of a route in words (with checksums: their time too), "" while it is not known"""
    info = self.plan.get("routes", {}).get(route or "", {})
    checksum = self.query_one("#tw-checksum", Checkbox).value
    est = P.estimate(route, self.plan.get("remaining_bytes"), self.plan.get("remaining_files"), info.get("rate"),
                     self.plan.get("total_bytes"), checksum=checksum) if info.get("rate") else info.get("estimate")
    if not est:
      return ""
    size, files = self.plan.get("remaining_bytes"), self.plan.get("remaining_files")
    what = f" for {human(size)} in {files} file{'s' if files != 1 else ''}" if size is not None else ""
    return (f"about {P.duration(est[0])} – {P.duration(est[1])}{what}" + (" (with checksums: all of it read on both sides)" if checksum else "")
            + (", plus the time in the queue" if self.chosen_runs_on() == "job" else ""))

  def show_flow(self):
    route = self.chosen_route()
    flow = self.query_one("#tw-flow", Static)
    info = self.plan.get("routes", {}).get(route or "", {})
    flow.update(flow_diagram(route, self.chosen_runs_on(), self.src, self.dst, self.src.name if self.src else "?",
                             self.dst.name if self.dst else "?", rate=info.get("rate"), estimate=self.estimate_words(route),
                             measuring=self.checking is not None and not info) if route else "")

  def runs_changed(self):
    self.show_flow()
    self.checksum_note()
    job = self.chosen_runs_on() == "job"
    self.query_one("#tw-job", Vertical).display = job
    route = self.chosen_route()
    client = self.dst if route == "direct-pull" else self.src
    part = self.query_one("#tw-partition", Input)
    if job and not part.value:
      part.value = str(site_of(client).get("transfer_partition") or "")
    if job:
      self.fill_accounts(client)
    self.ensure_node_check()
    self.ensure_job_test()
    self.update_next()

  def fill_accounts(self, cluster):
    """The Slurm accounts of the cluster that runs the job, to pick (looked up once per tab)"""
    if cluster is None:
      return
    def show(accounts):
      if accounts is not None:
        cluster.accounts = list(accounts)
      found = cluster.accounts or []
      pick = self.query_one("#tw-account-list", Select)
      pick.set_options([(a, a) for a in found])
      field = self.query_one("#tw-account", Input)
      from textual.suggester import SuggestFromList
      field.suggester = SuggestFromList(found, case_sensitive=False)
      if not field.value and len(found) == 1: # (one account: that one)
        field.value = found[0]
    if cluster.accounts is not None:
      show(None)
      return
    from ...cluster.jobs import slurm_accounts
    in_background(self, slurm_accounts, show, group="tw-accounts", name="looking up your Slurm accounts",
                  connection=cluster.connection if cluster.destination else None)

  @on(Select.Changed, "#tw-account-list")
  def account_picked(self, event):
    event.stop()
    if event.value not in (None, Select.BLANK):
      self.query_one("#tw-account", Input).value = str(event.value)

  # (a transfer job: its batch script tested with sbatch --test-only as soon as it is chosen, and when its partition,
  # time limit or account change; refused: Next refuses, and says what Slurm said)
  def job_test_key(self):
    route = self.chosen_route()
    if self.chosen_runs_on() != "job" or route is None:
      return None
    q = self.query_one
    return route, q("#tw-partition", Input).value.strip(), q("#tw-time", Input).value.strip(), q("#tw-account", Input).value.strip()

  def ensure_job_test(self):
    key = self.job_test_key()
    if key is not None and key not in self.job_tests:
      self.job_tests[key] = ("running", "testing the job script (sbatch --test-only) …")
      t = self.draft()
      t.route, t.runs_on, t.options = key[0], "job", self.options()
      def done(result):
        self.job_tests[key] = result
        self.show_job_test()
        self.update_next()
      self.app.transfers.test_job(t, key[0], done)
    self.show_job_test()

  def show_job_test(self):
    if not self.is_mounted:
      return
    key = self.job_test_key()
    state, words = self.job_tests.get(key, (None, "")) if key else (None, "")
    mark, style = {"running": (self.app.spinner(), "yellow"), True: ("✓ sbatch --test-only:", "green"),
                   False: ("✗ sbatch --test-only:", "red"), None: ("", "")}[state]
    self.query_one("#tw-job-test", Static).update(Text(f"{mark} {words}", style=style) if words else "")

  # (a direct route as a transfer job: rsync logs in from a node of the partition, not from the login node the check
  # ran on; some sites accept a key only from some hosts: the login is checked there too, before Next)
  def node_check_key(self):
    route = self.chosen_route()
    info = self.plan.get("routes", {}).get(route or "", {})
    if route not in ("direct-push", "direct-pull") or self.chosen_runs_on() != "job" or not info.get("ok"):
      return None
    return route, self.query_one("#tw-partition", Input).value.strip(), self.query_one("#tw-account", Input).value.strip()

  def ensure_node_check(self):
    key = self.node_check_key()
    if key is not None and key[1] and key not in self.node_checks:
      route, partition, account = key
      self.node_checks[key] = ("running", f"checking the login from a node of {partition} (srun) …")
      def done(result):
        self.node_checks[key] = result
        self.show_node_check()
        self.update_next()
      self.app.transfers.probe_node(self.draft(), route, partition, account, done)
    self.show_node_check()

  def show_node_check(self):
    if not self.is_mounted:
      return
    key = self.node_check_key()
    state, words = self.node_checks.get(key, (None, "")) if key else (None, "")
    if key is not None and not key[1]:
      state, words = None, "no partition: the login is not checked from a node of it"
    mark, style = {"running": (self.app.spinner(), "yellow"), True: ("✓", "green"), False: ("✗", "red"), None: ("ⓘ", "dim")}[state]
    self.query_one("#tw-node-check", Static).update(Text(f"{mark} {words}", style=style) if words else "")

  @on(Input.Changed, "#tw-partition, #tw-account, #tw-time")
  def job_place_typed(self, event):
    event.stop()
    if self.node_timer is not None:
      self.node_timer.stop()
    self.node_timer = self.set_timer(1.0, lambda: (self.ensure_node_check(), self.ensure_job_test()))
    self.update_next()

  @on(Checkbox.Changed, "#tw-checksum")
  def checksum_toggled(self, event):
    event.stop()
    self.checksum_note()
    self.show_flow()
    if self.step == 3:
      self.show_options()

  def checksum_note(self):
    note = self.query_one("#tw-checksum-note", Static)
    total = self.plan.get("total_bytes")
    if not self.query_one("#tw-checksum", Checkbox).value:
      note.update("")
      return
    rate = self.plan.get("routes", {}).get(self.chosen_route() or "", {}).get("rate")
    seconds = P.checksum_seconds(total, rate)
    note.update(f"Each run reads all {human(total)} on both sides to compare them" + (f" (about {P.duration(seconds)})" if seconds else "")
                + ", even when nothing changed; rsync shows no progress meanwhile. Size and time find almost every change.")

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
    test = self.job_test_key()
    if test is not None and not test[3]:
      client = self.dst if test[0] == "direct-pull" else self.src
      if client is not None and client.accounts: # (the user has accounts there: the job is charged to one of them)
        return False, f"the account of the job (one of yours on {client.name})"
    if test is not None:
      state, words = self.job_tests.get(test, ("running", ""))
      if state == "running":
        return False, "testing the job script (sbatch --test-only)"
      if state is False:
        return False, f"sbatch --test-only: {words}"
    key = self.node_check_key()
    if key is not None and key[1]:
      state, words = self.node_checks.get(key, ("running", ""))
      if state == "running":
        return False, f"checking the login from a node of {key[1]}"
      if state is False:
        return False, words
    return True, ""

  # ---- 4: options
  def options_ok(self):
    bw = self.query_one("#tw-bwlimit", Input).value.strip()
    if bw:
      try:
        if float(bw) <= 0:
          raise ValueError
      except ValueError:
        return False, "the speed limit: a number of MB/s (or empty)"
    return True, ""

  def show_options(self):
    """The options as they suit the route chosen (compression only for the relay), and the estimate with them"""
    route = self.chosen_route()
    relay = route == "relay"
    compress = self.query_one("#tw-compress", Checkbox)
    compress.disabled = not relay
    if not relay and compress.value:
      compress.value = False
    self.query_one("#tw-compress-note", Static).update("" if relay else "(compression only on the relay: elsewhere it would slow rsync down)")
    self.checksum_note()
    words = self.estimate_words(route) if route else ""
    self.query_one("#tw-options-flow", Static).update(f"⏱ {words}" if words else "")

  @on(Input.Changed, "#tw-bwlimit")
  def bwlimit_typed(self, event):
    event.stop()
    self.update_next()

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
      flow_diagram(t.route, t.runs_on, self.src, self.dst, self.src.name, self.dst.name,
                   rate=self.plan.get("routes", {}).get(t.route, {}).get("rate"), estimate=self.estimate_words(t.route)))
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
    if t.options.get("checksum"):
      est = P.estimate(t.route, self.plan.get("remaining_bytes"), self.plan.get("remaining_files"), info.get("rate"),
                       self.plan.get("total_bytes"), checksum=True)
    row("Estimate", (f"{P.duration(est[0])} – {P.duration(est[1])}" if est else "?") + (" (plus the time it waits in the queue)" if t.runs_on == "job" else "")
        + (" (with checksums: all of it is read on both sides)" if t.options.get("checksum") else ""))
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
    if self.how_ok()[0] and self.options_ok()[0] and self.when_ok()[0]:
      self.dismiss({"transfer": self.build(), "go": True})

  @on(Button.Pressed, "#tw-store")
  def store_pressed(self, event):
    event.stop()
    if self.editing is not None and not self.incomplete():
      self.dismiss({"transfer": self.build(), "go": False, "saved": True})

  @on(Button.Pressed, "#tw-save")
  def save_pressed(self, event):
    event.stop()
    if self.how_ok()[0] and self.options_ok()[0] and self.when_ok()[0]:
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
    """Only a paused transfer continues (mid-run: the run goes on; between runs: its schedule); one that ended runs
    anew with Run now"""
    return t.state == "paused"

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

def short_path(path, width=34):
  """A long path shortened in its middle, at its slashes: /very/long/…/in/its/path (its start and its end stay)"""
  from rich.cells import cell_len
  if cell_len(path) <= width:
    return path
  parts = path.split("/")
  tail, head = [], []
  room = width - 2 # (the "/…" between)
  for part in reversed(parts[1:]): # (the end first: the name says most; then as much of the start as fits)
    if cell_len("/".join([part, *tail])) > room * 0.6 and tail:
      break
    tail.insert(0, part)
  rest = room - cell_len("/".join(tail))
  for part in parts[:len(parts) - len(tail)]:
    if cell_len("/".join([*head, part])) > rest:
      break
    head.append(part)
  out = "/".join(head) + "/…/" + "/".join(tail) if head and head != [""] else "…/" + "/".join(tail)
  if cell_len(out) > width: # (one long name: its end)
    out = "…" + path[-(width - 1):]
  return out

def end_text(cluster, name, paths, width=34):
  """A tab with its path (the first of several: +n) for the list"""
  first = paths[0] if paths else ""
  more = f" +{len(paths) - 1}" if len(paths) > 1 else ""
  return Text.assemble(dot(cluster), f" {name}:", (short_path(first, width - len(more)), "cyan"), (more, "dim"))

COLUMNS = [("name", "Name"), ("from", "From"), ("to", "To"), ("how", "Route"), ("state", "State"), ("progress", "Progress"),
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
    rows = []
    for t in transfers:
      src, dst = self.manager.ends(t)
      p = t.progress
      shown = p.get("percent") if t.state in ("running", "pausing", "paused", "done", "done-errors", "failed", "waiting") else None
      last = t.runs[-1] if t.runs else None
      when = (S.when_text(t.next_run) if t.next_run else
              (f"{S.when_text(last['ended'])} {M.STATES.get(last['result'], last['result'])}" if last else ""))
      rows.append((t.id, [Text(t.name, style="bold"), end_text(src, t.source_name, t.source.get("paths", [])),
                          end_text(dst, t.dest_name, [t.dest.get("path", "")]), route_text(t), state_text(t),
                          Text(f"{self.app.spinner()} comparing", style="yellow") if t.state == "running" and p.get("phase") == "comparing"
                          else bar(shown) if shown is not None else "",
                          rate_text(p.get("rate")) if t.state == "running" else "",
                          P.duration(p.get("eta")) if t.state == "running" and p.get("eta") else "", when]))
    fill_table(table, rows)
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
    text.append(f"\n{t.source_name}:{', '.join(t.source.get('paths', []))}  →  {t.dest_name}:{t.dest.get('path', '')}", style="dim")
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
  #xd-sync { margin-top: 1; padding: 0 1; border: round $panel; }
  #xd-status { padding: 0 1; border: round $panel; text-style: bold; }
  #xd-check-bar { height: auto; margin-bottom: 1; }
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
      yield Static(id="xd-status")
      yield Static(id="xd-head")
      yield Static(id="xd-route")
      yield Static(id="xd-paths")
      yield Static(id="xd-sync")
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
      yield Button("", id="xd-runs", compact=True, tooltip="all its runs: when, how long, how they ended (a click on one: its details)")
      yield Static("The check", classes="section")
      with Horizontal(id="xd-check-bar"):
        yield Button(icons.label("reload", "Check again"), id="xd-recheck", compact=True,
                     tooltip="check it again as it is set up: what remains (in sync?), both sides, the routes and their speed")
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
    status = q("#xd-status", Static)
    status.update(status_text(t, self.manager, self.app.spinner()))
    status.styles.border = ("round", STATUS_BORDER.get(t.state, "grey"))
    self.frame = getattr(self, "frame", 0) + 1
    route = flow_diagram(t.route, t.runs_on, src, dst, t.source_name, t.dest_name, t.run.get("job", ""),
                         moving=self.frame if t.state == "running" else None)
    route.append(f"\n{M.ROUTES.get(t.route, t.route)}, {M.RUNS_ON.get(t.runs_on, t.runs_on)}", style="dim")
    if t.run.get("job"):
      route.append(f", job {t.run['job']}", style="dim")
    q("#xd-route", Static).update(route)
    q("#xd-paths", Static).update(self.paths_text(t))
    q("#xd-pause", Button).display = t.active or t.state in ("scheduled", "waiting")
    q("#xd-resume", Button).display = self.can_continue(t)
    resume = "Continue" if self.manager.paused_mid_run(t) else "Resume schedule"
    if str(q("#xd-resume", Button).label) != icons.label("running", resume):
      q("#xd-resume", Button).label = icons.label("running", resume)
      q("#xd-resume", Button).tooltip = ("a new run: it goes on where the paused one stopped (c)" if resume == "Continue" else
                                         "its schedule goes on, without a run now (c)")
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
    if t.state == "running" and p.get("phase") == "comparing":
      started = t.run.get("started") or time.time()
      words = "rsync compares the files of both sides" + (
        f" by their contents (checksums): it reads all {human(t.plan.get('total_bytes'))} on both sides, also when "
        "nothing changed" if t.options.get("checksum") else "")
      seconds = P.checksum_seconds(t.plan.get("total_bytes"), (t.plan.get("routes", {}).get(t.route) or {}).get("rate")) if t.options.get("checksum") else None
      q("#xd-current", Static).update(Text(f"{self.app.spinner()} {words}; for {P.duration(time.time() - started)}"
                                           + (f" (about {P.duration(seconds)} expected)" if seconds else "")
                                           + ". Progress comes with the files it copies.", style="yellow"))
    else:
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
    q("#xd-runs", Button).label = runs_label(t)
    q("#xd-runs", Button).disabled = not t.runs

    checking = t.id in self.manager.checking
    q("#xd-sync", Static).update(sync_text(t, checking, self.app.spinner()))
    recheck = q("#xd-recheck", Button)
    recheck.disabled = checking or t.active
    recheck.tooltip = ("a run is under way: after it" if t.active else "checking …" if checking else
                       "check it again as it is set up: what remains (in sync?), both sides, the routes and their speed")
    check = plan_text(t.plan, self.app.spinner() if checking else "") if t.plan.get("steps") else Text("no check", style="dim")
    if t.plan.get("ended") and not checking:
      check.append(f" checked {S.when_text(t.plan['ended'])}\n", style="dim")
    if t.plan.get("routes"):
      check.append_text(routes_text(t.plan))
    q("#xd-check", Static).update(check)
    q("#xd-log", Static).update(Text("\n".join(f"{datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S')}  {text}" for ts, text in t.log[-25:]), style="dim"))

  def paths_text(self, t):
    """The sources and the destination, each a link: a click opens it in its tab (a folder, a file, a pattern)"""
    text = Text()
    rows = [("from", t.source_name, p, i) for i, p in enumerate(t.source.get("paths", []))]
    rows.append(("into", t.dest_name, t.dest.get("path", ""), -1))
    for word, name, path, i in rows:
      text.append(f"{word} {name}: ", style="dim")
      text.append(path, style=Style(underline=True) + Style.from_meta({"@click": f"screen.open_end({i})"}))
      text.append("\n")
    text.append("(a click opens it in its tab)", style="dim italic")
    return text

  def action_open_end(self, index):
    """A source (index) or the destination (-1), opened in the tab of its cluster"""
    t = self.transfer
    if t is None:
      return
    src, dst = self.manager.ends(t)
    if index < 0:
      cluster, path = dst, t.dest.get("path", "")
    else:
      paths = t.source.get("paths", [])
      if index >= len(paths):
        return
      cluster, path = src, paths[index]
    if cluster is None:
      self.app.notify(f"the tab {t.dest_name if index < 0 else t.source_name} is not open", title=t.name, severity="warning")
      return
    self.app.open_transfer_path(cluster, path, [t.name])

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
    elif bid == "xd-recheck":
      self.manager.recheck(t)
      self.refresh_view()
    elif bid == "xd-runs" and t.runs:
      self.app.push_screen(TransferRunsScreen(t.id))

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

def sync_state(t):
  """In sync? From the newer of the last check and the last run that ended well: ("yes" | "no" | "unknown", words)"""
  plan = t.plan or {}
  checked = plan.get("done") and plan.get("remaining_bytes") is not None and not plan.get("error")
  checked_at = plan.get("ended") or plan.get("started") or 0
  last = next((r for r in reversed(t.runs) if r["result"] in ("done", "done-errors")), None)
  if last is not None and (not checked or last["ended"] >= checked_at):
    if last["result"] == "done":
      return "yes", f"as of the last run ({S.when_text(last['ended'])}): it copied all there was"
    return "no", (f"the last run ({S.when_text(last['ended'])}) could not copy {last.get('error_count') or 'some'} "
                  "files (its errors)")
  if checked:
    files, size = plan.get("remaining_files") or 0, plan.get("remaining_bytes") or 0
    if not files and not size:
      return "yes", f"as of the check ({S.when_text(checked_at)}): nothing to copy"
    return "no", f"as of the check ({S.when_text(checked_at)}): {human(size)} in {files} file{'s' if files != 1 else ''} to copy"
  return "unknown", "not checked yet, and no run ended well: Check again (below) finds out"

SYNC_LOOK = {"yes": ("● in sync", "bold green"), "no": ("● not in sync", "bold yellow"), "unknown": ("○ in sync?", "bold dim")}

def sync_text(t, checking=False, spinner=""):
  if checking:
    return Text.assemble((f"{spinner} checking …", "bold cyan"), "  what remains, both sides, the routes", style="")
  state, words = sync_state(t)
  mark, style = SYNC_LOOK[state]
  return Text.assemble((mark, style), f"  {words}" + ("; the source may have changed since" if state == "yes" else ""))

STATUS_BORDER = {"running": "green", "starting": "green", "queued": "cyan", "scheduled": "cyan", "checking": "cyan",
                 "waiting": "yellow", "pausing": "yellow", "paused": "yellow", "done": "green", "done-errors": "orange",
                 "failed": "red", "new": "grey"}

def status_text(t, manager, spinner=""):
  """The state of a transfer in big, with what it means now"""
  p = t.progress or {}
  mark = icons.icon(STATE_ICONS.get(t.state, "")) or "●"
  word = M.STATES.get(t.state, t.state).upper()
  last = t.runs[-1] if t.runs else None
  if t.state == "running":
    if p.get("phase") == "comparing":
      more = f"{spinner} comparing the files of both sides"
    else:
      more = (f"{p['percent']:.0f} %" if p.get("percent") is not None else "") + (f" · {rate_text(p.get('rate'))}" if p.get("rate") else "") + (
        f" · {P.duration(p['eta'])} left" if p.get("eta") else "")
  elif t.state in ("starting", "pausing", "checking"):
    more = f"{spinner} {t.message}" if t.message else spinner
  elif t.state == "paused":
    more = ("mid-run: Continue goes on where it stopped" + (f" ({t.message})" if t.message and t.message != "paused" else "")
            if manager.paused_mid_run(t) else "its schedule waits: Resume schedule lets it go on")
  elif t.state == "scheduled":
    more = S.describe(t.schedule, t.next_run)
  elif t.state in ("done", "done-errors", "failed"):
    more = (f"{S.when_text(last['ended'])}" if last else "") + (f": {t.message}" if t.message else "")
  elif t.state == "new":
    more = t.message or ("runs when you click Run now" if (t.schedule or {}).get("kind") == "never" else "Run now starts it")
  else: # (queued, waiting)
    more = t.message
  return Text.assemble((f"{mark} {word}", STATE_STYLES.get(t.state, "bold")), ("  " + more if more else "", ""))

def fill_table(table, rows):
  """Show rows ([(key, [cells])]) in a table that is refreshed while it is looked at: the same rows, new values: the
  cells that changed are updated in place (scrolling and the cursor stay); rows came or went: it is built anew and
  scrolled back to where it was, the cursor on the same row (as the links table of the connection window)"""
  shown = getattr(table, "slurp_shown", None)
  plain = [(key, [str(c) for c in cells]) for key, cells in rows]
  if shown is not None and [k for k, _ in shown] == [k for k, _ in plain] and table.row_count == len(rows):
    columns = [c.key for c in table.ordered_columns]
    for (key, cells), (_, now), (_, before) in zip(rows, plain, shown):
      for column, cell, new, old in zip(columns, cells, now, before):
        if new != old:
          table.update_cell(key, column, cell, update_width=True)
    table.slurp_shown = plain
    return
  x, y = table.scroll_x, table.scroll_y
  cursor_key = None
  if table.row_count and 0 <= table.cursor_row < table.row_count:
    try:
      cursor_key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
    except Exception:
      cursor_key = None
  cursor = table.cursor_row
  table.clear()
  for key, cells in rows:
    table.add_row(*cells, key=key)
  table.slurp_shown = plain
  keys = [k for k, _ in rows]
  if rows:
    row = keys.index(cursor_key) if cursor_key in keys else min(max(cursor, 0), len(rows) - 1)
    table.move_cursor(row=row, scroll=False)
  table.call_after_refresh(table.scroll_to, x, y, animate=False)

def runs_label(t):
  """The Runs field: how many times it ran, how they ended, the last one"""
  if not t.runs:
    return "never ran yet"
  counts = {}
  for r in t.runs:
    counts[r["result"]] = counts.get(r["result"], 0) + 1
  parts = ", ".join(f"{n} {M.STATES.get(k, k)}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
  last = t.runs[-1]
  shown = f" (the last {len(t.runs)}: {parts})" if t.run_count > len(t.runs) else f" ({parts})"
  return (f"ran {t.run_count} time{'s' if t.run_count != 1 else ''}{shown} · last: {S.when_text(last['ended'])} "
          f"{M.STATES.get(last['result'], last['result'])} · show all …")

def took(r):
  return P.duration(max(r["ended"] - r["started"], 0))

def stamp(epoch):
  return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch else "—"

RUN_COLUMNS = [("n", "#"), ("started", "Started"), ("ended", "Ended"), ("took", "Took"), ("result", "Result"),
               ("bytes", "Copied"), ("files", "Files"), ("rate", "Rate"), ("how", "Route"), ("where", "Job / host"),
               ("message", "Message")]

class TransferRunsScreen(TransfersBase):
  """The runs of a transfer, the last first: when, how long, how they ended, how much; a click on one: its details"""
  SHOW_BACK = True
  DEFAULT_CSS = """
  #xr-title { height: auto; padding: 0 1; background: $panel; }
  #xr-list { height: 1fr; }
  """
  BINDINGS = [Binding("escape", "back", "Back")]
  icon_name = "transfer"

  def __init__(self, tid):
    super().__init__()
    self.tid = tid
    self.shown_keys = None

  def compose_left(self):
    yield Static(id="xr-title")
    yield ClickTable(id="xr-list", cursor_type="row", zebra_stripes=True)

  def on_mount(self):
    table = self.query_one("#xr-list", ClickTable)
    for key, label in RUN_COLUMNS:
      table.add_column(label, key=key)
    super().on_mount()
    table.focus()

  def refresh_view(self):
    if not self.is_mounted:
      return
    t = self.manager.get(self.tid)
    if t is None:
      self.query_one("#xr-title", Static).update(Text("This transfer was deleted.", style="dim"))
      return
    self.query_one("#xr-title", Static).update(Text.assemble((t.name, "bold"), f": {runs_label(t).removesuffix(' · show all …')}"
                                                             " · Enter or a click: the details of a run"))
    keys = [str(r["started"]) for r in t.runs]
    if keys == self.shown_keys: # (only when the runs changed: the cursor stays)
      return
    self.shown_keys = keys
    table = self.query_one("#xr-list", ClickTable)
    first = t.run_count - len(t.runs) + 1
    rows = []
    for i, r in reversed(list(enumerate(t.runs))):
      where = (f"job {r['job']}" if r.get("job") else "") or r.get("host", "")
      rows.append((str(r["started"]), [str(r.get("number") or first + i), stamp(r["started"]), stamp(r["ended"]), took(r),
                                       Text(M.STATES.get(r["result"], r["result"]), style=STATE_STYLES.get(r["result"], "")),
                                       human(r.get("bytes")), str(r.get("files") or 0), rate_text(r.get("rate")),
                                       f"{ROUTE_SHORT.get(r.get('route'), r.get('route', ''))} · {RUNS_SHORT.get(r.get('runs_on'), '')}",
                                       where, (r.get("message") or "")[:80]]))
    fill_table(table, rows)

  @on(ClickTable.RowSelected, "#xr-list")
  def selected(self, event):
    event.stop()
    self.app.push_screen(TransferRunScreen(self.tid, float(event.row_key.value)))

  def action_back(self):
    self.app.pop_screen()

class TransferRunScreen(TransfersBase):
  """One run of a transfer: when and how long, how it ended and why, what it copied, where it ran, rsync's statistics,
  its errors, its events"""
  SHOW_BACK = True
  DEFAULT_CSS = """
  #xu-body { height: 1fr; padding: 0 1; }
  #xu-body Static { height: auto; }
  #xu-actions { height: auto; margin: 1 0 0 0; }
  #xu-actions Button { margin-right: 1; }
  """
  BINDINGS = [Binding("escape", "back", "Back")]
  icon_name = "transfer"

  def __init__(self, tid, started):
    super().__init__()
    self.tid, self.started = tid, started

  def compose_left(self):
    with VerticalScroll(id="xu-body"):
      yield Static(id="xu-head")
      with Horizontal(id="xu-actions"):
        yield Button(icons.label("jobs", "Its job"), id="xu-job", compact=True, tooltip="the job window of this run's transfer job")
      yield Static(id="xu-facts")
      yield Static("rsync's statistics", classes="section")
      yield Static(id="xu-stats")
      yield Static("Errors", classes="section", id="xu-errors-title")
      yield Static(id="xu-errors")
      yield Static("Events", classes="section")
      yield Static(id="xu-events")

  def run_and_transfer(self):
    t = self.manager.get(self.tid)
    r = next((r for r in (t.runs if t else []) if r["started"] == self.started), None)
    return t, r

  def refresh_view(self):
    if not self.is_mounted:
      return
    t, r = self.run_and_transfer()
    q = self.query_one
    if r is None:
      q("#xu-head", Static).update(Text("This run is no longer in the history.", style="dim"))
      return
    head = Text.assemble((f"Run {r.get('number') or '?'} of {t.name}", "bold"), "  ",
                         (M.STATES.get(r["result"], r["result"]), STATE_STYLES.get(r["result"], "")))
    if r.get("message"):
      head.append(f" · {r['message']}")
    q("#xu-head", Static).update(head)
    q("#xu-job", Button).display = bool(r.get("job"))
    facts = Text()
    def row(label, value, style=""):
      facts.append(f"{label:12}", style="bold")
      facts.append(f"{value}\n", style=style)
    row("Started", stamp(r["started"]))
    row("Ended", stamp(r["ended"]))
    row("Took", took(r))
    row("Copied", human(r.get("bytes")) + (f" of {human(r['total'])}" if r.get("total") else "") + f", {r.get('files') or 0} files"
        + (f", {rate_text(r['rate'])} on average" if r.get("rate") else ""))
    row("Route", f"{M.ROUTES.get(r.get('route'), r.get('route', ''))}, {M.RUNS_ON.get(r.get('runs_on'), r.get('runs_on', ''))}")
    if r.get("job") or r.get("host"):
      row("Ran on", ", ".join(x for x in (f"job {r['job']}" if r.get("job") else "", r.get("host", "")) if x))
    if r.get("folder"):
      row("Its folder", r["folder"], "dim")
    if r.get("sources"):
      row("From", ", ".join(r["sources"]))
      row("Into", r.get("dest", ""))
    opts = [w for k, w in (("checksum", "checksums"), ("compress", "compressed"), ("temp_key", "temporary key")) if (r.get("options") or {}).get(k)]
    if (r.get("options") or {}).get("bwlimit"):
      opts.append(f"at most {r['options']['bwlimit']} MB/s")
    row("Options", ", ".join(opts) or "—")
    q("#xu-facts", Static).update(facts)
    stats = r.get("stats") or {}
    names = {"files": "files and folders", "transferred": "files copied", "created": "files created", "total_size": "total size",
             "to_transfer": "size of the files copied", "literal": "data sent as is", "sent": "bytes sent", "received": "bytes received"}
    text = Text()
    for key, label in names.items():
      if key in stats:
        value = stats[key]
        text.append(f"{label:26}", style="dim")
        text.append((human(value) if key in ("total_size", "to_transfer", "literal", "sent", "received") else str(value)) + "\n")
    q("#xu-stats", Static).update(text if stats else Text("none (it ended before rsync said them)", style="dim"))
    errors = r.get("errors") or []
    q("#xu-errors-title", Static).display = q("#xu-errors", Static).display = bool(errors or r.get("error_count"))
    q("#xu-errors", Static).update(Text((f"{r.get('error_count')} errors" + (f", the first {len(errors)}:" if len(errors) < (r.get("error_count") or 0) else ":")
                                         + "\n" + "\n".join(errors)), style="yellow"))
    events = r.get("events") or []
    q("#xu-events", Static).update(Text("\n".join(f"{stamp(ts)}  {words}" for ts, words in events), style="dim")
                                   if events else Text("none", style="dim"))

  @on(Button.Pressed, "#xu-job")
  def job_pressed(self, event):
    event.stop()
    t, r = self.run_and_transfer()
    if r is not None and r.get("job"):
      self.app.open_transfer_job(t, r["job"])

  def action_back(self):
    self.app.pop_screen()

def rates(history):
  """[(time, bytes per second)] from the progress history ([[time, bytes]])"""
  out = []
  for (t0, b0), (t1, b1) in zip(history, history[1:]):
    if t1 > t0:
      out.append((t1, max(b1 - b0, 0) / (t1 - t0)))
  return out
