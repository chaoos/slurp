"""The progress files of the other tasks of an array, guessed from the file chosen for one task (jobs.progress_pattern)"""
import pytest

from slurp.cluster.jobs import pattern_for, progress_pattern


@pytest.mark.parametrize("path, task, raw, want_dir, want_name", [
  # the index in the name: the directory is shared, its digits are part of the path (pytest-1/, w1/, run1/)
  ("/tmp/pytest-1/lab0/jobs/9500/train_task1_2026-11-13T10-01-07.log", 1, "9500001",
   "/tmp/pytest-1/lab0/jobs/9500", "train_task2_2026-11-14T08-00-00.log"),
  # zero padding, and a time with a 07 is no task 7
  ("/scratch/x/train_task03_2026-11-13T09-59-13.log", 3, "9500003", "/scratch/x", "train_task02_2026-01-01T00-00-00.log"),
  ("/scratch/run1/slurm-9500_1.out", 1, "9500001", "/scratch/run1", "slurm-9500_2.out"),
  # the job id in the name: the directory is shared too
  ("/scratch/pytest-1/slurm-9500001.out", 1, "9500001", "/scratch/pytest-1", "slurm-9500002.out"),
  # a directory per task: only the deepest one with the index is the task's own
  ("/scratch/run3/task_3/out.log", 3, "9500003", "/scratch/run3/task_2", "out.log"),
  ("/scratch/run_9500001/out.log", 1, "9500001", "/scratch/run_9500002", "out.log"),
])
def test_pattern_for_another_task(path, task, raw, want_dir, want_name):
  directory, name = pattern_for(progress_pattern(path, task, raw), 2, "9500002")
  assert directory == want_dir
  assert name.match(want_name)


def test_without_index_or_job_id_nothing_is_guessed():
  assert progress_pattern("/scratch/x/out.log", 1, "9500001") is None


def test_another_tasks_file_does_not_match():
  _, name = pattern_for(progress_pattern("/scratch/x/train_task1_a.log", 1, "9500001"), 2, "9500002")
  assert not name.match("train_task12_a.log") and not name.match("train_task1_a.log")
