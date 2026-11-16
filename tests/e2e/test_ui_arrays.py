"""slurp's UI against the lab: job arrays (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import (
  t_lazy, t_array_reload, t_array_done, t_array_window, t_collapse, t_array_progress, t_array_sort, t_array,
)


def test_lazy(ui, check):
  ui(t_lazy, check)


def test_array_reload(ui, check):
  ui(t_array_reload, check)


def test_array_done(ui, check):
  ui(t_array_done, check)


def test_array_window(ui, check):
  ui(t_array_window, check)


def test_collapse(ui, check):
  ui(t_collapse, check)


def test_array_progress(ui, check):
  ui(t_array_progress, check)


def test_array_sort(ui, check):
  ui(t_array_sort, check)


def test_array(ui, check):
  ui(t_array, check)
