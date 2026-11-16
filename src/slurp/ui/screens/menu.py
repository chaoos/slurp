"""The menu of the job list: what slurp does for the tab (shell, drive, cluster info, quota, …), each with its key"""

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

# (action of the app, label, key shown); None: a line between groups
ITEMS = [
  ("cluster_info", "Overview of the cluster", "C"),
  ("quota", "Quota", "Q"),
  ("scratch", "Old files in scratch", "X"),
  None,
  ("cluster_shell", "Shell on the cluster", "t"),
  ("login_dir", "Login directory", "d"),
  ("cluster_drive", "Login directory as a drive", "F"),
  ("interactive_job", "Interactive job", "i"),
  ("new_script", "New job script", "n"),
  None,
  ("categories", "Categories", "K"),
  ("web_access", "This slurp in your browser", "W"),
  None,
  ("guides", "Guides", "F1"),
  ("rule_catalogue", "Rule catalogue", "F2"),
  ("options", "Options", "F3"),
  ("about", "About slurp", ""),
  None,
  ("quit", "Quit", "q"),
]

class MenuScreen(ModalScreen):
  """A list under the Menu button: Enter or a click runs the item (dismissed with its action), Esc or a click
  elsewhere closes it (None)"""
  DEFAULT_CSS = """
  MenuScreen { background: $background 0%; align: left top; }
  #menu-list { width: auto; min-width: 36; height: auto; max-height: 90%; border: round $accent; background: $surface;
               padding: 0; }
  #menu-list > .option-list--option { padding: 0 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("f10", "close", "Close", show=False)]

  def __init__(self, at=(0, 0)):
    super().__init__()
    self.at = at

  def compose(self) -> ComposeResult:
    width = max(len(label) + len(key) for _, label, key in filter(None, ITEMS)) + 4 # (the keys at the right edge)
    options = []
    for item in ITEMS:
      if item is None:
        options.append(None)
        continue
      action, label, key = item
      options.append(Option(Text.assemble(label.ljust(width - len(key)), (key, "bold")), id=action))
    yield OptionList(*options, id="menu-list")

  def on_mount(self):
    menu = self.query_one(OptionList)
    menu.styles.offset = self.at
    menu.focus()

  @on(OptionList.OptionSelected)
  def selected(self, event):
    event.stop()
    self.dismiss(event.option.id)

  def on_click(self, event: events.Click):
    if self.get_widget_at(event.screen_x, event.screen_y)[0] is self: # (outside the list)
      self.dismiss(None)

  def action_close(self):
    self.dismiss(None)
