"""Rules of the categories (slurp/cluster/categories.py): a pattern of job names puts jobs in a category when the job
list loads, each job once (its "after"); new jobs, those in no category, or all (moved); the first rule that matches
has a job; Apply now; what a rule matches; kept in the file's data; a rename takes them along, a delete removes them"""
from slurp.cluster import categories as c


def rule(rid="r1", pattern="ci-[0-9]+", category="CI", scope="new", after=0, on=True):
  return {"id": rid, "pattern": pattern, "category": category, "scope": scope, "after": after, "on": on}


def test_rule_entry(check):
  check("a rule: kept as given, made its category", c.Store("x").set_rule(rule()) == "r1")
  for bad, why in ((rule(pattern=""), "no pattern"), (rule(pattern="ci-[0-9"), "not a regular expression"), (rule(category=""), "no category"),
                   (rule(scope="some"), "no such scope"), (rule(rid="A;rm"), "a bad id"), ("x", "not a dict")):
    try:
      c.rule_entry(bad)
      check(f"refused: {why}", False)
    except ValueError:
      check(f"refused: {why}", True)
  check("the whole name: ci-12 yes, ci-12-b and my-ci-12 not", c.rule_matches(rule(), "ci-12") and not c.rule_matches(rule(), "ci-12-b")
        and not c.rule_matches(rule(), "my-ci-12"))


def test_new_jobs(check):
  s = c.Store("daint")
  s.set_rule(rule(after=100)) # (made when the newest job of the job list was 100)
  jobs = [("90", "ci-1"), ("100", "ci-2"), ("101", "ci-3"), ("102", "train"), ("103_1", "ci-4"), ("103_2", "ci-4")]
  done = s.apply_rules(jobs)
  check("new jobs only: those above its after; an array as a whole", done == {"CI": ["101", "103"]} and s.category_of("103_2") == "CI"
        and s.category_of("90") == "" and s.category_of("102") == "", done)
  check("…its after moved to the newest job", s.rule("r1")["after"] == 103)
  s.unassign(["101"])
  check("each job once: one taken out by hand is not put back", s.apply_rules(jobs) == {} and s.category_of("101") == "")
  check("nothing new: nothing due (the file not written)", not s.rules_due(jobs) and s.rules_due(jobs + [("104", "x")]))
  check("…hetjob and odd ids: skipped", s.apply_rules([("105+0", "ci-9")]) == {})


def test_scopes_and_order(check):
  s = c.Store("daint")
  s.assign(["1"], "old")
  s.set_rule(rule("a", scope="uncategorized"))
  jobs = [("1", "ci-1"), ("2", "ci-2")]
  check("in no category: the jobs of the job list now, not those in another", s.apply_rules(jobs) == {"CI": ["2"]} and s.category_of("1") == "old")
  s = c.Store("daint")
  s.assign(["1"], "old")
  s.set_rule(rule("a", scope="all"))
  check("all: also moved out of another category", s.apply_rules(jobs) == {"CI": ["1", "2"]} and s.category_of("1") == "CI")
  s = c.Store("daint")
  s.set_rule(rule("a", pattern="ci-.*", category="CI/any", scope="uncategorized"))
  s.set_rule(rule("b", pattern="ci-[0-9]+", category="CI/numbered", scope="uncategorized"))
  check("the first rule that matches has a job", s.apply_rules(jobs) == {"CI/any": ["1", "2"]})
  s = c.Store("daint")
  s.set_rule(rule("a", scope="uncategorized", on=False))
  check("off: nothing, its after stays", s.apply_rules(jobs) == {} and s.rule("a")["after"] == 0)


def test_apply_now_and_counts(check):
  s = c.Store("daint")
  s.set_rule(rule(after=1000))
  s.assign(["1"], "CI")
  s.assign(["2"], "other")
  jobs = [("1", "ci-1"), ("2", "ci-2"), ("3", "ci-3"), ("4_1", "ci-4"), ("4_2", "ci-4"), ("5", "train")]
  count = s.rule_count(s.rule("r1"), jobs)
  check("what it matches: an array once; in it, in none, in another", count == {"match": 4, "there": 1, "none": 2, "other": 1}, count)
  check("Apply now: the ones in no category, whatever it has seen", s.apply_rule("r1", jobs) == ["3", "4"] and s.category_of("2") == "other")
  check("…move: those in another too", s.apply_rule("r1", jobs, move=True) == ["2"] and s.category_of("2") == "CI")
  try:
    s.apply_rule("gone", jobs)
    check("a rule that is gone: said", False)
  except ValueError:
    check("a rule that is gone: said", True)


def test_kept_renamed_deleted(check):
  s = c.Store("daint")
  s.set_rule(rule("a", category="proj/CI"))
  s.set_rule(rule("b", pattern="eval_.*", category="proj/eval"))
  s.set_rule(dict(rule("a", category="proj/CI"), scope="all"))
  check("changed in its place", [r["id"] for r in s.rules] == ["a", "b"] and s.rule("a")["scope"] == "all")
  s2 = c.Store("daint", s.to_data())
  check("round trip through the file's data", s2.to_data() == s.to_data() and s2.rules == s.rules)
  check("bad rules in the file: skipped", c.Store("x", {"rules": [rule(pattern="("), "x", rule("ok")]}).rules == [c.rule_entry(rule("ok"))])
  s.rename("proj", "project")
  check("a rename takes them along", [r["category"] for r in s.rules] == ["project/CI", "project/eval"])
  s.delete("project/CI")
  check("a delete removes the rules of the category", [r["id"] for r in s.rules] == ["b"])
  check("rules_of: of the category itself", s.rules_of("project/eval") == [s.rule("b")] and s.rules_of("project") == [])
  s.drop_rule("b")
  check("drop_rule", s.rules == [] and "rules" not in s.to_data())
  check("as changes by name (kept to be done later, offline)", all(op in c.OPS for op in ("set_rule", "drop_rule", "apply_rules", "apply_rule")))


def test_hidden(check):
  s = c.Store("daint")
  s.assign(["1"], "noise/sub")
  s.hide("noise")
  check("hidden: itself and the categories in it, not others", s.hidden_by("noise") == "noise" and s.hidden_by("noise/sub") == "noise"
        and s.hidden_by("other") == "" and s.hidden_by("") == "")
  check("kept in the file's data", c.Store("daint", s.to_data()).hidden == {"noise"} and s.to_data()["hidden"] == ["noise"])
  s.rename("noise", "archive/noise")
  check("a rename takes it along", s.hidden == {"archive/noise"} and s.hidden_by("archive/noise/sub") == "archive/noise")
  s.hide("archive/noise", False)
  check("shown again", s.hidden == set() and "hidden" not in s.to_data())
  s.hide("archive/noise")
  s.delete("archive")
  check("a delete removes it", s.hidden == set())
  check("as a change by name (offline)", "hide" in c.OPS)
