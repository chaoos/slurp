"""slurp's logo: one pixel map, every form of it consistent; the files in docs/images/ current"""
import os
import re
import struct
import xml.dom.minidom
from slurp.ui import logo
from slurp.links.windows import winhelper

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_logo(check, tmp_path, monkeypatch):
  check("the map: 15×10, known colours", len(logo.PIXELS) == 10 and all(len(r) == 15 and set(r) <= set(".gca") for r in logo.PIXELS))
  lines = logo.half_blocks()
  check("half blocks: 5 rows of 15 cells", len(lines) == 5 and all(l.cell_len == 15 for l in lines))
  plain = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in logo.ansi_lines()]
  check("ANSI lines: the same characters", plain == [l.plain for l in lines], plain)
  check("one-line mark", logo.mark(theme=False).plain == "❯▂▄█" and str(logo.mark()) == "❯▂▄█")
  for name in ("svg", "boxed", "wordmark"):
    text = logo.wordmark_svg() if name == "wordmark" else logo.svg(boxed=(name == "boxed"))
    try: xml.dom.minidom.parseString(text); ok = True
    except Exception: ok = False
    check(f"{name}: valid SVG", ok)
  data = logo.ico()
  n = struct.unpack("<HHH", data[:6])[2]
  entries = [struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i]) for i in range(n)]
  check("ico: 6 frames, each inside the file", n == 6 and all(off + size <= len(data) for *_, size, off in entries))
  check("ico: bitmaps up to 64, PNG at 256", all((data[off:off + 4] == b"\x89PNG") == (w == 0) for w, _, _, _, _, _, size, off in entries))
  docs = os.path.join(REPO, "docs", "images")
  stale = [name for name, make in logo.DOCS.items()
           if open(os.path.join(docs, name), "rb").read() != (lambda d: d.encode() if isinstance(d, str) else d)(make())]
  check("docs/images/ logo files are current (python tools/make_logo_files.py)", not stale, stale)
  check("README and the docs' home page show the wordmark", 'src="docs/images/logo-wordmark.svg"' in open(os.path.join(REPO, "README.md")).read()
        and 'src="images/logo-wordmark.svg"' in open(os.path.join(REPO, "docs", "index.md")).read())
  setup = winhelper.setup_script()
  check("the helper's setup carries the icon, the tray loads it", "$IconData = '" in setup and "slurp.ico" in winhelper.SCRIPT.read_text())
