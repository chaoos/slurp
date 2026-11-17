"""The Submit dialog's array range: what sbatch --array takes, how many tasks, and its option in the command"""
from slurp.ui.screens.dialogs import array_task_count, with_array_spec, without_array


def test_array_ranges(check):
  for spec, n in [("1-100", 100), ("0-9:2", 5), ("1,5,7", 3), ("1-1000%50", 1000), ("3", 1), ("1-3,2-4", 4), ("0-0", 1)]:
    check(f"{spec}: {n} tasks", array_task_count(spec) == n, array_task_count(spec))
  for bad in ("", "1-", "a-b", "5-1", "1-10:0", "1..10", "-3", "1-10%"):
    check(f"{bad!r}: not a range", array_task_count(bad) is None, array_task_count(bad))


def test_the_array_option_of_the_command(check):
  args = ["sbatch", "--array=1-4", "--job-name=x", "-a", "7", "job.sh"]
  check("without --array (both spellings), the rest stays", without_array(args) == ["sbatch", "--job-name=x", "job.sh"], without_array(args))
  check("with another one", with_array_spec(without_array(args), "0-9") == ["sbatch", "--array=0-9", "--job-name=x", "job.sh"])
