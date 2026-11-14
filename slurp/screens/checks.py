"""The findings of the checks (a list of collapsible entries) and the rule catalogue"""

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Collapsible, Label, MarkdownViewer, Static

from .. import checks
from ..screens.base import SplitScreen, crumb_title

def finding_title(f, with_line=True):
  where = f"line {f.line} · " if with_line and f.line else ""
  return f"{f.icon} {f.rule.id} · {where}{f.evidence}"

def finding_body(f):
  """Markup of the details of a finding: what it implies, how to fix it, the link to the catalogue"""
  r = f.rule
  esc = lambda s: s.replace("[", r"\[")
  parts = [f"[b]{esc(r.title)}[/b]",
           f"[b]Why it matters:[/b] {esc(r.implies)}",
           f"[b]How to fix:[/b] {esc(f.fix or r.fix)}"]
  parts.append(f"[@click=app.rule_catalogue('{r.id}')][u]{r.id} in the rule catalogue →[/u][/]")
  return "\n".join(parts)

class FindingsView(Vertical):
  """The findings of a check: a summary line, then one collapsible entry per finding (the most severe first).
  An entry opens to show why it matters, how to fix it (with a snippet to copy) and the link to the rule catalogue.
  The screen may handle FindingsView.LineSelected (the line of a finding was clicked)."""
  DEFAULT_CSS = """
  FindingsView { height: auto; }
  FindingsView .fv-summary { height: auto; }
  FindingsView Collapsible { padding: 0; margin: 0; border: none; background: transparent; }
  FindingsView Collapsible > Contents { padding: 0 0 0 3; }
  FindingsView .fv-body { height: auto; }
  FindingsView .fv-snippet { background: $boost; padding: 0 1; width: 1fr; height: auto; }
  FindingsView .fv-row { height: auto; margin-top: 1; }
  FindingsView .fv-row Button { margin-left: 1; min-width: 8; }
  FindingsView Collapsible.-error CollapsibleTitle { color: $error; }
  FindingsView Collapsible.-warning CollapsibleTitle { color: $warning; }
  """

  def __init__(self, *args, line_buttons=False, **kwargs):
    super().__init__(*args, **kwargs)
    self.findings = []
    self.line_buttons = line_buttons

  def compose(self):
    yield Static("", classes="fv-summary")

  def show_text(self, text):
    self.findings = []
    self.remove_children(Collapsible)
    self.query_one(".fv-summary", Static).update(text)

  def show(self, findings, note=""):
    self.findings = list(findings)
    self.remove_children(Collapsible)
    c = checks.counts(findings)
    if not findings:
      summary = Text("✓ no problems found", style="green")
    else:
      summary = Text()
      for sev, word in ((checks.ERROR, "errors"), (checks.WARNING, "warnings"), (checks.INFO, "hints")):
        if c[sev]:
          if summary:
            summary.append(" · ")
          summary.append(f"{checks.SEVERITY_ICON[sev]} {c[sev]} {word if c[sev] != 1 else word[:-1]}", style=checks.SEVERITY_STYLE[sev])
      summary.append("  (open an entry for the details; ", style="dim")
      summary.append("rule catalogue", style="dim underline")
      summary.append(")", style="dim")
    if note:
      summary.append(f"  {note}", style="dim")
    self.query_one(".fv-summary", Static).update(summary)
    widgets = []
    for i, f in enumerate(self.findings):
      body = [Static(finding_body(f), classes="fv-body")]
      if f.snippet:
        body.append(Horizontal(Static(f.snippet, classes="fv-snippet", markup=False),
                               Button("Copy", classes="fv-copy", name=str(i), compact=True), classes="fv-row"))
      if self.line_buttons and f.line:
        body.append(Horizontal(Button(f"Go to line {f.line}", classes="fv-line", name=str(i), compact=True), classes="fv-row"))
      widgets.append(Collapsible(*body, title=finding_title(f), collapsed=True, classes=f"-{f.severity}"))
    if widgets:
      self.mount_all(widgets)

  def on_click(self, event):
    widget = event.widget
    if isinstance(widget, Static) and widget.has_class("fv-summary") and self.findings:
      self.app.action_rule_catalogue("")

  @on(Button.Pressed, ".fv-copy")
  def copy_snippet(self, event):
    event.stop()
    from ..screens.dialogs import copy_text
    f = self.findings[int(event.button.name)]
    copy_text(self.app, f.snippet, f"the fix for {f.rule.id}")

  @on(Button.Pressed, ".fv-line")
  def go_to_line(self, event):
    event.stop()
    f = self.findings[int(event.button.name)]
    handler = getattr(self.screen, "go_to_line", None)
    if handler:
      handler(f.line)

class RuleCatalogueScreen(SplitScreen):
  """All rules with what they find, why it matters and how to fix it; opened at one rule"""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back")]

  def __init__(self, rule_id=""):
    super().__init__()
    self.rule_id = rule_id
    self.crumbs = ["Rule catalogue"] + ([rule_id] if rule_id else [])
    self.tries = 0

  def compose_left(self):
    yield MarkdownViewer(checks.rules_markdown(), id="catalogue", show_table_of_contents=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#catalogue", MarkdownViewer).focus()
    if self.rule_id in checks.RULES:
      self.set_timer(0.2, self.go_to_rule)

  def go_to_rule(self):
    """The document is built after mounting: try until the heading is there"""
    viewer = self.query_one("#catalogue", MarkdownViewer)
    if not viewer.document.goto_anchor(checks.RULES[self.rule_id].anchor) and self.tries < 20:
      self.tries += 1
      self.set_timer(0.15, self.go_to_rule)

  def show_rule(self, rule_id):
    self.rule_id, self.tries = rule_id, 0
    self.crumbs = ["Rule catalogue"] + ([rule_id] if rule_id else [])
    self.sub_title = crumb_title(self.crumbs)
    if rule_id in checks.RULES:
      self.go_to_rule()

  def action_back(self):
    self.pop_back()
