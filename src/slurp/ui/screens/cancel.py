"""Cancelling a job or an array: the dialog asks why (a list of the usual reasons, or your own words), kept with the
job (cluster/categories.py, on the cluster) and shown in its window and in the categories window"""

from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, RadioButton, RadioSet, Static
from textual.widgets.option_list import Option

DEPENDENCY_GUIDE = "arrays-and-dependencies" # (the heading of the guides on dependencies)

# (key, label, hint shown when it is chosen, wants: "job" a related job, "text" your own words)
REASONS = [
  ("duplicate", "Submitted twice by mistake", "Which job is the one that stays? slurp proposes those that look the same "
   "(name, directory, command).", "job"),
  ("hung", "The program hangs (no progress, no output)", "Next time: srun --time or a timeout in the script stops a hung "
   "step; the job window's runtime diagnostics show whether it still computes.", ""),
  ("allocation", "Wrong resources asked for (nodes, tasks, memory, time, GPUs, array range)", "Before submitting: "
   "Check in the script view finds many of these, sbatch --test-only says what Slurm would give.", ""),
  ("account", "Wrong account or partition", "The job list's Accounts shows yours; #SBATCH --account and --partition "
   "in the script (or the submit line) choose them.", ""),
  ("dependency", "A job it depends on failed or did not finish", "Slurm can do this for you: sbatch "
   "--dependency=afterok:<job id> starts it only when the other one succeeded, --kill-on-invalid-dep=yes cancels it "
   "when that one failed.", "job"),
  ("input", "Wrong input, parameters or configuration", "", ""),
  ("bug", "A bug in the script or the program", "", ""),
  ("superseded", "Replaced by a newer job (or version of the code)", "Which job replaces it?", "job"),
  ("slow", "Runs much slower than expected", "The job window's Placement and runtime diagnostics show where the time "
   "goes (idle CPUs, GPUs, waiting for I/O).", ""),
  ("resources", "Will run out of memory, disk or quota", "The job window's memory and the quota (Q in the job list) "
   "show how close it is.", ""),
  ("queue", "Waits too long in the queue", "A shorter time limit, fewer nodes or another partition often start sooner "
   "(backfill); sbatch --test-only shows the expected start.", ""),
  ("cluster", "A problem of the cluster (maintenance, file system, nodes)", "", ""),
  ("done", "Not needed any more (got the results, converged early)", "", ""),
  ("test", "Only a test", "", ""),
  ("other", "Another reason (say it below)", "", "text"),
]
LABELS = {key: label for key, label, _, _ in REASONS}

def related_jobs(job, jobs, limit=12):
  """Jobs of the job list that look like this one (a duplicate, the one it depends on, …): the same name, directory
  or command; the likeliest first ([job])"""
  rec = job.rec
  mine = job.jid.partition("_")[0]
  def score(other):
    r = other.rec
    same = [r.get("JobName") and r.get("JobName") == rec.get("JobName"),
            r.get("WorkDir") and r.get("WorkDir") == rec.get("WorkDir"),
            other.command and other.command == job.command]
    return sum(bool(x) for x in same)
  seen, out = set(), []
  for other in jobs:
    top = other.jid.partition("_")[0]
    if top == mine or top in seen:
      continue
    s = score(other)
    if s:
      seen.add(top)
      busy = 0 if (other.queued or other.running) else 1
      out.append(((-s, busy, abs(submit_seconds(other) - submit_seconds(job))), other))
  return [o for _, o in sorted(out, key=lambda x: x[0])][:limit]

def job_choices(job, jobs):
  """The jobs to name with a reason (the one it duplicates, depends on, …): those that look like it first, then all
  other jobs of the job list, newest first (an array once); [(job, alike)]"""
  alike = related_jobs(job, jobs, limit=None)
  mine = job.jid.partition("_")[0]
  seen = {mine} | {j.jid.partition("_")[0] for j in alike}
  rest = []
  for other in sorted(jobs, key=submit_seconds, reverse=True):
    top = other.jid.partition("_")[0]
    if top not in seen:
      seen.add(top)
      rest.append(other)
  return [(j, True) for j in alike] + [(j, False) for j in rest]

def choice_id(job):
  return job.jid.partition("_")[0] if "_" in job.jid else job.jid

def submit_seconds(job):
  try:
    return datetime.fromisoformat(job.rec.get("Submit") or "").timestamp()
  except ValueError:
    return 0

def job_label(job):
  r = job.rec
  state = job.state.split()[0] if job.state else ""
  return f"{choice_id(job)}  {r.get('JobName') or ''}  {state}  submitted {(r.get('Submit') or '').replace('T', ' ')}"

def choice_matches(job, words):
  """Does a job match what was typed (every word in its id, name, state, account, directory or command)?"""
  r = job.rec
  hay = " ".join(str(x) for x in (job.jid, r.get("JobName"), job.state, r.get("Account"), r.get("Partition"),
                                  r.get("WorkDir"), job.command) if x).lower()
  return all(w in hay for w in words)

def reason_text(entry, link=True):
  """Why it was cancelled, for the job window: the reason, the details, the related job (a link), the guide (a link)"""
  if not entry:
    return Text("")
  if entry.get("reason") == "other": # (in your own words)
    out = Text(entry.get("text") or "another reason")
  else:
    out = Text(entry.get("label") or LABELS.get(entry.get("reason"), entry.get("reason", "")))
    if entry.get("text"):
      out.append(f": {entry['text']}")
  if entry.get("what"):
    out.append(f" ({entry['what']})", style="dim")
  rel = entry.get("related")
  if rel:
    word = {"duplicate": "the same as", "superseded": "replaced by", "dependency": "depends on"}.get(entry.get("reason"), "see")
    out.append(f"  · {word} ")
    out.append(f"job {rel}", style=Style(underline=True) + Style.from_meta({"@click": f"app.open_job('{rel}')"})
               if link else "")
  if entry.get("reason") == "dependency" and link:
    out.append("  · ")
    out.append("dependencies in the guides →", style=Style(underline=True) + Style.from_meta({"@click": f"app.guides('{DEPENDENCY_GUIDE}')"}))
  if entry.get("at"):
    out.append(f"  ({entry['at']})", style="dim")
  return out

def short_reason(entry):
  """One line of it, for a table"""
  if not entry:
    return ""
  text = entry.get("label") or LABELS.get(entry.get("reason"), "")
  if entry.get("reason") == "other" and entry.get("text"):
    text = entry["text"]
  elif entry.get("text"):
    text += f": {entry['text']}"
  if entry.get("related"):
    text += f" ({entry['related']})"
  return text if len(text) <= 60 else text[:59] + "…"


class CancelScreen(ModalScreen[dict | None]):
  """Cancel? and why: dismissed with {"reason": {…} or None} (cancel it, with the reason or none) or None (do not
  cancel)"""
  DEFAULT_CSS = """
  CancelScreen { align: center middle; }
  #cr-dialog { width: 110; max-width: 95%; height: auto; max-height: 95%; border: thick $error; background: $surface; padding: 1 2; }
  #cr-title { text-style: bold; height: auto; }
  #cr-message { height: auto; margin-bottom: 1; }
  #cr-body { height: auto; max-height: 26; }
  #cr-why { text-style: bold; height: auto; }
  #cr-reasons { height: auto; width: 1fr; border: none; padding: 0; }
  #cr-hint { height: auto; color: $text-muted; margin: 1 0 0 0; }
  #cr-hint.-empty { display: none; }
  #cr-related { margin: 1 0 0 0; width: 1fr; height: auto; }
  #cr-related-list { height: auto; max-height: 8; }
  #cr-related-chosen { height: auto; }
  #cr-guide { margin: 1 0 0 0; }
  #cr-details { margin: 1 0 0 0; }
  #cr-error { color: $error; height: auto; }
  #cr-buttons { height: auto; align-horizontal: right; margin-top: 1; }
  #cr-buttons Button { margin-left: 1; }
  """
  BINDINGS = [Binding("escape", "back", "Do not cancel")]

  SHOWN = 50 # jobs listed at most (the search narrows them down)

  def __init__(self, title, message, ok_label, related=()):
    super().__init__()
    self.title_text, self.message, self.ok_label = title, message, ok_label
    # (the jobs to choose from for a reason that names one: [(job, alike)], see job_choices; plain jobs: all alike)
    self.related = [r if isinstance(r, tuple) else (r, True) for r in related]
    self.reason = None # (the key chosen; None: not said)
    self.related_jid = "" # (the job chosen)

  @property
  def dialog_message(self): # (as ConfirmScreen)
    return self.message

  def compose(self) -> ComposeResult:
    with Vertical(id="cr-dialog"):
      yield Static(self.title_text, id="cr-title", markup=False)
      yield Static(self.message, id="cr-message", markup=False)
      with VerticalScroll(id="cr-body"):
        yield Static("Why? (kept with the job: its window and its category show it; you may also not say)", id="cr-why")
        with RadioSet(id="cr-reasons"):
          for key, label, _, _ in REASONS:
            yield RadioButton(label, name=key)
        yield Static(id="cr-hint", classes="-empty", markup=False)
        with Vertical(id="cr-related"):
          yield Input(id="cr-related-search", placeholder="which job? type to search: id, name, state, directory …")
          yield OptionList(id="cr-related-list", compact=True)
          yield Static(id="cr-related-chosen")
        yield Button("Dependencies in the guides", id="cr-guide", compact=True)
        yield Input(id="cr-details", placeholder="details (optional): what happened, what to do next time")
      yield Static(id="cr-error")
      with Horizontal(id="cr-buttons"):
        yield Button("Do not cancel", id="cr-back", compact=True)
        yield Button(self.ok_label, id="cr-ok", variant="error", compact=True)

  def on_mount(self):
    self.show_jobs()
    self.show_choice()
    self.query_one("#cr-reasons", RadioSet).focus()

  def show_jobs(self):
    """The jobs that match the search (the likely ones first: marked ★)"""
    words = self.query_one("#cr-related-search", Input).value.lower().split()
    found = [(j, alike) for j, alike in self.related if not words or choice_matches(j, words)]
    box = self.query_one("#cr-related-list", OptionList)
    box.clear_options()
    box.add_options([Option(Text.assemble(("★ " if alike else "  ", "yellow"), job_label(j)), id=choice_id(j))
                     for j, alike in found[:self.SHOWN]])
    if found:
      box.highlighted = 0
    self.show_chosen(len(found))

  def show_chosen(self, found=None):
    chosen = next((j for j, _ in self.related if choice_id(j) == self.related_jid), None)
    text = Text()
    if chosen is not None:
      text.append("chosen: ", style="bold")
      text.append(job_label(chosen))
    else:
      text.append("none chosen (Enter or a click chooses one; none: leave it)", style="dim")
    if found is not None and found > self.SHOWN:
      text.append(f"  · {found} match, the first {self.SHOWN} listed: type more", style="dim")
    elif found == 0:
      text.append("  · no job of the job list matches", style="yellow")
    self.query_one("#cr-related-chosen", Static).update(text)

  @on(Input.Changed, "#cr-related-search")
  def search_changed(self, event):
    event.stop()
    self.show_jobs()

  @on(Input.Submitted, "#cr-related-search")
  def search_submitted(self, event):
    event.stop()
    box = self.query_one("#cr-related-list", OptionList)
    if box.highlighted is not None and box.option_count:
      self.related_jid = box.get_option_at_index(box.highlighted).id
      self.show_chosen()

  @on(OptionList.OptionSelected, "#cr-related-list")
  def job_chosen(self, event):
    event.stop()
    self.related_jid = event.option.id
    self.show_chosen()

  def show_choice(self):
    entry = next((r for r in REASONS if r[0] == self.reason), None)
    hint = self.query_one("#cr-hint", Static)
    hint.update(entry[2] if entry else "")
    hint.set_class(not (entry and entry[2]), "-empty")
    self.query_one("#cr-related").display = bool(entry and entry[3] == "job" and self.related)
    self.query_one("#cr-guide", Button).display = self.reason == "dependency"
    details = self.query_one("#cr-details", Input)
    details.placeholder = ("the reason, in your words" if self.reason == "other" else
                           "details (optional): what happened, what to do next time")

  @on(RadioSet.Changed, "#cr-reasons")
  def reason_changed(self, event):
    event.stop()
    self.reason = event.pressed.name
    self.query_one("#cr-error", Static).update("")
    self.show_choice()

  @on(Button.Pressed, "#cr-guide")
  def guide(self, event):
    event.stop()
    self.app.action_guides(DEPENDENCY_GUIDE)

  def answer(self):
    """The reason as kept, None if none was said"""
    text = self.query_one("#cr-details", Input).value.strip()
    if self.reason is None and not text:
      return None
    wants_job = next((r[3] for r in REASONS if r[0] == self.reason), "") == "job"
    reason = self.reason or "other"
    return {"reason": reason, "label": "" if reason == "other" else LABELS[reason], "text": text,
            "related": self.related_jid if wants_job and self.related else "",
            "at": datetime.now().strftime("%Y-%m-%d %H:%M")}

  @on(Button.Pressed, "#cr-ok")
  @on(Input.Submitted, "#cr-details")
  def ok(self, event):
    event.stop()
    if self.reason == "other" and not self.query_one("#cr-details", Input).value.strip():
      self.query_one("#cr-error", Static).update("say the reason below (or choose one of the list)")
      self.query_one("#cr-details", Input).focus()
      return
    self.dismiss({"reason": self.answer()})

  @on(Button.Pressed, "#cr-back")
  def action_back(self, event=None):
    if event is not None:
      event.stop()
    self.dismiss(None)


def keep_reason(app, cluster, jid, reason, what="", after=None):
  """After the cancel worked: the reason with the job (on the cluster, see change_categories); then after() (the
  window shows it)"""
  if not reason:
    return
  from .categories import change_categories
  entry = dict(reason, **({"what": what} if what else {}))
  def kept(store, result):
    if after is not None:
      after()
    if app.main is not None:
      app.call_later(app.populate)
  change_categories(app, cluster, ["cancel_reason", jid, entry], kept, "keeping why the job was cancelled")
