# slurp

`slurp` is a Textual interface for checking Slurm jobs and inspecting
the files and resources associated with them. It is designed to run on a host
with access to your Slurm commands, configuration, and job files. You can use
it in a terminal or open it in a browser.

The images below are illustrative previews built from the app's current
features and sample data; they are not screenshots from a live cluster.

## Job list

Browse queued and running jobs alongside recently completed jobs, sort by
columns (the header marks the sort column with ▲ or ▼; Job ID cycles through job ID
descending and ascending and array index ascending and descending, marked △/▽), and filter by job
name, command, ID, state, user, account, or path. The window of finished jobs is
chosen from a list (only queued, 1 hour … 1 month) or typed (custom …, same
duration syntax as `slurm.py -c`, e.g. `3d12h`); each tab keeps its own. The dropdown next to it selects
whose jobs are listed: yours, those of one of your Slurm accounts, or those of
all of them (the accounts come from `sacctmgr`). Progress appears when the
output contains a line such as `65/100`; two columns estimate when a running job
gets to the end: **Done: same pace** (at the average rate since the job started)
and **Done: current pace** (at the rate of the last few `n/N` lines; `-` until there are two).
The list shows as soon as Slurm answers; the command, error file and progress of
each job (files on the cluster) follow, with spinners in those cells for jobs
not seen before (the others keep their values meanwhile). The sizes of the error
files of ended jobs are kept, and asked in one `stat` for many files; the progress
of all running jobs is read by one command that sends back only the `n/N` lines. An array with ten or more tasks in the list is
one row (`9600_[1-150] ▸ 150 tasks`) whose state is the mix of its tasks' states
in Slurm's short codes, coloured: `30R 10PD 100CD 10F` (running, pending,
completed, failed; CA cancelled, TO timeout, …); clicking ▸ (or Space) lists
every task under it and closes it again, and **group arrays** (or `g`) switches
between grouped arrays and every task. Enter or a click elsewhere opens the window
of the array: what it is, the node hours of all its tasks together, queue waits and run times (min, avg, median, max),
all its tasks by state (ended ones also by exit code, pending ones by reason)
with their number and ids (a click on an id opens the job window of that task), to copy, a list of every
task with its progress bar (sorted by the header clicked; a click on a task opens its job window), the nodes in use, and buttons to cancel the whole array or only its pending tasks
(after a confirmation). The job window of an array task has an **Array** button
for it.

![Illustrative slurp job list with status and progress columns](docs/jobs.svg)

## Job details and controls

Open a row to inspect accounting fields, steps, output and error files, the
batch script, and files referenced by the job. **Node hours (total)** is nodes ×
elapsed time (for a running job so far, with the most it can take by its time limit). **Cancel job** and **Restart
job** ask for confirmation; restart lets you use the original submission,
retry eligible array IDs, or edit a custom command (in a bash editor). If
the cluster keeps them, restart also offers the environment the job was
submitted with (`sacct --env-vars`: uenv, loaded modules, all variables; sbatch
then runs under `env -i` with exactly these, without the old job's `SLURM_*`)
and the batch script as submitted (`scontrol write batch_script` /
`sacct --batch-script`, saved as `.slurp-restart-<job>.sh` next to the job), so
the job runs again as it ran before even if the file on disk changed. For an
array task, **Run array** submits the array again with another range (start,
stop, step) and warns when the range covers tasks that are running, queued or
completed.

The progress shows the latest `n/N` line, and estimates when the job reaches
`N`: **on same pace** (the average rate since the job started) and **on current
pace** (the rate of the last few `n/N` lines), each with the remaining time and the time of day. The
times are the timestamps of the lines if they have one, else the times slurp
saw a new `n`. **Progress file…** picks another (log) file for this job, in
the directory browser; **Use stdout** goes back. For a task of an array, the
progress files of the other tasks are guessed from the name of the chosen one:
the index of the task (or its job id) in the name marks the task, other digits
(dates, times) may differ; the newest match is used, and tasks whose file is
not there yet are looked for again at every reload. Only the end of the file is
read (64 kB), every 10 seconds while the job runs (the window shows when it was
last read); a chosen file is also listed among the job's files as `progress`.

For a running job, press **Query current memory** to request current CPU RSS
and GPU process memory. The query is the script `slurp/scripts/memory_probe.sh`,
run on every node of the job as a step (`srun --overlap --input=all … bash -s`)
with the script on stdin, so nothing is copied to the cluster. Every query is
kept: the dropdown next to it repeats it every 1, 5, 15, 30 or 60 minutes (also
while the window is not shown), two sparklines plot CPU and GPU memory over
time (bars from zero, with the largest value and the time span), and the
measurements are appended to
`~/.local/share/slurp/memory/<tab>-<job>.csv`. This query is on demand; the fields are empty until
requested. GPU usage is gathered from the processes of the job reported by
`nvidia-smi` (or `rocm-smi` on AMD nodes), together with the used and total
memory of every GPU of the nodes. Completed and failed jobs show CPU peak memory from Slurm
accounting, and GPU peak memory when the accounting database reports it.

![Illustrative job overview with cancel, restart, progress, and memory controls](docs/job-details.svg)

## Files and node tools

The file browser opens from the job details and lets you move through the
working directory and related paths. It includes specialized views:

- **Markdown:** `.md`, `.markdown`, `.mdown`, and `.mdx` files render with
  Textual's `MarkdownViewer`, including a table of contents and link navigation.
- **HDF5:** browse groups, datasets, attributes, and paged data; a string
  dataset named `*.md` is rendered as Markdown. HDF5 files
  are recognized by their file signature, including files without an `.h5`
  extension.
- **Core dumps:** inspect ELF core metadata with GDB and edit the suggested GDB
  command before launching it in the terminal pane. Core-to-job association
  uses node and timestamp clues and is marked as likely/possible.
- **Text and output:** follow running stdout/stderr, search text, and open
  paths or line references found in file contents.

For a running job, run commands such as `nvidia-smi`, `free`, `top`, or `ps`
on its allocated nodes. Shells on its node and GDB sessions open as subtabs.

## Tabs and subtabs

Every tab is a cluster (or this machine). Under the tabs, each tab has its
subtabs: **Jobs** (the job list, always there), and one subtab with a `✕` for
each job window, each file or directory opened from the job list, and each
shell (on the cluster, on the node of a job, GDB). What you open inside a
subtab, like the files of a job, opens in that subtab; `←` or `Esc` goes back,
and back from its first window shows the job list. Opening a job again shows
its subtab. Switching tabs keeps the subtabs. In a shell all keys go to the
shell, except `Ctrl-]`, which goes back to the job list; `Ctrl-W` closes the
subtab shown (outside a shell). Every tab can be closed, also the last one;
then only the `+` button remains.

Every window has a **?** at the right end of its top bar (dialogs in their
top right corner), and the `?` key does the same: a window explains what the
view shows, what it can do and its keys (taken from the view's own key
bindings, so they are always current), with the keys that work everywhere and
buttons to the guides and the rule catalogue. In a shell it opens the
terminal help below.

Shells keep a history: the mouse wheel (or `Shift+PgUp`/`Shift+PgDn`) scrolls
back through the lines that went off the top (10000 by default,
`"terminal_scrollback"` in the config; `clear` empties it), with a bar saying
how far back; typing returns to the bottom. In full-screen programs (less,
vim) the wheel sends arrow keys to them. Dragging with the mouse selects text,
also in the history; a right-click on the selection copies it.

**ⓘ Help** in the bar of a shell (and `ⓘ` in the title bar of the login pane)
explains that terminal: scrolling back, how to copy out of it (drag to
select, right-click to copy; `Ctrl+C` interrupts and does not copy) and paste into it (your terminal's paste, not a
plain `Ctrl+V`), which keys go where, how to close it and that closing ends
everything running in it, and how to treat it: a limited history (use `less`
or `tmux` for long output), `tmux` or `sbatch` for work that must survive, and login nodes are
shared. Its **Copy the screen** button copies the visible screen as text.

![Illustrative Markdown, HDF5, and right-side shell views](docs/files.svg)

## Install and launch

Run the program from this repository on a login or other host with Slurm
access. The package directory `slurp/` must stay next to `slurm.py` (and the
modules it uses) in this repository.
Create and activate a virtual environment, then install the repository's
Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The requirements file includes the dependencies used by the repository's
scripts, plus the Textual app's terminal and browser features. System commands
such as `ssh`, `squeue`, `sacct`, `scontrol`, `srun`, and `sbatch` are installed
separately and must be available locally or on the selected SSH host.

The standalone `fzip.py` compression utility also uses the legacy `bitstream`
package, whose source build fails under pip's isolated build environment. It is
not needed by slurp. If you use `fzip.py`, install it separately with:

```bash
python -m pip install --no-build-isolation bitstream
```

`h5py` enables the HDF5 browser. If it is unavailable, the app can show
`h5dump -H` output when `h5dump` is installed.

Start the terminal UI from the repository:

```bash
python3 slurp          # or: python3 -m slurp
```

Running it again attaches to the slurp that runs already (see "One slurp,
several views"); `--new` starts a separate one, `--no-holder` runs it directly
in the terminal.

Use `--log FILE` to log Slurm commands and debug events. The app has no
required configuration file; job and file selection happens inside the UI.

## Connect to Slurm over SSH

The Textual app stays on your local machine. It starts with a **Local** tab.
Pass one or more regular OpenSSH destinations (`user@host`) or aliases from
your `~/.ssh/config` to add tabs:

```bash
python3 slurp --ssh localhost user@host cluster-login
```

OpenSSH uses your configured identities, certificates, agent, host aliases,
and `ProxyJump` chain. `localhost`, `127.0.0.1`, and `::1` refer to the local
machine tab.

Each remote tab has one SSH session, opened the first time you select the
tab and held open for the life of the app. The login runs in the terminal
pane on the right, where password, key passphrase, keyboard-interactive, and
OTP prompts are answered. Once you are logged in, the pane hides by itself
and the job list loads; nothing has to be typed after the login. A failed
login stays in the pane so the error can be read; Enter there logs in again,
and closing it (`✕`) gives up the connection.

The session runs a shell on the cluster that the app sends its commands to:
a reload (or opening a file, a directory, the job details) only sends lines
into the open session and costs a round trip; no ssh process is started for
it. Several commands can run at the same time, so a slow `sacct` does not hold
up browsing, and aborting one stops only that command. The login scripts of
the cluster run once, when the session starts, and what they print is
ignored. Interactive shells (Shell, node shells, GDB) open as extra channels
of the same connection, without a new login. Files are never copied: text
files are read in parts (the last 1 MB, `A` for the part before, following
reads only what was appended), and HDF5 files are read in place by h5py
through a file object that fetches only the byte ranges it asks for (1 MiB
blocks), so a large file costs only what is browsed.

Clicking the connection state in the footer opens the connection
window of the tab: its state, the course of a (re)connect (connecting,
waiting for input, connected, failed with the error and what ssh printed), the
logged-in user and login node, the resolved destination (`ssh -G`), traffic
and round-trip times of the command channel, the ssh process and its TCP
connection (bytes, rtt, retransmissions, from `ss`), and a log of the session
events. **Reconnect** ends the session and logs in again, **Disconnect** ends
it until the tab is selected again.

The footer shows the state of the session of the active tab, tinted by its
health: green (answers, with the round-trip time), yellow (slow, connecting),
red (no answer, lost); the connection window shows the same in its title and the tab buttons mark each tab: `●`
connected, `◌` connecting or not answering, `○` not connected. A heartbeat
notices a session that stops answering ("no answer for …" in the footer);
after 60 seconds without an answer, or when the session breaks, the app
reconnects by itself right away, and if that fails again after 5, 15, 30
and then every 60 seconds. If
the login needs a password, the pane appears for it. Commands that were
waiting, like a reload, go on once the session is back; a cancel or restart
of a job is never sent twice. The UI does not wait for the cluster at any
point: all commands and remote file access run in the background.

The remote host needs Slurm commands, a POSIX shell, and standard Linux file
tools; it does not need Python, a copy of this repository, or Textual
installed.

Each tab keeps its own job list, shell, and session. Switching tabs shows the
list as it was loaded; `r` (or **Reload**) loads it again. While a list
loads, the old one stays visible, the footer shows the progress, and `a` (or
the **Abort** button, which replaces **Reload** meanwhile) stops it.
**Shell** (or `t`) opens a login shell on the cluster of the tab in a subtab. The **+** tab button adds another local or remote connection.
`d` opens the login directory in the directory browser. Each tab has a `✕`
that closes it (after asking). Notifications name the tab they belong to.

**Interactive job** (or `i`) shows an `sbatch` command for a job that only
holds a node (`sleep infinity`) in a bash editor, with fields for the account
(one of yours or typed), the partition, the time limit, nodes, GPUs and the job
name; the fields write the command, which stays editable. Once the job runs, its job
window opens shells on its node. **Cluster info** (or `C`) shows the health
of the cluster (nodes down, drained, in maintenance, not responding, with
the reasons; reservations), its occupancy (busy and idle nodes, running and
pending jobs and why they wait) and the partitions, with its own Reload; its
title says healthy (green), degraded (yellow) or unhealthy (red) and why;
without a connection it says so, with Reconnect.

## Copying

Copy buttons send the text to the terminal (OSC 52, wrapped for tmux and GNU
screen) and put it on the clipboard with the tool of the machine slurp runs on
(`wl-copy`, `xclip`, `xsel`, `pbcopy`; over SSH only a forwarded display counts).
OSC 52 is the only way to your clipboard when you ssh to the machine slurp runs
on, and only some terminals take it (kitty, WezTerm, iTerm2, Alacritty, foot,
Windows Terminal; in tmux with `set -g set-clipboard on`; not GNOME Terminal or
Konsole). Without a tool a window says so and offers **Plain text** (slurp steps
aside and prints the text in the normal screen, where the terminal's own mouse
selection works; Enter comes back), **Save to a file** (`~/.cache/slurp/copied.txt`)
and **It arrived, don't ask again** (stored as `"copy": "terminal"` in the
settings; remove it to get the window again).

## Shells and drives from your own terminal

**Shell** (`t`, and `s` in the job window of a running job for its node) asks
every time: **In slurp** (a subtab, as before) or **From your own terminal**.
The latter makes a *link*: an `ssh` command and a password for the terminal or
SSH client on your own machine (OpenSSH in any terminal, cmd or PowerShell,
PuTTY, …). **Drive** (`F`) asks where your login directory goes as a drive:

- **On this machine** (where slurp runs): `sshfs` through the session of the tab
  (no login), at a path you choose (default `~/mnt/<tab>`, remembered per tab
  as `"drive_mounts"` in the settings), read only if you like. The connection
  window shows its state (connecting, mounted since, not answering,
  reconnecting, failed and why, or something else mounted there) and offers
  Mount…, Unmount, Remount and Force unmount. When the session of the tab
  breaks, the drive stops answering and comes back with the session; if sshfs
  gives up or dies, slurp mounts it again when the session is there. Quitting
  slurp (or closing the tab) unmounts it. Needs `sshfs` on this machine.
- **On your own machine**: a link (below): `sshfs` on Linux and macOS,
  SSHFS-Win on Windows, or WinSCP, Cyberduck and file managers.

The node of a job only gets shells, never a drive.

It works wherever your machine is, also when slurp runs behind several hops:
slurp runs a small SSH server for each link (on `127.0.0.1` only) and connects
out to a **relay**; your `ssh` connects out to the same relay and jumps through
it to slurp. The session stays SSH encrypted from your terminal to slurp; the
relay only passes bytes along. On the first connect your ssh shows slurp's host
key, whose fingerprint the link window shows (remembered under one name, so
it asks only once).

- **ssh-j.com** (the default) is a public relay run by a third party: each link
  gets a random name there. The setup panel warns about what that means
  (anyone with the command and the password gets in as you; the relay sees
  that connections happen; your site's rules may not allow it).
- **Your own relay**: any SSH server you can log in to with a key, e.g.
  `me@relay.example.org`; slurp asks it for a free port. Set it in the setup
  panel or as `"link_relay"` in the settings.

The **setup panel** of a link: the relay (with its warning), **Logins** (once,
the default for shells; any number until it expires, the default for drives,
since mounts reconnect), **Expires after** (15 minutes, 1 hour (default),
8 hours, until slurp quits), login with **a password** made by slurp (default)
or **your public key**, and for a drive **Read only**. A link stops taking
logins when it is used, expires, is revoked, or after 5 wrong passwords; open
sessions go on. The **link window** shows the commands for each kind of
client with copy buttons (and a `~/.ssh/config` entry), the password, the host
key and the state; **New password**, **Extend 1 h**, **Revoke**.

The **connection window** (click the status of the tab) lists the links of the
tab and their sessions (shell or drive, since when, client, traffic, terminal
size); a click on a link opens its window, on a session disconnects it.
Logins, wrong passwords and lost relays are notified, the footer shows open
links and sessions, and quitting with open sessions asks first. Every link and
session is written to `~/.local/state/slurp/links.log` (never a password);
slurp's host key is `~/.config/slurp/link_host_key`.

Links need the Python package `asyncssh` (`python -m pip install asyncssh`);
without it slurp says so in the Shell dialog and works as before. A drive uses
the cluster's `sftp-server` (read only needs it; otherwise the cluster's
standard SFTP).

## Checks: scripts, jobs, and the rule catalogue

slurp checks batch scripts and jobs against a catalogue of rules: beginner
mistakes and classic Slurm traps, requests that make no sense, paths and
programs that do not exist, full quotas, the software environment, NUMA
placement, and what a job really used. Every finding says what was found
(with the line of the script), why it matters and how to fix it, often with
a snippet to copy, and links to its rule in the **rule catalogue** (`F2`), a
subtab of its own (one per tab: a later rule link goes there and jumps to the
rule; the guides, `F1`, have theirs too). Code in the catalogue, the guides and
the help (and in Markdown files) shows as a framed, highlighted block.
The rules, by category:

| | Category | Examples |
|---|---|---|
| S | Script syntax and hygiene | Windows line endings, `#SBATCH` after the first command (ignored), misspelled options, `$VARS` in `#SBATCH`, `cd` without a check, background programs without `wait` |
| R | Requests that make no sense | `--mem=4` (4 MB), `--time=2:00` (2 minutes), more CPUs/GPUs/memory than a node has, time over the partition's maximum, unknown partition or account |
| B | Beginner mistakes | several nodes but no `srun`, arrays that never use `$SLURM_ARRAY_TASK_ID`, logs without `%j`, results left in the node's `/tmp`, `srun` in a loop, the same job twice, dependencies that can never be met, heavy work on the login node |
| P | Paths, files, programs | the log directory does not exist (the job fails without a log), missing directories and inputs, programs missing or not executable, built for another CPU, shared libraries not found, GPU code for other GPUs, changed since submission |
| Q | Quota and storage | writing where the quota (space or files) is full, little room left, output in `$HOME`, inputs close to the scratch purge, very many files in one directory |
| E | Environment | modules without versions or `module purge`, `conda activate` without the hook, `pip install` in jobs, modules that cannot be loaded, site advice |
| N | NUMA placement | tasks not aligned with NUMA domains, threads and tasks not bound, GPUs not assigned to tasks, `srun` without `--cpus-per-task` (Slurm 22.05+) |
| U | Usage and outcome | much more time or memory than used, low CPU usage, why a job failed (OOM, TIMEOUT, exit codes), why it waits (pending reasons), known errors in the logs, long jobs without checkpoints |
| G | Good practice | the script sets up its own environment instead of relying on the shell it is submitted from (compared with the environment Slurm kept of the submission, `sacct --env-vars`, where the cluster stores it, and by replaying its setup after a fresh login); failures that look like successes (`echo done` after `srun`, a failed step under a COMPLETED job); array logs without both `%A` and `%a` (the tasks race on one file, or the next run overwrites them); not submitting from inside a job; task and CPU counts from Slurm, not literal; runs that overwrite each other's files; a record of what ran; no interactive commands; the script works from any directory (relative paths before it sets its directory; relative logs with a `cd` but no `--chdir`); `$0` is Slurm's copy of the script |

**In the job list** the **Checks** column shows the errors (✖) and warnings
(⚠) of each job: the scripts of queued jobs, the records of all, and the paths
they name (checked on the cluster in one round trip, after the list is shown).
`!` shows only the jobs with problems.

**In the job window** the **Checks** section lists the findings, the most
severe first; each opens to show why it matters, how to fix it and the link
to the catalogue. It checks the script as submitted (or as the accounting
stored it), the record, the paths and programs, your quota where the job
writes, and the end of its logs, and then, a few seconds later, its
environment: the script's setup lines (module loads, exports, sources, `cd`;
never its commands) are replayed on the login node in a clean environment, as
after a fresh login, and the programs are checked: found at all (G01), shared
libraries (`ldd`), GPU code (`cuobjdump`). **Check** runs all of it again.
Its other buttons:

- **Placement** (running jobs) asks the nodes where the tasks and threads run,
  draws each node by NUMA domain with its GPUs, and names the problems: shared
  cores, unbound tasks, tasks spread over domains, GPUs of another domain.
- **Fixed copy…** writes a copy of the script with the fixes that are safe to
  make automatically (line endings, ignored `#SBATCH` lines moved up, `%j` in
  log names, `SRUN_CPUS_PER_TASK`, OpenMP binding, `module purge`, `wait`, …),
  shown as a diff first. Ambiguous ones (is `--mem=64` GB or MB?) stay with
  you. Submit the copy from the script view.
- **What changed** (failed jobs) compares the script, the command line and the
  request with the last run of the same job that worked.

**A batch script opened in the file browser** (a file with `#SBATCH` lines)
opens in the **script view**: the script with its findings below, each with
*Go to line*. **Explain** (`x`) annotates every `#SBATCH` line with what
Slurm makes of it (units, per node, the paths of the logs, IGNORED after the
first command). **Edit** (`e`) makes it editable right there, with Textual's
editor: the findings follow as you type (also the paths, directories and
programs on the cluster, a moment after you pause), `Ctrl+S` saves, `Ctrl+Z` undoes, **Revert** goes back to the file, `Esc`
stops editing; **Check** checks the edited text, saved or not; unsaved changes are asked about when you stop, close the subtab
or quit, and Save asks before it overwrites a file changed meanwhile. Bash is
highlighted with Textual's syntax extra (`pip install "textual[syntax]"`).
**Edit outside** opens your `$VISUAL`/`$EDITOR` instead. **Test** runs `sbatch --test-only` (accepted? when would it
start?). **Submit…** submits it after naming the errors found.

**New job script** (`n` in the job list) writes a commented script for the
usual kinds of jobs (serial, OpenMP, MPI, hybrid MPI+OpenMP, GPU, array,
dependency chain, long run with checkpoints), with the numbers of the
cluster's partitions (cores, NUMA domains, GPUs), creates `logs/` and opens it
in the script view.

**The guides** (`F1`) explain the background with this cluster's numbers:
Slurm in five minutes, writing a job script, common mistakes, arrays and
dependencies, software, filesystems and quotas, NUMA placement (a recipe
table for the node), GPUs, failed jobs. Their rule links open the catalogue.
**Cluster info** shows the node topology of every partition.

**Muting**: a finding's *Mute everywhere* stores the rule in
`"checks_muted"` in the config; *Mute in this script* (script view) adds
`# slurp: ignore R08` to the script, which works for any rule.

**Site rule packs** add what Slurm does not tell: scratch purge days, parallel
filesystems, NUMA domains per node, the GPU architecture, site plugin options
of `sbatch`, rules that do not apply, and site advice. A pack is a TOML file
in `slurp/rules/` (one for CSCS Alps is included) or in
`~/.config/slurp/rules/`; it applies when one of its `match` patterns matches
the tab, its SSH destination or the login node's host name. See
`slurp/rulepacks.py` for the keys.

**Without the UI**, `slurp check job.sh ...` prints the findings of batch
scripts (on the machine it runs on, e.g. a login node): exit code 1 if a script
has errors; `--all` adds the hints, `--no-probe` checks only the text,
`--rules` prints the catalogue.

## One slurp, several views: remote web access

`python3 slurp` does not run slurp in the terminal itself: it starts a small
*session holder* that runs slurp in the background (like tmux), and this
terminal shows it. Running `slurp` again (in another terminal, or after the
first one was closed or its SSH connection dropped) attaches to the same
slurp instead of starting another one; slurp keeps running until you quit it
(`q`). `--new` starts a separate one, `--no-holder` runs directly in the
terminal as before (ending with it).

**Web** (`W` in the job list) shows this same slurp in a browser, from
anywhere: it starts a web view on `127.0.0.1` (a random port and an access
token; nothing listens before you ask) and makes a *web link* through the
relay, like the links for shells and drives (relay, password or key, logins,
expiry, Revoke, listed in the connection window). The link only forwards to
the web view: no shell, no files. On your machine:

```bash
ssh -N -L 8000:127.0.0.1:<port> -J <name>@ssh-j.com slurp@slurp   # (the link window has the exact command)
# then open http://localhost:8000/?token=…
```

The browser is another view of the same slurp (textual-serve's page, a
terminal). **One view is active at a time**: its keys and mouse go to slurp,
and the screen takes its size. The others show a lock screen: who is active
(the terminal on which host, or which browser), since when, idle for how
long, its size, whether it is still connected, and the web links. **Take
over** (Enter, or a click) asks "are you sure?" and makes this view the
active one; the other one gets the lock screen, saying who took over.
**Watch** (`w`) shows the screen live and read only instead of the lock
screen, **Detach** (`d`) closes the view (slurp goes on). When the active view
goes away (a browser tab closed, a terminal gone), another one becomes active
by itself. Quitting asks when other views are attached, and closes them all.
When no web link is left (revoked, expired), the web view stops listening.

`--serve` (below) is different: every browser tab gets its own new slurp.

## Settings

slurp keeps a few settings in `~/.config/slurp/config.json` (or under
`$XDG_CONFIG_HOME`): the theme (chosen with the command palette, `Ctrl-P`;
`"theme"` in the terminal, `"theme_web"` in the browser with `--serve`, which
starts with the terminal's until one is chosen there), the
open tabs with the one shown and their choices (finished since, accounts),
the progress files chosen for jobs and the patterns for arrays. At the next
start the tabs are back, and the tab shown connects; the others connect when
they are selected. The dot of an SSH tab is coloured by the health of its
session (green, yellow, red; grey when not connected yet).

Settings you may edit by hand: `"terminal_scrollback"` (lines a shell keeps
above its screen, default 10000, 0 for none), `"checks_muted"` (rule ids the
checks do not report), `"link_relay"` (the relay of links), `"copy"`.

## Storage: quota and old files in scratch

**Quota** (or `Q`, a subtab that loads in the background) lists your quotas, and those of your groups where they
use space or have a limit: `lfs quota` on each Lustre filesystem and on
`$SCRATCH`, `$HOME`, `$PROJECT`, `$STORE`, `$WORK`, `quota` on the others,
with space and files (inodes), used, limit and soft limit, and a bar coloured
by how full it is. The output of a site's own `quota` command is shown too.

Scratch filesystems are usually cleaned up automatically: files not used for
some time are deleted without asking. **Scratch** (or `X`) searches your files
in `$SCRATCH` in the background (one `find` on the cluster; it can be
aborted) and lists the 2000 with the oldest access (or modification) time;
those older than the given number of days (30) are red, those in its last fifth
yellow. An exclude regex leaves matching paths out of the list and of every
touch. **Touch selected**,
**Touch all listed** and **Touch all my files there** set their access time
to now (after a confirmation). Mind that many sites do not allow keeping files
in scratch this way; move what you need to keep elsewhere.

## Browser access

### Local browser or SSH tunnel

Install `textual-serve` in the same Python environment and start the local
browser server:

```bash
python -m pip install textual-serve
python3 slurp --serve
```

Open <http://localhost:8000>. The server binds to `127.0.0.1` by default;
`--host` and `--port` change the bind address and port. To access it through
two SSH proxies, run the server on the Slurm host and forward the port:

```bash
ssh -J user@jump1,user@jump2 -N \
  -L 8000:127.0.0.1:8000 user@slurm-login
```

Then browse to <http://localhost:8000> on your computer. Substitute your SSH
hostnames and usernames. If local port 8000 is occupied, use a different local
port, e.g. `-L 18000:127.0.0.1:8000`, and open `http://localhost:18000`.

### Random public link with Textual Web

If the Slurm host can make outbound internet connections, Textual Web can
relay a Textual app to a random public URL without an inbound port or SSH
forward. Install `textual-web`, then create a configuration file such as
`serve.toml`:

```toml
[app.Slurm]
command = "cd /path/to/scripts && /path/to/venv/bin/python slurp"
```

Run it from the project directory:

```bash
textual-web --config serve.toml
```

Textual Web prints a random URL that can be opened from a browser on another
network. Its documentation currently describes the project as beta. Anyone
with the URL can access the running app, so treat it as a secret bearer link:
the app can run Slurm actions and launch shells as the account running it.
Use this only when that access is appropriate. A permanent account URL is
available in Textual Web, but it is an address and should not be treated as a
secret. See the [Textual Web project](https://github.com/Textualize/textual-web)
for its current setup and access behavior.

Install Textual Web separately with `python -m pip install textual-web`.
Terminal suspension is unavailable in browser mode, so opening a file in
`less` is a terminal-only action.

## Keyboard and mouse

The app supports clicking and scrolling as well as keyboard navigation. Use
`Tab` to move between controls, `Enter` to open a selection, and `Esc` or the
top-left `←` button to go back.

| Screen | Main controls |
|---|---|
| Job list | `/` search jobs; `Enter` open; `Space` open/close an array row; `g` group arrays on/off; `!` only jobs with problems; `n` new job script; `W` remote web access; `r` reload; `a` abort loading; `t` shell on the cluster (in slurp or your own terminal); `F` login directory as a drive; `d` login directory; `i` interactive job; `C` cluster info; `Q` quota; `X` old scratch files; `q` quit |
| Job details | `Cancel job` / `Restart job` / `Run array` / `Array`; `r` reload; `s` shell on the node (in slurp or your own terminal); Checks: `Check`, `Placement`, `Fixed copy…`, `What changed`, `Rule catalogue` |
| Script view | `r` check (all checks, the environment too); `x` explain the `#SBATCH` lines; `e` edit in place (`Ctrl+S` save, `Esc` stop editing); buttons `Test`, `Fixed copy…`, `Submit…` |
| Everywhere | `?` (or the **?** button) help of the window shown; `F1` guides; `F2` rule catalogue |
| Text file | `/` search; `n` / `N` next/previous; `f` follow; `A` the part before; `l` open in `less`; `r` reload |
| Directory | `/` filter; `f` follow; `.` toggle hidden files; `Backspace` or `u` go to parent |
| HDF5 | `s` enter a dataset selection; `[` / `]` change row page (200 rows); `{` / `}` change column page; `r` reload |
| Markdown | `r` reload; use the viewer's table of contents and link navigation |
| Shell (subtab) | mouse wheel or `Shift+PgUp`/`Shift+PgDn` scroll back; drag to select, right-click on it to copy; `Ctrl-]` back to the job list; `Ctrl-D` exits the shell, Enter starts it again; `✕` of the subtab closes it; **ⓘ Help** copy, paste, keys, closing |
| Subtabs | `Ctrl-W` close the subtab shown (outside a shell) |
| Login pane | `Ctrl-]` switch between the window and the login; `Ctrl-X` gives up the login; `ⓘ` copy, paste, keys, closing |

When using tmux, enable mouse support with `set -g mouse on` if you want to
click and scroll inside the app.
