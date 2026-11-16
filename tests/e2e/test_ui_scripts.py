"""slurp's UI against the lab: job scripts: the editor, the checks of scripts and jobs (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_chdir, t_script_edit, t_checks_ui, t_checks_more


def test_chdir(ui, check):
  ui(t_chdir, check)


def test_script_edit(ui, check):
  ui(t_script_edit, check)


def test_checks_ui(ui, check):
  ui(t_checks_ui, check)


def test_checks_more(ui, check):
  ui(t_checks_more, check)
