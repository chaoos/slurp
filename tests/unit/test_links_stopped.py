"""Links and drives ended at the end: their processes are waited for while the event loop still runs (a process still
running when its loop closes is cleaned up later without it: "Event loop is closed", as on CI)"""
import asyncio
import gc
import subprocess
import sys

from slurp.links import share as sh


def test_stopped_waits_for_the_processes(check):
  errors = []
  hook, sys.unraisablehook = sys.unraisablehook, lambda u: errors.append(u.exc_value)
  try:
    mgr = sh.ShareManager(lambda *a: None)
    link = sh.Link("shell", "x", "t", lambda: [], relay="ssh-j.com")
    mgr.links.append(link)
    procs = []
    async def main():
      # (a tunnel that runs on: its stderr read as the relay's is; one that does not end on SIGTERM: killed)
      link.tunnel = await asyncio.create_subprocess_exec("sleep", "30", stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
      stubborn = await asyncio.create_subprocess_exec("sh", "-c", "trap '' TERM; sleep 30", stdin=subprocess.DEVNULL)
      await asyncio.sleep(0.2) # (its trap set)
      link.sessions.append(sh.Session(link, "s1", proc=stubborn))
      procs.extend([link.tunnel, stubborn])
      await mgr.stopped(timeout=1)
    asyncio.run(main())
    check("the tunnel and the session's process are gone (the stubborn one killed)", all(p.returncode is not None for p in procs),
          [p.returncode for p in procs])
    del procs[:], link.sessions[:]
    link.tunnel = None
    gc.collect()
    check("no 'Event loop is closed' when they are cleaned up", not errors, errors)
  finally:
    sys.unraisablehook = hook
