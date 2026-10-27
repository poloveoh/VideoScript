"""Tray scheduling tests use real marker files/locks and no notification area."""
import subprocess
import os
import sys
import threading
import time

import pytest

from playlist_db import PlaylistDB


def coordinator_type():
    import tray_app
    return tray_app.SyncCoordinator


class Service:
    def __init__(self, db, callback=None):
        self.db = db
        self.calls = []
        self.callback = callback

    def sync_enabled(self):
        self.calls.append(time.monotonic())
        if self.callback:
            self.callback()
        return []


def test_coordinator_startup_and_600_second_timer(tmp_path):
    db = PlaylistDB(tmp_path / 'playlists.db')
    stop = threading.Event()
    now = [0]
    service = Service(db)
    observed = []

    def wait(seconds):
        time.sleep(.002)
        if len(service.calls) > len(observed):
            observed.append(now[0])
            if len(observed) == 2:
                stop.set()
                return
        now[0] += 1
        assert now[0] < 1000

    coordinator_type()(service, clock=lambda: now[0], wait=wait).run(stop)
    assert observed[0] < 3
    assert 600 <= observed[1] <= 603


def test_coordinator_paused_manual_marker_and_immediate_resume(tmp_path):
    from tray_app import request_sync
    db = PlaylistDB(tmp_path / 'playlists.db')
    db.set_auto_sync_paused(True)
    stop = threading.Event()
    service = Service(db)
    marker = tmp_path / 'sync.requested'
    ticks = [0]

    def wait(seconds):
        time.sleep(.002)
        ticks[0] += 1
        if ticks[0] == 3:
            assert service.calls == []
            request_sync(marker)
        if len(service.calls) == 1:
            db.set_auto_sync_paused(False)
        if len(service.calls) == 2:
            stop.set()
        assert ticks[0] < 100

    coordinator_type()(service, wait=wait).run(stop)
    assert len(service.calls) == 2
    assert not marker.exists()


def test_coordinator_coalesces_requests_during_run(tmp_path):
    from tray_app import request_sync
    db = PlaylistDB(tmp_path / 'playlists.db')
    marker = tmp_path / 'sync.requested'
    stop = threading.Event()

    def sync():
        assert not marker.exists()
        if len(service.calls) == 1:
            for _ in range(20):
                request_sync(marker)
        else:
            stop.set()

    service = Service(db, sync)
    coordinator_type()(service).run(stop)
    assert len(service.calls) == 2


def test_coordinator_respects_cross_process_lock_and_preserves_request(tmp_path):
    from tray_app import request_sync
    db = PlaylistDB(tmp_path / 'playlists.db')
    lock = tmp_path / 'sync.lock'
    code = ('from worker import WorkerLock; import sys; '
            'lock=WorkerLock(sys.argv[1]); lock.__enter__(); '
            'print("locked", flush=True); input(); lock.__exit__(None,None,None)')
    process = subprocess.Popen([sys.executable, '-c', code, str(lock)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    stop = threading.Event()
    service = Service(db, stop.set)
    ticks = [0]

    def wait(seconds):
        ticks[0] += 1
        if ticks[0] == 3:
            assert service.calls == []
            assert (tmp_path / 'sync.requested').exists()
            process.communicate('\n', timeout=5)
        time.sleep(.002)
        assert ticks[0] < 100

    try:
        assert process.stdout.readline().strip() == 'locked'
        request_sync(tmp_path / 'sync.requested')
        coordinator_type()(service, wait=wait).run(stop)
    finally:
        if process.poll() is None:
            process.communicate('\n', timeout=5)
    assert len(service.calls) == 1


def test_coordinator_stop_is_responsive_during_blocking_sync(tmp_path):
    db = PlaylistDB(tmp_path / 'playlists.db')
    entered = threading.Event()
    release = threading.Event()
    stop = threading.Event()
    service = Service(db, lambda: (entered.set(), release.wait(5)))
    coordinator = coordinator_type()(service, poll_interval=.01)
    thread = threading.Thread(target=coordinator.run, args=(stop,))
    thread.start()
    try:
        assert entered.wait(2)
        stop.set()
        thread.join(1)
        assert not thread.is_alive()
    finally:
        release.set()
        thread.join(2)
        coordinator._worker.join(2)


def test_coordinator_playlist_failure_isolated_and_lock_released(tmp_path):
    from pipeline import JobStore
    from playlist_service import PlaylistService
    from playlist_sync import PlaylistSnapshot
    from worker import WorkerLock
    db = PlaylistDB(tmp_path / 'playlists.db')
    first = db.add_or_reactivate_playlist('PLbad', 'bad', 'Bad')
    second = db.add_or_reactivate_playlist('PLgood', 'good', 'Good')
    stop = threading.Event()

    def fetch(url):
        if url == 'bad':
            raise ValueError('private Playlist')
        return PlaylistSnapshot('PLgood', 'Good', 'good', [])

    service = PlaylistService(db, JobStore(tmp_path / 'jobs'), fetcher=fetch)

    def wait(seconds):
        if db.list_playlists()[1].last_sync_success_at:
            stop.set()
        time.sleep(.002)

    coordinator = coordinator_type()(service, wait=wait)
    coordinator.run(stop)
    coordinator._worker.join(2)
    records = db.list_playlists()
    assert records[0].id == first.id and records[0].last_error == 'private Playlist'
    assert records[1].id == second.id and records[1].last_sync_success_at
    # Success is persisted before service cleanup; await the worker's finally.
    with WorkerLock(tmp_path / 'sync.lock'):
        pass


class Icon:
    def __init__(self, controller, callback=None):
        self.controller = controller
        self.callback = callback
        self.stopped = threading.Event()
        self.title = ''
        self.icon = None
        self.menu_updates = 0

    def run(self, setup=None):
        if setup:
            setup(self)
        if self.callback:
            self.callback(self.controller)
        assert self.stopped.wait(3)

    def stop(self):
        self.stopped.set()

    def update_menu(self):
        self.menu_updates += 1


class Loop:
    def __init__(self):
        self.thread_ids = []
        self.finished = threading.Event()
        self.syncing = False
        self.error = ''

    def run(self, stop):
        self.thread_ids.append(threading.get_ident())
        stop.wait(3)
        self.finished.set()

    run_until_idle = run


def test_tray_menu_actions_persist_pause_publish_request_and_exit(tmp_path):
    from tray_app import TrayController
    db = PlaylistDB(tmp_path / 'videoscript.db')
    opened = []
    controller = TrayController(db, Loop(), Loop(), browser_open=opened.append)
    actions = dict(controller.menu())
    assert list(actions) == ['Open VideoScript', 'Sync Now', 'Pause Auto Sync', 'Exit']
    actions['Open VideoScript']()
    assert opened == ['http://127.0.0.1:8501']
    actions['Pause Auto Sync']()
    assert PlaylistDB(db.path).auto_sync_paused()
    assert 'Resume Auto Sync' in dict(controller.menu())
    actions['Sync Now']()
    assert (tmp_path / 'sync.requested').exists()
    dict(controller.menu())['Resume Auto Sync']()
    assert not db.auto_sync_paused()
    actions['Exit']()
    assert controller.stop_event.is_set()


def test_tray_four_icon_states_and_tooltips(tmp_path):
    from tray_app import TrayController, make_icon
    db = PlaylistDB(tmp_path / 'videoscript.db')
    coordinator = Loop()
    controller = TrayController(db, coordinator, Loop())
    assert (controller.state, controller.tooltip) == ('normal', 'VideoScript — Auto Sync on')
    db.set_auto_sync_paused(True)
    assert (controller.state, controller.tooltip) == ('paused', 'VideoScript — Auto Sync paused')
    coordinator.syncing = True
    assert (controller.state, controller.tooltip) == ('syncing', 'VideoScript — Syncing Playlists')
    coordinator.error = 'private Playlist'
    assert (controller.state, controller.tooltip) == ('error', 'VideoScript — private Playlist')
    colors = [make_icon(state).getpixel((16, 16)) for state in
              ('normal', 'paused', 'syncing', 'error')]
    assert len(set(colors)) == 4


def test_tray_runs_both_background_loops_off_ui_and_cleans_exit(tmp_path):
    from tray_app import TrayController
    db = PlaylistDB(tmp_path / 'videoscript.db')
    coordinator, queue = Loop(), Loop()
    icons = []

    def factory(controller):
        icon = Icon(controller, lambda c: c.exit())
        icons.append(icon)
        return icon

    controller = TrayController(db, coordinator, queue, icon_factory=factory)
    controller.run()
    assert icons[0].stopped.is_set()
    assert coordinator.finished.is_set() and queue.finished.is_set()
    assert coordinator.thread_ids[0] != threading.get_ident()
    assert queue.thread_ids[0] != threading.get_ident()
    assert icons[0].title == 'VideoScript — Auto Sync on'
    assert icons[0].icon is not None


def test_tray_singleton_rejects_second_instance(tmp_path):
    from tray_app import TrayController
    from worker import WorkerBusy, WorkerLock
    db = PlaylistDB(tmp_path / 'videoscript.db')
    initialized = []
    with WorkerLock(tmp_path / 'tray.lock'):
        controller = TrayController(db, Loop(), Loop(),
                                    icon_factory=lambda c: initialized.append(c))
        with pytest.raises(WorkerBusy):
            controller.run()
    assert initialized == []


@pytest.mark.parametrize('stage', ['initialize', 'run'])
def test_tray_failure_is_visible_and_releases_singleton(tmp_path, stage):
    from tray_app import TrayController
    from worker import WorkerLock
    db = PlaylistDB(tmp_path / 'videoscript.db')
    messages = []

    def fail(**kwargs):
        raise RuntimeError('notification area unavailable')

    def factory(controller):
        if stage == 'initialize':
            fail()
        icon = Icon(controller)
        icon.run = fail
        return icon

    coordinator, queue = Loop(), Loop()
    controller = TrayController(db, coordinator, queue, icon_factory=factory,
                                notify=messages.append)
    with pytest.raises(RuntimeError, match='notification area unavailable'):
        controller.run()
    assert messages and 'notification area unavailable' in messages[0]
    assert controller.stop_event.is_set()
    with WorkerLock(tmp_path / 'tray.lock'):
        pass


def test_tray_main_wires_shared_database_and_queue(tmp_path, monkeypatch):
    import tray_app
    from queue_runner import QueueRunner
    instances = []

    class Controller:
        def __init__(self, db, coordinator, queue):
            assert db.path == tmp_path / 'videoscript.db'
            assert coordinator.db is db
            assert coordinator.marker_path == tmp_path / 'sync.requested'
            assert isinstance(queue, QueueRunner)
            assert queue.db is db
            assert queue.store is coordinator.service.store
            assert queue.store.root == tmp_path / 'jobs'

        def run(self):
            instances.append(self)

    monkeypatch.setattr(tray_app, 'TrayController', Controller)
    tray_app.main(tmp_path)
    assert len(instances) == 1


def test_tray_background_starts_after_icon_ready_and_failure_stops_ui(tmp_path):
    from tray_app import TrayController
    db = PlaylistDB(tmp_path / 'videoscript.db')
    coordinator, queue = Loop(), Loop()
    messages = []

    def broken(stop):
        raise RuntimeError('queue failed')

    queue.run_until_idle = broken

    class ReadyIcon(Icon):
        def run(self, setup=None):
            assert coordinator.thread_ids == []
            assert setup is not None
            setup(self)
            assert self.visible
            assert self.stopped.wait(2)

    controller = TrayController(db, coordinator, queue, icon_factory=ReadyIcon,
                                notify=messages.append)
    controller.run()
    assert messages == ['VideoScript background loop failed: queue failed']
    assert controller.state == 'error'
    assert coordinator.finished.is_set()


def test_tray_setup_failure_reports_error_and_stops_native_loop(tmp_path):
    from tray_app import TrayController
    db = PlaylistDB(tmp_path / 'videoscript.db')
    messages = []

    class BrokenIcon(Icon):
        @property
        def visible(self):
            return False

        @visible.setter
        def visible(self, value):
            raise RuntimeError('cannot show notification icon')

    controller = TrayController(db, Loop(), Loop(), icon_factory=BrokenIcon,
                                notify=messages.append)
    # pystray calls setup on a separate thread: exceptions must be caught there.
    controller.icon = BrokenIcon(controller)
    controller._setup(controller.icon)
    assert controller.stop_event.is_set()
    assert controller.icon.stopped.is_set()
    assert 'cannot show notification icon' in messages[0]
    assert controller.state == 'error'


def test_coordinator_two_instances_serialize_and_share_one_followup(tmp_path):
    from tray_app import request_sync
    db = PlaylistDB(tmp_path / 'videoscript.db')
    db.set_auto_sync_paused(True)
    marker = tmp_path / 'sync.requested'
    stop = threading.Event()
    entered, release = threading.Event(), threading.Event()
    gate = threading.Lock()
    active, runs = [], []

    def sync():
        with gate:
            active.append(1)
            assert len(active) == 1
            runs.append(1)
        if len(runs) == 1:
            entered.set()
            assert release.wait(3)
        with gate:
            active.pop()
            if len(runs) == 2:
                stop.set()

    services = [Service(db, sync), Service(db, sync)]
    coordinators = [coordinator_type()(s, poll_interval=.005) for s in services]
    request_sync(marker)
    threads = [threading.Thread(target=c.run, args=(stop,)) for c in coordinators]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(2)
        for _ in range(20):
            request_sync(marker)
        release.set()
        assert stop.wait(3)
    finally:
        release.set()
        stop.set()
        for thread in threads:
            thread.join(2)
    assert len(runs) == 2
    assert not marker.exists()


def test_coordinator_unexpected_service_failure_allows_manual_retry(tmp_path):
    from tray_app import request_sync
    from worker import WorkerLock
    db = PlaylistDB(tmp_path / 'playlists.db')
    stop = threading.Event()

    def sync():
        if len(service.calls) == 1:
            raise RuntimeError('database temporarily unavailable')
        stop.set()

    service = Service(db, sync)
    coordinator = coordinator_type()(service)

    def wait(seconds):
        if coordinator.error:
            request_sync(tmp_path / 'sync.requested')
        time.sleep(.002)

    coordinator.wait = wait
    coordinator.run(stop)
    coordinator._worker.join(2)
    assert len(service.calls) == 2
    assert coordinator.error == ''
    with WorkerLock(tmp_path / 'sync.lock'):
        pass


@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
def test_coordinator_retries_marker_held_open_by_request_writer(tmp_path):
    from worker import WorkerLock
    db = PlaylistDB(tmp_path / 'videoscript.db')
    db.set_auto_sync_paused(True)
    marker = tmp_path / 'sync.requested'
    stop = threading.Event()
    service = Service(db, stop.set)
    # Reproduce request_sync's exclusive creation handle before its close.
    writer = marker.open('xb')
    polls = []

    def wait(seconds):
        polls.append(1)
        if not writer.closed:
            assert service.calls == []
            assert marker.exists()
            # A failed consume attempt must release the sync critical section.
            with WorkerLock(tmp_path / 'sync.lock'):
                pass
            if len(polls) == 3:
                writer.close()
        assert len(polls) < 100
        time.sleep(.002)

    coordinator = coordinator_type()(service, wait=wait)
    try:
        coordinator.run(stop)
    finally:
        writer.close()
        if coordinator._worker:
            coordinator._worker.join(2)
    assert len(service.calls) == 1
    assert not marker.exists()
    with WorkerLock(tmp_path / 'sync.lock'):
        pass


def test_tray_exit_waits_for_forced_child_termination(tmp_path, monkeypatch):
    from pipeline import JobStore
    from queue_runner import QueueRunner
    from tray_app import TrayController
    import worker
    db = PlaylistDB(tmp_path / 'videoscript.db')
    store = JobStore(tmp_path / 'jobs')
    playlist = db.add_or_reactivate_playlist('PL-test', 'url', 'Playlist')
    db.apply_snapshot(playlist.id, [{'video_id': 'aaaaaaaaaaa'}], 100)
    job = store.create('Unresponsive child', 'youtube',
                       'https://www.youtube.com/watch?v=aaaaaaaaaaa')
    db.link_job('aaaaaaaaaaa', job['id'], 'pending')
    db.enqueue(job['id'], 'aaaaaaaaaaa')
    db.claim_next()
    child = subprocess.Popen([sys.executable, '-c',
                              'import time; print("ready", flush=True); time.sleep(60)'],
                             stdout=subprocess.PIPE, text=True)
    active = QueueRunner(db, store, poll_interval=.01)
    polled = threading.Event()
    terminated = []
    real_poll = active._poll
    real_terminate = worker.terminate_process

    def poll(item):
        result = real_poll(item)
        if result.state == 'processing':
            polled.set()
        return result

    def terminate(pid, identity=None):
        result = real_terminate(pid, identity)
        terminated.append((pid, result))
        return result

    monkeypatch.setattr(active, '_poll', poll)
    monkeypatch.setattr(worker, 'terminate_process', terminate)

    def exit_when_active(controller):
        assert polled.wait(2)
        controller.exit()

    controller = TrayController(db, Loop(), active,
                                icon_factory=lambda c: Icon(c, exit_when_active))
    try:
        assert child.stdout.readline().strip() == 'ready'
        store.update_fields(job['id'], status='running', worker_pid=child.pid,
                            worker_identity=worker.process_identity(child.pid))
        controller.run()
        # stop_worker waits five seconds, then forces this non-cooperating child
        # to terminate. Returning before then abandons the active transcription.
        assert not worker.process_is_running(child.pid)
        assert terminated == [(child.pid, True)]
        assert store.load(job['id'])['worker_pid'] is None
        with db._connect() as connection:
            row = connection.execute('SELECT state, retry_count FROM queue_items').fetchone()
        assert (row['state'], row['retry_count']) == ('waiting', 1)
    finally:
        controller.exit()
        for thread in controller._threads:
            thread.join(8)
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        child.stdout.close()


def test_tray_exit_keeps_blocked_discovery_and_preparation_interruptible(tmp_path, monkeypatch):
    from pipeline import JobStore
    from queue_runner import QueueRunner
    from tray_app import SyncCoordinator, TrayController
    db = PlaylistDB(tmp_path / 'videoscript.db')
    store = JobStore(tmp_path / 'jobs')
    playlist = db.add_or_reactivate_playlist('PL-test', 'url', 'Playlist')
    db.apply_snapshot(playlist.id, [{'video_id': 'aaaaaaaaaaa'}], 100)
    job = store.create('Blocked preparation', 'youtube',
                       'https://www.youtube.com/watch?v=aaaaaaaaaaa')
    db.link_job('aaaaaaaaaaa', job['id'], 'pending')
    db.enqueue(job['id'], 'aaaaaaaaaaa')
    discovery_entered, preparation_entered = threading.Event(), threading.Event()
    release, exited, queue_done = threading.Event(), threading.Event(), threading.Event()
    launches = []

    def discover():
        discovery_entered.set()
        assert release.wait(5)

    def prepare(job_id, store, db):
        preparation_entered.set()
        assert release.wait(5)
        return store.load(job_id)

    active = QueueRunner(db, store, poll_interval=.01)
    real_run_loop = active._run_loop

    def run_loop(stop):
        try:
            real_run_loop(stop)
        finally:
            queue_done.set()

    monkeypatch.setattr(active, '_run_loop', run_loop)
    monkeypatch.setattr('queue_runner.prepare_playlist_job', prepare)
    monkeypatch.setattr('queue_runner.launch_worker', lambda *a, **k: launches.append(a))

    def exit_when_blocked(controller):
        assert discovery_entered.wait(2)
        assert preparation_entered.wait(2)
        controller.exit()
        exited.set()

    coordinator = SyncCoordinator(Service(db, discover), poll_interval=.01)
    controller = TrayController(db, coordinator, active,
                                icon_factory=lambda c: Icon(c, exit_when_blocked))
    public = threading.Thread(target=controller.run)
    public.start()
    try:
        assert exited.wait(2)
        public.join(1)
        assert not public.is_alive()
        assert not release.is_set()
        assert launches == []
    finally:
        controller.exit()
        release.set()
        public.join(3)
        if coordinator._worker:
            coordinator._worker.join(2)
        assert queue_done.wait(2)
    assert launches == []
