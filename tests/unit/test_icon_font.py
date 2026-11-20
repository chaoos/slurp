"""slurp's icon font: every icon is in it; an installed copy of an older slurp is made the same as this one's (icons
added since were boxes in the terminal), one that is the same or none installed: left alone"""
from slurp.ui import icons


def test_every_icon_in_the_font(check):
  from fontTools.ttLib import TTFont
  cmap = TTFont(icons.FONT).getBestCmap()
  missing = sorted(name for name, (_, cp, _) in icons.ICONS.items() if cp not in cmap)
  check("every icon of the Nerd Font set is in slurp's font (tools/make_icon_font.py makes it)", not missing, missing)


def test_installed_copy_updated(check, tmp_path, monkeypatch):
  monkeypatch.setattr("sys.platform", "linux")
  monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
  check("none installed: nothing", icons.update_installed_font() is None and not (tmp_path / "fonts").exists())
  (tmp_path / "fonts").mkdir()
  installed = tmp_path / "fonts" / "slurp-icons.ttf"
  installed.write_bytes(b"an older font")
  path = icons.update_installed_font()
  check("an older copy: made this slurp's", path == str(installed) and installed.read_bytes() == open(icons.FONT, "rb").read(), path)
  check("the same: left alone", icons.update_installed_font() is None)
