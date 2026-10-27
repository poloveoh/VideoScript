from pathlib import Path
import os
import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / 'app.py'


@pytest.fixture
def playlist_ui(tmp_path, monkeypatch):
    import pipeline
    from playlist_db import PlaylistDB
    monkeypatch.setattr(pipeline, 'DATA', tmp_path)
    store = pipeline.JobStore(tmp_path / 'jobs')
    monkeypatch.setattr(pipeline, 'JobStore', lambda: store)
    return AppTest.from_file(str(APP)), PlaylistDB(tmp_path / 'videoscript.db'), store


def test_playlist_add_validate_toggle_remove_and_reactivate(playlist_ui):
    app, db, store = playlist_ui
    app.run()
    app.text_input(key='playlist_url').set_value('https://example.com/playlist?list=PLtest')
    app.button(key='add_playlist').click().run()
    assert not db.list_playlists()
    assert app.error
    app.text_input(key='playlist_url').set_value('https://youtube.com/playlist?list=PLtest')
    app.button(key='add_playlist').click().run()
    playlist, = db.list_playlists()
    app.button(key=f'disable_playlist_{playlist.id}').click().run()
    assert not db.list_playlists()[0].enabled
    app.button(key=f'enable_playlist_{playlist.id}').click().run()
    assert db.list_playlists()[0].enabled
    assert app.button(key=f'remove_playlist_{playlist.id}').disabled
    app.checkbox(key=f'confirm_remove_{playlist.id}').check().run()
    app.button(key=f'remove_playlist_{playlist.id}').click().run()
    assert not db.list_playlists()
    app.button(key='add_playlist').click().run()
    assert db.list_playlists()[0].id == playlist.id
    assert not store.list()
    assert not app.exception


def test_playlist_sync_request_and_readonly_status_without_rerun_fetch(playlist_ui):
    from playlist_service import PlaylistService
    from playlist_sync import PlaylistSnapshot, PlaylistVideo
    app, db, store = playlist_ui
    playlist = db.add_or_reactivate_playlist('PLtest', 'https://youtube.com/playlist?list=PLtest', 'Lessons')
    service = PlaylistService(db, store, fetcher=lambda url: PlaylistSnapshot(
        'PLtest', 'Lessons', url, [PlaylistVideo('abcdefghijk', 'New lesson',
        'https://youtube.com/watch?v=abcdefghijk', 1, 10, 'en')]))
    assert not service.sync_playlist(playlist.id).error
    db.record_sync_failure(playlist.id, 'Network unavailable', 123)
    app.run()
    assert not app.exception
    assert any('Network unavailable' in item.value for item in app.warning)
    assert any('1' in item.value and '视频' in item.value for item in app.caption)
    assert any('New lesson' in frame.value.to_string() for frame in app.dataframe)
    assert any('waiting' in frame.value.to_string() for frame in app.dataframe)
    app.button(key='sync_playlists').click().run()
    assert (db.path.parent / 'sync.requested').exists()
    app.run()
    assert len(store.list()) == 1
    assert db.list_playlists()[0].last_error == 'Network unavailable'


def test_interface_opens_and_rejects_invalid_link_without_network():
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert any('VideoScript' in title.value for title in app.title)
    app.text_input(key='youtube_url').set_value('https://example.com/video')
    app.button(key='inspect').click().run()
    assert not app.exception
    assert any('YouTube' in error.value for error in app.error)


def test_local_mode_explains_first_model_download():
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.radio(key='source').set_value('本地视频 / 音频').run()
    assert not app.exception
    assert any('首次' in item.value for item in app.info)
    assert app.checkbox(key='auto_cleanup_media').value is True
    assert any('成功后可自动清理' in item.value for item in app.caption)


def test_running_job_can_be_stopped_and_offers_resume_without_calling_whisper(
        monkeypatch):
    import pipeline

    job = {
        'id': 'a' * 32, 'title': 'Lecture', 'source': 'local', 'url': '',
        'created': 1, 'status': 'running', 'segments': [
            {'start': 0, 'end': 2, 'text': 'Saved words.'},
        ], 'error': '', 'edited_text': None,
        'media_path': str(Path(__file__).resolve()),
        'progress': 0.4, 'message': '正在转录…', 'worker_pid': os.getpid(),
    }

    class FakeStore:
        def list(self):
            return [job]

        def load(self, _job_id):
            return dict(job)

        def request_stop(self, _job_id):
            job['status'] = 'paused'

        def update_fields(self, _job_id, **fields):
            job.update(fields)
            return dict(job)

        def save_edits(self, _job_id, text):
            job['edited_text'] = text

    monkeypatch.setattr(pipeline, 'JobStore', FakeStore)
    app = AppTest.from_file(str(APP))
    app.session_state['active_job'] = job['id']
    app.run(timeout=20)

    assert not app.exception
    app.button(key='stop_job').click().run()
    assert job['status'] == 'paused'
    app.run()
    assert not app.button(key='resume_job').disabled


def test_stale_running_job_offers_resume(monkeypatch):
    import pipeline
    import worker

    job = {
        'id': 'b' * 32, 'title': 'Interrupted', 'source': 'local', 'url': '',
        'created': 1, 'status': 'running', 'segments': [], 'error': '',
        'edited_text': None, 'media_path': __file__, 'worker_pid': 999999,
        'progress': 0.2, 'message': '旧进程',
    }

    class FakeStore:
        def list(self):
            return [job]

        def load(self, _job_id):
            return dict(job)

        def update_fields(self, _job_id, **fields):
            job.update(fields)
            return dict(job)

    monkeypatch.setattr(pipeline, 'JobStore', FakeStore)
    monkeypatch.setattr(worker, 'process_is_running', lambda _pid: False)
    app = AppTest.from_file(str(APP))
    app.session_state['active_job'] = job['id']
    app.run(timeout=20)

    assert not app.exception
    assert app.button(key='resume_job')
    assert job['status'] == 'paused'


def test_stopping_job_does_not_offer_another_stop(monkeypatch):
    import pipeline

    job = {
        'id': 'c' * 32, 'title': 'Stopping', 'source': 'local', 'url': '',
        'created': 1, 'status': 'stopping', 'segments': [], 'error': '',
        'edited_text': None, 'media_path': __file__, 'worker_pid': os.getpid(),
        'progress': 0.2, 'message': '正在停止',
    }

    class FakeStore:
        def list(self):
            return [job]

        def load(self, _job_id):
            return dict(job)

    monkeypatch.setattr(pipeline, 'JobStore', FakeStore)
    app = AppTest.from_file(str(APP))
    app.session_state['active_job'] = job['id']
    app.run(timeout=20)

    assert not app.exception
    assert not any(button.key == 'stop_job' for button in app.button)
    assert not any(button.key == 'resume_job' for button in app.button)


def test_completed_job_can_retry_cleanup_without_transcription(monkeypatch):
    import pipeline
    import streamlit as st

    download_names = []
    original_download_button = st.download_button

    def capture_download(*args, **kwargs):
        download_names.append(kwargs['file_name'])
        return original_download_button(*args, **kwargs)

    monkeypatch.setattr(st, 'download_button', capture_download)

    job = {
        'id': 'd' * 32, 'title': 'Complete', 'source': 'local', 'url': '',
        'created': 1, 'status': 'complete', 'segments': [
            {'start': 0, 'end': 2, 'text': 'Saved transcript.'},
        ], 'error': '', 'edited_text': None, 'media_path': __file__,
        'progress': 1.0, 'message': '完成', 'worker_pid': None,
        'cleanup_status': 'failed', 'cleanup_error': 'file is busy',
    }

    class FakeStore:
        def list(self):
            return [job]

        def load(self, _job_id):
            return dict(job)

        def cleanup_media(self, _job_id):
            job.update(cleanup_status='complete', cleanup_error='')
            return dict(job)

        def save_edits(self, _job_id, text):
            job['edited_text'] = text

    monkeypatch.setattr(pipeline, 'JobStore', FakeStore)
    app = AppTest.from_file(str(APP))
    app.session_state['active_job'] = job['id']
    app.run(timeout=20)

    assert not app.exception
    assert download_names == ['Complete.txt', 'Complete.srt']
    app.button(key='retry_cleanup').click().run()
    assert not app.exception
    assert job['cleanup_status'] == 'complete'
