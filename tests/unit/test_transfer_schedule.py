"""Transfers: when a schedule runs next, runs that were missed (run once, or skip to the next time), the words for it"""
from datetime import datetime

from slurp.transfer import schedule as S

NOW = datetime(2026, 11, 18, 10, 30).timestamp()


def at(h, m, day=18):
  return datetime(2026, 11, day, h, m).timestamp()


def test_parsing(check):
  check("every: 6h, 1d12h, 30m", S.every_seconds("6h") == 21600 and S.every_seconds("1d12h") == 129600 and S.every_seconds("30m") == 1800)
  check("every: too short or nonsense", S.every_seconds("1m") is None and S.every_seconds("often") is None and S.every_seconds("") is None)
  check("daily", S.daily_time("02:00") == (2, 0) and S.daily_time("25:00") is None and S.daily_time("x") is None)
  check("at: a date and time", S.parse_at("2026-11-20 14:00", NOW) == at(14, 0, 20))
  check("at: a time today", S.parse_at("14:00", NOW) == at(14, 0))
  check("at: a time that is past: tomorrow", S.parse_at("09:00", NOW) == at(9, 0, 19))
  check("at: in so long", S.parse_at("+2h", NOW) == NOW + 7200)
  check("at: nonsense", S.parse_at("soon", NOW) is None)


def test_next(check):
  every = {"kind": "every", "every": "6h"}
  check("every: the first now", S.first_time(every, NOW) == NOW)
  check("every: the next one after the planned one", S.after_run(every, NOW, NOW + 10) == NOW + 21600)
  check("every: runs missed meanwhile are one", S.after_run(every, NOW, NOW + 3 * 21600 + 5) == NOW + 4 * 21600)
  daily = {"kind": "daily", "daily": "02:00"}
  check("daily: the next 02:00", S.first_time(daily, NOW) == at(2, 0, 19))
  check("daily: after a run, the next day", S.after_run(daily, at(2, 0, 19), at(2, 5, 19)) == at(2, 0, 20))
  check("once: no more", S.after_run({"kind": "at", "at": NOW}, NOW) is None and S.first_time({"kind": "at", "at": NOW + 5}) == NOW + 5)
  check("now and after a job: not by time", S.first_time({"kind": "now"}) is None and S.first_time({"kind": "after_job", "job": "1"}) is None)


def test_missed(check):
  daily = {"kind": "daily", "daily": "02:00"}
  check("not yet due", S.catch_up(daily, NOW + 60, NOW) == (False, NOW + 60))
  check("just due: runs", S.catch_up(daily, NOW - 30, NOW) == (True, NOW - 30))
  check("missed by hours: runs once (the default)", S.catch_up(daily, NOW - 8 * 3600, NOW)[0])
  skip = dict(daily, missed="skip")
  run, nxt = S.catch_up(skip, at(2, 0), NOW)
  check("missed with skip: the next time instead", not run and nxt == at(2, 0, 19), (run, nxt))
  check("a single one missed with skip: none", S.catch_up({"kind": "at", "at": NOW - 9999, "missed": "skip"}, NOW - 9999, NOW) == (False, None))


def test_words(check):
  check("now", S.describe({}) == "now")
  check("every", S.describe({"kind": "every", "every": "6h"}, at(16, 30)) == "every 6h (next: 16:30)")
  check("daily", S.describe({"kind": "daily", "daily": "02:00"}).startswith("daily at 02:00"))
  check("after a job", S.describe({"kind": "after_job", "job": "123"}) == "after job 123 ends")
  check("repeated", S.repeated({"kind": "daily"}) and not S.repeated({"kind": "at"}))
