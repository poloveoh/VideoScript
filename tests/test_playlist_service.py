import json
import sqlite3

import pytest

from pipeline import JobStore
from playlist_db import PlaylistDB
from playlist_sync import PlaylistSnapshot, PlaylistVideo


VIDEO = 'abcdefghijk'
OTHER = 'ABCDEFGHIJK'


def snapshot(*ids):
    return PlaylistSnapshot('PLtest', 'Playlist', 'https://youtube.com/playlist?list=PLtest', [
        PlaylistVideo(id, 'Video ' + id, f'https://www.youtube.com/watch?v={id}', i, 12, 'en')
        for i, id in enumerate(ids, 1)
    ])


@pytest.fixture
def setup(tmp_path):
    from playlist_service import PlaylistService
    db = PlaylistDB(tmp_path / 'playlist.sqlite')
    store = JobStore(tmp_path / 'jobs')
    playlist = db.add_or_reactivate_playlist('PLtest', 'https://youtube.com/playlist?list=PLtest', 'Playlist')
    service = PlaylistService(db, store, fetcher=lambda _: snapshot(VIDEO))
    return db, store, playlist, service


def rows(db, table):
    with sqlite3.connect(db.path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute('SELECT * FROM ' + table)]


def test_first_and_repeat_sync_create_one_waiting_job(setup):
    db, store, playlist, service = setup
    first = service.sync_playlist(playlist.id)
    second = service.sync_playlist(playlist.id)
    assert (first.created, second.created) == (1, 0)
    assert not first.error
    job, = store.list()
    assert job['status'] == 'waiting'
    assert job['source'] == 'youtube'
    assert job['url'] == 'https://www.youtube.com/watch?v=abcdefghijk'
    assert job['youtube_video_id'] == VIDEO
    assert job['model'] == 'medium'
    assert job['discovery_source'] == 'playlist'
    assert job['discovery_playlist_id'] == playlist.id
    assert job['discovery_marker'] == 'youtube:abcdefghijk'
    assert json.loads((store.directory(job['id']) / 'job.json').read_text('utf-8'))['youtube_video_id'] == VIDEO
    assert len(rows(db, 'queue_items')) == 1


def test_shared_video_has_two_memberships_one_job(setup):
    db, store, playlist, service = setup
    other = db.add_or_reactivate_playlist('PLother', 'https://youtube.com/playlist?list=PLother', 'Other')
    service.sync_playlist(playlist.id)
    service.sync_playlist(other.id)
    assert len(rows(db, 'playlist_videos')) == 2
    assert len(store.list()) == len(rows(db, 'queue_items')) == 1


@pytest.mark.parametrize('status', ['complete', 'running', 'paused', 'failed'])
def test_reconcile_manual_jobs_preserves_status_without_duplicate_work(setup, status):
    db, store, playlist, service = setup
    job = store.create('Manual', 'youtube', 'https://youtu.be/abcdefghijk')
    store.update_fields(job['id'], status=status)
    result = service.sync_playlist(playlist.id)
    assert result.created == 0
    assert db.find_job_id(VIDEO) == job['id']
    assert store.load(job['id'])['status'] == status
    if status == 'complete':
        assert rows(db, 'queue_items')[0]['state'] == 'completed'
        assert db.claim_next() is None
    else:
        assert rows(db, 'queue_items') == []


def test_snapshot_is_persisted_before_creation(setup, monkeypatch):
    db, store, playlist, service = setup
    original = store.save
    def save(job):
        assert rows(db, 'playlist_videos')[0]['video_id'] == VIDEO
        return original(job)
    monkeypatch.setattr(store, 'save', save)
    assert service.sync_playlist(playlist.id).created == 1


def test_reconcile_crash_orphan_before_second_creation(setup, monkeypatch):
    db, store, playlist, service = setup
    with monkeypatch.context() as active:
        active.setattr(db, 'link_job', lambda *_: (_ for _ in ()).throw(RuntimeError('crash after save')))
        assert service.sync_playlist(playlist.id).error == 'crash after save'
    orphan, = store.list()
    service.sync_playlist(playlist.id)
    assert db.find_job_id(VIDEO) == orphan['id']
    assert len(store.list()) == len(rows(db, 'queue_items')) == 1


def test_missing_incomplete_job_repaired_with_immutable_id(setup):
    db, store, playlist, service = setup
    service.sync_playlist(playlist.id)
    original = db.find_job_id(VIDEO)
    (store.directory(original) / 'job.json').unlink()
    result = service.sync_playlist(playlist.id)
    assert not result.error
    assert db.find_job_id(VIDEO) == original
    assert store.load(original)['status'] == 'waiting'
    assert len(store.list()) == len(rows(db, 'queue_items')) == 1


def test_absent_video_cleans_waiting_then_reappearance_reuses_job(setup):
    db, store, playlist, service = setup
    service.sync_playlist(playlist.id)
    original = db.find_job_id(VIDEO)
    service.fetcher = lambda _: snapshot()
    service.sync_playlist(playlist.id)
    assert rows(db, 'queue_items') == []
    assert rows(db, 'playlist_videos')[0]['present'] == 0
    service.fetcher = lambda _: snapshot(VIDEO)
    service.sync_playlist(playlist.id)
    assert db.find_job_id(VIDEO) == original
    assert len(rows(db, 'queue_items')) == 1


def test_removed_playlist_not_fetched_and_shared_waiting_survives(setup):
    db, store, playlist, service = setup
    other = db.add_or_reactivate_playlist('PLother', 'other-url', 'Other')
    service.sync_playlist(playlist.id)
    service.sync_playlist(other.id)
    db.soft_remove_playlist(playlist.id)
    assert len(service.sync_enabled()) == 1
    assert len(rows(db, 'queue_items')) == 1
    db.soft_remove_playlist(other.id)
    assert service.sync_enabled() == []
    assert rows(db, 'queue_items') == []
    assert len(store.list()) == 1


def test_sync_enabled_continues_after_failure_preserving_snapshot(setup):
    db, store, playlist, service = setup
    service.sync_playlist(playlist.id)
    previous = db.list_playlists()[0].last_sync_success_at
    other = db.add_or_reactivate_playlist('PLother', 'other-url', 'Other')
    def fetch(url):
        if url == playlist.url:
            raise RuntimeError('network unavailable')
        return snapshot(OTHER)
    service.fetcher = fetch
    results = service.sync_enabled()
    assert [result.playlist_id for result in results] == [playlist.id, other.id]
    assert results[0].error == 'network unavailable'
    assert not results[1].error
    saved = db.list_playlists()[0]
    assert saved.last_error == 'network unavailable'
    assert saved.last_sync_success_at == previous
    assert rows(db, 'playlist_videos')[0]['present'] == 1


def test_snapshot_transaction_failure_preserves_previous_memberships(setup):
    db, store, playlist, service = setup
    service.sync_playlist(playlist.id)
    previous = db.list_playlists()[0].last_sync_success_at
    with sqlite3.connect(db.path) as connection:
        connection.execute("CREATE TRIGGER reject_video BEFORE INSERT ON videos WHEN NEW.video_id='ABCDEFGHIJK' BEGIN SELECT RAISE(ABORT, 'snapshot rejected'); END")
    service.fetcher = lambda _: snapshot(OTHER)
    result = service.sync_playlist(playlist.id)
    assert 'snapshot rejected' in result.error
    assert db.list_playlists()[0].last_sync_success_at == previous
    membership, = rows(db, 'playlist_videos')
    assert membership['video_id'] == VIDEO and membership['present'] == 1
    assert len(store.list()) == 1


def test_missing_completed_job_is_not_recreated(setup):
    db, store, playlist, service = setup
    service.sync_playlist(playlist.id)
    item = db.claim_next()
    db.finish_queue_item(item.job_id, 'completed')
    (store.directory(item.job_id) / 'job.json').unlink()
    service.sync_playlist(playlist.id)
    assert store.list() == []
    assert rows(db, 'queue_items')[0]['state'] == 'completed'


def test_completed_manual_job_survives_missing_metadata_without_transcript_loss(setup):
    db, store, playlist, service = setup
    job = store.create('Manual', 'youtube', 'https://youtu.be/abcdefghijk')
    store.update_fields(job['id'], status='complete', segments=[
        {'start': 0, 'end': 2, 'text': 'Preserve the finished transcript.'},
    ])
    folder = store.directory(job['id'])
    transcript = (folder / 'Manual.txt').read_bytes()
    subtitles = (folder / 'Manual.srt').read_bytes()
    assert not service.sync_playlist(playlist.id).error
    (folder / 'job.json').unlink()
    # Restart both stores: completion must survive outside job.json.
    from playlist_service import PlaylistService
    restarted = PlaylistService(PlaylistDB(db.path), JobStore(store.root), service.fetcher)
    assert not restarted.sync_playlist(playlist.id).error
    assert db.find_job_id(VIDEO) == job['id']
    assert db.claim_next() is None
    assert not (folder / 'job.json').exists()
    assert (folder / 'Manual.txt').read_bytes() == transcript
    assert (folder / 'Manual.srt').read_bytes() == subtitles


def test_invalid_and_duplicate_video_ids_never_create_jobs(setup):
    db, store, playlist, service = setup
    service.fetcher = lambda _: snapshot(VIDEO, VIDEO, VIDEO + 'X')
    service.sync_playlist(playlist.id)
    assert len(store.list()) == len(rows(db, 'playlist_videos')) == 1
