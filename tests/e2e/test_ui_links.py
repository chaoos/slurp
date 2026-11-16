"""slurp's UI against the lab: links: shells and web views on your own machine, their relays, Cloudflare (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_web_access, t_links_scroll, t_links_health, t_cloudflare_ask, t_web_cloudflare, t_links


def test_web_access(ui, check):
  ui(t_web_access, check)


def test_links_scroll(ui, check):
  ui(t_links_scroll, check)


def test_links_health(ui, check):
  ui(t_links_health, check)


def test_cloudflare_ask(ui, check):
  ui(t_cloudflare_ask, check)


def test_web_cloudflare(ui, check):
  ui(t_web_cloudflare, check)


def test_links(ui, check):
  ui(t_links, check)
