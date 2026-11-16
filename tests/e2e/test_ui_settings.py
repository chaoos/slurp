"""slurp's UI against the lab: copying and the options (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_copy_views, t_options


def test_copy_views(ui, check):
  ui(t_copy_views, check)


def test_options(ui, check):
  ui(t_options, check)
