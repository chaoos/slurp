"""The ? of every window: what the view shows, what it can do, its keys (from its live bindings)"""

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

EVERYWHERE = """
Tabs at the top are clusters (`+` adds one, `✕` closes one); under them the subtabs of the tab: **Jobs** and every
window opened from it (`Ctrl-W` closes the one shown). `Ctrl-]` switches between the window and the shell pane on the
right. The footer shows the keys of the window and, on the right, the state of the tab's connection (click it for the
connection window).
"""

VIEW_HELP = {
  "MainScreen": ("Job list", """
Your queued and running jobs and those that ended recently, of the active tab's cluster.

- **Columns:** a click on a header sorts by it (again: the other way). **Progress** comes from `n/N` lines in the
  output; **Done: same pace / current pace** estimate the end at the average rate and at the recent rate.
  **Checks** shows errors ✖ and warnings ⚠ the checks found for the job; **Err** marks error files that are not empty.
- **Arrays** with ten or more tasks are one row: its state is the mix of the tasks' states (`30R 10PD 100CD`).
  `Space` (or ▸) lists the tasks under it, Enter opens the array window. `g` switches grouping off and on.
- **Loading:** the list shows as soon as Slurm answers; commands, error files, progress and checks follow (spinners).
  `r` reloads, `a` aborts a load. **finished since** chooses how far back ended jobs are listed, the dropdown
  next to it whose jobs (yours, an account's, all your accounts').
- **Toolbar:** Shell (`t`, in slurp or from your own terminal), Drive (`F`, your login directory on this machine or
  yours), Web (`W`, this slurp in your browser from anywhere, through a link), Interactive job (`i`), Cluster info (`C`),
  Quota (`Q`), Scratch (`X`, old files the purge would take).
- `n` writes a new job script from a template; `!` shows only the jobs with problems; `/` filters by any text.
"""),
  "JobScreen": ("Job window", """
Everything about one job.

- **Buttons:** Cancel job, Restart job (the original submission, another array range, or an edited command; with the
  environment and script it was submitted with, if the cluster kept them), Run array, Array (the window of its array).
- **Info:** state (with the pending reason or exit code), account, partition, nodes, times, node hours, the command.
- **Checks:** what is wrong or could be better with the script and the job, most severe first; open an entry for why
  it matters and how to fix it, and its rule in the catalogue. **Check** runs all checks again: first the record,
  paths, quota and logs, then (a few seconds later) the script's environment: its setup replayed on the login node
  after a fresh login, the programs found and their libraries. **Placement** (running jobs) shows where tasks and
  threads run; **Fixed copy…** writes a copy of the script with the safe fixes; **What changed** (failed jobs)
  compares with the last run that worked.
- **Memory** (running jobs): Query current memory (CPU and GPU), and repeated queries with plots.
- **Progress:** the latest `n/N` line with two estimates of the end; **Progress file…** picks another log file.
- **Files:** output, error, script, working directory and files the job names; Enter opens one.
- **Steps** of the job; for a running job **Run on the nodes** (a command on every node, or one) and **Shell** (`s`).
"""),
  "ArrayScreen": ("Array window", """
One job array as a whole.

- **Overview:** what it is, how many tasks in which state, node hours, queue waits and run times.
- **By state:** the task ids of every state (ended ones also by exit code, pending ones by reason), compressed for
  copying (Copy); a click on an id opens that task's job window.
- **Tasks:** every task with its progress bar; a click on a header sorts, a click on a task opens it.
- **Cancel** the whole array or only its pending tasks (after asking). `r` reloads.
"""),
  "FileScreen": ("Text file", """
A text file (or command output) of the cluster, read in parts: large files show their end (`A` loads the part before).

- **Follow** (`f`) shows what is appended, like `tail -f`; **Watch** (`w`) reruns a command output every 5 s.
- `/` searches, `n` / `N` next and previous match; `g` / `G` top and bottom.
- Paths and `file:line` references in the text open with Enter on them (or a click).
- `l` opens the file in `less` (outside slurp; `q` comes back). `r` reloads.
"""),
  "DirScreen": ("Directory", """
A directory of the cluster.

- Enter opens an entry: directories here, files in the matching view (text, Markdown, HDF5, core dumps, batch
  scripts in the script view with their checks).
- A click on a header sorts by name, size or time; `/` filters by name; `.` shows hidden files.
- `Backspace` or `u` goes to the parent; `f` follows (lists again every few seconds); `r` reloads.
"""),
  "MarkdownScreen": ("Markdown", """
A Markdown file, rendered, with its table of contents on the left (click a heading to go there). Links to other
files open them. `r` reloads.
"""),
  "H5Screen": ("HDF5 file", """
An HDF5 file, read in place (only the parts you look at are fetched).

- The tree on the left: groups and datasets; the right side shows the selected one: attributes, shape, type, and the
  data in pages of 200 rows (`[` `]`) and columns (`{` `}`).
- `s` enters a selection (numpy slicing like `0:10, 3`). A string dataset named `*.md` is rendered as Markdown.
- `r` reloads.
"""),
  "CoreScreen": ("Core dump", """
A core dump: which program crashed, with which signal, when, on which node (from the ELF notes, read with GDB), and
how likely it belongs to the job. **Open in GDB** suggests the command (editable) and runs it in a shell subtab.
"""),
  "TerminalScreen": ("Shell", ""),
  "ScriptScreen": ("Batch script", """
A batch script with the findings of the checks below it.

- Every finding opens to show why it matters, how to fix it (with a snippet to copy), the rule in the catalogue,
  **Go to line**, and Mute (everywhere, or in this script with a `# slurp: ignore` comment).
- **Explain** (`x`) writes after every `#SBATCH` line what Slurm makes of it: units, per node, the paths of the logs,
  IGNORED after the first command.
- **Check** (`r`) runs all checks: the script, its paths, programs and quota on the cluster at once, then its
  environment a few seconds later: the setup lines (module loads, sources, exports; never the job's commands) replayed
  on the login node after a fresh login, the programs found and their shared libraries (`ldd`). Opening and saving the
  script run it too; while you edit, Check checks the edited text, saved or not.
- **Edit** (`e`) makes the script editable right here: the checks follow as you type (the script, and its
  paths and programs on the cluster), Go to line jumps into it, `Ctrl+S` saves, `Ctrl+Z` undoes, **Revert** goes back
  to the file, `Esc` or **Done** stops editing (asking about unsaved changes, as do closing the subtab and quitting).
  Save asks before it overwrites a file that something else changed meanwhile. Bash is highlighted when Textual's
  syntax extra is installed (`pip install "textual[syntax]"`). **Edit outside** opens your own `$VISUAL`/`$EDITOR`.
- **Test** asks Slurm whether it accepts it and when it would start (`sbatch --test-only`). **Fixed copy…** writes a copy with the safe fixes.
  **Submit…** submits it (after naming the errors found). `r` checks again.
"""),
  "RuleCatalogueScreen": ("Rule catalogue", """
All rules of the checks, by category: what each finds, why it matters, how to fix it, often with an example.
The contents on the left jump to a rule; rule ids in the text are links. Findings everywhere link here.
"""),
  "GuidesScreen": ("Guides", """
How to use the cluster well, with this cluster's numbers: Slurm basics, job scripts, common mistakes, arrays and
dependencies, software, filesystems and quotas, NUMA placement, GPUs, failed jobs. Rule ids are links into the
rule catalogue; the contents on the left jump to a section.
"""),
  "QuotaScreen": ("Quota", """
Your quotas (and those of your groups where they use space or have a limit) on every filesystem: space and files,
used, soft limit, limit, and a bar coloured by how full it is; also what the site's own quota command says. A full
quota (space *or* files) makes writes fail. `r` asks again.
"""),
  "ScratchScreen": ("Old scratch files", """
Your files in `$SCRATCH` with the oldest access (or modification) time: the ones a purge deletes first. Older than the
given days: red, close to it: yellow. An exclude regex leaves paths out.

- `Space` selects, `A` selects all; **Touch selected / all listed / all my files there** set their access time to
  now (after asking). Many sites do not allow keeping files this way: move what you keep to permanent storage.
- `r` searches again (it can be aborted).
"""),
  "ClusterInfoScreen": ("Cluster info", """
The state of the cluster: health (nodes down, drained, in maintenance, with the reasons), reservations, occupancy
(busy and idle nodes, running and pending jobs and why they wait), the partitions, and the node topology of every
partition (CPUs, sockets × cores × threads, NUMA domains, GPUs, memory). The title says healthy, degraded or
unhealthy and why. Reload asks again.
"""),
  "ConnectionScreen": ("Connection window", """
The SSH session of the tab: its state and the course of a (re)connect, the logged-in user and login node, the
resolved destination, traffic and round-trip times, the ssh process and its TCP connection, and a log of events.

- **Reconnect** (`R`) logs in again; **Disconnect** ends the session until the tab is selected again.
- **Drive:** your login directory mounted on this machine (state, Mount/Unmount/Remount/Force unmount).
- **Links:** shells and drives made for your own terminal: their state and sessions; select one for its commands,
  a new password, more time or Revoke. When you use slurp in a browser through a web link, its session is marked
  **◀ this view**; disconnecting it (or revoking its link) ends the view you are in, and slurp warns first.
"""),
  "PlacementScreen": ("Placement", """
Where the tasks and threads of the running job are, node by node and NUMA domain by NUMA domain: **bound** are the
CPUs a task may run on, **now** where its threads are at the moment; letters are tasks, `#` means several tasks share
a CPU, `·` none. The GPUs of each domain and who uses them are listed under it. Problems (shared cores, unbound
tasks, tasks spread over domains, GPUs of another domain) are named at the top and become findings of the job's
checks. **Refresh** asks the nodes again. The NUMA guide (F1) explains the background.
"""),
  "NewScriptScreen": ("New job script", """
A commented job script for a kind of job (serial, OpenMP, MPI, hybrid, GPU, array, chain, long run with checkpoints)
with the numbers of the chosen partition: nodes, tasks per node, CPUs per task, GPUs. Change the fields, the preview
follows. **Create** writes the file (never over an existing one) and a `logs/` directory next to it, and opens it in
the script view with its checks.
"""),
  "FixPreviewScreen": ("Fixed copy", """
The fixes that are safe to make automatically, as a diff against the original: line endings, ignored `#SBATCH` lines
moved up, job ids in log names, thread and binding settings, `module purge`, `wait`, … **Write** saves them as a copy
(the original stays) and opens it. Fixes that need your decision (memory units, time limits, paths) are not made.
"""),
  "LinkScreen": ("Link", """
A shell or drive of the cluster for your own terminal or file manager, through a relay: its state, logins, the
password and slurp's host key, and the commands for your machine (Linux, macOS, Windows, PuTTY/WinSCP, ssh config),
each with Copy. **New password** (the old one stops working), **Extend 1 h**, **Revoke** (no more logins, sessions end).

**On your Windows computer** (password links): the slurp helper opens the link with one click. **Open on my
computer** (slurp in your browser) hands it to the browser, which starts the helper; **Copy helper link** for Win+R
(from a terminal). The helper asks for the password, then maps the drive, opens the web view or a terminal, and keeps
it in its tray icon. **Set up the helper…** sets it up once on your computer.
"""),
  "HelperSetupScreen": ("The slurp helper", """
The tray program for Windows that opens links with one click and the password. **Download the setup** (when slurp is
open in your browser) gives `slurp-helper-setup.cmd` to run; **Copy the setup command** is the same for a PowerShell
window. What it installs (in your user, no admin), what it needs, and how to remove it: in the window.
"""),
  "LinkSetupScreen": ("Link setup", """
How a link for your own terminal works: the relay (public ssh-j.com, or your own SSH server), how many logins it
allows and how long, password or your public key, and for drives read only. A public relay sees only encrypted
traffic, but anyone who has the command and the password can log in until the link expires.
"""),
  "DriveScreen": ("Drive", """
Your login directory of the cluster as a drive: **on this machine** (sshfs through the tab's session, at the path
given, optionally read only; the connection window shows its state), or **on your own machine** (a link with commands
for sshfs, SSHFS-Win, WinSCP).
"""),
  "ShellChoiceScreen": ("Shell", """
Where the shell opens: **in slurp** (a subtab with a terminal), or **from your own terminal** (a link: an ssh command
and a password for the terminal on your machine, through a relay).
"""),
  "AddClusterScreen": ("Add a cluster", """
A new tab: an SSH destination (`user@host` or an alias of your `~/.ssh/config`, with its jump hosts and keys), or
blank for this machine. The login runs in the pane on the right.
"""),
  "InteractiveJobScreen": ("Interactive job", """
An `sbatch` command for a job that only holds nodes (`sleep infinity`), for shells on them from its job window. The
fields (account, partition, time, nodes, GPUs, name) write the command, which stays editable. Cancel it when you are
done: it is charged all the time it runs.
"""),
  "CommandScreen": ("Command", "A command to run, editable before it runs."),
  "GdbCommandScreen": ("GDB", "The GDB command for the core dump, editable; Run opens it in a shell subtab."),
  "RunArrayScreen": ("Run array", """
Submits the array again with another range (start, stop, step). It warns when the range covers tasks that are
running, queued or completed.
"""),
  "RestartScreen": ("Restart job", """
How to submit the job again: as it was submitted (with its original environment and script, if the cluster kept
them), only the array ids that failed, or a custom command (editable).
"""),
  "CopyScreen": ("Copy", """
The text was sent to your terminal's clipboard (OSC 52), which only some terminals accept. If it did not arrive:
**Plain text** shows it outside slurp to select with the mouse, **Save to a file** writes it to
`~/.cache/slurp/copied.txt`, **It arrived** stops asking.
"""),
  "ConfirmScreen": ("Confirm", "Confirm or cancel the action named (Esc cancels)."),
  "OptionsScreen": ("Options", """
The settings slurp keeps in `config.json` (the path is shown at the bottom), to see and change; **Save** writes them.

- **Appearance:** the theme in a terminal and the one in a browser (or the same). The theme follows the view that is
  active: when a browser takes over, its theme is used, when a terminal does, the terminal's. Choosing a theme with the
  command palette (`Ctrl-P`) sets it for the kind of view that is active.
- **Shells:** how many lines a shell keeps above its screen (scrolled back with the mouse wheel).
- **Copying:** for a terminal and for a browser: when copying cannot be confirmed (no clipboard tool here; in a
  browser always), ask how to get the text, or only send it (OSC 52) without asking.
- **Links:** the relay of links for your own terminal and browser.
- **Checks:** the rules not to report.
- **What slurp remembers:** drives on this machine, progress files and patterns, the tabs of the next start; Forget
  removes them (on Save).
"""),
}

def help_markdown(screen):
  """The help of a window: its text, its own keys (from its live bindings), the keys of everywhere"""
  entry = None
  for cls in type(screen).__mro__:
    entry = VIEW_HELP.get(cls.__name__)
    if entry:
      break
  title, text = entry or (getattr(screen, "sub_title", "") or type(screen).__name__, "")
  def rows(classes):
    out, seen = [], set()
    for cls in classes:
      if not cls.__module__.startswith("slurp"):
        continue
      for b in cls.__dict__.get("BINDINGS", []):
        if not isinstance(b, Binding) or not b.description or b.action in seen:
          continue
        seen.add(b.action)
        key = b.key_display or ", ".join(screen.app.get_key_display(Binding(k, b.action)) for k in b.key.split(","))
        out.append(f"| `{key}` | {b.description} |")
    return out
  own = rows(type(screen).__mro__)
  everywhere = rows(type(screen.app).__mro__)
  out = [f"# {title}", text.strip() or "_No description yet._", ""]
  if own:
    out += ["## Keys of this window", "", "| Key | |", "|---|---|", *own, ""]
  out += ["## Everywhere", "", "| Key | |", "|---|---|", "| `?` | this help, for the window shown (also the **?** button) |",
          *[r for r in everywhere if "this help" not in r and "| Help |" not in r], "", EVERYWHERE]
  return "\n".join(out)

class ViewHelpScreen(ModalScreen):
  DEFAULT_CSS = """
  ViewHelpScreen { align: center middle; }
  #vh-dialog { width: 110; max-width: 96%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
  #vh-doc { height: 1fr; }
  #vh-brand { height: 5; margin: 1 0 0 1; }
  #vh-logo { width: 17; }
  #vh-name { width: 1fr; padding-top: 1; }
  #vh-buttons { height: auto; align-horizontal: right; margin: 1 0 0 0; }
  #vh-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", "Close", show=False)]

  def __init__(self, markdown):
    super().__init__()
    self.markdown = markdown

  def compose(self):
    from .checks import DocViewer
    from rich.text import Text
    from .. import logo
    with Vertical(id="vh-dialog"):
      with Horizontal(id="vh-brand"):
        yield Static(Text("\n").join(logo.half_blocks()), id="vh-logo")
        yield Static(Text.assemble(("slurp", "bold"), "\n", (logo.TAGLINE, "dim"), "\n",
                                   ("jobs, files and nodes of Slurm clusters, in a terminal or a browser", "dim")), id="vh-name")
      yield DocViewer(self.markdown, id="vh-doc", show_table_of_contents=False, open_links=False)
      with Horizontal(id="vh-buttons"):
        yield Button("Guides (F1)", id="vh-guides")
        yield Button("Rule catalogue (F2)", id="vh-rules")
        yield Button("Close", id="vh-close", variant="primary")

  def on_mount(self):
    self.query_one("#vh-close", Button).focus()

  def on_markdown_link_clicked(self, event):
    event.stop()
    if event.href.startswith("rule:"):
      self.dismiss()
      self.app.action_rule_catalogue(event.href[5:])
    elif event.href.startswith(("http://", "https://")):
      self.app.open_url(event.href)

  @on(Button.Pressed, "#vh-guides")
  def guides(self, event):
    event.stop()
    self.dismiss()
    self.app.action_guides()

  @on(Button.Pressed, "#vh-rules")
  def rules(self, event):
    event.stop()
    self.dismiss()
    self.app.action_rule_catalogue("")

  @on(Button.Pressed, "#vh-close")
  def action_close(self):
    self.dismiss()

NO_HELP_BUTTON = ("ViewHelpScreen", "TerminalHelpScreen")

def add_help_button(screen):
  """The ? in the top right corner of a dialog (modal windows; the windows have theirs in the top bar)"""
  if type(screen).__name__ in NO_HELP_BUTTON or not screen.is_attached or screen.query(".help-corner"):
    return
  container = next((c for c in screen.children if c.display), None)
  if container is None:
    return
  try:
    container.mount(Button("?", classes="help-corner", compact=True, tooltip="about this window: what it shows, what it can do, its keys (?)"),
                    before=0)
  except Exception:
    pass
