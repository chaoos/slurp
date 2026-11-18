"""The top-level tab Transfers, part of the app: there while transfers exist (also finished ones), left of the tabs of
the clusters. Its subtabs: the list (always there, its first), and one for each transfer opened from it. It has no
connection of its own: what acts on a cluster (the job list, a shell, …) goes to the cluster tab shown before.

Transfers are set up with Menu → Transfer data… (T), or New transfer in the list: the wizard, then Go."""

import os
import re

from rich.text import Text

from ..config import load_config
from ..state import View, log
from ..transfer.manager import Manager, client_side
from . import icons

TRANSFERS_MODE = "transfers"
ENDPOINTS_MODE = "transfer-endpoints"
# actions of the app that act on the cluster of a tab: not while the Transfers tab is shown (from the menu: the cluster
# tab shown before is shown first)
CLUSTER_ACTIONS = ("reload", "job_list", "cluster_shell", "login_dir", "cluster_drive", "interactive_job", "cluster_info",
                   "quota", "scratch", "abort_load", "new_script", "categories", "categorize", "mark", "mark_all",
                   "toggle_problems", "toggle_collapse", "toggle_group", "connection")

class TransfersTab:
  def init_transfers(self):
    self.in_transfers = False   # the Transfers tab is shown
    self.transfer_views = []    # its subtabs besides the list (View, key "transfer <id>")
    self.transfers_current = TRANSFERS_MODE # the mode it shows
    self.wizard_draft = None    # what a wizard that stepped aside for a login had (T brings it back)
    self.transfers = Manager(self)
    gone = [t.name for t in self.transfers.transfers if t.unscheduled]
    if gone: # (transfers of an older slurp with a schedule: said once, the store written without it)
      self.transfers.store.save()
      names = ", ".join(gone[:5]) + (f" and {len(gone) - 5} more" if len(gone) > 5 else "")
      self.call_later(lambda: self.notify(f"{names}: their schedules are gone, they run on Run now. Copy as a command (in "
                                          "their details) gives the command for scrontab or cron.", title="Transfers",
                                          severity="warning", timeout=20))

  # ---- the tab
  def is_transfers_mode(self, mode):
    return mode in (TRANSFERS_MODE, ENDPOINTS_MODE) or any(v.mode == mode for v in self.transfer_views)

  def ensure_transfers_mode(self):
    if TRANSFERS_MODE not in self._modes:
      from .screens.transfers import EndpointsScreen, TransfersScreen
      self.add_mode(TRANSFERS_MODE, TransfersScreen)
      self.add_mode(ENDPOINTS_MODE, EndpointsScreen)

  def transfers_tab_wanted(self):
    """The Transfers tab is there while transfers or defined endpoints exist (or its endpoints are shown)"""
    return bool(self.transfers.transfers or self.transfers.endpoint_store.defs or
                (self.in_transfers and self.transfers_current == ENDPOINTS_MODE))

  def show_endpoints(self):
    """The Endpoints subtab of the Transfers tab (Menu → Endpoints…)"""
    self.ensure_transfers_mode()
    self.in_transfers = True
    self.transfers_current = ENDPOINTS_MODE
    if self.current_mode != ENDPOINTS_MODE:
      self.switch_mode(ENDPOINTS_MODE)
    self.call_after_refresh(self.refresh_status)
    self.call_after_refresh(self.save_tabs)

  def action_endpoints(self):
    self.show_endpoints()

  def show_transfers(self, view=None):
    """Show the Transfers tab: its list, or a transfer's subtab (view), or what it showed last"""
    self.ensure_transfers_mode()
    if view is None and not self.in_transfers and any(v.mode == self.transfers_current for v in self.transfer_views):
      view = next(v for v in self.transfer_views if v.mode == self.transfers_current)
    if view is None and not self.in_transfers and self.transfers_current == ENDPOINTS_MODE:
      self.show_endpoints()
      return
    mode = view.mode if view is not None else TRANSFERS_MODE
    self.in_transfers = True
    self.transfers_current = mode
    if self.current_mode != mode:
      self.switch_mode(mode)
    self.call_after_refresh(self.refresh_status)
    self.call_after_refresh(self.save_tabs)

  def leave_transfers(self, index=None):
    """Back to a cluster tab (the one shown before, or index)"""
    self.in_transfers = False
    if self.clusters:
      self.activate_cluster(self.cluster_index if index is None else index, connect=False)
    else:
      self.switch_mode("_default")
    self.refresh_status()

  def transfers_tab_label(self):
    running = sum(t.state in ("running", "starting", "pausing") for t in self.transfers.transfers)
    waiting = sum(t.state in ("queued", "waiting") for t in self.transfers.transfers)
    label = Text(icons.label("transfer", "Transfers"))
    if running:
      pcts = [t.progress.get("percent") for t in self.transfers.transfers if t.state == "running" and t.progress.get("percent") is not None]
      label.append(f" · {running} running" + (f" · {pcts[0]:.0f} %" if len(pcts) == 1 else ""), style="bold")
    elif waiting:
      label.append(f" · {waiting} waiting")
    return label

  def refresh_transfers_button(self, tabs):
    """The tab's button in a window's tab bar: there while transfers exist"""
    from textual.widgets import Button
    found = tabs.query("#transfers-tab")
    wanted = self.transfers_tab_wanted()
    if not found:
      if not wanted:
        return
      first = tabs.query("Button.cluster-tab") or tabs.query("#cluster-add")
      tabs.mount(Button(self.transfers_tab_label(), id="transfers-tab", classes="transfers-tab", compact=True,
                        tooltip="the transfers of data between the tabs"), before=first.first())
      return
    button = found.first()
    button.display = wanted
    variant = "primary" if self.in_transfers else "default"
    if button.variant != variant:
      button.variant = variant
    label = self.transfers_tab_label()
    if getattr(button, "shown_label", None) != label.markup:
      button.label, button.shown_label = label, label.markup

  # ---- its subtabs
  def refresh_transfer_tabs(self, screen):
    """The subtab bar of a window of the Transfers tab: the list, then a subtab for each transfer opened"""
    from textual.widgets import Button
    bar = screen.query("#view-tabs").first() if screen.query("#view-tabs") else None
    if bar is None:
      return
    for sel in ("#view-overview", "#view-jobs", "#view-close-jobs"):
      for b in bar.query(sel):
        b.display = False
    if not bar.query("#view-transfers"):
      bar.mount(Button(icons.label("jobs", "All transfers"), id="view-transfers", classes="view-tab", compact=True,
                       tooltip="the list of transfers: the first subtab of the Transfers tab"))
    if not bar.query("#view-endpoints"):
      bar.mount(Button(icons.label("connect", "Endpoints"), id="view-endpoints", classes="view-tab", compact=True,
                       tooltip="the endpoints: this machine, the cluster tabs, S3, Globus, …"), after=bar.query_one("#view-transfers"))
    for b in bar.query("#view-transfers"):
      b.variant = "primary" if self.current_mode == TRANSFERS_MODE else "default"
    for b in bar.query("#view-endpoints"):
      b.variant = "primary" if self.current_mode == ENDPOINTS_MODE else "default"
    wanted = {str(v.id) for v in self.transfer_views}
    for b in bar.query("Button.view-tab, Button.view-close"):
      if b.id in ("view-transfers", "view-endpoints") or b.id in ("view-overview", "view-jobs", "view-close-jobs"):
        continue
      vid = b.id.rsplit("-", 1)[1]
      if not b.id.startswith("xfer-") or vid not in wanted:
        if not getattr(b, "removing", False):
          b.removing = True
          b.remove()
    for v in self.transfer_views:
      if not bar.query(f"#xfer-tab-{v.id}"):
        bar.mount(Button(icons.label(v.icon, v.title[:40]), id=f"xfer-tab-{v.id}", classes="view-tab", compact=True, tooltip=v.title),
                  Button("✕", id=f"xfer-close-{v.id}", classes="view-close", compact=True, tooltip="close the subtab (the transfer goes on)"))
      for b in bar.query(f"#xfer-tab-{v.id}"):
        b.variant = "primary" if self.current_mode == v.mode else "default"
        t = self.transfers.get(v.key.split(" ", 1)[1])
        title = t.name if t is not None else v.title
        if str(b.label) != icons.label(v.icon, title[:40]):
          b.label = icons.label(v.icon, title[:40])

  def transfer_view(self, t):
    return next((v for v in self.transfer_views if v.key == f"transfer {t.id}"), None)

  def open_transfer(self, t):
    """A transfer's subtab under the Transfers tab (the one open already: shown)"""
    view = self.transfer_view(t)
    if view is None:
      from .screens.transfers import TransferScreen
      type(self).next_view_id += 1
      view = View(type(self).next_view_id, t.name, "window", f"transfer {t.id}", None, "transfer")
      tid = t.id
      self.add_mode(view.mode, lambda: TransferScreen(tid))
      self.transfer_views.append(view)
    self.show_transfers(view)

  def close_transfer_view(self, view):
    if view not in self.transfer_views:
      return
    self.transfer_views.remove(view)
    if self.current_mode == view.mode:
      self.transfers_current = TRANSFERS_MODE
      if self.in_transfers:
        self.switch_mode(TRANSFERS_MODE)
    def remove():
      try:
        self.remove_mode(view.mode)
      except Exception as e:
        log.debug(f"removing the subtab {view.title}: {e}")
    self.call_after_refresh(remove)
    self.refresh_status()
    self.call_after_refresh(self.save_tabs)

  def close_current_transfer_view(self):
    view = next((v for v in self.transfer_views if v.mode == self.current_mode), None)
    if view is not None:
      self.close_transfer_view(view)

  def transfer_subtab_pressed(self, button_id):
    """A button of the subtab bar of the Transfers tab"""
    if button_id == "view-transfers":
      self.show_transfers_list()
      return
    if button_id == "view-endpoints":
      self.show_endpoints()
      return
    vid = int(button_id.rsplit("-", 1)[1])
    view = next((v for v in self.transfer_views if v.id == vid), None)
    if view is None:
      return
    if button_id.startswith("xfer-tab-"):
      self.show_transfers(view)
    else:
      self.close_transfer_view(view)

  def show_transfers_list(self):
    self.ensure_transfers_mode()
    self.transfers_current = TRANSFERS_MODE
    self.show_transfers(None) if self.in_transfers else None
    if self.in_transfers and self.current_mode != TRANSFERS_MODE:
      self.switch_mode(TRANSFERS_MODE)
    self.call_after_refresh(self.refresh_status)

  # ---- endpoints (tabs, this machine, Globus; S3, SMB and WebDAV come later)
  def endpoint_kinds(self):
    """The kinds of endpoints that can be made here: {kind: "" (it can) or why not}"""
    from ..transfer import globus as G
    ok, why = G.available()
    return {"ssh": "", "globus": "" if ok else why}

  def edit_endpoint(self, endpoint, kind):
    """New (endpoint None) or Edit… of an endpoint; for a tab ("tab"): its link to a Globus collection"""
    from .screens.globus import GlobusEndpointScreen
    from ..transfer.manager import site_of
    store = self.transfers.endpoint_store
    if kind == "globus":
      def saved(result):
        if not result:
          return
        d = result["definition"]
        try:
          store.add(d)
        except ValueError as e:
          self.notify(str(e), title="Endpoints", severity="warning")
          return
        self.transfers.endpoint_status.pop(d.id, None)
        self.transfers.changed(None)
        self.after_globus_change(d.settings, d.name, lambda: self.connect_endpoint(self.endpoint_named(d.id)))
      self.push_screen(GlobusEndpointScreen(store, endpoint=endpoint), saved)
    elif kind == "tab" and endpoint is not None:
      suggestion = str(site_of(endpoint.cluster).get("globus") or "") if endpoint.cluster is not None else ""
      def linked(result):
        if not result:
          return
        if result["link"] is None:
          store.links.pop(endpoint.id, None)
          self.notify(f"{endpoint.name} is no longer linked to a Globus collection", title="Endpoints", timeout=5)
        else:
          store.links[endpoint.id] = result["link"]
          self.notify(f"{endpoint.name} is linked to {result['link'].get('collection_name') or result['link']['collection']}", title="Endpoints", timeout=5)
        store.save()
        self.transfers.endpoint_status.pop(f"link:{endpoint.id}", None)
        self.transfers.changed(None)
        if result["link"] is not None:
          self.after_globus_change(result["link"], endpoint.name, None)
      self.push_screen(GlobusEndpointScreen(store, tab=endpoint, suggestion=suggestion), linked)
    else:
      self.notify(f"endpoints of the kind {kind} are not there yet", title="Endpoints", severity="warning")

  def endpoint_named(self, eid):
    from ..transfer import endpoints as E
    m = self.transfers
    return E.resolve(self, m.endpoint_store, {"endpoint": eid}, m.endpoint_status)

  def after_globus_change(self, settings, name, then):
    """A collection that needs a consent the login does not have: the login again, with it; then then()"""
    from ..transfer import globus as G
    from .screens.globus import GlobusLoginScreen
    cid = settings.get("collection")
    if cid and (not G.logged_in() or (settings.get("needs_consent") and cid not in G.consented())):
      why = (f"{name}: its collection needs your consent to its data: log in to Globus again, with it" if G.logged_in()
             else f"{name}: log in to Globus to use it")
      self.push_screen(GlobusLoginScreen([cid], why), lambda ok: then() if ok and then else None)
    elif then is not None:
      then()

  def connect_endpoint(self, endpoint, quiet=True):
    """Connect (Log in) of an endpoint that is no tab: Globus: the login if there is none, then a test"""
    from ..transfer import globus as G
    if endpoint is None:
      return
    if endpoint.kind != "globus":
      self.notify(f"{endpoint.name}: endpoints of the kind {endpoint.kind} are not there yet", title="Endpoints", severity="warning")
      return
    ok, why = G.available()
    if not ok:
      self.notify(why, title=endpoint.name, severity="warning", timeout=10)
      return
    settings = endpoint.definition.settings if endpoint.definition is not None else {}
    if not G.logged_in() or endpoint.state in ("expired", "needs consent"):
      from .screens.globus import GlobusLoginScreen
      wanted = [settings["collection"]] if settings.get("collection") and (settings.get("needs_consent") or endpoint.state == "needs consent") else []
      self.push_screen(GlobusLoginScreen(wanted, endpoint.words if endpoint.state in ("expired", "needs consent") else ""),
                       lambda done: self.connect_endpoint(self.endpoint_named(endpoint.id), quiet) if done else None)
      return
    def tested(state, words):
      if not quiet or state != "connected":
        self.notify(f"{endpoint.name}: {state}" + (f" ({words})" if words else ""), title="Endpoints",
                    severity="information" if state == "connected" else "warning", timeout=8)
    self.transfers.test_globus(endpoint, tested)

  def test_endpoint(self, endpoint):
    if endpoint is not None and endpoint.kind == "tab":
      link = self.transfers.endpoint_store.links.get(endpoint.id)
      if link:
        def tested(state, words):
          self.notify(f"{endpoint.name} (Globus: {link.get('collection_name') or link['collection']}): {state}" + (f" ({words})" if words else ""),
                      title="Endpoints", severity="information" if state == "connected" else "warning", timeout=8)
        self.transfers.test_globus(endpoint, tested)
        return
    self.connect_endpoint(endpoint, quiet=False)

  def delete_endpoint(self, endpoint):
    from ..transfer import endpoints as E
    store = self.transfers.endpoint_store
    try:
      store.remove(endpoint.id, self.transfers.transfers)
    except E.InUse as e:
      self.notify(f"{endpoint.name} cannot be deleted: {e}", title="Endpoints", severity="warning", timeout=10)
      return
    self.transfers.endpoint_status.pop(endpoint.id, None)
    self.transfers.changed(None)

  # ---- the wizard, editing, deleting
  def action_transfer_data(self):
    """Menu → Transfer data… (T): the wizard; Go starts the transfer (or schedules it)"""
    if not self.clusters:
      self.notify("open the tabs of the clusters first (+)", severity="warning")
      return
    from .screens.transfers import TransferWizard
    source = None if self.in_transfers else self.cluster
    draft, self.wizard_draft = self.wizard_draft, None # (one that stepped aside for a login: as it was)
    self.push_screen(TransferWizard(self, source_cluster=source, draft=draft), lambda result: self.wizard_done(result, None))

  def edit_transfer(self, t):
    from .screens.transfers import TransferWizard
    self.push_screen(TransferWizard(self, editing=t), lambda result: self.wizard_done(result, t))

  def wizard_done(self, result, editing):
    if not result:
      return
    new = result["transfer"]
    self.wizard_draft = None
    if editing is not None:
      for key in ("name", "source", "dest", "route", "runs_on", "options", "plan"):
        setattr(editing, key, getattr(new, key))
      editing.event("settings changed")
      t = editing
      self.transfers.changed(t)
    else: # (added: it runs when the user clicks Run now)
      t = new
      t.state, t.message = "new", ""
      t.event("set up")
      self.transfers.add(t)
    self.open_transfer(t)

  def delete_transfer(self, t):
    view = self.transfer_view(t)
    if view is not None:
      self.close_transfer_view(view)
    try:
      self.transfers.delete(t)
    except ValueError as e:
      self.notify(str(e), title=t.name, severity="warning")
      return
    if not self.transfers_tab_wanted() and self.in_transfers: # (no transfers, no endpoints: no tab)
      self.leave_transfers()
    self.refresh_status()

  # ---- to the clusters
  def connect_cluster(self, cluster):
    """Log in to a tab a transfer waits for: its tab is shown, the login in the pane"""
    if cluster not in self.clusters:
      return
    self.in_transfers = False
    self.activate_cluster(self.clusters.index(cluster), connect=False)
    self.connect_tab()

  def open_transfer_job(self, t, jid=""):
    """The job window of a transfer's job (of its run under way or its last one, or jid), in the tab of its cluster"""
    jid = jid or t.run.get("job") or next((r.get("job") for r in reversed(t.runs) if r.get("job")), "")
    src, dst = self.transfers.ends(t)
    cluster = dst if client_side(t.route) == "dest" else src
    if not jid or cluster is None:
      return
    self.in_transfers = False
    self.activate_cluster(self.clusters.index(cluster), connect=False)
    if cluster.loaded:
      self.action_open_job(jid)
    else:
      self.notify(f"job {jid}: in the job list once it is loaded", title=cluster.name)

  def open_transfer_path(self, cluster, path, crumbs):
    """A path of a transfer in the tab of its cluster: a folder in a directory view, a file in its view (one that
    cannot be shown: its folder, searched for it), a pattern (*.log) as its folder searched for it, a path that is not
    there (yet: a destination) as the nearest folder that is"""
    if cluster not in self.clusters:
      self.notify("its tab is not open", severity="warning")
      return
    if cluster.destination and not (cluster.connection and cluster.connection.ready):
      self.notify(f"connect {cluster.name} first", title=cluster.name, severity="warning")
      self.connect_cluster(cluster)
      return
    from ..cluster.paths import is_binary
    from ..cluster.remote import fs_isdir, fs_isfile, in_background
    from .screens.files import DirScreen, FILE_VIEWS, open_path
    def work():
      folder, name = os.path.dirname(path.rstrip("/")) or "/", os.path.basename(path.rstrip("/"))
      if re.search(r"[*?\[]", name):
        return "dir", folder, name
      if fs_isdir(path):
        return "path", path, ""
      if fs_isfile(path):
        if any(view.matches(path) for view in FILE_VIEWS) or not is_binary(path):
          return "path", path, ""
        return "dir", folder, name # (it cannot be shown: its folder, searched for it)
      while folder not in ("/", "") and not fs_isdir(folder):
        folder = os.path.dirname(folder)
      return "dir", folder or "/", ""
    def found(result):
      if result is None:
        return
      kind, where, search = result
      if cluster not in self.clusters:
        return
      self.in_transfers = False
      self.activate_cluster(self.clusters.index(cluster), connect=False)
      if kind == "path":
        open_path(self, crumbs, where)
        return
      if search or where != os.path.normpath(path):
        self.notify(f"{path}: " + (f"its folder, searched for {search}" if search else f"not there (yet): {where}"),
                    title=cluster.name, timeout=5)
      self.in_new_subtab(lambda: self.open_window(DirScreen(crumbs, where, search=search)),
                         key=f"path {os.path.normpath(where)}" + (f" {search}" if search else ""))
    in_background(self, work, found, group="open", name=f"opening {path}",
                  connection=cluster.connection if cluster.destination else None)

  # ---- kept for the next start
  def transfers_state(self):
    return {"open": [v.key.split(" ", 1)[1] for v in self.transfer_views], "shown": self.in_transfers,
            "endpoints": self.transfers_current == ENDPOINTS_MODE,
            "current": next((v.key.split(" ", 1)[1] for v in self.transfer_views if v.mode == self.transfers_current), "")}

  def restore_transfers(self):
    """The subtabs of the Transfers tab of the last run (they need no connection: open at once); shown if it was"""
    kept = load_config().get("transfers_tab") or {}
    if not isinstance(kept, dict) or self.fresh_start or not (self.transfers.transfers or self.transfers.endpoint_store.defs):
      return
    from .screens.transfers import TransferScreen
    current = None
    for tid in kept.get("open") or []:
      t = self.transfers.get(tid)
      if t is None or self.transfer_view(t) is not None:
        continue
      type(self).next_view_id += 1
      view = View(type(self).next_view_id, t.name, "window", f"transfer {t.id}", None, "transfer")
      self.add_mode(view.mode, lambda tid=t.id: TransferScreen(tid))
      self.transfer_views.append(view)
      if tid == kept.get("current"):
        current = view
    if current is not None:
      self.transfers_current = current.mode
    elif kept.get("endpoints"):
      self.ensure_transfers_mode()
      self.transfers_current = ENDPOINTS_MODE
    if kept.get("shown"):
      self.call_after_refresh(lambda: self.show_transfers(current))

  def transfers_for_tab(self, cluster):
    """The transfers under way from or to a tab (closing it asks)"""
    return [t for t in self.transfers.transfers if t.state in ("running", "starting", "pausing", "waiting")
            and cluster in self.transfers.ends(t)]
