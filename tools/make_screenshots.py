"""Make the screenshots of the docs again (docs/images/overview.svg, jobs.svg, job-details.svg, files.svg): slurp against the test lab
(tests/e2e/lab: a fake Slurm cluster behind a local sshd, sample jobs and files), saved by Textual as SVG. Run it after
changing what these windows show:

    python tools/make_screenshots.py

It needs what the e2e tests need: this checkout installed (pip install -e ".[all,dev]"), Python 3.12, and OpenSSH
(ssh, sshd, ssh-keygen). The lab is built in a temporary directory and removed again; your user name, which the lab
cluster shows (it is this machine), is replaced in the pictures by a sample name."""
import getpass
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = os.path.join(ROOT, "docs", "images")
sys.path.insert(0, os.path.join(ROOT, "tests", "e2e")) # (the lab and the session of the UI tests)

SIZE = (170, 45) # the terminal: columns, rows (as in the UI tests)
WORK = "/tmp/slurp-docs" # the lab (a fixed place: the paths in the pictures stay the same)
SAMPLE_NAMES = {3: "ada", 4: "alex", 5: "alice", 6: "robert", 7: "charlie", 8: "jennifer"}


def no_check(name, cond, info=""):
  return cond


# the node of the lab is this machine: for the pictures, a node name and GPUs of a cluster node instead of its own
FAKE_HOSTNAME = "#!/bin/sh\necho nid000001\n"
FAKE_NVIDIA_SMI = """#!/usr/bin/env python3
import os, sys
def parent(pid):
  return int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
args = " ".join(sys.argv[1:])
if "--query-compute-apps" in args: # the job's processes: the shell of the probe, as scontrol listpids gives it
  pid = parent(os.getppid())
  for gpu in range(4):
    print(f"{pid}, {61440 + 1024 * gpu}")
elif "--query-gpu" in args:
  for gpu in range(4):
    print(f"{gpu}, NVIDIA A100-SXM4-80GB, {62012 + 1024 * gpu}, 81920")
"""


def node_fakes(lab):
  for name, text in (("hostname", FAKE_HOSTNAME), ("nvidia-smi", FAKE_NVIDIA_SMI)):
    path = os.path.join(lab.dir, "fakebin", name)
    with open(path, "w") as f:
      f.write(text)
    os.chmod(path, 0o755)


def sample_hdf5(path):
  """A small simulation output: groups, datasets with attributes, and a Markdown note"""
  import h5py
  import numpy as np
  with h5py.File(path, "w") as f:
    f.attrs["code"] = "lattice-sim 2.4"
    f.attrs["created"] = "2026-11-13T10:01:07"
    f["README.md"] = "# Run 9001\n\nStrong scaling, **L = 64**, 2 nodes, 8 GPUs.\n\n- `observables/`: per trajectory\n- `fields/`: the last configuration"
    obs = f.create_group("observables")
    t = np.arange(200)
    obs.create_dataset("plaquette", data=0.5935 + 0.002 * np.sin(t / 7.0) + 0.0005 * np.cos(t / 3.0))
    obs["plaquette"].attrs["unit"] = "1"
    obs.create_dataset("energy", data=np.round(1.2e3 - 40 * np.exp(-t / 30.0), 3))
    obs["energy"].attrs["unit"] = "GeV"
    fields = f.create_group("fields")
    fields.create_dataset("gauge", data=np.random.default_rng(7).standard_normal((16, 16, 4)).astype("float32"))
    fields["gauge"].attrs["lattice"] = [16, 16, 4]


async def shot_overview(app, pilot):
  """The overview of a tab: health, your jobs, occupancy, your quotas, the partitions"""
  app.show_overview()
  await U.until(pilot, lambda: "info" in app.cluster.overview and "quota" in app.cluster.overview
                and not app.cluster.overview.get("loading"), 60)
  await pilot.pause(1.0)
  app.save_screenshot(filename="overview.svg", path=IMAGES)
  app.show_view(None); await pilot.pause(0.3)


async def shot_jobs(app, pilot):
  """The job list: running, pending and finished jobs, progress, an array of 150 tasks as one row"""
  U.arrays(True)
  await U.reload(app, pilot)
  await U.until(pilot, lambda: app.cluster.loader is None and all(j.annotated for j in app.cluster.jobs.values()), 60)
  # newest first: queued jobs, then the running ones with their estimates, the arrays, then the finished ones
  app.sort_column, app.sort_reverse = "start", False
  app.sort_by(type("Event", (), {"column_key": type("Key", (), {"value": "start"})()})())
  await pilot.pause(2.0)
  table = app.main.query_one("#jobs")
  table.focus()
  table.move_cursor(row=0)
  table.scroll_home(animate=False)
  table.refresh()
  await pilot.pause(1.0)
  app.save_screenshot(filename="jobs.svg", path=IMAGES)


async def shot_job(app, pilot):
  """The window of a running job: accounting, progress with the estimates, files, memory queried"""
  js = await U.open_job(app, pilot, "9001")
  js.query_one("#memory-query").scroll_visible(); await pilot.pause(0.3)
  await pilot.click("#memory-query")
  await U.until(pilot, lambda: "RSS" in str(js.query_one("#memory-cpu").render()), 30)
  js.scroll_home(animate=False)
  for scroller in js.query("VerticalScroll"):
    scroller.scroll_home(animate=False)
  await pilot.pause(1.0)
  app.save_screenshot(filename="job-details.svg", path=IMAGES)
  app.show_view(None); await pilot.pause(0.3)


async def shot_files(app, pilot):
  """The HDF5 view of a job's output: the tree of groups and datasets, a dataset's attributes and data"""
  path = os.path.join(U.LAB, "jobs", "9001", "run9001.h5")
  sample_hdf5(path)
  U.open_path(app, ["Jobs", "9001"], path)
  await U.until(pilot, lambda: type(app.screen).__name__ == "H5Screen", 20)
  scr = app.screen
  tree = scr.query_one("#htree")
  await U.until(pilot, lambda: len(tree.root.children) >= 3, 20)
  group = next(n for n in tree.root.children if str(n.label).startswith("observables"))
  group.expand()
  await U.until(pilot, lambda: len(group.children) >= 2, 20)
  node = next(n for n in group.children if str(n.label).startswith("energy"))
  tree.focus()
  tree.move_cursor(node)
  await pilot.pause(2.0)
  app.save_screenshot(filename="files.svg", path=IMAGES)


def anonymize(path, user):
  """The user name (the lab cluster is this machine) -> a sample name of the same length (the text is on a grid)"""
  sample = SAMPLE_NAMES.get(len(user)) or ("user" + "x" * len(user))[:len(user)]
  with open(path) as f:
    text = f.read()
  with open(path, "w") as f:
    f.write(text.replace(user, sample))


def main():
  from lab import Lab, missing_tools
  missing = missing_tools()
  if missing or sys.version_info < (3, 12):
    sys.exit(f"needs Python 3.12 and {', '.join(missing) or 'OpenSSH'}")
  global U
  import ui_scenarios as U
  shutil.rmtree(WORK, ignore_errors=True)
  work = WORK
  try:
    with Lab(work) as lab:
      node_fakes(lab)
      session = U.UISession(lab.work, os.path.join(lab.work, "home"), os.path.join(lab.work, "out"), size=SIZE).start()
      try:
        for shot in (shot_jobs, shot_overview, shot_job, shot_files):
          session.run(shot, no_check, timeout=300)
          print("made", shot.__doc__.split(":")[0])
      finally:
        session.close()
  finally:
    shutil.rmtree(work, ignore_errors=True)
  for name in ("overview.svg", "jobs.svg", "job-details.svg", "files.svg"):
    anonymize(os.path.join(IMAGES, name), getpass.getuser())
  print("written to", IMAGES)


if __name__ == "__main__":
  main()
