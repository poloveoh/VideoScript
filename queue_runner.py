"""Single-item durable FIFO execution, shared by tray scheduling and recovery."""
from dataclasses import dataclass

from pipeline import JobStore
from playlist_processor import prepare_playlist_job
from worker import (WorkerBusy, WorkerLock, launch_worker, process_is_running,
                    stop_worker)


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

    def _poll(self, item):
        try:
            job = self.store.load(item['job_id'])
        except FileNotFoundError:
            return RunResult('repair', item['job_id'], item['video_id'],
                             'Filesystem job missing; synchronize Playlist to repair')
        if process_is_running(job.get('worker_pid')):
            return RunResult('processing', job['id'])
        if job['status'] == 'complete':
            return self._finish(job['id'], 'completed')
        state = 'failed' if job['status'] == 'failed' else 'interrupted'
        return self._finish(job['id'], state, job.get('error') or 'Worker interrupted')

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
        try:
            with WorkerLock(self.lock_path):
                processing = self._processing()
                if processing:
                    return self._poll(processing[0])
                if any(process_is_running(job.get('worker_pid'))
                       for job in self.store.list()):
                    return RunResult('busy')
                with WorkerLock(self.cpu_lock_path):
                    item = self.db.claim_next()
                    if item is None:
                        return RunResult('idle')
                    try:
                        self.store.load(item.job_id)
                    except FileNotFoundError:
                        return RunResult('repair', item.job_id, item.video_id,
                                         'Filesystem job missing; synchronize Playlist to repair')
                    try:
                        job = prepare_playlist_job(item.job_id, self.store, self.db)
                    except Exception as exc:
                        self.store.update_fields(item.job_id, status='failed', error=str(exc))
                        return self._finish(item.job_id, 'failed', str(exc))
                    if job['status'] == 'complete':
                        self.db.finish_queue_item(item.job_id, 'completed')
                        return RunResult('completed', item.job_id)
                try:
                    launch_worker(item.job_id, store=self.store)
                except Exception as exc:
                    self.store.update_fields(item.job_id, status='failed', error=str(exc))
                    return self._finish(item.job_id, 'failed', str(exc))
                return RunResult('processing', item.job_id)
        except WorkerBusy:
            return RunResult('busy')

    def run_until_idle(self, stop_event) -> None:
        self.recover()
        try:
            while not stop_event.is_set():
                result = self.run_once()
                if result.state != 'completed':
                    stop_event.wait(self.poll_interval)
        finally:
            try:
                with WorkerLock(self.lock_path):
                    for item in self._processing():
                        try:
                            job = self.store.load(item['job_id'])
                        except FileNotFoundError:
                            continue
                        if process_is_running(job.get('worker_pid')):
                            stop_worker(job['id'], store=self.store)
                        self._poll(item)
            except WorkerBusy:
                pass
