"""Rules of the categories (see cluster/categories.py): the window that lists them, with how many jobs of the job list
each matches (Apply now puts them in), and the form that makes or changes one, which shows its matches as it is typed"""

import time
import uuid

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, RadioButton, RadioSet, Static

from ...cluster import categories as cat
from .. import icons

SCOPE_LABELS = {
  "new": "New jobs only: those submitted from now on",
  "uncategorized": "Jobs in no category: those of the job list now, and the new ones",
  "all": "All jobs: also moved out of another category, now and the new ones",
}
SCOPE_SHORT = {"new": "new jobs", "uncategorized": "in no category", "all": "all (moves)"}

def job_pairs(cluster):
  """The jobs of the job list as the rules see them: [(jid, name)]"""
  return [(job.jid, job.rec.get("JobName") or "") for job in cluster.jobs.values()]

def newest(pairs):
  """The highest job number of the job list (a rule for new jobs takes the ones above it)"""
  return max((n for n in (cat.job_number(j) for j, _ in pairs) if n is not None), default=0)

def count_text(count, category):
  """"12 jobs of the job list: 8 in CI already, 3 in no category, 1 in another" """
  n = count["match"]
  if not n:
    return Text("matches no job of the job list", style="dim")
  parts = [(f"{count['there']} in {category} already", "green"), (f"{count['none']} in no category", "yellow"),
           (f"{count['other']} in another category", "magenta")]
  out = Text(f"matches {n} job{'s' if n != 1 else ''} of the job list: ")
  shown = [(t, style) for (t, style), k in zip(parts, ("there", "none", "other")) if count[k]]
  for i, (t, style) in enumerate(shown):
    out.append(", " if i else "")
    out.append(t, style=style)
  return out

class RuleFormScreen(ModalScreen[dict | None]):
  """A rule, new or changed: the pattern of job names, the category, what it takes, on or off; under it what it
  matches in the job list now. Dismissed with the rule (as categories.rule_entry takes it) or None"""
  DEFAULT_CSS = """
  RuleFormScreen { align: center middle; }
  #rf-dialog { width: 96; max-width: 96%; height: auto; max-height: 95%; border: thick $accent; background: $surface; padding: 1 2; }
  #rf-title { text-style: bold; margin-bottom: 1; }
  .rf-label { height: auto; margin-top: 1; text-style: bold; }
  .rf-note { height: auto; color: $text-muted; }
  #rf-scope { width: 100%; height: auto; }
  #rf-preview { height: auto; margin-top: 1; padding: 0 1; background: $boost; }
  #rf-error { height: auto; color: $error; }
  #rf-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #rf-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "save", "Save", priority=True)]

  def __init__(self, store, pairs, rule=None, category=""):
    super().__init__()
    self.store, self.pairs, self.rule = store, pairs, rule
    self.category = rule["category"] if rule else category

  def compose(self) -> ComposeResult:
    r = self.rule or {}
    with Vertical(id="rf-dialog"):
      yield Static("Change the rule" if self.rule else "New rule: jobs into a category by their name", id="rf-title", markup=False)
      yield Static("Job names that match", classes="rf-label")
      yield Input(r.get("pattern", ""), id="rf-pattern", placeholder="ci-[0-9]+")
      yield Static("A regular expression for the whole name: ci-[0-9]+ takes ci-1234, not ci-1234-b or my-ci-1; "
                   "ci-.* takes all that start with ci-; (train|eval)_.* either.", classes="rf-note", markup=False)
      yield Static("go into the category", classes="rf-label")
      yield Input(self.category, id="rf-category", placeholder="CI  (a/b: b in a; made if it is new)")
      yield Static("Which jobs", classes="rf-label")
      with RadioSet(id="rf-scope"):
        for key, label in SCOPE_LABELS.items():
          yield RadioButton(label, value=key == r.get("scope", "new"), id=f"rf-scope-{key}")
      yield Static("Each job is looked at once, when the job list loads: one taken out of the category by hand is not "
                   "put back. Of several rules, the first that matches a job has it.", classes="rf-note", markup=False)
      yield Checkbox("On (off: kept, but it does nothing)", r.get("on", True), id="rf-on")
      yield Static(id="rf-preview")
      yield Static(id="rf-error")
      with Horizontal(id="rf-buttons"):
        yield Button("Cancel", id="rf-cancel", compact=True)
        yield Button("Save", id="rf-ok", variant="primary", compact=True)

  def on_mount(self):
    self.query_one("#rf-pattern", Input).focus()
    self.preview()

  def scope(self):
    pressed = self.query_one("#rf-scope", RadioSet).pressed_button
    return pressed.id.removeprefix("rf-scope-") if pressed is not None else "new"

  def entry(self):
    """The rule of the form (ValueError: what is wrong). Its "after": kept if it stays the same rule; a new or
    changed one starts again (new jobs: those above the newest of the job list; else all of them)"""
    r = self.rule or {}
    entry = {"id": r.get("id") or uuid.uuid4().hex[:8], "pattern": self.query_one("#rf-pattern", Input).value.strip(),
             "category": self.query_one("#rf-category", Input).value, "scope": self.scope(),
             "on": self.query_one("#rf-on", Checkbox).value, "made": r.get("made") or time.strftime("%Y-%m-%dT%H:%M:%S")}
    rule = cat.rule_entry(dict(entry, after=0))
    same = r and all(r.get(k) == rule[k] for k in ("pattern", "category", "scope")) and (r.get("on") or not rule["on"])
    rule["after"] = r.get("after", 0) if same else newest(self.pairs) if rule["scope"] == "new" else 0
    return rule

  @on(Input.Changed)
  @on(RadioSet.Changed)
  @on(Checkbox.Changed)
  def preview(self, event=None):
    out = self.query_one("#rf-preview", Static)
    self.query_one("#rf-error", Static).update("")
    pattern = self.query_one("#rf-pattern", Input).value.strip()
    if not pattern:
      out.update(Text("type a pattern: what it matches in the job list shows here", style="dim"))
      return
    try:
      rule = cat.rule_entry({"id": "preview", "pattern": pattern, "category": self.query_one("#rf-category", Input).value or "?",
                             "scope": self.scope()})
    except ValueError as e:
      out.update(Text(str(e), style="red"))
      return
    count = self.store.rule_count(rule, self.pairs)
    text = count_text(count, rule["category"])
    names = sorted({n for j, n in self.pairs if cat.rule_matches(rule, n)})
    if names:
      text.append("\ne.g. " + ", ".join(names[:5]) + (" …" if len(names) > 5 else ""), style="dim")
    takes = {"new": count["none"] + count["other"] and "none of these (they are not new): Apply now in the rules window puts them in",
             "uncategorized": count["none"] and f"the {count['none']} in no category, when the job list loads next",
             "all": count["none"] + count["other"] and f"these {count['none'] + count['other']}, when the job list loads next"}[rule["scope"]]
    if takes:
      text.append(f"\nit takes {takes}")
    out.update(text)

  @on(Button.Pressed, "#rf-ok")
  @on(Input.Submitted)
  def action_save(self, event=None):
    if event is not None:
      event.stop()
    try:
      rule = self.entry()
    except ValueError as e:
      self.query_one("#rf-error", Static).update(str(e))
      return
    self.dismiss(rule)

  @on(Button.Pressed, "#rf-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

class ApplyRuleScreen(ModalScreen[str | None]):
  """Apply a rule now to the jobs of the job list: the ones in no category, or also those in another one.
  Dismissed with "none", "move" or None"""
  DEFAULT_CSS = """
  ApplyRuleScreen { align: center middle; }
  #ar-dialog { width: 84; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
  #ar-title { text-style: bold; margin-bottom: 1; }
  #ar-text { height: auto; margin-bottom: 1; }
  #ar-buttons { height: auto; align-horizontal: right; }
  #ar-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, rule, count):
    super().__init__()
    self.rule, self.count = rule, count

  def compose(self) -> ComposeResult:
    c, path = self.count, self.rule["category"]
    with Vertical(id="ar-dialog"):
      yield Static(f"Apply {cat.rule_text(self.rule)} now", id="ar-title", markup=False)
      text = count_text(c, path)
      text.append("\nTo the jobs of the job list (queued, and those finished since …), whatever the rule has seen "
                  "already.", style="dim")
      yield Static(text, id="ar-text")
      with Horizontal(id="ar-buttons"):
        yield Button("Cancel", id="ar-cancel", compact=True)
        if c["other"]:
          yield Button(f"Also move the {c['other']} in another", id="ar-move", variant="warning", compact=True)
        yield Button(f"Put the {c['none']} in no category in {path}", id="ar-none", variant="primary", compact=True,
                     disabled=not c["none"])

  @on(Button.Pressed, "#ar-none")
  def none(self, event):
    event.stop()
    self.dismiss("none")

  @on(Button.Pressed, "#ar-move")
  def move(self, event):
    event.stop()
    self.dismiss("move")

  @on(Button.Pressed, "#ar-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)

class RulesScreen(ModalScreen):
  """The rules of the categories of the tab's cluster: what each matches in the job list now; New, Edit, Delete,
  Apply now"""
  DEFAULT_CSS = """
  RulesScreen { align: center middle; }
  #ru-dialog { width: 120; max-width: 98%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #ru-title { text-style: bold; }
  #ru-note { height: auto; color: $text-muted; margin-bottom: 1; }
  #ru-table { height: auto; max-height: 16; }
  #ru-detail { height: auto; margin-top: 1; }
  #ru-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #ru-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("n", "new", "New rule"), Binding("e", "edit", "Edit"),
              Binding("delete", "delete", "Delete"), Binding("a", "apply", "Apply now"), Binding("space", "toggle", "On / off")]

  def __init__(self, category=""):
    super().__init__()
    self.category = category # (a new rule: into the category selected in the categories window)

  @property
  def cluster(self):
    return self.app.cluster

  @property
  def store(self):
    return self.cluster.categories or cat.Store(self.cluster.slurm_cluster or "default")

  def compose(self) -> ComposeResult:
    with Vertical(id="ru-dialog"):
      yield Static("Rules of the categories", id="ru-title")
      yield Static("They put jobs in a category by their name, when the job list loads; each job once. The first rule "
                   "that matches a job has it. Kept on the cluster, with the categories.", id="ru-note", markup=False)
      yield DataTable(id="ru-table", cursor_type="row", zebra_stripes=True)
      yield Static(id="ru-detail")
      with Horizontal(id="ru-buttons"):
        yield Button(icons.label("new_script", "New…"), id="ru-new", compact=True, tooltip="a new rule (n)")
        yield Button(icons.label("edit", "Edit…"), id="ru-edit", compact=True, tooltip="the rule selected (e, Enter)")
        yield Button("Delete", id="ru-delete", variant="error", compact=True, tooltip="the rule selected; the jobs it put in stay (Del)")
        yield Button("Apply now…", id="ru-apply", variant="primary", compact=True,
                     tooltip="the rule selected on all the jobs of the job list now (a)")
        yield Button("Close", id="ru-close", compact=True)

  def on_mount(self):
    table = self.query_one("#ru-table", DataTable)
    for key, label in (("on", "On"), ("pattern", "Job names"), ("category", "Category"), ("scope", "Takes"), ("match", "Matches now")):
      table.add_column(label, key=key)
    self.show()
    table.focus()

  def show(self, select=None):
    table = self.query_one("#ru-table", DataTable)
    current = select or self.selected()
    table.clear()
    store, pairs = self.store, job_pairs(self.cluster)
    for rule in store.rules:
      count = store.rule_count(rule, pairs)
      table.add_row(Text("●", style="green") if rule["on"] else Text("○ off", style="dim"), rule["pattern"], rule["category"],
                    SCOPE_SHORT[rule["scope"]], Text(f"{count['match']}" + (f" ({count['none'] + count['other']} not in it)"
                                                                            if count["none"] + count["other"] else ""),
                                                     style="" if count["match"] else "dim"), key=rule["id"])
    if current is not None and store.rule(current) is not None:
      table.move_cursor(row=table.get_row_index(current))
    for b in ("#ru-edit", "#ru-delete", "#ru-apply"):
      self.query_one(b, Button).disabled = not store.rules
    self.show_detail()

  def selected(self):
    table = self.query_one("#ru-table", DataTable)
    if not table.row_count:
      return None
    return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

  @on(DataTable.RowHighlighted, "#ru-table")
  def show_detail(self, event=None):
    out = self.query_one("#ru-detail", Static)
    rule = self.store.rule(self.selected() or "")
    if rule is None:
      out.update(Text("No rules yet. New… makes one: e.g. the jobs named ci-[0-9]+ into the category CI.", style="dim"))
      return
    count = self.store.rule_count(rule, job_pairs(self.cluster))
    text = Text.assemble((cat.rule_text(rule), "bold"), f"  ({SCOPE_LABELS[rule['scope']].split(':')[0].lower()}"
                         + ("" if rule["on"] else ", off") + ")\n")
    text.append_text(count_text(count, rule["category"]))
    if rule["after"]:
      text.append(f"\nhas looked at the jobs up to {rule['after']}; it takes newer ones", style="dim")
    if rule.get("made"):
      text.append(f"\nmade {rule['made'].replace('T', ' ')[:16]}", style="dim")
    out.update(text)

  def change(self, op, what, select=None, said=None):
    from .categories import change_categories
    def done(store, result):
      self.show(select)
      if said:
        self.notify(said(result), title=self.cluster.name)
      if self.app.main is not None:
        self.app.call_later(self.app.populate) # (the job list's column)
    change_categories(self.app, self.cluster, op, done, what)

  @on(Button.Pressed, "#ru-new")
  def action_new(self, event=None):
    def saved(rule):
      if rule:
        self.change(["set_rule", rule], "making a rule", rule["id"])
    self.app.push_screen(RuleFormScreen(self.store, job_pairs(self.cluster), category=self.category), saved)

  @on(Button.Pressed, "#ru-edit")
  @on(DataTable.RowSelected, "#ru-table")
  def action_edit(self, event=None):
    rule = self.store.rule(self.selected() or "")
    if rule is None:
      return
    def saved(new):
      if new and new != rule:
        self.change(["set_rule", new], "changing a rule", new["id"])
    self.app.push_screen(RuleFormScreen(self.store, job_pairs(self.cluster), rule=dict(rule)), saved)

  def action_toggle(self):
    rule = self.store.rule(self.selected() or "")
    if rule is None:
      return
    new = dict(rule, on=not rule["on"])
    if new["on"] and new["scope"] == "new": # (on again: new jobs are those from now on)
      new["after"] = max(rule["after"], newest(job_pairs(self.cluster)))
    self.change(["set_rule", new], "turning a rule on or off", rule["id"])

  @on(Button.Pressed, "#ru-delete")
  def action_delete(self, event=None):
    rule = self.store.rule(self.selected() or "")
    if rule is None:
      return
    from .dialogs import ConfirmScreen
    def done(yes):
      if yes:
        self.change(["drop_rule", rule["id"]], "deleting a rule")
    self.app.push_screen(ConfirmScreen(f"Delete the rule {cat.rule_text(rule)}?", "The jobs it put in stay in the category.",
                                       "Delete"), done)

  @on(Button.Pressed, "#ru-apply")
  def action_apply(self, event=None):
    rule = self.store.rule(self.selected() or "")
    if rule is None:
      return
    pairs = job_pairs(self.cluster)
    count = self.store.rule_count(rule, pairs)
    if not count["none"] + count["other"]:
      self.notify(f"nothing to do: the {count['match']} jobs it matches are in {rule['category']} already" if count["match"]
                  else "it matches no job of the job list", title=cat.rule_text(rule))
      return
    def chosen(how):
      if how:
        self.change(["apply_rule", rule["id"], pairs, how == "move"], "applying a rule", rule["id"],
                    said=lambda jids: f"{len(jids or [])} job{'s' if len(jids or []) != 1 else ''} put in {rule['category']}")
    self.app.push_screen(ApplyRuleScreen(rule, count), chosen)

  @on(Button.Pressed, "#ru-close")
  def action_close(self, event=None):
    self.dismiss()
