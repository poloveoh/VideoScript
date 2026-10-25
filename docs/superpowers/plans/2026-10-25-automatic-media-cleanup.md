# Automatic Media Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete only VideoScript-managed temporary media after verified successful transcription.

**Architecture:** `JobStore` owns output verification and cleanup state. The worker invokes it after persisting completion; the UI records per-job ownership/settings and retries cleanup errors.

**Tech Stack:** Python 3.12, Streamlit, pytest

**Spec:** `docs/superpowers/specs/2026-10-25-automatic-media-cleanup-design.md`

## Global Constraints

- Preserve UTF-8 output and current Whisper settings.
- Never delete external source files or media for incomplete jobs.
- Keep cleanup failure separate from transcription status.

## Review Focus

- Path traversal or stale ownership metadata must not permit deletion outside the job directory.
- Output mismatch must retain media.
- Stop and failure states must retain media.
- Cleanup errors must be retryable without transcription.
- Existing jobs without ownership fields must retain media.

---

### Task 1: Safe cleanup lifecycle

**Files:**
- Modify: `pipeline.py`
- Modify: `worker.py`
- Modify: `app.py`
- Modify: `README.md`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Produces: `JobStore.cleanup_media(job_id)` returning the updated job.
- Consumes: persisted job status, segments, output files, ownership, and setting fields.

- [x] Write failing tests for successful cleanup, protected states/paths, output mismatch, retry, worker integration, and the default UI setting.
- [x] Run the focused tests and confirm failures are caused by missing cleanup behavior.
- [x] Implement the minimal cleanup method, ownership metadata, worker hook, retry UI, and documentation.
- [x] Run focused tests, then the complete suite and syntax check.
- [x] Review the diff for scope and safety, then commit as one feature change.
