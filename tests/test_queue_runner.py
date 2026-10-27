import threading
import time

import pytest

from pipeline import JobStore
from playlist_db import PlaylistDB
from worker import WorkerLock


def setup_queue(tmp_path, count=2):
    db = PlaylistDB(tmp_path / 'playlist.db')
    store = JobStore(tmp_path / 'jobs')
    playlist = db.add_or_reactivate_playlist('PL-test', 'url', 'Playlist')
    videos = [{'video_id': char * 11} for char in 'abc'[:count]]
    db.apply_snapshot(playlist.id, videos, 100)
    jobs = []
    for video in videos:
        job = store.create(video['video_id'], 'youtube',
                           'https://www.youtube.com/watch?v=' + video['video_id'])
        store.update_fields(job['id'], status='waiting',
                            youtube_video_id=video['video_id'])
        db.link_job(video['video_id'], job['id'], 'pending')
        db.enqueue(job['id'], video['video_id'])
        jobs.append(job['id'])
    return db, store, jobs


def states(db):
    with db._connect() as connection:
        return [(r['state'], r['retry_count']) for r in connection.execute(
            'SELECT * FROM queue_items ORDER BY position')]


def runner(db, store):
    from queue_runner import QueueRunner
    return QueueRunner(db, store, poll_interval=0.01)


def test_run_captions_finish_fifo_across_instances(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    prepared = []

    def prepare(job_id, store, db):
        prepared.append(job_id)
        return store.update_fields(job_id, status='complete')

    monkeypatch.setattr('queue_runner.prepare_playlist_job', prepare)
    assert runner(db, store).run_once().state == 'completed'
    assert states(db) == [('completed', 0), ('waiting', 0)]
    assert runner(PlaylistDB(db.path), JobStore(store.root)).run_once().state == 'completed'
    assert prepared == jobs
    assert runner(db, store).run_once().state == 'idle'


def test_run_whisper_polls_durable_completion_and_process_exit(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    active = runner(db, store)
    monkeypatch.setattr('queue_runner.prepare_playlist_job',
                        lambda job_id, store, db: store.update_fields(job_id, status='waiting'))
    alive = {4321}
    monkeypatch.setattr('queue_runner.process_is_running', lambda pid: pid in alive)
    launches = []

    def launch(job_id, store):
        launches.append(job_id)
        store.update_fields(job_id, status='running', worker_pid=4321)

    monkeypatch.setattr('queue_runner.launch_worker', launch)
    assert active.run_once().state == 'processing'
    assert runner(db, store).run_once().state == 'processing'
    store.update_fields(jobs[0], status='complete')
    assert active.run_once().state == 'processing'
    assert launches == [jobs[0]]
    alive.clear()
    assert active.run_once().state == 'completed'
    assert states(db) == [('completed', 0), ('waiting', 0)]
    assert active.run_once().state == 'processing'
    assert launches == jobs


def test_run_does_not_prepare_while_manual_worker_or_cpu_lock_active(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    active = runner(db, store)
    manual = store.create('Manual', 'local', '')
    store.update_fields(manual['id'], worker_pid=4321)
    monkeypatch.setattr('queue_runner.process_is_running', lambda pid: pid == 4321)
    assert active.run_once().state == 'busy'
    store.update_fields(manual['id'], worker_pid=None)
    with WorkerLock(store.root.parent / 'transcription.lock'):
        assert active.run_once().state == 'busy'
    assert states(db) == [('waiting', 0), ('waiting', 0)]


def test_run_serializes_runner_instances(tmp_path):
    db, store, _ = setup_queue(tmp_path)
    with WorkerLock(db.path.with_suffix('.runner.lock')):
        assert runner(db, store).run_once().state == 'busy'
    assert states(db) == [('waiting', 0), ('waiting', 0)]


def test_run_empty_queue_waits_and_stop_event_exits_promptly(tmp_path):
    db, store, _ = setup_queue(tmp_path, 0)
    active = runner(db, store)
    event = threading.Event()
    thread = threading.Thread(target=active.run_until_idle, args=(event,))
    thread.start()
    time.sleep(0.04)
    assert thread.is_alive()
    event.set()
    thread.join(0.5)
    assert not thread.is_alive()


@pytest.mark.parametrize('status', ['running', 'paused', 'failed', 'complete'])
def test_recover_stale_processing_obeys_terminal_state(tmp_path, monkeypatch, status):
    db, store, jobs = setup_queue(tmp_path)
    db.claim_next()
    store.update_fields(jobs[0], status=status, worker_pid=4321)
    monkeypatch.setattr('queue_runner.process_is_running', lambda pid: False)
    runner(PlaylistDB(db.path), JobStore(store.root)).recover()
    assert states(db)[0] == (('completed', 0) if status == 'complete' else ('waiting', 1))
    runner(db, store).recover()
    assert states(db)[0][1] == (0 if status == 'complete' else 1)


def test_recover_preserves_live_worker(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    db.claim_next()
    store.update_fields(jobs[0], worker_pid=4321)
    monkeypatch.setattr('queue_runner.process_is_running', lambda pid: pid == 4321)
    runner(db, store).recover()
    assert states(db) == [('processing', 0), ('waiting', 0)]


def test_retry_failed_worker_once_at_original_position_then_manual_state(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    db.claim_next()
    store.update_fields(jobs[0], status='failed', error='Recognition failed')
    active = runner(db, store)
    assert active.run_once().state == 'waiting'
    assert states(db) == [('waiting', 1), ('waiting', 0)]
    db.claim_next()
    assert runner(db, store).run_once().state == 'failed'
    assert states(db) == [('failed', 1), ('waiting', 0)]


def test_retry_preparation_exception_is_durable_and_bounded(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)

    def fail(*args):
        raise OSError('YouTube unavailable')

    monkeypatch.setattr('queue_runner.prepare_playlist_job', fail)
    active = runner(db, store)
    assert active.run_once().state == 'waiting'
    assert store.load(jobs[0])['status'] == 'failed'
    assert active.run_once().state == 'failed'
    assert states(db) == [('failed', 1), ('waiting', 0)]


def test_recover_missing_job_requests_repair_then_resumes(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    saved = store.load(jobs[0])
    (store.directory(jobs[0]) / 'job.json').unlink()
    active = runner(db, store)
    result = active.run_once()
    assert (result.state, result.repair_video_id) == ('repair', 'aaaaaaaaaaa')
    active.recover()
    assert states(db)[0] == ('processing', 0)
    store.save(saved)  # PlaylistService repairs the immutable job ID on sync.
    assert active.run_once().state == 'waiting'
    monkeypatch.setattr('queue_runner.prepare_playlist_job',
                        lambda job_id, store, db: store.update_fields(job_id, status='complete'))
    assert active.run_once().state == 'completed'
    assert states(db) == [('completed', 1), ('waiting', 0)]


def test_exit_stops_active_worker_using_existing_stop_semantics(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    db.claim_next()
    store.update_fields(jobs[0], worker_pid=4321)
    monkeypatch.setattr('queue_runner.process_is_running', lambda pid: pid == 4321)
    stopped = []

    def stop(job_id, store):
        stopped.append(job_id)
        return store.update_fields(job_id, status='paused', worker_pid=None)

    monkeypatch.setattr('queue_runner.stop_worker', stop)
    event = threading.Event()
    event.set()
    runner(db, store).run_until_idle(event)
    assert stopped == [jobs[0]]
    assert store.load(jobs[0])['status'] == 'paused'
    assert states(db) == [('waiting', 1), ('waiting', 0)]


def test_recover_second_interruption_is_terminal(tmp_path):
    db, store, jobs = setup_queue(tmp_path)
    db.claim_next()
    runner(db, store).recover()
    assert db.claim_next().job_id == jobs[0]
    runner(PlaylistDB(db.path), JobStore(store.root)).recover()
    assert states(db) == [('interrupted', 1), ('waiting', 0)]


def test_retry_worker_launch_failure_is_durable(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)
    monkeypatch.setattr('queue_runner.prepare_playlist_job',
                        lambda job_id, store, db: store.load(job_id))

    def fail(*args, **kwargs):
        raise OSError('Cannot start worker')

    monkeypatch.setattr('queue_runner.launch_worker', fail)
    active = runner(db, store)
    assert active.run_once().state == 'waiting'
    assert active.run_once().state == 'failed'
    assert states(db) == [('failed', 1), ('waiting', 0)]


def test_run_real_worker_failure_is_polled_before_next_fifo(tmp_path, monkeypatch):
    db, store, jobs = setup_queue(tmp_path)

    def prepare(job_id, store, db):
        # Local missing media deterministically exercises worker failure without
        # invoking external YouTube or Whisper dependencies.
        return store.update_fields(job_id, source='local', status='waiting')

    monkeypatch.setattr('queue_runner.prepare_playlist_job', prepare)
    active = runner(db, store)
    assert active.run_once().state == 'processing'
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = active.run_once()
        if result.state != 'processing':
            break
        time.sleep(0.01)
    assert result.state == 'waiting'
    assert store.load(jobs[0])['status'] == 'failed'
    assert states(db) == [('waiting', 1), ('waiting', 0)]
