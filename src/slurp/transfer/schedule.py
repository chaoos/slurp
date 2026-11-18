"""When a transfer runs: now, once at a time, repeatedly (every so long, or daily at a time), or after a job ended.

A schedule is a dict: {"kind": "now" | "at" | "every" | "daily" | "after_job", "at": epoch, "every": "6h",
"daily": "02:00", "job": job id, "login": log in to the tabs by itself (default yes), "missed": "run" (once, as soon as
it can: the default) or "skip" (to the next time)}. Repeated runs copy only what changed: a repeated transfer keeps
the destination in step with the source."""

import re
import time
from datetime import datetime, timedelta

UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
EVERY = re.compile(r"(\d+)\s*([mhdw])")
GRACE = 120 # seconds late that are not missed

def every_seconds(text):
  """Seconds of "30m", "6h", "1d", "1d12h" (None if it is not one; at least 5 minutes)"""
  text = (text or "").replace(" ", "")
  if not text or not re.fullmatch(rf"(?:{EVERY.pattern})+", text):
    return None
  seconds = sum(int(n) * UNITS[u] for n, u in EVERY.findall(text))
  return seconds if seconds >= 300 else None

def daily_time(text):
  """(hour, minute) of "02:00" (None if it is not one)"""
  m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
  if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
    return None
  return int(m.group(1)), int(m.group(2))

def parse_at(text, now=None):
  """An epoch from "2026-11-18 14:00", "14:00" (today, or tomorrow if it is past), "+2h" (None if it is not one)"""
  text = (text or "").strip()
  now = now or time.time()
  if text.startswith("+"):
    rest = text[1:].replace(" ", "")
    if not re.fullmatch(rf"(?:{EVERY.pattern})+", rest):
      return None
    return now + sum(int(n) * UNITS[u] for n, u in EVERY.findall(rest))
  hm = daily_time(text)
  if hm:
    base = datetime.fromtimestamp(now).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if base.timestamp() <= now:
      base += timedelta(days=1)
    return base.timestamp()
  for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
    try:
      return datetime.strptime(text, fmt).timestamp()
    except ValueError:
      continue
  return None

def first_time(schedule, now=None):
  """When it runs first (None: now, or when its job ends)"""
  now = now or time.time()
  kind = (schedule or {}).get("kind", "now")
  if kind == "at":
    return schedule.get("at")
  if kind == "every":
    return now # (the first run now, the next ones every so long after it)
  if kind == "daily":
    return next_daily(schedule, now)
  return None

def next_daily(schedule, after):
  hm = daily_time(schedule.get("daily"))
  if hm is None:
    return None
  base = datetime.fromtimestamp(after).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
  if base.timestamp() <= after:
    base += timedelta(days=1)
  return base.timestamp()

def after_run(schedule, planned, now=None):
  """When it runs next after the run planned for `planned` started (None: no more runs)"""
  now = now or time.time()
  kind = (schedule or {}).get("kind", "now")
  if kind == "every":
    seconds = every_seconds(schedule.get("every"))
    if not seconds:
      return None
    nxt = (planned or now) + seconds
    while nxt <= now: # (runs missed meanwhile are one run: this one)
      nxt += seconds
    return nxt
  if kind == "daily":
    return next_daily(schedule, max(now, planned or now))
  return None

def catch_up(schedule, planned, now=None):
  """A run planned for `planned` that is due: (run it now?, when the next one is if not). Late by more than a little
  (slurp did not run, a tab was not connected): with missed "skip", the next time instead"""
  now = now or time.time()
  if planned is None or planned > now:
    return False, planned
  if now - planned <= GRACE or (schedule or {}).get("missed", "run") != "skip":
    return True, planned
  return False, after_run(schedule, planned, now) if schedule.get("kind") in ("every", "daily") else None

def repeated(schedule):
  return (schedule or {}).get("kind") in ("every", "daily")

def describe(schedule, next_run=None):
  """In words, for the list and the details"""
  s = schedule or {}
  kind = s.get("kind", "now")
  when = f" (next: {when_text(next_run)})" if next_run else ""
  if kind == "at":
    return f"once at {when_text(s.get('at'))}"
  if kind == "every":
    return f"every {s.get('every')}{when}"
  if kind == "daily":
    return f"daily at {s.get('daily')}{when}"
  if kind == "after_job":
    return f"after job {s.get('job')} ends"
  return "now"

def when_text(epoch):
  if not epoch:
    return "—"
  moment = datetime.fromtimestamp(epoch)
  today = datetime.now().date()
  if moment.date() == today:
    return moment.strftime("%H:%M")
  if moment.date() == today + timedelta(days=1):
    return moment.strftime("tomorrow %H:%M")
  return moment.strftime("%Y-%m-%d %H:%M")
