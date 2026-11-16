# Copying

Copy buttons send the text to the terminal (OSC 52, wrapped for tmux and GNU
screen) and put it on the clipboard with the tool of the machine slurp runs on
(`wl-copy`, `xclip`, `xsel`, `pbcopy`; over SSH only a forwarded display counts).
OSC 52 is the only way to your clipboard when you ssh to the machine slurp runs
on, and only some terminals take it (kitty, WezTerm, iTerm2, Alacritty, foot,
Windows Terminal; in tmux with `set -g set-clipboard on`; not GNOME Terminal or
Konsole). Without a tool a window says so and offers **Plain text** (slurp steps
aside and prints the text in the normal screen, where the terminal's own mouse
selection works; Enter comes back), **Save to a file** (`~/.cache/slurp/copied.txt`)
and **It arrived, don't ask again** (stored as `"copy": "terminal"` for a
terminal and `"copy_web": "terminal"` for a browser view, the kind that is
active; Options, `F3`, changes them back). While a browser is the active view,
this machine's clipboard tool is not used: it would fill the clipboard of the
machine slurp runs on, not the browser's.
