"""slurp's UI against the lab: storage: quota and the old files of scratch (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_scratch_exclude, t_quota, t_scratch


def test_scratch_exclude(ui, check):
  ui(t_scratch_exclude, check)


def test_quota(ui, check):
  ui(t_quota, check)


def test_scratch(ui, check):
  ui(t_scratch, check)
