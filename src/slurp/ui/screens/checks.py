"""The findings of the checks (a list of collapsible entries) and the rule catalogue"""

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, MarkdownViewer, Static

from ...checks import engine as checks
from .base import SplitScreen
from ..widgets import crumb_title

def code(text, language):
  """Syntax-highlighted code (plain text if highlighting is not available)"""
  try:
    from textual.highlight import highlight
    return highlight(text, language=language)
  except Exception:
    return Text(text)

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
  FindingsView .fv-snippet { background: $boost; border-left: outer $accent; padding: 0 1; width: 1fr; height: auto; }
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

  def busy(self, text):
    """A check is running: say so in the summary line, the entries stay (and opened ones open) until its results"""
    if not self.findings:
      self.show_text(text)
      return
    self.query_one(".fv-summary", Static).update(text)

  def show_text(self, text):
    self.findings = []
    self.remove_children(Collapsible)
    self.query_one(".fv-summary", Static).update(text)

  @staticmethod
  def key(f):
    return (f.rule.id, f.line, f.evidence, f.severity)

  def show(self, findings, note=""):
    """Show findings. A second phase of a check (the environment, seconds later) shows them again: the entries stay
    as they are if nothing changed, and opened ones stay open; buttons belong to their finding, not to a position"""
    findings = list(findings)
    same = [self.key(f) for f in findings] == [self.key(f) for f in self.findings] and self.query(Collapsible)
    expanded = {getattr(c, "finding_key", None) for c in self.query(Collapsible) if not c.collapsed}
    self.findings = findings
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
    if same:
      return
    self.remove_children(Collapsible)
    widgets = []
    def button(label, cls, f, **kw):
      b = Button(label, classes=cls, compact=True, **kw)
      b.finding = f
      return b
    for f in self.findings:
      body = [Static(finding_body(f), classes="fv-body")]
      if f.snippet:
        body.append(Horizontal(Static(code(f.snippet, "bash"), classes="fv-snippet"), button("Copy", "fv-copy", f), classes="fv-row"))
      buttons = []
      if self.line_buttons and f.line:
        buttons.append(button(f"Go to line {f.line}", "fv-line", f))
      buttons.append(button(f"Mute {f.rule.id} everywhere", "fv-mute", f,
                            tooltip="never report this rule again (\"checks_muted\" in the config)"))
      if hasattr(self.screen, "mute_in_script"):
        buttons.append(button(f"Mute {f.rule.id} in this script", "fv-mute-script", f, tooltip="adds # slurp: ignore to the script"))
      body.append(Horizontal(*buttons, classes="fv-row"))
      entry = Collapsible(*body, title=finding_title(f), collapsed=self.key(f) not in expanded, classes=f"-{f.severity}")
      entry.finding_key = self.key(f)
      widgets.append(entry)
    if widgets:
      self.mount_all(widgets)

  def on_click(self, event):
    widget = event.widget
    if isinstance(widget, Static) and widget.has_class("fv-summary") and self.findings:
      self.app.action_rule_catalogue("")

  @on(Button.Pressed, ".fv-copy")
  def copy_snippet(self, event):
    event.stop()
    from .dialogs import copy_text
    f = event.button.finding
    copy_text(self.app, f.snippet, f"the fix for {f.rule.id}")

  @on(Button.Pressed, ".fv-mute")
  def mute(self, event):
    event.stop()
    from ...config import load_config, update_config
    f = event.button.finding
    muted = sorted(set((load_config().get("checks_muted") or [])) | {f.rule.id})
    update_config(checks_muted=muted)
    self.app.notify(f"{f.rule.id} is muted (to report it again: remove it from \"checks_muted\" in the config)", timeout=6)
    recheck = getattr(self.screen, "recheck", None)
    if recheck:
      recheck()

  @on(Button.Pressed, ".fv-mute-script")
  def mute_script(self, event):
    event.stop()
    f = event.button.finding
    self.screen.mute_in_script(f.rule.id)

  @on(Button.Pressed, ".fv-line")
  def go_to_line(self, event):
    event.stop()
    f = event.button.finding
    handler = getattr(self.screen, "go_to_line", None)
    if handler:
      handler(f.line)

class DocViewer(MarkdownViewer):
  """A MarkdownViewer that leaves link clicks to its screen (the viewer would load them as files)"""
  async def _on_markdown_link_clicked(self, message):
    message.prevent_default() # (not the viewer's own handler; the message still bubbles to the screen)

class DocScreen(SplitScreen):
  """A Markdown document with a table of contents; links: rule:R01 (the rule catalogue), #anchor (here), http(s)"""
  SHOW_BACK = True
  BINDINGS = [Binding("escape", "back", "Back")]
  title_word = ""

  def __init__(self, markdown, anchor=""):
    super().__init__()
    self.markdown, self.anchor = markdown, anchor
    self.crumbs = [self.title_word]
    self.tries = 0

  def compose_left(self):
    yield DocViewer(self.markdown, id="doc", show_table_of_contents=True, open_links=False)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#doc", MarkdownViewer).focus()
    if self.anchor:
      self.set_timer(0.2, self.go_to_anchor)

  def go_to_anchor(self):
    """The document is built after mounting: try until the heading is there"""
    viewer = self.query_one("#doc", MarkdownViewer)
    if not viewer.document.goto_anchor(self.anchor) and self.tries < 20:
      self.tries += 1
      self.set_timer(0.15, self.go_to_anchor)

  def jump(self, anchor):
    self.anchor, self.tries = anchor, 0
    self.go_to_anchor()

  def on_markdown_link_clicked(self, event):
    event.stop()
    href = event.href
    if href.startswith("rule:"):
      self.app.action_rule_catalogue(href[5:])
    elif href.startswith("#"):
      self.jump(href[1:])
    elif href.startswith(("http://", "https://")):
      self.app.open_page(href)

  def action_back(self):
    self.pop_back()

class RuleCatalogueScreen(DocScreen):
  """All rules with what they find, why it matters and how to fix it; opened at one rule"""
  icon_name = "rules" # (of its subtab)
  title_word = "Rule catalogue"

  def __init__(self, rule_id=""):
    super().__init__(checks.rules_markdown(), checks.RULES[rule_id].anchor if rule_id in checks.RULES else "")
    self.rule_id = rule_id
    self.crumbs = ["Rule catalogue"] + ([rule_id] if rule_id else [])

  def show_rule(self, rule_id):
    self.rule_id = rule_id
    self.crumbs = ["Rule catalogue"] + ([rule_id] if rule_id else [])
    self.sub_title = crumb_title(self.crumbs)
    if rule_id in checks.RULES:
      self.jump(checks.RULES[rule_id].anchor)

class GuidesScreen(DocScreen):
  icon_name = "docs" # (of its subtab)
  title_word = "Guides"


class FixPreviewScreen(ModalScreen):
  """The fixes that can be made automatically, as a diff; dismisses with True to write the fixed copy"""
  DEFAULT_CSS = """
  FixPreviewScreen { align: center middle; }
  #fx-dialog { width: 120; max-width: 98%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #fx-title { text-style: bold; margin: 1 0 0 0; }
  #fx-done { height: auto; margin: 1 0; }
  #fx-diff { height: 1fr; }
  #fx-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #fx-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self, path, target, old, new, done):
    super().__init__()
    self.path, self.target, self.old, self.new, self.done = path, target, old, new, done

  def compose(self):
    import difflib
    diff = self.diff = "".join(difflib.unified_diff(self.old.replace("\r", "").splitlines(True), self.new.splitlines(True),
                                        self.path, self.target))
    with Vertical(id="fx-dialog"):
      yield Static(f"Fixed copy of {self.path}", id="fx-title")
      yield Static(Text("\n".join(f"• {d}" for d in self.done) + "\n\nThe original stays as it is. Fixes that need your "
                        "decision (memory units, time limits, paths) are not made: see the findings.", style=""), id="fx-done")
      with VerticalScroll(id="fx-diff"):
        yield Static(code(diff, "diff"), id="fx-diff-text")
      with Horizontal(id="fx-buttons"):
        yield Button("Cancel", id="fx-cancel", compact=True)
        yield Button(f"Write {self.target.rsplit('/', 1)[-1]}", id="fx-write", variant="primary", compact=True)

  @on(Button.Pressed, "#fx-write")
  def write(self, event):
    event.stop()
    self.dismiss(True)

  @on(Button.Pressed, "#fx-cancel")
  def action_cancel(self):
    self.dismiss(False)
