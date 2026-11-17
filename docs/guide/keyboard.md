# Keyboard and mouse

The app supports clicking and scrolling as well as keyboard navigation. Use
`Tab` to move between controls, `Enter` to open a selection, and `Esc` or the
top-left `←` button to go back.

| Screen | Main controls |
|---|---|
| Overview | `j` the job list; `Q` quota; `C` health; `r` ask the cluster again; `F10` the menu |
| Job list | `F10` the menu; `/` search jobs (`cat:a/b`: a category); `Enter` open; `Space` open/close an array row; `g` group arrays on/off; `!` only jobs with problems; `m` mark, `M` all/none, `c` categorize, `K` categories; `n` new job script; `W` remote web access; `r` reload; `a` abort loading; `t` shell on the cluster (in slurp or your own terminal); `F` login directory as a drive; `d` login directory; `i` interactive job; `C` health; `Q` quota; `X` old scratch files; `q` quit |
| Job details | `Reload` (or `r`); `Cancel job` / `Restart job` / `Run array` / `Array` / `Shell…` (or `s`: a shell on a node of the job, chosen in a list, in slurp or your own terminal); `c` categorize; Checks: `Check`, `Placement`, `Fixed copy…`, `What changed`, `Rule catalogue` |
| Script view | `r` check (all checks, the environment too); `x` explain the `#SBATCH` lines; `e` edit in place (`Ctrl+S` save, `Esc` stop editing); buttons `Test`, `Fixed copy…`, `Submit…` |
| Everywhere | `F10` (or **☰ Menu** in the header) the menu; `?` (or the **?** button) help of the window shown; `F1` guides; `F2` rule catalogue (`/` searches it); `F3` options |
| Categories | the tree (and *not categorized*): `n` new, `e` rename/move, `Del` delete; its jobs: `Enter` open, `c` categorize, `x` take out; `l` in the job list; `r` reload |
| Text file | `/` search (the whole file); `n` / `N` next/previous; `g` / `G` start / end; `:` a line or `50%`; `Space` / `b` page down / up; `j` / `k` a line; `f` follow; `A` the part before; `r` reload |
| Directory | `/` search names (here as you type; `Ctrl+R` also in the subdirectories, in the background; `Esc` stops, then clears); `f` follow; `.` toggle hidden files; `Backspace` or `u` go to parent |
| HDF5 | `s` enter a dataset selection; `[` / `]` change row page (200 rows); `{` / `}` change column page; `r` reload |
| Markdown | `r` reload; use the viewer's table of contents and link navigation |
| Shell (subtab) | mouse wheel or `Shift+PgUp`/`Shift+PgDn` scroll back; drag to select, right-click on it to copy; `Ctrl-]` back to the job list; `Ctrl-D` exits the shell, Enter starts it again; `✕` of the subtab closes it; **ⓘ Help** copy, paste, keys, closing |
| Subtabs | `Ctrl-W` close the subtab shown, the job list too (outside a shell) |
| Login pane | `Ctrl-]` switch between the window and the login; `Ctrl-X` gives up the login; `ⓘ` copy, paste, keys, closing |

When using tmux, enable mouse support with `set -g mouse on` if you want to
click and scroll inside the app.
