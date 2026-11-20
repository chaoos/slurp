"""slurp's UI against the lab: categories, the job list kept for offline use, the status page of a site (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_categories, t_category_rules, t_hidden_categories, t_offline, t_status_page


def test_categories(ui, check):
  ui(t_categories, check)


def test_category_rules(ui, check):
  ui(t_category_rules, check)


def test_hidden_categories(ui, check):
  ui(t_hidden_categories, check)


def test_offline(ui, check):
  ui(t_offline, check)


def test_status_page(ui, check):
  ui(t_status_page, check)
