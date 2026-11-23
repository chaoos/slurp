"""The workers of cluster/remote.py in the Textual app: a widget, a screen or the app is the receiver of a worker
(start_worker(self, ...)); TextualScheduler is the scheduler of cluster/workers.py for it"""
from textual.dom import DOMNode

from ..cluster.remote import register_scheduler
from ..cluster.workers import Scheduler


class TextualScheduler(Scheduler):
  """Workers of a Textual node: Textual's thread workers of the node (its group; exclusive: Textual cancels the
  others), results through app.call_from_thread, only while the node is attached, errors as notifications"""

  def __init__(self, node):
    self.node, self.app = node, node.app

  def submit(self, body, group, exclusive, name):
    return self.node.run_worker(body, thread=True, group=group, exclusive=exclusive, exit_on_error=False, name=name)

  def call_soon(self, fn):
    self.app.call_from_thread(fn)

  def alive(self):
    return self.node is self.app or self.node.is_attached

  def notify_error(self, tab, message):
    self.app.notify(message, title=tab, severity="error", timeout=10)

  def cancel_group(self, group):
    for w in list(self.app.workers):
      if w.node is self.node and w.group == group and hasattr(w, "abort"):
        w.abort.set()


register_scheduler(DOMNode, TextualScheduler)
