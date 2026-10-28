"""Deterministic caption preparation for durable Playlist jobs."""
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from pipeline import (JobStore, extract_captions, extract_youtube_video_id,
                      inspect_youtube)
from playlist_db import PlaylistDB


@dataclass(frozen=True)
class CaptionSelection:
    kind: str
    language: str
    reason: str


def _language(value):
    if not isinstance(value, str):
        return ''
    return value.strip().lower().replace('_', '-').removesuffix('-orig')


def _usable_tracks(tracks):
    return sorted((track for track in tracks
                   if track.get('ext') == 'json3' and track.get('url')
                   and not track.get('is_translated')
                   and not track.get('is_dubbed')
                   and not track.get('is_dub')
                   and 'tlang' not in parse_qs(urlsplit(track['url']).query,
                                              keep_blank_values=True)),
                  key=lambda track: track['url'])


def _original_language(info):
    language = _language(info.get('language'))
    if language:
        return language
    # yt-dlp's YouTube extractor marks original audio as 10 and default as 5.
    for preference in (10, 5):
        languages = {_language(fmt.get('language')).split('-')[0]
                     for fmt in info.get('formats') or []
                     if fmt.get('acodec') != 'none'
                     and fmt.get('language_preference') == preference
                     and _language(fmt.get('language'))}
        if languages:
            return next(iter(languages)) if len(languages) == 1 else ''
    languages = {_language(key).split('-')[0]
                 for key, tracks in (info.get('automatic_captions') or {}).items()
                 if key.endswith('-orig') and _usable_tracks(tracks)}
    return next(iter(languages)) if len(languages) == 1 else ''


def choose_caption(info: dict) -> CaptionSelection | None:
    original = _original_language(info)
    candidates = []
    for kind, field in [('manual', 'subtitles'), ('auto', 'automatic_captions')]:
        for language, tracks in (info.get(field) or {}).items():
            if language == 'live_chat' or not _usable_tracks(tracks):
                continue
            normalized = _language(language)
            matches = bool(original and normalized.split('-')[0] == original.split('-')[0])
            if original and not matches:
                continue
            candidates.append((not matches, kind != 'manual', normalized, language, kind))
    if not candidates:
        return None
    unmatched, _, _, language, kind = min(candidates)
    reason = 'original_language' if not unmatched else 'deterministic_fallback'
    return CaptionSelection(kind, language, reason)


def prepare_playlist_job(job_id: str, store: JobStore, db: PlaylistDB) -> dict:
    """Prepare the claimed job; its queue lifecycle remains owned by the runner."""
    job = store.load(job_id)
    if job['status'] == 'complete':
        return job
    video_id = job.get('youtube_video_id') or extract_youtube_video_id(job['url'])
    info = inspect_youtube(job['url'])
    selection = choose_caption(info)
    error = 'No usable JSON3 caption track'
    if selection is not None:
        job.update(caption_kind=selection.kind, caption_language=selection.language,
                   caption_reason=selection.reason)
        store.save(job)
        field = 'subtitles' if selection.kind == 'manual' else 'automatic_captions'
        selected_track = _usable_tracks(info[field][selection.language])[0]
        try:
            result = extract_captions(
                info, (selection.kind, selection.language), store, job,
                track=selected_track,
            )
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            if exc.__cause__ is not None:
                error += f' ({type(exc.__cause__).__name__}: {exc.__cause__})'
        else:
            db.link_job(video_id, job_id, selection.kind)
            return result
    else:
        job.update(caption_kind=None, caption_language=None,
                   caption_reason='no_usable_json3')
    job.update(status='waiting', model='medium', method='whisper', error='',
               caption_error=error)
    store.save(job)
    db.link_job(video_id, job_id, 'whisper')
    return job
