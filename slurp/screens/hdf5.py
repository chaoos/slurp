"""The HDF5 viewer"""

import argparse
import asyncio
import codecs
import collections
import fcntl
import functools
import json
import logging
import os
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import humanize
from rich.color import Color
from rich.progress_bar import ProgressBar as RichProgressBar
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, MarkdownViewer, OptionList, ProgressBar, RadioButton, RadioSet, Select, Static, Switch, TextArea, Tree
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual.suggester import SuggestFromList
from textual.worker import get_current_worker

import slurm as sl

from ..state import *
from ..remote import *
from ..details import *
from ..widgets import *
from ..screens.base import *
from ..screens.files import *

def open_hdf5(app, crumbs, path, bases=(), env=None):
  """Browse an HDF5 file, or without h5py show its structure as printed by h5dump -H (runs in a worker)"""
  try:
    import h5py # noqa: F401
  except ImportError:
    if command_exists("h5dump"):
      cmd = ["h5dump", "-H", path]
      text = run(cmd)
      def show():
        app.open_window(FileScreen([*crumbs, os.path.basename(path)], text=text, refresher=lambda: run(cmd), bases=bases, env=env))
        app.notify("h5py is not installed: showing the structure printed by h5dump -H", severity="warning")
      return show
    return lambda: app.notify(f"{path} is an HDF5 file: install h5py to browse it", severity="warning")
  return lambda: app.open_window(H5Screen(crumbs, path, bases=bases, env=env))
H5_ROWS = 200       # rows of a dataset shown at a time
H5_COLS = 50        # columns (or fields of a compound type) shown at a time
H5_MEMBERS = 5000   # members of a group listed at most

def open_h5(path):
  """Open read-only without file locking, so files that a running job writes can be read too"""
  import h5py
  try:
    return h5py.File(path, "r", locking=False)
  except TypeError: # h5py < 3.5
    return h5py.File(path, "r")

def parse_selection(text, shape):
  """'[3, :, 10:20]' -> tuple with an int or a slice for every axis ('...' and missing trailing axes: all)"""
  text = text.strip()
  if text.startswith("[") and text.endswith("]"):
    text = text[1:-1]
  parts = [p.strip() for p in text.split(",")] if text.strip() else []
  if parts.count("...") > 1:
    raise ValueError("only one ... allowed")
  if "..." in parts:
    i = parts.index("...")
    parts[i:i + 1] = [":"] * max(0, len(shape) - len(parts) + 1)
  if len(parts) > len(shape):
    raise ValueError(f"the dataset has {len(shape)} dimensions, not {len(parts)}")
  parts += [":"] * (len(shape) - len(parts))
  sel = []
  for p, n in zip(parts, shape):
    items = p.split(":")
    try:
      nums = [int(x) if x.strip() else None for x in items]
    except ValueError:
      nums = None
    if nums is None or len(nums) > 3 or (len(nums) == 1 and nums[0] is None):
      raise ValueError(f"'{p}' is not an index or a slice like 10:20 or ::2")
    if len(nums) == 1:
      if not -n <= nums[0] < n:
        raise ValueError(f"index {nums[0]} is out of range for size {n}")
      sel.append(nums[0] % n)
    else:
      nums += [None] * (3 - len(nums))
      if nums[2] is not None and nums[2] < 1:
        raise ValueError(f"only positive steps are supported: '{p}'")
      sel.append(slice(*nums))
  return tuple(sel)

def window(sel, shape, row0=0, col0=0):
  """Restrict the (at most two) sliced axes of a selection to the rows and columns shown.
  Returns the new selection and for each sliced axis (indices shown, number of indices selected)."""
  sel = list(sel)
  axes = [k for k, s in enumerate(sel) if isinstance(s, slice)]
  if len(axes) > 2:
    raise ValueError(f"{len(axes)} dimensions selected: fix all but two with an index, e.g. [0, :, :]")
  shown = []
  for k, start, limit in zip(axes, (row0, col0), (H5_ROWS, H5_COLS)):
    full = range(*sel[k].indices(shape[k]))
    part = full[start:start + limit]
    sel[k] = slice(part[0], part[-1] + 1, part.step) if len(part) else slice(0, 0)
    shown.append((part, len(full)))
  return tuple(sel), shown

def h5_value(v, width=300):
  """Short text for a value of an attribute or a cell"""
  import numpy as np
  if isinstance(v, bytes):
    s = v.decode("utf-8", errors="replace")
  elif isinstance(v, np.ndarray):
    if v.dtype.kind == "S":
      v = np.char.decode(v, "utf-8", errors="replace")
    s = np.array2string(v, threshold=20, edgeitems=3, max_line_width=10**6)
  else:
    s = str(v)
  s = s.replace("\n", " ")
  return s if len(s) <= width else s[:width - 1] + "…"

def h5_link_target(path, filename):
  """File name of an external link, relative to the file that has the link"""
  return os.path.normpath(os.path.join(os.path.dirname(path), fs_expanduser(filename)))

def h5_label(name, kind, extra=""):
  """The parts (text, style) of the label of a tree item"""
  if kind == "external":
    return [(name, "bold magenta"), (extra, "dim")]
  if kind == "error":
    return [(name, "red"), (extra or "  (cannot open)", "dim")]
  if kind == "group":
    return [(name + "/", "bold blue"), (extra, "dim")]
  return [(name, ""), (extra, "dim")]

def list_group(file, gpath):
  """The members of a group as plain data: ([{path, name, kind, label, ...}], members not listed)"""
  import h5py
  group = file[gpath]
  names = list(group)
  items = []
  for name in names[:H5_MEMBERS]:
    path = gpath.rstrip("/") + "/" + name
    item = {"path": path, "name": name}
    try:
      link = group.get(name, getlink=True)
    except Exception:
      link = None
    if isinstance(link, h5py.ExternalLink):
      item.update(kind="external", file=link.filename, target=link.path,
                  label=h5_label(name, "external", f"  ⇢ {link.filename}:{link.path}"))
      items.append(item)
      continue
    soft = f"  → {link.path}" if isinstance(link, h5py.SoftLink) else ""
    try:
      obj = group[name]
    except Exception as e:
      item.update(kind="error", error=f"broken link{soft}: {e}", label=h5_label(name, "error", soft))
      items.append(item)
      continue
    if isinstance(obj, h5py.Group):
      item.update(kind="group", label=h5_label(name, "group", soft))
    elif isinstance(obj, h5py.Dataset):
      shape = "scalar" if obj.shape == () else "empty" if obj.shape is None else "×".join(map(str, obj.shape))
      item.update(kind="dataset", label=h5_label(name, "dataset", f"{soft}  {shape} {obj.dtype}"))
    else:
      item.update(kind="datatype", label=h5_label(name, "datatype", f"{soft}  type"))
    items.append(item)
  return items, max(0, len(names) - H5_MEMBERS)

MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd")

def markdown_value(file, path):
  """The text of a scalar string dataset whose name ends in .md (or .markdown, ...), None for anything else"""
  import h5py
  if not path.lower().endswith(MARKDOWN_SUFFIXES):
    return None
  ds = file[path]
  if not isinstance(ds, h5py.Dataset) or ds.shape != () or h5py.check_string_dtype(ds.dtype) is None and ds.dtype.kind not in "SUO":
    return None
  value = ds.asstr()[()] if h5py.check_string_dtype(ds.dtype) else ds[()]
  if isinstance(value, bytes):
    value = value.decode("utf-8", errors="replace")
  return value if isinstance(value, str) else None

def describe_item(file, path, kind):
  """Info text of a group, dataset or named type"""
  obj = file[path]
  t = Text()
  t.append(obj.name, style="bold")
  if kind == "group":
    t.append(f"\ngroup with {len(obj)} members")
  elif kind == "dataset":
    shape = "scalar" if obj.shape == () else "empty (no dataspace)" if obj.shape is None else " × ".join(map(str, obj.shape))
    t.append(f"\ndataset  {shape}  {obj.dtype}")
    if obj.dtype.names:
      t.append("\nfields: " + ", ".join(f"{n} {obj.dtype.fields[n][0]}" for n in obj.dtype.names))
    layout = f"chunks {obj.chunks}" if obj.chunks else "contiguous"
    filters = [f for f in (obj.compression and f"{obj.compression}" + (f"({obj.compression_opts})" if obj.compression_opts is not None else ""),
                           obj.shuffle and "shuffle", obj.fletcher32 and "fletcher32", obj.scaleoffset and "scaleoffset") if f]
    t.append(f"\n{layout}" + (", " + ", ".join(filters) if filters else ""))
    try:
      stored = obj.id.get_storage_size()
      t.append(f"\n{humanize.naturalsize(obj.nbytes)} of data, {humanize.naturalsize(stored)} on disk")
      if stored and obj.nbytes:
        t.append(f" (ratio {obj.nbytes / stored:.2g})")
    except Exception:
      pass
    if obj.fillvalue is not None and obj.shape:
      t.append(f"\nfill value {h5_value(obj.fillvalue)}")
    try:
      dims = [", ".join(d.keys()) or (d.label if d.label else "") for d in obj.dims]
      if any(dims):
        t.append("\ndimensions: " + " × ".join(d or "-" for d in dims))
    except Exception:
      pass
    if obj.shape == ():
      t.append("\nvalue: ", style="bold")
      t.append(read_scalar(obj))
  else:
    t.append(f"\nnamed datatype {obj.dtype}")
  try:
    attrs = list(obj.attrs)
  except Exception as e:
    t.append(f"\ncannot list the attributes: {e}", style="red")
    return t
  t.append(f"\n\n{len(attrs)} attribute{'s' if len(attrs) != 1 else ''}" + (":" if attrs else ""), style="bold" if attrs else "dim")
  for name in attrs:
    t.append(f"\n  {name}", style="bold")
    try:
      t.append(f" = {h5_value(obj.attrs[name])}")
    except Exception as e:
      t.append(f" (cannot read: {e})", style="red")
  return t

def read_scalar(obj):
  import h5py
  try:
    ds = obj.asstr() if h5py.check_string_dtype(obj.dtype) else obj
    return h5_value(ds[()], width=2000)
  except Exception as e:
    return f"cannot read: {e}"

def dataset_page(file, path, text, row0, col0):
  """The rows and columns of a page of a dataset, read from the file (only that part):
  {"shape", "nfields", "rows", "columns", "status"} (rows None: nothing to show in a table)"""
  import h5py
  ds = file[path]
  page = {"shape": ds.shape, "nfields": len(ds.dtype.names or ()), "rows": None, "columns": (), "status": ""}
  if not ds.shape: # scalar or empty: shown in the info
    return page
  try:
    sel = parse_selection(text, ds.shape)
    sel, shown = window(sel, ds.shape, row0, col0)
  except ValueError as e:
    return dict(page, rows=[], status=Text(str(e), style="red"))
  if any(not len(part) for part, _ in shown):
    return dict(page, rows=[], status="nothing selected")
  try:
    src = ds.asstr() if h5py.check_string_dtype(ds.dtype) else ds
    values = src[sel]
  except Exception as e:
    return dict(page, rows=[], status=Text(f"cannot read: {e}", style="red"))
  names = ds.dtype.names
  status = []
  if len(shown) == 0: # every axis fixed by an index: a single value
    return dict(page, rows=[("", [h5_value(values)])], columns=["value"])
  rows_part, nrows = shown[0]
  page["nrows"] = nrows
  status.append(f"rows {rows_part[0]}–{rows_part[-1]} ({len(rows_part)} of {nrows}" + (", [ ]: page" if len(rows_part) < nrows else "") + ")")
  if len(shown) == 2:
    cols_part, ncols = shown[1]
    page["ncols"] = ncols
    columns = [str(i) for i in cols_part]
    status.append(f"columns {cols_part[0]}–{cols_part[-1]} ({len(cols_part)} of {ncols}" + (", { }: page" if len(cols_part) < ncols else "") + ")")
    rows = [(str(i), [h5_value(v, 60) for v in row]) for i, row in zip(rows_part, values)]
  elif names: # one axis of a compound type: a column per field
    fields = names[col0:col0 + H5_COLS]
    columns = list(fields)
    if len(names) > H5_COLS:
      status.append(f"fields {col0}–{col0 + len(fields) - 1} of {len(names)}, {{ }}: page")
    rows = [(str(i), [h5_value(v[f], 60) for f in fields]) for i, v in zip(rows_part, values)]
  else:
    columns = ["value"]
    rows = [(str(i), [h5_value(v, 200)]) for i, v in zip(rows_part, values)]
  return dict(page, rows=rows, columns=columns, status=" · ".join(status))

class H5Screen(SplitScreen):
  """HDF5 browser: tree of groups and datasets, info and attributes of the selected one, and a table of its data.
  The file is read where it is: on a cluster only the parts shown, over the SSH session (a RemoteFile), never copied.
  Every h5py call runs in a worker (one at a time); the UI shows the results."""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape", "back", "Back"),
    Binding("s", "selection", "Selection"),
    Binding("left_square_bracket", "page(-1, 0)", "Rows ↑", key_display="["),
    Binding("right_square_bracket", "page(1, 0)", "Rows ↓", key_display="]"),
    Binding("left_curly_bracket", "page(0, -1)", "Cols ←", key_display="{"),
    Binding("right_curly_bracket", "page(0, 1)", "Cols →", key_display="}"),
    Binding("r", "reload", "Reload"),
  ]

  def __init__(self, crumbs, path, start="/", bases=(), env=None):
    super().__init__()
    self.path = path
    self.crumbs = [*crumbs, os.path.basename(path)]
    self.start = start
    self.bases, self.env = list(bases), env
    self.file = None
    self.fobj = None      # the RemoteFile of a file on a cluster
    self.connection = None
    self.lock = threading.Lock() # h5py calls, one at a time
    self.listings = {}    # group path -> (items, not listed)
    self.current = None   # data of the selected tree node
    self.page = None      # the last page read of the selected dataset
    self.dataset_selections = {} # dataset path -> selection text typed for it
    self.row0 = self.col0 = 0
    self.file_size = None

  def compose_left(self):
    yield Static(id="hstatus")
    with Horizontal(id="hbody"):
      yield Tree(os.path.basename(self.path), id="htree", data={"path": "/", "kind": "group"})
      with Vertical(id="hright"):
        with VerticalScroll(id="hinfo"):
          yield Static(id="hinfotext")
        yield Markdown(id="hmarkdown")
        yield Input(id="hslice", placeholder="selection, e.g. [0, :, 10:20] or [..., 3] (Enter: apply)")
        yield Static(id="hpage")
        yield DataTable(id="hdata", zebra_stripes=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.connection = conn()
    self.query_one("#htree", Tree).show_root = True
    self.action_reload(first=True)
    self.query_one("#htree").focus()

  def on_unmount(self):
    # in a thread: a worker may hold the lock while it waits for the cluster
    threading.Thread(target=self.close_file, daemon=True).start()

  def close_file(self):
    with self.lock:
      if self.file is not None:
        try:
          self.file.close()
        except Exception:
          pass
        self.file = None
      if self.fobj is not None:
        self.fobj.close()
        self.fobj = None

  def h5(self, task, done, group="h5"):
    """task(file) in a worker, h5py calls one at a time; done(result) on the UI thread"""
    def run_it():
      with self.lock:
        if self.file is None:
          raise OSError("the file is not open")
        return task(self.file)
    in_background(self, run_it, done, group=group, connection=self.connection)

  def status_text(self, extra=""):
    size = humanize.naturalsize(self.file_size) if self.file_size is not None else "?"
    where = ""
    if self.fobj is not None:
      where = f" · read over SSH: {self.fobj.round_trips} reads, {humanize.naturalsize(self.fobj.bytes_read)}"
    return f"{self.path}  {size}{where} · r: reload (e.g. while a job writes it){extra}"

  # --------------------------------------------------------------- tree
  def action_reload(self, first=False):
    """(Re)open the file and rebuild the tree with the same groups expanded and the same item selected"""
    tree = self.query_one("#htree", Tree)
    expanded = set()
    if not first:
      stack = [tree.root]
      while stack:
        node = stack.pop()
        if node.is_expanded and node.data and node.data["kind"] == "group":
          expanded.add(node.data["path"])
        stack.extend(node.children)
    target = self.start if first else (self.current or {}).get("path", "/")
    self.query_one("#hstatus", Static).update(f"{self.path} · opening …")
    connection = self.connection
    def open_and_list():
      """open the file (on a cluster: through a RemoteFile) and list the groups to show"""
      import h5py
      self.close_file()
      with self.lock:
        size = fs_getsize(self.path)
        if connection is not None:
          fobj = RemoteFile(connection, self.path, size)
          try:
            file = h5py.File(fobj, "r")
          except Exception as e:
            raise OSError(f"cannot read the HDF5 file over SSH ({e}); run slurp on the cluster to inspect it") from None
        else:
          fobj, file = None, open_h5(self.path)
        self.file, self.fobj, self.file_size = file, fobj, size
        listings = {}
        wanted = {"/", *expanded}
        parts = [p for p in (target or "/").split("/") if p]
        wanted.update("/" + "/".join(parts[:i]) for i in range(len(parts)))
        for gpath in sorted(wanted, key=lambda p: p.count("/")):
          try:
            if isinstance(file.get(gpath, getlink=False), h5py.Group):
              listings[gpath] = list_group(file, gpath)
          except Exception as e:
            listings[gpath] = ([{"path": gpath, "kind": "error", "error": str(e), "label": h5_label("cannot list", "error", f": {e}")}], 0)
        return listings
    def opened(listings):
      if listings is None:
        self.query_one("#hstatus", Static).update(Text(f"{self.path} · cannot open", style="red"))
        self.show_message(Text(f"cannot open {self.path}", style="red"))
        return
      self.listings = listings
      tree.clear()
      tree.root.data = {"path": "/", "kind": "group"}
      self.add_children(tree.root)
      tree.root.expand()
      for p in sorted(expanded, key=lambda p: p.count("/")):
        node = self.find_node(p)
        if node is not None:
          self.add_children(node)
          node.expand()
      self.goto(target)
      self.query_one("#hstatus", Static).update(self.status_text())
    in_background(self, open_and_list, opened, group="h5-open", connection=connection)

  def add_children(self, node):
    """Tree nodes for the members of a group, from its listing (listed before, in a worker)"""
    data = node.data
    if not data or data["kind"] != "group" or data.get("loaded") or data["path"] not in self.listings:
      return False
    data["loaded"] = True
    items, more = self.listings[data["path"]]
    for item in items:
      label = Text.assemble(*item["label"])
      if item["kind"] == "group":
        node.add(label, data=dict(item))
      else:
        node.add_leaf(label, data=dict(item))
    if more:
      node.add_leaf(Text(f"… {more} more members not listed", style="dim"), data=None)
    return True

  def load_children(self, node):
    """The members of a group: from its listing, or listed in a worker first"""
    data = node.data
    if not data or data["kind"] != "group" or data.get("loaded"):
      return
    if self.add_children(node):
      return
    gpath = data["path"]
    node.set_label(Text.assemble(*h5_label(data.get("name", gpath), "group", "  listing …")))
    def listed(result):
      if result is None:
        return
      self.listings[gpath] = result
      node.set_label(Text.assemble(*h5_label(data.get("name", "") or os.path.basename(self.path), "group")) if gpath != "/" else
                     Text(os.path.basename(self.path)))
      self.add_children(node)
      self.query_one("#hstatus", Static).update(self.status_text())
    self.h5(lambda f: list_group(f, gpath), listed, group=f"h5-list-{gpath}")

  def find_node(self, path, expand=False):
    """Tree node of a path (from the groups listed so far); with expand, the groups on the way are expanded"""
    node = self.query_one("#htree", Tree).root
    for name in [p for p in path.split("/") if p]:
      self.add_children(node)
      if expand:
        node.expand()
      child = next((c for c in node.children if c.data and c.data.get("name") == name), None)
      if child is None:
        return node if expand else None
      node = child
    return node

  def goto(self, path):
    """Select a path in the tree; groups on the way that are not listed yet are listed first (in a worker)"""
    import h5py
    parts = [p for p in (path or "/").split("/") if p]
    missing = [g for g in ("/" + "/".join(parts[:i]) for i in range(len(parts))) if g not in self.listings]
    if missing:
      def list_them(file):
        found = {}
        for g in missing:
          if isinstance(file.get(g, getlink=False), h5py.Group):
            found[g] = list_group(file, g)
        return found
      def listed(found):
        if found:
          self.listings.update(found)
          self.goto(path)
      self.h5(list_them, listed, group="h5-goto")
      return
    node = self.find_node(path or "/", expand=True)
    tree = self.query_one("#htree", Tree)
    self.call_after_refresh(lambda: tree.move_cursor(node))
    self.show(node.data)

  @on(Tree.NodeExpanded, "#htree")
  def expanded(self, event):
    event.stop()
    self.load_children(event.node)

  @on(Tree.NodeHighlighted, "#htree")
  def highlighted(self, event):
    event.stop()
    if event.node.data and event.node.data is not self.current:
      self.show(event.node.data)

  @on(Tree.NodeSelected, "#htree")
  def selected(self, event):
    event.stop()
    data = event.node.data
    if data and data["kind"] == "external":
      target = h5_link_target(self.path, data["file"])
      def checked(ok):
        if ok:
          self.app.open_window(H5Screen(self.crumbs, target, start=data["target"], bases=self.bases, env=self.env))
        elif ok is not None:
          self.notify(f"{target} of the external link does not exist or is no HDF5 file", severity="warning")
      in_background(self, lambda: fs_isfile(target) and is_hdf5(target), checked, group="link")
    elif data and data["kind"] == "dataset":
      self.query_one("#hdata").focus()

  # --------------------------------------------------------------- info and data
  def show_message(self, text):
    self.query_one("#hinfotext", Static).update(text)
    self.query_one("#hmarkdown").display = False
    self.show_table(None)

  def show(self, data):
    self.current, self.row0, self.col0, self.page = data, 0, 0, None
    if not data:
      return
    kind = data["kind"]
    if kind == "error":
      return self.show_message(Text(f"{data['path']}\n{data['error']}", style="red"))
    if kind == "external":
      target = h5_link_target(self.path, data["file"])
      return self.show_message(Text.assemble((data["path"], "bold"), "\nexternal link to ", (f"{data['target']}", "bold"),
                                             " in ", (target, "bold"), "\nEnter or click: open it"))
    self.query_one("#hinfotext", Static).update(Text(f"{data['path']}\nreading …", style="dim"))
    if kind == "dataset":
      self.query_one("#hslice", Input).value = self.dataset_selections.get(data["path"], "")
    text = self.query_one("#hslice", Input).value if kind == "dataset" else ""
    row0, col0 = self.row0, self.col0
    def read(file):
      info = describe_item(file, data["path"], kind)
      page = dataset_page(file, data["path"], text, row0, col0) if kind == "dataset" else None
      markdown = markdown_value(file, data["path"]) if kind == "dataset" else None
      return info, page, markdown
    def shown(result):
      if result is None or self.current is not data:
        return
      info, page, markdown = result
      self.query_one("#hinfotext", Static).update(info)
      md = self.query_one("#hmarkdown", Markdown)
      md.display = markdown is not None
      if markdown is not None: # a string named *.md: rendered
        md.update(markdown)
      self.show_page(page)
      self.query_one("#hstatus", Static).update(self.status_text())
    self.h5(read, shown, group="h5-show")

  def show_page(self, page):
    self.page = page
    if page is None or page["rows"] is None:
      return self.show_table(None)
    self.show_table(page["rows"], page["columns"], page["status"])

  def show_table(self, rows, columns=(), status=""):
    table = self.query_one("#hdata", DataTable)
    table.clear(columns=True)
    sliced = rows is not None and self.current and self.current["kind"] == "dataset"
    self.query_one("#hslice", Input).display = bool(sliced and self.page and self.page["shape"])
    self.query_one("#hpage", Static).update(status)
    self.query_one("#hpage", Static).display = bool(status)
    table.display = rows is not None
    if rows is None:
      return
    for c in columns:
      table.add_column(c)
    for label, cells in rows:
      table.add_row(*cells, label=label)

  def show_data(self):
    """Read the current page of the selected dataset (in a worker) and show it"""
    data = self.current
    if not data or data["kind"] != "dataset":
      return self.show_table(None)
    text, row0, col0 = self.query_one("#hslice", Input).value, self.row0, self.col0
    self.query_one("#hpage", Static).update(Text("reading …", style="dim"))
    def shown(page):
      if page is not None and self.current is data:
        self.show_page(page)
        self.query_one("#hstatus", Static).update(self.status_text())
    self.h5(lambda f: dataset_page(f, data["path"], text, row0, col0), shown, group="h5-page")

  @on(Input.Submitted, "#hslice")
  def selection_submitted(self, event):
    event.stop()
    if self.current:
      self.dataset_selections[self.current["path"]] = event.value
    self.row0 = self.col0 = 0
    self.show_data()
    self.query_one("#hdata").focus()

  def action_selection(self):
    inp = self.query_one("#hslice", Input)
    if inp.display:
      inp.focus()

  def action_page(self, rows, cols):
    if not self.current or self.current["kind"] != "dataset" or not self.page:
      return
    self.row0 = max(0, self.row0 + rows * H5_ROWS)
    self.col0 = max(0, self.col0 + cols * H5_COLS)
    page = self.page # do not page past the end (sizes of the selection from the last page read)
    if page.get("nrows"):
      self.row0 = min(self.row0, max(0, (page["nrows"] - 1) // H5_ROWS * H5_ROWS))
    ncols = page.get("ncols") or page.get("nfields") or 0
    self.col0 = min(self.col0, max(0, (ncols - 1) // H5_COLS * H5_COLS)) if ncols else 0
    self.show_data()

  def action_back(self):
    if self.focused is self.query_one("#hslice"):
      self.query_one("#htree").focus()
    else:
      self.pop_back()


register_file_view("HDF5", is_hdf5, open_hdf5)
