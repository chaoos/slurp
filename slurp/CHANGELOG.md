# Changelog

slurp and its Windows helper share one version number. Beta: settings, link
formats and the helper may still change between versions (a link from one
version may need the helper of the same version).

## Unreleased

- slurp has its own repository (https://github.com/chaoos/slurp), with its
  own `requirements.txt`; it no longer needs `slurm.py` and the other modules
  of the scripts repository it started in.
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
