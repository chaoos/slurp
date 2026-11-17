"""The subtabs of a tab, kept for the next start: what each one shows as a small description (a job, an array, a file
or directory, the categories, the guides, the rule catalogue; shells and windows of the moment are not kept), saved
with the tabs (app.save_tabs). At the next start they are there at once, as placeholders (a click before the tab is
connected: connect first, the overview); once it is connected and its job list loaded, each is opened in its place"""

import os

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
    spec.update(title=view.title, icon=view.icon) # (its placeholder looks the same)
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
  """The subtabs of a tab to keep: {"views": [spec], "shown": "jobs", "overview" or the index of a view, "jobs_open"};
  those not opened again yet (placeholders: not connected since the start) as they were"""
  specs, shown = [], "overview"
  for view in cluster.views:
    spec = view_spec(app, view)
    if spec is not None:
      if cluster.current_view == view.mode:
        shown = len(specs)
      specs.append(spec)
  if cluster.current_view == "_default":
    shown = "jobs"
  saved = cluster.pending_views
  if saved and cluster.current_view == "overview" and isinstance(saved.get("shown"), int):
    shown = saved["shown"] # (not opened again yet: the one shown before stays the one to show)
  return {"views": specs, "shown": shown, "jobs_open": bool(cluster.jobs_open or (saved or {}).get("jobs_open"))}


def spec_key(spec):
  """The key of the subtab a kept one opens as (so that opening it before it is restored finds its placeholder)"""
  kind = spec.get("type")
  if kind == "job":
    return f"job {spec.get('jid')}"
  if kind == "array":
    return f"array {spec.get('id')}"
  if kind == "path":
    return f"path {os.path.normpath(spec.get('path') or '/')}"
  if kind == "categories":
    return spec.get("key") or "categories"
  return {"rules": "rules", "guides": "guides"}.get(kind, "")


def spec_title(spec):
  """(the title and the icon of its placeholder: as the subtab had them)"""
  if spec.get("title"):
    return str(spec["title"]), str(spec.get("icon") or "file")
  kind = spec.get("type")
  if kind == "job":
    return f"job {spec.get('jid')}", "file"
  if kind == "array":
    return f"array {spec.get('id')}", "array"
  if kind == "path":
    path = spec.get("path") or "/"
    return os.path.basename(path.rstrip("/")) or path, "file"
  if kind == "categories":
    key = spec.get("key") or "categories"
    return key, "categories"
  if kind == "rules":
    return "Rule catalogue", "rules"
  if kind == "guides":
    return "Guides", "docs"
  return kind or "?", "file"


def placeholders(app, cluster, saved):
  """The kept subtabs of a tab as placeholders, at the start: shown at once (title, icon), opened when it is connected"""
  from ..state import View
  for spec in (saved or {}).get("views") or []:
    if not isinstance(spec, dict):
      continue
    type(app).next_view_id += 1
    title, icon = spec_title(spec)
    cluster.views.append(View(type(app).next_view_id, title, "window", spec_key(spec), icon=icon, kept=spec, pending=True))


def ready(app, cluster):
  """Can the subtabs of a tab be opened now: it is shown, connected, its job list loaded, no dialog open"""
  from textual.screen import ModalScreen
  return (cluster is app.cluster and ((cluster.loaded and not cluster.from_cache) or cluster.no_slurm) and cluster.loader is None
          and (not cluster.destination or (cluster.connection is not None and cluster.connection.ready))
          and not isinstance(app.screen, ModalScreen))


def placeholder_clicked(app, view):
  """A placeholder clicked: not connected yet: say so, the overview (Connect is there); else it opens (soon)"""
  cluster = app.cluster
  if ready(app, cluster) or cluster.restoring:
    cluster.restore_target = view.kept
    restore(app, cluster)
    app.notify(f"opening {view.title} …", timeout=3)
    return
  app.notify(f"{view.title}: connect first; it opens when {cluster.name} is connected", severity="warning", timeout=6)
  app.show_overview()


def restore(app, cluster):
  """Open the placeholders of a tab (once it is ready; in their order, each in its place; nothing switches), then
  show the one shown before (if the overview or the job list is still shown) or the one clicked meanwhile"""
  if getattr(cluster, "restoring", False) or not any(v.pending for v in cluster.views):
    return
  if not ready(app, cluster):
    if cluster is app.cluster and ((cluster.loaded and not cluster.from_cache) or cluster.no_slurm):
      app.set_timer(1, lambda: restore(app, cluster)) # (a dialog open: when it closed)
    return
  cluster.restoring = True
  saved = cluster.pending_views or {}
  shown = saved.get("shown")
  specs_order = [v.kept for v in cluster.views if v.pending]
  opened = {} # id(spec) -> the view it opened
  def finish():
    cluster.restoring = False
    cluster.pending_views = None
    if saved.get("jobs_open"):
      cluster.jobs_open = True
    target = getattr(cluster, "restore_target", None)
    cluster.restore_target = None
    if cluster is app.cluster:
      if target is not None and id(target) in opened:
        app.show_view(opened[id(target)])
      elif app.current_mode in ("overview", "_default") and isinstance(shown, int) and 0 <= shown < len(specs_order) \
          and id(specs_order[shown]) in opened:
        app.show_view(opened[id(specs_order[shown])])
      app.refresh_status()
    app.save_tabs()
  def step():
    holder = next((v for v in cluster.views if v.pending), None)
    if holder is None or cluster is not app.cluster:
      finish()
      return
    spec = holder.kept
    holder.key = "" # (the real subtab opens with its key)
    before = list(cluster.views)
    waited, done = [0.0], [False]
    def placed():
      new = [v for v in cluster.views if v not in before]
      if holder in cluster.views:
        index = cluster.views.index(holder)
        cluster.views.remove(holder)
        if new:
          view = new[-1]
          cluster.views.remove(view)
          cluster.views.insert(index, view)
          view.kept = spec
          opened[id(spec)] = view
        if cluster.current_view == holder.mode:
          cluster.current_view = "overview"
      app.refresh_status()
      app.call_later(step)
    def wait():
      if [v for v in cluster.views if v not in before] or waited[0] >= RESTORE_WAIT or done[0]:
        placed()
      else:
        waited[0] += 0.1
        app.set_timer(0.1, wait)
    try:
      if open_spec(app, cluster, spec, lambda: done.__setitem__(0, True)):
        wait()
      else:
        placed()
    except Exception as e:
      log.warning(f"{cluster.name}: opening the subtab {spec} again failed: {e!r}")
      placed()
  step()


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
