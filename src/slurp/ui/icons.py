"""The small icons in front of buttons, tabs, menu items, states and files: one per concept, in three sets.

- "nerd" (the default): the Material Design icons of Nerd Fonts (nf-md-*), all one cell wide. slurp's own font
  fonts/slurp-icons.ttf has them (tools/make_icon_font.py): the browser view loads it, a terminal needs it (slurp
  --install-font) or a Nerd Font, else it shows boxes
- "symbols": Unicode symbols. Even only where the terminal's font has them all (DejaVu Sans Mono has all but ⧉ and
  ⓘ); else the terminal takes them from other, proportional fonts, one to two and a half cells wide
- "none": no icons

The set is chosen in Options (config "icons") and applies from the next start. Each name maps to (symbol, Nerd Font
codepoint, its nf-md name); "" for a symbol means: none of its own (a kind of file: the file's). File types beyond
folder, file and script are icons of the Nerd Font set only."""

import os

from ..config import load_config

STYLES = ("nerd", "symbols", "none")
FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "slurp-icons.ttf")


ICONS = {
  # tabs and the overview
  "local": ("⌂", 0xF0322, "laptop"),
  "cluster": ("▦", 0xF048D, "server_network"),
  "jobs": ("≡", 0xF0279, "format_list_bulleted"),
  "shell": ("❯", 0xF018D, "console"),
  "drive": ("⏏", 0xF0870, "folder_network"),
  "folder": ("▤", 0xF024B, "folder"),
  "file": ("▯", 0xF0224, "file_outline"),
  "script": ("§", 0xF0BC2, "script_text"),
  "docs": ("¶", 0xF14F7, "book_open_variant"),
  "status": ("◉", 0xF052B, "traffic_light"),
  "health": ("♥", 0xF05F6, "heart_pulse"),
  "quota": ("◔", 0xF012B, "chart_pie"),
  "scratch": ("✂", 0xF00E2, "broom"),
  "rules": ("☑", 0xF014E, "clipboard_check"),
  "steps": ("»", 0xF027B, "format_list_numbered"),
  "progress": ("◔", 0xF012B, "chart_pie"), # (the glyph of quota: in the font already)
  "options": ("⚙", 0xF0493, "cog"),
  "web": ("◈", 0xF059F, "web"),
  "interactive": ("⌨", 0xF08A9, "console_network"),
  "categories": ("◆", 0xF04F9, "tag"),
  "new_script": ("✚", 0xF0752, "file_plus"),
  "filter": ("⌕", 0xF0232, "filter"),
  "key": ("✱", 0xF0306, "key"),
  "about": ("?", 0xF02FD, "information_outline"),
  "quit": ("⎋", 0xF0206, "exit_to_app"),
  "array": ("⊞", 0xF0136, "checkbox_multiple_blank"),
  # actions
  "connect": ("⇄", 0xF0318, "lan_connect"),
  "reconnect": ("⇄", 0xF031A, "lan_pending"),
  "disconnect": ("⊘", 0xF0319, "lan_disconnect"),
  "reload": ("↻", 0xF0450, "refresh"),
  "copy": ("⧉", 0xF018F, "content_copy"),
  "revoke": ("⌧", 0xF0338, "link_off"),
  "check": ("☑", 0xF014E, "clipboard_check"),
  "edit": ("✎", 0xF03EB, "pencil"),
  "submit": ("➤", 0xF048A, "send"),
  "test": ("⚗", 0xF0093, "flask"),
  "explain": ("ⓘ", 0xF06E8, "lightbulb_on"),
  "restart": ("↺", 0xF099B, "restore"),
  "cancel_job": ("⊘", 0xF073A, "cancel"),
  # states of jobs
  "pending": ("◷", 0xF0150, "clock_outline"),
  "running": ("▶", 0xF040C, "play_circle"),
  "completed": ("✓", 0xF05E0, "check_circle"),
  "failed": ("✗", 0xF0159, "close_circle"),
  "cancelled": ("⊘", 0xF073A, "cancel"),
  "timeout": ("◴", 0xF199F, "timer_sand_complete"),
  "suspended": ("‖", 0xF03E5, "pause_circle"),
  "requeued": ("↺", 0xF099B, "restore"),
  # kinds of files (Nerd Font only)
  "markdown": ("", 0xF0354, "language_markdown"),
  "hdf5": ("", 0xF01BC, "database"),
  "core": ("", 0xF068C, "skull"),
  "text": ("", 0xF09EE, "file_document_outline"),
  "code": ("", 0xF102B, "file_code_outline"),
  "image": ("", 0xF0976, "image_outline"),
  "archive": ("", 0xF120E, "archive_outline"),
}

STATE_ICONS = {"PENDING": "pending", "CONFIGURING": "pending", "RUNNING": "running", "COMPLETING": "running",
               "RESIZING": "running", "COMPLETED": "completed", "CANCELLED": "cancelled", "REVOKED": "cancelled",
               "TIMEOUT": "timeout", "DEADLINE": "timeout", "SUSPENDED": "suspended", "PREEMPTED": "suspended",
               "STOPPED": "suspended", "REQUEUED": "requeued", "REQUEUE_FED": "requeued", "REQUEUE_HOLD": "requeued"}
# (the others: FAILED, OUT_OF_MEMORY, NODE_FAIL, BOOT_FAIL, SPECIAL_EXIT, ...: failed)

SCRIPT_EXTS = {".sbatch", ".slurm", ".job", ".sh", ".bash"}
FILE_KINDS = {**dict.fromkeys((".md", ".markdown", ".mdown", ".mdx"), "markdown"),
              **dict.fromkeys((".h5", ".hdf5", ".he5", ".nc"), "hdf5"),
              **dict.fromkeys((".out", ".err", ".log", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
                               ".ini", ".cfg", ".conf"), "text"),
              **dict.fromkeys((".py", ".c", ".h", ".cc", ".cpp", ".hpp", ".f", ".f90", ".jl", ".r", ".m", ".rs",
                               ".go", ".java", ".pl", ".lua"), "code"),
              **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".eps", ".tif", ".tiff"), "image"),
              **dict.fromkeys((".tar", ".gz", ".tgz", ".zip", ".bz2", ".xz", ".zst", ".7z"), "archive")}

_style = None

def style():
  """The set in use: the one stored in the config, read once"""
  global _style
  if _style is None:
    chosen = load_config().get("icons")
    _style = chosen if chosen in STYLES else "nerd"
  return _style

def set_style(chosen):
  """(for the tests, and the start: None reads the config again)"""
  global _style
  _style = chosen if chosen in STYLES else None

def icon(name):
  """The icon of a concept in the set in use ("" when there is none)"""
  entry = ICONS.get(name)
  current = style()
  if entry is None or current == "none":
    return ""
  if current == "symbols":
    return entry[0] or ICONS["file"][0] # (a kind of file with no symbol of its own: a file)
  return chr(entry[1])

def label(name, text):
  """A text with the icon of a concept in front (just the text without one)"""
  mark = icon(name)
  return f"{mark} {text}" if mark else text

def state_icon(state):
  """The concept of a Slurm state ("FAILED (1:0)", "CANCELLED by 123", "RUNNING", …)"""
  word = (state or "").split(" ")[0].rstrip("+")
  return STATE_ICONS.get(word, "failed" if word else "")

def file_kind(name, is_dir=False):
  """The concept of a file or directory by its name: folder, script, and with the Nerd Font set its type"""
  if is_dir:
    return "folder"
  lower = name.lower()
  ext = os.path.splitext(lower)[1]
  if ext in SCRIPT_EXTS:
    return "script"
  if style() == "nerd":
    if lower == "core" or lower.startswith("core."):
      return "core"
    if ext in FILE_KINDS:
      return FILE_KINDS[ext]
  return "file"
