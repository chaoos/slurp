"""The icons: one cell wide in each set (a wider one shifts the columns and the buttons), a state and a file kind for
each name, and none at all when switched off"""
from rich.cells import cell_len

from slurp.resources import icons


def test_icons_are_one_cell_wide(check):
  for name, (symbol, codepoint, nerd_name) in icons.ICONS.items():
    check(f"{name}: the symbol is one cell", symbol == "" or cell_len(symbol) == 1, symbol)
    check(f"{name}: the Nerd Font icon ({nerd_name}) is one cell, in the Material Design range",
          cell_len(chr(codepoint)) == 1 and 0xF0001 <= codepoint <= 0xF1AF0, hex(codepoint))


def test_the_sets(check):
  try:
    icons.set_style("symbols")
    check("a symbol in front of the text", icons.label("reload", "Reload") == "↻ Reload")
    check("a kind of file without a symbol: the file's", icons.icon("hdf5") == icons.ICONS["file"][0])
    check("an unknown name: just the text", icons.label("nothing", "Text") == "Text")
    icons.set_style("nerd")
    check("Nerd Font: its codepoint", icons.icon("reload") == chr(0xF0450))
    check("Nerd Font: kinds of files", icons.file_kind("notes.md") == "markdown" and icons.file_kind("core.1234") == "core")
    icons.set_style("none")
    check("none: just the text", icons.label("reload", "Reload") == "Reload" and icons.icon("folder") == "")
  finally:
    icons.set_style(None)


def test_states_and_files(check):
  try:
    icons.set_style("symbols")
    for state, name in (("PENDING (Priority)", "pending"), ("RUNNING", "running"), ("COMPLETED (0:0)", "completed"),
                        ("FAILED (1:0)", "failed"), ("OUT_OF_MEMORY (0:125)", "failed"), ("CANCELLED by 1000", "cancelled"),
                        ("CANCELLED+", "cancelled"), ("TIMEOUT (0:0)", "timeout"), ("SUSPENDED", "suspended"),
                        ("REQUEUED", "requeued"), ("", "")):
      check(f"{state or '(none)'}: {name or 'no icon'}", icons.state_icon(state) == name, icons.state_icon(state))
    check("a directory", icons.file_kind("results", is_dir=True) == "folder")
    check("batch scripts", all(icons.file_kind(n) == "script" for n in ("run.sbatch", "job.slurm", "go.sh")))
    check("with symbols other files are files", icons.file_kind("notes.md") == "file" and icons.file_kind("core.1") == "file")
  finally:
    icons.set_style(None)


def test_the_icon_font_has_every_icon_in_one_cell(check):
  """slurp-icons.ttf (tools/make_icon_font.py): each icon of the Nerd Font set, all as wide as a cell"""
  fontTools = __import__("pytest").importorskip("fontTools.ttLib")
  font = fontTools.TTFont(icons.FONT)
  cmap, upem = font.getBestCmap(), font["head"].unitsPerEm
  missing = [name for name, (_, codepoint, _) in icons.ICONS.items() if codepoint not in cmap]
  check("every icon is in the font", not missing, missing)
  widths = {font["hmtx"][glyph][0] / upem for glyph in cmap.values()}
  check("all 0.6 em wide: one cell", widths == {round(0.6 * upem) / upem}, widths)
  glyf = font["glyf"]
  inks = [(glyf[g].xMin / upem, glyf[g].xMax / upem) for g in cmap.values() if hasattr(glyf[g], "xMin")]
  check("…and within it (0.57 em: Ubuntu Sans Mono's cell is 0.56)", all(0 <= a and b <= 0.57 for a, b in inks), inks[:3])
