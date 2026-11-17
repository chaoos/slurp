# Changelog

slurp and its Windows helper share one version number. Beta: settings, link
formats and the helper may still change between versions (a link from one
version may need the helper of the same version).

## Unreleased

- **Options** (`F3`) → *Progress of jobs*: how much of the end of a file
  is read for the progress (KiB, of it the last lines), and over how many
  progress values the current pace goes; high values are said to slow the
  job list and the job window down.
- In the notes of categories and jobs, web addresses and the paths that
  exist on the cluster are links: a click opens the page, or the file or
  directory in slurp.
- The progress file window's filter takes patterns (`run_123*.log`,
  `step_??.out`); directories stay listed, and the filter stays when
  going into one.
- The cancel dialog's job to name (the duplicate, the job it depends on,
  the one that replaces it) is a searchable list of all jobs of the job
  list: type an id, a name, a state, a directory; the jobs that look the
  same come first (★).
- The text file view has no `l` (open in `less`) any more: it pages like
  `less` itself.
- The array window opens the job window of a task in a subtab of its own
  (a click on an id in By state, or on a task in Tasks).
- Fixed: the **Array** line of a task's job window did nothing when the
  task was opened from the array window (it showed the subtab already
  shown); it opens the array window in a new subtab now, and so does the
  **Array** button.
- **Progress file…** opens a window with the directory listing instead
  of the directory browser: type a path, filter, and for the file under
  the cursor its end and whether it has progress in it.
- Cancelling a job or an array asks why: a list of the usual reasons
  (submitted twice, with the jobs that look the same to name the one that
  stays; the program hangs; wrong resources or account; a job it depends
  on failed, with `--dependency` and the guide; a bug, wrong input,
  replaced, too slow, out of memory or quota, waits too long, the
  cluster, not needed, a test) or your own words. Kept with the job on
  the cluster; shown in the job and array windows and in the categories
  window.
- Jobs have notes, in Markdown, like categories: **Notes…** (`o`) in the
  job window; shown there rendered, kept on the cluster.
- Large text files (logs of millions of lines) work like `less`, without
  being read as a whole: scrolling past the part shown reads on (a window
  of 4 MB at most), `g` / `G` jump to the start and end, `:` to a line or
  a percentage, `/` searches the whole file (`grep` on the cluster);
  `Space` / `b`, `j` / `k`; the line numbers are the file's. Before, only
  1 MB (about 10000 lines) was shown, more with `A` only upwards.
- The categories window opens a job (or an array) in a subtab of its own.
- Submit… and Run array say which ids an array range gives, the first
  and the last, and warn when a step misses the end (`1-100:2`: the last
  id is 99, not 100).
- Checks: paths of a script must not only exist but be readable. P02 says
  when a sourced file, an input or a Python script cannot be read (no r)
  or a `cd`/`--chdir` directory cannot be entered (no x); P03 a script run
  by path that is executable but not readable; P07 an environment you may
  not read; P01 a log directory you cannot enter; S02 an interpreter that
  is not executable. Each says how to get the permission.
- The job window of an array task has an **Array** line: a click opens
  the array window, in a subtab of its own.
- The job window: a click on the job's category opens the categories
  window at that category, in a subtab of its own.
- Categories have notes, in Markdown: **Notes…** (`o`) in the categories
  window; shown there rendered, under the numbers of the category.
- The job list, the job window and the array window refresh by themselves
  every 60 s (`"refresh_seconds"` in the config; counted from the last
  load, `r` too); **⟳ 0:42** in the title bar counts down, a click there
  switches it off (**⟳ off**) and on again (kept).
- **Check…** of an SSH connection (the add dialog, SSH settings…, the
  connection window) opens a window with a terminal and goes as the tab's
  login: what needs no login, the command before the login, the login
  itself, where you answer what it asks (passphrase, password, code, a new
  host key), then the verdict and the findings with ssh's log. Fixed: with
  much output the dialog's buttons were out of reach; the dialog keeps
  them in view.
- New rule S10: Slurm variables the job does not get. Slurm sets some only
  when the job asks for something (`$SLURM_ARRAY_TASK_ID` in arrays,
  `$SLURM_CPUS_PER_TASK` with `--cpus-per-task`, `$SLURM_MEM_PER_NODE` with
  `--mem`, …): used without it they are empty (a warning for a job, a hint
  for a script, which may still get it on the command line); a `SLURM_`
  name close to one Slurm sets is a typo. A default (`${…:-1}`), a test
  (`-n`) or the script setting it is fine. S06 knows the variables only the
  job's environment has (`SLURMD_NODENAME`, `ENVIRONMENT`, …).
- New rule Q07: the job's logs (`--output`, `--error`, or the default
  `slurm-%j.out` in the working directory) are in `$HOME`, `$PROJECT` or
  `$STORE`, not on `$SCRATCH`; the fix names the scratch path to use (a
  warning from 128 tasks, else a hint).
- New rule P09: the program that srun (or mpirun) starts on many tasks, or
  an interpreter's script, is in `$HOME`, `$PROJECT` or `$STORE`, not on
  `$SCRATCH`: at the start all ranks read it at once (a warning from 128
  tasks, a hint from 16). Fix: copy it to scratch, or `srun --bcast`.
- A newer slurp: at the start slurp asks PyPI (at most once a day, in the
  background, offline nothing), says so and shows **⬆ slurp <version>** in
  the title bar; it opens what changed, how slurp is installed here (pipx,
  uv tool, pip, a git checkout) and the command that updates it (Copy),
  then to start slurp again. Skip this version; `"update_check": false`.
- **Run before login** (a tab's SSH settings, also for a `~/.ssh/config`
  destination): a command on this machine before every login, e.g. one
  that signs a key (`cscs-keygen`, `step ssh login`); it runs in the login
  pane, where the user answers it (Ctrl-C ends only it), then the SSH login
  follows as usual.
- Runtime diagnostics open in a subtab of their own (one per job and node;
  asked again, it measures again), not on top of the job window.
- The job window's progress has a **?** next to Progress file…: what the
  progress is and why it helps, what it needs (a file with n/N or M% lines,
  best the output, written unbuffered), how much of the file is read (its
  last 64 KiB, 600 lines), and how the current and the same pace are
  worked out.
- The overview of a cluster's tab has **SSH settings…** (below Connect and
  Disconnect): its destination or full SSH settings, filled in, the blocks
  of `~/.ssh/config` that apply and what `ssh -G` makes of it all, Check;
  Save changes the tab, the next login uses it.
- Links (web, drive, shell) show when they started and how long they are
  still valid: **Started** and **Valid** in the connection window's links
  (sessions: when they started) and in the link window; until when, or
  until slurp quits, the relay's end if sooner (pinggy's hour), yellow in
  the last five minutes.
- A web link that ends: the title bar counts down to it (⏱, left of **?**;
  a click opens the link), when it takes no more logins or its relay ends
  the tunnel (pinggy's free hour). Five minutes before, a warning: **Extend
  by 1 h** for its time limit; for the relay's hour what happens and how to
  avoid it.
- **+** (a new tab): the SSH destination, a Host of `~/.ssh/config`, stays
  the main way (now suggested as you type); as a fallback the **Full SSH
  settings**: host, user, port, jump hosts, identity files, more `ssh`
  options, the tab's name, kept with the tab and used by its login.
  **Check** (also in the connection window) tries the connection without
  asking anything and writing nothing: what ssh makes of it, the keys,
  name, port, server, host key and a login; precise findings (works /
  the login asks / what is wrong) with `ssh -v`'s log. A tab can be added
  checked or not.
- **Query runtime diagnostics…** replaces Query current memory in the job
  window: on which node (all, or one: radio buttons), then a window with
  what the job does there, measured over one second: memory (its cgroup and
  limit, out-of-memory kills, PSS/RSS, the node as `free`, swap), CPU (the
  job's CPUs busy, the node's use and load), GPUs (memory, busy, bandwidth,
  temperature, power, clock), I/O, local disks and the busiest processes;
  what looks wrong in red or yellow; **Again** (`r`).
- The job window's progress shows only the estimate on the current pace;
  the one on the average pace since the start only while there are too
  few updates for the current one.
- The job window's **Shell…** is in its button bar (with Cancel job,
  Restart job, …), for a running job; it asks on which of the job's nodes
  (radio buttons) and where the shell opens. It was at the end of the
  window, under Run on the nodes.
- Where a text file opens depends on where from: a job's output, error and
  progress file (the job window, the job list's Err) at its end, followed
  while the job runs (an ended job's at its end too, not followed); the
  directory browser, a path in a text and the files of the script at the
  top, not followed.
- Fixed: the **Shell** button of a running job's window opened the
  categorize dialog (its handler was on the wrong method since categories
  came); `s` worked.
- The job window keeps the progress file of a job that ended: the last
  `n/N` or `M%` line of its file (where it got to: a failed job at
  `873/1000`) and how it ended, instead of an empty bar of its end state;
  **Progress file…** picks another file for it too.
- Query current memory shows the right numbers. Memory is in binary units
  (MiB, GiB) as `free -h` and `nvidia-smi` show it (it was decimal GB: an
  80 GB GPU of 81559 MiB showed as 85.5 GB). CPU: the job's cgroup (what
  Slurm counts against `--mem`, with its page cache), else the PSS of its
  processes (RSS counted shared memory in every process), with PSS, RSS and
  each node as `free` shows it. GPU: the job's processes, or the used
  memory of its GPUs where the driver reports none per process (`[N/A]`; it
  counted 0), and every GPU's used/total as in `nvidia-smi`. The memory
  watch's file has `cpu_job_bytes` (was `cpu_rss_bytes`).
- A drive mapped on Windows (the slurp helper) is named `user@cluster:/path`
  in Explorer, not `slurp (\\sshfs.r\slurp@localhost!8000\users)`: the link
  says whose files where, the helper sets the name after `net use` and
  removes it with the drive. Set the helper up again for it.
- Map a directory… on this machine: **Directory** chooses the directory of
  the cluster from a list (home, scratch, project, …, those set on the
  cluster) or as a path typed in; before, it was always the login directory.
  It is remembered per tab and mounted again after a reconnect; one that
  does not exist is refused.
- The menu's **Login directory as a drive** is now **Map a directory…**
  (`F`), next to **Open a directory…** (`d`).
- The job window has a **Reload** button (as `r`): the job, its files and
  progress from the cluster again; it says *Reloading …* meanwhile.
- Progress also from `M%` lines (0 to 100, integer or not: `45%`,
  `12.5 %`), where a line has no `n/N`; shown as a percentage. A line with
  both (as tqdm writes) gives its `n/N`.
- Submit… in the script view shows the `sbatch` command to edit, with an
  **Array** field for an array job (`1-100`, `0-9:2`, `1,5,7`, `%50`): it
  writes the command's `--array` (an edited command keeps its other edits),
  says how many tasks, and warns when the script never uses
  `$SLURM_ARRAY_TASK_ID`.
- A text file opens at its top; a large one shows its first 1 MB (`G` or
  `f` read its end). A log that is followed (a running job's) still opens
  at its end.
- The directory view searches names (`/`): a part of them or a pattern
  (`*.out`), here as you type, or with **Subdirectories** (`Ctrl+R`) in all
  directories below, in the background (on a cluster a round trip per batch
  of directories): the results come in with their paths while the window
  stays usable; `Esc` stops the search, then shows the directory again.
- The rule catalogue has a search (`/`, at the top): only the rules with all
  its words (id, title, texts, example), as you type; `Esc` shows all again.
- Rules by software stack: the module rules (E01, E04) apply on clusters
  with environment modules, the uenv rules on clusters with uenv, found with
  the cluster's facts (or a rule pack's `stacks`). A module rule applies in
  part to a uenv's modules view (`--view=modules`): E01 asks for versions,
  not for `module purge`. The rule catalogue says where each rule applies
  and greys out the ones that do not apply on the tab's cluster.
- New uenv rules: E06 (a uenv without version or tag; proposes the one
  pulled), E07 (a uenv without a view), E08 (`module load` on a uenv cluster
  without the modules view).
- E04 (modules that cannot be loaded) in a job with a uenv: the module
  loads are replayed in the uenv, as the libraries are; when the uenv cannot
  be used, P05 says the module loads are not checked either. Tested with a
  module only the uenv's modules view has.
- New rule E05 (error): the script's uenv (`--uenv`) is not pulled. A job
  can only use a uenv in your repository; `uenv image ls`, run with the
  environment check, lists them. The finding has the `uenv image pull`
  command. Labels match as uenv does: name, version, tag, `@system`,
  `%arch` or an image id; several uenvs and mount points too.
- The environment check also replays `conda activate`, `mamba activate`,
  `micromamba activate`, `spack load` and `spack env activate`: a program of
  such an environment was checked without it (P05: libraries missing).
- The environment check (P05: shared libraries with `ldd`; P06, B02, E04)
  runs in the job's uenv: with `--uenv` and `--view` the replay of the
  script's setup runs in `uenv run` with them, as the job does. Before, it
  ran without, and every library of the uenv was reported missing. Without a
  `uenv` command on the login node, or a uenv that does not start, P05 says
  the libraries are not checked. In a container (`--environment`,
  `--container-image`) the programs are not looked at (and E04 does not
  report its modules).
- slurp in your browser: `Ctrl+C` (`Cmd+C`) copies the text you marked. The
  page now writes it to the clipboard itself, in the key's event; before,
  the copy went through OSC 52, which browsers refused outside HTTPS and
  often outside a key press.
- Closing the tab or subtab shown goes back to the one shown before it, not
  to its neighbour (a tab) or the job list (a subtab).
- **Open a directory…** (the menu, `d`; was Login directory) asks which one:
  your home (the default), scratch, project, … (`$SCRATCH`, `$PROJECT`,
  `$STORE`, `$WORK`, those set on the cluster) or a path. Each time in a new
  subtab. The directory view shows its path as an input (edit it, Enter goes
  there) with buttons for home, scratch, project, … next to it.
- The directory view shows the permissions of each entry (as `ls -l`, a `+`
  with extended ACL entries), owner and group, the extended ACL entries
  (`getfacl`), and what you cannot do with it: **no read** (red) or **no
  write** (yellow), as the cluster answers for you (groups and ACLs count).
  The same for the directory itself above the list. On a cluster still in
  one round trip.
- The script view has **Reload** (`Ctrl+R`): it reads the script from the
  file again and checks it, also while you edit (the file goes into the
  editor, which stays editable; unsaved changes are asked about first). It
  replaces Revert, which was only there while editing and ended the editing.

## 0.8.1 (beta), 2026-11-17

- On PyPI as `slurp-ui`: `pip install "slurp-ui[all]"` (or `pipx install`); the
  command stays `slurp`. A tag `v<version>` publishes a release.
- Files and directories always open in a subtab of their own, wherever they
  are opened (the files of a job, a path in a file, a file in a directory);
  where they were opened stays as it was. Opening one again shows its
  subtab. A folder entered in a directory view stays in that subtab (`←`
  goes back).
- A plain button (Close, Cancel, a tab not shown, …) has a box of its own, a
  shade lighter than the window, like the coloured ones: before, only its
  word showed, and the coloured button next to it looked much bigger.
- Icons in front of tabs, buttons, menu items, the states of jobs and the
  files of a folder: the Material Design icons of Nerd Fonts, every one a
  cell wide. A terminal draws them from slurp's icon font (8 KB): install it
  once on the machine of the terminal you look through with
  `slurp --install-font` (a Nerd Font works too); the browser view loads it
  itself. **Options** → **Icons**: icons (the
  default), Unicode symbols (no font, but uneven in most fonts), or none.
- The header (slurp and its version) is the top line, above the tabs, and has
  **☰ Menu** at its left, in every window (it was in the job list's toolbar
  and the overview).
- **Quota** and **Health** are windows again, not subtabs: they open over the
  job list or the overview, `Esc` or **Close** closes them.
- Quota, Health, Categories and the window of a job array no longer cover the
  whole screen while they load (Textual's loading indicator took every click
  until they were done).
- The menu (`F10`) has **Job list** (`j`), so it opens from the overview's
  menu too.
- slurp no longer logs in at the start: a cluster tab shows **Connect** in its
  overview. Only a tab added with `+` logs in right away. Connected, the
  overview has **Disconnect**.
- The job list shows only **Done: current pace**; the *same pace* column is
  gone (the job window still shows both estimates).
- The `--ssh` option is gone: `+` adds a tab for a cluster, and slurp starts
  with the tabs of the last run.
- Without Slurm on the machine of a tab (no `squeue`), the job list says so
  in its middle, with what to do (a tab for a cluster with `+`), instead of an error notification for every Slurm command at
  each load. `r` looks again.
- The documentation is a website now, <https://chaoos.github.io/slurp/>
  (Material for MkDocs): the manual is split into pages (the overview, the
  job list, the job window, …, shells and drives, the helper, web access,
  development), with search and the changelog. GitHub Actions builds it for
  every push and pull request (`mkdocs build --strict`: a broken link fails)
  and publishes it from main. The README is a short start page; `.[docs]`
  installs MkDocs.
- A tab starts with no subtab open, and then shows its **overview**: in its
  middle the cluster in a line (site, Slurm version, nodes and partitions),
  its health (healthy, degraded or unhealthy, and why), the SSH session, and
  **Jobs** (with how many run and wait; *No Slurm available* where there is
  none), **Quota** and **Health**; not connected, **Connect** with what
  happened to the connection. The job list is a subtab like the others, with
  a `✕` (and `Ctrl-W`); when the last subtab closes, the overview shows again.
  Cluster info is called **Health** (`C`). *At the start a tab shows* in
  Options keeps the job list open from the start (`"start_page": "jobs"`).
- The buttons of the job list's toolbar are a menu now: **☰ Menu** (`F10`)
  has cluster overview, quota, scratch, shells, the login directory and drive,
  interactive job, new job script, categories, web access, guides, rule
  catalogue, options, about and quit, each with its key. The toolbar keeps
  Reload and the choices of the list. Textual's command palette (and its ⭘ at
  the top left) is gone; themes are chosen in Options (`F3`).
- Web links (this slurp in a browser) belong to no tab: the connection
  window of every tab lists them, not only that of the tab they were made in.
- The slurp helper for Linux: **Open on my computer** in a link window, the
  password, and a drive is mounted (sshfs, `~/slurp/<link>/<folder>`) and
  opened in the file manager, a web view opens in the browser, or a terminal
  window opens with the shell, as the Windows helper does. One Python script
  (only python3 needed), set up once from **Set up the helper…** (tab Linux):
  a setup to download or one line to paste; dialogs with zenity or kdialog,
  desktop notifications, `slurp-helper list` / `stop`.
- Helper links of an own relay given as `user@host` were refused by the
  Windows helper ("no valid 'rh'"): the link now carries only the host there.
- Switching to a tab without an SSH session no longer logs in right away:
  its job list shows a **Connect** button in the middle (a bar above the jobs
  from last time, if it has some). A click on the active tab logs in, too.
- Back on a tab with jobs after one without Slurm, the job list has the focus
  again (it was drawn in the unfocused colours).
- In a browser (`slurp --serve`, or the browser view of the session holder),
  a web page (a site's status page, a link in the help or the checks) opens in
  a new tab, so the page with slurp stays. The links in the help and the checks
  open in that browser now, not on the machine slurp runs on.
- All buttons are compact (one line, as wide as their label): the toolbar of
  the job list, the job window, the dialogs and the other windows had a mix of
  three-line and one-line ones. The toolbar of the job list fits in one row
  with its choices.
- The tagline is "All of slurm in one slurp" (was "Slurm at a glance").
- slurp could end right at its start with an error (no attribute "main")
  when its 0.25 s timer came before the job list was shown.
- Save to a file (the copy window) uses the cache directory as it is when
  saving ($XDG_CACHE_HOME), not as it was when slurp started.
- An array task's progress file: when the files of the other tasks are
  guessed from it, a number in the directory that happens to equal the task's
  index (`run1/`, `pytest-1/`) is no longer taken for it, when the file name
  has the index or the job id.
- slurp has its own repository (https://github.com/chaoos/slurp); it no
  longer needs `slurm.py` and the other modules of the scripts repository it
  started in.
- slurp is a Python package to install (`pyproject.toml`): `pip install
  ".[all]"` gives the `slurp` command (and `python -m slurp`); `pipx install`
  works without a clone. The optional features are extras: `links`, `hdf5`,
  `web`, `all`. `requirements.txt` is gone, and `python3 slurp` from the
  checkout no longer works without installing. slurp starts itself again (the
  session holder, the app under it, `--serve`) as exactly the same copy,
  installed or not.
- The changelog is at the top of the repository (`CHANGELOG.md`), the manual
  is `docs/manual.md` and its images are in `docs/images/` (they were in the
  package directory); `python tools/make_logo_files.py` writes the logo files
  (was `python -m slurp.logo`). The About window links both.
- The code is in `src/slurp/`, in subpackages by what it does: `cluster/`
  (Slurm, the jobs and their files), `checks/` (the checks of batch scripts;
  the built-in rule packs are `checks/packs/`), `links/` (shells, drives and
  web views on your own machine, the session holder) and `ui/` (the app and
  its windows). Your own rule packs stay in `~/.config/slurp/rules/`.
- The tests are in the repository (`tests/`, run with `pytest`), with a
  GitHub Actions workflow: the unit tests, and the lab tests that drive slurp
  against a fake Slurm cluster behind a local sshd.
- The job list is kept on this machine after each load: a start shows it at
  once, and when the cluster cannot be reached it can still be browsed and
  categorized (the changes reach the cluster with the next load); the status
  line says it may be outdated.

- When the login to a cluster fails (or the connection is lost), slurp says to
  check its site's status page, as a link: for CSCS https://status.cscs.ch,
  for JSC https://status.jsc.fz-juelich.de/ (the rule pack's new key
  "status"); in the status at the bottom right and in the connection window.
- Rule packs for JSC's JURECA, JUWELS, JUSUF and JEDI: their documentation
  (for the guides) and the status page.

- Categories of jobs, like folders (`project X/production runs`): mark jobs
  in the job list (`m`), put them in a category (`c`, a new one is made); a
  Category column, `cat:` in the filter; the Categories window (`K`): the
  tree, each one's numbers (jobs by state, node, CPU and GPU hours, when,
  where) and its jobs, also old ones; new, rename/move, delete; "not
  categorized" (the jobs of the job list in none, `cat:-` in the filter). Kept
  on the cluster, per Slurm cluster.

- Web links through Cloudflare: a quick tunnel (cloudflared, no account; slurp
  asks, then downloads it once, and the link window shows downloading and
  installing it) gives the web view a public https address that opens in any
  browser, with nothing to install on your side and no password.
- The web view needs the token in the URL every time; no cookie from an
  earlier visit opens it (each page load gets a one-use ticket).
- In the connection window, the web session of the browser you are on is
  marked "this view"; disconnecting or revoking it warns first.
- The links list has a health column for every link and session (as for the
  SSH login: a heartbeat, reconnects); a drive lists the folders its clients
  mounted, with their reconnects.
- **? slurp** at the top right: slurp in general, its version, the machine it
  runs on (Copy details for a bug report); also "About slurp" in the palette.
- The Windows helper has one window: the connections, and below them the one
  selected with its settings and steps (no window per connection any more); a
  right click on a row: open, map another folder, unmap one, open another
  shell, reconnect, disconnect, logs, copy the details, remove. The
  "Helper ..." button is gone (the tray's menu has the helper's settings).

- In the browser, a download (Download the setup of the Windows helper)
  opens in a new tab: slurp's page stays; if the browser blocks the tab, a
  banner has the download to click.
- Closed links say which way they went: "closed, was via ssh-j.com".

## 0.8.0 (beta), 2026-11-15

The first version with a number. What slurp has at this point:

- Jobs: the list (queued, running, finished) with filters and state, the job
  window (details, steps, logs that follow as they grow, progress with
  estimates), cancel and restart (as submitted, or edited), arrays run again
  with another range, interactive jobs.
- Files and nodes: the directory browser, Markdown, HDF5 and text views,
  shells and commands in slurp on login nodes and on the nodes of running
  jobs.
- Clusters over SSH: one tab per cluster, and the local machine; storage
  (quota, old files in scratch).
- Checks: batch scripts and jobs against the rule catalogue (`slurp check`
  without the UI).
- One slurp, several views: the session holder keeps slurp running, terminals
  and a browser attach to it; remote web access through a link.
- Shells and drives from your own terminal: one-time links (a password or a
  key, uses, expiry) through a relay of your choice: direct (no relay),
  ssh-j.com, serveo.net, pinggy.io, sshreach.me or your own SSH server.
- The Windows helper (tray): opens slurp links with one click and the
  password; shells (the terminal opens by itself), drives (SSHFS-Win, several
  folders: $HOME, $SCRATCH, $PROJECT or typed in), the web view; its window
  shows the connections, with reconnect and disconnect.
