"""B14 srun in a loop: a loop is for, while, until where a command starts; not those words in a comment ("threads for
the solver"), in a string (echo "wait for it") or as an argument (echo done), which made every line after them "in a
loop" before"""
from slurp.checks import engine as c

HEAD = "#!/bin/bash -l\n#SBATCH --time=01:00:00\n#SBATCH --nodes=1\n"


def b14(body):
  ctx = c.make_context(HEAD + body + "\n", "/users/me/job.sh")
  return [f.line for f in c.run_rules(ctx) if f.rule.id == "B14"]


def test_not_a_loop(check):
  check("for in a comment: not a loop", b14("export OMP_NUM_THREADS=4   # 4 cores per rank: threads for the solver\nsrun ./sim") == [])
  check("…in a string", b14('echo "wait for it, while it runs"\nsrun ./sim') == [])
  check("echo done: not the end of a loop (the next loop is still one)", b14("echo done\nfor i in 1 2; do\n  srun ./sim $i\ndone") == [6])
  check("a comment's # in a string is not a comment", b14('echo "#1"; for i in 1 2; do srun ./sim $i; done') == [4])


def test_loops(check):
  check("for … do / done", b14("for i in 1 2; do\n  srun ./sim $i\ndone\nsrun ./post") == [5])
  check("one line", b14("for i in 1 2; do srun ./sim $i; done") == [4])
  check("while read, after a pipe", b14("seq 3 | while read i; do\n  srun ./sim $i\ndone") == [5])
  check("in if … then", b14("if true; then for i in 1 2; do\n  srun ./sim $i\ndone; fi") == [5])
  check("after the loop: not in it", b14("for i in 1 2; do\n  echo $i\ndone\nsrun ./sim") == [])
