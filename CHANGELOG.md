# Changelog

slurp and its Windows helper share one version number. Beta: settings, link
formats and the helper may still change between versions (a link from one
version may need the helper of the same version).

## Unreleased

- **Open a directory…** (the menu, `d`; was Login directory) asks which one:
  your home (the default), scratch, project, … (`$SCRATCH`, `$PROJECT`,
  `$STORE`, `$WORK`, those set on the cluster) or a path. Each time in a new
  subtab. The directory view shows its path as an input (edit it, Enter goes
  there) with buttons for home, scratch, project, … next to it.

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
