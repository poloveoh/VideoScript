"""Single-item durable FIFO execution, shared by tray scheduling and recovery."""
from dataclasses import dataclass
import threading
import time

from pipeline import JobStore
from playlist_db import QueueItem
from playlist_processor import prepare_playlist_job
from worker import (WorkerBusy, WorkerLock, launch_worker, process_identity,
                    process_is_running, stop_worker)


REPAIR_ERROR = 'Filesystem job missing; synchronize Playlist to repair'


@dataclass(frozen=True)
class RunResult:
    state: str
    job_id: str | None = None
    repair_video_id: str | None = None
    error: str = ''


class QueueRunner:
    def __init__(self, db, store=None, *, poll_interval=0.1):
        self.db = db
        self.store = store or JobStore()
        self.poll_interval = poll_interval
        self.lock_path = db.path.with_suffix('.runner.lock')
        self.cpu_lock_path = self.store.root.parent / 'transcription.lock'

    def _processing(self):
        with self.db._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM queue_items WHERE state='processing' ORDER BY position")]

    def _load_job(self, job_id):
        # JobStore resolves the directory before its file lock. On Windows a
        # concurrent replace can briefly hide job.json from that directory scan.
        for attempt in range(3):
            try:
                return self.store.load(job_id)
            except FileNotFoundError:
                if attempt == 2:
                    raise
                time.sleep(0.01)

    def _poll(self, item):
        try:
            job = self._load_job(item['job_id'])
        except FileNotFoundError:
            return self._repair(item['job_id'], item['video_id'])
        if self._worker_active(job):
            return RunResult('processing', job['id'])
        if job['status'] == 'complete':
            return self._finish(job['id'], 'completed')
        if item['error'] == REPAIR_ERROR and job['status'] == 'waiting':
            return RunResult('ready', job['id'])
        state = 'failed' if job['status'] == 'failed' else 'interrupted'
        return self._finish(job['id'], state, job.get('error') or 'Worker interrupted')

    def _worker_active(self, job):
        pid = job.get('worker_pid')
        identity = job.get('worker_identity')
        terminal = job['status'] in {'complete', 'failed', 'paused'}
        active = (process_is_running(pid)
                  and (process_identity(pid) == identity if identity is not None
                       else not terminal))
        if not active and terminal and pid is not None:
            self.store.update_fields(job['id'], worker_pid=None, worker_identity=None)
        return active

    def _repair(self, job_id, video_id):
        # Persist the reason across runner instances without spending an attempt.
        with self.db._write() as connection:
            connection.execute("UPDATE queue_items SET error=? WHERE job_id=? AND state='processing'",
                               (REPAIR_ERROR, job_id))
        return RunResult('repair', job_id, video_id, REPAIR_ERROR)

    def _finish(self, job_id, state, error=''):
        self.db.finish_queue_item(job_id, state, error)
        with self.db._connect() as connection:
            row = connection.execute('SELECT state FROM queue_items WHERE job_id=?',
                                     (job_id,)).fetchone()
        return RunResult(row['state'], job_id, error=error)

    def recover(self) -> None:
        try:
            with WorkerLock(self.lock_path):
                for item in self._processing():
                    self._poll(item)
        except WorkerBusy:
            pass

    def run_once(self) -> RunResult:
        return self._run_once()

    def _run_once(self, stop_event=None):
        def stopping():
            return stop_event is not None and stop_event.is_set()

        try:
            with WorkerLock(self.lock_path):
                if stopping():
                    return RunResult('stopped')
                processing = self._processing()
                item = None
                if processing:
                    result = self._poll(processing[0])
                    if result.state != 'ready':
                        return result
                    item = QueueItem(**processing[0])
                if any(self._worker_active(job)
                       for job in self.store.list()):
                    return RunResult('busy')
                with WorkerLock(self.cpu_lock_path):
                    item = item or self.db.claim_next()
                    if item is None:
                        return RunResult('idle')
                    try:
                        self._load_job(item.job_id)
                    except FileNotFoundError:
                        return self._repair(item.job_id, item.video_id)
                    with self.db._write() as connection:
                        connection.execute("UPDATE queue_items SET error='' WHERE job_id=?",
                                           (item.job_id,))
                    try:
                        job = prepare_playlist_job(item.job_id, self.store, self.db)
                    except Exception as exc:
                        self.store.update_fields(item.job_id, status='failed', error=str(exc))
                        return self._finish(item.job_id, 'failed', str(exc))
                    if job['status'] == 'complete':
                        self.db.finish_queue_item(item.job_id, 'completed')
                        return RunResult('completed', item.job_id)
                    if stopping():
                        return self._finish(item.job_id, 'interrupted', 'Stopped during preparation')
                if stopping():
                    return self._finish(item.job_id, 'interrupted', 'Stopped before worker launch')
                try:
                    launch_worker(item.job_id, store=self.store)
                except Exception as exc:
                    self.store.update_fields(item.job_id, status='failed', error=str(exc))
                    return self._finish(item.job_id, 'failed', str(exc))
                return RunResult('processing', item.job_id)
        except WorkerBusy:
            return RunResult('busy')

    def run_until_idle(self, stop_event) -> None:
        if stop_event.is_set():
            self._shutdown()
            return
        finished = threading.Event()
        errors = []

        def drive():
            try:
                self._run_loop(stop_event)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        # A blocking external caption request cannot delay application exit.
        # It retains both locks until it unwinds and observes cancellation.
        thread = threading.Thread(target=drive, daemon=True)
        thread.start()
        while not stop_event.is_set() and not finished.wait(self.poll_interval):
            pass
        if stop_event.is_set():
            self._shutdown()
        if finished.is_set() and errors:
            raise errors[0]

    def _run_loop(self, stop_event):
        self.recover()
        try:
            while not stop_event.is_set():
                result = self._run_once(stop_event)
                if result.state != 'completed':
                    stop_event.wait(self.poll_interval)
        finally:
            self._shutdown()

    def _shutdown(self):
        try:
            with WorkerLock(self.lock_path):
                for item in self._processing():
                    try:
                        job = self._load_job(item['job_id'])
                    except FileNotFoundError:
                        continue
                    if self._worker_active(job):
                        stop_worker(job['id'], store=self.store)
                    self._poll(item)
        except WorkerBusy:
            pass
