"""slurp's logo, the "prompt meter": a shell prompt whose cursor became a load meter rising (cyan, cyan, amber).

One pixel map on the terminal grid (a pixel is one column wide and half a row high, so a terminal cell shows two:
▀ ▄ █) is the source of every form of it:
- mark(): one line of text for headers, `❯▂▄█`, in the theme's colours ($success, $accent, $warning) or fixed ones;
- half_blocks() / ansi_lines(): the logo in 5 rows of a terminal (help, the lock screen of the web view);
- svg(): the same pixels as an SVG (favicon, the start screen of the web view, README, docs);
- winhelper.ps1 draws the tray icon from the same map (the tests check that it matches)."""

PIXELS = (
  "...............",
  ".g..........aa.",
  ".gg.........aa.",
  "..gg........aa.",
  "...gg....cc.aa.",
  "..gg.....cc.aa.",
  ".gg......cc.aa.",
  ".g....cc.cc.aa.",
  "......cc.cc.aa.",
  "...............",
)
COLORS = {"g": "#38d39f", "c": "#26c6da", "a": "#f5b841"}
THEME = {"g": "$success", "c": "$accent", "a": "$warning"} # (in Textual: the colours of the active theme)
BOX = "#14202b" # the background of the boxed icon
MARK = (("❯", "g"), ("▂▄", "c"), ("█", "a"))
TAGLINE = "Slurm at a glance"
WIDTH, HEIGHT = len(PIXELS[0]), len(PIXELS)

def mark(theme=True):
  """The one-line mark as Textual Content (theme colours) or, with theme=False, as Rich Text (fixed colours)"""
  if theme:
    from textual.content import Content
    return Content.assemble(*((text, f"bold {THEME[k]}") for text, k in MARK))
  from rich.text import Text
  return Text.assemble(*((text, f"bold {COLORS[k]}") for text, k in MARK))

def cells():
  """[(char, top colour key or None, bottom colour key or None)] per row of the terminal (5 rows)"""
  rows = []
  for r in range(0, HEIGHT, 2):
    row = []
    for c in range(WIDTH):
      top = PIXELS[r][c] if PIXELS[r][c] != "." else None
      bottom = PIXELS[r + 1][c] if PIXELS[r + 1][c] != "." else None
      if top is None and bottom is None:
        row.append((" ", None, None))
      elif top == bottom:
        row.append(("█", top, None))
      elif top is None:
        row.append(("▄", bottom, None))
      elif bottom is None:
        row.append(("▀", top, None))
      else:
        row.append(("▀", top, bottom))
    rows.append(row)
  return rows

def half_blocks():
  """The logo as Rich Text lines (fixed colours: it is a picture)"""
  from rich.style import Style
  from rich.text import Text
  lines = []
  for row in cells():
    t = Text()
    for ch, fg, bg in row:
      t.append(ch, Style(color=COLORS[fg] if fg else None, bgcolor=COLORS[bg] if bg else None))
    lines.append(t)
  return lines

def _sgr(hex_colour, ground):
  r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
  return f"{ground};2;{r};{g};{b}"

def ansi_lines():
  """The logo as lines of terminal output (truecolor escape sequences, each line ends with a reset)"""
  out = []
  for row in cells():
    line = ""
    for ch, fg, bg in row:
      if fg is None:
        line += ch
      else:
        line += f"\x1b[{_sgr(COLORS[fg], 38)}" + (f";{_sgr(COLORS[bg], 48)}" if bg else "") + f"m{ch}\x1b[0m"
    out.append(line)
  return out

def svg(size=None, boxed=False):
  """The pixels as an SVG (square: the 15×10 map centred), crisp at any size; boxed: on a dark rounded square"""
  rects = []
  for r, row in enumerate(PIXELS):
    c = 0
    while c < WIDTH:
      k = row[c]
      e = c
      while e < WIDTH and row[e] == k:
        e += 1
      if k != ".":
        rects.append(f'<rect x="{c}" y="{r}" width="{e - c}" height="1" fill="{COLORS[k]}"/>')
      c = e
  pad = (WIDTH - HEIGHT) / 2
  view = f"-1 {-pad - 1:g} {WIDTH + 2} {WIDTH + 2}" if boxed else f"0 {-pad:g} {WIDTH} {WIDTH}"
  box = f'<rect x="-1" y="{-pad - 1:g}" width="{WIDTH + 2}" height="{WIDTH + 2}" rx="3" fill="{BOX}"/>' if boxed else ""
  dims = f' width="{size}" height="{size}"' if size else ""
  return (f'<svg xmlns="http://www.w3.org/2000/svg"{dims} viewBox="{view}" shape-rendering="crispEdges">'
          f'<title>slurp</title>{box}{"".join(rects)}</svg>')

def svg_data_url(boxed=True):
  import urllib.parse
  return "data:image/svg+xml," + urllib.parse.quote(svg(boxed=boxed))

def wordmark_svg():
  """Logo and name side by side (README, docs): the name in a monospace font, as in the terminal"""
  inner = svg(boxed=True).split(">", 1)[1].rsplit("</svg>", 1)[0].replace("<title>slurp</title>", "")
  pad = (WIDTH - HEIGHT) / 2
  return ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="72" viewBox="0 0 300 72">'
          '<title>slurp: Slurm at a glance</title>'
          f'<svg x="0" y="0" width="72" height="72" viewBox="-1 {-pad - 1:g} {WIDTH + 2} {WIDTH + 2}" shape-rendering="crispEdges">{inner}</svg>'
          '<text x="88" y="44" font-family="DejaVu Sans Mono, Menlo, Consolas, monospace" font-size="38" font-weight="700" '
          'fill="#2aa889">slurp</text>'
          f'<text x="89" y="64" font-family="DejaVu Sans, Helvetica, Arial, sans-serif" font-size="13" fill="#7a8a99">{TAGLINE}</text>'
          '</svg>')

def _rgb(hex_colour):
  return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))

def rgba(size, boxed=True):
  """size×size pixels, rows of (r, g, b, a), nearest pixel (crisp), on the dark rounded square or transparent"""
  units = WIDTH + 2 if boxed else WIDTH
  shift = 1 if boxed else 0
  pad = (WIDTH - HEIGHT) / 2
  radius = 3 * size / units
  rows = []
  for y in range(size):
    row = []
    for x in range(size):
      ux = (x + 0.5) * units / size - shift
      uy = (y + 0.5) * units / size - shift - pad
      colour = None
      if 0 <= uy < HEIGHT and 0 <= ux < WIDTH and PIXELS[int(uy)][int(ux)] != ".":
        colour = COLORS[PIXELS[int(uy)][int(ux)]]
      if colour is None and boxed:
        cx = min(max(x + 0.5, radius), size - radius)
        cy = min(max(y + 0.5, radius), size - radius)
        if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= radius ** 2:
          colour = BOX
      row.append((*_rgb(colour), 255) if colour else (0, 0, 0, 0))
    rows.append(row)
  return rows

def png(size, boxed=True):
  """The logo as a PNG of size×size"""
  import struct
  import zlib
  raw = b"".join(b"\0" + bytes(v for px in row for v in px) for row in rgba(size, boxed))
  def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
  return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
          + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

def _dib(size):
  """An icon frame as a 32-bit bitmap (what every Windows icon reader takes): BGRA rows bottom up, an empty mask"""
  import struct
  head = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
  pixels = b"".join(bytes((b, g, r, a)) for row in reversed(rgba(size)) for r, g, b, a in row)
  mask = b"\0" * (((size + 31) // 32) * 4 * size)
  return head + pixels + mask

def ico(sizes=(16, 24, 32, 48, 64, 256)):
  """A Windows icon: bitmaps up to 64, a PNG for 256 (the tray icon of the slurp helper, its start menu entry)"""
  import struct
  frames = [png(s) if s >= 256 else _dib(s) for s in sizes]
  out = struct.pack("<HHH", 0, 1, len(frames))
  offset = 6 + 16 * len(frames)
  for s, data in zip(sizes, frames):
    out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
  return out + b"".join(frames)

DOCS = {"logo.svg": lambda: svg(boxed=True), "logo-plain.svg": lambda: svg(), "logo-wordmark.svg": wordmark_svg,
        "logo-256.png": lambda: png(256), "logo.ico": ico}

def write_docs(folder):
  """The logo files of the repository's docs/images/ (DOCS) from the pixel map: tools/make_logo_files.py"""
  import os
  for name, make in DOCS.items():
    data = make()
    with open(os.path.join(folder, name), "wb") as f:
      f.write(data.encode() if isinstance(data, str) else data)
  return sorted(DOCS)
