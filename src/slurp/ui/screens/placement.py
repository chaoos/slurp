"""Placement of a running job: a map of its nodes (NUMA domains, cores, GPUs) coloured by task, and its problems"""

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ...cluster.remote import cluster_work
from ...cluster.details import run_script_on_nodes
from ...checks.placement import analyse, ranges

SYMBOLS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
COLORS = ["cyan", "magenta", "green", "yellow", "blue", "red", "bright_cyan", "bright_magenta", "bright_green",
          "bright_yellow", "bright_blue", "bright_red"]

def render_node(node):
  out = Text()
  tasks = sorted(node.tasks.values(), key=lambda t: (t.step, int(t.local) if t.local.isdigit() else 0))
  index = {t.label: i for i, t in enumerate(tasks)}
  owner = {}
  for t in tasks:
    for c in t.allowed:
      owner.setdefault(c, []).append(t)
  now = {}
  for t in tasks:
    for c in t.running:
      now.setdefault(c, []).append(t)
  out.append(f"{node.host}", style="bold")
  out.append(f"   {len(node.cpu_numa)} CPUs, {len(set(node.cpu_numa.values()))} NUMA domains, {len(node.gpus)} GPUs, "
             f"{len(tasks)} tasks\n", style="dim")
  def cell(c, who):
    if not who:
      return ("·", "dim")
    if len(who) > 1:
      return ("#", "bold red")
    i = index[who[0].label]
    return (SYMBOLS[i % len(SYMBOLS)], COLORS[i % len(COLORS)])
  for d in sorted(set(node.cpu_numa.values())):
    cpus = sorted(c for c, n in node.cpu_numa.items() if n == d)
    out.append(f"  NUMA {d:<2} bound ")
    for c in cpus:
      out.append(*cell(c, owner.get(c, [])))
    out.append("\n         now   ")
    for c in cpus:
      out.append(*cell(c, now.get(c, [])))
    gpus = [g for g, (_, n) in sorted(node.gpus.items()) if n == d]
    if gpus:
      users = []
      for g in gpus:
        who = [t.label for t in tasks if g in t.gpus]
        users.append(f"GPU {g}: {', '.join(who) if who else 'idle'}")
      out.append("\n         " + " · ".join(users), style="dim")
    out.append("\n")
  for t in tasks:
    i = index[t.label]
    out.append(f"  {SYMBOLS[i % len(SYMBOLS)]} ", style="bold " + COLORS[i % len(COLORS)])
    out.append(f"task {t.label} {t.comm}: {t.threads} threads, CPUs {ranges(t.allowed) or '?'}"
               + (f", NUMA {','.join(map(str, sorted(node.numa_of(t.allowed))))}" if t.allowed else "")
               + (f", GPUs {','.join(sorted(t.gpus))}" if t.gpus else "") + "\n")
  return out

class PlacementScreen(ModalScreen):
  """Runs placement.sh on the nodes of the job; dismisses with the result (for the checks, N05) or None"""
  DEFAULT_CSS = """
  PlacementScreen { align: center middle; }
  #pl-dialog { width: 95%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #pl-title { text-style: bold; margin: 1 0; }
  #pl-body { height: 1fr; }
  #pl-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #pl-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close")]

  def __init__(self, job):
    super().__init__()
    self.job, self.result = job, None

  def compose(self):
    with Vertical(id="pl-dialog"):
      yield Static(f"Placement of job {self.job.jid}: where its tasks and threads run", id="pl-title")
      with VerticalScroll(id="pl-body"):
        yield Static(Text("asking the nodes …", style="dim"), id="pl-problems")
        yield Static(id="pl-map")
        yield Static(Text("bound: the CPUs a task may run on · now: where its threads are at the moment · "
                          "# several tasks · · none. NUMA guide: F1", style="dim"), id="pl-legend")
      with Horizontal(id="pl-buttons"):
        yield Button("Refresh", id="pl-refresh", compact=True)
        yield Button("Close", id="pl-close", variant="primary", compact=True)

  def on_mount(self):
    self.probe()

  @cluster_work("placement")
  def probe(self):
    tasks, other, err = run_script_on_nodes(self.job, "placement.sh", self.job.rec["JobIDRaw"], timeout=60)
    result = analyse(tasks) if tasks else {"nodes": [], "problems": [], "error": err or "\n".join(other) or "no answer from the nodes"}
    self.app.call_from_thread(self.show, result)

  def show(self, result):
    self.result = result
    problems = Text()
    if result.get("error"):
      problems.append(f"the nodes did not answer: {result['error'][:300]}", style="red")
    elif result["problems"]:
      for p in result["problems"]:
        problems.append(f"⚠ {p}\n", style="yellow")
    else:
      problems.append("✓ no placement problems found\n", style="green")
    self.query_one("#pl-problems", Static).update(problems)
    body = Text()
    for node in result["nodes"]:
      body.append_text(render_node(node))
      body.append("\n")
    self.query_one("#pl-map", Static).update(body)

  @on(Button.Pressed, "#pl-refresh")
  def refresh_pressed(self, event):
    event.stop()
    self.query_one("#pl-problems", Static).update(Text("asking the nodes …", style="dim"))
    self.probe()

  @on(Button.Pressed, "#pl-close")
  def action_close(self):
    self.dismiss(self.result if self.result and not self.result.get("error") else None)
