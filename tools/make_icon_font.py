"""Make slurp's icon font, src/slurp/resources/fonts/slurp-icons.ttf, from Symbols Nerd Font Mono: only the icons slurp
uses (resources/icons.py), each scaled to fit one cell of a monospace font (0.6 em wide, as DejaVu Sans Mono and Roboto
Mono; narrower ones such as Ubuntu Sans Mono's 0.56 em hold the icon too). Nerd Fonts' own Mono icons are a whole em
wide: a terminal draws them over the next cell or two.

The browser view loads it for the codepoints of the icons; a terminal shows the icons with it when it is installed
(slurp --install-font), as a fallback font: the terminal's own font stays.

Usage: python tools/make_icon_font.py SymbolsNerdFontMono-Regular.ttf
(from https://github.com/ryanoasis/nerd-fonts/releases: NerdFontsSymbolsOnly.zip). Needs fontTools."""

import os
import sys

from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from slurp.resources.icons import ICONS # noqa: E402

OUT = os.path.join(ROOT, "src", "slurp", "resources", "fonts", "slurp-icons.ttf")
ADVANCE = 0.6 # em: one cell
INK = 0.535   # em: the width of an icon (its square), centred in the cell
MIDDLE = 0.36 # em above the baseline: the middle of capitals, where the icon's centre goes
FAMILY = "slurp icons"


def main(source):
  font = TTFont(source)
  options = Options()
  options.notdef_outline = True
  options.name_IDs = []
  options.layout_features = []
  subsetter = Subsetter(options)
  subsetter.populate(unicodes=sorted({codepoint for _, codepoint, _ in ICONS.values()}))
  subsetter.subset(font)

  upem = font["head"].unitsPerEm
  advance = round(ADVANCE * upem)
  glyf, hmtx = font["glyf"], font["hmtx"]
  old_middle = (font["hhea"].ascent + font["hhea"].descent) / 2 # (the icons are centred in the em box)
  for name in font.getGlyphOrder():
    width = hmtx[name][0] or upem
    scale = INK * upem / width
    dx = (advance - width * scale) / 2
    dy = MIDDLE * upem - old_middle * scale
    pen = TTGlyphPen(None)
    glyf[name].draw(TransformPen(pen, (scale, 0, 0, scale, dx, dy)), glyf)
    glyf[name] = pen.glyph()
    glyf[name].recalcBounds(glyf)
    hmtx[name] = (advance, getattr(glyf[name], "xMin", 0))

  font["hhea"].advanceWidthMax = advance
  font["OS/2"].xAvgCharWidth = advance
  font["post"].isFixedPitch = 1
  font["OS/2"].panose.bProportion = 9 # (monospaced)
  names = font["name"]
  for record in list(names.names):
    if record.nameID in (1, 3, 4, 6, 16, 17):
      names.removeNames(nameID=record.nameID)
  for name_id, value in ((1, FAMILY), (2, "Regular"), (3, f"{FAMILY} (from Symbols Nerd Font Mono)"), (4, FAMILY),
                         (6, FAMILY.replace(" ", "-"))):
    names.setName(value, name_id, 3, 1, 0x409)
    names.setName(value, name_id, 1, 0, 0)
  os.makedirs(os.path.dirname(OUT), exist_ok=True)
  font.save(OUT)
  print(f"{OUT}: {len(font.getGlyphOrder()) - 1} icons, {os.path.getsize(OUT)} bytes")


if __name__ == "__main__":
  if len(sys.argv) != 2:
    sys.exit(__doc__)
  main(sys.argv[1])
