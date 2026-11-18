"""The ? of every window: what the view shows, what it can do, its keys (from its live bindings)"""

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from .. import icons

EVERYWHERE = """
Tabs at the top are clusters (`+` adds one, `✕` closes one), and, while there are transfers, **Transfers** at their
left; under them the subtabs of the tab: **Jobs** and every window opened from it (`Ctrl-W` closes the one shown). `Ctrl-]` switches between the window and the shell pane on the
right. The footer shows the keys of the window and, on the right, the state of the tab's connection (click it for the
connection window). **? slurp** at the top right is about slurp in general: how it works, its version, the machine
it runs on.
"""

VIEW_HELP = {
  "MainScreen": ("Job list", """
Your queued and running jobs and those that ended recently, of the active tab's cluster.
It refreshes by itself every 60 s (`"refresh_seconds"` in the config): **⟳ 0:42** in the title bar counts down
to it; a click there switches automatic refreshing off (**⟳ off**) and on again, for the job list, the job window
and the array window (kept). `r` reloads at once.

- **Columns:** a click on a header sorts by it (again: the other way). **Progress** comes from `n/N` lines in the
  output (or `M%`, 0 to 100, in lines without one); **Done: current pace** estimates the end at the recent rate (the job window too; at the average rate since the start
  while there are too few updates for that).
  **Checks** shows errors ✖ and warnings ⚠ the checks found for the job; **Err** marks error files that are not empty.
- **Arrays** with ten or more tasks are one row: its state is the mix of the tasks' states (`30R 10PD 100CD`).
  `Space` (or ▸) lists the tasks under it, Enter opens the array window. `g` switches grouping off and on.
- **Loading:** the list shows as soon as Slurm answers; commands, error files, progress and checks follow (spinners).
  `r` reloads, `a` aborts a load. **finished since** chooses how far back ended jobs are listed, the dropdown
  next to it whose jobs (yours, an account's, all your accounts').
- **☰ Menu** (`F10`, at the left of the header, the top line), what slurp does for the tab, each with its key: the job list (`j`), Health (`C`), Quota (`Q`), old files in
  scratch (`X`, what the purge would take), Shell (`t`, in slurp or from your own terminal), Open a directory (`d`: home,
  scratch, project, … or a path), Map a directory (`F`: the login directory as a drive, on this machine or yours), Interactive job (`i`), New job script (`n`), Categories (`K`), this
  slurp in your browser (`W`, from anywhere, through a link), the guides, the rule catalogue, Options, About, Quit.
- **Categories**, like folders (`project X/production runs`): `m` marks a row (again: unmarks it; `M`: all or none),
  `c` puts the marked jobs (or the one under the cursor) in a category, an existing one or a new one. The
  **Category** column shows it (sort by it with a click); `/` with `cat:project X` lists only that category
  (`cat:-`: the jobs in none). The
  Categories window (`K`) has the tree, the numbers of each (node hours, …) and its jobs, also old ones.
- **Kept list:** the list of the last load is kept on this machine. When the cluster cannot be reached (or before it
  answers after a start) that one is shown, and the status line says it may be outdated; categorizing still works,
  the changes go to the cluster with the next load.
- `n` writes a new job script from a template; `!` shows only the jobs with problems; `/` filters by any text.
"""),
  "JobScreen": ("Job window", """
Everything about one job.
It refreshes by itself every 60 s (`"refresh_seconds"` in the config): **⟳ 0:42** in the title bar counts down
to it; a click there switches automatic refreshing off (**⟳ off**) and on again, for the job list, the job window
and the array window (kept). `r` reloads at once.

- **Category:** a click on it opens the categories window at that category, in a subtab of its own.
- **Cancel job** asks why (a list of the usual reasons, or your own words); the reason is kept with the job and shown
  here (**Cancelled because**) and in the categories window.
- **Notes…** (`o`): notes of the job, in Markdown (what this run was for, what came out); shown rendered under its
  details. Kept on the cluster with the categories, so slurp on the login node sees them too.
- **Array** (a task of an array): a click on it opens the window of the array, in a subtab of its own.
- **Buttons:** Reload (`r`: the job, its files and progress from the cluster again), Cancel job, Restart job (the original submission, another array range, or an edited command; with the
  environment and script it was submitted with, if the cluster kept them), Run array, Array (the window of its array, in a subtab of its own), Shell… (`s`, a running job: on
  which of its nodes, in slurp or your own terminal).
- **Info:** state (with the pending reason or exit code), account, partition, nodes, times, node hours, the command.
- **Checks:** what is wrong or could be better with the script and the job, most severe first; open an entry for why
  it matters and how to fix it, and its rule in the catalogue. **Check** runs all checks again: first the record,
  paths, quota and logs, then (a few seconds later) the script's environment: its setup replayed on the login node
  after a fresh login, the programs found and their libraries. **Placement** (running jobs) shows where tasks and
  threads run; **Fixed copy…** writes a copy of the script with the safe fixes; **What changed** (failed jobs)
  compares with the last run that worked.
- **Runtime diagnostics** (running jobs): on a node of the job or all of them, its memory, CPU and GPU use and load,
  I/O, local disks and busiest processes, in a subtab of its own (one per job and node: again, it measures again); the memory row shows its memory (MiB/GiB, as
  `free -h` and `nvidia-smi`). Repeated memory queries with plots (every 1 to 60 min).
- **Progress:** the latest `n/N` (or `M%`) line with an estimate of the end (a job that ended: where its file got to
  and how it ended); its **?** explains what it needs and how the estimates are made; **Progress file…** picks another log file.
- **Files:** output, error, script, working directory and files the job names; Enter opens one.
- **Steps** of the job; for a running job **Run on the nodes** (a command on every node, or one).
"""),
  "ArrayScreen": ("Array window", """
One job array as a whole.
It refreshes by itself every 60 s (`"refresh_seconds"` in the config): **⟳ 0:42** in the title bar counts down
to it; a click there switches automatic refreshing off (**⟳ off**) and on again, for the job list, the job window
and the array window (kept). `r` reloads at once.

- **Overview:** what it is, how many tasks in which state, node hours, queue waits and run times.
- **By state:** the task ids of every state (ended ones also by exit code, pending ones by reason), compressed for
  copying (Copy); a click on an id opens that task's job window, in a subtab of its own.
- **Tasks:** every task with its progress bar; a click on a header sorts, a click on a task opens it (in a subtab).
- **Cancel** the whole array or only its pending tasks (after asking). `r` reloads.
"""),
  "FileScreen": ("Text file", """
A text file (or command output) of the cluster, read in parts. Where it opens depends on where it is opened from:
a job's output, error and progress file (the job window, the job list's Err) at its end, followed while the job
runs; everything else (the directory browser, a path in a text, files of the script) at its top, not followed.

- **A large file works like `less`:** it is never read as a whole. A window of it (4 MB at most) is shown;
  scrolling past its top or bottom reads the part before or after (1 MB each, the farthest part goes). `g` / `G`
  the file's start and end, `:` a line (`12000`) or a place in it (`50%`). The status line says which lines and
  how far into the file; the line numbers are the file's (counted on the cluster for a part in the middle).
- `/` searches (in the part shown, then the rest of the file, on the cluster), `n` / `N` next and previous match.
- `Space` / `b` a page down / up, `j` / `k` a line.
- **Follow** (`f`) shows what is appended, like `tail -f`; **Watch** (`w`) reruns a command output every 5 s.
- Paths and `file:line` references in the text open with Enter on them (or a click).
- `r` reloads.
"""),
  "DirScreen": ("Directory", """
A directory of the cluster.

- The path at the top is an input: edit it and Enter goes there (`~` is your home; `Esc` puts it back). The buttons next
  to it go to your home, scratch, project, … (`$SCRATCH`, `$PROJECT`, `$STORE`, `$WORK`, those set on the cluster).
  **Shell here** (`s`) opens a shell in a subtab that starts in this directory (your login shell, on the cluster).
- Enter opens an entry: folders here (← goes back), files in a subtab of their own (the directory stays), in the
  matching view (text, Markdown, HDF5, core dumps, batch scripts in the script view with their checks). A file
  open already: its subtab.
- The columns: size, time, the permissions as `ls -l` shows them (a `+` when there are extended ACL entries), owner
  and group, **You**: what you cannot do (**no read** in red: you cannot read the file or list and enter the
  directory; **no write** in yellow), as the cluster answers for you (groups and ACLs count), and the extended ACL
  entries (`u:bob:r-x`: user bob, `g:` group, `m:` mask, `d:` default for new entries). The line above them shows
  the same of the directory itself.
- A click on a header sorts by that column; `.` shows hidden files.
- `/` searches by name: a part of the names or a pattern (`*.out`, `run_?`), any case. Here, as you type; with
  **Subdirectories** (`Ctrl+R`) in all directories below, in the background: the results come in as they are found
  (with their paths from here), the window stays usable, and it stops at 5000. `Esc` stops a search that runs, then
  shows the directory again.
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
  on the login node after a fresh login (in the job's uenv, `--uenv` and `--view`, if it has one), the programs found
  and their shared libraries (`ldd`). Opening and saving the
  script run it too; while you edit, Check checks the edited text, saved or not.
- **Edit** (`e`) makes the script editable right here: the checks follow as you type (the script, and its
  paths and programs on the cluster), Go to line jumps into it, `Ctrl+S` saves, `Ctrl+Z` undoes, **Reload**
  (`Ctrl+R`) reads the file into the editor again (asking about unsaved changes), `Esc` or **Done** stops editing (asking about unsaved changes, as do closing the subtab and quitting).
  Save asks before it overwrites a file that something else changed meanwhile. Bash is highlighted when Textual's
  syntax extra is installed (`pip install "textual[syntax]"`). **Edit outside** opens your own `$VISUAL`/`$EDITOR`.
- **Reload** (`Ctrl+R`) reads the script from the file again and checks it, also while you edit (the editor gets the
  file, and you go on editing).
- **Test** asks Slurm whether it accepts it and when it would start (`sbatch --test-only`). **Fixed copy…** writes a copy with the safe fixes.
  **Submit…** shows the `sbatch` command to edit (after naming the errors found); **Array** makes it
  an array job (`1-100`, `0-9:2`, `1,5,7`, `%50`: at most 50 at a time), written into the command's `--array`. `r` checks again.
"""),
  "RuleCatalogueScreen": ("Rule catalogue", """
All rules of the checks, by category: what each finds, why it matters, how to fix it, often with an example.
The contents on the left jump to a rule; rule ids in the text are links. Findings everywhere link here.

The search at the top (`/`) shows only the rules that contain all its words (in the id, title, texts or example; an
id like E01 or a word like memory, uenv, error), as you type; Enter goes to them, `Esc` shows all again.

Some rules are about one software stack: environment modules (E01, E04) or uenv (E05 to E08). Those that do not
apply on this tab's cluster (it has no modules, or no uenv) are greyed out, here and in the contents.
"""),
  "GuidesScreen": ("Guides", """
How to use the cluster well, with this cluster's numbers: Slurm basics, job scripts, common mistakes, arrays and
dependencies, software, filesystems and quotas, NUMA placement, GPUs, failed jobs. Rule ids are links into the
rule catalogue; the contents on the left jump to a section.
"""),
  "QuotaScreen": ("Quota", """
Your quotas (and those of your groups where they use space or have a limit) on every filesystem: space and files,
used, soft limit, limit, and a bar coloured by how full it is; also what the site's own quota command says. A full
quota (space *or* files) makes writes fail. `r` asks again, `Esc` closes the window.
"""),
  "ScratchScreen": ("Old scratch files", """
Your files in `$SCRATCH` with the oldest access (or modification) time: the ones a purge deletes first. Older than the
given days: red, close to it: yellow. An exclude regex leaves paths out.

- `Space` selects, `A` selects all; **Touch selected / all listed / all my files there** set their access time to
  now (after asking). Many sites do not allow keeping files this way: move what you keep to permanent storage.
- `r` searches again (it can be aborted).
"""),
  "MenuScreen": ("Menu", """
What slurp does for the tab, each with the key that does the same from the job list or the overview: the job list (`j`),
the health of the cluster (`C`), your quotas (`Q`), your oldest files in scratch (`X`), a shell on the cluster (`t`), a directory
(`d`: home, scratch, project, … or a path; each time in a new subtab) and Map a directory (`F`: the login directory as a drive), a transfer of data between two tabs (`T`), an interactive job (`i`), a new job script (`n`), the
categories (`K`), this slurp in your browser (`W`), the guides (`F1`), the rule catalogue (`F2`), the options (`F3`),
about slurp, and quit (`q`). Enter or a click does the item; `Esc` or a click elsewhere closes the menu.
"""),
  "TransfersScreen": ("Transfers", """
The transfers of data between the tabs (clusters, or this machine): the first subtab of the **Transfers** tab, which is
there while transfers exist (also finished ones). Each row: its name, from where and to where (each tab with a dot for
its connection, and its path, a long one shortened in the middle: /very/long/…/in/its/path; several sources: the first
and +n), what moves the data (rsync, rclone, Globus), its route and where it runs, its state, how far it is, its rate and the time left, and when it runs next
(or when it ran last). The lines below say more of the highlighted one (why it waits, how it failed,
its full paths). **Endpoints** (Menu → Endpoints…) lists where data can come from and go to.
Enter or a click opens its subtab with everything about it. A click on a column's header sorts the list by it (▲),
another click the other way (▼); by State the running ones come first; the order is kept for the next start.
**Reload** (`r`) asks again now (also automatically, counted down in the title bar as in the other views; a click
there turns it off and on): the runs on the clusters and at Globus, and the S3, SMB, WebDAV and Globus endpoints.

**New transfer** (`n`, or Menu → Transfer data…, `T`) sets one up in a wizard. `p` pauses (a transfer job is
cancelled for it: slurp asks first), `c` continues (rsync goes on where it stopped: what is there is not copied again),
`s` runs it now, `e` changes its settings (in the wizard), `Del` deletes it (its settings and
history; the files stay). Keys that act on a cluster do nothing here: its tab first.
"""),
  "TransferScreen": ("A transfer", """
One transfer: its state at the top, in big (RUNNING with how far and how long, PAUSED,
WAITING and for what, QUEUED, DONE, FAILED and why, …), the source and the destination as cards (the endpoint, its
state, e.g. ● connected or ○ needs login, its paths, and a button for what it needs: Connect, Open its tab), whether both sides are in sync (● in sync, ● not in sync with what is left, ○ not known:
from the newer of the last check and the last run that ended well, and when), how the data flows (a picture: the tabs as boxes with their connections, slurp between them
for the relay; on the arrow push, pull, relay or the same filesystem; under each box what runs there; while it runs a
dot moves along the arrows), the paths on the cards (links: a click opens one in the tab of its cluster, a
folder as a directory view, a file in its view, a file that cannot be shown or a pattern such as `*.log` as its folder
searched for it, a destination not there yet as the nearest folder that is), how far it is (bytes, rate, time left, files; for the relay also the rate through slurp), the file it
copies now, its rate over time, what happens if slurp closes (in slurp: it stops as paused and Continue goes on;
detached on the cluster or as a transfer job: it goes on without slurp), the errors rsync reported for
single files, **Runs** (how many times it ran, how they ended, the last one; a click: the list of its runs), what the
check found (**Check again** checks it anew as it is set up: what remains, both sides, the routes; not while a run
goes on), and its log.

**Pause** (`p`) stops the run; rsync keeps the half-copied file in `.slurp-partial` and the next run goes on from it.
**Continue** is there only for a transfer paused mid-run: it goes on where it stopped. A transfer runs when you click
**Run now**, which waits until every endpoint it needs is connected (its tooltip names what is missing).
A transfer job is cancelled for it (it loses its place in the queue): slurp asks first, and **Continue** (`c`) submits a
new one. **Run now** (`s`), **Edit…** (`e`), **Its job** (the job window of its transfer job), **Copy as a command**
(the rsync line of a run, or its `sbatch --wrap`, to put into scrontab or cron yourself: slurp has no schedules),
**Delete**. A transfer is idempotent: running it again copies only what is missing or changed, and
nothing is ever deleted on the destination. With an S3 endpoint (rclone): **Verify** (`rclone check`: is everything of
the sources on the destination, the same?), and a file half copied when it was paused starts over at Continue.
"""),
  "TransferRunsScreen": ("Runs of a transfer", """
Every run of the transfer, the last first (the history keeps the last 50): its number, when it started and ended, how
long it took, how it ended (done, done with errors, paused, failed, interrupted), how much it copied, how many files,
its rate, the route and where it ran (its job, the host), and why it ended so. Enter or a click opens a run's
details; `Esc` (or ←) goes back to the transfer.
"""),
  "TransferRunScreen": ("A run of a transfer", """
One run: when it started and ended and how long it took, how it ended and why, what it copied (of how much), its
average rate, the route and where it ran (its transfer job, the login node, its folder on the cluster), the sources and
the destination, its options, rsync's statistics (files, how many it copied, sizes, bytes sent and received), the
errors rsync reported for single files, and the events of the run. **Its job**: the job window of its transfer job.
`Esc` (or ←) goes back to the list of runs.
"""),
  "TransferWizard": ("Transfer data", """
Sets up a transfer in five steps; **Next** waits until a step is complete (its tooltip says what is missing). The
steps at the top can be clicked: any step whose steps before it are complete. Editing a transfer (**Edit…**), every
step is a click away and the buttons are **Cancel** and **Save**: Save keeps the changes (nothing
starts now) once every step is complete; its tooltip says what is missing. An endpoint that is not connected does not
hold it up: its paths stay as they are (they can be changed once it is connected), the options, the route and where it
runs can be changed, and the check is not run again (what the last one found stays; Run now checks first).

1. **Source**: an endpoint (this machine, or a tab, with its state; **Connect** or **Log in in its tab** when it
   needs it) and one or more files or folders there (Browse…: Space marks several; or type a path), or
   patterns such as `/path/to/dir/*.h5` (typed, or in Browse… the pattern field, which shows its matches, and **Choose
   the pattern**): each match is copied. Braces as bash's: `run_{1..1000..20}`, `{in,out}/*.h5`, `step_{001..100}`
   (one line, all its paths; at most 10,000; no `$(…)`, no variables). Each is copied into the destination folder under its own name. Each shows what it holds, counted on its tab in the
   background (its size, files, folders, links), and in all.
2. **Destination**: an endpoint and a folder there (created if it is not there), with its home, scratch, project, …
3. **Options**: how rsync copies, the same at every run: checksums (they read every file on both sides at every run,
   also when nothing changed: the estimate counts it), compression (used only on the relay), a speed limit in MB/s.
4. **Route**: slurp checks both sides and changes nothing: rsync on both, the paths, what remains to copy (an rsync
   dry run), the room there (free space, quota, a scratch that is purged). Each route is a choice that shows what its
   check found, as it comes: ✓ passed (its speed, an estimate; the recommended one is chosen), ! only with the
   temporary key, ✗ failed (why; it cannot be chosen): the same filesystem (a marker file, removed at once), direct
   (one cluster logs in to the other by itself), the relay through slurp (always possible). Any route will do (also
   one that failed, or is not checked yet: **Run now** checks first and runs only if it passes). For large transfers it suggests Globus or a similar service, where both sites offer it. A
   picture shows how the data flows (push, pull, relay through slurp, the same filesystem, where rsync runs), the
   bandwidth measured under its arrows and the estimate below it (with checksums: their time too). A direct route
   as a transfer job is checked from a node of its partition too (srun): refused there, Next refuses. As a transfer
   job: its account (a list of yours there to choose from; a field where none can be found), and its batch script tested with `sbatch --test-only` as soon as
   it is chosen and when its partition, time limit or account change: when and where it would start, or Slurm's
   error (then Next refuses). Below: where it runs (in slurp; detached on the cluster's login node; as a transfer job
   where the site has a transfer partition, e.g. xfer), and for a direct route that cannot log in by itself the
   **temporary key** (opt-in, with its warning).
5. **Summary**: a name, everything chosen, the estimate, what happens if slurp closes; **Add**: the transfer is
   added and runs each time you click **Run now**.
"""),
  "EndpointsScreen": ("Endpoints", """
Where transfers take data from and put it: this machine, every tab (a cluster tab is an endpoint by itself, as long
as it is open), S3 buckets, SMB shares, WebDAV folders and Globus collections. Each row: its name, its kind, where it is (a tab linked to a Globus collection:
which), its state (● connected, ○ needs login, connecting, needs you, tab not open; Globus: not set up until you log
in, expired or needs consent: log in again) and how many transfers use it. **Connect** logs in (a tab: in its tab;
Globus: the Globus login if there is none, then a test), **Disconnect**, **Test**, **Edit…** (a cluster tab: its SSH
settings; a Globus endpoint: its collection; S3, SMB, WebDAV: its settings and secrets), **Globus link…** (`g`, a
cluster tab: the Globus collection of its site), **Delete** (refused while transfers
use it; its keys leave the keyring too; a tab goes away when it is closed). An S3 endpoint is "not set up" while the
keyring lacks one of its keys. S3, SMB and WebDAV: rclone moves the data; secrets are kept only in the system keyring.
**Enter** or a click opens its window: a tab's connection window, or the window of an S3, SMB, WebDAV or Globus endpoint.
"""),
  "EndpointScreen": ("An endpoint", """
The window of an S3, SMB, WebDAV or Globus endpoint, as a tab's connection window: its border says its state (green:
connected, yellow: not tested or connecting, red: failed or not set up). **Settings**: its address and what its form
holds; its secrets only as there (✓) or missing (✗) in the keyring, never their values. **Server**: what its server
says of itself without a change: WebDAV its software (the Server header, Nextcloud's or ownCloud's product and version)
and WebDAV classes; S3 its software and the bucket's region; SMB the protocol it speaks (SMB 2.1, 3.0, …), signing,
encryption and its other offers, the logins it takes, Samba; its space where the server tells (total, used, free);
Globus what Globus knows of the collection (owner, organization, GCS version, storage). Asked when the window opens and
after each test. **Connection**: its state, its
last test (when, how long it took), what reaches it (rclone on this machine, as a remote of its environment; or Globus,
and whether you are logged in and gave consent). **Used by**: the transfers that use it. **Course of its tests**: each
test, with its result. **Test** (`t`), **Edit…** (`e`), **Disconnect**, **Copy address** (s3://…, //server/share, the
URL, the collection).
"""),
  "RcloneEndpointScreen": ("An S3, SMB or WebDAV endpoint", """
An endpoint rclone moves data to and from. **S3** (AWS or S3-compatible: Ceph, MinIO, a site's object store): the
provider, the endpoint URL (empty: AWS), the region, the bucket, and its keys (access key, secret key; a session token
for temporary keys). **SMB** (a Windows or Samba share): the server, the share, your user, the domain, the port, and
the password. **WebDAV** (Nextcloud, ownCloud, SharePoint, …): the URL, the vendor, your user, and a password or a
token. Each has a name and its folder (where Browse… starts). The secrets go to the keyring of this machine only:
editing, an empty field keeps the one there. **Test** lists the folder; with **as a destination** it also writes a marker object
(`.slurp-test-…`) and deletes it, and says when it cannot (no right to write). rclone moves the data (it must be
installed on this machine, and for a tab's files on that cluster).
"""),
  "NewEndpointScreen": ("New endpoint", """
Choose the kind of the new endpoint, then **Next** for its settings (`Esc` or **Cancel** closes it). A cluster is
added as a tab (`+`): every open tab is an endpoint by itself. A kind that cannot be made here says why (Globus needs
the Python package globus-sdk and a keyring); the kinds this version cannot make yet are not offered.
"""),
  "GlobusLoginScreen": ("Log in to Globus", """
One Globus login for all collections. The first time, step 1: the Globus app slurp logs in as (Globus lets programs
log in only as a registered app): **Open the Globus developers page**, register "a thick client or script that will be
installed and run by users on their devices" (redirect https://auth.globus.org/v2/web/auth-code), and paste its Client
UUID (**Use it**; kept in slurp's settings; **Another app…** changes it). Then the login: **Open the login page** (or
**Copy its link** into a browser, e.g. when slurp runs over SSH), log in there with your institution, allow slurp, and
paste the code Globus shows into the field, then **Log in**. slurp keeps the login (a refresh token) only in the keyring
of this machine. Collections that
need a consent to their data (mapped collections of Globus Connect Server 5) are asked for in the same login.
"""),
  "GlobusEndpointScreen": ("A Globus collection", """
A Globus endpoint: its name, its collection (**Search** by name at Globus, or paste its id; a click on a result chooses
it), and its folder (where Browse… starts; empty: your home there). Linking a tab: the collection of its site (the
site's suggestion, where its rule pack names one), and which folder of the tab is which folder in the collection (the
paths of transfers under the folder of the tab map to it); **Remove the link** unlinks it. **Test** asks Globus for the
collection and lists the folder. **Log in to Globus** when there is no login yet (search and Test need it). Globus
moves the data between collections itself: not through this machine, and on without slurp.
"""),
  "PickPathsScreen": ("Choose paths", """
A folder of a tab: Enter or a click goes into a folder (`Backspace` or **Up**: the one above), the path at the top can
be typed (Enter goes there), the buttons go to home, scratch, project, … To copy: `Space` marks files and folders,
**Choose the marked** takes them; **Choose this folder** takes the folder shown; a pattern in the field below
(`*.h5`, `run_??.out`) shows its matches in the list, and **Choose the pattern** (or Enter) takes it: each match is
copied. Choosing the destination: **New folder…** makes a folder in the one shown (its name, then **Create** or Enter)
and goes into it, for **Choose this folder** (on a tab, Globus, S3, SMB or WebDAV; on S3 a folder is there once files
are in it).
"""),
  "OverviewScreen": ("Overview", """
The first subtab of every tab, always there (it cannot be closed): what a tab shows at the start, and when its other
subtabs are closed; **Overview** at the left of the subtabs shows it any time. In its middle:
the cluster (its site, the Slurm version, its nodes and partitions), its health (healthy, degraded or unhealthy, and
why), the SSH session, and the ways on: **Jobs** (`j`, with how many run and wait; *No Slurm available* where there
is none), **Quota** (`Q`) and **Health** (`C`, everything about the nodes and partitions); a line about the
transfers from or to the tab while they run or wait (a click: the Transfers tab). Below them the site's
status page and documentation (a click opens them). Not connected: **Connect**, and what happened to the connection (slurp does not log in at the start; a tab added
with `+` logs in right away); connected: **Disconnect**. While it connects or reconnects (its session does not answer
yet) it is not shown as connected: no Jobs and no health, what ssh printed last (a host key that changed: said
plainly), and **Abort** gives up the login. **SSH settings…**: how the tab logs in, its destination or
full SSH settings, what `~/.ssh/config` and `ssh -G` make of it, **Check**; saved, the next login uses them.
`r` asks the cluster again (it does by itself after 5 minutes). *At the start a tab shows* in **Options** (`F3`) can
open the job list instead.
"""),
  "ClusterInfoScreen": ("Health", """
The state of the cluster (a window; `Esc` closes it): health (nodes down, drained, in maintenance, not responding, with the reasons),
reservations, occupancy (busy and idle nodes, running and pending jobs and why they wait), the partitions, and the
node topology of every partition (CPUs, sockets × cores × threads, NUMA domains, GPUs, memory). The title says
healthy, degraded or unhealthy and why. `r` (Reload) asks again.
"""),
  "ConnectionScreen": ("Connection window", """
The SSH session of the tab: its state and the course of a (re)connect, the logged-in user and login node, the
resolved destination (and the tab's full SSH settings), traffic and round-trip times, the ssh process and its TCP
connection, and a log of events. **Check** tries the connection from scratch, as in the add dialog: precisely what is
wrong, with ssh's verbose log.

- **Reconnect** (`R`) logs in again; **Disconnect** ends the session until you press **Connect** (overview, job list). When the
  login fails, the cluster may be down or in maintenance: a site's status page (from its rule pack, e.g.
  status.cscs.ch) shows as a link here and in the status at the bottom right; a click opens it.
- **Drive:** your login directory mounted on this machine (state, Mount/Unmount/Remount/Force unmount).
- **Links:** shells and drives made for your own terminal: their state and sessions; select one for its commands,
  a new password, more time or Revoke. **Health** is as for the SSH login: healthy (a heartbeat's round trip), slow
  or unstable (reconnects in the last 10 minutes), no answer or relay lost. A drive lists the folders its clients
  mounted, one row each, with their reconnects. When you use slurp in a browser through a web link, its session is marked
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
A shell or drive of the cluster for your own terminal or file manager, through a relay: when it started and how long
it is still valid (until when; its relay's end if that comes first, e.g. pinggy's hour), its state, logins, the
password and slurp's host key, and the commands for your machine (Linux, macOS, Windows, PuTTY/WinSCP, ssh config),
each with Copy. **New password** (the old one stops working), **Extend lifetime…** (how much longer: 1 hour, 2, 4, 8, 12 hours, a day, two, a week; added to its end), **Revoke** (no more logins, sessions end).
A web link through Cloudflare has only its https address (no password): Revoke ends the tunnel, the address with it.

**On your Windows computer** (password links): the slurp helper opens the link with one click. **Open on my
computer** (slurp in your browser) hands it to the browser, which starts the helper; **Copy helper link** for Win+R
(from a terminal). The helper asks for the password, then maps the drive, opens the web view or a terminal, and keeps
it in its tray icon. **Set up the helper…** sets it up once on your computer.
"""),
  "CategoriesScreen": ("Categories", """
Your jobs in categories, like folders (`project X/production runs`): the tree on the left (with how many jobs each
has, the ones in the categories in it counted too), and for the one selected its numbers and its jobs.

- **Numbers:** jobs (arrays once, with their tasks), by state, how many of those that ended completed; node, CPU
  and GPU hours (from Slurm's accounting: nodes, CPUs, GPUs times run time); from the first submit to the last end;
  partitions, accounts. Slurm is asked for the jobs themselves (`sacct -j`), so old jobs count too, as long as its
  accounting keeps them.
- **Jobs:** a click (Enter) opens a job (an array: its window), in a subtab of its own. `m` marks a job (and moves
  on; again: unmarks it), `M` marks all of them or none, as in the job list. **Categorize…** (`c`) puts the marked
  jobs (else the one selected) in a category, **Take job out** (`x`) takes them out of theirs.
- **not categorized** (at the end of the tree): the jobs of the job list (queued, and those finished since …) that
  are in no category, with their numbers; `c` sorts them in.
- **New** (`n`, in the category selected), **Rename** (`e`: another path moves it, with all it has), **Delete**
  (`Del`: its jobs go to the category it is in). **In the job list** (`l`) filters the job list to the category
  (`cat:` in the filter). **Reload** (`r`) reads the categories and asks Slurm again.
- **Notes…** (`o`): notes of the category, in Markdown (what it is for, how the runs differ, links); shown under its
  numbers. A rename takes them along, a delete removes them.
- They are kept on the cluster (`~/.config/slurp/categories.json` in your home there), per Slurm cluster: slurp on
  your own machine and slurp on the login node see the same.
"""),
  "CategorizeScreen": ("Categorize", """
Put the jobs in a category: pick one in the tree, or type a new one (`a/b` makes b in a, and a if it is new).
An array goes in as a whole, with all its tasks. **Take out of its category** leaves them in none.
"""),
  "CancelScreen": ("Cancel a job", """
Cancelling cannot be undone; the dialog shows the `scancel` command. It also asks **why** (you may also not say):
submitted twice (name the one that stays: a searchable list of the job list's jobs, those that look the same
first, ★), the program hangs, wrong
resources or account, a job it depends on failed (with how Slurm does that for you: `--dependency=afterok`, and the
guide), a bug, wrong input, replaced by a newer job, too slow, out of memory or quota, waits too long, a problem of
the cluster, not needed any more, a test, or your own words. **Details** add to any of them.

The reason is kept with the job, on the cluster (next to the categories): the job window (and the array window)
show it, with the other job as a link, and the categories window has a column for it.
"""),
  "FilePickScreen": ("Choose a file", """
A directory of the cluster, to choose a file from (the progress file of a job, …). Enter or a click goes into a
directory, or chooses the file; `..` or `Backspace` (**↑ Up**) goes up; the path at the top can be typed (Enter goes
there; `~` is your home); the filter keeps the files whose names contain what you type, or match a pattern
(`run_123*.log`, `step_??.out`, `*.[oe]*`); directories stay listed, and the filter stays when you change directory. Under the list: the end of the
file under the cursor, and (the progress file) whether slurp finds progress in it (`n/N` or `M%`). `Esc` cancels.
"""),
  "ExtendScreen": ("Extend the lifetime of a link", """
How much longer the link takes logins: 1 hour, 2, 4, 8 or 12 hours, a day, two days or a week, added to its end (to
now, if it has ended). It says until when. Sessions that are open go on either way; this is about new logins.
"""),
  "NotesScreen": ("Notes", """
Notes of a category or a job, in Markdown: `# headings`, `**bold**`, `- lists`, `` `code` ``, `[links](https://…)`.
The categories window (under the numbers of the category) and the job window (under its details) show them
rendered, with web addresses and the paths in them that exist on the cluster (`/scratch/…/out.log`, `~/input.dat`,
`$SCRATCH/run`, `logs/run.out` from the job's directory, also with `:line` or in `code`) as links: a click opens the
page in your browser, or the file or directory in slurp. **Save** (`Ctrl+S`) keeps them with the categories (on the
cluster); empty: no notes.
"""),
  "NameScreen": ("Category name", """
The name of a category: `a/b` is b in the category a (made too, if it is new). Renaming to another path moves it
with all it has; into a category that exists, the two are merged.
"""),
  "AboutScreen": ("About slurp", """
slurp in general (how it works, where to find what), its version (and the commit it runs from), and the machine it
runs on: host, system, CPUs, memory, load, user; how this slurp runs (in the background, its views), the active tab,
where its settings and logs are. **Copy details** copies the version and the machine as text, for a bug report.
"""),
  "HelperSetupScreen": ("The slurp helper", """
The program on your own computer that opens links with one click and the password: a tab for **Windows** (a tray
program) and one for **Linux** (a Python script, with dialogs and notifications). **Download the setup** (when slurp
is open in your browser) gives `slurp-helper-setup.cmd` or `slurp-helper-setup.sh` to run; **Copy the setup command**
is the same to paste into PowerShell or a terminal. What it installs (in your user, no admin or root), what it needs,
and how to remove it: in the window.
"""),
  "LinkSetupScreen": ("Link setup", """
How a link for your own terminal works: the relay (public ssh-j.com, or your own SSH server), how many logins it
allows and how long, password or your public key, and for drives read only. A public relay sees only encrypted
traffic, but anyone who has the command and the password can log in until the link expires. Web links can also
go through **Cloudflare**: a public https address that opens in any browser, nothing to install, no password (the
address is the key; Cloudflare can read what passes).
"""),
  "DriveScreen": ("Map a directory", """
A directory of the cluster as a drive (Map a directory…, `F`): **on this machine** (sshfs through the tab's session,
optionally read only; the connection window shows its state), or **on your own machine** (a link with commands for
sshfs, SSHFS-Win, WinSCP).

On this machine, **Directory** is the one of the cluster: choose it in the list (your home, scratch, project, …:
`$SCRATCH`, `$PROJECT`, `$STORE`, `$WORK`, those set on the cluster), or type a path next to it (`~/…` and relative
paths: in your home; the list then says Other). **Mount at** is an empty or new directory here. Both are remembered
for the tab; while it is mounted they stay as they are (Unmount first).
"""),
  "ShellChoiceScreen": ("Shell", """
Where the shell opens: **in slurp** (a subtab with a terminal), or **from your own terminal** (a link: an ssh command
and a password for the terminal on your machine, through a relay).
"""),
  "SshCheckScreen": ("Check of a connection", """
The check of an SSH connection, in a terminal, as the tab's login goes:

1. what needs no login: what ssh makes of the destination and settings (`ssh -G`), the keys (there, permissions, a
   passphrase), the host's name, the port, and the server's greeting; when one of these fails, it stops there;
2. the command before the login (Run before login), if there is one: answer it here (with "only when the login fails
   without it": first a login without it that asks nothing; the command only if that fails);
3. the login itself (`ssh -v`, its verbose log to a file): answer what it asks, a new host key (yes: it is kept, as
   at any login), a passphrase, a password, a one-time code.

Then the verdict and the findings (the server's methods, the keys tried, a changed host key, …) with the end of ssh's
log. All keys go to the terminal while it runs (`Ctrl-]` closes); then **Again** checks once more, `Esc` or **Close**
closes.
"""),
  "UpdateScreen": ("A newer slurp", """
slurp asks PyPI (`slurp-ui`) at the start, at most once a day, whether there is a newer version; then the title bar has
**⬆ slurp <version>**, which opens this. It says what changed (the changelog), how slurp is installed here (pipx, uv
tool, pip, or a git checkout) and the command that updates it (**Copy the command**), to run in a terminal. Then
start slurp again: quit it (`q`) and run `slurp`; the running one stays as it is until then. **Skip this version**:
no reminder until a newer one. No check at all: `"update_check": false` in the config.
"""),
  "WebEndingScreen": ("A web link ends soon", """
A web link (slurp in your browser) ends in a few minutes; the title bar counts down to it (⏱, left of ?).

- **It takes no more logins then** (its time limit): a browser that is open goes on, but reloading the page or a new
  browser needs a login. **Extend lifetime…** keeps it usable (how much longer: 1 hour to a week).
- **Its relay ends the tunnel** (the free pinggy relay keeps one for an hour): slurp opens a new one, with a new
  address, so a browser on the old one loses slurp. That cannot be extended; a relay without a time limit (Cloudflare,
  your own) avoids it.

**Its window** shows the link. You are warned once per deadline (again after an extension).
"""),
  "NodeChoiceScreen": ("On which node", """
The node of the job to query: all of them (the default) or one. Arrow keys and Space choose; `Esc` cancels.
"""),
  "DiagnosticsScreen": ("Runtime diagnostics", """
What a running job does now on a node (or all its nodes), measured over one second by a step of the job (`srun
--overlap`): **Again** (`r`) measures again.

- **Memory:** the job's cgroup (what Slurm counts against `--mem`) and its limit, with the page cache in it;
  out-of-memory kills (red) and how often it hit its limit; its processes' PSS and RSS; the node as `free` shows it;
  swap.
- **CPU:** how many of the job's CPUs are busy (yellow: most are idle), the node's use and load average.
- **GPUs:** the memory of the job's processes; per GPU its memory, how busy it is (yellow: hardly), memory bandwidth,
  temperature, power and its limit, clock (`nvidia-smi`; `rocm-smi` on AMD).
- **I/O** of the job (read and written per second), the **local disks** (`/tmp`, `/dev/shm`, which counts as memory,
  `$TMPDIR`), and its **busiest processes** (CPU %, memory, threads, state: `D` waits for I/O).

Memory in binary units (MiB, GiB), as `free -h` and `nvidia-smi` show it.
"""),
  "SubmitScriptScreen": ("Submit a job script", """
The `sbatch` command that submits the script, in its directory: edit it as you like (more options, a dependency,
`--begin`, …); the first word stays `sbatch`. **Array** makes it an array job: task ids like `1-100`, `0-9:2` (every
2nd), `1,5,7`, and `%50` for at most 50 at a time. The range writes the command's `--array` (the rest of the command
stays as you edited it); empty: one job, or the array the script asks for itself. It says how many tasks that is
and which ids, the first and the last (with a step the last is not always the end: `1-100:2` ends at 99, said in
yellow), and warns when the script never uses `$SLURM_ARRAY_TASK_ID` (every task would do the same). The button is red when
the checks found errors. `Esc` cancels.
"""),
  "OpenDirScreen": ("Open a directory", """
Which directory of the tab's cluster to open, in a new subtab (each time another one): your home (the default:
Enter), your scratch, project, … (`$SCRATCH`, `$PROJECT`, `$STORE`, `$WORK`, those set on the cluster), or a path
typed in the field below (`~/…` and relative paths are in your home). `Esc` cancels.
"""),
  "AddClusterScreen": ("Add a cluster", """
A new tab. The best way: an **SSH destination**, a Host of your `~/.ssh/config` with the cluster's user, jump hosts
and keys (suggested as you type; ssh, scp and your other tools use it too), or `user@host`; blank: this machine.

The fallback, **Full SSH settings**: host, user, port, jump hosts (ProxyJump), identity files (only these are tried),
the tab's name, and more ssh options, one `Key=Value` per line. They are kept with the tab.

**Run before login** (both ways): a command on this machine before every login of the tab, e.g. one that signs your
key for the day (`cscs-keygen`, `step ssh login`, `tsh login`). It runs in the login pane: answer it there (a
password, a one-time code; `Ctrl-C` ends only it); when it ends, the SSH login follows as usual, with what it exported
(`eval "$(ssh-agent -s)"` in it: the login uses that agent). **only when the login fails without it**: first a login
without the command that asks nothing; only if it fails, the command and the login again (a certificate still valid
is not made anew).

**Check…** tries the connection in a window with a terminal, as the tab's login goes: what needs no login (what ssh
makes of the settings, the keys, the host name, the port and the server), the command before the login if there is
one, and the login itself, where you answer what it asks (a host key, a passphrase, a password, a code). Then whether
it works, or what is wrong, precisely, with the end of ssh's verbose log. **Add** adds the tab, checked or not; the
login runs in the pane on the right.

From a tab's overview (**SSH settings…**) the same dialog shows the tab's settings, filled in, with the blocks of your
`~/.ssh/config` that apply (with their file and line) and what ssh makes of it all (`ssh -G`); **Save** changes the tab
(the next login uses it: **Reconnect**).
"""),
  "InteractiveJobScreen": ("Interactive job", """
An `sbatch` command for a job that only holds nodes (`sleep infinity`), for shells on them from its job window. The
fields (account, partition, time, nodes, GPUs, name) write the command, which stays editable. Cancel it when you are
done: it is charged all the time it runs.
"""),
  "CommandScreen": ("Command", "A command to run, editable before it runs."),
  "GdbCommandScreen": ("GDB", "The GDB command for the core dump, editable; Run opens it in a shell subtab."),
  "RunArrayScreen": ("Run array", """
Submits the array again with another range (start, stop, step). It says which ids that is (the first and the last:
with a step the last is not always stop) and warns when the range covers tasks that are running, queued or
completed.
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
  active: when a browser takes over, its theme is used, when a terminal does, the terminal's. **At the start a tab
  shows** its overview (the cluster at a glance; no subtab open) or its job list.
- **Shells:** how many lines a shell keeps above its screen (scrolled back with the mouse wheel).
- **Copying:** for a terminal and for a browser: when copying cannot be confirmed (no clipboard tool here; in a
  browser always), ask how to get the text, or only send it (OSC 52) without asking.
- **Links:** the relay of links for your own terminal and browser.
- **Progress of jobs:** how much of the end of a job's file is read for its progress (KiB, of it the last lines), and
  over how many progress values the current pace goes. More finds progress further back in a file that writes much
  else, and smooths the pace; but every running job's file is read that far at each reload and every 10 s in the job
  window: high values make slurp slow (it warns), over SSH most of all.
- **Checks:** the rules not to report.
- **What slurp remembers:** drives on this machine, progress files and patterns, the tabs of the next start (with
  their subtabs, which open again when a tab is connected); Forget
  removes them (on Save).
"""),
}

def progress_help_markdown():
  """What the progress of a job is, why it helps, how to get it, and how its estimates are made (the ? next to
  Progress file… in the job window)"""
  from ...cluster.jobs import progress_scan
  from .job import JobScreen
  scan = progress_scan()
  kib, lines, pace, every = scan["bytes"] // 1024, scan["lines"], scan["pace"], JobScreen.PROGRESS_EVERY
  return f"""# Progress of a job

**What it is.** slurp reads how far a running job is from a file it writes: the latest line with a count like
`step 450/1000` (*n/N*), or, in a line without one, a percentage like `training 45.5% done` (*M%*, 0 to 100). The bar,
the line and the estimates of when it is done come from there; the job list has the same bar and **Done: current pace**.

**Why it is useful.** You see at a glance how far each job is and when it will be done, without opening its log: whether
it will finish within its time limit (or needs more), whether it is stuck (the count does not move), whether a restart
made it faster or slower. For a job that ended, the progress shows where it got to (a failed job at `873/1000`).

## What it needs

A file in which the job writes its progress as it goes, in one of these forms, a line per step:

```
step 450/1000            epoch 3/20, loss 0.41        [2026-11-17 14:03:12] 450/1000 done
45%|████▌     | 450/1000 [00:10<00:12]        training 45.5% done
```

- **Which file:** by default the job's output (its stdout, `--output`), the easiest place: print the progress there.
  **Progress file…** chooses another one for this job (a log of the program, a file per task) in a window with the
  directory listing, which shows the end of the file under the cursor and whether it has progress in it; **Use
  stdout** goes back.
  For a task of an array, slurp guesses the files of the other tasks from the name of the one chosen.
- **The latest line counts**, n/N before M% in the same line (tqdm writes both: its n/N is exact). A count above its
  total, or a percentage over 100, is no progress.
- **Only lines like the progress, with the same N.** The progress lines of a file look alike (`step 450/1000 loss
  0.41`, the numbers aside) and count to the same N; a line of another structure (`checkpoint 1/1 saved`, `copying
  3/7 files`) or another N (`step 1/50` among `step k/1000`) is not progress, even if it is the latest. slurp keeps the
  kind of a job's progress (structure and N) from read to read; a new kind counts only when it repeats after it (the
  program went on to another phase: then its pace starts anew). The current pace compares values of the same N only.
- **Write it when it happens.** Programs often buffer what they print into a file: the progress then comes in chunks,
  late. In Python `print(..., flush=True)` or `PYTHONUNBUFFERED=1`; in C `fflush(stdout)`; `srun --unbuffered`.
- **A time in the line helps** (`2026-11-17 14:03:12`, `2026-11-17T14:03:12`, or `14:03:12` for today): the pace
  is then measured from when the steps really happened, not from when slurp saw them.

## How much of the file is read

Only its end: the last {kib} KiB, and of these the last {lines} lines (a carriage return counts as a line end, as
progress bars write). A file is never read as a whole: a large log costs nothing more. The job window reads it every
{every} s while the job runs (the line below the bar says when). The job list reads the files of all running jobs at
each reload, in one command, which sends back only their lines with n/N or M%.

How much (the KiB, the lines, and the progress values of the current pace) is set in the **Options** (F3,
*Progress*). More finds progress further back in a file that writes much else, and smooths the current pace over
more steps; but every running job's file is read that far at each reload and every {every} s in the job window: much
more makes the job list and the job window slow, over SSH most of all.

## The estimates of when it is done

- **On current pace** (shown when it can be told): the rate of the last {pace} different progress values, from the first
  of them to the latest: (latest n − first n) / (the time between them); done in (N − n) / that rate. The times are the
  timestamps in the lines when at least two of them have one (with the same N); else the times slurp saw a new n (at
  its reads, to the millisecond: every {every} s in the job window, at each reload in the job list; so a little late). It needs two values: right after the start, or with progress that rarely moves, it is
  not there yet.
- **On same pace** (only while the current pace cannot be told): the average rate since the job started:
  n / (the time of the latest value − the job's start); done in (N − n) / that rate. It counts the start-up (loading,
  compiling) as if it were steps, so it is often too pessimistic early and too optimistic when the job slows down.

Both assume the steps take the same time from now on. A job whose steps get slower (or faster) is done later (or
sooner) than they say.
"""

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

  def __init__(self, markdown, details=None):
    """details: text for Copy details (the About window: version and machine, for a bug report)"""
    super().__init__()
    self.markdown, self.details = markdown, details

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
        if self.details:
          yield Button(icons.label("copy", "Copy details"), id="vh-copy", tooltip="the version and the machine, as text (for a bug report)", compact=True)
        yield Button(icons.label("docs", "Guides (F1)"), id="vh-guides", compact=True)
        yield Button(icons.label("rules", "Rule catalogue (F2)"), id="vh-rules", compact=True)
        yield Button("Close", id="vh-close", variant="primary", compact=True)

  def on_mount(self):
    self.query_one("#vh-close", Button).focus()

  def on_markdown_link_clicked(self, event):
    event.stop()
    if event.href.startswith("rule:"):
      self.dismiss()
      self.app.action_rule_catalogue(event.href[5:])
    elif event.href.startswith(("http://", "https://")):
      self.app.open_page(event.href)

  @on(Button.Pressed, "#vh-copy")
  def copy_details(self, event):
    event.stop()
    from .dialogs import copy_text
    copy_text(self.app, self.details, "the details of slurp and this machine")

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

NO_HELP_BUTTON = ("ViewHelpScreen", "AboutScreen", "TerminalHelpScreen", "MenuScreen")

class AboutScreen(ViewHelpScreen):
  """The ? slurp of the top right corner: slurp in general, its version, the machine it runs on"""

ABOUT = """
slurp shows the jobs, files and nodes of Slurm clusters, in a terminal or in a browser. It runs on **this machine**
(below) and reaches each cluster from here: this machine itself (a local tab) or over SSH (a tab per destination,
one SSH connection each, with its jump hosts and keys from your `~/.ssh/config`).

- **Tabs** at the top are clusters: `+` adds one (blank for this machine, or `user@host` / an alias), `✕` closes one.
  Under them the **subtabs** of the tab: **Jobs**, and every window opened from it (`Ctrl-W` closes the one shown).
- **The job list**: your queued, running and recently ended jobs, with progress, estimates and the findings of the
  checks. A click (or Enter) opens a job: details, steps, logs that follow as they grow, nodes, placement, a shell.
- **Checks** look at job scripts and finished jobs for mistakes (memory, time, paths, MPI, GPUs, …); the rule
  catalogue (`F2`) lists them, the guides (`F1`) explain the background with the numbers of the active cluster.
- **Shells and drives**: a shell on the cluster in slurp (the pane on the right, `Ctrl-]`), or from your own
  terminal; your files as a drive on this machine or on yours. **Links** (an ssh command and a password, through a
  relay) bring shells, drives and this slurp itself (**Web**, `W`) to your own computer; the connection window (click
  the state at the bottom right) lists them.
- **Sessions**: slurp keeps running in the background (like tmux); `slurp` again attaches to it, from another
  terminal or a browser. `q` quits it.
- **Help**: `?` (or the **?** at the right of the title bar) explains the window shown, with its keys; this page
  (**? slurp**) is about slurp in general. **☰ Menu** (`F10`) at the left of the title bar has what slurp does for the tab, and
  **Options** (`F3`), the settings.
"""

def _run(argv, cwd=None):
  import subprocess
  try:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
  except (OSError, subprocess.SubprocessError):
    return ""

def about_facts(app):
  """What the About window shows (blocking: git, cloudflared and the session holder are asked; run it in a worker)"""
  import getpass
  import importlib.metadata
  import os
  import platform
  import socket
  import sys
  import time
  from ... import VERSION_TEXT, __version__
  from ...links import cloudflare, holder
  from ...config import config_path
  import slurp
  pkg = os.path.dirname(os.path.abspath(slurp.__file__))
  f = {"version": VERSION_TEXT, "number": __version__, "package": pkg}
  commit = _run(["git", "log", "-1", "--format=%h %cs"], cwd=pkg)
  if commit:
    changed = _run(["git", "status", "--porcelain", "--", "."], cwd=pkg)
    f["commit"] = commit + (" (with local changes)" if changed else "")
  f["python"] = f"{platform.python_version()} ({sys.executable})"
  libs = []
  for name in ("textual", "rich", "asyncssh", "textual-serve", "aiohttp", "pyte"):
    try:
      libs.append(f"{name} {importlib.metadata.version(name)}")
    except importlib.metadata.PackageNotFoundError:
      libs.append(f"{name} (not installed)")
  f["libraries"] = ", ".join(libs)
  path = cloudflare.binary()
  f["cloudflared"] = f"{cloudflare.version(path) or '?'} ({path})" if path else "not here (downloaded when a web link through Cloudflare is made)"
  # the machine
  f["host"] = socket.getfqdn()
  f["hostname"] = platform.node()
  osname = ""
  try:
    osname = next((l.split("=", 1)[1].strip().strip('"') for l in open("/etc/os-release") if l.startswith("PRETTY_NAME=")), "")
  except OSError:
    pass
  f["os"] = f"{osname or platform.system()}, kernel {platform.release()}, {platform.machine()}"
  cpus = os.cpu_count() or 0
  try:
    usable = len(os.sched_getaffinity(0))
  except (AttributeError, OSError):
    usable = cpus
  f["cpus"] = f"{cpus}" + (f" ({usable} usable by slurp)" if usable != cpus else "")
  try:
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if l.split(":")[0] in ("MemTotal", "MemAvailable")}
    f["memory"] = f"{mem['MemTotal'] / 2**20:.1f} GiB, {mem.get('MemAvailable', 0) / 2**20:.1f} GiB available"
  except (OSError, KeyError, ValueError, IndexError):
    pass
  try:
    f["load"] = " ".join(f"{x:.2f}" for x in os.getloadavg()) + " (1, 5, 15 min)"
  except OSError:
    pass
  try:
    up = float(open("/proc/uptime").read().split()[0])
    f["uptime"] = f"{int(up // 86400)} d {int(up % 86400 // 3600)} h" if up >= 86400 else f"{int(up // 3600)} h {int(up % 3600 // 60)} min"
  except (OSError, ValueError, IndexError):
    pass
  f["user"] = f"{getpass.getuser()} (uid {os.getuid()})"
  # this slurp
  try:
    started = os.stat(f"/proc/{os.getpid()}").st_mtime
  except OSError:
    started = None
  f["process"] = f"pid {os.getpid()}" + (f", running since {time.strftime('%Y-%m-%d %H:%M', time.localtime(started))}" if started else "")
  if holder.under_holder():
    status = holder.control("status") or {}
    views = status.get("views") or []
    f["session"] = (f"in the background (session holder {os.environ.get('SLURP_HOLDER')}), {len(views)} view"
                    f"{'s' if len(views) != 1 else ''} attached: " + ", ".join(v.get("label", "?") + (" (active)" if v.get("active") else "") for v in views)
                    + (f"; web view on 127.0.0.1:{status['web']}" if status.get("web") else ""))
  else:
    f["session"] = "directly in this " + ("browser (--serve)" if app.is_web else "terminal (no session holder)")
  f["view"] = (f"{getattr(app, 'view_kind', 'terminal')}, {app.size.width}×{app.size.height}, TERM={os.environ.get('TERM', '?')}"
               + (f", COLORTERM={os.environ['COLORTERM']}" if os.environ.get("COLORTERM") else ""))
  f["config"] = config_path()
  f["state"] = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "slurp")
  try:
    cluster = app.cluster if app.clusters else None
  except (AttributeError, IndexError):
    cluster = None
  if cluster is not None:
    c = cluster.connection
    f["tab"] = (f"{cluster.name}: " + (f"SSH {cluster.destination}" + (f", login node {c.hostname} as {c.user}" if c is not None and c.hostname else "")
                                        if cluster.destination else "this machine"))
  return f

FACTS = [("Version", [("slurp", "version"), ("from", "commit"), ("installed in", "package"), ("Python", "python"),
                      ("libraries", "libraries"), ("cloudflared", "cloudflared")]),
         ("This machine (slurp runs here)", [("host", "host"), ("hostname", "hostname"), ("system", "os"), ("CPUs", "cpus"),
                                             ("memory", "memory"), ("load", "load"), ("up for", "uptime"), ("user", "user")]),
         ("This slurp", [("process", "process"), ("runs", "session"), ("view", "view"), ("active tab", "tab"),
                         ("config", "config"), ("logs", "state")])]

def about_text(facts):
  """The facts as plain text (Copy details)"""
  out = []
  for title, rows in FACTS:
    out.append(title)
    out += [f"  {label}: {facts[key]}" for label, key in rows if facts.get(key)]
  return "\n".join(out)

def about_markdown(facts):
  def cell(text):
    return str(text).replace("|", "\\|")
  out = [f"# About slurp {facts['version']}", ABOUT.strip(), ""]
  for title, rows in FACTS:
    out += [f"## {title}", "", "| | |", "|---|---|", *[f"| {label} | {cell(facts[key])} |" for label, key in rows if facts.get(key)], ""]
  out += ["What changed from version to version: [CHANGELOG.md](https://github.com/chaoos/slurp/blob/main/CHANGELOG.md); "
          "the documentation: [chaoos.github.io/slurp](https://chaoos.github.io/slurp/)."]
  return "\n".join(out)

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
