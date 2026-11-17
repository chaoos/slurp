"""Why a job was cancelled: kept with the categories (per job, an array as a whole), the jobs that look like it
proposed (a duplicate, the one it depends on), and how it reads in the job window and in a table"""
import types

from slurp.cluster import categories as c
from slurp.ui.screens.cancel import LABELS, REASONS, related_jobs, reason_text, short_reason


def job(jid, name="run", workdir="/scratch/me/a", command="/scratch/me/a/job.sh", state="RUNNING", submit="2026-11-17T10:00:00"):
  return types.SimpleNamespace(jid=jid, rec={"JobName": name, "WorkDir": workdir, "Submit": submit}, command=command,
                               state=state, queued=state == "PENDING", running=state == "RUNNING")


def test_kept(check):
  s = c.Store("daint")
  c.apply_op(s, ["cancel_reason", "4711", {"reason": "duplicate", "label": LABELS["duplicate"], "related": "4710", "at": "2026-11-17 10:00"}])
  c.apply_op(s, ["cancel_reason", "9600", {"reason": "hung", "label": LABELS["hung"], "what": "the pending tasks"}])
  check("a job's", s.cancel_reason_of("4711")["related"] == "4710" and s.cancel_reason_of("4712") is None)
  check("an array's: its tasks too", s.cancel_reason_of("9600_3")["reason"] == "hung")
  data = s.to_data()
  check("in the file, read back", c.Store("daint", data).cancelled == s.cancelled and list(data["cancelled"]) == ["4711", "9600"], data)
  for bad in ({"reason": ""}, "hung", {"reason": "x", "related": "; rm -rf"}):
    try:
      entry = c.cancel_entry("1", bad)
      check(f"refused or cleaned: {bad!r}", entry.get("related", "") == "", entry)
    except ValueError:
      check(f"refused: {bad!r}", True)
  c.apply_op(s, ["cancel_reason", "4711", None])
  check("None: not said any more", "4711" not in s.cancelled)


def test_related_jobs(check):
  me = job("100")
  jobs = [me, job("101"), job("102", name="other", workdir="/x", command="/x/y"), job("103", state="COMPLETED"),
          job("104", workdir="/elsewhere", command="/elsewhere/j.sh"), job("100_1"), job("105_2"), job("105_3")]
  got = [j.jid for j in related_jobs(me, jobs)]
  check("the same name, directory and command first, busy before ended; not itself, not its own tasks; an array once",
        got[:2] == ["101", "105_2"] and "103" in got and "102" not in got and "100_1" not in got and got.count("105_3") == 0, got)
  check("one alike only in its name: later", got.index("104") > got.index("103"), got)


def test_how_it_reads(check):
  entry = {"reason": "duplicate", "label": LABELS["duplicate"], "text": "pressed twice", "related": "4710", "at": "2026-11-17 10:00"}
  text = reason_text(entry)
  check("the job window: the reason, the details, the job it duplicates as a link", text.plain.startswith("Submitted twice by mistake: pressed twice")
        and "the same as job 4710" in text.plain and any(s.style.meta.get("@click") == "app.open_job('4710')" for s in text.spans if hasattr(s.style, "meta")), text.plain)
  dep = reason_text({"reason": "dependency", "label": LABELS["dependency"]})
  check("a dependency: the guide, a link", "dependencies in the guides" in dep.plain
        and any("app.guides" in (s.style.meta.get("@click") or "") for s in dep.spans if hasattr(s.style, "meta")), dep.plain)
  check("your own words", reason_text({"reason": "other", "text": "the node smelled of smoke"}).plain == "the node smelled of smoke")
  check("a table: one short line", short_reason(entry) == "Submitted twice by mistake: pressed twice (4710)" and len(short_reason({"reason": "other", "text": "x" * 200})) == 60)
  keys = [k for k, *_ in REASONS]
  check("the list: the asked for ones, and others, and one of your own", {"duplicate", "hung", "allocation", "account", "dependency", "other"} <= set(keys)
        and len(keys) >= 12 and keys[-1] == "other", keys)
