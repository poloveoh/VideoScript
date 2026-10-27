import pytest

import playlist_processor as processor
from pipeline import JobStore
from playlist_db import PlaylistDB


def track(url='https://captions.example/original', **fields):
    return {'ext': 'json3', 'url': url, **fields}


@pytest.mark.parametrize('info,expected', [
    ({'language': 'en', 'subtitles': {'fr': [track()], 'en': [track()]},
      'automatic_captions': {'en': [track()]}}, ('manual', 'en')),
    ({'language': 'en', 'subtitles': {'fr': [track()]},
      'automatic_captions': {'en': [track()]}}, ('auto', 'en')),
    ({'language': ' ZH_hant ', 'subtitles': {'zh': [track()]}}, ('manual', 'zh')),
    ({'language': 'zh', 'subtitles': {'zh-TW': [track()], 'zh-CN': [track()]}},
     ('manual', 'zh-CN')),
    ({'subtitles': {'fr': [track()], 'en': [track()]},
      'automatic_captions': {'de': [track()]}}, ('manual', 'en')),
    ({'language': ['en', 'fr'], 'automatic_captions': {'fr': [track()], 'en': [track()]}},
     ('auto', 'en')),
    ({'automatic_captions': {'fr': [track('https://x?lang=en&tlang=fr')],
                            'en-orig': [track('https://x?lang=en')]}}, ('auto', 'en-orig')),
    ({'subtitles': {'en': [track(is_translated=True)],
                   'fr': [track(is_dubbed=True)],
                   'de': [track('https://x?lang=en&tlang=de')],
                   'es': [track()]}}, ('manual', 'es')),
    ({'formats': [{'language': 'fr', 'language_preference': 10, 'acodec': 'opus'},
                  {'language': 'en', 'language_preference': 5, 'acodec': 'opus'}],
      'subtitles': {'en': [track()], 'fr': [track()]}}, ('manual', 'fr')),
    ({'formats': [{'language': 'fr', 'language_preference': 5, 'acodec': 'opus'}],
      'subtitles': {'en': [track()], 'fr': [track()]}}, ('manual', 'fr')),
    ({'formats': [{'language': 'fr', 'language_preference': 10, 'acodec': 'opus'},
                  {'language': 'en', 'language_preference': 10, 'acodec': 'opus'}],
      'subtitles': {'fr': [track()], 'en': [track()]}}, ('manual', 'en')),
    ({'automatic_captions': {'en': [track()], 'fr-orig': [track()]}}, ('auto', 'fr-orig')),
])
def test_choose_caption_priority_normalization_and_stable_fallback(info, expected):
    selection = processor.choose_caption(info)
    assert (selection.kind, selection.language) == expected
    assert selection.reason
    # Dictionary insertion order must not change the selected track key.
    reversed_info = {**info}
    for field in ('subtitles', 'automatic_captions'):
        if field in info:
            reversed_info[field] = dict(reversed(list(info[field].items())))
    assert processor.choose_caption(reversed_info) == selection


@pytest.mark.parametrize('info', [
    {}, {'subtitles': {'en': [{'ext': 'vtt', 'url': 'https://x'}]}},
    {'automatic_captions': {'en': [track('https://x?tlang=en')]}},
    {'subtitles': {'live_chat': [track()], 'en': [track(is_dubbed=True)]}},
    {'language': 'de', 'subtitles': {'en': [track()]},
     'automatic_captions': {'fr': [track()]}},
])
def test_choose_caption_returns_none_without_usable_json3(info):
    assert processor.choose_caption(info) is None


@pytest.fixture
def queued_job(tmp_path):
    store = JobStore(tmp_path / 'jobs')
    db = PlaylistDB(tmp_path / 'playlists.sqlite3')
    playlist = db.add_or_reactivate_playlist('PLtest', 'https://www.youtube.com/playlist?list=PLtest', 'List')
    db.apply_snapshot(playlist.id, [{'video_id': 'abcdefghijk', 'title': 'Lecture'}], 1)
    job = store.create('Lecture', 'youtube', 'https://www.youtube.com/watch?v=abcdefghijk')
    job.update(status='waiting', youtube_video_id='abcdefghijk', discovery_source='playlist')
    store.save(job)
    db.link_job('abcdefghijk', job['id'], 'discovery')
    db.enqueue(job['id'], 'abcdefghijk')
    item = db.claim_next()
    return store, db, job, item


def caption_network(monkeypatch, payload, visited):
    from io import BytesIO
    import yt_dlp

    class YoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def urlopen(self, url):
            visited.append(url)
            if isinstance(payload, Exception):
                raise payload
            return BytesIO(payload)

    monkeypatch.setattr(yt_dlp, 'YoutubeDL', YoutubeDL)


@pytest.mark.parametrize('legacy', [False, True])
def test_prepare_completes_same_job_with_exact_caption_and_existing_outputs(
        queued_job, monkeypatch, legacy):
    store, db, job, item = queued_job
    if legacy:
        del job['youtube_video_id']
        store.save(job)
    info = {'language': 'en', 'subtitles': {'en': [
        track('https://x/a-translated?tlang=en'), track('https://x/z-original'),
        track('https://x/b-original'),
    ]}}
    inspected = []

    def inspect(url):
        inspected.append(url)
        return info

    monkeypatch.setattr(processor, 'inspect_youtube', inspect)
    visited = []
    caption_network(monkeypatch, b'{"events":[{"tStartMs":0,"dDurationMs":2000,"segs":[{"utf8":"Actual words."}]}]}', visited)
    result = processor.prepare_playlist_job(job['id'], store, db)

    assert inspected == [job['url']]
    assert visited == ['https://x/b-original']
    assert result['id'] == job['id']
    assert result['status'] == 'complete'
    assert result['caption_kind'] == 'manual'
    assert result['caption_language'] == 'en'
    assert result['caption_reason'] == 'original_language'
    assert store.load(job['id']) == result
    assert (store.directory(job['id']) / 'Lecture.txt').read_text('utf-8') == 'Actual words.'
    assert '00:00:02,000' in (store.directory(job['id']) / 'Lecture.srt').read_text('utf-8')
    assert len(store.list()) == 1
    assert not list(store.directory(job['id']).glob('audio.*'))
    with db._connect() as connection:
        rows = connection.execute('SELECT * FROM queue_items').fetchall()
        assert len(rows) == 1
        assert rows[0]['position'] == item.position
        assert rows[0]['state'] == 'processing'
        assert connection.execute('SELECT method FROM playlist_jobs').fetchone()['method'] == 'manual'


@pytest.mark.parametrize('payload,reason', [
    (OSError('caption network unavailable'), 'caption network unavailable'),
    (b'<html>denied</html>', '字幕'),
    (b'{"events":[]}', '字幕'),
])
def test_fallback_from_caption_technical_failure_keeps_identity_and_queue(
        queued_job, monkeypatch, payload, reason):
    store, db, job, item = queued_job
    monkeypatch.setattr(processor, 'inspect_youtube', lambda _: {
        'language': 'en', 'automatic_captions': {'en-orig': [track()]},
    })
    caption_network(monkeypatch, payload, [])

    result = processor.prepare_playlist_job(job['id'], store, db)

    assert result['status'] == 'waiting'
    assert result['model'] == 'medium'
    assert result['method'] == 'whisper'
    assert result['id'] == job['id']
    assert reason in result['caption_error']
    assert result['caption_kind'] == 'auto'
    assert result['caption_language'] == 'en-orig'
    assert result['caption_reason'] == 'original_language'
    assert result['segments'] == []
    assert store.load(job['id']) == result
    assert len(store.list()) == 1
    with db._connect() as connection:
        rows = connection.execute('SELECT * FROM queue_items').fetchall()
        assert len(rows) == 1
        assert rows[0]['position'] == item.position
        assert rows[0]['retry_count'] == 0
        assert rows[0]['state'] == 'processing'
        assert connection.execute('SELECT method FROM playlist_jobs').fetchone()['method'] == 'whisper'


def test_prepare_no_caption_fallback_is_runnable_medium(queued_job, monkeypatch):
    store, db, job, _ = queued_job
    monkeypatch.setattr(processor, 'inspect_youtube', lambda _: {})

    result = processor.prepare_playlist_job(job['id'], store, db)

    assert result['status'] == 'waiting'
    assert result['model'] == 'medium'
    assert result['method'] == 'whisper'
    assert result['caption_error'] == 'No usable JSON3 caption track'
    assert result['error'] == ''


def test_prepare_fallback_supports_legacy_linked_youtube_job(queued_job, monkeypatch):
    store, db, job, _ = queued_job
    del job['youtube_video_id']
    store.save(job)
    monkeypatch.setattr(processor, 'inspect_youtube', lambda _: {})
    result = processor.prepare_playlist_job(job['id'], store, db)
    assert result['status'] == 'waiting'
    assert db.find_job_id('abcdefghijk') == job['id']


def test_prepare_preserves_completed_job_without_inspection(queued_job, monkeypatch):
    store, db, job, _ = queued_job
    store.update_fields(job['id'], status='complete',
                        segments=[{'start': 0, 'end': 1, 'text': 'Saved words.'}])

    def unexpected_inspection(_):
        pytest.fail('completed jobs must not be inspected again')

    monkeypatch.setattr(processor, 'inspect_youtube', unexpected_inspection)
    result = processor.prepare_playlist_job(job['id'], store, db)
    assert result['status'] == 'complete'
    assert result['segments'][0]['text'] == 'Saved words.'
