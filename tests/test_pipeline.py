import json
import multiprocessing
from pathlib import Path

import pytest

from pipeline import (JobStore, process_transcription, read_caption,
                      subtitle_choices, transcribe)


def _write_edits_from_process(root, job_id, prefix, start):
    store = JobStore(root)
    start.wait()
    for number in range(80):
        store.save_edits(job_id, f'{prefix} {number}')


def test_history_recovers_partial_output_without_claiming_complete(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('Lecture', 'local', '')
    job['segments'] = [{'start': 0, 'end': 2, 'text': 'A useful idea.'}]
    job['status'] = 'running'
    store.save(job)
    recovered = JobStore(tmp_path).list()[0]
    assert recovered['segments'][0]['text'] == 'A useful idea.'
    assert recovered['status'] == 'running'
    assert (store.directory(job['id']) / 'Lecture.txt').read_text('utf-8') == 'A useful idea.'


def test_store_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        JobStore(tmp_path).load('../outside')


def test_new_jobs_use_readable_folders_resolved_by_full_id(tmp_path):
    store = JobStore(tmp_path)
    first = store.create('影片标题.mp4', 'local', '')
    second = store.create('影片标题.mp4', 'local', '')

    assert store.directory(first['id']).name == f"影片标题_{first['id'][:8]}"
    assert store.directory(second['id']).name == f"影片标题_{second['id'][:8]}"
    assert store.directory(first['id']) != store.directory(second['id'])
    assert store.load(first['id'])['id'] == first['id']


def test_directory_falls_back_to_legacy_full_id_folder(tmp_path):
    store = JobStore(tmp_path)
    job_id = 'a' * 32
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / 'job.json').write_text(json.dumps({
        'id': job_id, 'title': 'Legacy', 'created': 1,
    }), encoding='utf-8')

    assert store.directory(job_id) == folder
    assert store.load(job_id)['title'] == 'Legacy'


def test_list_deduplicates_same_job_id_and_prefers_readable_folder(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('Lecture', 'local', '')
    readable = store.directory(job['id'])
    legacy = tmp_path / job['id']
    legacy.mkdir()
    (legacy / 'job.json').write_text(
        (readable / 'job.json').read_text('utf-8'), encoding='utf-8',
    )

    assert [item['id'] for item in store.list()] == [job['id']]


def _write_legacy_job(root, job_id='a' * 32, title='Lecture', **fields):
    folder = root / job_id
    folder.mkdir()
    job = {
        'id': job_id, 'title': title, 'source': 'local', 'url': '',
        'created': 1, 'status': 'running', 'segments': [], 'error': '',
        'edited_text': None, 'output_naming': 'title', **fields,
    }
    (folder / 'job.json').write_text(json.dumps(job), encoding='utf-8')
    return folder, job


def test_initialization_migrates_legacy_folder_once(tmp_path):
    legacy, job = _write_legacy_job(tmp_path)

    store = JobStore(tmp_path)
    migrated = store.directory(job['id'])

    assert migrated.name == 'Lecture_aaaaaaaa'
    assert not legacy.exists()
    assert JobStore(tmp_path).directory(job['id']) == migrated


@pytest.mark.parametrize('payload', [
    '{not json',
    json.dumps({'id': 'b' * 32, 'title': 'Wrong job'}),
])
def test_migration_ignores_invalid_or_mismatched_metadata(tmp_path, payload):
    legacy = tmp_path / ('a' * 32)
    legacy.mkdir()
    (legacy / 'job.json').write_text(payload, encoding='utf-8')

    JobStore(tmp_path)

    assert legacy.is_dir()


def test_migration_collision_keeps_legacy_job_loadable(tmp_path):
    legacy, job = _write_legacy_job(tmp_path)
    destination = tmp_path / 'Lecture_aaaaaaaa'
    destination.mkdir()
    (destination / 'job.json').write_text(json.dumps({
        'id': 'b' * 32, 'title': 'Other', 'created': 2,
    }), encoding='utf-8')

    store = JobStore(tmp_path)

    assert legacy.is_dir()
    assert store.directory(job['id']) == legacy
    assert store.load(job['id'])['title'] == 'Lecture'


def test_migration_rename_failure_keeps_legacy_job_loadable(
        tmp_path, monkeypatch):
    legacy, job = _write_legacy_job(tmp_path)
    original_rename = Path.rename

    def fail_legacy_rename(path, target):
        if path == legacy:
            raise PermissionError('busy')
        return original_rename(path, target)

    monkeypatch.setattr(Path, 'rename', fail_legacy_rename)
    store = JobStore(tmp_path)

    assert store.directory(job['id']) == legacy
    assert store.load(job['id'])['title'] == 'Lecture'
    assert store.migration_errors == [f'{job["id"]}: busy']


def test_migration_does_not_follow_legacy_directory_symlink(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    job_id = 'a' * 32
    (outside / 'job.json').write_text(json.dumps({
        'id': job_id, 'title': 'Outside', 'created': 1,
    }), encoding='utf-8')
    link = tmp_path / job_id
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks are unavailable')

    store = JobStore(tmp_path)

    assert link.is_symlink()
    assert not (tmp_path / 'Outside_aaaaaaaa').exists()
    assert store.directory(job_id) == link


def test_migration_rebases_managed_media_path_for_cleanup(tmp_path):
    job_id = 'a' * 32
    legacy = tmp_path / job_id
    media = legacy / 'input.mp4'
    legacy, job = _write_legacy_job(
        tmp_path, job_id=job_id, status='complete',
        segments=[{'start': 0, 'end': 1, 'text': 'Words'}],
        media_path=str(media), media_managed=True, auto_cleanup_media=True,
    )
    media.write_bytes(b'media')
    (legacy / 'Lecture.txt').write_text('Words', encoding='utf-8')
    (legacy / 'Lecture.srt').write_text(
        '1\n00:00:00,000 --> 00:00:01,000\nWords\n', encoding='utf-8',
    )

    store = JobStore(tmp_path)
    migrated = store.directory(job_id)
    loaded = store.load(job_id)

    assert migrated.name == 'Lecture_aaaaaaaa'
    assert Path(loaded['media_path']) == migrated / 'input.mp4'
    assert store.cleanup_media(job_id)['cleanup_status'] == 'complete'
    assert not (migrated / 'input.mp4').exists()


def test_title_named_outputs_preserve_edits_and_separate_duplicate_titles(tmp_path):
    store = JobStore(tmp_path)
    first = store.create('影片标题.mp4', 'local', '')
    second = store.create('影片标题.mp4', 'local', '')
    store.save_edits(first['id'], '修改后的文字。')
    assert (store.directory(first['id']) / '影片标题.txt').read_text('utf-8') == '修改后的文字。'
    assert (store.directory(first['id']) / '影片标题.srt').is_file()
    assert (store.directory(second['id']) / '影片标题.txt').read_text('utf-8') == ''
    assert not (store.directory(first['id']) / 'transcript.txt').exists()


def test_legacy_output_names_remain_editable_and_allow_verified_cleanup(tmp_path):
    store = JobStore(tmp_path)
    folder = store.directory('a' * 32)
    folder.mkdir()
    media = folder / 'input.mp4'
    media.write_bytes(b'media')
    legacy = {
        'id': 'a' * 32, 'title': '旧影片', 'source': 'local', 'url': '',
        'created': 1, 'status': 'complete', 'edited_text': None,
        'segments': [{'start': 0, 'end': 1, 'text': '原文'}],
        'media_path': str(media), 'media_managed': True, 'auto_cleanup_media': True,
    }
    (folder / 'job.json').write_text(json.dumps(legacy), encoding='utf-8')
    (folder / 'transcript.txt').write_text('原文', encoding='utf-8')
    (folder / 'original.srt').write_text('', encoding='utf-8')
    store.save_edits(legacy['id'], '校对文字')
    assert (folder / 'transcript.txt').read_text('utf-8') == '校对文字'
    assert store.cleanup_media(legacy['id'])['cleanup_status'] == 'complete'
    assert not media.exists()


def test_subtitle_choices_distinguish_manual_and_auto():
    info = {'subtitles': {'en': [{'ext': 'json3', 'url': 'https://x'}]},
            'automatic_captions': {'zh': [{'ext': 'json3', 'url': 'https://y'}],
                                   'live_chat': [{'ext': 'json', 'url': 'https://z'}]}}
    assert subtitle_choices(info) == [('manual', 'en'), ('auto', 'zh')]


def test_caption_reader_does_not_treat_html_error_as_transcript(tmp_path):
    path = tmp_path / 'bad.json3'
    path.write_text('<html>Access denied</html>', encoding='utf-8')
    with pytest.raises(ValueError, match='字幕'):
        read_caption(path)


@pytest.mark.parametrize('exact', [False, True])
def test_caption_extraction_supports_legacy_choice_and_exact_track(tmp_path, monkeypatch, exact):
    from io import BytesIO
    import yt_dlp
    from pipeline import extract_captions

    store = JobStore(tmp_path)
    job = store.create('Captions', 'youtube', 'https://www.youtube.com/watch?v=abcdefghijk')
    tracks = [{'ext': 'json3', 'url': 'https://x/first'},
              {'ext': 'json3', 'url': 'https://x/selected'}]
    requested = []

    class YoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def urlopen(self, url):
            requested.append(url)
            return BytesIO(b'{"events":[{"segs":[{"utf8":"Caption words."}]}]}')

    monkeypatch.setattr(yt_dlp, 'YoutubeDL', YoutubeDL)
    result = extract_captions({'subtitles': {'en': tracks}}, ('manual', 'en'), store, job,
                              **({'track': tracks[1]} if exact else {}))
    assert requested == ['https://x/selected' if exact else 'https://x/first']
    assert result['status'] == 'complete'
    assert (store.directory(job['id']) / 'Captions.txt').read_text('utf-8') == 'Caption words.'


def test_transcription_failure_preserves_completed_segments(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('test', 'local', '')

    def chunks():
        yield {'start': 0, 'end': 2, 'text': 'Saved before failure.'}
        raise RuntimeError('decoder stopped')

    with pytest.raises(RuntimeError, match='decoder stopped'):
        process_transcription(store, job, chunks(), 5, lambda *args: None)
    result = store.load(job['id'])
    assert result['status'] == 'failed'
    assert result['segments'][0]['text'] == 'Saved before failure.'


def test_successful_transcription_is_saved_and_reports_completion(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('test', 'local', '')
    events = []
    result = process_transcription(store, job, iter([
        {'start': 0, 'end': 2, 'text': 'Actual words.'}
    ]), 3, lambda progress, message: events.append(progress))
    assert result['status'] == 'complete'
    assert events[-1] == 1.0
    assert store.load(job['id'])['segments'][0]['text'] == 'Actual words.'


def test_transcribe_keeps_required_cpu_settings_and_resume_checkpoint(
        tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = store.create('test', 'local', '')
    job['segments'] = [{'start': 0, 'end': 2, 'text': 'Existing.'}]
    store.save(job)
    captured = {}

    class Info:
        language = 'ms'
        duration = 4

    class Model:
        def transcribe(self, path, **options):
            captured.update(path=path, **options)
            return iter([
                type('Segment', (), {'start': 2, 'end': 4, 'text': ' New.'})(),
            ]), Info()

    monkeypatch.setattr('pipeline.load_model', lambda size: Model())
    transcribe(
        tmp_path / 'audio.wav', 'small', 'ms', '', store, job,
        lambda *_: None,
    )

    assert captured['beam_size'] == 2
    assert captured['vad_filter'] is False
    assert captured['clip_timestamps'] == [2]


def test_worker_progress_preserves_edits_from_another_tab(tmp_path):
    store = JobStore(tmp_path)
    worker = store.create('lecture', 'local', '')
    worker['segments'] = [{'start': 0, 'end': 1, 'text': 'First.'}]
    store.save(worker)
    store.save_edits(worker['id'], 'My corrected words.')
    worker['segments'].append({'start': 1, 'end': 2, 'text': 'Second.'})
    store.save(worker)
    result = store.load(worker['id'])
    assert len(result['segments']) == 2
    assert result['edited_text'] == 'My corrected words.'
    assert result['status'] == 'running'


def test_concurrent_edits_do_not_race_on_temporary_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = JobStore(tmp_path)
    job = store.create('lecture', 'local', '')
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda i: store.save_edits(job['id'], f'Edit {i}'), range(30)))
    assert store.load(job['id'])['edited_text'].startswith('Edit ')


def test_separate_processes_do_not_race_on_job_files(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('lecture', 'local', '')
    context = multiprocessing.get_context('spawn')
    start = context.Event()
    processes = [
        context.Process(
            target=_write_edits_from_process,
            args=(tmp_path, job['id'], prefix, start),
        )
        for prefix in ('First', 'Second')
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert store.load(job['id'])['edited_text'].split()[0] in {'First', 'Second'}


def test_old_jobs_remain_accessible_in_history(tmp_path):
    store = JobStore(tmp_path)
    first = store.create('First lecture', 'local', '')
    for i in range(50):
        store.create(f'Lecture {i}', 'local', '')
    assert first['id'] in [job['id'] for job in store.list()]


def test_stop_request_is_persisted_until_cleared(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('Lecture', 'local', '')
    assert not store.stop_requested(job['id'])
    store.request_stop(job['id'])
    assert JobStore(tmp_path).stop_requested(job['id'])
    assert store.load(job['id'])['status'] == 'stopping'
    store.clear_stop(job['id'])
    assert not store.stop_requested(job['id'])


def test_stop_request_does_not_overwrite_newer_committed_segments(
        tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = store.create('Lecture', 'local', '')
    stale = dict(job)
    job['segments'] = [
        {'start': 0, 'end': 2, 'text': 'Committed while Stop was clicked.'},
    ]
    store.save(job)
    monkeypatch.setattr(store, 'load', lambda _job_id: stale)

    store.request_stop(job['id'])

    saved = JobStore(tmp_path).load(job['id'])
    assert [part['text'] for part in saved['segments']] == [
        'Committed while Stop was clicked.',
    ]
    assert saved['status'] == 'stopping'


def test_transcription_stop_preserves_only_committed_segments(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('Lecture', 'local', '')

    def report(*_):
        store.request_stop(job['id'])

    process_transcription(store, job, iter([
        {'start': 0, 'end': 2, 'text': 'Keep this.'},
        {'start': 2, 'end': 4, 'text': 'Do not append this.'},
    ]), 10, report)

    saved = store.load(job['id'])
    assert saved['status'] == 'paused'
    assert [part['text'] for part in saved['segments']] == ['Keep this.']


def test_resume_skips_segments_at_or_before_saved_checkpoint(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('Lecture', 'local', '')
    job['segments'] = [{'start': 0, 'end': 2, 'text': 'Already saved.'}]
    store.save(job)

    process_transcription(store, job, iter([
        {'start': 0, 'end': 2, 'text': 'Duplicate.'},
        {'start': 2, 'end': 4, 'text': 'New words.'},
    ]), 4, lambda *_: None)

    saved = store.load(job['id'])
    assert [part['text'] for part in saved['segments']] == [
        'Already saved.', 'New words.',
    ]


def _completed_job_with_managed_media(store):
    job = store.create('Lecture', 'local', '')
    media = store.directory(job['id']) / 'input.mp4'
    media.write_bytes(b'temporary media')
    job.update(
        status='complete',
        segments=[{'start': 0, 'end': 2, 'text': 'Finished transcript.'}],
        media_path=str(media),
        media_managed=True,
        auto_cleanup_media=True,
    )
    store.save(job)
    return job, media


def test_cleanup_deletes_managed_media_only_after_verified_completion(tmp_path):
    store = JobStore(tmp_path)
    job, media = _completed_job_with_managed_media(store)

    result = store.cleanup_media(job['id'])

    assert not media.exists()
    assert result['status'] == 'complete'
    assert result['cleanup_status'] == 'complete'
    assert result['cleanup_deleted_bytes'] == len(b'temporary media')
    assert (store.directory(job['id']) / 'Lecture.txt').read_text('utf-8') == (
        'Finished transcript.'
    )
    assert (store.directory(job['id']) / 'Lecture.srt').is_file()


@pytest.mark.parametrize('status', ['paused', 'stopping', 'failed', 'running'])
def test_cleanup_keeps_media_for_incomplete_jobs(tmp_path, status):
    store = JobStore(tmp_path / status)
    job, media = _completed_job_with_managed_media(store)
    store.update_fields(job['id'], status=status)

    result = store.cleanup_media(job['id'])

    assert media.exists()
    assert result['status'] == status
    assert 'cleanup_status' not in result


def test_cleanup_never_deletes_external_or_unowned_media(tmp_path):
    store = JobStore(tmp_path / 'jobs')
    external = tmp_path / 'original.mp4'
    external.write_bytes(b'original')
    job = store.create('Lecture', 'local', '')
    job.update(
        status='complete',
        segments=[{'start': 0, 'end': 1, 'text': 'Done.'}],
        media_path=str(external),
        media_managed=False,
        auto_cleanup_media=True,
    )
    store.save(job)

    result = store.cleanup_media(job['id'])

    assert external.exists()
    assert result['cleanup_status'] == 'not_managed'


def test_cleanup_is_disabled_safely_for_existing_jobs(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('Legacy', 'local', '')
    media = store.directory(job['id']) / 'input.mp4'
    media.write_bytes(b'legacy media')
    job.update(
        status='complete',
        segments=[{'start': 0, 'end': 1, 'text': 'Legacy transcript.'}],
        media_path=str(media),
    )
    store.save(job)

    result = store.cleanup_media(job['id'])

    assert media.exists()
    assert result['cleanup_status'] == 'disabled'


def test_cleanup_rejects_managed_path_outside_job_directory(tmp_path):
    store = JobStore(tmp_path / 'jobs')
    external = tmp_path / 'outside.mp4'
    external.write_bytes(b'original')
    job = store.create('Lecture', 'local', '')
    job.update(
        status='complete',
        segments=[{'start': 0, 'end': 1, 'text': 'Done.'}],
        media_path=str(external),
        media_managed=True,
        auto_cleanup_media=True,
    )
    store.save(job)

    result = store.cleanup_media(job['id'])

    assert external.exists()
    assert result['cleanup_status'] == 'failed'
    assert '任务目录' in result['cleanup_error']


def test_cleanup_keeps_media_when_persisted_transcript_is_not_verified(tmp_path):
    store = JobStore(tmp_path)
    job, media = _completed_job_with_managed_media(store)
    (store.directory(job['id']) / 'Lecture.txt').write_text(
        'truncated', encoding='utf-8',
    )

    result = store.cleanup_media(job['id'])

    assert media.exists()
    assert result['status'] == 'complete'
    assert result['cleanup_status'] == 'failed'
    assert '文字稿' in result['cleanup_error']
    assert (store.directory(job['id']) / 'Lecture.txt').read_text('utf-8') == (
        'truncated'
    )


def test_cleanup_rejects_symbolic_media_link_without_unlinking(
        tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job, media = _completed_job_with_managed_media(store)
    original_is_symlink = Path.is_symlink

    def identify_media_as_symlink(path):
        if path == media:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, 'is_symlink', identify_media_as_symlink)

    result = store.cleanup_media(job['id'])

    assert media.exists()
    assert result['cleanup_status'] == 'failed'
    assert '符号链接' in result['cleanup_error']


def test_cleanup_failure_can_be_retried_without_transcription(
        tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job, media = _completed_job_with_managed_media(store)
    original_unlink = Path.unlink

    def fail_media_unlink(path, *args, **kwargs):
        if path == media:
            raise PermissionError('file is busy')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as active:
        active.setattr(Path, 'unlink', fail_media_unlink)
        failed = store.cleanup_media(job['id'])

    assert media.exists()
    assert failed['status'] == 'complete'
    assert failed['cleanup_status'] == 'failed'
    assert 'file is busy' in failed['cleanup_error']

    retried = store.cleanup_media(job['id'])
    assert not media.exists()
    assert retried['cleanup_status'] == 'complete'


@pytest.mark.parametrize('url, expected', [
    ('https://youtu.be/abcdefghijk?t=5', 'abcdefghijk'),
    ('https://www.youtube.com/shorts/abcdefghijk', 'abcdefghijk'),
    ('https://youtube.com/watch?v=abcdefghijk&list=PLtest', 'abcdefghijk'),
    ('https://example.com/watch?v=abcdefghijk', None),
    ('https://youtube.com/watch?v=abcdefghijkX', None),
    ('not a url', None),
])
def test_extract_youtube_job_video_id(url, expected):
    from pipeline import extract_youtube_video_id
    assert extract_youtube_video_id(url) == expected


def test_find_youtube_job_reconciles_legacy_completed_duplicates(tmp_path):
    store = JobStore(tmp_path)
    first = store.create('Completed', 'youtube', 'https://youtu.be/abcdefghijk')
    store.update_fields(first['id'], status='complete')
    store.create('Running', 'youtube', 'https://youtube.com/watch?v=abcdefghijk')
    store.create('Local', 'local', 'https://youtu.be/abcdefghijk')
    assert store.find_youtube_job('abcdefghijk')['id'] == first['id']
    assert store.find_youtube_job('abcdefghijkX') is None


def test_find_youtube_job_prefers_explicit_identity_and_is_deterministic(tmp_path):
    store = JobStore(tmp_path)
    legacy = store.create('Legacy', 'youtube', 'https://youtu.be/abcdefghijk')
    store.update_fields(legacy['id'], status='complete')
    explicit = store.create('Explicit', 'youtube', '')
    store.update_fields(explicit['id'], youtube_video_id='abcdefghijk')
    assert store.find_youtube_job('abcdefghijk')['id'] == explicit['id']
    duplicate = store.create('Duplicate', 'youtube', '')
    store.update_fields(duplicate['id'], youtube_video_id='abcdefghijk', status='complete')
    store.update_fields(explicit['id'], status='complete')
    assert store.find_youtube_job('abcdefghijk')['id'] == min(explicit['id'], duplicate['id'])


def test_find_youtube_job_ignores_conflicting_explicit_id(tmp_path):
    store = JobStore(tmp_path)
    job = store.create('Conflicting', 'youtube', 'https://youtu.be/abcdefghijk')
    store.update_fields(job['id'], youtube_video_id='ABCDEFGHIJK')
    assert store.find_youtube_job('abcdefghijk') is None
