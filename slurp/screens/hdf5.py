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
H5_ROWS = 500       # rows of a dataset shown at a time
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

class H5Screen(SplitScreen):
  """HDF5 browser: tree of groups and datasets, info and attributes of the selected one, and a table of its data"""
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
    self.local_path = None
    self.current = None # data of the selected tree node
    self.dataset_selections = {} # dataset path -> selection text typed for it
    self.row0 = self.col0 = 0

  def compose_left(self):
    yield Static(id="hstatus")
    with Horizontal(id="hbody"):
      yield Tree(os.path.basename(self.path), id="htree", data={"path": "/", "kind": "group"})
      with Vertical(id="hright"):
        with VerticalScroll(id="hinfo"):
          yield Static(id="hinfotext")
        yield Input(id="hslice", placeholder="selection, e.g. [0, :, 10:20] or [..., 3] (Enter: apply)")
        yield Static(id="hpage")
        yield DataTable(id="hdata", zebra_stripes=True)

  def on_mount(self):
    self.sub_title = crumb_title(self.crumbs)
    self.query_one("#htree", Tree).show_root = True
    self.action_reload(first=True)
    self.query_one("#htree").focus()

  def on_unmount(self):
    self.gone = True
    self.close_file()

  gone = False

  def close_file(self):
    if self.file is not None:
      try:
        self.file.close()
      except Exception:
        pass
      self.file = None
    if self.local_path:
      try: os.unlink(self.local_path)
      except OSError: pass
      self.local_path = None

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
    self.query_one("#hstatus", Static).update(f"{self.path} · {'downloading' if conn() else 'opening'} …")
    def fetch():
      """the size, and on a remote cluster a local copy (h5py reads local files)"""
      try:
        size = fs_getsize(self.path)
        local = conn().download(self.path, suffix=os.path.splitext(self.path)[1]) if conn() else None
        if local and self.gone: # the window was closed meanwhile
          os.unlink(local)
          local = None
        return size, local, ""
      except OSError as e:
        return None, None, str(e)
    in_background(self, fetch, lambda result: self.fetched(result, expanded, target), group="fetch")

  def fetched(self, result, expanded, target):
    size, local_path, error = result or (None, None, "failed")
    tree = self.query_one("#htree", Tree)
    self.close_file()
    self.local_path = local_path
    tree.clear()
    tree.root.data = {"path": "/", "kind": "group"}
    try:
      if error:
        raise OSError(error)
      self.file = open_h5(local_path or self.path)
    except Exception as e:
      self.query_one("#hstatus", Static).update(f"{self.path} · cannot open: {e}")
      self.show_message(f"cannot open {self.path}:\n{e}")
      return
    size = humanize.naturalsize(size)
    self.query_one("#hstatus", Static).update(f"{self.path}  {size} · r: reload (e.g. while a job writes it)")
    self.load_children(tree.root)
    tree.root.expand()
    for p in sorted(expanded, key=lambda p: p.count("/")):
      node = self.find_node(p)
      if node is not None:
        self.load_children(node)
        node.expand()
    self.goto(target)

  def find_node(self, path, expand=False):
    """Tree node of a path; with expand, the groups on the way are expanded"""
    node = self.query_one("#htree", Tree).root
    for name in [p for p in path.split("/") if p]:
      self.load_children(node)
      if expand:
        node.expand()
      child = next((c for c in node.children if c.data and c.data.get("name") == name), None)
      if child is None:
        return node if expand else None
      node = child
    return node

  def goto(self, path):
    node = self.find_node(path or "/", expand=True)
    tree = self.query_one("#htree", Tree)
    self.call_after_refresh(lambda: tree.move_cursor(node))
    self.show(node.data)

  def load_children(self, node):
    import h5py
    data = node.data
    if not data or data["kind"] != "group" or data.get("loaded") or self.file is None:
      return
    data["loaded"] = True
    try:
      group = self.file[data["path"]]
      names = list(group)
    except Exception as e:
      node.add_leaf(Text(f"cannot list: {e}", style="red"), data={"path": data["path"], "kind": "error", "error": str(e)})
      return
    for name in names[:H5_MEMBERS]:
      path = data["path"].rstrip("/") + "/" + name
      item = {"path": path, "name": name}
      try:
        link = group.get(name, getlink=True)
      except Exception:
        link = None
      if isinstance(link, h5py.ExternalLink):
        item.update(kind="external", file=link.filename, target=link.path)
        node.add_leaf(Text.assemble((name, "bold magenta"), (f"  ⇢ {link.filename}:{link.path}", "dim")), data=item)
        continue
      soft = f"  → {link.path}" if isinstance(link, h5py.SoftLink) else ""
      try:
        obj = group[name]
      except Exception as e:
        item.update(kind="error", error=f"broken link{soft}: {e}")
        node.add_leaf(Text.assemble((name, "red"), (soft or "  (cannot open)", "dim")), data=item)
        continue
      if isinstance(obj, h5py.Group):
        item["kind"] = "group"
        node.add(Text.assemble((name + "/", "bold blue"), (soft, "dim")), data=item)
      elif isinstance(obj, h5py.Dataset):
        item["kind"] = "dataset"
        shape = "scalar" if obj.shape == () else "empty" if obj.shape is None else "×".join(map(str, obj.shape))
        node.add_leaf(Text.assemble(name, (soft, "dim"), (f"  {shape} {obj.dtype}", "dim")), data=item)
      else:
        item["kind"] = "datatype"
        node.add_leaf(Text.assemble(name, (soft, "dim"), ("  type", "dim")), data=item)
    if len(names) > H5_MEMBERS:
      node.add_leaf(Text(f"… {len(names) - H5_MEMBERS} more members not listed", style="dim"), data=None)

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
    self.show_table(None)

  def show(self, data):
    self.current, self.row0, self.col0 = data, 0, 0
    if not data:
      return
    kind = data["kind"]
    if kind == "error":
      return self.show_message(Text(f"{data['path']}\n{data['error']}", style="red"))
    if kind == "external":
      target = h5_link_target(self.path, data["file"])
      return self.show_message(Text.assemble((data["path"], "bold"), "\nexternal link to ", (f"{data['target']}", "bold"),
                                             " in ", (target, "bold"), "\nEnter or click: open it"))
    try:
      obj = self.file[data["path"]]
      text = self.describe(obj, kind)
    except Exception as e:
      return self.show_message(Text(f"{data['path']}\ncannot read: {e}", style="red"))
    self.query_one("#hinfotext", Static).update(text)
    if kind == "dataset" and obj.shape:
      inp = self.query_one("#hslice", Input)
      inp.value = self.dataset_selections.get(data["path"], "")
    self.show_data()

  def describe(self, obj, kind):
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
        t.append(self.read_scalar(obj))
    else:
      t.append(f"\nnamed datatype {obj.dtype}")
    if obj.file.filename != self.file.filename:
      t.append(f"\nin file {obj.file.filename}")
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

  def read_scalar(self, obj):
    import h5py
    try:
      ds = obj.asstr() if h5py.check_string_dtype(obj.dtype) else obj
      return h5_value(ds[()], width=2000)
    except Exception as e:
      return f"cannot read: {e}"

  def show_table(self, rows, columns=(), status=""):
    table = self.query_one("#hdata", DataTable)
    table.clear(columns=True)
    sliced = rows is not None and self.current and self.current["kind"] == "dataset"
    self.query_one("#hslice", Input).display = bool(sliced and self.file[self.current["path"]].shape)
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
    """Table with the rows and columns of the current page of the selected dataset"""
    import h5py
    data = self.current
    if not data or data["kind"] != "dataset":
      return self.show_table(None)
    ds = self.file[data["path"]]
    if not ds.shape: # scalar or empty: shown in the info
      return self.show_table(None)
    text = self.query_one("#hslice", Input).value
    try:
      sel = parse_selection(text, ds.shape)
      sel, shown = window(sel, ds.shape, self.row0, self.col0)
    except ValueError as e:
      return self.show_table([], status=Text(str(e), style="red"))
    if any(not len(part) for part, _ in shown):
      return self.show_table([], status="nothing selected")
    try:
      src = ds.asstr() if h5py.check_string_dtype(ds.dtype) else ds
      values = src[sel]
    except Exception as e:
      return self.show_table([], status=Text(f"cannot read: {e}", style="red"))
    names = ds.dtype.names
    status = []
    if len(shown) == 0: # every axis fixed by an index: a single value
      return self.show_table([("", [h5_value(values)])], ["value"])
    rows_part, nrows = shown[0]
    status.append(f"rows {rows_part[0]}–{rows_part[-1]} ({len(rows_part)} of {nrows}" + (", [ ]: page" if len(rows_part) < nrows else "") + ")")
    if len(shown) == 2:
      cols_part, ncols = shown[1]
      columns = [str(i) for i in cols_part]
      status.append(f"columns {cols_part[0]}–{cols_part[-1]} ({len(cols_part)} of {ncols}" + (", { }: page" if len(cols_part) < ncols else "") + ")")
      rows = [(str(i), [h5_value(v, 60) for v in row]) for i, row in zip(rows_part, values)]
    elif names: # one axis of a compound type: a column per field
      fields = names[self.col0:self.col0 + H5_COLS]
      columns = list(fields)
      if len(names) > H5_COLS:
        status.append(f"fields {self.col0}–{self.col0 + len(fields) - 1} of {len(names)}, {{ }}: page")
      rows = [(str(i), [h5_value(v[f], 60) for f in fields]) for i, v in zip(rows_part, values)]
    else:
      columns = ["value"]
      rows = [(str(i), [h5_value(v, 200)]) for i, v in zip(rows_part, values)]
    self.show_table(rows, columns, " · ".join(status))

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
    if not self.current or self.current["kind"] != "dataset":
      return
    self.row0 = max(0, self.row0 + rows * H5_ROWS)
    self.col0 = max(0, self.col0 + cols * H5_COLS)
    try:
      ds = self.file[self.current["path"]]
      sel = parse_selection(self.query_one("#hslice", Input).value, ds.shape)
      _, shown = window(sel, ds.shape)
      if shown: # do not page past the end
        self.row0 = min(self.row0, max(0, (shown[0][1] - 1) // H5_ROWS * H5_ROWS))
        ncols = shown[1][1] if len(shown) == 2 else len(ds.dtype.names or ())
        self.col0 = min(self.col0, max(0, (ncols - 1) // H5_COLS * H5_COLS))
    except Exception:
      pass
    self.show_data()

  def action_back(self):
    if self.focused is self.query_one("#hslice"):
      self.query_one("#htree").focus()
    else:
      self.pop_back()


register_file_view("HDF5", is_hdf5, open_hdf5)
