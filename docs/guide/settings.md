# Settings

slurp keeps a few settings in `~/.config/slurp/config.json` (or under
`$XDG_CONFIG_HOME`): the theme (`"theme"` in a terminal, `"theme_web"` in a
browser, which uses the terminal's until it has its own; with several views
the theme follows the active one; **Options**, `F3`, sets each), the
open tabs with the one shown and their choices (finished since, accounts),
the progress files chosen for jobs and the patterns for arrays. At the next
start the tabs are back, and the tab shown connects; the others show
**Connect** when they are selected. The dot of an SSH tab is coloured by the health of its
session (green, yellow, red; grey when not connected yet).

**Options** (`F3`) shows the settings and changes them: the two themes, the
scrollback of shells, what Copy does without a clipboard tool (for a terminal
and for a browser), the relay of
links, the rules the checks do not report, and what slurp remembers (drives on
this machine, progress files and patterns, the tabs of the next start), with
Forget. Save writes `config.json`; the settings below are the same, by hand: `"terminal_scrollback"` (lines a shell keeps
above its screen, default 10000, 0 for none), `"checks_muted"` (rule ids the
checks do not report), `"link_relay"` (the relay of links), `"copy"`.
