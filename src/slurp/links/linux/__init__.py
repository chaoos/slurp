"""The slurp helper for Linux: opens slurp:// links (shell, drive, web view) with one click and the link's password.

The same slurp:// URL as for the Windows helper (windows/winhelper.link_url). The helper is one Python script
(slurp_helper.py, only the standard library: slurp need not be installed on the user's machine). The setup is a
shell script, as one line to paste into a terminal or as a file to download from the web view: it writes the helper
into ~/.local/share/slurp-helper, registers slurp:// for the user (a .desktop file and xdg-mime, no root) and
remembers the host key of this slurp (a link of another slurp asks first)."""

import base64
import gzip
from importlib import resources
import shlex

from .. import share

SCRIPT = resources.files(__package__) / "slurp_helper.py"
SETUP_NAME = "slurp-helper-setup.sh"

def packed(data):
  """bytes as base64 of their gzip (the setup carries the helper and its icon this way: one line each, no quoting)"""
  return base64.b64encode(gzip.compress(data, mtime=0)).decode()

def helper_script():
  """The helper as installed: with slurp's version"""
  from ... import __version__
  text = SCRIPT.read_text(encoding="utf-8")
  marker = 'VERSION = "dev"'
  assert marker in text
  return text.replace(marker, f"VERSION = {__version__!r}", 1)

def setup_script():
  """The shell script that sets the helper up (or updates it) on the user's machine"""
  from ...ui import logo
  from ... import VERSION_TEXT
  alias, fingerprint = share.host_alias(), share.host_fingerprint()
  return f"""#!/bin/sh
# slurp helper {VERSION_TEXT} for Linux: opens slurp:// links (shell, drive, web view) with one click and the password.
# Puts the helper into ~/.local/share/slurp-helper and registers slurp:// links for this user (no root).
set -e
say() {{ printf 'slurp helper: %s\\n' "$*"; }}
if ! command -v python3 >/dev/null 2>&1; then say "python3 is needed (e.g. sudo apt install python3)"; exit 1; fi
data="${{XDG_DATA_HOME:-$HOME/.local/share}}"
dir="$data/slurp-helper"
apps="$data/applications"
mkdir -p "$dir" "$apps"
say "setting up {VERSION_TEXT} in $dir"
echo '{packed(helper_script().encode())}' | base64 -d | gzip -d > "$dir/slurp-helper.new"
chmod 755 "$dir/slurp-helper.new"
mv -f "$dir/slurp-helper.new" "$dir/slurp-helper"
echo '{packed(logo.png(64))}' | base64 -d | gzip -d > "$dir/slurp.png"
line={shlex.quote(f"{alias} {fingerprint}")}
touch "$dir/trusted.txt"
grep -qxF "$line" "$dir/trusted.txt" || printf '%s\\n' "$line" >> "$dir/trusted.txt"
cat > "$apps/slurp-helper.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=slurp helper
Comment=Opens slurp links: shells, drives and web views of slurp
Exec="$dir/slurp-helper" %u
Icon=$dir/slurp.png
Terminal=false
Categories=Network;
MimeType=x-scheme-handler/slurp;
EOF
if command -v xdg-mime >/dev/null 2>&1; then
  xdg-mime default slurp-helper.desktop x-scheme-handler/slurp || say "xdg-mime failed: slurp:// links may not open the helper"
else
  say "xdg-mime is missing (xdg-utils): slurp:// links may not open the helper; open them with: $dir/slurp-helper LINK"
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps" >/dev/null 2>&1 || true
for tool in ssh ssh-keyscan; do
  command -v "$tool" >/dev/null 2>&1 || say "$tool is missing: install OpenSSH (e.g. sudo apt install openssh-client)"
done
command -v sshfs >/dev/null 2>&1 || say "sshfs is missing: drives need it (e.g. sudo apt install sshfs, sudo dnf install fuse-sshfs)"
command -v zenity >/dev/null 2>&1 || command -v kdialog >/dev/null 2>&1 || \\
  say "neither zenity nor kdialog: the helper cannot ask for passwords in a window (e.g. sudo apt install zenity)"
say "ready. It trusts this slurp: {alias} ({fingerprint})"
say "in slurp, a link window: Open on my computer. Open connections: 'slurp helper' in your apps, or $dir/slurp-helper list"
say "remove it again: $dir/slurp-helper uninstall"
"""

def setup_command():
  """The setup as one line to paste into a terminal"""
  return f"echo {packed(setup_script().encode())} | base64 -d | gzip -d | sh"

def setup_file():
  """The setup as a file (bytes) to run: sh slurp-helper-setup.sh"""
  return setup_script().encode()
