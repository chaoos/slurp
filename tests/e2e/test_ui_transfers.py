"""slurp's UI against the lab: transfers (the wizard over SSH and the relay, a transfer job, a detached run; the
scenarios are in ui_scenarios.py)"""
from ui_scenarios import t_transfer_detached, t_transfer_job, t_transfer_job_queued, t_transfer_rclone_errors, t_transfer_s3, t_transfer_webdav, t_transfer_wizard


def test_transfer_wizard(ui, check):
  ui(t_transfer_wizard, check)


def test_transfer_job(ui, check):
  ui(t_transfer_job, check)


def test_transfer_detached(ui, check):
  ui(t_transfer_detached, check)


def test_transfer_s3(ui, check):
  ui(t_transfer_s3, check)


def test_transfer_webdav(ui, check):
  ui(t_transfer_webdav, check)


def test_transfer_rclone_errors(ui, check):
  ui(t_transfer_rclone_errors, check)


def test_transfer_job_queued(ui, check):
  ui(t_transfer_job_queued, check)
