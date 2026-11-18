"""slurp's UI against the lab: directories of the cluster (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_dir_permissions, t_shell_here


def test_dir_permissions(ui, check):
  ui(t_dir_permissions, check)


def test_shell_here(ui, check):
  ui(t_shell_here, check)
