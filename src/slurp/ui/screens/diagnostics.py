"""Runtime diagnostics of a running job: on which node (or all), then what the job does there now: memory, CPU, GPUs,
I/O, local disks, its busiest processes (scripts/memory_probe.sh "full", measured over one second)"""

from datetime import datetime

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, RadioButton, RadioSet, Static

from ...cluster.details import runtime_diagnostics
from ...cluster.remote import in_background
from .. import icons
from ..widgets import crumb_title
from .base import SplitScreen

ALL = "" # (the choice of all nodes)


class NodeChoiceScreen(ModalScreen):
  """On which node of the job (or all of them): radio buttons; dismissed with the node, ALL, or None"""
  DEFAULT_CSS = """
  NodeChoiceScreen { align: center middle; }
  #nc-dialog { width: 70; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #nc-title { text-style: bold; margin-bottom: 1; }
  #nc-nodes { height: auto; max-height: 16; width: 1fr; margin-bottom: 1; }
  #nc-buttons { height: auto; align-horizontal: right; }
  #nc-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, title, nodes, node=ALL, action="Query"):
    super().__init__()
    self.title_text, self.nodes, self.action = title, list(nodes), action
    self.node = node if node in self.nodes else ALL

  def compose(self) -> ComposeResult:
    with Vertical(id="nc-dialog"):
      yield Static(self.title_text, id="nc-title", markup=False)
      with RadioSet(id="nc-nodes"):
        yield RadioButton(f"All nodes ({len(self.nodes)})", value=self.node == ALL, name="*")
        for name in self.nodes:
          yield RadioButton(name, value=name == self.node, name=name)
      with Horizontal(id="nc-buttons"):
        yield Button("Cancel", id="nc-cancel", compact=True)
        yield Button(self.action, id="nc-ok", variant="primary", compact=True)

  def on_mount(self):
    nodes = self.query_one("#nc-nodes", RadioSet)
    try: # (the keys start at the choice, not at the first: Textual has no public way to say so)
      nodes._selected = 0 if self.node == ALL else self.nodes.index(self.node) + 1
    except (AttributeError, ValueError):
      pass
    nodes.focus()

  @on(RadioSet.Changed, "#nc-nodes")
  def node_changed(self, event):
    event.stop()
    self.node = ALL if event.pressed.name == "*" else event.pressed.name

  @on(Button.Pressed, "#nc-ok")
  def ok(self, event):
    event.stop()
    self.dismiss(self.node)

  @on(Button.Pressed, "#nc-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)


def size(kib):
  from .job import memory_text
  return memory_text(kib * 1024)


def node_text(name, d):
  """What the job does on one node, in sections; what looks wrong highlighted"""
  out = Text()
  def section(title):
    out.append(f"\n{title}\n", style="bold")
  def line(text, style=""):
    out.append(f"  {text}\n", style=style)
  out.append(name, style="bold underline")
  out.append("\n")
  # memory
  section("Memory")
  if d["cgroup"]:
    used, _anon, cache = d["cgroup"]
    limit = d.get("limit")
    share = used / limit if limit else None
    text = f"job (its cgroup, as Slurm counts it): {size(used)}"
    text += f" of {size(limit)} limit ({share:.0%})" if limit else " (no limit)"
    text += f" · {size(cache)} of it page cache"
    line(text, "bold yellow" if share and share > 0.9 else "")
  if d["events"]:
    kills, at_max, _high = d["events"]
    if kills:
      line(f"out-of-memory kills in the job: {kills} (processes were killed for lack of memory)", "bold red")
    elif at_max:
      line(f"the job reached its memory limit {at_max} times (the kernel had to reclaim memory)", "yellow")
  procs = [f"{size(v)} {k}" for k, v in (("PSS", d["pss_kib"]), ("RSS", d["cpu_kib"])) if v is not None]
  if procs:
    line("processes: " + ", ".join(procs))
  if d["node"]:
    total, available, _free, cache = d["node"]
    line(f"node (as free): used {size(total - available)} of {size(total)}, available {size(available)}, buff/cache {size(cache)}")
  if d["swap"] and d["swap"][0]:
    swap_total, swap_free = d["swap"]
    used = swap_total - swap_free
    line(f"swap: {size(used)} of {size(swap_total)} used", "yellow" if used > 0.1 * swap_total else "")
  # CPU
  section("CPU")
  if d["job_cpu"]:
    cores, cpus = d["job_cpu"]
    if cpus:
      share = cores / cpus
      line(f"job: {cores:.1f} of its {cpus} CPUs busy ({share:.0%})" + (" · most of its CPUs are idle" if share < 0.5 else ""),
           "yellow" if share < 0.5 else "")
    else:
      line(f"job: {cores:.1f} CPUs busy")
  if d["node_cpu"]:
    busy, ncpu = d["node_cpu"]
    load = d["loadavg"] or []
    line(f"node: {busy}% of {ncpu} CPUs busy" + (f" · load {load[0]} / {load[1]} / {load[2]} (1 / 5 / 15 min), "
                                                  f"{load[3]} running" if len(load) == 4 else ""))
  # GPUs
  if d["gpus"] or d["gpu_load"] or d["gpu_error"]:
    section("GPUs")
    if d["gpu_error"]:
      line(d["gpu_error"], "dim")
    if d["gpu_bytes"] is not None:
      line(f"job's processes: {size(d['gpu_bytes'] // 1024)}" + (" (its GPUs: the driver does not report them per process)"
                                                                  if d.get("gpu_how") == "its GPUs" else ""))
    load = {g["index"]: g for g in d["gpu_load"]}
    for idx, gname, used, total in d["gpus"]:
      g = load.get(idx, {})
      parts = [f"GPU {idx} {gname}: {size(used // 1024)} / {size(total // 1024)}"]
      if g.get("util") is not None:
        parts.append(f"busy {g['util']:.0f}%")
      if g.get("mem_util") is not None:
        parts.append(f"memory bandwidth {g['mem_util']:.0f}%")
      if g.get("temp") is not None:
        parts.append(f"{g['temp']:.0f} °C")
      if g.get("power") is not None:
        parts.append(f"{g['power']:.0f}" + (f" / {g['power_limit']:.0f} W" if g.get("power_limit") else " W"))
      if g.get("clock") is not None:
        parts.append(f"{g['clock']:.0f} MHz")
      idle = g.get("util") is not None and g["util"] < 20
      line(" · ".join(parts) + (" · hardly used" if idle else ""), "yellow" if idle else "")
    for row in d["amd_load"][:8]:
      line(row, "dim")
  # I/O, disks
  if d["job_io"]:
    read, written = d["job_io"]
    section("I/O of the job")
    line(f"read {size(read / 1024)}/s, written {size(written / 1024)}/s")
  if d["disks"]:
    section("Local disks")
    for path, total, used, _avail in d["disks"]:
      note = " (counts as memory)" if path == "/dev/shm" else ""
      line(f"{path}: {size(used)} of {size(total)} used{note}", "yellow" if total and used > 0.9 * total else "")
  # processes
  if d["procs"]:
    section("Busiest processes of the job")
    line(f"{'PID':>8} {'CPU %':>6} {'RSS':>10} {'threads':>7}  state  command", "dim")
    for p in d["procs"]:
      waiting = p["state"] == "D"
      line(f"{p['pid']:>8} {p['cpu']:>6.0f} {size(p['rss_kib']):>10} {p['threads']:>7}  {p['state']:<5}  {p['cmd'] or p['name']}"
           + ("  (waits for I/O)" if waiting else ""), "yellow" if waiting else "")
  return out


def diagnostics_text(result):
  if "error" in result:
    return Text(f"no diagnostics: {result['error']}", style="bold red")
  out = Text()
  for name, d in sorted(result["nodes"].items()):
    if out:
      out.append("\n")
    out.append_text(node_text(name, d))
  return out


class DiagnosticsScreen(SplitScreen):
  """The runtime diagnostics of a job on a node (or all of them): queried when opened and with Again"""
  SHOW_BACK = True
  icon_name = "health"
  BINDINGS = [Binding("escape", "back", "Back"), Binding("r", "again", "Again")]
  DEFAULT_CSS = """
  #diag-bar { height: auto; margin: 1 0 0 0; }
  #diag-bar Button { margin-right: 1; }
  #diag-status { height: auto; padding: 0 1; }
  #diag-body { height: 1fr; padding: 0 1; }
  """

  def __init__(self, job, node=ALL, done=None):
    super().__init__()
    self.job, self.node, self.done = job, node, done
    self.crumbs = ["Jobs", job.jid, "diagnostics" + (f" {node}" if node else "")]

  def compose_left(self):
    with Horizontal(id="diag-bar"):
      yield Button(icons.label("reload", "Again"), id="diag-again", compact=True, tooltip="measure again (r)")
    yield Static(id="diag-status")
    with VerticalScroll(id="diag-body"):
      yield Static(id="diag-text")

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.action_again()

  @on(Button.Pressed, "#diag-again")
  def again_pressed(self, event):
    event.stop()
    self.action_again()

  def action_again(self):
    where = self.node or "all nodes of the job"
    self.query_one("#diag-status", Static).update(Text(f"measuring on {where} … (about 2 s)", style="dim"))
    self.query_one("#diag-again", Button).disabled = True
    job, node = self.job, self.node or None
    in_background(self, lambda: runtime_diagnostics(job, node), self.measured, group="diagnostics",
                  name=f"diagnostics of job {job.jid}")

  def measured(self, result):
    self.query_one("#diag-again", Button).disabled = False
    if result is None:
      return
    where = self.node or f"all {len(result.get('nodes', {}))} nodes"
    self.query_one("#diag-status", Static).update(Text(
      f"job {self.job.jid} on {where} · measured at {datetime.now():%H:%M:%S}, over one second · r: again", style="dim"))
    self.query_one("#diag-text", Static).update(diagnostics_text(result))
    if self.done is not None:
      self.done(result)

  def action_back(self):
    self.pop_back()
