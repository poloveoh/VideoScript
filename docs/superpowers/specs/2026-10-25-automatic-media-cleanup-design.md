# Automatic Media Cleanup Design

## Intent

Reduce long-term disk usage after successful transcription without risking source media or recoverability.

## Rules

- A new per-job setting, enabled by default, controls automatic cleanup.
- Only the exact media path recorded as managed by VideoScript may be deleted.
- Managed media must resolve inside that job's directory. External paths are never deleted.
- Cleanup requires `status == complete`, non-empty segments, and UTF-8 `transcript.txt` and `original.srt` contents matching the persisted job.
- Paused, stopping, failed, queued, and running jobs retain media.
- Cleanup failure leaves the transcript complete, records the error, and can be retried without transcription.
- Existing jobs without explicit ownership metadata are treated as unmanaged.

## Files

Retain `job.json`, `transcript.txt`, `original.srt`, caption sources, URLs, timestamps, and diagnostic metadata. Delete only the exact uploaded copy or YouTube audio file that VideoScript created inside the job directory.

## Approach

Centralize verification and deletion in `JobStore.cleanup_media`. Mark ownership when a job is created, invoke cleanup only after the worker persists completion, and expose a retry button for cleanup failures. This avoids extension-based deletion and keeps cleanup independent from transcription success.
