"""Schedulers: how the workers of cluster/remote.py (start_worker, in_background, cluster_work) reach a frontend.

A worker runs blocking work (commands on a cluster, file access) on a thread; its result is delivered on the
frontend's own loop, only if the receiver (a window, a widget) is still there; an error is logged and told to the user.
One Scheduler per receiver; one implementation per frontend: ui/workers.py (Textual), ThreadScheduler below (no
frontend: the headless entry point, tests).

The semantics the implementations keep:
  - exclusive groups: a new worker of a group (exclusive=True) aborts the workers of that group of the same receiver
  - every worker has its own abort event (worker.abort): set, the command it waits for stops (remote.Cancelled)
  - remote.Cancelled (a BaseException) passes `except Exception` on the way and ends the worker quietly
  - an error ends the worker, not the frontend: logged and told (notify_error), unless the worker was aborted
  - a result for a receiver that is gone is dropped silently (deliver)
"""

import queue
import threading

from ..state import log


class Scheduler:
  """Delivers worker results to a frontend, for one receiver"""

  def submit(self, body, group, exclusive, name):
    """Start body() on a thread, in group; returns a worker (remote.start_worker sets worker.abort on it)"""
    raise NotImplementedError

  def call_soon(self, fn):
    """From a worker thread: run fn() on the frontend's loop (waits until it ran)"""
    raise NotImplementedError

  def alive(self):
    """On the frontend's loop: whether the receiver is still there"""
    raise NotImplementedError

  def notify_error(self, tab, message):
    """On the frontend's loop: tell the user (titled with the tab the work was for); remote.py logs it"""
    raise NotImplementedError

  def cancel_group(self, group):
    """Abort the workers of the group of this receiver (their abort events)"""
    raise NotImplementedError

  def deliver(self, fn):
    """From a worker thread: run fn() on the frontend's loop, if the receiver is still there"""
    self.call_soon(lambda: fn() if self.alive() else None)


class Worker:
  """A worker of ThreadScheduler: its thread, its result (None after an error or an abort)"""

  def __init__(self, scheduler, body, group, name):
    self.scheduler, self.group, self.name, self.result = scheduler, group, name, None
    self.abort = threading.Event()
    self.done = threading.Event()
    self.thread = threading.Thread(target=self.run, args=(body,), name=f"slurp-{name}", daemon=True)

  def run(self, body):
    try:
      self.result = body()
    finally:
      self.done.set()
      self.scheduler.finished(self)

  def cancel(self):
    self.abort.set()

  @property
  def is_running(self):
    return not self.done.is_set()

  def wait(self, timeout=None):
    """Block until the worker ended; its result"""
    self.done.wait(timeout)
    return self.result


class ThreadScheduler(Scheduler):
  """A frontend without a UI loop of its own: workers on threads; their results run on the thread that calls pump()
  (the frontend's loop), or, with deliver_inline, right away on the worker's thread (serialised by a lock). Errors
  go to on_error(tab, message) (default: kept in .errors). close() ends the receiver: later results are dropped."""

  def __init__(self, deliver_inline=False, on_error=None):
    self.inline = deliver_inline
    self.on_error = on_error
    self.errors = []
    self.workers = []
    self.open = True
    self.lock = threading.RLock()
    self.queue = queue.Queue()

  def submit(self, body, group, exclusive, name):
    worker = Worker(self, body, group, name)
    with self.lock:
      self.workers.append(worker)
    worker.thread.start()
    return worker

  def finished(self, worker):
    with self.lock:
      if worker in self.workers:
        self.workers.remove(worker)

  def call_soon(self, fn):
    if self.inline:
      with self.lock:
        fn()
      return
    ran = threading.Event()
    def run():
      try:
        fn()
      finally:
        ran.set()
    self.queue.put(run)
    ran.wait()

  def pump(self, timeout=0.0):
    """On the frontend's thread: run the results that came (waits up to timeout for the first); how many ran"""
    n = 0
    try:
      fn = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
      while True:
        try:
          fn()
        except Exception: # (as a UI loop: one failing callback does not stop the others)
          log.exception("a callback of a worker failed")
        n += 1
        fn = self.queue.get_nowait()
    except queue.Empty:
      return n

  def alive(self):
    return self.open

  def close(self):
    self.open = False

  def notify_error(self, tab, message):
    if self.on_error is not None:
      self.on_error(tab, message)
    else:
      self.errors.append((tab, message))

  def cancel_group(self, group):
    with self.lock:
      for w in self.workers:
        if w.group == group:
          w.abort.set()

  def running(self, group=None):
    with self.lock:
      return [w for w in self.workers if group is None or w.group == group]
