"""slurp's UI against the lab: the connection: its health, the cluster window, tabs, reconnecting, closing tabs (last) (the scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_conn_health, t_border, t_ci_noconn, t_cluster_info, t_titles, t_reconnect, t_connect_button, t_close


def test_conn_health(ui, check):
  ui(t_conn_health, check)


def test_border(ui, check):
  ui(t_border, check)


def test_ci_noconn(ui, check):
  ui(t_ci_noconn, check)


def test_cluster_info(ui, check):
  ui(t_cluster_info, check)


def test_titles(ui, check):
  ui(t_titles, check)


def test_reconnect(ui, check):
  ui(t_reconnect, check)


def test_connect_button(ui, check):
  ui(t_connect_button, check)


def test_close(ui, check):
  ui(t_close, check)
