"""slurp's UI against the lab: help, About and the views of the windows (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_about, t_view_help, t_term_help, t_pane_help, t_scrollback, t_this_view, t_web_links_everywhere


def test_about(ui, check):
  ui(t_about, check)


def test_view_help(ui, check):
  ui(t_view_help, check)


def test_term_help(ui, check):
  ui(t_term_help, check)


def test_pane_help(ui, check):
  ui(t_pane_help, check)


def test_scrollback(ui, check):
  ui(t_scrollback, check)


def test_this_view(ui, check):
  ui(t_this_view, check)


def test_web_links_everywhere(ui, check):
  ui(t_web_links_everywhere, check)
