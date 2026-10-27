import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from playlist_db import PlaylistDB, PlaylistRecord


def rows(path, query, parameters=()):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(query, parameters).fetchall()


def test_schema_is_versioned_and_configures_every_connection(tmp_path):
    path = tmp_path / 'nested' / 'playlist.db'
    db = PlaylistDB(path)
    with db._connect() as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    assert PlaylistDB(path).list_playlists() == []


def test_playlist_crud_reactivates_same_record_and_retains_history(tmp_path):
    path = tmp_path / 'playlist.db'
    db = PlaylistDB(path)
    original = db.add_or_reactivate_playlist('PL-one', 'https://example.test/?list=one', "A ' title")
    assert isinstance(original, PlaylistRecord)
    assert original.playlist_id == 'PL-one'
    assert original.enabled and not original.removed
    db.set_playlist_enabled(original.id, False)
    assert not db.list_playlists()[0].enabled
    db.soft_remove_playlist(original.id)
    assert db.list_playlists() == []
    assert len(rows(path, 'SELECT * FROM playlists')) == 1
    restored = PlaylistDB(path).add_or_reactivate_playlist('PL-one', 'new-url', 'New title')
    assert restored.id == original.id
    assert restored.created_at == original.created_at
    assert restored.enabled and not restored.removed
    assert restored.title == 'New title'


def test_paused_setting_survives_restart(tmp_path):
    path = tmp_path / 'playlist.db'
    db = PlaylistDB(path)
    assert not db.auto_sync_paused()
    db.set_auto_sync_paused(True)
    assert PlaylistDB(path).auto_sync_paused()
    db.set_auto_sync_paused(False)
    assert not PlaylistDB(path).auto_sync_paused()


def test_schema_refuses_unknown_future_version(tmp_path):
    path = tmp_path / 'playlist.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version = 99')
    with pytest.raises(ValueError, match='version'):
        PlaylistDB(path)


def video(video_id='aaaaaaaaaaa', title='First'):
    return {'video_id': video_id, 'title': title, 'duration': 12,
            'url': f'https://www.youtube.com/watch?v={video_id}',
            'availability': 'public'}


def populated(tmp_path):
    path = tmp_path / 'playlist.db'
    db = PlaylistDB(path)
    playlist = db.add_or_reactivate_playlist('PL-one', 'url', 'One')
    db.apply_snapshot(playlist.id, [video()], 100)
    db.link_job('aaaaaaaaaaa', 'job-one', 'pending')
    return path, db, playlist


def test_enqueue_completed_job_is_durable_and_never_claimed(tmp_path):
    path, db, playlist = populated(tmp_path)
    item = db.enqueue('job-one', 'aaaaaaaaaaa', completed=True)
    assert item.state == 'completed'
    assert item.finished_at is not None
    restarted = PlaylistDB(path)
    assert restarted.claim_next() is None
    assert restarted.enqueue('job-one', 'aaaaaaaaaaa').state == 'completed'


def test_snapshot_upserts_global_video_and_preserves_membership_history(tmp_path):
    path, db, playlist = populated(tmp_path)
    other = db.add_or_reactivate_playlist('PL-two', 'url-two', 'Two')
    db.apply_snapshot(other.id, [video(title='Updated')], 150)
    assert len(rows(path, 'SELECT * FROM videos')) == 1
    assert len(rows(path, 'SELECT * FROM playlist_videos')) == 2
    db.apply_snapshot(playlist.id, [], 200)
    membership = rows(path, 'SELECT * FROM playlist_videos WHERE playlist_id=?', (playlist.id,))[0]
    assert membership['present'] == 0
    assert membership['first_seen_at'] == 100
    assert membership['last_seen_at'] == 100
    db.apply_snapshot(playlist.id, [video('bbbbbbbbbbb'), video(title='Returned'), video()], 300)
    membership = rows(path, 'SELECT * FROM playlist_videos WHERE playlist_id=? AND video_id=?',
                      (playlist.id, 'aaaaaaaaaaa'))[0]
    assert (membership['present'], membership['position']) == (1, 1)
    assert (membership['first_seen_at'], membership['last_seen_at']) == (100, 300)
    assert db.list_playlists()[0].video_count == 2


def test_snapshot_failure_rolls_back_previous_snapshot_and_records_error_separately(tmp_path):
    path, db, playlist = populated(tmp_path)
    with pytest.raises(KeyError):
        db.apply_snapshot(playlist.id, [video('bbbbbbbbbbb'), {'title': 'invalid'}], 200)
    assert [r['video_id'] for r in rows(path, 'SELECT * FROM videos')] == ['aaaaaaaaaaa']
    db.record_sync_failure(playlist.id, "Network ' error", 250)
    record = PlaylistDB(path).list_playlists()[0]
    assert record.video_count == 1
    assert record.last_sync_success_at == 100
    assert record.last_sync_started_at == 250
    assert record.last_error == "Network ' error"
    db.apply_snapshot(playlist.id, [video()], 300)
    assert db.list_playlists()[0].last_error == ''


def test_queue_link_and_enqueue_are_unique_and_fifo_positions_never_reused(tmp_path):
    path, db, playlist = populated(tmp_path)
    db.link_job('aaaaaaaaaaa', 'job-one', 'creator')
    assert PlaylistDB(path).find_job_id('aaaaaaaaaaa') == 'job-one'
    assert db.find_job_id('unknown') is None
    with pytest.raises(sqlite3.IntegrityError):
        db.link_job('aaaaaaaaaaa', 'different-job', 'pending')
    first = db.enqueue('job-one', 'aaaaaaaaaaa')
    assert db.enqueue('job-one', 'aaaaaaaaaaa').position == first.position
    db.soft_remove_playlist(playlist.id)
    assert db.remove_unreferenced_waiting() == ['job-one']
    second = db.enqueue('job-one', 'aaaaaaaaaaa')
    assert second.position > first.position
    assert len(rows(path, 'SELECT * FROM queue_items')) == 1


def test_membership_removal_keeps_shared_waiting_and_all_nonwaiting_items(tmp_path):
    path, db, playlist = populated(tmp_path)
    other = db.add_or_reactivate_playlist('PL-two', 'url-two', 'Two')
    db.apply_snapshot(other.id, [video()], 150)
    db.enqueue('job-one', 'aaaaaaaaaaa')
    db.soft_remove_playlist(playlist.id)
    assert db.remove_unreferenced_waiting() == []
    db.set_playlist_enabled(other.id, False)
    assert db.remove_unreferenced_waiting() == ['job-one']
    db.enqueue('job-one', 'aaaaaaaaaaa')
    db.claim_next()
    assert db.remove_unreferenced_waiting() == []
    assert len(rows(path, 'SELECT * FROM playlist_videos')) == 2


def test_queue_claim_is_atomic_across_connections_and_obeys_fifo(tmp_path):
    path, db, playlist = populated(tmp_path)
    db.apply_snapshot(playlist.id, [video(), video('bbbbbbbbbbb')], 200)
    db.link_job('bbbbbbbbbbb', 'job-two', 'pending')
    db.enqueue('job-one', 'aaaaaaaaaaa')
    db.enqueue('job-two', 'bbbbbbbbbbb')
    assert db.claim_next().job_id == 'job-one'
    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _: PlaylistDB(path).claim_next(), range(2)))
    assert [item.job_id for item in claimed if item] == ['job-two']
    assert db.claim_next() is None


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'interrupted'])
def test_queue_terminal_state_and_one_retry_persist_across_restart(tmp_path, terminal):
    path, db, _ = populated(tmp_path)
    db.enqueue('job-one', 'aaaaaaaaaaa')
    db.claim_next()
    db.finish_queue_item('job-one', terminal, 'failure')
    restarted = PlaylistDB(path)
    if terminal != 'completed':
        retried = restarted.claim_next()
        assert retried.retry_count == 1
        restarted.finish_queue_item('job-one', terminal, 'last error')
    assert PlaylistDB(path).claim_next() is None
    item = rows(path, 'SELECT * FROM queue_items')[0]
    assert item['state'] == terminal
    assert item['finished_at'] is not None
    db.enqueue('job-one', 'aaaaaaaaaaa')
    assert db.claim_next() is None


def test_stale_processing_recovery_preserves_active_and_bounds_retries(tmp_path):
    path, db, playlist = populated(tmp_path)
    db.apply_snapshot(playlist.id, [video(), video('bbbbbbbbbbb')], 200)
    db.link_job('bbbbbbbbbbb', 'job-two', 'pending')
    db.enqueue('job-one', 'aaaaaaaaaaa')
    db.enqueue('job-two', 'bbbbbbbbbbb')
    db.claim_next()
    db.claim_next()
    restarted = PlaylistDB(path)
    assert restarted.recover_stale_processing({'job-two'}) == ['job-one']
    assert restarted.claim_next().job_id == 'job-one'
    assert restarted.recover_stale_processing({'job-two'}) == ['job-one']
    assert restarted.claim_next() is None
    assert rows(path, "SELECT state FROM queue_items WHERE job_id='job-one'")[0]['state'] == 'interrupted'


def test_queue_rejects_unlinked_video_and_invalid_terminal_state(tmp_path):
    _, db, _ = populated(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        db.enqueue('wrong-job', 'aaaaaaaaaaa')
    db.enqueue('job-one', 'aaaaaaaaaaa')
    db.claim_next()
    with pytest.raises(ValueError):
        db.finish_queue_item('job-one', 'invented')
    with pytest.raises(ValueError):
        db.finish_queue_item('job-one', 'waiting')


@pytest.mark.parametrize('failure', ['failed', 'interrupted'])
def test_retry_budget_survives_cleanup_reappearance_and_restart(tmp_path, failure):
    path, db, playlist = populated(tmp_path)
    first = db.enqueue('job-one', 'aaaaaaaaaaa')
    db.claim_next()
    db.finish_queue_item('job-one', failure, 'first failure')
    db.apply_snapshot(playlist.id, [], 200)
    assert db.remove_unreferenced_waiting() == ['job-one']
    assert rows(path, 'SELECT * FROM queue_items') == []

    restarted = PlaylistDB(path)
    restarted.apply_snapshot(playlist.id, [video()], 300)
    restored = restarted.enqueue('job-one', 'aaaaaaaaaaa')
    assert restored.position > first.position
    assert restored.retry_count == 1
    assert restarted.claim_next().job_id == 'job-one'
    restarted.finish_queue_item('job-one', failure, 'second failure')
    assert PlaylistDB(path).claim_next() is None
    assert rows(path, 'SELECT state FROM queue_items')[0]['state'] == failure


def test_schema_v1_upgrade_preserves_existing_retry_budget(tmp_path):
    path = tmp_path / 'legacy.db'
    # The migration only adds to these existing v1 tables; retain a consumed
    # budget before cleanup can delete its legacy scheduling row.
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE playlist_jobs (job_id TEXT UNIQUE)')
        connection.execute('CREATE TABLE queue_items (job_id TEXT, retry_count INTEGER)')
        connection.execute("INSERT INTO playlist_jobs VALUES ('job-one')")
        connection.execute("INSERT INTO queue_items VALUES ('job-one', 1)")
        connection.execute('PRAGMA user_version=1')
    PlaylistDB(path)
    assert rows(path, 'SELECT retry_count FROM playlist_jobs')[0]['retry_count'] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
        connection.execute('DELETE FROM queue_items')
    PlaylistDB(path)
    assert rows(path, 'SELECT retry_count FROM playlist_jobs')[0]['retry_count'] == 1
