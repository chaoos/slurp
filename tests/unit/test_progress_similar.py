"""Only lines like the progress are progress: a "1/1" of another structure in the log is not; the structure is kept
from read to read; a new structure that repeats takes over (another phase), a lone one does not. The current pace from
the times slurp saw new values, read after read"""
import types

from slurp.cluster import jobs as J


def job(jid):
  J.PROGRESS.pop((J.tab_name(), jid), None)
  return types.SimpleNamespace(jid=jid, rec={"Start": "2026-11-18T10:00:00"})


def steps(a, b):
  return [f"[10:{m:02d}:00] epoch 3 step {m}/100 loss 0.{m}" for m in range(a, b)]


def test_lines_like_the_progress(check):
  tail = "\n".join(steps(1, 6) + ["checkpoint 1/1 saved", "copying files 3/7"])
  p = J.output_progress("x", tail=tail)
  check("a lone line of another structure is not progress: the last step", p.current == 5 and p.total == 100, p and p.text)
  check("its points: only the steps", [c for _, c, _ in p.points] == [1, 2, 3, 4, 5], p.points)
  check("tqdm: a bar that grows is the same structure", J.similar(J.line_shape("45%|████▌     | 450/1000 [00:10<00:12]"),
                                                                   J.line_shape("46%|█████▏    | 460/1000 [00:11<00:11]")))
  check("unlike lines are not", not J.similar(J.line_shape("[10:01:00] epoch 3 step 4/100 loss 0.4"), J.line_shape("checkpoint 1/1 saved")))
  only = J.output_progress("x", tail="building 2/9\n")
  check("one line alone, nothing else known: it", only.current == 2 and only.total == 9)


def test_kept_from_read_to_read(check):
  j = job("501")
  J.record_progress(j, J.output_progress("x", tail="\n".join(steps(1, 6)), job=j))
  p = J.output_progress("x", tail="\n".join(steps(6, 8) + ["checkpoint 1/1 saved"]), job=j)
  check("the next read: like the progress so far", p.current == 7, p and p.text)
  p = J.output_progress("x", tail="checkpoint 1/1 saved\nnoise\n", job=j)
  check("only a lone unlike line in the tail: no progress (not 1/1)", p is None, p and p.text)
  p = J.output_progress("x", tail="\n".join(f"phase 2: block {i}/40 done" for i in range(1, 4)), job=j)
  check("a new structure that repeats: it takes over (another phase)", p.current == 3 and p.total == 40, p and p.text)
  J.record_progress(j, p)
  check("…kept for the next reads, the times of the old phase gone", "block" in J.progress_state("501")["shape"]
        and [c for _, c, _ in J.progress_state("501")["points"]] == [3], J.progress_state("501"))


def test_the_current_pace_from_the_reads(check, monkeypatch):
  """No times in the lines: every read notes when it saw a new value (to the ms); the pace from these, after two reads"""
  j = job("502")
  clock = [1000.0]
  monkeypatch.setattr(J.time, "time", lambda: clock[0])
  for read, n in enumerate((10, 15, 20, 25)): # (every 10 s, 5 steps more; a 1/1 in between)
    clock[0] = 1000.0 + 10 * read + 0.123
    tail = "\n".join([f"step {i}/100" for i in range(1, n + 1)] + ["checkpoint 1/1 saved"])
    p = J.record_progress(j, J.output_progress("x", tail=tail, job=j))
    if read == 0:
      check("one read: no current pace yet", "recent" not in p.estimate, p.estimate)
  check("the times seen, to the ms", [round(t % 1, 3) for t, _, _ in J.progress_state("502")["points"]] == [0.123] * 4)
  rate = p.estimate["recent"][0]
  check("after the reads: 5 steps per 10 s, the 1/1 not counted", abs(rate - 0.5) < 1e-9 and p.current == 25 and p.estimate["source"] == "seen at reloads",
        (rate, p.text, p.estimate))


def test_n_stays_the_same(check):
  """N is part of the progress: a line like it with another N is not, unless it repeats after it (another phase)"""
  j = job("503")
  old = [f"step {i}/100" for i in range(1, 9)]
  p = J.record_progress(j, J.output_progress("x", tail="\n".join(old + ["step 1/50"]), job=j))
  check("the same structure, a lone other N: not progress", p.current == 8 and p.total == 100, p.text)
  p = J.record_progress(j, J.output_progress("x", tail="\n".join(old + ["step 9/100", "step 1/50", "step 10/100"]), job=j))
  check("…also between lines of the progress", p.current == 10 and p.total == 100, p.text)
  p = J.record_progress(j, J.output_progress("x", tail="\n".join(old + ["step 1/50", "step 2/50"]), job=j))
  check("a new N that repeats after the progress so far: another phase, it takes over", p.current == 2 and p.total == 50, p.text)
  check("…its points only; the times seen of the old phase gone", {t for _, _, t in p.points} == {50}
        and [c for _, c, _ in J.progress_state("503")["points"]] == [2], (p.points, J.progress_state("503")["points"]))
  p = J.output_progress("x", tail="\n".join(old + ["step 1/50", "step 2/50"]))
  check("the first read too: the latest kind if it repeats, not the one most lines have", p.total == 50 and p.current == 2, p.text)
  p = J.output_progress("x", tail="\n".join(old + ["step 1/50"]))
  check("…a lone latest one: the kind most lines have", p.total == 100 and p.current == 8, p.text)


def test_the_pace_keeps_its_n(check, monkeypatch):
  """The current pace compares values of the same N only (the same kind of progress)"""
  j = job("504")
  clock = [2000.0]
  monkeypatch.setattr(J.time, "time", lambda: clock[0])
  for read, (n, stray) in enumerate(((10, ""), (15, "step 3/7"), (20, ""))):
    clock[0] = 2000.0 + 10 * read
    tail = "\n".join([f"step {i}/100" for i in range(1, n + 1)] + ([stray] if stray else []))
    p = J.record_progress(j, J.output_progress("x", tail=tail, job=j))
  check("a stray other N between the reads: not counted, 0.5/s", abs(p.estimate["recent"][0] - 0.5) < 1e-9 and p.total == 100, (p.text, p.estimate))


def test_a_latest_line_alike_but_not_the_same(check):
  """The latest line alike in structure to the ones before, not the same (a word more): its kind is the one of those
  before (it was missing from the counts: a KeyError, "progress failed: ('[#-#-#T#] Evaluating inversion …', 1024)")"""
  line = "[2026-11-23T10:00:{s:02d}] Evaluating inversion (type=multigrid-coarse/L2,src=wall,sidx=3) {n}/1024"
  tail = "\n".join([line.format(s=1, n=5), line.format(s=2, n=6), line.format(s=3, n=7) + " done"])
  try:
    p = J.output_progress("x", tail=tail)
  except KeyError as e:
    p, error = None, e
  else:
    error = None
  check("no error; the progress: the latest line", error is None and p and p.current == 7 and p.total == 1024, (error, p and p.text))
