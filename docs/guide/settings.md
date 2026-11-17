# Settings

slurp keeps a few settings in `~/.config/slurp/config.json` (or under
`$XDG_CONFIG_HOME`): the theme (`"theme"` in a terminal, `"theme_web"` in a
browser, which uses the terminal's until it has its own; with several views
the theme follows the active one; **Options**, `F3`, sets each), the
open tabs with the one shown and their choices (finished since, accounts),
the progress files chosen for jobs and the patterns for arrays. At the next
start the tabs are back; a cluster tab logs in when you press **Connect** in
its overview. The dot after the name of an SSH tab is coloured by the health
of its session (green, yellow, red; grey when not connected).

**Options** (`F3`) shows the settings and changes them: the two themes, the
icons, the scrollback of shells, what Copy does without a clipboard tool (for a terminal
and for a browser), the relay of
links, the rules the checks do not report, how the progress of jobs is read
(how much of the end of a file, of it how many lines, and over how many
progress values the current pace goes: more finds progress further back and
smooths the pace, but makes the job list and the job window slower; the window
warns about high values), and what slurp remembers (drives on
this machine, progress files and patterns, the tabs of the next start), with
Forget. Save writes `config.json`; the settings below are the same, by hand: `"terminal_scrollback"` (lines a shell keeps
above its screen, default 10000, 0 for none), `"checks_muted"` (rule ids the
checks do not report), `"link_relay"` (the relay of links), `"copy"`,
`"progress_tail_kib"` (default 64), `"progress_tail_lines"` (600),
`"progress_pace_points"` (6).

## Icons

Tabs, buttons, menu items, the states of jobs and the files of a folder have a
small icon in front, one per meaning (reload, copy, connect, a folder, a batch
script, completed, failed, an array, …). **Options** → **Icons** (`"icons"` in
`config.json`) chooses them, from the next start:

- **icons** (the default): the Material Design icons of
  [Nerd Fonts](https://www.nerdfonts.com/), all one cell wide, with icons for
  kinds of files (Markdown, HDF5, code, core dumps, …). A terminal draws them
  from slurp's icon font (`slurp --install-font`, see
  [Install](../install.md#the-icon-font)) or from a Nerd Font; the browser
  view has them.
- **Unicode symbols** (`"symbols"`): ↻ ⧉ ⇄ ▤ § ✓ ✗ ⊞ …, no font to install.
  They are even only in a terminal whose font has them all (DejaVu Sans Mono
  nearly); most fonts lack them, and the terminal then takes them from other
  fonts, one to two and a half cells wide.
- **none** (`"none"`): no icons.
