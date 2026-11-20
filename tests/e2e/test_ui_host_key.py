"""slurp's UI against the lab: a host key that changed (the scenario is in ui_scenarios.py)"""
from ui_scenarios import t_host_key_changed


def test_host_key_changed(ui, check):
  ui(t_host_key_changed, check)
