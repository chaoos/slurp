"""Options (F3): the settings slurp keeps in config.json, to see and change"""

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, SelectionList, Static
from textual.widgets.selection_list import Selection

from ...checks import engine as checks
from ...config import config_path, load_config, update_config
from ...state import PROGRESS_SETTINGS
from .. import icons

ICON_CHOICES = [("icons (slurp's icon font or a Nerd Font)", ""), ("Unicode symbols (uneven in most fonts)", "symbols"),
                ("none", "none")]

# (the progress settings: key, label, unit)
PROGRESS_FIELDS = [("progress_tail_kib", "End of a file read", "KiB"), ("progress_tail_lines", "Of it, the last", "lines"),
                   ("progress_pace_points", "Current pace over", "progress values")]

COPY_CHOICES = [("ask how to get it, when this machine has no clipboard tool", ""),
                ("send it to the terminal only (OSC 52), never ask", "terminal")]

class OptionsScreen(ModalScreen):
  DEFAULT_CSS = """
  OptionsScreen { align: center middle; }
  #op-dialog { width: 110; max-width: 98%; height: 94%; border: thick $accent; background: $surface; padding: 0 1; }
  #op-title { text-style: bold; margin: 1 0 0 0; }
  #op-body { height: 1fr; }
  .op-section { text-style: bold underline; margin-top: 1; }
  .op-row { height: auto; margin-top: 1; }
  .op-row Label { width: 24; padding: 1 1 0 0; }
  .op-row Input, .op-row Select { width: 1fr; }
  .op-note { color: $text-muted; height: auto; padding-left: 24; }
  #op-muted { height: 14; }
  .op-forget { margin-left: 1; }
  #op-path { color: $text-muted; height: auto; margin-top: 1; }
  #op-buttons { height: auto; align-horizontal: right; margin: 1 0; }
  #op-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "cancel", "Cancel")]

  def __init__(self):
    super().__init__()
    self.config = load_config()
    self.forget = set() # what the user asked to forget (keys of the config, or ("drive", destination))

  def compose(self):
    c = self.config
    app = self.app
    themes = sorted(app.available_themes)
    active = getattr(app, "view_kind", "terminal")
    with Vertical(id="op-dialog"):
      yield Static(icons.label("options", "Options"), id="op-title")
      with VerticalScroll(id="op-body"):
        yield Static("Appearance", classes="op-section")
        with Horizontal(classes="op-row"):
          yield Label("Theme in a terminal")
          yield Select([(t, t) for t in themes], value=c.get("theme") if c.get("theme") in themes else app.theme,
                       allow_blank=False, id="op-theme")
        with Horizontal(classes="op-row"):
          yield Label("Theme in a browser")
          yield Select([("as in the terminal", "")] + [(t, t) for t in themes],
                       value=c.get("theme_web") if c.get("theme_web") in themes else "", allow_blank=False, id="op-theme-web")
        yield Static(f"The theme follows the view that is active (now: {'a browser' if active == 'web' else 'a terminal'}).",
                     classes="op-note")
        with Horizontal(classes="op-row"):
          yield Label("Icons")
          yield Select(ICON_CHOICES, value=c.get("icons") if c.get("icons") in ("symbols", "none") else "",
                       allow_blank=False, id="op-icons")
        yield Static("In front of tabs, buttons, menu items, states and files; from the next start. A browser has "
                     "the icons; a terminal shows boxes without slurp's icon font (slurp --install-font, on the "
                     "machine of the terminal you look through) or a Nerd Font.", classes="op-note")
        with Horizontal(classes="op-row"):
          yield Label("At the start a tab shows")
          yield Select([("its overview", ""), ("its job list", "jobs")], value="jobs" if c.get("start_page") == "jobs" else "",
                       allow_blank=False, id="op-start-page")
        yield Static("Shells", classes="op-section")
        with Horizontal(classes="op-row"):
          yield Label("Scrollback (lines)")
          scrollback = c.get("terminal_scrollback")
          yield Input(str(scrollback if isinstance(scrollback, int) else 10000), type="integer", id="op-scrollback")
        yield Static("Lines a shell keeps above its screen (0: none), for shells opened from now on.", classes="op-note")
        yield Static("Progress of jobs", classes="op-section")
        for key, label, unit in PROGRESS_FIELDS:
          default, low, high, _slow = PROGRESS_SETTINGS[key]
          value = c.get(key, default)
          with Horizontal(classes="op-row"):
            yield Label(label)
            yield Input(str(value if isinstance(value, int) else default), type="integer", id=f"op-{key}",
                        tooltip=f"{low} to {high} {unit}; default {default}")
        yield Static("How the progress of a job (n/N or M%) is read from its file: only the file's end (KiB), of it "
                     "the last lines, and the current pace over the last progress values. More finds progress further "
                     "back and smooths the pace, but slows slurp down: every running job's file is read that far at "
                     "each reload of the job list and every 10 s in the job window, over SSH most of all.",
                     classes="op-note")
        yield Static(id="op-progress-warning", classes="op-note")
        yield Static("Copying", classes="op-section")
        with Horizontal(classes="op-row"):
          yield Label("Copy in a terminal")
          yield Select(COPY_CHOICES, value=c.get("copy") or "", allow_blank=False, id="op-copy")
        with Horizontal(classes="op-row"):
          yield Label("Copy in a browser")
          yield Select(COPY_CHOICES, value=c.get("copy_web") or "", allow_blank=False, id="op-copy-web")
        yield Static("Copy sends the text to the active view (OSC 52); whether it arrives depends on that terminal or "
                     "browser. \"It arrived, don't ask again\" in the copy window sets this for the kind of view "
                     "that is active.", classes="op-note")
        yield Static("Links", classes="op-section")
        with Horizontal(classes="op-row"):
          yield Label("Relay")
          yield Input(c.get("link_relay") or "ssh-j.com", id="op-relay")
        yield Static("direct, ssh-j.com, serveo.net, pinggy, sshreach://user@server:port, or your own SSH server "
                     "(user@host). A link's setup panel lists them with what each one means (and keeps the host and "
                     "port of direct for each machine slurp runs on). Web links keep their own relay, which can also be cloudflare "
                     "(a public https address, no ssh).", classes="op-note")
        yield Static("Checks: rules not to report (muted)", classes="op-section")
        muted = set(c.get("checks_muted") or [])
        yield SelectionList(*[Selection(f"{r.id}  {r.title}", r.id, r.id in muted) for r in checks.RULES.values()],
                            id="op-muted")
        yield Static("What slurp remembers", classes="op-section")
        for dest, entry in sorted((c.get("drive_mounts") or {}).items()):
          with Horizontal(classes="op-row"):
            yield Label(icons.label("drive", f"Drive of {dest}")[:23])
            yield Static(f"{entry.get('path', '?')}{'  (read only)' if entry.get('read_only') else ''}")
            yield Button("Forget", classes="op-forget", name=f"drive:{dest}", compact=True)
        for key, what in (("progress_files", "progress files chosen for jobs"), ("array_progress_patterns", "progress patterns of arrays")):
          n = len(c.get(key) or {})
          with Horizontal(classes="op-row"):
            yield Label(what.capitalize()[:23])
            yield Static(f"{n} remembered")
            if n:
              yield Button("Forget all", classes="op-forget", name=key, compact=True)
        tabs = c.get("tabs") or []
        names = [t.get("destination") or "Local" if isinstance(t, dict) else str(t) for t in tabs] if isinstance(tabs, list) else []
        with Horizontal(classes="op-row"):
          yield Label("Tabs at the next start")
          yield Static(", ".join(names) or "none saved")
        yield Static("The open tabs are saved by themselves (+ and ✕ change them).", classes="op-note")
      yield Static(f"Stored in {config_path()}", id="op-path")
      with Horizontal(id="op-buttons"):
        yield Button("Cancel", id="op-cancel", compact=True)
        yield Button("Save", id="op-save", variant="primary", compact=True)

  def on_mount(self):
    self.progress_warning()

  def progress_values(self):
    """{key: the number typed} of the progress settings (None: not a number)"""
    out = {}
    for key, *_ in PROGRESS_FIELDS:
      try:
        out[key] = int(self.query_one(f"#op-{key}", Input).value)
      except ValueError:
        out[key] = None
    return out

  @on(Input.Changed, "#op-progress_tail_kib, #op-progress_tail_lines, #op-progress_pace_points")
  def progress_warning(self, event=None):
    """Said at once: values out of range, or so high that slurp gets slow"""
    said = []
    for key, label, unit in PROGRESS_FIELDS:
      value = self.progress_values()[key]
      default, low, high, slow = PROGRESS_SETTINGS[key]
      if value is None or not low <= value <= high:
        said.append((f"{label}: {low} to {high} {unit}", "bold red"))
      elif value > slow and key != "progress_pace_points":
        said.append((f"{label}: {value} {unit} is a lot: the job list and the job window get slower (above {slow} "
                     "noticeably, with many running jobs and over SSH)", "bold yellow"))
      elif value > slow:
        said.append((f"{label}: over {value} values the current pace follows changes slowly", "yellow"))
    warning = self.query_one("#op-progress-warning", Static)
    warning.update(Text("\n").join(Text(t, style=s) for t, s in said) if said else "")
    warning.display = bool(said)

  @on(Button.Pressed, ".op-forget")
  def forget_pressed(self, event):
    event.stop()
    self.forget.add(event.button.name)
    event.button.label = "forgotten (on Save)"
    event.button.disabled = True

  @on(Button.Pressed, "#op-save")
  def save(self, event=None):
    if event is not None:
      event.stop()
    theme = self.query_one("#op-theme", Select).value
    web = self.query_one("#op-theme-web", Select).value
    try:
      scrollback = max(int(self.query_one("#op-scrollback", Input).value or 0), 0)
    except ValueError:
      self.notify("the scrollback is a number of lines", severity="error")
      return
    values = self.progress_values()
    for key, label, unit in PROGRESS_FIELDS:
      default, low, high, _slow = PROGRESS_SETTINGS[key]
      if values[key] is None or not low <= values[key] <= high:
        self.notify(f"{label}: {low} to {high} {unit}", severity="error")
        return
    changes = {**{key: (None if values[key] == PROGRESS_SETTINGS[key][0] else values[key]) for key in values},
               "theme": theme, "theme_web": web or None, "terminal_scrollback": scrollback,
               "copy": self.query_one("#op-copy", Select).value or None,
               "copy_web": self.query_one("#op-copy-web", Select).value or None,
               "link_relay": self.query_one("#op-relay", Input).value.strip() or None,
               "checks_muted": sorted(self.query_one("#op-muted", SelectionList).selected),
               "start_page": self.query_one("#op-start-page", Select).value or None,
               "icons": self.query_one("#op-icons", Select).value or None}
    drives = dict(self.config.get("drive_mounts") or {})
    for item in self.forget:
      if item.startswith("drive:"):
        drives.pop(item[6:], None)
      else:
        changes[item] = {}
    if any(item.startswith("drive:") for item in self.forget):
      changes["drive_mounts"] = drives
    update_config(**changes)
    self.dismiss(changes)

  @on(Button.Pressed, "#op-cancel")
  def action_cancel(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)
