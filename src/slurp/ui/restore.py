"""The subtabs of a tab, kept for the next start: what each one shows as a small description (a job, an array, a file
or directory, the categories, the guides, the rule catalogue; shells and windows of the moment are not kept), saved
with the tabs (app.save_tabs), and opened again, in order, once the tab is connected and its job list loaded"""

from ..state import log

RESTORE_WAIT = 15 # seconds a subtab may take to open (a file looked at on the cluster) before the next one is opened


def root_screen(app, view):
  """The first window of a subtab (what it was opened with), None if it has none (yet)"""
  stack = (getattr(app, "_screen_stacks", {}) or {}).get(view.mode) or []
  return stack[0] if stack else None


def view_spec(app, view):
  """What a subtab shows, to open it again: {"type": …} or None (a shell, a window of the moment); remembered with the
  subtab (at the end its windows may be gone already)"""
  spec = describe(app, view)
  if spec is not None:
    view.kept = spec
  return spec if spec is not None or root_screen(app, view) is not None else getattr(view, "kept", None)


def describe(app, view):
  from .screens.array import ArrayScreen
  from .screens.categories import NONE, CategoriesScreen
  from .screens.checks import GuidesScreen, RuleCatalogueScreen
  from .screens.files import FileScreen
  from .screens.job import JobScreen
  if view.session is not None: # (a shell: it ended with slurp)
    return None
  screen = root_screen(app, view)
  if screen is None:
    return None
  if isinstance(screen, JobScreen):
    return {"type": "job", "jid": screen.job.jid}
  if isinstance(screen, ArrayScreen):
    return {"type": "array", "id": str(screen.array_id)}
  if isinstance(screen, CategoriesScreen):
    return {"type": "categories", "key": view.key, "path": "" if screen.path == NONE else screen.path}
  if isinstance(screen, RuleCatalogueScreen):
    return {"type": "rules", "rule": getattr(screen, "rule_id", "") or ""}
  if isinstance(screen, GuidesScreen):
    return {"type": "guides"}
  path = getattr(screen, "path", None)
  if isinstance(path, str) and path and view.key.startswith("path "): # (a file or directory: a subtab of its own)
    crumbs = [c for c in (getattr(screen, "crumbs", None) or [])[:-1] if isinstance(c, str)] or ["Jobs"]
    spec = {"type": "path", "path": path, "crumbs": crumbs}
    if isinstance(screen, FileScreen):
      spec.update(follow=bool(screen.following), at_end=not screen.from_top)
    return spec
  return None


def tab_views(app, cluster):
  """The subtabs of a tab to keep: {"views": [spec], "shown": "jobs", "overview" or the index of a view,
  "jobs_open"}; those not opened again yet (not connected since the start) are kept as they were"""
  pending = getattr(cluster, "pending_views", None)
  if pending is not None and not cluster.views:
    return pending
  specs, shown = [], "overview"
  for view in cluster.views:
    spec = view_spec(app, view)
    if spec is not None:
      if cluster.current_view == view.mode:
        shown = len(specs)
      specs.append(spec)
  if cluster.current_view == "_default":
    shown = "jobs"
  return {"views": specs, "shown": shown, "jobs_open": bool(cluster.jobs_open)}


def ready(app, cluster):
  """Can the subtabs of a tab be opened again now: it is shown, connected, its job list loaded, no dialog open"""
  from textual.screen import ModalScreen
  return (cluster is app.cluster and ((cluster.loaded and not cluster.from_cache) or cluster.no_slurm) and cluster.loader is None
          and (not cluster.destination or (cluster.connection is not None and cluster.connection.ready))
          and not isinstance(app.screen, ModalScreen))


def restore(app, cluster):
  """Open the kept subtabs of a tab again (once, when it is ready; in their order), then show the one shown before"""
  saved = getattr(cluster, "pending_views", None)
  if not saved or getattr(cluster, "restoring", False):
    return
  if not ready(app, cluster):
    if cluster is app.cluster and ((cluster.loaded and not cluster.from_cache) or cluster.no_slurm):
      app.set_timer(1, lambda: restore(app, cluster)) # (a dialog open: when it closed)
    return
  cluster.restoring = True
  specs = list(saved.get("views") or [])
  opened = {} # index of the spec -> the view it opened
  def finish():
    cluster.pending_views, cluster.restoring = None, False
    if saved.get("jobs_open"):
      cluster.jobs_open = True
    shown = saved.get("shown")
    if cluster is not app.cluster:
      return
    if shown == "jobs":
      app.show_view(None)
    elif isinstance(shown, int) and shown in opened and opened[shown] in cluster.views:
      app.show_view(opened[shown])
    elif shown == "overview":
      app.show_overview()
    app.refresh_status()
    app.save_tabs()
  def step(i):
    if i >= len(specs) or cluster is not app.cluster:
      finish()
      return
    before = list(cluster.views)
    waited, done = [0.0], [False]
    def next_one():
      new = [v for v in cluster.views if v not in before]
      if new:
        opened[i] = new[-1]
      app.call_later(lambda: step(i + 1))
    def wait():
      if [v for v in cluster.views if v not in before] or waited[0] >= RESTORE_WAIT or done[0]:
        next_one()
      else:
        waited[0] += 0.1
        app.set_timer(0.1, wait)
    try:
      if open_spec(app, cluster, specs[i], lambda: done.__setitem__(0, True)):
        wait()
      else:
        next_one()
    except Exception as e:
      log.warning(f"{cluster.name}: opening the subtab {specs[i]} again failed: {e!r}")
      next_one()
  step(0)


def open_spec(app, cluster, spec, done):
  """Open what a kept subtab showed; True if it opens in the background (done() when it did, or could not)"""
  kind = spec.get("type")
  if kind == "job":
    jid = str(spec.get("jid") or "")
    job = cluster.jobs.get(jid)
    if job is not None:
      app.open_job_subtab(job)
      return False
    from ..cluster.jobs import annotate, load_jobs
    from ..cluster.remote import in_background
    def loaded(jobs):
      job = next((j for j in jobs or [] if j.jid == jid), None)
      if job is not None and cluster is app.cluster:
        app.open_job_subtab(job)
      done()
    in_background(app, lambda: annotate(load_jobs(None, jids=[jid])), loaded, group="restore", name=f"job {jid} again")
    return True
  if kind == "array":
    app.open_array_subtab(str(spec.get("id")))
    return False
  if kind == "categories":
    if cluster.categories is None:
      return False
    if spec.get("key", "").startswith("category ") and spec.get("path"):
      app.open_category(spec["path"])
    else:
      app.action_categories()
    return False
  if kind == "rules":
    app.action_rule_catalogue(spec.get("rule") or "")
    return False
  if kind == "guides":
    app.action_guides()
    return True
  if kind == "path" and spec.get("path"):
    from .screens.files import open_path
    open_path(app, list(spec.get("crumbs") or ["Jobs"]), spec["path"], follow=bool(spec.get("follow")),
              at_end=spec.get("at_end"), then=done)
    return True
  return False
