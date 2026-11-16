"""slurp in a browser (slurp --serve): a web page opens in a new tab, so slurp's page stays; a slurp:// link (no page)
in the same"""
from slurp.ui.app import Slurp


def test_web_pages_in_a_new_tab(check, monkeypatch):
  monkeypatch.setattr(Slurp, "is_web", property(lambda self: True))
  app, opened, done = Slurp(), [], []
  app.open_url = lambda url, new_tab=True: opened.append((url, new_tab))
  for url in ("https://status.example.org/", "http://example.org/x", "slurp://drive?v=1&id=abc"):
    app.open_on_client(url, lambda ok, why: done.append(ok))
  check("http(s) in a new tab, slurp:// in the same", opened == [("https://status.example.org/", True), ("http://example.org/x", True),
                                                               ("slurp://drive?v=1&id=abc", False)], opened)
  check("each reported as opened", done == [True, True, True], done)
