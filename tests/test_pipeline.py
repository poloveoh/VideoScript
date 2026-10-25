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
    assert (tmp_path / job['id'] / 'transcript.txt').read_text('utf-8') == 'A useful idea.'


def test_store_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        JobStore(tmp_path).load('../outside')


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
