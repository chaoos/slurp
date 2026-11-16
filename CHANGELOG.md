# Changelog

slurp and its Windows helper share one version number. Beta: settings, link
formats and the helper may still change between versions (a link from one
version may need the helper of the same version).

## Unreleased

- Without Slurm on the machine of a tab (no `squeue`), the job list says so
  in its middle, with what to do (a tab for a cluster with `+`, or
  `slurp --ssh`), instead of an error notification for every Slurm command at
  each load. `r` looks again.
- Switching to a tab without an SSH session no longer logs in right away:
  its job list shows a **Connect** button in the middle (a bar above the jobs
  from last time, if it has some). slurp still logs in at the start and for a
  tab you add. A click on the active tab logs in, too.
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
