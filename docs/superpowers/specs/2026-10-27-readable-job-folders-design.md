# Readable Job Folders Design

## Intent

New and existing job folders use `<sanitized video title>_<first eight job-id characters>` while the full ID in `job.json` and worker arguments stays unchanged.

## Behavior

- `JobStore` resolves jobs by the full ID stored in `job.json`, not by directory name.
- Initialization migrates legacy directories named as the full 32-character ID.
- Migration is idempotent and validates that the directory name and metadata ID match.
- Invalid metadata, destination collisions, busy files, or permission errors leave the legacy folder usable and do not block startup.
- Managed media paths inside a renamed folder are rebased and persisted.
- Windows-invalid characters, reserved names, trailing dots/spaces, empty names, long UTF-16 titles, and duplicate titles remain safe.

## Scope

Reuse the existing title sanitizer and keep transcript/subtitle naming unchanged. Do not add shortcuts, indexes, dependencies, or change IDs.

## Approaches

Physical rename with legacy fallback was selected over directory links or a second index because it gives a readable Explorer layout with one canonical data tree.
