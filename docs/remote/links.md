# Shells and drives on your own machine

!!! tip "One click instead of commands"
    The [slurp helper](helper.md) for Windows and Linux opens a link with one click and its password.

**Shell on the cluster** (in the menu, `t`, and `s` in the job window of a running job for its node) asks
every time: **In slurp** (a subtab, as before) or **From your own terminal**.
The latter makes a *link*: an `ssh` command and a password for the terminal or
SSH client on your own machine (OpenSSH in any terminal, cmd or PowerShell,
PuTTY, …). **Map a directory…** (in the menu, `F`) asks where your login directory goes as a drive:

- **On this machine** (where slurp runs): `sshfs` through the session of the tab
  (no login). **Directory** is the directory of the cluster: your home, scratch,
  project, … from the list (`$SCRATCH`, `$PROJECT`, `$STORE`, `$WORK`, those set
  on the cluster) or a path typed in (`~/…` and relative paths are in your home).
  It goes at a path you choose (default `~/mnt/<tab>`), both remembered per tab
  as `"drive_mounts"` in the settings, read only if you like. The connection
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
it to slurp (`ssh -J`). A relay is nothing more than an SSH host that both ends
can reach: it only passes bytes along, the session stays SSH encrypted from
your terminal to slurp, and slurp's host key is checked end to end (on the
first connect your ssh shows it; the link window shows its fingerprint,
remembered under one name, so it asks only once). The setup panel lists the
relays to choose from, each with a line on what it is and the details of the
one chosen; the choice is a setting (`"link_relay"` in the settings):

- **ssh-j.com**, the default and the prime example: a public relay, there for
  exactly this. It needs no account on either end: slurp registers each link
  under a random name, your ssh asks for that name. It is run by a third party:
  the setup panel warns about what that means (anyone with the command and the
  password gets in as you; the relay sees that connections happen; your site's
  rules may not allow it). Another relay of that kind: `sshj://host`.
- **serveo.net**: public, no account, used the same way as ssh-j.com (a random
  name there, `ssh -J`); slurp connects anonymously (none of your keys go
  there).
- **pinggy.io**: public, no account, but a different kind: it gives slurp's
  server for the link a TCP port on the internet, and your ssh connects to
  that host and port itself (no jump). Anyone on the internet can reach the
  port and try the password (5 wrong ones lock the link, so a scanner may lock
  it); a free tunnel lasts 60 minutes, then slurp opens a new one at another
  address (copy the command again); the address names the public IP of slurp's
  network.
- **sshreach.me**: a service with an account (a trial, then paid): create a
  client in its web interface, and give the user, forwarding server and port
  its "manual control" page shows (`user@fw1.sshreach.me:10248`). slurp logs in
  there with the key registered for the client and forwards that port; your
  ssh connects to that server and port (no jump).
- **custom / own relay**: any OpenSSH server both ends reach, e.g. `me@relay.example.org`: a
  plain jump host, nothing to install. slurp logs in to it with your key and
  asks it for a free port (`ssh -R 0:...`), so it must allow forwarding (the
  default); your ssh then needs a login there too (the same account, or
  another). Nobody else is involved.
- **direct** (no relay): when your machine reaches the one slurp runs on
  itself (the same network, a VPN), `direct` makes the link listen on that
  machine (every interface, a free port) and your ssh connects straight to it,
  no third party. With `direct` the setup panel shows **Host**, the name or
  address your machine connects to (by default this machine's fully qualified
  name; often another one: its name on your VPN, an alias, an address), and
  **Port** (empty: a free one; or a fixed one your firewall lets through).
  slurp keeps both for the machine it runs on (`"link_direct"` in the settings,
  one entry per machine, since many nodes often share one home directory).
  With an address as the host it listens on that address only. Whoever
  reaches the port can try the password (5 wrong ones lock the link). The
  commands, the drive window and the Windows helper then connect directly, with
  no tunnel step.

The **setup panel** of a link: the relay (with its warning), **Logins** (once,
the default for shells; any number until it expires, the default for drives,
since mounts reconnect), **Expires after** (15 minutes, 1 hour (default),
8 hours, until slurp quits), login with **a password** made by slurp (default)
or **your public key**, and for a drive **Read only**. A link stops taking
logins when it is used, expires, is revoked, or after 5 wrong passwords; open
sessions go on. The **link window** shows the commands for each kind of
client with copy buttons (and a `~/.ssh/config` entry), the password, the host
key and the state; **New password**, **Extend lifetime…** (how much longer: 1 hour up to a week, added to its end), **Revoke**.

The **connection window** (click the status of the tab) lists the links of the
tab and their sessions (shell or drive, since when, client, traffic, terminal
size), with **Started** (when each link was made, how long ago) and **Valid**
(how long it still takes logins and until when, or until slurp quits; its
relay's end if that comes first, e.g. pinggy's hour; yellow in the last five
minutes; when it takes no more, why); the link window shows the same. A click on
a link opens its window, on a session disconnects it.
**Health** shows for every link and session what the status of the tab shows
for its SSH login: green *healthy* (with the round trip of a heartbeat that
slurp sends each client it has not heard from for 15 s), yellow *slow* or
*unstable* (the relay, or a drive's client, reconnected in the last 10
minutes), red *no answer* or *relay lost*. A drive is one row, and under it one
row per folder its clients mounted (`/scratch/…`, the login directory, …, as
the client's first request names it), with how often that folder's
connection was made again (sshfs and the helper reconnect by themselves).
Logins, wrong passwords and lost relays are notified, the footer shows open
links and sessions, and quitting with open sessions asks first. Every link and
session is written to `~/.local/state/slurp/links.log` (never a password);
slurp's host key is `~/.config/slurp/link_host_key`.

Links need the Python package `asyncssh` (`python -m pip install asyncssh`);
without it slurp says so in the Shell dialog and works as before. A drive uses
the cluster's `sftp-server` (read only needs it; otherwise the cluster's
standard SFTP).
