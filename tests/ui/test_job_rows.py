"""The rows of the job list have a cell for each column shown: a job and a grouped array, with and without the
progress columns (a cell too many is a ValueError of the table, at the start of the app)"""
from slurp.cluster.jobs import Job
from slurp.ui.app import Slurp
from slurp.ui.screens.main import COLUMNS


def task(index, state="RUNNING"):
  return Job(rec={"JobID": f"100_{index}", "JobIDRaw": f"10{index}", "JobName": "sweep", "User": "me", "State": state,
                  "NNodes": "1", "Elapsed": "00:01:00", "Timelimit": "01:00:00", "Start": "2026-11-16T10:00:00",
                  "End": "Unknown", "Submit": "2026-11-16T09:59:00", "WorkDir": "/tmp"})


def test_a_cell_for_each_column(check):
  app = Slurp()
  cluster = app.cluster
  tasks = [task(1), task(2, "PENDING")]
  for keys in ([k for k, _ in COLUMNS if k not in ("user", "category")],
               [k for k, _ in COLUMNS if k not in ("user", "category", "progress", "eta_now")]):
    app.job_column_keys = keys
    row = app.group_row("100", tasks, cluster)
    check(f"a grouped array: one cell per column ({len(keys)} columns)", len(row) == len(keys), (len(row), keys))
