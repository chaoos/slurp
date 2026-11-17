"""The Submit dialog's array range: what sbatch --array takes, how many tasks, and its option in the command"""
from slurp.ui.screens.dialogs import array_ids_text, array_task_count, with_array_spec, without_array


def test_array_ranges(check):
  for spec, n in [("1-100", 100), ("0-9:2", 5), ("1,5,7", 3), ("1-1000%50", 1000), ("3", 1), ("1-3,2-4", 4), ("0-0", 1)]:
    check(f"{spec}: {n} tasks", array_task_count(spec) == n, array_task_count(spec))
  for bad in ("", "1-", "a-b", "5-1", "1-10:0", "1..10", "-3", "1-10%"):
    check(f"{bad!r}: not a range", array_task_count(bad) is None, array_task_count(bad))


def test_the_first_and_last_id(check):
  ids, off = array_ids_text("1-100")
  check("1-100: first 1, last 100, nothing to warn of", "first 1, last 100" in ids and not off, (ids, off))
  ids, off = array_ids_text("1-100:2")
  check("1-100:2: the last is 99, said so", ids == "ids 1, 3, 5, …, 99 (first 1, last 99)" and "the last id is 99, not 100" in off, (ids, off))
  ids, off = array_ids_text("0-9:4%2")
  check("0-9:4: 0, 4, 8, the last is 8", ids == "ids 0, 4, 8 (first 0, last 8)" and "not 9" in off, (ids, off))
  check("one id", array_ids_text("5") == ("id 5", ""))
  ids, off = array_ids_text("1-10:3,20-30:5")
  check("two ranges that reach their ends: the first and the last of all, no warning", "first 1, last 30" in ids and not off, (ids, off))
  ids, off = array_ids_text("1-9:3,20-30:5")
  check("1-9:3 ends at 7", "1-9:3: the last id is 7, not 9" in off and "20-30" not in off, off)
  check("huge ranges: no set of all ids", array_task_count("1-1000000000:7") == 142857143 and "999999995" in array_ids_text("1-1000000000:7")[0])


def test_the_array_option_of_the_command(check):
  args = ["sbatch", "--array=1-4", "--job-name=x", "-a", "7", "job.sh"]
  check("without --array (both spellings), the rest stays", without_array(args) == ["sbatch", "--job-name=x", "job.sh"], without_array(args))
  check("with another one", with_array_spec(without_array(args), "0-9") == ["sbatch", "--array=0-9", "--job-name=x", "job.sh"])
