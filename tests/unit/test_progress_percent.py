"""Progress from M% lines (0 to 100, integer or not), where a line has no n/N; n/N wins in a line with both"""
import subprocess

from slurp.cluster import jobcache
from slurp.cluster.jobs import Job, output_progress, progress_tails, parse_progress_tails


def latest(text):
  p = output_progress("/x/out.log", tail=text)
  return None if p is None else (p.current, p.total, p.percent, p.text)


def test_percent_lines(check):
  check("an integer", latest("epoch 1\ntraining 45% done\n") == (45, 100, True, "45%"), latest("training 45% done\n"))
  check("a float, with a space", latest("progress: 12.5 %\n") == (12.5, 100, True, "12.5%"), latest("progress: 12.5 %\n"))
  check("100% and 0%", latest("0%\n100%\n") == (100, 100, True, "100%") and latest("0%\n") == (0, 100, True, "0%"))
  check("over 100 is no progress (CPU 250%)", latest("CPU 250%\n") is None, latest("CPU 250%\n"))
  check("the last one of a line", latest("a 10% b 20%\n")[0] == 20)
  check("a number that goes on (1.5.3%, 1000%) is none", latest("v1.5.3% x\n") is None and latest("1000%\n") is None,
        (latest("v1.5.3% x\n"), latest("1000%\n")))
  check("%j and %x of Slurm names are none", latest("--output=slurm-%j.out %x\n") is None)
  tqdm = " 45%|████▌     | 450/1000 [00:10<00:12, 45.0it/s]\n"
  check("a line with both (tqdm): its n/N", latest(tqdm) == (450, 1000, False, "450/1000"), latest(tqdm))
  check("the latest line wins, whichever kind", latest("3/10\nnow 70%\n") == (70, 100, True, "70%")
        and latest("70%\n8/10\n") == (8, 10, False, "8/10"))
  p = output_progress("/x", tail="2026-11-17 10:00:00 10%\n2026-11-17 10:01:00 20.5%\n")
  check("its points, with the lines' times, for the estimate", [(c, t) for _, c, t in p.points] == [(10, 100), (20.5, 100)]
        and all(t is not None for t, _, _ in p.points), p.points)


def test_the_cluster_sends_the_percent_lines(check, tmp_path):
  f = tmp_path / "out.log"
  f.write_text("starting\nstep 3/10\nloss 0.5\nprogress 42.5%\r progress 43%\nnothing\n")
  chunks, cmds, marker = progress_tails([str(f)])
  res = [subprocess.run(cmd, capture_output=True, text=True) for cmd in cmds]
  tails = parse_progress_tails(chunks, res, marker)
  lines = tails[str(f)].split("\n")
  check("only the n/N and M% lines come back (a \\r ends a line too)", [l for l in lines if l] == ["step 3/10", "progress 42.5%", " progress 43%"], lines)
  check("and give the progress", latest(tails[str(f)]) == (43, 100, True, "43%"))


def test_the_job_cache_keeps_it(check):
  p = output_progress("/x", tail="62.5%\n")
  job = Job(rec={"JobID": "1"}, progress=p)
  back = jobcache.job_from(jobcache.job_data(job)).progress
  check("a percentage stays one (and its fraction)", back.percent and back.current == 62.5 and back.text == "62.5%", back)
