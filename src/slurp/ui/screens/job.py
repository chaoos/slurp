"""The job window"""

import os
import subprocess
import time
from datetime import datetime

import humanize
from rich.table import Table
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, OptionList, ProgressBar, Select, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from ...cluster import slurm as sl

from ...state import PRESETS, STEP_NAME
from ...cluster.remote import cluster_work, in_background, run, run_cmd, SLURM_ENV, start_worker, tab_name
from ...cluster.jobs import (
  annotate, array_ids_not_running_or_completed, array_task_id, array_task_states, compress_array_ids, eta_text, expand_array_spec, FAILED_JOB_STATES, guess_array_progress, is_time, load_jobs, original_array_spec, original_submission, output_progress, PROGRESS, progress_for_job, progress_path, progress_pattern, progress_state, rate_text, record_progress, restart_args, set_array_pattern, set_progress_file, with_array_spec
)
from ...cluster.details import (
  current_memory_usage, load_details, MEMORY_EVERY, memory_watch, peak_job_memory, record_memory, run_on_nodes, sacct_all
)
from ...cluster.paths import job_vars
from .. import icons
from ..widgets import ClickTable, state_text, TimePlot
from .dialogs import ConfirmScreen, RestartScreen, RunArrayScreen
from .base import SplitScreen
from .files import DirScreen, FileScreen, open_path
from .links import ShellChoiceScreen
from .checks import FindingsView
from ...checks import engine as checks

def node_hours_text(job):
  """The node hours a job used (nodes × elapsed time): bold, with how they come about; for a running job so far and
  at most at the time limit, for a pending one what it may take"""
  rec = job.rec
  try:
    nodes = int(rec["NNodes"] or 0)
  except ValueError:
    nodes = 0
  try:
    limit = sl.hours(rec["Timelimit"]) if rec["Timelimit"] and rec["Timelimit"][0].isdigit() else None
  except ValueError:
    limit = None
  if job.state == "PENDING":
    text = Text("none yet", style="bold")
    if nodes and limit:
      text.append(f"   at most {nodes * limit:.2f} ({nodes} node{'s' if nodes != 1 else ''} × {limit:.2f} h limit)", style="dim")
    return text
  used = job.node_hours()
  text = Text(f"{used:.2f}", style="bold")
  text.append(f"   {nodes} node{'s' if nodes != 1 else ''} × {used / nodes if nodes else 0:.2f} h", style="dim")
  if job.running:
    text.append("   so far", style="yellow")
    if nodes and limit:
      text.append(f", at most {nodes * limit:.2f} at the time limit", style="dim")
  return text

def memory_text(value, fmt="%.1f"):
  """Bytes in binary units (KiB, MiB, GiB), as free -h and nvidia-smi show them (not the decimal kB, MB, GB)"""
  return humanize.naturalsize(value, binary=True, format=fmt)

CPU_SOURCES = {"cgroup": "the job's cgroup: what Slurm counts against --mem",
               "PSS": "the job's processes, PSS: shared memory counted once",
               "RSS": "the job's processes, RSS: shared memory counted in every process",
               "sstat estimate": "sstat's estimate"}

def cpu_memory_text(result):
  """The CPU memory of a query: the job's (and what it is), its processes, the nodes as free shows them"""
  cpu = result["cpu_bytes"]
  if cpu is None:
    return "unavailable"
  lines = [f"{memory_text(cpu)} ({CPU_SOURCES.get(result['cpu_source'], result['cpu_source'])}"
           + (f"; {memory_text(result['cpu_cache'])} of it page cache" if result.get("cpu_cache") else "")
           + (", partial" if result["cpu_partial"] else "") + ")"]
  procs = [f"{memory_text(result[k])} {name}" for k, name in (("cpu_pss", "PSS"), ("cpu_rss", "RSS"))
           if result.get(k) is not None and result["cpu_source"] != name]
  if procs:
    lines.append("the processes: " + ", ".join(procs))
  for node, total, available, free, cache in (result.get("nodes") or [])[:8]:
    lines.append(f"{node} (as free): used {memory_text(total - available)} of {memory_text(total)}, "
                 f"available {memory_text(available)}, buff/cache {memory_text(cache)}")
  return "\n".join(lines)

def gpu_memory_text(result):
  """The GPU memory of a query: the job's, and every GPU's used and total (the numbers of nvidia-smi's table)"""
  gpu, gpus = result["gpu_bytes"], result.get("gpus") or []
  if gpu is None:
    errors = result["gpu_errors"]
    lines = [f"unavailable ({errors[0]})" if errors else "unavailable"] if not gpus else []
  else:
    how = ("the GPUs of the job: the driver does not report its processes" if result.get("gpu_how") == "its GPUs"
           else "processes of the job")
    lines = [f"{memory_text(gpu)} ({how}" + (", partial" if result["gpu_partial"] else "") + ")"]
  if gpus:
    used, total = sum(g[3] for g in gpus), sum(g[4] for g in gpus)
    lines.append(f"GPUs of the nodes (as nvidia-smi): {memory_text(used)} of {memory_text(total)} used ({used / max(total, 1):.0%})")
    lines += [f"  {node} GPU {idx} {name}: {memory_text(u)} / {memory_text(t)}" for node, idx, name, u, t in gpus[:16]]
  return "\n".join(lines)

class JobScreen(SplitScreen):
  """Details, files and steps of one job, commands on its nodes"""
  SHOW_BACK = True
  BINDINGS = [
    Binding("escape,backspace", "back", "Back"),
    Binding("r", "reload", "Reload"),
    Binding("s", "shell", "Shell on node"),
    Binding("c", "categorize", "Categorize"),
  ]

  def __init__(self, job):
    super().__init__()
    self.job = job
    self.details = {"files": [], "sstat": {}, "nodes": []}

  def compose_left(self):
    with Horizontal(id="job-actions"):
      yield Button(icons.label("reload", "Reload"), id="reload-job", compact=True,
                   tooltip="the job's state, times, files and progress again (r)")
      yield Button(icons.label("cancel_job", "Cancel job"), id="cancel-job", variant="error", compact=True)
      yield Button(icons.label("restart", "Restart job"), id="restart-job", variant="primary", compact=True)
      yield Button(icons.label("restart", "Run array"), id="run-array", tooltip="submit the array again with another range", compact=True)
      yield Button(icons.label("array", "Array"), id="array-window", tooltip="all tasks of the array by state, to copy or cancel", compact=True)
    with VerticalScroll(id="details"):
      yield Static(id="info")
      with Horizontal(id="checks-bar"):
        yield Label(icons.label("check", "Checks"), classes="section")
        yield Button(icons.label("check", "Check"), id="checks-run", compact=True,
                     tooltip="all checks of the script and the job: the record, paths, quota and logs, then the script's "
                             "environment (its setup replayed after a fresh login, the programs' libraries)")
        yield Button("Placement", id="checks-placement", compact=True,
                     tooltip="where the tasks and threads of the running job are: cores, NUMA domains, GPUs")
        yield Button("Fixed copy…", id="checks-fix", compact=True,
                     tooltip="the fixes that are safe to make, in a copy of the script next to the job (then Submit there)")
        yield Button("What changed", id="checks-changed", compact=True,
                     tooltip="the script and the command line, compared with the last run of this job that worked")
        yield Button("Rule catalogue", id="checks-catalogue", compact=True)
      yield FindingsView(id="job-checks")
      with Horizontal(id="memory-controls"):
        yield Button("Query current memory", id="memory-query", variant="primary", compact=True)
        yield Label("CPU:")
        yield Static(id="memory-cpu", markup=False)
        yield Label("GPU:")
        yield Static(id="memory-gpu", markup=False)
      with Vertical(id="memory-watch"):
        with Horizontal(id="memory-watch-bar"):
          yield Select(MEMORY_EVERY, value=0, allow_blank=False, id="memory-every",
                       tooltip="query the memory again and again; every query is kept and written to a file", compact=True)
          yield Static(id="memory-watch-status")
        size = lambda v: memory_text(v, "%.3g")
        with Horizontal(classes="memory-plot"):
          yield Label("CPU", classes="memory-plot-label")
          yield TimePlot(color="cyan", fmt=size, id="spark-cpu")
          yield Static(id="spark-cpu-text", classes="memory-plot-text")
        with Horizontal(classes="memory-plot"):
          yield Label("GPU", classes="memory-plot-label")
          yield TimePlot(color="magenta", fmt=size, id="spark-gpu")
          yield Static(id="spark-gpu-text", classes="memory-plot-text")
      with Vertical(id="job-progress"):
        with Horizontal(id="progress-top"):
          yield ProgressBar(total=1, show_eta=False, id="progress-bar")
          yield Button("Progress file…", id="progress-file", compact=True,
                       tooltip="choose the (log) file checked for n/N or M% lines, for this job")
          yield Button("Use stdout", id="progress-stdout", compact=True)
        yield Static(id="progress-line", markup=False)
        yield Static(id="progress-age")
      yield Label(icons.label("folder", "Files"), classes="section")
      yield OptionList(Option("loading ...", disabled=True), id="files")
      yield Label(icons.label("steps", "Steps"), classes="section")
      yield ClickTable(id="steps", cursor_type="row", zebra_stripes=True)
      with Vertical(id="nodes"):
        yield Label("Run on the nodes of the job", classes="section")
        with Horizontal(id="nodebar"):
          yield Input(id="cmd", placeholder="command, e.g. nvidia-smi", compact=True)
          yield Select([], id="node", prompt="all nodes", allow_blank=True, compact=True)
          yield Button("Run", id="run", variant="primary", compact=True)
          yield Button("Shell", id="shell", tooltip="interactive shell on the node, opens on the right", compact=True)
        with Horizontal(id="presets"):
          for i, name in enumerate(PRESETS):
            yield Button(name, id=f"preset{i}", classes="preset", compact=True)

  def on_mount(self):
    self.sub_title = f"Jobs › {self.job.jid}"
    table = self.query_one("#steps", ClickTable)
    for key, label in [("step", "Step"), ("name", "Name"), ("state", "State"), ("exit", "Exit"), ("elapsed", "Elapsed"),
                       ("tasks", "Tasks"), ("nodes", "Nodes"), ("maxrss", "MaxRSS"), ("total", "~Total RSS"), ("cmd", "Command")]:
      table.add_column(label, key=key)
    self.show_job()
    self.query_one("#files").focus()
    self.load()
    self.run_checks()
    self.set_interval(self.PROGRESS_EVERY, self.refresh_progress)
    self.memory = memory_watch(self.job.jid, self.job)
    self.query_one("#memory-every", Select).value = self.memory["every"]
    self.set_interval(1, self.show_memory_watch)
    self.show_memory_watch()
    self.set_interval(1, self.show_progress_age)

  def show_job(self):
    job, rec = self.job, self.job.rec
    self.query_one("#cancel-job", Button).disabled = not (job.queued or job.running)
    self.query_one("#restart-job", Button).disabled = restart_args(job) is None
    self.query_one("#run-array", Button).display = array_task_id(job) is not None and restart_args(job) is not None
    self.query_one("#array-window", Button).display = array_task_id(job) is not None
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    state = state_text(job.state)
    if job.reason and job.reason not in ("None", ""):
      state.append(f"  ({job.reason})", style="dim")
    elif not job.queued and rec["ExitCode"]:
      state.append(f"  exit code {rec['ExitCode']}")
    store = self.app.cluster.categories if self.app.clusters else None
    category = store.category_of(job.jid) if store else ""
    rows = [
      ("Job", f"{job.jid}  {rec['JobName']}"),
      *([("Category", category)] if category else []),
      ("State", state),
      ("User / account", f"{rec['User']} / {rec['Account']}   partition {rec['Partition']}"),
      ("Nodes", f"{rec['NNodes']}  {rec['NodeList'] if rec['NodeList'] not in ('None assigned', '') else ''}"),
      ("Tasks", str(rec["NTasks"] or max((int(s["NTasks"] or 0) for s in job.steps), default=0) or "-")),
      ("Submit", rec["Submit"].replace("T", " ")),
      ("Start", ("~ " if job.state == "PENDING" else "") + rec["Start"].replace("T", " ") if is_time(rec["Start"]) else "-"),
      ("End", rec["End"].replace("T", " ") if is_time(rec["End"]) else "-"),
      ("Elapsed / limit", f"{rec['Elapsed']} / {rec['Timelimit']}"),
      ("Node hours (total)", node_hours_text(job)),
      ("WorkDir", rec["WorkDir"]),
      ("Submitted with", rec["SubmitLine"]),
      ("Command", job.command),
    ]
    state_name = job.state.split()[0].rstrip("+") if job.state else ""
    if not job.queued and state_name in (FAILED_JOB_STATES | {"COMPLETED"}):
      peak_cpu, peak_gpu = peak_job_memory(job)
      rows.append(("Peak CPU memory (Slurm DB)", memory_text(peak_cpu) if peak_cpu else "not reported"))
      if peak_gpu:
        rows.append(("Peak GPU memory (Slurm DB)", memory_text(peak_gpu)))
    for key, val in rows:
      grid.add_row(key, val)
    self.query_one("#info", Static).update(grid)
    self.query_one("#memory-controls", Horizontal).display = job.running
    self.query_one("#memory-query", Button).disabled = not job.running
    self.show_progress()
    self.query_one("#nodes").display = job.running
    self.query_one("#checks-placement", Button).display = job.running
    state = job.state.split()[0].rstrip("+") if job.state else ""
    self.query_one("#checks-changed", Button).display = not job.queued and state in FAILED_JOB_STATES
    self.show_steps()

  PROGRESS_EVERY = 10 # seconds between reads of the progress of a running job
  progress_at = None  # time.monotonic() of the last read
  progress_reading = False

  def show_progress(self):
    job = self.job
    # (a job that ended: where its progress file got to, else the bar of its end state)
    progress = job.progress if job.running or job.progress is not None else progress_for_job(job)
    progress_view = self.query_one("#job-progress", Vertical)
    chosen = PROGRESS.get((tab_name(), job.jid), {}).get("file")
    self.query_one("#progress-stdout", Button).display = bool(chosen)
    if progress is not None or job.running or chosen:
      progress_view.display = True
      self.query_one("#progress-file", Button).display = not job.state.startswith("PENDING") # (running or ended)
      if progress is None:
        self.query_one("#progress-bar", ProgressBar).update(total=None)
        self.query_one("#progress-line", Static).update(
          f"no n/N or M% line in the end of {chosen or job.stdout_path() or 'the output file'}" + (" (yet)" if job.running else ""))
      else:
        self.query_one("#progress-bar", ProgressBar).update(total=max(progress.total, 1), progress=progress.current)
        self.query_one("#progress-line", Static).update(self.progress_text(job, progress, chosen))
    else:
      progress_view.display = False
    self.show_progress_age()

  def show_progress_age(self):
    """When the progress was read (counting up), and how often"""
    age = self.query_one("#progress-age", Static)
    age.display = self.job.running
    if self.job.running:
      ago = "" if self.progress_at is None else f", last {time.monotonic() - self.progress_at:.0f} s ago"
      reading = " · reading …" if self.progress_reading else ""
      age.update(Text(f"updated every {self.PROGRESS_EVERY} s{ago}{reading}", style="dim"))

  def refresh_progress(self):
    """Read the progress of a running job again (in the background)"""
    if not self.job.running or self.progress_reading:
      return
    self.progress_reading = True
    job = self.job
    def read():
      return record_progress(job, output_progress(progress_path(job)))
    def got(progress):
      self.progress_reading = False
      self.progress_at = time.monotonic()
      if job is self.job:
        self.job.progress = progress
        self.show_progress()
    in_background(self, read, got, group="progress")
    self.show_progress_age()

  def progress_text(self, job, progress, chosen):
    share = "" if progress.percent else f" ({progress.current / max(progress.total, 1):.0%})"
    lines = [f"{progress.text}{share}: {progress.line}"]
    est = progress.estimate
    if job.running and progress.current < progress.total:
      if "overall" in est:
        rate, eta = est["overall"]
        lines.append(f"on same pace (since the start): {rate_text(rate)}, done in {eta_text(eta)}")
      if "recent" in est:
        rate, eta, count = est["recent"]
        lines.append(f"on current pace (last {count} updates): {rate_text(rate)}, done in {eta_text(eta)}")
      if not ("overall" in est or "recent" in est):
        lines.append("no estimate yet")
      else:
        lines.append(f"(times: {est['source']})")
    if progress.path and not job.running and not job.state.startswith("PENDING"): # (where it got to: and how it ended)
      state = job.state.split()[0].rstrip("+") if job.state else "?"
      exit_code = job.rec.get("ExitCode") or ""
      lines.append(f"the job ended {state}" + (f" (exit code {exit_code})" if exit_code and exit_code != "0:0" else "")
                   + (" here" if progress.current < progress.total else ""))
    if progress.path:
      lines.append(f"from {progress.path}" + (" (chosen for this job)" if chosen else ""))
    return "\n".join(lines)

  @on(Button.Pressed, "#progress-file")
  def choose_progress_file(self, event):
    event.stop()
    state = progress_state(self.job.jid)
    start = os.path.dirname(state["file"] or self.job.stdout_path() or "") or self.job.rec.get("WorkDir") or "/"
    def picked(path):
      set_progress_file(self.job.jid, path)
      self.guess_array(path)
      self.notify(f"progress of job {self.job.jid} from {path}", timeout=4)
      self.load()
    self.app.push_screen(DirScreen(["Jobs", self.job.jid, "progress file"], start, pick=picked))

  def guess_array(self, path):
    """For an array task: the progress files of the other tasks, from the name of the file chosen for this one"""
    task = array_task_id(self.job)
    if task is None:
      return
    array_id = self.job.jid.partition("_")[0]
    pattern = progress_pattern(path, task, self.job.rec.get("JobIDRaw", ""))
    if pattern is None:
      self.notify(f"the name of {os.path.basename(path)} has neither the array index {task} nor the job id: the progress "
                  "files of the other tasks cannot be guessed", severity="warning", timeout=8)
      return
    set_array_pattern(array_id, pattern)
    tasks = [j for j in self.app.jobs.values() if j.jid.partition("_")[0] == array_id and j.jid != self.job.jid]
    running = [j for j in tasks if j.running]
    def guessed(found):
      if found is None:
        return
      self.notify(f"array {array_id}: progress files found for {len(found)} of {len(running)} other running tasks"
                  + (f" ({len(tasks) - len(running)} not running)" if len(tasks) > len(running) else "")
                  + "; the others are looked for again at every reload", timeout=8)
    in_background(self, lambda: guess_array_progress(running), guessed, group="guess-array")

  @on(Button.Pressed, "#progress-stdout")
  def progress_from_stdout(self, event):
    event.stop()
    set_progress_file(self.job.jid, None)
    self.load()

  def show_steps(self):
    table = self.query_one("#steps", ClickTable)
    table.clear()
    sstat = self.details["sstat"]
    for s in self.job.steps:
      step = s["JobID"].partition(".")[2]
      live = sstat.get(step, {})
      maxrss = sl.sacct_bytes(live.get("MaxRSS") or s["MaxRSS"])
      ntasks = int(s["NTasks"] or 0)
      total = sl.sacct_bytes(live.get("AveRSS") or s["AveRSS"]) * ntasks
      table.add_row(step, s["JobName"], state_text(s["State"]), s["ExitCode"], s["Elapsed"], s["NTasks"], s["NNodes"],
                    memory_text(maxrss) if maxrss else "", memory_text(total) if total else "",
                    s["SubmitLine"], key=s["JobID"])
    if not self.job.steps:
      table.add_row("", "no steps", *[""]*8)

  def show_files(self):
    files = self.query_one("#files", OptionList)
    files.clear_options()
    for i, e in enumerate(self.details["files"]):
      prompt = Text()
      kind = ("folder" if e.kind in ("workdir", "output dir") else "script" if e.kind == "script" else "core" if e.kind == "core"
              else icons.file_kind(os.path.basename(e.path)) if e.path else "file")
      if icons.icon(kind):
        prompt.append(icons.icon(kind) + " ")
      prompt.append(f"{e.kind:<10} ", style="bold")
      openable = (e.path and e.exists) or e.text is not None or e.kind == "sacct"
      prompt.append(e.label, style="" if openable else "dim")
      if e.note:
        prompt.append(f"  {e.note}", style="dim")
      if e.kind == "stderr" and e.path and self.job.err[0] == "file" and self.job.err[1]:
        prompt.append("  !", style="bold red")
      files.add_option(Option(prompt, id=str(i), disabled=not openable))
    if self.details["nodes"]:
      self.query_one("#node", Select).set_options([(n, n) for n in self.details["nodes"]])

  @cluster_work("details")
  def load(self, refresh_job=False):
    job = self.job
    try:
      if refresh_job:
        jobs = annotate(load_jobs(None, jids=[job.jid]))
        job = next((j for j in jobs if j.jid == job.jid), job)
      details = load_details(job)
    except BaseException: # (failed or aborted: the Reload button is back; the error is shown)
      self.app.call_from_thread(self.reload_done)
      raise
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.loaded, job, details)
    else:
      self.app.call_from_thread(self.reload_done)

  def reload_done(self):
    for button in self.query("#reload-job").results(Button):
      button.label, button.disabled = icons.label("reload", "Reload"), False

  def loaded(self, job, details):
    self.reload_done()
    self.job, self.details = job, details
    self.job.progress = details.get("progress")
    self.progress_at = time.monotonic()
    self.show_job()
    self.show_files()
    self.app.show_warnings()

  # ---- checks
  check_context = None
  live = None     # the result of Placement

  def run_checks(self):
    view = self.query_one("#job-checks", FindingsView)
    view.busy(Text("checking the script and the job …", style="dim"))
    self.check_worker(self.job, self.live)

  @cluster_work("checks")
  def check_worker(self, job, live):
    """All checks: the job, its script, paths, quota and logs (shown at once), then the script's environment replayed
    on the login node (seconds), added when done"""
    findings, ctx = checks.check_job(job, full=True, live=live)
    worker = get_current_worker()
    if worker.is_cancelled:
      return
    self.app.call_from_thread(self.checks_done, findings, ctx, ctx.script is not None)
    if ctx.script is None:
      return
    from ...checks.environment import replay_environment
    ctx.replay = replay_environment(ctx)
    findings = checks.run_rules(ctx)
    if not worker.is_cancelled:
      self.app.call_from_thread(self.checks_done, findings, ctx)

  def checks_done(self, findings, ctx, environment_follows=False):
    self.check_context = ctx
    self.job.checks = findings
    source = ""
    if ctx.script is None:
      source = "(no batch script known: only the job record is checked)"
    elif ctx.submitted:
      source = "(the script as submitted)"
    elif ctx.on_disk:
      source = "(the script file as it is now)"
    if environment_follows:
      source += " (checking the environment on the login node …)"
    self.query_one("#job-checks", FindingsView).show(findings, source.strip())

  @on(Button.Pressed, "#checks-run")
  def checks_run_pressed(self, event):
    event.stop()
    self.run_checks()

  @on(Button.Pressed, "#checks-catalogue")
  def checks_catalogue_pressed(self, event):
    event.stop()
    self.app.action_rule_catalogue("")

  def recheck(self):
    self.run_checks()

  @on(Button.Pressed, "#checks-fix")
  def checks_fix_pressed(self, event):
    event.stop()
    ctx = self.check_context
    if ctx is None or ctx.script is None:
      self.notify("no batch script known for this job", severity="warning")
      return
    from ...checks.fixes import apply_fixes, fixed_name
    from .checks import FixPreviewScreen
    from .script import write_fixed_copy
    new, done = apply_fixes(ctx.script.text, self.job.checks or [])
    if not done:
      self.notify("none of the findings can be fixed automatically: see How to fix in each", timeout=6)
      return
    _, script_arg = checks.submit_options(self.job.rec.get("SubmitLine") or "")
    workdir = self.job.rec.get("WorkDir") or ""
    base = os.path.join(workdir, script_arg) if script_arg and not os.path.isabs(script_arg) else script_arg or \
           os.path.join(workdir, f"{self.job.rec.get('JobName') or 'job'}.sh")
    target = fixed_name(base)
    self.app.push_screen(FixPreviewScreen(base, target, ctx.script.text, new, done),
                         lambda ok: write_fixed_copy(self, target, new) if ok else None)

  @on(Button.Pressed, "#checks-changed")
  def checks_changed_pressed(self, event):
    event.stop()
    job = self.job
    def work():
      return checks.what_changed(job)
    def show(text):
      if text:
        self.app.push_screen(FileScreen([f"job {job.jid}", "what changed"], text=text))
    in_background(self, work, show, group="changed")

  @on(Button.Pressed, "#checks-placement")
  def checks_placement_pressed(self, event):
    event.stop()
    if not self.job.running:
      self.notify("the job is not running", severity="warning")
      return
    from .placement import PlacementScreen
    self.app.push_screen(PlacementScreen(self.job), self.placement_done)

  def placement_done(self, live):
    if live:
      self.live = live
      self.run_checks()

  @on(Button.Pressed, "#memory-query")
  def memory_query_pressed(self, event):
    event.stop()
    if not self.job.running:
      return
    self.query_one("#memory-query", Button).disabled = True
    self.query_one("#memory-cpu", Static).update("querying …")
    self.query_one("#memory-gpu", Static).update("querying …")
    self.query_current_memory()

  @cluster_work("current-memory")
  def query_current_memory(self):
    try:
      result = current_memory_usage(self.job)
    except Exception as e:
      result = {"error": str(e)}
    if not get_current_worker().is_cancelled:
      self.app.call_from_thread(self.memory_loaded, result)

  shown_samples = -1

  @on(Select.Changed, "#memory-every")
  def memory_every_changed(self, event):
    if event.value is Select.NULL or event.value == self.memory["every"]:
      return
    self.memory["every"], self.memory["misses"] = event.value, 0
    self.memory["due"] = time.monotonic() # the first query now
    self.show_memory_watch()

  def show_memory_watch(self):
    """Status of the watch (samples, next query, the file) and the plots of CPU and GPU memory over time"""
    w = self.memory
    self.query_one("#memory-watch").display = self.job.running or bool(w["samples"])
    samples = w["samples"]
    parts = [f"{len(samples)} measurement{'s' if len(samples) != 1 else ''}"]
    if samples:
      parts.append(f"since {datetime.fromtimestamp(samples[0][0]):%H:%M}")
    if w["busy"]:
      parts.append("measuring …")
    elif w["every"]:
      left = max(0, w["due"] - time.monotonic())
      parts.append(f"next in {int(left) // 60}:{int(left) % 60:02d}")
    if samples:
      parts.append(f"file {w['file']}")
    text = Text(" · ".join(parts))
    if w["error"]:
      text.append(f" · {w['error']}", style="yellow")
    self.query_one("#memory-watch-status", Static).update(text)
    if len(samples) == self.shown_samples:
      return
    self.shown_samples = len(samples)
    for key, index in (("cpu", 1), ("gpu", 2)):
      values = [s[index] for s in samples if s[index] is not None]
      self.query_one(f"#spark-{key}", TimePlot).set_data([(s[0], s[index]) for s in samples])
      self.query_one(f"#spark-{key}-text", Static).update(
        f"{memory_text(values[-1])} now\nmin {memory_text(min(values))}\nmax {memory_text(max(values))}"
        if values else "-")

  def memory_loaded(self, result):
    if not self.is_mounted:
      return
    record_memory(self.memory, result) # a query by hand is kept too
    self.show_memory_watch()
    if "error" in result:
      self.query_one("#memory-cpu", Static).update(f"unavailable ({result['error']})")
      self.query_one("#memory-gpu", Static).update("unavailable")
      self.query_one("#memory-query", Button).disabled = not self.job.running
      self.app.show_warnings()
      return
    self.details["sstat"] = result["sstat"]
    self.query_one("#memory-cpu", Static).update(cpu_memory_text(result))
    self.query_one("#memory-gpu", Static).update(gpu_memory_text(result))
    self.query_one("#memory-query", Button).disabled = not self.job.running
    self.show_steps()
    self.app.show_warnings()

  def action_back(self):
    self.pop_back()

  @on(Button.Pressed, "#cancel-job")
  def confirm_cancel(self, event):
    event.stop()
    jid = self.job.rec["JobIDRaw"]
    self.app.push_screen(
      ConfirmScreen("Cancel job?", f"Cancel Slurm job {jid}? This cannot be undone.", "Cancel job"),
      lambda confirmed: self.run_job_action("cancel", ["scancel", str(jid)]) if confirmed else None,
    )

  @on(Button.Pressed, "#restart-job")
  def confirm_restart(self, event):
    event.stop()
    args = restart_args(self.job)
    if args is None:
      self.notify("The original sbatch command is not available for this job", severity="warning")
      return
    self.prepare_restart_options(args)

  @on(Button.Pressed, "#array-window")
  def array_window_pressed(self, event):
    event.stop()
    self.app.open_array(self.job.jid.partition("_")[0], self.job)

  @on(Button.Pressed, "#run-array")
  def run_array_pressed(self, event):
    event.stop()
    args = restart_args(self.job)
    if args is None or array_task_id(self.job) is None:
      return
    self.prepare_run_array(args)

  @cluster_work("array-options")
  def prepare_run_array(self, args):
    parent = self.job.jid.partition("_")[0]
    try:
      states = array_task_states(parent)
    except Exception as e:
      states = {}
      self.app.call_from_thread(self.notify, f"the states of the array tasks are unknown: {e}", title=tab_name(), severity="warning")
    try:
      ids = expand_array_spec(original_array_spec(self.job) or "")
    except ValueError:
      ids = sorted(int(i) for i in states if i.isdigit())
    self.app.call_from_thread(self.show_run_array, args, states, ids)

  def show_run_array(self, args, states, ids):
    if not self.is_mounted:
      return
    cwd = self.job.rec.get("WorkDir")
    self.app.push_screen(RunArrayScreen(self.job.jid.partition("_")[0], args, states, ids),
                         lambda a: self.run_job_action("restart", a, cwd=cwd) if a else None)

  @cluster_work("restart-options")
  def prepare_restart_options(self, default_args):
    task_id = array_task_id(self.job)
    retry_args, retry_label = None, "Retry array IDs not running/completed (not an array task)"
    if task_id is not None:
      parent_id = self.job.jid.partition("_")[0]
      try:
        spec = original_array_spec(self.job)
        if not spec:
          raise ValueError("the original array range is unavailable")
        all_ids = expand_array_spec(spec)
        states = array_task_states(parent_id)
        retry_ids = array_ids_not_running_or_completed(all_ids, states)
        if retry_ids:
          retry_spec = compress_array_ids(retry_ids)
          retry_args = with_array_spec(default_args, retry_spec)
          retry_label = f"Retry IDs not running/completed (--array={retry_spec})"
        else:
          retry_label = "Retry IDs not running/completed (none found)"
      except Exception as e:
        retry_label = f"Retry IDs not running/completed (unavailable: {str(e)[:80]})"
    original = original_submission(self.job, default_args)
    self.app.call_from_thread(self.show_restart_options, default_args, retry_args, retry_label, original)

  def show_restart_options(self, default_args, retry_args, retry_label, original=None):
    if not self.is_mounted:
      return
    cwd = self.job.rec.get("WorkDir")
    self.app.push_screen(RestartScreen(self.job.jid, default_args, retry_args, retry_label, original),
                         lambda choice: self.restart(choice, original, cwd) if choice else None)

  def restart(self, choice, original, cwd):
    """Submit again; with the environment (env -i NAME=value ... sbatch) and/or the batch script Slurm kept"""
    args = list(choice["args"])
    script = original["script"] if choice["same_script"] and original["script_arg"] is not None else None
    env = original["env"] if choice["same_env"] else None
    if script is None and env is None:
      self.run_job_action("restart", args, cwd=cwd)
      return
    self.restart_with_original(args, script, original["script_arg"], env, cwd)

  @cluster_work("job-action")
  def restart_with_original(self, args, script, script_arg, env, cwd):
    cwd = cwd if cwd and cwd not in ("(null)", "None") else None
    if script is not None:
      path = os.path.join(cwd or ".", f".slurp-restart-{self.job.jid}.sh")
      res = run_cmd(["sh", "-c", 'printf "%s" "$1" > "$2"', "sh", script, path], cwd=cwd, retry=False)
      if res.returncode:
        self.app.call_from_thread(self.job_action_finished, "restart", res.returncode, "", f"cannot save the original script: {res.stderr}")
        return
      args[script_arg] = path
    if env is not None:
      sbatch = run(["sh", "-c", 'command -v "$1"', "sh", args[0]], quiet=True).strip() # PATH of the job may not have it
      args = ["env", "-i", *(f"{k}={v}" for k, v in env.items()), sbatch or args[0], *args[1:]]
    result = run_cmd(args, cwd=cwd, timeout=60, env=SLURM_ENV if env is None else None, retry=False)
    self.app.call_from_thread(self.job_action_finished, "restart", result.returncode, result.stdout, result.stderr)

  @cluster_work("job-action")
  def run_job_action(self, action, args, cwd=None):
    cwd = cwd if cwd and cwd not in ("(null)", "None") else None
    try:
      result = run_cmd(args, cwd=cwd, timeout=60, env=SLURM_ENV, retry=False)
      self.app.call_from_thread(self.job_action_finished, action, result.returncode, result.stdout, result.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
      self.app.call_from_thread(self.job_action_finished, action, -1, "", str(e))

  def job_action_finished(self, action, returncode, stdout, stderr):
    output = (stdout or stderr).strip()
    if returncode == 0:
      if action == "cancel":
        self.notify(f"Cancellation requested for job {self.job.jid}")
        if self.is_mounted:
          self.load(refresh_job=True)
      else:
        self.notify(f"Restarted {self.job.jid}: {output or 'new job submitted'}")
    else:
      self.notify(f"Could not {action} job {self.job.jid}: {output or 'Slurm command failed'}", severity="error")

  def check_action(self, action, parameters):
    if action == "shell":
      return self.job.running
    return True

  @on(Button.Pressed, "#shell")
  def action_categorize(self):
    """c: this job (an array task: the task) in a category"""
    self.app.categorize_jobs([self.job.jid], after=self.show_job)

  def action_shell(self):
    if not self.job.running:
      self.notify("the job is not running", severity="error")
      return
    node = self.query_one("#node", Select).value
    node = (self.details["nodes"] or [None])[0] if node is Select.NULL else node
    if not node:
      self.notify("the nodes of the job are not known yet, try again in a moment", severity="warning")
      return
    args = ["srun", f"--jobid={self.job.rec['JobIDRaw']}", "--overlap", "--pty", "--nodes=1", "--ntasks=1", "-w", node,
            f"--job-name={STEP_NAME}", "bash", "-l"]
    title = f"shell on {node} · job {self.job.jid}"
    def chosen(where): # (a node: only a shell from the own terminal, never a drive)
      if where == "slurp":
        self.app.open_shell(args, title)
      elif where == "own":
        cluster = self.app.cluster
        self.app.open_link_setup("shell", title, cluster, self.app.link_argv(cluster, args))
    self.app.push_screen(ShellChoiceScreen(f"Shell on {node} (job {self.job.jid})"), chosen)

  @on(Button.Pressed, "#reload-job")
  def reload_pressed(self, event):
    event.stop()
    self.action_reload()

  def action_reload(self):
    """The job from Slurm again, with its files and progress (the button says so meanwhile)"""
    button = self.query_one("#reload-job", Button)
    button.label, button.disabled = icons.label("reload", "Reloading …"), True
    self.load(refresh_job=True)

  def context(self):
    """Base directories and variables to resolve paths in texts of this job"""
    return [self.job.rec["WorkDir"]], job_vars(self.job)

  def open_entry(self, e):
    bases, env = self.context()
    crumbs = ["Jobs", self.job.jid]
    key = f"text {self.job.jid} {e.kind} {e.label}" # (a text of the job, as a file: a subtab of its own, once)
    if e.kind == "sacct":
      job = self.job
      in_background(self, lambda: sacct_all(job), lambda text: self.app.in_new_subtab(lambda: self.app.open_window(
        FileScreen([*crumbs, e.label], text=text, refresher=lambda: sacct_all(job), bases=bases, env=env)), key=key)
        if text is not None else None, group="sacct")
    elif e.path:
      open_path(self.app, crumbs, e.path, bases, env, follow=self.job.running and e.kind in ("stdout", "stderr", "progress"))
    elif e.text is not None:
      self.app.in_new_subtab(lambda: self.app.open_window(FileScreen([*crumbs, e.label], text=e.text, bases=bases, env=env)), key=key)

  @on(OptionList.OptionSelected, "#files")
  def file_selected(self, event):
    self.open_entry(self.details["files"][int(event.option_id)])

  @on(DataTable.RowSelected, "#steps")
  def step_selected(self, event):
    event.stop()
    self.query_one("#steps", ClickTable).take_clicked_column()
    step = next((s for s in self.job.steps if s["JobID"] == event.row_key.value), None)
    if step:
      width = max(map(len, step))
      text = "\n".join(f"{k:<{width}}  {v}" for k, v in step.items() if v) + "\n"
      bases, env = self.context()
      self.app.push_screen(FileScreen(["Jobs", self.job.jid, f"step {step['JobID']}"], text=text, bases=bases, env=env))

  @on(Button.Pressed, "#run")
  @on(Input.Submitted, "#cmd")
  def run_pressed(self, event):
    self.run_command(self.query_one("#cmd", Input).value.strip())

  @on(Button.Pressed, ".preset")
  def preset_pressed(self, event):
    cmd = list(PRESETS.values())[int(event.button.id.removeprefix("preset"))]
    self.query_one("#cmd", Input).value = cmd
    self.run_command(cmd)

  def run_command(self, cmd):
    if not cmd:
      self.notify("enter a command first", severity="warning")
      return
    if not self.job.running:
      self.notify("the job is not running", severity="error")
      return
    node = self.query_one("#node", Select).value
    node = None if node is Select.NULL else node
    self.notify(f"running '{cmd}' on {node or 'all nodes'} ...")
    start_worker(self, lambda: self.command_done(cmd, node, run_on_nodes(self.job, cmd, node)), "nodes", exclusive=False)

  def command_done(self, cmd, node, text):
    job = self.job
    crumbs = ["Jobs", job.jid, f"{cmd} @ {node or 'all nodes'}"]
    bases, env = self.context()
    self.app.call_from_thread(self.app.push_screen, FileScreen(crumbs, text=text, refresher=lambda: run_on_nodes(job, cmd, node),
                                                               bases=bases, env=env))
