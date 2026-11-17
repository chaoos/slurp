# Tabs and subtabs

The top line of every window is the header: **☰ Menu** (`F10`) at its left,
the title of slurp with its version, and **?** (the help of the window) at its
right. The menu has what slurp does for the tab, each with its key (the job
list, health, quota, old files in scratch, shells, a directory (home, scratch,
project, … or a path) and Map a directory (the drive), interactive job, new job script, categories, this slurp in your
browser, the guides, the rule catalogue, options, about). Under the header
are the tabs, then the subtabs. The toolbar of the job list has **Reload**,
the time window of finished jobs, whose jobs are listed and **group arrays**.

Every tab is a cluster (or this machine). Under the tabs, each tab has its
subtabs, each with a `✕`: **Jobs** (the job list), and one for each job
window, each file or directory, each shell (on the cluster, on the node of a
job, GDB). Quota and Health are windows over it.
At the start no
subtab is open, and while none is the tab shows its
[overview](overview.md), from where **Jobs** opens the job list. When the
subtab shown closes, the one shown before it comes back (the job list, a
subtab, or the overview). A file or
directory always opens in a subtab of its own, wherever you open it (the
files of a job, a path in a file, a file in a directory), and where you
opened it stays as it was. A folder you enter in a directory view opens in
that subtab (`←` or `Esc` goes back). Other windows you open inside a subtab
open in it, and back from its first window shows the job list (or the
overview). Opening a job, file or directory again shows its subtab. Switching tabs keeps the subtabs. In a shell all keys go to the
shell, except `Ctrl-]`, which goes back to the job list; `Ctrl-W` closes the
subtab shown (outside a shell). Every tab can be closed, also the last one;
then only the `+` button remains. Closing the tab shown goes back to the tab
shown before it.

Every window has a **?** at the right end of its top bar (dialogs in their
top right corner), and the `?` key does the same: a window explains what the
view shows, what it can do and its keys (taken from the view's own key
bindings, so they are always current), with the keys that work everywhere and
buttons to the guides and the rule catalogue. In a shell it opens the
terminal help below.

Shells keep a history: the mouse wheel (or `Shift+PgUp`/`Shift+PgDn`) scrolls
back through the lines that went off the top (10000 by default,
`"terminal_scrollback"` in the config; `clear` empties it), with a bar saying
how far back; typing returns to the bottom. In full-screen programs (less,
vim) the wheel sends arrow keys to them. Dragging with the mouse selects text,
also in the history; a right-click on the selection copies it.

**ⓘ Help** in the bar of a shell (and `ⓘ` in the title bar of the login pane)
explains that terminal: scrolling back, how to copy out of it (drag to
select, right-click to copy; `Ctrl+C` interrupts and does not copy) and paste into it (your terminal's paste, not a
plain `Ctrl+V`), which keys go where, how to close it and that closing ends
everything running in it, and how to treat it: a limited history (use `less`
or `tmux` for long output), `tmux` or `sbatch` for work that must survive, and login nodes are
shared. Its **Copy the screen** button copies the visible screen as text.
