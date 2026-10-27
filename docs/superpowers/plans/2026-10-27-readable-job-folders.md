# Readable Job Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store new and existing jobs in Windows-safe `<video-title>_<8-char-id>` folders while retaining full-ID lookup and legacy compatibility.

**Architecture:** Add a length-aware job-folder helper beside the existing title sanitizer. Make `JobStore` discover directories through validated `job.json` IDs and migrate legacy ID-named folders conservatively at initialization.

**Tech Stack:** Python 3, pathlib, JSON, pytest

**Spec:** `docs/superpowers/specs/2026-10-27-readable-job-folders-design.md`

## Global Constraints

- Full IDs and worker arguments do not change; transcript/subtitle behavior stays unchanged.
- Migration is idempotent, per-folder failures do not block startup, and no dependency is added.

## Review Focus

- Duplicate and long Unicode titles remain distinct and within Windows UTF-16 limits (Task 1).
- Malformed/mismatched metadata is never migrated or resolved as another ID (Task 2).
- Destination collisions and rename failures leave legacy jobs usable (Task 2).
- Managed media paths are rebased after migration so cleanup remains safe (Task 2).

---

### Task 1: Safe readable folder names

**Files:** Modify `core.py`; test `tests/test_core.py`.

**Interfaces:** Produces `job_folder_name(title: str, job_id: str) -> str` from existing title normalization.

- [ ] Write tests for readable Chinese names, removed extensions, unsafe/reserved/empty names, duplicate ID suffixes, invalid IDs, and a 160 UTF-16-unit total limit.
- [ ] Run `.venv\Scripts\python.exe -m pytest tests/test_core.py -v`; expect failure because the helper is absent.
- [ ] Implement the helper, reserving nine UTF-16 units for `_<8-char-id>` while reusing normalization rules.
- [ ] Run the focused tests; expect all pass.
- [ ] Commit `core.py` and `tests/test_core.py` as `feat: add readable job folder names`.

### Task 2: Full-ID discovery and legacy migration

**Files:** Modify `pipeline.py`; test `tests/test_pipeline.py`.

**Interfaces:** Consumes `job_folder_name`; preserves `JobStore.directory(job_id: str) -> Path`; produces `migrate_legacy_directories() -> None`.

- [ ] Write tests proving new readable folders, distinct duplicate titles, full-ID resolution, single listing, and legacy fallback.
- [ ] Run the focused tests; expect failures from full-ID directory assumptions.
- [ ] Implement validated discovery through `*/job.json`, preferring canonical readable names and falling back to legacy paths.
- [ ] Run focused lookup tests; expect pass.
- [ ] Write tests for successful/idempotent migration, invalid/mismatched metadata, collision, rename failure, and managed-media rebasing plus cleanup.
- [ ] Run migration tests; expect failure because migration is absent.
- [ ] Implement conservative initialization migration under `STORE_LOCK`, catching per-directory errors.
- [ ] Run `tests/test_pipeline.py`; expect pass.
- [ ] Commit as `feat: migrate jobs to readable folders`.

### Task 3: Worker and application regression verification

**Files:** Test `tests/test_worker.py` and `tests/test_app.py`; modify `worker.py` or `app.py` only if a hard-coded folder assumption fails.

**Interfaces:** Consumes the unchanged full-ID `JobStore.directory()` contract.

- [ ] Add assertions that worker/history/stop-resume flows use readable folders while passing full IDs.
- [ ] Run worker/app tests; remove only exposed hard-coded path assumptions through `JobStore.directory()`.
- [ ] Run the complete suite; expect all pass.
- [ ] Commit regression tests and any necessary fixes as `test: verify readable job folder workflows`.
