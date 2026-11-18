# Transfers

slurp copies data from one tab to another: from a cluster to another cluster, from a cluster to this machine or back.
It needs no new login for it: it uses the SSH sessions of both tabs. The two clusters need not reach each other, as
long as slurp reaches both. Every transfer is rsync, so it is idempotent: running it again copies only what is
missing or changed, a half-copied file is picked up where it stopped, and nothing is ever deleted on the
destination.

## Setting one up

**Menu → Transfer data…** (`T`), or **New transfer** in the list of transfers, opens a wizard of five steps. **Next**
waits until a step is complete (its tooltip says what is missing); **Back** goes back. The steps at the top can be
clicked: any step whose steps before it are complete. **Edit…** of a transfer opens the same wizard with every step a
click away and two buttons, **Cancel** and **Save**: Save keeps the changes once every step is complete (its tooltip
says what is missing; after another source or destination the route is checked again), and the transfer's schedule
goes on as set, without a run now.

1. **Source**: a tab and one or more files or folders there: **Browse…** (`Space` marks several, **Choose the
   marked**; **Choose this folder** takes the folder shown) or a path typed in. Each is copied into the destination
   folder under its own name. Both tabs must be connected.
2. **Destination**: a tab and a folder there (created if it is not there), with buttons for its home, scratch,
   project, … The wizard shows what goes where.
3. **Route**: slurp checks both sides and changes nothing (except a marker file it removes at once), and you choose
   the route from what it found:
    - rsync on both sides, and its version;
    - the sources (there, readable) and the destination (a folder, writable, or the folder it is created in);
    - whether both see the same filesystem (a marker written on the destination is there on the source too);
    - what remains to copy: an rsync dry run, so after an earlier run only the rest;
    - the room on the destination: its free space, the quota of its filesystem (space and files), a scratch
      filesystem that is purged (from the [rule pack](checks.md) of the site);
    - many small files: bundling them first (tar) is suggested;
    - the routes (below), each a choice that shows its check as it comes: ✓ passed, with its speed (measured with a
      sample of the data, a few seconds) and an estimate; ! only with the temporary key; ✗ failed, and why (it
      cannot be chosen). When the check is done the recommended route is chosen; **Next** comes as soon as the
      chosen route passed (the other routes may still be checked).

    For a large transfer (about a terabyte, or hours) slurp suggests Globus or a similar service, where both sites
    offer it: parallel streams, retries, and it runs without slurp. A rule pack can name a site's Globus collection
    (`globus = "…"`); with both sites' it is named.
    Below the routes: where it runs, and the options. A picture shows how the data flows, here and in the summary and
    the transfer's subtab; in the wizard with the bandwidth the check measured under its arrows and the estimate below:

    ```
     ┌────────┐       relay       ┌─────────┐        relay      ┌────────┐
     │ ● alps │   ────────────▶   │ ⇄ slurp │    ────────────▶  │ ● jedi │
     └────────┘    250.0 MiB/s    └─────────┘     250.0 MiB/s   └────────┘
    rsync (sends)               passes the bytes               rsync server
                                                                (receives)
    ⏱ about 2 min – 4 min for 1.2 GiB in 37 files
    ```

    A direct route says **push** (rsync on the source logs in to the destination) or **pull** (the other way); under
    each box what runs there (on the login node, in a transfer job). The options:

    - **checksums**: rsync compares the contents, not size and time. It reads every file on both sides at every
      run, also when nothing changed, and shows no progress meanwhile: the estimate counts that time, and a run says
      it is comparing. Size and time find almost every change; checksums are for when they cannot be trusted.
    - compression (the relay only: it helps on slow links), a limit in MB/s.
4. **When**: now, once at a time (`14:00`, `2026-11-20 14:00`, `+2h`), every so long (`6h`, `1d`, `30m`), daily at a
   time, after a job ends, or never by itself: only when you click **Run now** (`s`). For a schedule: whether slurp logs in to the tabs by itself then (a login that needs
   you waits, and says so), and whether a missed run runs once as soon as it can or is skipped.
5. **Summary**: a name, everything chosen, the estimate, what happens if slurp closes. **Go** starts it (or schedules
   it); **Save without starting** keeps it for later. With *never by itself* the button reads **Add**: the transfer
   is added and runs each time you click **Run now**.

## Routes

- **Same filesystem**: both tabs see the destination's filesystem (two clusters of one site often share theirs).
  rsync runs on the source, from one folder to the other, without the network. Recommended whenever it works.
- **Direct**: one cluster logs in to the other by itself (without a password or a question) and rsync copies straight
  there: the source pushes, or the destination pulls. The host key is checked as this machine knows it. What login
  scripts print on the other side does not disturb it.
- **Relay through slurp**: rsync runs on the source, its server on the destination (each over its tab's SSH session),
  and slurp passes the bytes between them. It always works when both tabs are connected and have rsync, but the data
  goes through the machine slurp runs on: its connections to both clusters set the speed (from a laptop at home that
  may be slow). With one end this machine, the relay is a direct copy over slurp's SSH session. slurp checks what the
  source's rsync asks for: an rsync server that receives into the destination, nothing else.

The check logs in from the login node of the session. A run logs in once more before rsync starts, with the same
ssh, where it runs: on the login node it starts on, or on a node of the transfer partition for a transfer job. Sites
may accept a key only from some hosts (JSC, for example, `from=` in `authorized_keys`): refused there, the run ends at
once and says which host could not log in where, what ssh said, and where the check had logged in. A direct route as
a transfer job is therefore checked from a node of the partition too, in the wizard (`srun --immediate`, a minute at
most): refused there, the wizard does not go on; with no node free, it says it could not check there.

A direct route that cannot log in by itself, where the other cluster's SSH server answers, can use a **temporary
key** (opt-in, in step 4, with a warning): slurp makes a key on the cluster that runs rsync (in the run's folder there,
readable only by you) and adds a line for it to `~/.ssh/authorized_keys` on the other one. The key may only run an
rsync server (no shell, no forwarding), expires after some hours, and the line is removed when the run ends. Some
sites forbid this, or overwrite the file (keys managed in a portal, certificates only).

## Where it runs, and if slurp closes

- **In slurp** (the relay): slurp passes the bytes, so the run needs slurp. A closed terminal does not matter (slurp
  keeps running in its session holder); when slurp quits, the run stops and shows *paused (slurp ended)*, and
  **Continue** at the next start goes on where it stopped.
- **Detached on the cluster** (same filesystem, direct): it runs on the login node, away from slurp's session, and goes
  on when slurp ends; slurp shows it again at the next start. Some sites end long processes on login nodes (a rule
  pack can say so: `login_node_limits`).
- **As a transfer job** (same filesystem, direct; where the site has a transfer partition, e.g. `xfer` on CSCS Alps,
  `transfer_partition` in its rule pack): a Slurm job with the partition, time limit and account chosen (the account
  one of yours there, to pick; where you have accounts, one must be chosen). The wizard tests its batch script with
  `sbatch --test-only` as soon as it is chosen and when these change: it shows when and where the job would start, or
  Slurm's error, and then does not go on. It goes on
  without slurp. When its time limit ends, slurp can submit the next job (the default): the next run goes on where this
  one stopped (slurp must run then, or does it at its next start).

A run on a cluster keeps everything in its folder there, `~/.cache/slurp/transfers/<id>/` (`$XDG_CACHE_HOME`):
`progress.json` (its progress: bytes, percent, rate, time left, files, the file it copies, updated every moment), `log`
(rsync's errors and statistics), `result.json` (its exit code, when it ended), and a lock while it runs, so that a
second run of the same transfer does not start.

## The Transfers tab

While transfers exist (also finished ones), the tab **Transfers** is at the left of the tabs of the clusters, with
how many run. Its first subtab is the list: each transfer with its name, from where and to where (each tab with a dot
for its connection and its path; a long path is shortened in the middle, `/very/long/…/in/its/path`, and the full
paths are below the list for the transfer highlighted), its route and where it runs, its state, how far it is, its rate, the time left, and when it runs next (or
ran last); the line below says why it waits, how it failed, its schedule. `Enter` or a click opens a transfer in a
subtab of its own:

- its state at the top, in big: RUNNING (how far, the rate, the time left), PAUSED (mid-run, or its schedule),
  WAITING (for what), SCHEDULED (when), QUEUED (its job), STARTING, PAUSING, DONE, DONE WITH ERRORS, FAILED (why),
  NOT STARTED;
- whether both sides are in sync: **● in sync**, **● not in sync** (how much is left, or how many files the last run
  could not copy) or **○ in sync?** (not known yet), from the newer of the last check and the last run that ended
  well, and as of when (the source may have changed since);
- its state, the route (the tabs with their connections, and slurp in between for the relay), the sources and the
  destination: each a link that opens it in the tab of its cluster: a folder as a directory view, a file in its view
  (one that cannot be shown: its folder, searched for it), a pattern such as `/path/to/*.log` as its folder searched
  for it, a destination that is not there yet as the nearest folder that is;
- its progress: a bar, bytes of the total, the rate (for the relay also through slurp), the time left, files, the
  errors; the file it copies now; the rate over time;
- what happens if slurp closes; its schedule; the errors rsync reported for single files; **Runs**: how many times it
  ran, how the runs ended and the last one. A click on it lists the runs, the last first (number, start, end, how long,
  how it ended, how much and how many files, the rate, the route, the job or host, why it ended so); a click on a run
  shows its details: what it copied of how much, where it ran (its job, its folder on the cluster), its options,
  rsync's statistics, its errors and its events. `Esc` goes back. What the check found, with **Check again**: the
  check anew, as the transfer is set up (its paths and options): what remains, both sides, the routes and their speed
  (not while a run goes on); its log.

**Pause** (`p`) stops the run; rsync keeps the half-copied file in `.slurp-partial` on the destination and the next run
goes on from it. A transfer job is cancelled for it, and loses its place in the queue and the time it used: slurp asks
first. **Continue** (`c`) is there only for a paused transfer: paused mid-run, it starts a new run that goes on where
the paused one stopped (for a transfer job: submits a new job); paused between runs, it reads **Resume schedule** and
lets the schedule go on without a run now. A transfer that ended has no Continue: **Run now** runs it anew. **Run now** (`s`), **Edit…**
(`e`: the wizard with its settings; pause it first), **Its job** (the job window of its transfer job), **Connect …**
(the tab it waits for), **Delete** (`Del`: its settings and history, the files stay). The subtabs of the Transfers tab
are kept for the next start, like the others; `Ctrl-W` closes the one shown (the list stays). Keys that act on a
cluster (the job list, a shell, …) do nothing there; from the menu, the cluster tab shown before is shown first.

A transfer that needs a tab that is not connected waits for it (*waiting for jedi*); once both are connected it starts
by itself. When a tab's connection breaks during a relay run, the run waits and goes on when the tab is back. The
overview of each tab has a line about the transfers from or to it (a click shows the Transfers tab), notifications say
when a transfer is done, failed or needs a login, and closing a tab with a transfer under way says so first.

The transfers are kept in `$XDG_STATE_HOME/slurp/transfers.json` (`~/.local/state/slurp/`).
