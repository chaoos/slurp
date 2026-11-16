# Tabs and subtabs

Every tab opens on its **Overview** (the cluster at a glance, see
[The overview](overview.md)); **Jobs** is the job list.
The toolbar of the job list has **☰ Menu** (`F10`) with what slurp does for
the tab, each with its key (the overview, quota, old files in scratch, shells,
the login directory and the drive, interactive job, new job script,
categories, this slurp in your browser, the guides, the rule catalogue,
options, about), and **Reload**, the time window of finished jobs, whose jobs
are listed and **group arrays**.

Every tab is a cluster (or this machine). Under the tabs, each tab has its
subtabs: **Jobs** (the job list, always there), and one subtab with a `✕` for
each job window, each file or directory opened from the job list, and each
shell (on the cluster, on the node of a job, GDB). What you open inside a
subtab, like the files of a job, opens in that subtab; `←` or `Esc` goes back,
and back from its first window shows the job list. Opening a job again shows
its subtab. Switching tabs keeps the subtabs. In a shell all keys go to the
shell, except `Ctrl-]`, which goes back to the job list; `Ctrl-W` closes the
subtab shown (outside a shell). Every tab can be closed, also the last one;
then only the `+` button remains.

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
