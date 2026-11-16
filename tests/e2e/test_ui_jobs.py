"""slurp's UI against the lab: the job list and the job window: memory, progress, restart, subtabs, sorting, accounts (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import (
  t_memory_watch, t_memory, t_since, t_restart_env, t_subtabs, t_progress, t_ij_fields, t_sort, t_accounts, t_zebra, t_restart, t_interactive,
)


def test_memory_watch(ui, check):
  ui(t_memory_watch, check)


def test_memory(ui, check):
  ui(t_memory, check)


def test_since(ui, check):
  ui(t_since, check)


def test_restart_env(ui, check):
  ui(t_restart_env, check)


def test_subtabs(ui, check):
  ui(t_subtabs, check)


def test_progress(ui, check):
  ui(t_progress, check)


def test_ij_fields(ui, check):
  ui(t_ij_fields, check)


def test_sort(ui, check):
  ui(t_sort, check)


def test_accounts(ui, check):
  ui(t_accounts, check)


def test_zebra(ui, check):
  ui(t_zebra, check)


def test_restart(ui, check):
  ui(t_restart, check)


def test_interactive(ui, check):
  ui(t_interactive, check)
