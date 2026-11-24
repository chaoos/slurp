# Development

## Working on slurp

Install the checkout in editable mode, so changes to `src/` take effect without installing again:

```bash
git clone https://github.com/chaoos/slurp.git
cd slurp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all,dev,docs]"   # dev: pytest, ruff, pre-commit; docs: this site
pre-commit install                            # the lint and file checks before each commit
```

`python3 slurp` from the top of the checkout, as in older versions, no longer works: the code is in `src/slurp/`, and
slurp runs as an installed package.

## Layout

```
src/slurp/            the package (installed with pip; `slurp` is slurp.cli:main)
  cli.py launch.py      the command line, how slurp starts itself again
  config.py state.py    settings kept between runs; logging and the data of the tabs (ClusterState; the app's ViewState)
  headless.py           the core without a frontend: tabs, job lists as data, transfers, changes pushed to subscribers
  cluster/              Slurm here or over SSH: the connection, jobs, their files, storage, categories; workers.py:
                        how work on threads reaches a frontend (Scheduler)
  transfer/             transfers between tabs, endpoints (rclone, Globus) and their runs
  checks/               the checks of batch scripts: engine, rules, fixes, guides
  sites/                the site packs (packs/*.toml): what slurp knows about a site that Slurm does not tell
  links/                shells, drives and web views on your own machine, the session holder, sshfs;
                        windows/ and linux/: the slurp helper
  resources/            the logo, the icons and their font (every frontend, the session holder)
  ui/                   the Textual app, its widgets and terminals; screens/: the windows
  scripts/              shell scripts slurp runs on the nodes of a job
tests/                pytest: unit/ (no app), ui/ (Textual windows), e2e/ (slurp against lab/: a fake Slurm behind a local sshd)
docs/                 this site (mkdocs.yml: its pages and settings) and its images
tools/                maintenance scripts: the logo files, the screenshots of the docs
```

## Tests

```bash
python -m pytest tests/unit tests/ui   # seconds: nothing needed beyond pip
python -m pytest tests/e2e             # slurp against a fake Slurm over SSH (~10 min)
python -m pytest                       # all of them
pre-commit run --all-files             # the lint (ruff) and file checks
```

- `tests/unit/`: no app, no cluster. `tests/ui/`: windows of the Textual app, no cluster.
- `tests/e2e/` (Python 3.12+): slurp against the lab (`tests/e2e/lab/`), its own `sshd` on a free port of 127.0.0.1
  with a fake Slurm and fresh keys, in pytest's temporary directory. It needs OpenSSH (`ssh`, `sshd`, `ssh-keygen`) and
  `sshfs` (Debian/Ubuntu: `apt install openssh-server sshfs`); without `sshd` these tests are skipped. They never touch
  your `~/.ssh` or slurp's settings.
    - The UI tests (`test_ui_*.py`, the scenarios in `ui_scenarios.py`) run in a logged-in app per module.
    - Logs and screenshots go to `<basetemp>/lab0/out/` (`--basetemp=DIR` sets the place).
    - One test: `python -m pytest tests/e2e/test_ui_arrays.py -k collapse`.
- The tests use the installed slurp and stop if it is not this checkout's (another checkout or worktree: install that
  one, or `PYTHONPATH=src`). See `tests/conftest.py` and `tests/e2e/conftest.py`.

## Without a frontend

`cluster/`, `transfer/`, `links/` and `checks/` import no Textual, nor anything of `ui/`. Work on threads reaches a
frontend through a `Scheduler` (`cluster/workers.py`): the app's is `ui/workers.py`, `ThreadScheduler` is the one of
no frontend. `slurp/headless.py` is the core as data, for tests and a second frontend:

```bash
python -m slurp.headless --jobs user@cluster   # the job list as JSON (--cached: the one kept by the last load)
```

`tests/e2e/test_headless.py` runs it against the lab with Textual blocked.

## Continuous integration

GitHub Actions runs on every push and pull request:

- **tests** (`.github/workflows/tests.yml`): the lint, the unit and ui tests on Python 3.11 to 3.13, the e2e tests on
  3.12 (their logs and screenshots are kept as an artifact for 14 days).
- **docs** (`.github/workflows/docs.yml`): builds this site with `mkdocs build --strict`, where a broken link, a
  missing anchor or a page left out of the navigation fails the build; from `main` it is published to GitHub Pages.
- **release** (`.github/workflows/release.yml`): on a tag `v<version>`, see below.

## Releases

slurp is on PyPI as [`slurp-ui`](https://pypi.org/project/slurp-ui/) (the name `slurp` is taken there; the command and
the module are `slurp`). A release:

1. Set `__version__` in `src/slurp/__init__.py` (one number for slurp and its Windows helper).
2. In `CHANGELOG.md`, turn **Unreleased** into `## <version> (beta), <date>` and start a new empty **Unreleased**.
3. Commit, tag and push: `git tag v<version> && git push origin main v<version>`.

The tag runs the release workflow: it checks that the tag is the version, builds the package (`python -m build`),
checks it (`twine check --strict`), installs it with `[all]` in an empty venv and runs it, publishes it to PyPI and
makes a GitHub release with the same files and the version's part of the changelog. PyPI takes it through trusted
publishing, without a token. Set up once:

- on pypi.org: *Your projects* → *Publishing* → *Add a pending publisher*: project `slurp-ui`, owner `chaoos`,
  repository `slurp`, workflow `release.yml`, environment `pypi`
- in the repository: *Settings* → *Environments* → *New environment* `pypi` (optionally with required reviewers, so
  that a release waits for a click)

To try a build locally: `python -m pip install build twine && python -m build && twine check --strict dist/*`.

## This site

The pages are Markdown in `docs/`, the navigation and theme (Material for MkDocs) in `mkdocs.yml`. The changelog page
includes `CHANGELOG.md` from the top of the repository, so it is written in one place.

```bash
mkdocs serve            # http://127.0.0.1:8000, rebuilt on every save
mkdocs build --strict   # as the CI builds it, into site/
```

The screenshots are slurp against the lab of the tests (a fake Slurm cluster with sample jobs and files):
`python tools/make_screenshots.py` makes them again after a change of the UI (it needs what the e2e tests need).

## Logo

The *prompt meter*: a shell prompt whose cursor became a load meter. It is drawn on the terminal grid (a pixel is one
column wide and half a row high), so the TUI shows it exactly with block characters: `❯▂▄█` in the header (in the
theme's colours), five rows of `▀▄█` in the help windows and on the lock screen of a view. `src/slurp/resources/logo.py` holds
the pixel map and makes every other form from it: the favicon and the start screen of the web view, the icons of the
slurp helper, and the files in `docs/images/` (`logo.svg`, `logo-plain.svg`, `logo-wordmark.svg`, `logo-256.png`,
`logo.ico`; `python tools/make_logo_files.py` writes them again).
