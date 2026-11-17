"""slurp's UI against the lab: directories of the cluster (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_dir_permissions


def test_dir_permissions(ui, check):
  ui(t_dir_permissions, check)
