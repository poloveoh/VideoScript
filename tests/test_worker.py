import os
import subprocess

import pytest

from pipeline import JobStore, process_transcription
from worker import (WorkerBusy, WorkerLock, launch_worker, process_is_running,
                    run_job, stop_worker)


def test_second_worker_cannot_acquire_cpu_lock(tmp_path):
    lock_path = tmp_path / 'worker.lock'
    with WorkerLock(lock_path):
        with pytest.raises(WorkerBusy):
            with WorkerLock(lock_path):
                pass


def test_process_liveness_recognizes_current_process():
    assert process_is_running(os.getpid())
    assert not process_is_running(None)


def test_worker_uses_saved_options_and_persists_completion(tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'jobs')
    media = tmp_path / 'lecture.wav'
    media.write_bytes(b'audio')
    job = store.create('Lecture', 'local', '')
    assert store.directory(job['id']).name == f"Lecture_{job['id'][:8]}"
    job.update(media_path=str(media), model='small', language='ms',
               glossary='photosynthesis')
    store.save(job)

    def fake_transcribe(path, size, language, glossary, used_store, active, report):
        assert (path, size, language, glossary) == (
            media, 'small', 'ms', 'photosynthesis',
        )
        return process_transcription(used_store, active, iter([
            {'start': 0, 'end': 2, 'text': 'Saved words.'},
        ]), 2, report)

    monkeypatch.setattr('worker.transcribe', fake_transcribe)
    run_job(job['id'], store=store, lock_path=tmp_path / 'worker.lock')

    saved = store.load(job['id'])
    assert saved['status'] == 'complete'
    assert saved['progress'] == 1.0
    assert saved['segments'][0]['text'] == 'Saved words.'


def test_worker_honors_existing_stop_without_starting_transcription(
        tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'jobs')
    media = tmp_path / 'lecture.wav'
    media.write_bytes(b'audio')
    job = store.create('Lecture', 'local', '')
    job.update(media_path=str(media), model='small', language=None, glossary='')
    store.save(job)
    store.request_stop(job['id'])
    assert (store.directory(job['id']) / 'stop.requested').is_file()

    def must_not_run(*_args, **_kwargs):
        raise AssertionError('transcription started after Stop')

    monkeypatch.setattr('worker.transcribe', must_not_run)
    run_job(job['id'], store=store, lock_path=tmp_path / 'worker.lock')

    assert store.load(job['id'])['status'] == 'paused'
    assert media.exists()


def test_rejected_duplicate_worker_does_not_pause_active_job(tmp_path):
    store = JobStore(tmp_path / 'jobs')
    media = tmp_path / 'lecture.wav'
    media.write_bytes(b'audio')
    job = store.create('Lecture', 'local', '')
    job.update(status='running', worker_pid=123, media_path=str(media),
               model='small', language=None, glossary='')
    store.save(job)

    lock_path = tmp_path / 'worker.lock'
    with WorkerLock(lock_path):
        result = run_job(job['id'], store=store, lock_path=lock_path)

    assert result['status'] == 'running'
    assert store.load(job['id'])['status'] == 'running'


def test_stop_worker_terminates_after_timeout_and_preserves_job(tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'jobs')
    job = store.create('Lecture', 'local', '')
    job.update(status='running', worker_pid=4321, segments=[
        {'start': 0, 'end': 2, 'text': 'Already committed.'},
    ])
    store.save(job)
    terminated = []
    monkeypatch.setattr('worker.process_is_running', lambda _pid: True)
    monkeypatch.setattr('worker.terminate_process',
                        lambda pid: terminated.append(pid) or True)

    result = stop_worker(job['id'], store=store, timeout=0)

    assert terminated == [4321]
    assert result['status'] == 'paused'
    assert result['worker_pid'] is None
    assert result['segments'][0]['text'] == 'Already committed.'


@pytest.mark.skipif(os.name != 'nt', reason='Windows-only launch behavior')
def test_launch_worker_does_not_open_a_console_window(tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'jobs')
    job = store.create('Lecture', 'local', '')
    captured = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr('worker.subprocess.Popen', fake_popen)

    launch_worker(job['id'], store=store)

    assert captured['creationflags'] & subprocess.CREATE_NO_WINDOW
    assert not captured['creationflags'] & subprocess.DETACHED_PROCESS


def test_worker_cleans_managed_media_after_success(tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'jobs')
    job = store.create('Lecture', 'local', '')
    media = store.directory(job['id']) / 'input.wav'
    media.write_bytes(b'audio')
    job.update(
        media_path=str(media), media_managed=True, auto_cleanup_media=True,
        model='small', language='ms', glossary='',
    )
    store.save(job)

    def fake_transcribe(path, size, language, glossary, used_store, active, report):
        return process_transcription(used_store, active, iter([
            {'start': 0, 'end': 2, 'text': 'Complete.'},
        ]), 2, report)

    monkeypatch.setattr('worker.transcribe', fake_transcribe)

    result = run_job(
        job['id'], store=store, lock_path=tmp_path / 'worker.lock',
    )

    assert result['status'] == 'complete'
    assert result['cleanup_status'] == 'complete'
    assert not media.exists()


def test_worker_failure_keeps_managed_media(tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'jobs')
    job = store.create('Lecture', 'local', '')
    media = store.directory(job['id']) / 'input.wav'
    media.write_bytes(b'audio')
    job.update(
        media_path=str(media), media_managed=True, auto_cleanup_media=True,
        model='small', language='ms', glossary='',
    )
    store.save(job)

    def fail_transcription(*_args, **_kwargs):
        raise RuntimeError('recognition failed')

    monkeypatch.setattr('worker.transcribe', fail_transcription)

    result = run_job(
        job['id'], store=store, lock_path=tmp_path / 'worker.lock',
    )

    assert result['status'] == 'failed'
    assert media.exists()
    assert 'cleanup_status' not in result


def test_unexpected_cleanup_error_does_not_fail_completed_transcription(
        tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'jobs')
    job = store.create('Lecture', 'local', '')
    media = store.directory(job['id']) / 'input.wav'
    media.write_bytes(b'audio')
    job.update(
        media_path=str(media), media_managed=True, auto_cleanup_media=True,
        model='small', language='ms', glossary='',
    )
    store.save(job)

    def fake_transcribe(path, size, language, glossary, used_store, active, report):
        return process_transcription(used_store, active, iter([
            {'start': 0, 'end': 2, 'text': 'Complete.'},
        ]), 2, report)

    monkeypatch.setattr('worker.transcribe', fake_transcribe)
    monkeypatch.setattr(
        store, 'cleanup_media',
        lambda _job_id: (_ for _ in ()).throw(OSError('cleanup unavailable')),
    )

    result = run_job(
        job['id'], store=store, lock_path=tmp_path / 'worker.lock',
    )

    assert result['status'] == 'complete'
    assert result['cleanup_status'] == 'failed'
    assert 'cleanup unavailable' in result['cleanup_error']
    assert media.exists()
