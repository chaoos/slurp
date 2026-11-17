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
  lines = [f"step {i}/100" for i in range(1, 11)]
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
