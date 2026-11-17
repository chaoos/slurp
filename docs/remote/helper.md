# The slurp helper

A small program on your own computer that opens [links](links.md) with one click: **Open on my computer** in the link window, the link's password, and the drive is mapped, the web view open in your browser, or a terminal open with the shell. It exists for Windows and Linux; on macOS the link window's commands do the same by hand.

## Windows

A tray program for Windows 10 and 11 that opens links: the link window of a
password link has **Open on my computer**. With slurp open in your browser
(remote web access), that hands a `slurp://` link to the browser, which starts
the helper (the first time, it asks whether to: *always allow* saves the
question). From a terminal, **Copy helper link**, then Win+R, paste, Enter.
The helper shows a small window: the link's password (Paste) and, for a drive,
the letter (the first free one); a shell asks for no password there. Then it
connects through the relay, checks slurp's host key against the one in the
link, and:

- **drive**: maps a folder of the cluster (SSHFS-Win, `net use`) and opens
  Explorer, where the drive is named after it, `user@cluster:/path` (not
  `slurp (\\sshfs.r\slurp@localhost!…)`; the name goes when it is unmapped). Its list has `$HOME`, `$SCRATCH`, `$PROJECT`, `$STORE` and `$WORK`
  (those that are set, with their paths) and `/`; or type a path (absolute,
  `$SCRATCH/run1`, or relative to `$HOME`). *Map another folder* (a right
  click on its row) maps more folders, on other letters, through the same
  connection. It reconnects them all when
  the connection drops;
- **web view**: an SSH tunnel to the web view (no window to keep open) and the
  browser at it;
- **shell**: at once (nothing to ask), a terminal window with the shell,
  where ssh asks for the password (you type or paste it there, once; 3
  tries); the helper sees when the shell's prompt shows. *Open another shell*
  (a right click on its row) opens one more, if the link takes more logins.

The password is not part of the link and not stored: for a web view the helper
types it into ssh's prompt for you, for a drive it gives it to `net use`. What is open stays in the
tray icon (^ next to the clock). The helper has **one window** (a click on the
icon; it shows by itself when a link comes): every connection is a row with
its state (mapped folders, the web view's local port, relay or direct, since
when, until when the link takes logins), and the row selected shows below
the list: what it is, its settings while it is not connected (folder, letter,
password, then *Map drive* / *Open web view* / *Open terminal*), its state and
all its steps. **A right click on a row** has what that connection can do: open
it (each mapped letter in Explorer, the web view in the browser), *Map another
folder…* (a small window: folder and letter) and *Unmap* one letter for a
drive, *Open another shell* for a shell, *Reconnect* (a drive maps all its
folders again; the password is remembered while the helper runs),
*Disconnect* (it stays in the list), *Logs*, *Copy the details* and *Remove*.
The buttons under the list do the most common of these. The window also shows
whether ssh and SSHFS-Win are there. The icon's menu has the connections too,
and the helper's own settings: *Start with Windows*, *Known slurps*, the
folder with the logs, *Uninstall* and *Quit*.

**Set it up once** (no admin rights): **Set up the helper…** in a link
window. **Download the setup** (with slurp in your browser) gives
`slurp-helper-setup.cmd` to run (Windows may warn about a file from the
internet: *More info → Run anyway*); **Copy the setup command** is the same as
a command to paste into PowerShell. It puts the helper into
`%LOCALAPPDATA%\slurp-helper`, compiles its small launcher `slurp-open.exe`
there from the source code in the setup (no program is downloaded), registers
`slurp://` for your Windows user, starts it with Windows, adds a start menu
entry, and remembers this slurp's host key: a link of another slurp asks
before it is used. Setting it up again updates it. It needs the OpenSSH Client
of Windows (an optional feature, there by default on Windows 11); a drive also
needs WinFsp and SSHFS-Win, which it offers to install with winget.

## Linux

The same one click for Linux: **Open on my computer** in the link window (or
`xdg-open 'slurp://…'` with the link from **Copy helper link**), the link's
password in a small window, and the helper connects through the relay, checks
slurp's host key against the one in the link, and:

- **drive**: asks which folder (`$HOME`, `$SCRATCH`, …, `/`, or one you
  type), mounts it with sshfs at `~/slurp/<link>/<folder>` and opens your file
  manager. It reconnects when the connection drops. Ejecting it in the file
  manager (or `fusermount -u`) ends it; for another folder open the link again;
- **web view**: an SSH tunnel to the web view and the browser at it;
- **shell**: a terminal window that connects and then runs ssh, which asks
  for the password there.

It asks with zenity or kdialog (in the terminal if it runs in one) and reports
with desktop notifications; a failure shows in a window, with why. **slurp
helper** in your applications lists the open connections (open one again, or
end it); in a terminal `slurp-helper list` and `slurp-helper stop ID` (or
`all`). The password is not part of the link and never written to disk: ssh
gets it from the helper as its askpass program.

**Set it up once** (no root): **Set up the helper…** in a link window, tab
**Linux**. **Download the setup** (with slurp in your browser) gives
`slurp-helper-setup.sh` (run `sh ~/Downloads/slurp-helper-setup.sh`); **Copy
the setup command** is the same as one line to paste into a terminal. It puts
the helper, one Python script that needs only `python3`, into
`~/.local/share/slurp-helper`, registers `slurp://` for your user (a `.desktop`
file and `xdg-mime`), and remembers this slurp's host key: a link of another
slurp asks before it is used. Setting it up again updates it;
`~/.local/share/slurp-helper/slurp-helper uninstall` removes it. It needs
OpenSSH 8.4 or newer (`ssh`, `ssh-keyscan`), zenity or kdialog for its windows,
and for a drive `sshfs`; the setup says what is missing. macOS keeps the
commands of the link window for now.
