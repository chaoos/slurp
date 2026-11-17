"""The menu of the job list and the overview: what slurp does for the tab (job list, shell, drive, health, quota,
…), each with its key"""

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from .. import icons

# (action of the app, label, key shown, icon (ui/icons.py)); None: a line between groups
ITEMS = [
  ("job_list", "Job list", "j", "jobs"),
  ("cluster_info", "Health", "C", "health"),
  ("quota", "Quota", "Q", "quota"),
  ("scratch", "Old files in scratch", "X", "scratch"),
  None,
  ("cluster_shell", "Shell on the cluster", "t", "shell"),
  ("login_dir", "Open a directory…", "d", "folder"),
  ("cluster_drive", "Map a directory…", "F", "drive"),
  ("interactive_job", "Interactive job", "i", "interactive"),
  ("new_script", "New job script", "n", "new_script"),
  None,
  ("categories", "Categories", "K", "categories"),
  ("web_access", "This slurp in your browser", "W", "web"),
  None,
  ("guides", "Guides", "F1", "docs"),
  ("rule_catalogue", "Rule catalogue", "F2", "rules"),
  ("options", "Options", "F3", "options"),
  ("about", "About slurp", "", "about"),
  None,
  ("quit", "Quit", "q", "quit"),
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
    items = [item and (item[0], icons.label(item[3], item[1]), item[2]) for item in ITEMS]
    width = max(len(label) + len(key) for _, label, key in filter(None, items)) + 4 # (the keys at the right edge)
    options = []
    for item in items:
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
