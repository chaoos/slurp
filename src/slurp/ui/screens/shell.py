"""A shell in a subtab"""


from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from ...resources import icons
from textual.widgets import Markdown


from ..terminal import TerminalSession, TerminalView
from .base import SplitScreen

def shell_kind(session):
  """What a terminal runs: login (the SSH login of a tab), node (a step in a job), gdb, cluster or local"""
  args = list(session.args or [])
  if session.login is not None:
    return "login"
  if args[:1] == ["srun"] and any(a.startswith("--jobid=") for a in args):
    return "node"
  if args[:1] == ["gdb"]:
    return "gdb"
  return "cluster" if session.via is not None else "local"

def screen_text(session):
  """The visible screen of a terminal as plain text (trailing blanks and empty lines cut)"""
  vt = session.vt if session else None
  if vt is None:
    return ""
  return "\n".join(line.rstrip() for line in vt.display).rstrip("\n")

TERMINAL_HELP_COMMON = """
## Scrolling back

The mouse wheel scrolls through the lines that went off the top of the screen (the history), `Shift+PgUp` and
`Shift+PgDn` by a page. A bar in the top row says how far back you are; typing goes back to the bottom. The history
keeps the last 10000 lines (`"terminal_scrollback"` in slurp's config.json, 0: none); `clear` empties it. In
full-screen programs (less, vim, htop) the wheel sends arrow keys to them instead.

## Copying out of the terminal

- **Drag with the mouse** to select (also in the history: drag past the top or bottom edge to scroll), then
  **right-click on the selection** to copy it to your clipboard. A click elsewhere, or typing, removes the selection.
- **Copy the screen** below: everything visible in this terminal, as text.
- Your terminal's own selection still works holding a modifier key (`Shift`+drag on Linux, Windows Terminal and in a
  browser; `Option`+drag in iTerm2; `Fn`+drag in macOS Terminal); it selects whole screen lines, also the window on
  the left.
- `Ctrl+C` does **not** copy: it goes to the shell and interrupts the running command.

## Pasting into the terminal

- Your terminal's paste: `Ctrl+Shift+V` (Linux), `Cmd+V` (macOS), `Ctrl+V` or right click (Windows Terminal),
  `Shift+Insert`, or `Shift`+middle click.
- A plain `Ctrl+V` goes to the shell as a key (in bash it inserts the next key literally); it does not paste.
- Pasted lines can run as commands: a shell with bracketed paste (bash 5.1 or newer, zsh) waits for your Enter;
  older shells run every line that ends with a newline right away.

## Keys

| Key | What it does |
|---|---|
| All keys | go to the program in the terminal, including `Ctrl+C`, `Ctrl+D`, `Ctrl+Z`, `Ctrl+Q`, `Ctrl+P`, `Ctrl+W`, `Esc` |
| `Ctrl+]` | the one key slurp keeps: {switch} |{extra}
| `Shift+PgUp` / `Shift+PgDn` | scroll the history (not sent to the program) |
| `Ctrl+D` or `exit` | ends the shell; then `Enter` starts a new one (the old one's state is gone) |
"""

TERMINAL_HELP_KIND = {
  "login": """
## This terminal: the login of the tab

The SSH login of this tab runs here: password, passphrase or one-time code prompts appear and are typed here. Once
logged in, slurp sends all its commands, shells and drives through this one connection.

**Closing it** (`✕` or `Ctrl+X`) **gives up the connection**: the job list of the tab stops updating, its shells and
drives end, and the connection window offers to log in again.
""",
  "cluster": """
## This terminal: a shell on the login node

A login shell on the cluster of the tab, as a new channel of the tab's SSH connection (no new login).
Login nodes are shared by everyone: edit, compile and submit here, but run heavy computations in jobs
(Interactive job, or a shell on the node of a running job).
""",
  "node": """
## This terminal: a shell on the node of a job

The shell runs as an extra step of the job (`srun --overlap`), with the job's resources. It ends at the job's
time limit or when the job ends. Closing it ends only this step, never the job.
""",
  "gdb": """
## This terminal: GDB

GDB runs on the cluster of the tab. `quit` (or `Ctrl+D`) ends it; `Ctrl+C` interrupts the program being debugged.
""",
  "local": """
## This terminal: a shell on this machine

A login shell on the machine slurp runs on, as the user running slurp.
""",
}

TERMINAL_HELP_CLOSE = """
## Closing

{close}

When it closes, the shell and everything running in it gets a hangup and ends: unsaved files in an editor are
lost and running commands stop. The same happens when you close the tab, quit slurp, or the tab's SSH connection
is lost.

## Good to know

- **It keeps running in the background**: switching subtabs, windows or tabs does not stop it.
- **The history is limited** (see Scrolling back) and lost when the shell ends: for long output use `less`
  (`command | less`), write it to a file, or run `tmux` / `screen` in the shell.
- **For work that must survive** slurp, a lost connection or closing: start it in `tmux` / `screen` on the
  cluster (and attach again later) or submit it as a job with `sbatch`.
- The terminal is emulated by slurp: bash, vim, less, htop and the like work, with colors; its size follows the
  window. For long or heavy interactive work, a shell **From your own terminal** (`t` in the job list, `s` in the
  job window) gives your terminal's own scrollback, selection and fonts.
"""

class TerminalHelpScreen(ModalScreen):
  """How to copy from and paste into a terminal, its keys, closing it, and how to treat it"""
  DEFAULT_CSS = """
  TerminalHelpScreen { align: center middle; }
  #thelp-dialog { width: 110; max-width: 95%; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
  #thelp-text { height: 1fr; }
  #thelp-buttons { height: auto; margin-top: 1; }
  #thelp-buttons Button { margin-right: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close", priority=True)]

  def __init__(self, session, pane=False):
    super().__init__()
    self.session, self.pane = session, pane

  def help_text(self):
    kind = shell_kind(self.session)
    if self.pane:
      switch = "switches between the window on the left and this terminal"
      close = ("`✕` in the title bar of the terminal, or `Ctrl+X`. This is the login of the tab: closing it gives up "
               "the connection (see above)." if kind == "login" else "`✕` in the title bar of the terminal, or `Ctrl+X`.")
    else:
      switch = "back to the job list; this shell keeps running in its subtab"
      close = ("`✕` next to the name of the subtab in the subtab bar. `Ctrl+W` closes subtabs only while you are "
               "not in a shell, since in a shell it goes to the shell.")
    extra = "\n| `Ctrl+X` | in this pane, also kept by slurp: closes the terminal |" if self.pane else ""
    text = (TERMINAL_HELP_KIND[kind] + TERMINAL_HELP_COMMON.replace("{switch}", switch).replace("{extra}", extra)
            + TERMINAL_HELP_CLOSE.replace("{close}", close))
    return f"# {self.session.title}\n" + text

  def compose(self) -> ComposeResult:
    with Vertical(id="thelp-dialog"):
      with VerticalScroll(id="thelp-text"):
        yield Markdown(self.help_text())
      with Horizontal(id="thelp-buttons"):
        yield Button(icons.label("copy", "Copy the screen"), id="thelp-copy", variant="primary", compact=True)
        yield Button("Close", id="thelp-close", compact=True)

  def on_mount(self):
    self.query_one("#thelp-close", Button).focus()

  @on(Button.Pressed, "#thelp-copy")
  def copy_screen(self):
    from .dialogs import copy_text
    text = screen_text(self.session)
    if not text:
      self.notify("the screen is empty", severity="warning")
      return
    self.dismiss()
    copy_text(self.app, text, "the screen of the terminal")

  @on(Button.Pressed, "#thelp-close")
  def action_close(self):
    self.dismiss()

class TerminalScreen(SplitScreen):
  """A shell in a subtab of its own"""
  def __init__(self, session):
    super().__init__()
    self.session = session

  def compose_left(self):
    with Horizontal(id="shell-bar"):
      yield Static(id="shell-status")
      yield Button("ⓘ Help", id="shell-help", compact=True, tooltip="copy, paste, keys, closing")
    yield TerminalView(session=self.session)

  def on_mount(self):
    self.sub_title = self.session.title
    self.watch(self.app, "shell_version", self.update_status)
    self.query_one(TerminalView).focus()

  def on_screen_resume(self):
    self.query_one(TerminalView).focus()
    self.call_after_refresh(self.query_one(TerminalView).attach)

  @on(Button.Pressed, "#shell-help")
  def show_help(self, event):
    event.stop()
    self.app.push_screen(TerminalHelpScreen(self.session), lambda _: self.query_one(TerminalView).focus())

  def update_status(self, _=None):
    s = self.session
    text = f"{s.title} · ended (exit code {s.code}) · Enter: start it again · ✕ of the subtab: close" if s.done else \
           f"{s.title} · all keys go to the shell, Ctrl-] goes to the job list · ✕ of the subtab closes it · ⓘ: copy, paste"
    self.query_one("#shell-status", Static).update(Text(text, style="bold" if not s.done else "yellow"))

  def restart(self):
    old = self.session
    self.session = TerminalSession(self.app, old.args, old.title, via=old.via)
    for view in self.app.cluster.views:
      if view.session is old:
        view.session = self.session
    view = self.query_one(TerminalView)
    view.own_session = self.session
    self.call_after_refresh(view.attach)
    self.update_status()
