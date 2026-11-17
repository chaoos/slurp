"""slurp's UI against the lab: adding a tab with full SSH settings, the check of a connection (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_pre_login, t_ssh_edit, t_ssh_setup


def test_ssh_setup(ui, check):
  ui(t_ssh_setup, check)


def test_ssh_edit(ui, check):
  ui(t_ssh_edit, check)


def test_pre_login(ui, check):
  ui(t_pre_login, check)
