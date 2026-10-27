# Playlist Auto Sync Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tray-managed Playlist monitoring that deduplicates YouTube videos, creates durable FIFO jobs, prefers original-language captions, falls back to Whisper medium, and processes one job at a time.

**Architecture:** Keep filesystem `JobStore` authoritative for transcript content and worker progress while a focused SQLite store owns Playlist relationships, global Video ID identity, and FIFO scheduling. A tray companion runs synchronization and queue supervision independently of browser sessions; Streamlit remains the management and diagnosis UI.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, yt-dlp, Streamlit, faster-whisper, pystray, Pillow, pytest

**Spec:** `docs/superpowers/specs/2026-10-27-playlist-auto-sync-phase-1-design.md`

## Global Constraints

- Preserve all VideoScript 1.0 manual YouTube/local workflows, readable job folders, interruption recovery, and managed-media cleanup.
- Deduplicate YouTube work globally by the exact 11-character Video ID; one video may retain multiple Playlist memberships but only one job and queue item.
- Use yt-dlp without browser cookies, Google API keys, OAuth, or telemetry.
- Prefer original-language creator captions, then original-language YouTube automatic captions; use Whisper `medium` only for absent or objectively unusable captions.
- Run at most one caption/Whisper job at a time and preserve FIFO order across restart.
- Auto-sync runs immediately at tray start/resume and every 10 minutes thereafter; manual Sync Now works while auto-sync is paused.
- SQLite state lives at `data/videoscript.db`; transcript bodies remain only in job files.
- Bind the web app to `127.0.0.1`; all accepted URLs must canonicalize to HTTPS YouTube Playlist or watch URLs.
- Do not implement drag-and-drop, search/filter, OAuth Playlist mutation, semantic subtitle scoring, parallel transcription, or dual transcript versions.

## Review Focus

- A crash between filesystem job creation and SQLite linking must reconcile to the orphan job rather than create a duplicate (Task 3 tests).
- The same video manually created in 1.0 and later discovered in two Playlists must link one existing job and preserve both memberships (Task 3 tests).
- Overlapping tray timer, tray Sync Now, and web Sync Now requests must serialize and coalesce without duplicate work or SQLite lock leakage (Task 6 tests).
- Ambiguous/missing original-language metadata and regional language tags must still choose one caption deterministically (Task 4 tests).
- Stop/Exit with an active worker and stale PID files must not kill an unrelated process or leave a second tray instance possible (Task 7 tests).

---

### Task 1: Playlist SQLite store and durable FIFO

**Files:**
- Create: `playlist_db.py`
- Create: `tests/test_playlist_db.py`

**Interfaces:**
- Produces: `PlaylistDB(path: Path)`, `PlaylistRecord`, `QueueItem`, and transactional CRUD/claim methods consumed by Tasks 3, 5, and 6.
- Produces exact methods: `add_or_reactivate_playlist(playlist_id, url, title)`, `list_playlists()`, `set_playlist_enabled(id, enabled)`, `soft_remove_playlist(id)`, `set_auto_sync_paused(paused)`, `auto_sync_paused()`, `apply_snapshot(playlist_id, videos, synced_at)`, `record_sync_failure(playlist_id, message, at)`, `link_job(video_id, job_id, method)`, `find_job_id(video_id)`, `enqueue(job_id, video_id)`, `remove_unreferenced_waiting()`, `claim_next()`, `finish_queue_item(job_id, state, error='')`, and `recover_stale_processing(active_job_ids)`.

- [ ] **Step 1: Write failing schema and Playlist CRUD tests**

Test initial schema/version creation, WAL and foreign-key configuration, duplicate Playlist reactivation, enable/disable, soft removal/history retention, and persisted global pause state.

- [ ] **Step 2: Run schema/CRUD tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_db.py -k "schema or playlist or paused" -v`

Expected: FAIL because `playlist_db` does not exist.

- [ ] **Step 3: Implement schema and Playlist CRUD**

Create schema-versioned tables from the spec with `sqlite3.Row`, foreign keys, WAL, a 5-second busy timeout, UTC epoch timestamps, parameterized statements, and short `BEGIN IMMEDIATE` write transactions.

- [ ] **Step 4: Run schema/CRUD tests and verify GREEN**

Run the Step 2 command; expected all selected tests pass.

- [ ] **Step 5: Write failing snapshot, membership, and FIFO tests**

Test global video upsert, stable many-to-many membership/order, absent/reappearing membership, one job link per Video ID, removal of waiting work only when no active Playlist still references it, monotonic FIFO append, transactional single claim, terminal states, one automatic retry, and stale-processing recovery across a new `PlaylistDB` instance.

- [ ] **Step 6: Run FIFO tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_db.py -k "snapshot or membership or queue or retry or stale" -v`

Expected: FAIL because snapshot/link/queue methods are absent.

- [ ] **Step 7: Implement snapshot and queue methods**

Use a unique Video ID primary key, unique `(playlist_id, video_id)` membership, unique job/video queue constraints, and atomic compare-and-update claiming ordered by `position, created_at`.

- [ ] **Step 8: Run store tests and full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_db.py -v` then `.venv\Scripts\python.exe -m pytest -q`.

Expected: all tests pass.

- [ ] **Step 9: Commit**

```powershell
git add playlist_db.py tests/test_playlist_db.py
git commit -m "feat: add playlist database and fifo queue"
```

### Task 2: Playlist URL validation and yt-dlp discovery

**Files:**
- Modify: `core.py`
- Create: `playlist_sync.py`
- Create: `tests/test_playlist_sync.py`

**Interfaces:**
- Consumes: existing canonical video URL rules.
- Produces: `canonical_youtube_playlist_url(value: str) -> tuple[str, str]`, `PlaylistVideo(video_id, title, url, position, duration, language)`, `PlaylistSnapshot(playlist_id, title, url, videos)`, and `fetch_playlist(url: str) -> PlaylistSnapshot`.

- [ ] **Step 1: Write failing Playlist URL tests**

Assert canonical `https://www.youtube.com/playlist?list=<id>`, removal of unrelated query parameters, and rejection of watch-only, non-HTTPS, credentialed, non-YouTube, empty-list, and malformed URLs.

- [ ] **Step 2: Run URL tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_sync.py -k "url" -v`

Expected: FAIL because the canonicalizer is absent.

- [ ] **Step 3: Implement `canonical_youtube_playlist_url` in `core.py`**

Return canonical URL plus Playlist ID; accept standard YouTube hosts only and validate the list token before constructing the URL.

- [ ] **Step 4: Run URL tests and verify GREEN**

Run the Step 2 command; expected all selected tests pass.

- [ ] **Step 5: Write failing flat-extraction tests**

Use a controlled fake `YoutubeDL` result to assert title/ID/order extraction, canonical watch URLs, unavailable entry filtering, duplicate Video ID collapse preserving first position, and rejection of a response whose extracted Playlist ID does not match the requested ID.

- [ ] **Step 6: Run extraction tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_sync.py -k "fetch or snapshot" -v`

Expected: FAIL because `fetch_playlist` is absent.

- [ ] **Step 7: Implement yt-dlp flat extraction**

Create a Playlist-specific yt-dlp option set with playlist extraction enabled, `extract_flat='in_playlist'`, no download, no browser cookies, bounded retries/timeouts, and existing Node/ffmpeg runtime settings where relevant.

- [ ] **Step 8: Run sync tests and full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_sync.py -v` then `.venv\Scripts\python.exe -m pytest -q`.

Expected: all tests pass.

- [ ] **Step 9: Commit**

```powershell
git add core.py playlist_sync.py tests/test_playlist_sync.py
git commit -m "feat: discover youtube playlists"
```

### Task 3: Idempotent synchronization and legacy-job reconciliation

**Files:**
- Create: `playlist_service.py`
- Modify: `pipeline.py`
- Create: `tests/test_playlist_service.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `PlaylistDB`, `fetch_playlist`, `JobStore`.
- Produces: `extract_youtube_video_id(url: str) -> str | None`, `JobStore.find_youtube_job(video_id: str) -> dict | None`, `PlaylistService.sync_playlist(playlist_id: int) -> SyncSummary`, and `PlaylistService.sync_enabled() -> list[SyncSummary]`.

- [ ] **Step 1: Write failing reconciliation tests**

Test matching an existing manual YouTube job by canonical URL, preferring a completed match if corrupted duplicate legacy jobs exist, storing `youtube_video_id` on new jobs, and ignoring local/non-YouTube jobs.

- [ ] **Step 2: Run reconciliation tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_pipeline.py tests/test_playlist_service.py -k "youtube_job or reconcile" -v`

Expected: FAIL because lookup/reconciliation interfaces are absent.

- [ ] **Step 3: Implement Video ID extraction and `JobStore.find_youtube_job`**

Scan valid job metadata without mutating it; match explicit `youtube_video_id` first, then canonical YouTube URL for 1.0 compatibility.

- [ ] **Step 4: Run reconciliation tests and verify GREEN**

Run the Step 2 command; expected all selected tests pass.

- [ ] **Step 5: Write failing synchronization tests**

Cover first discovery, repeat sync, shared video across two Playlists, manual-job linking, completed preservation, missing-incomplete-job repair, simulated crash orphan reconciliation, soft-removed membership, independent Playlist failure, and transaction rollback preserving the last successful snapshot.

- [ ] **Step 6: Run service tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_service.py -v`

Expected: FAIL because `PlaylistService` is absent.

- [ ] **Step 7: Implement `PlaylistService`**

Persist snapshot membership first, reconcile by Video ID before creation, create Playlist jobs with status `waiting`, source `youtube`, canonical URL, `youtube_video_id`, `model='medium'`, and Playlist discovery metadata, then link/enqueue idempotently. Recover an orphan by scanning `JobStore` before any second creation.

- [ ] **Step 8: Run service tests and full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_service.py tests/test_pipeline.py -v` then `.venv\Scripts\python.exe -m pytest -q`.

Expected: all tests pass.

- [ ] **Step 9: Commit**

```powershell
git add pipeline.py playlist_service.py tests/test_pipeline.py tests/test_playlist_service.py
git commit -m "feat: sync playlists into deduplicated jobs"
```

### Task 4: Deterministic caption selection and Whisper fallback

**Files:**
- Modify: `pipeline.py`
- Create: `playlist_processor.py`
- Create: `tests/test_playlist_processor.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: existing `inspect_youtube`, `extract_captions`, `subtitle_choices`, `JobStore`.
- Produces: `CaptionSelection(kind: str, language: str, reason: str)`, `choose_caption(info: dict) -> CaptionSelection | None`, and `prepare_playlist_job(job_id: str, store: JobStore, db: PlaylistDB) -> dict`.

- [ ] **Step 1: Write failing deterministic caption-selection tests**

Assert manual-before-auto for original language, `zh-Hant`/`zh` base-language matching, stable lexical tie-breaks, deterministic manual then auto fallback when original language is missing, exclusion of translated/dub-only tracks, and `None` when no JSON3 track exists.

- [ ] **Step 2: Run selection tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_processor.py -k "choose_caption" -v`

Expected: FAIL because the processor is absent.

- [ ] **Step 3: Implement `choose_caption` and expose track extraction by exact selection**

Keep current single-video choices compatible; persist selected kind/language/reason on the job.

- [ ] **Step 4: Run selection tests and verify GREEN**

Run the Step 2 command; expected all selected tests pass.

- [ ] **Step 5: Write failing preparation/fallback tests**

Test successful caption completion without audio download, no-caption fallback to waiting Whisper medium, caption download/parse/empty failure fallback on the same job, no duplicate queue record, and preservation of technical failure reason.

- [ ] **Step 6: Run preparation tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_processor.py -k "prepare or fallback" -v`

Expected: FAIL because `prepare_playlist_job` is absent.

- [ ] **Step 7: Implement caption-first preparation**

Inspect once, attempt only the selected exact track, complete and persist caption jobs through existing output functions, or update the same job for Whisper `medium` and leave it runnable by the existing worker.

- [ ] **Step 8: Run processor tests and full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_playlist_processor.py tests/test_pipeline.py -v` then `.venv\Scripts\python.exe -m pytest -q`.

Expected: all tests pass.

- [ ] **Step 9: Commit**

```powershell
git add pipeline.py playlist_processor.py tests/test_pipeline.py tests/test_playlist_processor.py
git commit -m "feat: prefer captions for playlist jobs"
```

### Task 5: FIFO queue runner and recovery

**Files:**
- Create: `queue_runner.py`
- Create: `tests/test_queue_runner.py`
- Modify: `worker.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Consumes: `PlaylistDB.claim_next`, `prepare_playlist_job`, `launch_worker`, `process_is_running`, and `JobStore`.
- Produces: `QueueRunner.run_once() -> RunResult`, `QueueRunner.recover() -> None`, and `QueueRunner.run_until_idle(stop_event) -> None`.

- [ ] **Step 1: Write failing FIFO execution tests**

Test one-at-a-time claim, caption job completion followed by next item, Whisper launch followed by durable terminal polling, no second launch while a worker is active, FIFO persistence across a new runner, empty-queue waiting, and graceful stop-event exit.

- [ ] **Step 2: Run runner tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_queue_runner.py -k "run" -v`

Expected: FAIL because `QueueRunner` is absent.

- [ ] **Step 3: Implement queue execution**

Use database claims for ownership, existing worker process state for Whisper completion, bounded polling with a stop event, and terminal updates that start the next FIFO item only after the current job is durable.

- [ ] **Step 4: Run execution tests and verify GREEN**

Run the Step 2 command; expected all selected tests pass.

- [ ] **Step 5: Write failing recovery/retry tests**

Cover stale processing with no worker, live worker preservation, interrupted job requeue, one automatic retry then failed/manual state, missing filesystem job repair request, and exit behavior for an active worker.

- [ ] **Step 6: Run recovery tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_queue_runner.py -k "recover or retry or exit" -v`

Expected: FAIL until recovery paths exist.

- [ ] **Step 7: Implement recovery and retry limits**

Reuse existing stop/pause semantics; never kill a PID without the existing identity/liveness safeguards.

- [ ] **Step 8: Run runner/worker tests and full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_queue_runner.py tests/test_worker.py -v` then `.venv\Scripts\python.exe -m pytest -q`.

Expected: all tests pass.

- [ ] **Step 9: Commit**

```powershell
git add queue_runner.py worker.py tests/test_queue_runner.py tests/test_worker.py
git commit -m "feat: run playlist jobs in durable fifo order"
```

### Task 6: Sync coordinator and Windows tray companion

**Files:**
- Create: `tray_app.py`
- Create: `tests/test_tray_app.py`
- Modify: `requirements.txt`
- Modify: `requirements.lock.txt`

**Interfaces:**
- Consumes: `PlaylistService.sync_enabled`, `QueueRunner`, `PlaylistDB`, and existing process-lock patterns.
- Produces: `request_sync(marker_path: Path) -> None`, `SyncCoordinator.run(stop_event)`, `TrayController.open_app()`, `sync_now()`, `toggle_pause()`, and `exit()`.

- [ ] **Step 1: Write failing coordinator tests**

Assert immediate startup/resume sync, 600-second interval, manual sync while paused, web-process request-marker pickup, global serialization, overlap coalescing into exactly one follow-up run, per-Playlist failure isolation, and responsive stop-event handling using an injected clock/wait function.

- [ ] **Step 2: Run coordinator tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tray_app.py -k "coordinator" -v`

Expected: FAIL because the coordinator is absent.

- [ ] **Step 3: Implement `SyncCoordinator`**

Use one byte-range/process file lock for the singleton/sync critical section and `data/sync.requested` as an atomic cross-process coalescing marker. Remove the marker when a run begins; a marker created during that run triggers exactly one follow-up. Poll the marker at a short bounded interval while using the injected wait function; always release locks in `finally`.

- [ ] **Step 4: Run coordinator tests and verify GREEN**

Run the Step 2 command; expected all selected tests pass.

- [ ] **Step 5: Write failing tray-controller tests**

Test menu labels/actions, tooltip state text, four icon-state selections, browser open URL, pause persistence, clean exit signaling, second-instance rejection, and visible startup failure if tray initialization raises.

- [ ] **Step 6: Run tray tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tray_app.py -k "tray or singleton" -v`

Expected: FAIL until tray integration exists.

- [ ] **Step 7: Implement pystray companion**

Pin `pystray`, build simple Pillow icons in memory, keep queue and sync loops off the UI callback thread, and expose dependency-injected controller logic so tests do not require a real Windows notification area.

- [ ] **Step 8: Run tray tests and full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tray_app.py -v` then `.venv\Scripts\python.exe -m pytest -q`.

Expected: all tests pass.

- [ ] **Step 9: Commit**

```powershell
git add tray_app.py tests/test_tray_app.py requirements.txt requirements.lock.txt
git commit -m "feat: add playlist tray companion"
```

### Task 7: Playlist web UI and Start/Stop lifecycle

**Files:**
- Modify: `app.py`
- Modify: `Start.ps1`
- Modify: `Stop.ps1`
- Modify: `README.md`
- Modify: `VERIFICATION.md`
- Modify: `tests/test_app.py`
- Create: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `PlaylistDB`, `PlaylistService`, tray companion CLI, existing Streamlit history and job controls.
- Produces: Playlist management UI and verified two-process local lifecycle.

- [ ] **Step 1: Write failing Streamlit Playlist UI tests**

Test valid Add, duplicate reactivation, enable/disable, confirmed soft Remove, web Sync Now request, status/error/count display, recent discoveries, read-only FIFO display, and unchanged existing manual/local UI behavior using real temporary SQLite/JobStore state with network fetch injected below the service boundary.

- [ ] **Step 2: Run UI tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_app.py -k "playlist" -v`

Expected: FAIL because the Playlist UI is absent.

- [ ] **Step 3: Implement the Playlist management section**

Keep Playlist controls in a focused rendering function/module if `app.py` would otherwise grow materially; never perform an unbounded network sync during ordinary reruns.

- [ ] **Step 4: Run UI tests and verify GREEN**

Run the Step 2 command; expected all selected tests pass.

- [ ] **Step 5: Write failing lifecycle tests**

Exercise scripts through extracted/testable process-validation helpers or controlled PowerShell subprocesses: start server plus one tray, repeated Start creates no duplicate, Stop validates and stops only saved matching processes, stale PID safety, tray Exit shutdown, and clean restart preserving database state.

- [ ] **Step 6: Run lifecycle tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_lifecycle.py -v`

Expected: FAIL until scripts support the tray process.

- [ ] **Step 7: Update Start/Stop and documentation**

Launch the tray with hidden-window semantics, store `data/tray.pid`, validate executable identity before stop, keep server diagnosis possible if tray fails, and document MVP scope, 10-minute behavior, tray controls, local data paths, and recovery expectations.

- [ ] **Step 8: Run complete verification**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: all tests pass with only documented environment-dependent skips/warnings.

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\Start.ps1`, verify health at `http://127.0.0.1:8501/_stcore/health`, verify one tray PID, then run `powershell -NoProfile -ExecutionPolicy Bypass -File .\Stop.ps1` and verify both validated processes stop.

Expected: lifecycle completes without an orphan server/tray process.

- [ ] **Step 9: Commit**

```powershell
git add app.py Start.ps1 Stop.ps1 README.md VERIFICATION.md tests/test_app.py tests/test_lifecycle.py
git commit -m "feat: integrate playlist mvp"
```
