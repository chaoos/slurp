# Transfers with endpoints: implementation brief

This brief is for an agent that implements it in the slurp repository, on the branch `feature/data_transfer`. It is
self-contained: read it whole before starting, then the code it points to.

## 0. Ground rules

- **Commits:** one commit per small point, on `feature/data_transfer`. Never push. Commit messages in the style of the
  existing ones (`git log --oneline -40`): a lowercase subject `area: what changed`, a body only when the why is not
  obvious.
- **Code style:** as the existing code (2-space indents, compact one-line forms, docstrings and comments in plain
  sentences that say what a thing is and why; ruff with `pyproject.toml`'s settings). No formatter.
- **Docs style:** the guide pages in `docs/guide/` are plain present-tense prose (see `docs/guide/transfers.md`). Every
  user-visible change also goes into the `?` help of its window (`src/slurp/ui/screens/help.py`, `VIEW_HELP`), the
  guide page, and `CHANGELOG.md` (Unreleased).
- **Tests:** every behaviour gets a test, in the layer that fits:
  - `tests/unit/`: no app.
  - `tests/ui/`: Textual windows, no cluster.
  - `tests/e2e/`: the lab: a real sshd with a fake Slurm on this machine (`tests/e2e/lab/`); scenarios go in
    `tests/e2e/ui_scenarios.py`.

  Tests use the soft `check(name, cond, info)` fixture. Run `python -m pytest tests/unit tests/ui` (minutes) and the
  transfer e2e tests (`tests/e2e/test_ui_transfers.py`) before each commit that touches them; the full e2e suite at
  the end of each phase. A failing test you did not cause: verify on the commit before yours (a temporary
  `git worktree`) before saying so.
- **Verify, don't assume:** when a fix is for a bug, first make a test that fails without it, then fix.
- **Remote tools** (rclone, Globus SDK) are optional: slurp must work without them; what needs them says so.

## 1. Where things are (read these first)

The transfer feature exists already (about 5000 lines). The parts:

| File | What it is |
|---|---|
| `src/slurp/transfer/model.py` | `Transfer` (dataclass), `Store` (`$XDG_STATE_HOME/slurp/transfers.json`), `tab_ref` / `find_tab`, `record_run` |
| `src/slurp/transfer/rsync.py` | rsync options, progress/stats parsing, exit codes, the pattern expansion snippet (`EXPAND`, `marked`) |
| `src/slurp/transfer/endpoint.py` | `Endpoint`: **a tab as a place to run commands** (command channel, channels of the SSH ControlMaster, local processes). Will be renamed (§3.1) |
| `src/slurp/transfer/relay.py` | the relay: rsync on the source, its server on the destination, bytes through slurp |
| `src/slurp/transfer/runner.py` | runs on a cluster (detached, Slurm transfer job): runner script, `progress.json`, preflight login, temporary key |
| `src/slurp/transfer/plan.py` | the check: facts, sources, destination, routes, probes, estimates, Globus hint, counting |
| `src/slurp/transfer/schedule.py` | schedules: **to be removed** (§6) |
| `src/slurp/transfer/manager.py` | `Manager`: runs, follows, pauses, continues, checks; `run_now`, `recheck`, `probe_node`, `test_job` |
| `src/slurp/ui/transfers_tab.py` | the top-level Transfers tab (mixin of the app): its subtabs, wizard entry, restore |
| `src/slurp/ui/screens/transfers.py` | `TransferWizard`, `TransfersScreen` (list), `TransferScreen` (details), runs list/details, `PickPathsScreen`, `flow_diagram` |
| `src/slurp/checks/rulepacks.py` + `checks/packs/*.toml` | site rule packs (`transfer_partition`, `globus`, …) |
| `docs/guide/transfers.md` | the user guide of transfers |

Run the app's tests once before you start, to know the baseline.

## 2. What we build (the decisions, final)

1. In the **Transfers tab** the user defines **endpoints**: places data comes from or goes to. A transfer's source and
   destination are each **an endpoint** (plus paths).
2. **Cluster tabs are endpoints automatically**: every open tab is listed as one; its status follows the tab's login
   (connected, needs login, connecting). SSH endpoints are **always tabs**: "New SSH endpoint…" opens the existing
   `+` (add a tab) dialog.
3. Kinds besides tabs: **this machine**, **S3 (and S3-compatible)**, **Globus collection**, **SMB**, **WebDAV**.
4. **Secrets only in the OS keyring.** No other storage of secrets, no fallback file. Where no keyring works, the kinds
   that need secrets cannot be created (and say why); tabs and this machine always work.
5. **The When step disappears.** Every transfer is manual: it runs only on **Run now**. Instead of schedules there is
   **Copy as a command** (the command of a run, to put into scrontab/cron by hand).
6. In the details of a transfer, **each endpoint shows its login status**; **Run now** is enabled only when every
   endpoint the mover needs is connected.
7. When a mover would run on a cluster with another endpoint's secret (rclone on alps with an S3 key), **slurp asks
   and warns** before; the answer is kept per transfer.
8. **Deleting an endpoint that transfers use is refused**, and the refusal names them.
9. **Test of an S3 endpoint:** a listing; **if the endpoint is a destination of the transfer being set up** (or Test
   is pressed with "as a destination"), also a write test: a marker object written and deleted.
10. **Globus for a cluster tab:** the user **links a tab to a Globus collection** themselves (the endpoint form of a
    tab, §4.3). The rule packs' `globus` field only suggests one.

## 3. Data model

### 3.1 Rename first (phase 1, its first commit)

The existing `transfer/endpoint.py` `Endpoint` means *a tab where commands run*. Rename it to `Host` (module
`transfer/host.py`), with all its uses and tests, so that **Endpoint** is free for the user-facing concept. Nothing
else changes in that commit.

### 3.2 Endpoint

New module `src/slurp/transfer/endpoints.py`:

```python
@dataclass
class EndpointDef:
  id: str                 # new_id()
  name: str               # shown everywhere; unique among endpoints
  kind: str               # "local" | "s3" | "globus" | "smb" | "webdav"   (tabs are not stored: see below)
  settings: dict          # per kind (§4.2); never a secret
  secrets: list[str]      # the names of its secrets in the keyring (e.g. ["access_key", "secret_key"])
  created: float
```

- Stored in `$XDG_STATE_HOME/slurp/endpoints.json` (atomic writes, as `Store` does).
- Secrets: keyring service `"slurp"`, username `f"endpoint:{id}:{name_of_secret}"`. Read them only when needed (a test,
  a connect, a run); never log them, never put them in `transfers.json`, `progress.json`, command lines (use the
  environment of the process) or error messages.
- **Tab endpoints are not stored**: they are the open tabs (and, for a transfer that names a tab that is closed, its
  `tab_ref`). Their id in references is `"tab:" + destination` (local tab: `"tab:"`), resolved with `find_tab`.
- **This machine** exists once, built in (id `"local"`), like a tab without login.

A **reference** in a transfer: `{"endpoint": id, "paths": [...]}` (source) / `{"endpoint": id, "path": "..."}`
(destination). Keep `tab` refs readable for existing transfers (§7).

### 3.3 Endpoint status (runtime, not stored)

States: `not set up` (a secret is missing in the keyring) · `not connected` · `connecting` · `needs you` (a tab whose
login waits for input) · `connected` (with its round trip where known) · `failed` (why) · `expired` (OAuth token) ·
`tab not open`. For tabs the state comes from the tab (`app.connect_state`, `connection.ready`, as the overview does).
For the other kinds, "connected" means the last connect/test succeeded in this session; it is checked again before
every run.

### 3.4 Transfer

`Transfer` gets `source = {"endpoint", "paths"}`, `dest = {"endpoint", "path"}`, `mover` (§5; replaces `route` +
`runs_on`, or extends them: rsync keeps its routes), `secrets_to` (per transfer: which secrets the user agreed to send
to which cluster, §5.2). `schedule`, `next_run`, `pending["scheduled"]` go away (§6).

## 4. UI

### 4.1 The Transfers tab

- Its fixed subtabs: **Transfers** (the list, as now) and **Endpoints** (new). The tab is there while transfers **or
  defined endpoints** exist.
- Menu: **Transfer data…** (`T`, as now) and **Endpoints…** (opens the Endpoints subtab; also when none exist).

### 4.2 The Endpoints subtab

A table, refreshed in place like the transfer list (use `fill_table` in `screens/transfers.py`: the scroll position and
cursor must stay):

| Name | Kind | Where | Status | Used by |
|---|---|---|---|---|
| ● alps | cluster tab | daint.alps.cscs.ch | connected · 38 ms | 3 transfers |
| ○ jedi | cluster tab | jedi | needs login | 1 transfer |
| ⌂ this machine | local | — | always | 0 |
| ● my-bucket | S3 | eu-central-1 · my-bucket/runs | connected | 2 transfers |
| ◌ CSCS Globus | Globus | <collection name> | expired: Log in | 1 transfer |

- Tabs first, then the others by name.
- Buttons/keys:
  - **New endpoint…** (`n`).
  - **Connect** / **Log in**: for a tab, its login, as the tab's own Connect does.
  - **Disconnect**.
  - **Test**.
  - **Edit…** (`e`): not for tabs, except their Globus link, §4.3.
  - **Delete** (`Del`).
- **Delete is refused while transfers use the endpoint**: a message names them (up to five, then "and n more"). A
  successful delete removes its keyring secrets too.
- The line below the table: the highlighted endpoint's full settings (never secrets) and its last error.

### 4.3 The endpoint form (a modal, `EndpointScreen`)

- Kind first (a select), then its fields, then **Test**, **Save**, **Cancel**. Save offers "Connect now".
- Per kind:
  - **S3**: endpoint URL (empty: AWS), region, bucket, prefix; secrets access key, secret key, session token
    (optional). **Test**: list the prefix; when the endpoint is (or is about to be) a **destination**, also write and
    delete a marker object `.slurp-test-<random>` under the prefix, and say if either fails (no write permission).
  - **Globus**: search the collection by name or paste its ID, base path; **Log in** with the device-code flow (show
    the URL and code; open the URL in the browser where possible); then consent for the collection if it needs one
    (data access scope). The refresh token goes to the keyring.
  - **SMB**: server, share, domain, user; secret password. Test: list the share root.
  - **WebDAV**: URL, user; secret password or token. Test: list the URL.
  - **Cluster tab** (only reachable by Edit… on a tab endpoint): **link to a Globus collection** (search or ID, base
    path mapping: which path on the tab corresponds to which path in the collection). If the site's rule pack has a
    `globus` value, it is suggested. Stored in `endpoints.json` keyed by the tab id.
- **No keyring available** (`keyring.get_keyring()` is the fail/null backend, or a write test fails): the kinds that
  need secrets are greyed out with the words "needs a keyring (Secret Service, Keychain or Windows Credential
  Manager): there is none here". Add `keyring` as an optional dependency (extra `transfers`, and in `all`).

### 4.4 The wizard (5 steps)

`TransferWizard` becomes: **Source · Destination · Options · Route (Mover) · Summary**. Remove the When step and
everything of it (§6). Keep: clickable steps, editing with Cancel/Save, the check, the picture, the estimate.

1. **Source**: an **endpoint select** (connected first; each with its status dot and words; the not connected ones
   with an inline **Connect** / **Log in** button), **New endpoint…** (the form of §4.3; "SSH" opens the `+` dialog),
   then the paths: Browse… (the picker works on every endpoint kind, §5.3), typed paths, patterns (`*.h5`), and what
   they hold (counted on the endpoint). The step waits until the endpoint is connected.
2. **Destination**: the same, one path. The S3 write test runs when an S3 endpoint is chosen here (once per session per
   endpoint).
3. **Options**: per mover (rsync: checksums, speed limit; rclone: files at a time, checksums; Globus: sync level,
   verify checksums).
4. **Route**: the check proposes movers and where they run (§5) with speed and estimate. Every transfer is manual now:
   a route that did not pass may be chosen (as the manual case is today); Run now checks again first. The secrets
   question (§5.2) is asked here when the chosen mover runs on a cluster and needs another endpoint's secret.
5. **Summary**: name, the picture, the endpoints with their status, **Add** (Go and Save-without-starting are gone;
   editing: Save).

### 4.5 The details of a transfer

- Two **endpoint cards** at the top (source, destination): name, kind, path(s), status in words with its dot, and the
  action that fits (Connect, Log in, Open tab, Test). A tab's login that needs input opens the login pane as now.
- The status line (RUNNING …) and the flow picture stay; the picture names the mover and says where the data passes
  ("not through this machine" / "through this machine").
- **Run now** is enabled only when every endpoint the mover needs is connected; its tooltip names what is missing
  ("connect jedi", "log in to CSCS Globus", "the keyring has no secret for my-bucket: Edit… the endpoint").
- **Copy as a command** (new button): the command of a run (rsync / rclone / `globus transfer` line, or the `sbatch`
  script of a transfer job) to the clipboard (use slurp's copy: `ui/screens/dialogs.py` `copy_text`), with secrets
  as placeholders (`$AWS_SECRET_ACCESS_KEY`), never in clear.
- Remove everything about schedules (the Schedule section, "Resume schedule").

## 5. Movers

### 5.1 Which mover for which pair

| From → To | Mover(s) proposed by the check | Data passes through |
|---|---|---|
| tab ↔ tab | rsync: same filesystem / direct push / direct pull / relay (as now) | a cluster, or this machine (relay) |
| tab ↔ S3, SMB, WebDAV | rclone **on the cluster**; or rclone **on this machine** with the tab's SFTP over slurp's session (`--sftp-ssh "ssh -S <control socket> <dest>"`) | the cluster, or this machine |
| S3 ↔ S3, same provider and credentials | rclone server-side copy (on this machine) | the provider |
| S3 ↔ other non-tab kinds | rclone on this machine | this machine |
| Globus ↔ Globus, or a tab linked to a collection ↔ Globus/linked tab | Globus transfer (task); slurp submits and follows | the collections |
| this machine ↔ anything | rsync (to a tab) / rclone (others), here | this machine |

- The check verifies the mover exists where it would run:
  - `rsync --version`;
  - `rclone version` (with the minimum version a feature needs: `--sftp-ssh` needs a recent rclone, verify the exact
    version in rclone's changelog before relying on it);
  - `globus_sdk` importable.
- A missing mover is a failed route with its reason.

### 5.2 Secrets that would travel (ask and warn)

When the chosen mover runs on a cluster and needs a secret of another endpoint, ask before (in the Route step, and
again at Run now if the answer is missing):

> rclone on **alps** needs the keys of **my-bucket**. They would be written to `~/.cache/slurp/transfers/<id>/`
> (readable only by you) for the run, and removed when it ends. Root and the administrators of alps could read them
> meanwhile.
> [Send them and run there] [Run through this machine instead] [Cancel]

- Keep the answer in `transfer.secrets_to`.
- The details show it ("keys of my-bucket sent to alps for each run").
- Edit… can change it.
- The runner removes the secrets file at the end of a run in every case (trap on EXIT); slurp checks and removes it
  when it sees the run's end.

### 5.3 The `Storage` interface (browsing, counting, checking)

One interface over endpoints, used by the picker, the source count, the check and the endpoint Test:

```python
class Storage:
  def connect(self): ...            # raises with a reason (for the status)
  def status(self) -> str: ...
  def list(self, path) -> list[dict]: ...   # name, dir, size, mtime (as cluster.listing gives)
  def count(self, paths) -> dict: ...       # as plan.count: files, dirs, links, bytes, matches
  def exists(self, path) -> bool: ...
  def test_write(self, path) -> None: ...   # S3 destinations: a marker object, written and deleted
```

- **Tab / this machine:** today's code (`cluster.listing`, `plan.count`, `Host`).
- **rclone kinds:** `rclone lsjson`, `rclone size --json`, on-the-fly remotes (`:s3,provider=…:bucket/prefix`), the
  secrets in the process environment (`RCLONE_S3_ACCESS_KEY_ID`, …), never on the command line.
- **Globus:** `globus_sdk.TransferClient.operation_ls`, `get_endpoint` / collection info.

### 5.4 Running, progress, pause, continue

- **rsync:** unchanged.
- **rclone:** `rclone copy` (never `sync`: nothing is deleted on the destination) with `--use-json-log --stats 1s
  --stats-log-level NOTICE`. Parse its JSON stats lines (bytes, totalBytes, speed, eta, transfers) into the same
  `progress` dict the UI uses. Pause = stop (TERM); Continue = run again (finished files are skipped; a half-copied
  file starts over: say so in the details). Patterns: `--include`. Checksums: `--checksum`. On a cluster: through the
  runner (`runner.py`), with rclone's stats written to `progress.json` as rsync's are.
- **Globus:** `submit_transfer` with `sync_level` ("mtime" by default, "checksum" with the option) and
  `verify_checksum`; follow with `get_task` / task events (bytes_transferred, files, faults, status). Pause = cancel
  the task (Globus users cannot pause tasks); Continue = submit a new one (the sync level skips what is there). A
  Globus run goes on without slurp: store the task id like a job id and follow it at the next start.
- **Run now gating:** every endpoint the mover needs is connected; then, as today, a route that did not pass is
  checked first.

## 6. What is removed

- The When step and its widgets.
- `transfer/schedule.py` and its tests.
- `Transfer.schedule` / `next_run`, the scheduler in `Manager.tick`, `go()`'s schedule branches, `reschedule()`,
  `watch_job()` (after a job), `--begin` jobs, automatic logins for schedules (`login_started`), missed runs, the state
  `scheduled` and its words, Resume schedule, the Schedule section of the details, the `scheduled` wording in the
  overview line.
- Every test of those. Keep: transfer jobs as a way to run (Run now submits one), detached runs, the relay.

## 7. Migration of existing transfers (`transfers.json`)

- `source.tab` / `dest.tab` → `{"endpoint": "tab:<destination>"}` (the local tab: `"tab:"`), paths kept.
- A `schedule` other than manual → manual; a one-time notification at the start names those transfers ("… run only on
  Run now from now on; Copy as a command gives the command for scrontab or cron").
- `next_run`, `pending["scheduled"]` dropped. History (`runs`), plan, options kept.
- Migrate on load (`Transfer.from_json`) and write the store back once.

## 8. Phases and what "done" means

**Phase 1: endpoints with tabs and this machine; no When.**

1. Rename `transfer/endpoint.Endpoint` → `transfer/host.Host` (§3.1).
2. `endpoints.py`: `EndpointDef`, its store, tab endpoints, this machine, status.
3. The Endpoints subtab (§4.2) with tabs and this machine; Menu → Endpoints…; delete refusal.
4. Wizard: Source and Destination select endpoints (§4.4), Connect inline; When removed.
5. Details: endpoint cards, Run now gating (§4.5); Copy as a command (rsync and transfer-job forms).
6. Remove schedules (§6); migration (§7).
7. Help, guide, changelog.

*Done when:*
- every transfer that worked before works through endpoints;
- the Endpoints list shows each tab's state live (scrolling kept);
- Run now waits for logins and says which;
- no scheduling code or test is left;
- old `transfers.json` files load (a test with one of the previous format);
- unit, ui and transfer e2e tests pass.

**Phase 2: S3 through rclone.**

1. Keyring access (`keyring` extra), the "no keyring here" state.
2. The S3 form, Test (list; write test for destinations), status.
3. `Storage` for rclone kinds; the picker, the count, the check use it.
4. The rclone mover: on this machine (and with `--sftp-ssh` for tab ↔ S3 through slurp), on a cluster through the
   runner; the secrets question (§5.2); progress, Pause/Continue.
5. Verify (`rclone check`) as a button in the details of a transfer with an rclone mover.

*Done when:*
- tab ↔ S3 and S3 ↔ S3 transfers run, are followed and paused/continued;
- secrets exist only in the keyring and, for a run the user agreed to, in that run's folder until it ends (a test
  asserts the file is gone);
- a destination without write permission fails Test with that reason.

**Phase 3: SMB and WebDAV.** Forms and `Storage` through rclone; the rclone mover as in phase 2. *Done when* a
transfer each way runs.

**Phase 4: Globus.**

1. Device-code login, token in the keyring, consent per collection.
2. The Globus endpoint form; linking a tab to a collection (§4.3, suggestion from the rule pack).
3. The Globus mover: submit, follow (also after a restart of slurp), Pause as cancel, Continue as resubmit.

*Done when* a Globus ↔ Globus transfer (and a linked tab ↔ Globus one) runs without data through slurp and is followed
across a restart.

## 9. Testing notes

- **Keyring:** use `keyring`'s in-memory backend in tests (set it in a fixture); test the "no keyring" path with the
  fail backend.
- **S3:** an S3-compatible server in tests: `moto` (`moto[server]`, add to the `dev` extra) started on a free local
  port, or `rclone serve s3` where the installed rclone has it. Test listing, the write test (a bucket policy without
  write: the test says so), copying both ways with a tab of the lab, the secrets file removed after a run on the lab
  "cluster".
- **rclone missing / too old:** tests with a fake `rclone` on `PATH` printing an old version.
- **Globus:** mock `globus_sdk` (no network in tests); test the task states → transfer states mapping, cancel and
  resubmit, following after a restart.
- **UI:** the endpoint select in the wizard (statuses, Connect inline, New endpoint…), the Endpoints list (in-place
  refresh, delete refused with names), the endpoint cards and Run now gating in the details, Copy as a command (secrets
  as placeholders).
- **e2e:** a scenario with the lab tab and an S3 server on this machine.

## 10. Out of scope

- Schedules of any kind inside slurp (Copy as a command replaces them).
- SSH connections that are not tabs.
- Secrets anywhere but the keyring.
- Deleting files on a destination (`rclone sync`, `rsync --delete`).
