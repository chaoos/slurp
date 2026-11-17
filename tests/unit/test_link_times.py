"""When a link started and how long it is still valid (its time limit, its relay's hour), in words"""
import time

from slurp.links import share as sh
from slurp.ui.screens.links import link_times


def link(**kw):
  l = sh.Link("shell", "shell on daint", "daint", lambda: [], relay="ssh-j.com", **kw)
  return l


def test_started_and_valid(check):
  now = time.time()
  l = link(); l.created, l.expiry = now - 600, 600 + 41 * 60 + 23
  started, valid = link_times(l, now)
  check("started: the time of day and how long ago", started.plain.endswith("(10:00 ago)") and ":" in started.plain[:5], started.plain)
  check("valid: how long more, until when; green", valid.plain.startswith("41:23 more, until ") and valid.style == "green", valid.plain)
  l.expiry = 600 + 4 * 60
  check("the last five minutes: yellow", link_times(l, now)[1].style == "bold yellow")
  l.expiry = None
  check("no time limit: until slurp quits", link_times(l, now)[1].plain == "until slurp quits")
  l.relay_state, l.relay_until = "ready", now + 30 * 60
  check("a relay with an hour: its end, then a new address", link_times(l, now)[1].plain.startswith("relay until ")
        and "then a new address; the link itself until slurp quits" in link_times(l, now)[1].plain, link_times(l, now)[1].plain)
  l.expiry = 600 + 2 * 3600
  text = link_times(l, now)[1].plain
  check("both: the sooner first, then the other", text.startswith("relay until") and " · 2:00:00 more, until" in text, text)
  l.revoked = True
  check("done: no more logins, why", link_times(l, now)[1].plain == "no more logins: revoked")
  old = link(); old.created = now - 3 * 86400
  check("not today: with the date", len(link_times(old, now)[0].plain.split(" (")[0]) == 16, link_times(old, now)[0].plain)
