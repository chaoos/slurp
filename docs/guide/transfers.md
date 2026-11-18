# Transfers

slurp copies data from one tab to another: from a cluster to another cluster, from a cluster to this machine or back.
It needs no new login for it: it uses the SSH sessions of both tabs. The two clusters need not reach each other, as
long as slurp reaches both. Every transfer is rsync, so it is idempotent: running it again copies only what is
missing or changed, a half-copied file is picked up where it stopped, and nothing is ever deleted on the
destination.

## Setting one up

**Menu → Transfer data…** (`T`), or **New transfer** in the list of transfers, opens a wizard of six steps. **Next**
waits until a step is complete (its tooltip says what is missing); **Back** goes back. The steps at the top can be
clicked: any step whose steps before it are complete. So **Edit…** of a transfer (all its steps are) goes straight to
the step to change; after another source or destination, the steps after the check wait for a new check.

1. **Source**: a tab and one or more files or folders there: **Browse…** (`Space` marks several, **Choose the
   marked**; **Choose this folder** takes the folder shown) or a path typed in. Each is copied into the destination
   folder under its own name. Both tabs must be connected.
2. **Destination**: a tab and a folder there (created if it is not there), with buttons for its home, scratch,
   project, … The wizard shows what goes where.
3. **Check**: slurp looks at both sides and changes nothing (except a marker file it removes at once):
    - rsync on both sides, and its version;
    - the sources (there, readable) and the destination (a folder, writable, or the folder it is created in);
    - whether both see the same filesystem (a marker written on the destination is there on the source too);
    - what remains to copy: an rsync dry run, so after an earlier run only the rest;
    - the room on the destination: its free space, the quota of its filesystem (space and files), a scratch
      filesystem that is purged (from the [rule pack](checks.md) of the site);
    - many small files: bundling them first (tar) is suggested;
    - the routes (below), each with its speed, measured with a sample of the data (a few seconds), and an estimate.

    For a large transfer (about a terabyte, or hours) slurp suggests Globus or a similar service, where both sites
    offer it: parallel streams, retries, and it runs without slurp. A rule pack can name a site's Globus collection
    (`globus = "…"`); with both sites' it is named.
4. **How**: the route (the recommended one is chosen), where it runs, and the options. A picture shows how the data
   flows, here and in the summary and the transfer's subtab:

    ```
     ┌────────┐    relay     ┌─────────┐     relay    ┌────────┐
     │ ● alps │   ───────▶   │ ⇄ slurp │    ───────▶  │ ● jedi │
     └────────┘              └─────────┘              └────────┘
    rsync (sends)          passes the bytes          rsync server
                                                      (receives)
    ```

    A direct route says **push** (rsync on the source logs in to the destination) or **pull** (the other way); under
    each box what runs there (on the login node, in a transfer job). The options: checksums (slower; finds
   changes that keep size and time), compression (the relay only: it helps on slow links), a limit in MB/s.
5. **When**: now, once at a time (`14:00`, `2026-11-20 14:00`, `+2h`), every so long (`6h`, `1d`, `30m`), daily at a
   time, after a job ends, or never by itself: only when you click **Run now** (`s`). For a schedule: whether slurp logs in to the tabs by itself then (a login that needs
   you waits, and says so), and whether a missed run runs once as soon as it can or is skipped.
6. **Summary**: a name, everything chosen, the estimate, what happens if slurp closes. **Go** starts it (or schedules
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
  `transfer_partition` in its rule pack): a Slurm job with the partition, time limit and account chosen. It goes on
  without slurp. When its time limit ends, slurp can submit the next job (the default): the next run goes on where this
  one stopped (slurp must run then, or does it at its next start).

A run on a cluster keeps everything in its folder there, `~/.cache/slurp/transfers/<id>/` (`$XDG_CACHE_HOME`):
`progress.json` (its progress: bytes, percent, rate, time left, files, the file it copies, updated every moment), `log`
(rsync's errors and statistics), `result.json` (its exit code, when it ended), and a lock while it runs, so that a
second run of the same transfer does not start.

## The Transfers tab

While transfers exist (also finished ones), the tab **Transfers** is at the left of the tabs of the clusters, with
how many run. Its first subtab is the list: each transfer with its name, from where to where (a dot for each tab's
connection), its route and where it runs, its state, how far it is, its rate, the time left, and when it runs next (or
ran last); the line below says why it waits, how it failed, its schedule. `Enter` or a click opens a transfer in a
subtab of its own:

- its state, the route (the tabs with their connections, and slurp in between for the relay), the sources and the
  destination;
- its progress: a bar, bytes of the total, the rate (for the relay also through slurp), the time left, files, the
  errors; the file it copies now; the rate over time;
- what happens if slurp closes; its schedule; the errors rsync reported for single files; its runs (when, how it
  ended, how much, the route, the job); what the check found; its log.

**Pause** (`p`) stops the run; rsync keeps the half-copied file in `.slurp-partial` on the destination and the next run
goes on from it. A transfer job is cancelled for it, and loses its place in the queue and the time it used: slurp asks
first. **Continue** (`c`) starts a new run (for a transfer job: submits a new job). **Run now** (`s`), **Edit…**
(`e`: the wizard with its settings; pause it first), **Its job** (the job window of its transfer job), **Connect …**
(the tab it waits for), **Delete** (`Del`: its settings and history, the files stay). The subtabs of the Transfers tab
are kept for the next start, like the others; `Ctrl-W` closes the one shown (the list stays). Keys that act on a
cluster (the job list, a shell, …) do nothing there; from the menu, the cluster tab shown before is shown first.

A transfer that needs a tab that is not connected waits for it (*waiting for jedi*); once both are connected it starts
by itself. When a tab's connection breaks during a relay run, the run waits and goes on when the tab is back. The
overview of each tab has a line about the transfers from or to it (a click shows the Transfers tab), notifications say
when a transfer is done, failed or needs a login, and closing a tab with a transfer under way says so first.

The transfers are kept in `$XDG_STATE_HOME/slurp/transfers.json` (`~/.local/state/slurp/`).
