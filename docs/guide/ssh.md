# Clusters over SSH

The Textual app stays on your local machine. It starts with a **Local** tab
the first time, and later with the tabs of the last run. `+` next to the tabs
adds one: a regular OpenSSH destination (`user@host`) or an alias from your
`~/.ssh/config`.

OpenSSH uses your configured identities, certificates, agent, host aliases,
and `ProxyJump` chain. `localhost`, `127.0.0.1`, and `::1` refer to the local
machine tab.

Each remote tab has one SSH session, held open for the life of the app. It is
opened right away for a tab you add with `+`; otherwise slurp does not log in
by itself, neither at the start nor when you switch to a tab: its overview
shows **Connect** in its middle (and its job list, or, when the tab still
lists the jobs from last time, a bar above them), and the session opens when
you press it (or click the tab again). The login runs in the terminal
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
files are read in parts (a job's output, error and progress file open at their end,
the last 1 MB, followed while the job runs; anything else, e.g. from
the directory browser, at its top, the first 1 MB; scrolling on reads the next MB, `G`, `:` and searches jump
in the file, like `less`; following reads only what was appended), and HDF5 files are read in place by h5py
through a file object that fetches only the byte ranges it asks for (1 MiB
blocks), so a large file costs only what is browsed.

Clicking the connection state in the footer opens the connection
window of the tab: its state, the course of a (re)connect (connecting,
waiting for input, connected, failed with the error and what ssh printed), the
logged-in user and login node, the resolved destination (`ssh -G`), traffic
and round-trip times of the command channel, the ssh process and its TCP
connection (bytes, rtt, retransmissions, from `ss`), and a log of the session
events. **Reconnect** ends the session and logs in again, **Disconnect** (also
in the overview) ends it until you press **Connect**.

The footer shows the state of the session of the active tab, tinted by its
health: green (answers, with the round-trip time), yellow (slow, connecting),
red (no answer, lost); the connection window shows the same in its title, and the tab buttons mark each tab with a
dot in that colour after its name: `●` connected, `◌` connecting or not answering, `○` not connected. A heartbeat
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
**Shell on the cluster** (in the menu, or `t`) opens a login shell on the cluster of the tab in a subtab. The **+** tab button adds another local or remote connection, in a form like the endpoints': its **Name** first (the
tab's name; empty: the destination), then best an **SSH destination**, a Host of your
`~/.ssh/config` (suggested as you type), or `user@host`; as a fallback the **Full SSH settings** (host, user,
port, jump hosts, identity files, more `ssh` options), kept with the tab. A jump host is offered the tab's identity
files too (`ssh -J` alone would give them only to the cluster). An identity file must be a private key on this
machine: one that is not there, a directory or a `.pub` file gets a red mark at once, with what is wrong below it,
and **Add** and **Test** say so first: they become **Add anyway** and **Test anyway** (red), and pressed again they
go on (the command before the login may make the key, for example). **Browse…** next to it chooses a key on this machine (it starts in
`~/.ssh`; slurp does not read or show its content). Its buttons are **Test**, **Cancel** and
**Add**, as the endpoints' forms.

**From a site**, at the top of a new connection: **Choose…** opens a window with the clusters slurp knows how to log
in to, to search (every word you type must match the cluster, its site, its host or the pack: `juwels booster`,
`cscs`, `lumi`; `↑` `↓` while typing, `Enter` takes it; below the list, how the highlighted one logs in), and
**Clear** takes the choice back. They come from its site packs: CSCS Alps (Daint, Santis, Clariden, Eiger), JSC (JURECA, JUWELS, JUSUF, JEDI, JUPITER), LUMI, CINECA
Leonardo (a 12-hour certificate from `step ssh login`), the other EuroHPC systems (MareNostrum 5, Karolina, MeluXina,
Vega, Discoverer, Deucalion), LRZ SuperMUC-NG, DKRZ Levante, MPCDF Raven and Viper (through the gateway
`gate1.mpcdf.mpg.de`), SURF Snellius, NERSC Perlmutter (a 24-hour key from `sshproxy`), OLCF Frontier, TACC
Stampede3 and NCSA Delta (a password and a one-time code, asked in the login pane). One
fills in the form: its name, the full settings (the host; at CSCS the jump host `ela.cscs.ch` and the key
`~/.ssh/cscs-key`), and the command before the login (at CSCS `cscs-key sign --headless`, only when a login without
it fails: a signed key is valid for a day). You fill in your user (at CSCS the jump host takes it too): the
fields the site needs from you have a yellow border until they are filled in, and **Add** or **Test** without them
turns them red, with what to fill in below each (a pack names them in `required`); then **Add anyway** and **Test
anyway** go on without them. Below the
choice the form says how the site logs in, whether what it needs is installed on this machine (`cscs-key`; if not,
where to get it), and what to do once before the first login (make a key; at JSC, upload it in JuDoor with a
`from=` clause). Nothing of it is used until **Add**, and all of it can be changed first. slurp never writes your
`~/.ssh/config`. A site pack of your own can offer clusters too (`[login]` and `[[clusters]]`, see
`src/slurp/sites/__init__.py`). **Test** tries the
connection in a window with a terminal, as the tab's login goes: what needs no login (what ssh makes of it, the
keys, the name, the port, the server), the command before the login if there is one, and the login itself, where
you answer what it asks (a host key, a passphrase, a password, a one-time code). Then it says whether it works,
or precisely what does not (the host key, the keys and what the server accepts, …), with the end of `ssh -v`'s
log; the connection window has the same check (**Check…**). A tab can be added
tested or not.

**Run before login** (in the same dialog, for a `~/.ssh/config` destination too) is a command run on this machine
before every login of the tab, e.g. one that signs a key first (`cscs-keygen`, `step ssh login`, `tsh login`). It
runs in the login pane, where you answer it (a password, a one-time code; `Ctrl-C` ends only the command); when it
returns, slurp shows how it ended and the SSH login follows as usual. What the command exports is there for the
login: `eval "$(ssh-agent -s)"; ssh-add …` in it starts an agent the login then uses (slurp says which variables it
set). With **only when the login fails without it**, slurp first logs in without the command, asking nothing (`ssh -o
BatchMode=yes`): a key or certificate that is still valid is not made anew. Only if that fails does the command run,
and the login follows at once, in the pane; after a login that worked the next one tries without it first again.
The connection window shows it, and **Check…** goes the same way (without it first, if the box is ticked; then the
command, before its login).
**Open a directory…** (in the menu, or `d`) asks which directory: your home (the default), scratch, project, … (`$SCRATCH`, `$PROJECT`, `$STORE` and `$WORK`, those set on the cluster) or a path, and opens it in a new subtab, each time another one. Its path at the top is an input (edit it, Enter goes there) with buttons for home, scratch, project, … next to it. The directory view shows the permissions of each entry as `ls -l` does (a `+` when it has extended ACL entries, which are listed in the **ACL** column), its owner and group, and in **You** what you cannot do with it: **no read** (red) or **no write** (yellow), as the cluster answers for your user (groups and ACLs count). `/` searches the names (a part of them, or `*.out`), here as you type, or with **Subdirectories** (`Ctrl+R`) in all directories below, in the background: the results come in as they are found while the window stays usable, and `Esc` stops the search. Each tab has a `✕`
that closes it (after asking). Notifications name the tab they belong to.

**Interactive job** (in the menu, or `i`) shows an `sbatch` command for a job that only
holds a node (`sleep infinity`) in a bash editor, with fields for the account
(one of yours or typed), the partition, the time limit, nodes, GPUs and the job
name; the fields write the command, which stays editable. Once the job runs, its job
window opens shells on its node.

While no subtab of a tab is open it shows its [overview](overview.md).
