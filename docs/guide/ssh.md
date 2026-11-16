# Clusters over SSH

The Textual app stays on your local machine. It starts with a **Local** tab.
Pass one or more regular OpenSSH destinations (`user@host`) or aliases from
your `~/.ssh/config` to add tabs:

```bash
slurp --ssh localhost user@host cluster-login
```

OpenSSH uses your configured identities, certificates, agent, host aliases,
and `ProxyJump` chain. `localhost`, `127.0.0.1`, and `::1` refer to the local
machine tab.

Each remote tab has one SSH session, held open for the life of the app. It is
opened at the start for the tab shown then, and right away for a tab you add.
Switching to a tab that has no session does not log in by itself: its job
list shows **Connect** in its middle (or, when the tab still lists the jobs
from last time, in a bar above them), and the session opens when you press
it (or click the tab again). The login runs in the terminal
pane on the right, where password, key passphrase, keyboard-interactive, and
OTP prompts are answered. Once you are logged in, the pane hides by itself
and the job list loads; nothing has to be typed after the login. A failed
login stays in the pane so the error can be read; Enter there logs in again,
and closing it (`✕`) gives up the connection. When the login fails or the
connection is lost, the cluster may be down or in maintenance: for a site
whose rule pack names its status page (CSCS: `https://status.cscs.ch`, JSC:
`https://status.jsc.fz-juelich.de/`), the
status at the bottom right says *check status.cscs.ch*, and a click on it
opens the page (a click elsewhere there opens the connection window, which
says the same, with the link).

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
it until you press **Connect** in the job list.

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
**Shell on the cluster** (in the menu, or `t`) opens a login shell on the cluster of the tab in a subtab. The **+** tab button adds another local or remote connection.
`d` opens the login directory in the directory browser. Each tab has a `✕`
that closes it (after asking). Notifications name the tab they belong to.

**Interactive job** (in the menu, or `i`) shows an `sbatch` command for a job that only
holds a node (`sleep infinity`) in a bash editor, with fields for the account
(one of yours or typed), the partition, the time limit, nodes, GPUs and the job
name; the fields write the command, which stays editable. Once the job runs, its job
window opens shells on its node.

Every tab opens on its [overview](overview.md).
