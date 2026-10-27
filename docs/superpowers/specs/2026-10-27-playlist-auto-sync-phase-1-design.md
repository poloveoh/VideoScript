# VideoScript 2.0 Phase 1: Playlist Auto Sync Design

## Intent

Phase 1 adds reliable monitoring of multiple public or otherwise yt-dlp-readable YouTube Playlists without replacing the existing VideoScript 1.0 transcription engine. While VideoScript is running, a Windows tray companion periodically discovers new videos, records them once by YouTube Video ID, and continuously processes them through a minimal durable FIFO queue. Phase 2 adds user-controlled drag-and-drop ordering on this foundation.

## Goals

- Add, list, enable/disable, remove, and manually sync multiple Playlist URLs in the Streamlit UI.
- Automatically sync enabled Playlists every 10 minutes while the VideoScript tray companion is running.
- Discover Playlist videos with yt-dlp without requiring a Google account, API key, or OAuth.
- Deduplicate globally by YouTube Video ID while retaining many-to-many Playlist membership and each Playlist's observed order.
- Create a recoverable VideoScript job for every newly discovered or restored incomplete video.
- Automatically process one waiting job at a time and continue with the next until the queue is empty.
- Prefer creator-provided captions, then YouTube automatic captions, and use local Whisper `medium` only when no usable caption track exists or caption retrieval fails technically.
- Preserve the existing job folders, full job IDs, transcript naming, interruption recovery, and managed-media cleanup.
- Expose tray status and the commands Open VideoScript, Sync Now, Pause/Resume Auto Sync, and Exit VideoScript.

## Non-goals

- Phase 1 does not add drag-and-drop ordering, search, Playlist filtering, or manual priority controls; those belong to Phase 2.
- Phase 1 does not modify or remove YouTube Playlist entries and does not use Google OAuth; that remains Phase 4.
- Phase 1 does not automatically judge subtle caption accuracy, compare two transcripts semantically, or create side-by-side YouTube/Whisper versions.
- Phase 1 does not migrate all existing job metadata into SQLite or rebuild the current job-folder tree.
- Phase 1 does not run as a Windows service or continue after the user exits the tray companion/uses `Stop.cmd`.

## Selected approach

Use SQLite plus yt-dlp. SQLite owns Playlist configuration, globally unique YouTube videos, Playlist/video membership, synchronization history, and the link from a discovered video to its existing filesystem job. The current `JobStore` remains authoritative for transcript content and worker progress. This avoids a risky storage rewrite while giving Phase 2 durable queue-ready records.

Rejected alternatives:

- YouTube Data API was deferred because it introduces API keys, quotas, and OAuth before Playlist mutation is needed.
- JSON Playlist files were rejected because global deduplication and many-to-many Playlist membership become fragile.
- Browser-session polling was rejected because it stops when no Streamlit session is active.
- A Windows service was deferred because installation, privileges, upgrades, and diagnostics add complexity without improving sync speed.

## Runtime structure

`Start.ps1` launches one tray companion process in addition to the existing Streamlit server. The tray process owns a single-instance lock, a stoppable 10-minute sync loop, and tray UI state. Closing the browser does not stop it. `Stop.ps1` and the tray Exit command stop both tray and Streamlit processes safely using validated PID files.

The tray menu contains:

- Open VideoScript
- Sync Now
- Pause/Resume Auto Sync
- Exit VideoScript

The tooltip reports the latest state, for example `VideoScript · 3 waiting · synced 5 minutes ago`. Icon states are green (healthy), blue (syncing or transcribing), yellow (paused), and red (last background operation failed). Errors remain visible in the web UI and logs; a later successful sync clears the red sync-error state.

If tray support cannot initialize, startup reports the error rather than silently running an invisible monitor. The web server remains independently startable for diagnosis.

## SQLite data model

Store `data/videoscript.db` with foreign keys enabled, a schema version, and transactions around each synchronization.

- `playlists`: internal ID, canonical URL, YouTube Playlist ID (unique), title, enabled flag, created time, last sync start/success times, last error, and observed video count.
- `videos`: YouTube Video ID (primary key), canonical watch URL, latest title, duration when available, availability state, and first/last seen times.
- `playlist_videos`: Playlist ID + Video ID (unique pair), latest observed position, first/last seen times, present flag, and completed-at-removal archive fields for future phases.
- `playlist_jobs`: Video ID (unique) to full filesystem job ID, creation reason, selected processing method, and lifecycle timestamps.
- `queue_items`: linked job ID (unique), monotonic FIFO position, waiting/processing/interrupted/completed/failed state, retry count, and timestamps. Phase 2 will reuse this table when it adds drag-and-drop ordering.
- `settings`: auto-sync paused state and schema-level application settings required by the tray process.

SQLite never stores transcript bodies. Database writes use short transactions, WAL mode, busy timeout, and parameterized queries so Streamlit and the tray process can safely share the file.

## Playlist validation and synchronization

Adding a Playlist first canonicalizes and validates a YouTube Playlist URL, rejects non-Playlist URLs, and performs a lightweight yt-dlp flat extraction. The UI shows a clear error if the Playlist is private, unavailable, malformed, or blocked. Duplicate Playlist IDs reopen the existing record instead of creating another.

For every sync:

1. Mark sync start without changing user-controlled state.
2. Fetch flat Playlist metadata through yt-dlp with playlist extraction enabled only for this code path.
3. Upsert video metadata and Playlist membership in observed order.
4. Mark prior memberships absent when they are no longer returned.
5. Reuse an existing `playlist_jobs` link for a globally known video.
6. Before creating work, reconcile existing YouTube jobs by canonical URL/Video ID so a manually submitted 1.0 job is linked rather than retranscribed.
7. If a video has no linked job, or its linked incomplete job is genuinely missing from `JobStore`, create one waiting job and queue/link it through a recoverable two-store protocol.
8. Never recreate a completed job, and never create a second job because the same video appears in another Playlist.
9. Append newly discovered work only; Phase 1 never reorders existing waiting work.
10. Commit membership/job-link changes, then record success time and counts. On fetch failure, retain the last successful snapshot and record the error.

Filesystem job creation and SQLite linking cannot share one atomic transaction. Every Playlist-created job therefore stores `youtube_video_id` and a deterministic discovery marker in `job.json`. Synchronization/recovery scans both stores: it links an orphan job with the matching Video ID before creating anything, and only creates a new job when neither store contains one. A crash may leave a repairable orphan but must not produce two runnable jobs.

Removing a Playlist from VideoScript soft-deletes/disables its monitoring record after user confirmation while retaining membership and synchronization history. Re-adding the same Playlist ID reactivates that record. It never deletes completed transcripts. A waiting item with no remaining active Playlist source may be removed from the FIFO queue; processing, interrupted, failed, and completed jobs remain. Videos shared with another active Playlist retain their job and queue relationship.

## Caption-first job preparation

Playlist jobs retain canonical video URL, Video ID, source Playlist memberships, and default `model='medium'` for fallback.

When a discovered job is prepared for processing:

1. Inspect video caption tracks.
2. Prefer creator-provided captions in the video's original/default language.
3. Otherwise choose YouTube automatic captions in that language.
4. Do not prefer auto-translated tracks over original-language tracks.
5. Treat only objective failures as unusable: no matching track, download failure, parse failure, or empty/no-valid-text content.
6. On an objective failure, enqueue/mark the same job for local Whisper `medium`; do not create a duplicate job.

Caption selection uses yt-dlp's original/default language metadata when available, normalizes language tags to their base language, prefers manual over automatic tracks, and uses a stable lexical track-key tie-break. If original language is unavailable, choose the first deterministic manual track, then the first deterministic automatic track. The selected track key and reason are persisted for diagnosis.

Subtle transcription errors, terminology mistakes, accents, and semantic quality are not automatically classified. A manual “re-run with Whisper” workflow and preservation of multiple transcript versions are deferred to Phase 4 unless separately promoted.

## UI

Add a Playlist management section without redesigning the existing single-video/local-upload screens. It provides:

- Playlist URL input and Add action.
- A compact row per Playlist with title, enabled state, last successful sync, last error, video count, discovered/job progress, Sync Now, and Remove.
- A global auto-sync paused/running indicator matching the tray state.
- A list of recently discovered videos and their linked existing job status for diagnosis.

Phase 1-created jobs appear in the existing history view. A compact read-only FIFO view shows waiting and active work; Phase 2 replaces it with drag-and-drop controls without changing queue storage.

## Minimal FIFO runner

The tray companion owns queue scheduling. It claims at most one waiting item transactionally, then uses the existing caption extraction and worker mechanisms. Caption extraction may complete quickly in the companion; Whisper fallback launches the existing single worker and waits for its durable terminal state before claiming the next item. The runner never starts a second transcription while the existing cross-process transcription lock is held.

On startup it converts a stale processing item whose worker is gone to interrupted/waiting according to the existing recovery rules, then resumes FIFO processing. Failed work stops retrying automatically after one automatic retry and remains available for manual retry. Pausing auto sync stops Playlist polling but does not stop the current transcription or FIFO runner; queue pause/stop controls arrive in Phase 2. Exiting VideoScript safely stops scheduling and uses the existing worker-stop semantics for an active task.

## Failure and recovery rules

- Network/YouTube errors never erase the last successful Playlist snapshot.
- Per-Playlist failure does not block other Playlists from syncing.
- A sync interrupted mid-run rolls back that Playlist's database transaction.
- Duplicate tray processes are rejected by a cross-process lock.
- Automatic, tray, and web sync requests use one cross-process global sync lock. Overlapping requests coalesce into one pending follow-up run rather than executing concurrently.
- The tray performs an immediate sync at startup and when auto sync resumes, then waits 10 minutes between runs. Manual Sync Now works even while automatic sync is paused.
- Database migrations are versioned, transactional, and backed up before a destructive future migration; the initial schema only creates new files/tables.
- A database record pointing to a missing incomplete job is repaired on the next sync; a completed record is never silently retranscribed.
- If YouTube temporarily omits or hides an item, mark membership absent rather than deleting video/job history.
- Existing 1.0 jobs remain accessible even if the database is unavailable.

## Security and privacy

- Bind Streamlit to `127.0.0.1` as today.
- Accept only canonical HTTPS YouTube Playlist URLs and canonical video IDs/URLs.
- Do not read browser cookies or credentials in Phase 1.
- Do not follow filesystem links when locating jobs or database files.
- Store all state locally under `data/`; do not introduce telemetry.

## Verification and acceptance

Automated tests cover URL validation, schema creation/migration, concurrent access, duplicate Playlist/video handling, shared videos across Playlists, membership removal/reappearance, missing-job repair, completed-job preservation, caption-selection order, Whisper fallback, sync failure rollback, pause/resume state, single-instance tray behavior, and Start/Stop lifecycle.

Phase 1 is accepted when:

- Multiple Playlists can be added, enabled/disabled, manually synced, and automatically synced every 10 minutes while the tray companion runs.
- New Video IDs create at most one linked job globally and re-syncing creates no duplicate.
- A shared video retains every Playlist membership but only one job.
- Creator captions are selected before automatic captions; missing/broken captions select Whisper `medium`.
- Waiting jobs run one at a time in durable FIFO order and the next job starts automatically after a terminal completion.
- Existing 1.0 jobs and manual workflows still pass their regression tests.
- Tray status and four commands work without requiring the browser to remain open.
- Restarting VideoScript preserves Playlists, pause state, synchronization history, and all discovered/job links.

## Builder calls

- Choose focused Python modules for database, sync, and tray responsibilities; do not expand `pipeline.py` or `app.py` into new monoliths.
- Use a small maintained tray library and the already-installed Pillow dependency; pin any new package in both requirement files.
- Keep the Phase 1 FIFO schema directly reusable by Phase 2; do not build a second provisional queue.
