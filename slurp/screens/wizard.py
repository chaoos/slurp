"""New job script: pick a kind of job, get a commented script with the cluster's real numbers, checked"""

import os

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from .. import checks, templates
from ..remote import cluster_work, fs_exists, fs_write, run_cmd

class NewScriptScreen(ModalScreen):
  """Dismisses with the path of the new script (or None)"""
  DEFAULT_CSS = """
  NewScriptScreen { align: center middle; }
  #ns-dialog { width: 120; max-width: 98%; height: 94%; border: thick $accent; background: $surface; padding: 0 1; }
  #ns-title { text-style: bold; margin: 1 0 0 0; }
  .ns-row { height: auto; margin-top: 1; }
  .ns-row Label { padding: 1 1 0 0; width: auto; }
  #ns-kind { width: 60; }
  #ns-account, #ns-partition { width: 26; margin-right: 2; }
  #ns-nodes, #ns-tpn, #ns-cpt, #ns-gpn { width: 9; margin-right: 1; }
  #ns-time { width: 15; margin-right: 1; }
  #ns-name { width: 22; margin-right: 2; }
  #ns-path { width: 1fr; }
  #ns-preview { height: 1fr; margin-top: 1; }
  #ns-note { height: auto; }
  #ns-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #ns-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, directory):
    super().__init__()
    self.directory = directory
    self.facts = None

  def compose(self):
    with Vertical(id="ns-dialog"):
      yield Static("New job script", id="ns-title")
      yield Static(Text("Pick what kind of job; the numbers come from the cluster (change them as you need). The script "
                        "is commented line by line and checked when it opens.", style="dim"))
      with Horizontal(classes="ns-row"):
        yield Label("kind")
        yield Select([(text, key) for key, text in templates.KINDS], value="hybrid", allow_blank=False, id="ns-kind")
      with Horizontal(classes="ns-row"):
        yield Label("account")
        yield Select([], prompt="default", id="ns-account")
        yield Label("partition")
        yield Select([], prompt="default", id="ns-partition")
        yield Label("time")
        yield Input("01:00:00", id="ns-time")
      with Horizontal(classes="ns-row"):
        yield Label("nodes")
        yield Input("1", id="ns-nodes", type="integer")
        yield Label("tasks/node")
        yield Input("1", id="ns-tpn", type="integer")
        yield Label("CPUs/task")
        yield Input("1", id="ns-cpt", type="integer")
        yield Label("GPUs/node")
        yield Input("0", id="ns-gpn", type="integer")
      with Horizontal(classes="ns-row"):
        yield Label("name")
        yield Input("myjob", id="ns-name")
        yield Label("file")
        yield Input(os.path.join(self.directory, "myjob.sh"), id="ns-path")
      yield Static(Text("asking the cluster for its partitions …", style="dim"), id="ns-note")
      yield TextArea(id="ns-preview", read_only=True, show_line_numbers=True, soft_wrap=False)
      with Horizontal(id="ns-buttons"):
        yield Button("Cancel", id="ns-cancel")
        yield Button("Create", id="ns-create", variant="primary")

  def on_mount(self):
    self.load_facts()
    self.refresh_preview()

  @cluster_work("facts")
  def load_facts(self):
    facts = checks.gather_facts()
    self.app.call_from_thread(self.facts_loaded, facts)

  def facts_loaded(self, facts):
    self.facts = facts
    self.query_one("#ns-account", Select).set_options([(a, a) for a in facts.accounts])
    if facts.accounts:
      self.query_one("#ns-account", Select).value = facts.accounts[0]
    parts = sorted(facts.partitions.values(), key=lambda p: (not p.default, p.name))
    self.query_one("#ns-partition", Select).set_options(
      [(f"{p.name} ({p.cpus} CPUs{f', {p.gpus} GPUs' if p.gpus else ''}, max {checks.time_text(p.max_minutes)})", p.name) for p in parts])
    if parts:
      self.query_one("#ns-partition", Select).value = parts[0].name
    self.apply_defaults()

  def partition(self):
    value = self.query_one("#ns-partition", Select).value
    if self.facts is None:
      return None
    return self.facts.partition(value if isinstance(value, str) else None)

  def apply_defaults(self):
    kind = self.query_one("#ns-kind", Select).value
    p = self.partition()
    d = templates.defaults(kind, p, (self.facts.site.get("numa_domains") if self.facts else 0) or 0)
    for wid, key in (("#ns-nodes", "nodes"), ("#ns-tpn", "ntasks_per_node"), ("#ns-cpt", "cpus_per_task"), ("#ns-gpn", "gpus_per_node")):
      self.query_one(wid, Input).value = str(d[key])
    self.query_one("#ns-time", Input).value = d["time"]
    note = Text()
    if p:
      note.append(f"{p.name}: {p.cpus} CPUs ({p.sockets or '?'} sockets × {p.cores or '?'} cores × {p.threads} threads), "
                  f"{p.gpus} GPUs, {checks.mem_text(p.mem_mb)} per node, at most {checks.time_text(p.max_minutes)}", style="dim")
    self.query_one("#ns-note", Static).update(note)
    self.refresh_preview()

  @on(Select.Changed, "#ns-kind")
  @on(Select.Changed, "#ns-partition")
  def kind_changed(self, event):
    self.apply_defaults()

  @on(Input.Changed)
  @on(Select.Changed, "#ns-account")
  def changed(self, event):
    if getattr(event, "input", None) is not None and event.input.id == "ns-name":
      path = self.query_one("#ns-path", Input)
      directory = os.path.dirname(path.value) or self.directory
      path.value = os.path.join(directory, f"{event.value or 'job'}.sh")
    self.refresh_preview()

  def text(self):
    def num(wid, default):
      try:
        return int(self.query_one(wid, Input).value)
      except ValueError:
        return default
    account = self.query_one("#ns-account", Select).value
    partition = self.query_one("#ns-partition", Select).value
    return templates.make_script(
      self.query_one("#ns-kind", Select).value, self.query_one("#ns-name", Input).value or "myjob",
      account if isinstance(account, str) else "", partition if isinstance(partition, str) else "",
      num("#ns-nodes", 1), num("#ns-tpn", 1), num("#ns-cpt", 1), num("#ns-gpn", 0),
      self.query_one("#ns-time", Input).value or "01:00:00", site=self.facts.site if self.facts else {})

  def refresh_preview(self):
    try:
      self.query_one("#ns-preview", TextArea).load_text(self.text())
    except Exception:
      pass

  @on(Button.Pressed, "#ns-create")
  def create_pressed(self, event):
    event.stop()
    self.create(self.query_one("#ns-path", Input).value, self.text())

  @cluster_work("create")
  def create(self, path, text):
    try:
      if fs_exists(path):
        raise OSError(f"{path} exists already: pick another name")
      fs_write(path, text, exclusive=True)
      run_cmd(["mkdir", "-p", os.path.join(os.path.dirname(path), "logs")], retry=False)
    except OSError as e:
      self.app.call_from_thread(self.notify, str(e), severity="error")
      return
    self.app.call_from_thread(self.dismiss, path)

  @on(Button.Pressed, "#ns-cancel")
  def action_cancel(self):
    self.dismiss(None)
