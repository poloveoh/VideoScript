"""Windows tray companion and cross-process Playlist sync scheduling."""
from pathlib import Path
import os
import sys
import threading
import time
import webbrowser

from worker import WorkerBusy, WorkerLock


def request_sync(marker_path: Path) -> None:
    """Atomically publish a coalescing request, including from the web process."""
    marker_path = Path(marker_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation publishes the complete (empty) marker in one operation.
    try:
        with marker_path.open('xb'):
            pass
    except FileExistsError:
        pass


class SyncCoordinator:
    def __init__(self, service, db=None, marker_path=None, *, clock=time.monotonic,
                 wait=None, interval=600, poll_interval=.1):
        self.service = service
        self.db = db if db is not None else service.db
        self.marker_path = Path(marker_path or self.db.path.parent / 'sync.requested')
        self.lock_path = self.marker_path.with_name('sync.lock')
        self.clock = clock
        self.wait = wait
        self.interval = interval
        self.poll_interval = poll_interval
        self.syncing = False
        self.error = ''
        self._worker = None

    def _sync(self, lock):
        try:
            summaries = self.service.sync_enabled()
            self.error = '; '.join(item.error for item in summaries if item.error)
        except Exception as exc:
            self.error = str(exc)
        finally:
            lock.__exit__(None, None, None)
            self.syncing = False

    def run(self, stop_event):
        was_paused = self.db.auto_sync_paused()
        next_sync = self.clock()
        while not stop_event.is_set():
            paused = self.db.auto_sync_paused()
            now = self.clock()
            if not paused and (was_paused or now >= next_sync):
                request_sync(self.marker_path)
                next_sync = now + self.interval
            was_paused = paused
            if (self.marker_path.exists()
                    and (self._worker is None or not self._worker.is_alive())):
                lock = WorkerLock(self.lock_path)
                try:
                    lock.__enter__()
                except WorkerBusy:
                    pass
                else:
                    # The lock owner alone consumes requests. Requests published
                    # after this unlink remain for exactly one follow-up run.
                    try:
                        if self.marker_path.exists():
                            self.marker_path.unlink()
                            self.syncing = True
                            self._worker = threading.Thread(
                                target=self._sync, args=(lock,), daemon=True)
                            self._worker.start()
                        else:
                            lock.__exit__(None, None, None)
                    except BaseException:
                        self.syncing = False
                        lock.__exit__(None, None, None)
                        raise
            (self.wait or stop_event.wait)(self.poll_interval)


def make_icon(state):
    from PIL import Image, ImageDraw
    colors = {'normal': '#26a269', 'paused': '#e5a50a',
              'syncing': '#3584e4', 'error': '#e01b24'}
    image = Image.new('RGB', (32, 32), '#202020')
    ImageDraw.Draw(image).ellipse((3, 3, 29, 29), fill=colors[state])
    return image


def _notify(message):
    print(message, file=sys.stderr, flush=True)
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'VideoScript tray', 0x10)


def _tray_icon(controller):
    # Import lazily so web marker requests never initialize a notification area.
    import pystray

    def label(index):
        return lambda item: controller.menu()[index][0]

    def action(index):
        return lambda icon, item: controller.menu()[index][1]()

    menu = pystray.Menu(*(pystray.MenuItem(label(i), action(i)) for i in range(4)))
    return pystray.Icon('VideoScript', make_icon(controller.state),
                        controller.tooltip, menu)


class TrayController:
    def __init__(self, db, coordinator, queue_runner, *, icon_factory=None,
                 browser_open=webbrowser.open, notify=_notify, stop_event=None):
        self.db = db
        self.coordinator = coordinator
        self.queue_runner = queue_runner
        self.icon_factory = icon_factory or _tray_icon
        self.browser_open = browser_open
        self.notify = notify
        self.stop_event = stop_event or threading.Event()
        self.icon = None
        self._threads = []
        self._background_error = ''

    @property
    def state(self):
        if self._background_error or self.coordinator.error:
            return 'error'
        if self.coordinator.syncing:
            return 'syncing'
        return 'paused' if self.db.auto_sync_paused() else 'normal'

    @property
    def tooltip(self):
        descriptions = {'normal': 'Auto Sync on', 'paused': 'Auto Sync paused',
                        'syncing': 'Syncing Playlists',
                        'error': self._background_error or self.coordinator.error}
        return ('VideoScript — ' + descriptions[self.state])[:127]

    def menu(self):
        return [('Open VideoScript', self.open_app), ('Sync Now', self.sync_now),
                ('Resume Auto Sync' if self.db.auto_sync_paused() else 'Pause Auto Sync',
                 self.toggle_pause), ('Exit', self.exit)]

    def open_app(self):
        self.browser_open('http://127.0.0.1:8501')

    def sync_now(self):
        request_sync(self.db.path.parent / 'sync.requested')

    def toggle_pause(self):
        self.db.set_auto_sync_paused(not self.db.auto_sync_paused())
        self._refresh()

    def exit(self):
        self.stop_event.set()
        if self.icon is not None:
            self.icon.stop()

    def _refresh(self):
        if self.icon is not None:
            self.icon.title = self.tooltip
            self.icon.icon = make_icon(self.state)
            self.icon.update_menu()

    def _monitor(self, stop_event):
        previous = None
        while not stop_event.is_set():
            current = (self.state, self.tooltip)
            if current != previous:
                self._refresh()
                previous = current
            stop_event.wait(.1)

    def _background(self, target):
        try:
            target(self.stop_event)
        except Exception as exc:
            self._background_error = str(exc)
            self.notify(f'VideoScript background loop failed: {exc}')
            self.exit()

    def _setup(self, icon):
        # pystray invokes setup only after its native event loop is ready.
        # Starting loops earlier can lose an immediate stop on startup failure.
        def start(stop_event):
            icon.visible = True
            for target in (self.coordinator.run,
                           self.queue_runner.run_until_idle, self._monitor):
                thread = threading.Thread(target=self._background,
                                          args=(target,), daemon=True)
                self._threads.append(thread)
                thread.start()

        self._background(start)

    def run(self):
        # A separate lifetime lock rejects a second tray; sync.lock only covers
        # a single discovery pass and is shared by all coordinator processes.
        with WorkerLock(self.db.path.parent / 'tray.lock'):
            try:
                self.icon = self.icon_factory(self)
                self._refresh()
                self.icon.run(setup=self._setup)
            except Exception as exc:
                self.notify(f'VideoScript tray failed to start: {exc}')
                raise
            finally:
                self.exit()
                for thread in self._threads:
                    thread.join(timeout=2)


def main(data_dir=None):
    from pipeline import DATA, JobStore
    from playlist_db import PlaylistDB
    from playlist_service import PlaylistService
    from queue_runner import QueueRunner

    data_dir = Path(data_dir or DATA)
    db = PlaylistDB(data_dir / 'videoscript.db')
    store = JobStore(data_dir / 'jobs')
    service = PlaylistService(db, store)
    TrayController(db, SyncCoordinator(service), QueueRunner(db, store)).run()


if __name__ == '__main__':
    main()
