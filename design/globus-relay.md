# Globus relay through slurp: implementation brief

This brief is for an agent that implements it in the slurp repository, on the branch `feature/data_transfer`. It is
self-contained: read it whole before starting, then the code it points to. The ground rules of
`design/transfer-endpoints.md` §0 apply (one commit per small point, never push, code and docs style, tests in the
layer that fits, verify before claiming).

## 1. The problem

Globus moves data only between two Globus collections. A transfer between a Globus collection and anything else (this
machine, a cluster tab without a Globus link, an S3 bucket, an SMB share, a WebDAV folder) has no route today: the
wizard offers nothing. The user wants such transfers anyway, relayed through slurp (the data passes this machine),
accepting that it is slower than Globus.

## 2. The decision

Three ways were weighed:

| | How | Speed | Needs | Verdict |
|---|---|---|---|---|
| **A. HTTPS relay** | slurp downloads each file from the collection over HTTPS and writes it to the other end as it arrives (or reads from the other end and uploads) | this machine's bandwidth, streamed | the collection runs Globus Connect Server 5 with HTTPS on (most institutional collections) | **build it**: works for every kind of other end |
| B. Stage through Globus Connect Personal | Globus copies to a collection on this machine, then rsync/rclone copy on | Globus, then a second copy | GCP installed and running here, disk space for everything | later: the fallback for collections without HTTPS |
| C. Stage through a cluster's collection | Globus copies to a linked tab's collection (e.g. CSCS), then rclone on that cluster copies on | fast, nothing passes this machine | an open tab with a Globus link, scratch space there | later: an optimisation where such a tab exists |

This brief is about **A**. B and C are §9.

## 3. Where things are (read these first)

| File | What it is |
|---|---|
| `src/slurp/transfer/globus.py` | the Globus side: login (`Login`, scopes, consents), `client()`, `call()` (errors as states), `collection`, `ls`, `submit`, task polling |
| `src/slurp/transfer/manager.py` | the transfers: routes, checks (`rclone_check`, `globus_check`), runs (`run_rclone_here`: the model for a run in slurp with progress, Pause, errors), `verify`, `not_ready`, `route_passed` |
| `src/slurp/transfer/rclone.py` | rclone: remotes from the environment (`Endpoint`, `TabEnd`), `run`, `lsjson`, `test`, `stats_of`, `error_of`, `causes`, `outcome` |
| `src/slurp/transfer/sftp_ssh.py` | the "ssh" rclone runs for a tab: slurp's session, the output of login scripts dropped |
| `src/slurp/transfer/relay.py` | the rsync relay: rsync on one tab, its server on the other, bytes through slurp |
| `src/slurp/transfer/endpoints.py` | endpoint definitions and their states; `rclone_routes`, `globus_only`, `collection_of` |
| `src/slurp/transfer/probe.py` | what a server says of itself (the endpoint window's Server part; Globus: the collection's fields, `https_server` among them) |
| `src/slurp/transfer/model.py` | `Transfer`, `ROUTES`, `RUNS_ON`, `record_run` |
| `src/slurp/ui/screens/transfers.py` | the wizard (`TransferWizard`: `rclone_routes`, `fill_runs`, `show_plan`, `flow_diagram`), the list (`how_text`, `TOOLS`), the details |
| `src/slurp/ui/screens/globus.py` | the Globus login window (`GlobusLoginScreen(collections, why)`) |
| `src/slurp/ui/screens/endpoint.py` | the endpoint window |

## 4. Globus HTTPS: the facts to verify first

Before the first line of the route, confirm against a real collection (CSCS's will do) with a small script, and write
what you find into this section:

1. The collection document (`get_endpoint`) has `https_server` (e.g. `https://g-1a2b3c.data.globus.org`) when HTTPS
   is on; Globus Connect Personal and Globus Connect Server 4 collections have none.
2. The scope of HTTPS access to one collection is `https://auth.globus.org/scopes/<collection-id>/https`. Whether a
   mapped collection also needs its `data_access` consent as a dependency of it: find out.
3. `GET <https_server>/<path>` with `Authorization: Bearer <token>` reads a file; `Range: bytes=N-` resumes it (an
   answer `206`).
4. `PUT <https_server>/<path>` writes a file (whole; no resume). Whether the folders above it must exist: find out
   (if so, `operation_mkdir` through the Transfer API first).
5. `HEAD` gives the size; listing is not offered over HTTPS (it is the Transfer API's `operation_ls`, as Browse… does).
6. How long an access token lives, and that the refresh token renews it (`G.client()`'s authorizer does this for the
   Transfer API: do the same for the HTTPS scope).
7. High-assurance collections: what they refuse (long-lived tokens?), and what the error says.

## 5. The route

- A new route `globus-relay` in `M.ROUTES` ("Globus relay through slurp"), `runs_on` `slurp` only.
- **Offered when** one end is a Globus endpoint (or a tab linked to a collection, used as Globus) and the other is
  *not* Globus-capable: this machine, a tab without a link, S3, SMB, WebDAV. Between two Globus ends: the real Globus
  route, as now. Put the rule next to `E.rclone_routes` (`E.globus_relay_routes`, or one function that returns all
  routes of a pair).
- **List:** `Globus · relay · in slurp` (`TOOLS`, `ROUTE_SHORT`, `how_text`).
- **Flow picture:** `collection (HTTPS) → slurp → bucket` (a box for slurp, as the rclone here route).

## 6. The check

In the manager, as `rclone_check`, one step "globus" plus the other end's step:

1. The Globus login, the collection (`G.collection`), its `https_server`: none: the route is refused with "this
   collection offers no HTTPS (Globus Connect Personal, or HTTPS switched off): Globus cannot relay it", pointing to the
   alternatives once they exist (§9).
2. The HTTPS scope consented: not yet: "needs your consent to HTTPS access: log in to Globus again" (the wizard opens
   `GlobusLoginScreen([cid], why)` with the HTTPS scope added, see §7).
3. A `HEAD` of the source folder or the destination folder with the token: refused: said (403: no access there; 401:
   the token).
4. The sources: walked with `operation_ls` (§8.1), counted (size, files): the estimate as for the other routes.
5. The other end: as the rclone and rsync checks do it now (a tab: connected, the path there, written; S3, SMB, WebDAV:
   `RC.test(..., write=True)`; this machine: the folder).

## 7. Access (the login)

- `G.scopes(collections)` today builds the Transfer scope with `data_access` dependencies. Add HTTPS scopes for the
  collections that the relay uses: `https://auth.globus.org/scopes/<cid>/https` (with the dependency found in §4.2).
- Keep each HTTPS token in the keyring as the refresh token is now (`globus:https:<cid>`), or derive it from the one
  refresh token if Globus allows it for these scopes (§4.6).
- A helper `G.https_token(cid)` that returns a valid access token (refreshed when it expires) or raises
  `GlobusError("needs consent", …)`.

## 8. The engine

A new module `src/slurp/transfer/globus_relay.py`, run in a thread from the manager like `run_rclone_here`.

### 8.1 What to copy

- Expand the sources as every route does (`R.sources(t)`: braces; patterns matched against `operation_ls` of their
  folder).
- Walk folders with `operation_ls` (paged; depth first), giving `[(relative path, size, mtime)]`. Many folders: show
  "listing … (n folders)" as progress.
- On the other end, list what is there (the same paths): a tab with `find -printf` over its session, rclone ends with
  `lsjson -R`, this machine with `os.walk`.
- **Skip** a file whose path and size match. (Times are not kept by an HTTPS `PUT`, so they cannot decide; with
  "checksums" on, compare hashes after copying, §8.4.)

### 8.2 Moving one file

| Other end | From Globus (download) | To Globus (upload) |
|---|---|---|
| this machine | `GET` → the file (a `.slurp-part` name, renamed when complete) | the file → `PUT` |
| a tab | `GET` → `cat > part` over the tab's session (a command channel of its ControlMaster, as the relay uses), renamed at the end | `cat file` over the session → `PUT` |
| S3, SMB, WebDAV | `GET` → `rclone rcat remote:path` (its stdin) | `rclone cat remote:path` → `PUT` |

- Stream in chunks (1–8 MB); never hold a whole file in memory.
- **Files at a time:** the existing `transfers` option (default 4).
- **Speed limit:** the existing `bwlimit` option, shared by all streams (a token bucket).
- A download that breaks: resumed with `Range` from the bytes already written (this machine and tabs: the part file's
  size; rclone ends: start over, `rcat` cannot append). An upload that breaks: starts over.
- Retries: 3 per file, with a pause; then the file is an error of the run (as rsync's errors), and the run goes on.

### 8.3 Progress, Pause, Continue, results

- As `run_rclone_here`: `self.progress(t, {bytes, rate, eta, percent}, current, files, errors, error_count)` every
  second; the total from the walk.
- **Pause:** stop all streams (close the HTTP responses and the writer processes), the part files stay; the run is
  recorded as paused. **Continue:** a new run; the walk again; done files skipped, part files resumed.
- The end: `done`, `done-errors` (some files failed: their errors), or `failed` (nothing could be done: the reason).
- slurp quits: as the other runs in slurp (paused; Continue goes on).

### 8.4 Verify, checksums

- **Verify:** walk both sides again; every source file there with the same size: the same; else what differs.
- **Checksums** on: hash each stream as it passes (sha256) and compare with the destination's hash after writing (a
  tab: `sha256sum` over its session; rclone ends: `rclone hashsum sha256` where the backend has it, else said not
  checked; this machine: hashlib). A mismatch: the file is copied again once, then an error.

## 9. Later: B and C

- **C, through a cluster's collection** (where an open tab is linked to a collection and the other end is S3, SMB,
  WebDAV): a route of two runs: Globus to the tab's scratch (`globus` route), then rclone on that cluster to the other
  end (`rclone-cluster`, as a transfer job where the login node has none). The transfer shows both phases; a folder in
  scratch is left for the next run (incremental) or removed at the end (an option).
- **B, through Globus Connect Personal**: for collections without HTTPS. GCP's endpoint made through the Transfer API
  (`create_endpoint` with `is_globus_connect`, its setup key given to `globusconnectpersonal -setup`), GCP started by
  slurp while the transfer runs, a staging folder here; then the second copy as now. Needs GCP installed: the check
  says how.

## 10. UI and docs

- Wizard: the route with its check (§6); when HTTPS consent is missing, a button **Log in to Globus (HTTPS access)**
  as for the data access consent today.
- Endpoint window (Globus): a line "HTTPS: yes (<server>) / no" (`probe.globus` reads `https_server` already), and
  whether the HTTPS consent is given.
- Help (`VIEW_HELP` of the wizard, the transfer, the endpoint window), `docs/guide/transfers.md` (a section "Globus
  relay through slurp": what it does, what it needs, its limits), `CHANGELOG.md`.

## 11. Limits (say them in the guide)

- Speed: this machine's bandwidth; many small files are slow (a few requests each).
- Only while slurp runs (as the other relays). Running it on a cluster would need the token there, with the user's
  agreement as for rclone's keys: not in this brief.
- Collections without HTTPS (Globus Connect Personal, Globus Connect Server 4, HTTPS switched off): not relayed (B).
- File times are not kept on uploads to Globus.
- High-assurance collections may need a login more often (§4.7).

## 12. Phases and what "done" means

1. **Facts** (§4): the script, its findings written into §4. Done: each point answered.
2. **Access** (§7, the endpoint window's HTTPS line): consent, token, `HEAD`. Done: a unit test with a fake HTTPS
   server and a fake token source; the window shows HTTPS.
3. **This machine ↔ Globus** (§5, §6, §8 for this machine): route, check, engine, Pause/Continue, Verify. Done: unit
   tests against a fake Globus (a local HTTPS-like server for `GET`/`PUT`/`HEAD` with `Range`, and a fake Transfer
   API `operation_ls`); a manual run against the CSCS collection, both ways.
4. **Tabs and rclone ends** (§8.2): the writers and readers. Done: e2e on the lab (a tab) and with moto / rclone serve
   webdav (S3, WebDAV), with the fake Globus.
5. Later: C, then B (§9).

## 13. Testing notes

- A fake Globus for the tests: a small `http.server` that answers `HEAD`, `GET` (with `Range`), `PUT` on a folder,
  checking a bearer token; and a fake `G.client()` whose `operation_ls` lists that folder. Inject both (monkeypatch
  `G.client`, `G.https_token`, the HTTPS base URL).
- The real thing cannot run in CI: phase 3 and 4 each end with a manual run against a real collection, written down
  in the commit message (what was copied, how fast).
