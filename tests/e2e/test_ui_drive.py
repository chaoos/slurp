"""slurp's UI against the lab: the login directory as a drive on this machine (sshfs) (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_drive


def test_drive(ui, check):
  ui(t_drive, check)
