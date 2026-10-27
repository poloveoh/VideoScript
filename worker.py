"""Standalone transcription worker, intentionally independent of Streamlit."""
from contextlib import AbstractContextManager
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from pipeline import DATA, ROOT, JobStore, download_audio, transcribe


class WorkerBusy(RuntimeError):
    pass


def _windows_process_api():
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


def process_is_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        kernel32 = _windows_process_api()
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_process(pid):
    pid = int(pid or 0)
    if pid <= 0 or pid == os.getpid():
        return False
    if not process_is_running(pid):
        return True
    if os.name == 'nt':
        kernel32 = _windows_process_api()
        handle = kernel32.OpenProcess(0x00100001, False, pid)
        if not handle:
            return False
        try:
            stopped = bool(kernel32.TerminateProcess(handle, 1))
            if stopped:
                kernel32.WaitForSingleObject(handle, 2000)
            return stopped
        finally:
            kernel32.CloseHandle(handle)
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return not process_is_running(pid)


def stop_worker(job_id, store=None, timeout=5.0):
    store = store or JobStore()
    current = store.load(job_id)
    if current.get('status') in {'complete', 'failed'}:
        return current
    store.request_stop(job_id)
    current = store.load(job_id)
    pid = current.get('worker_pid')
    deadline = time.monotonic() + timeout
    while (
        current.get('status') not in {'paused', 'complete', 'failed'}
        and process_is_running(pid)
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)
        current = store.load(job_id)
    if current.get('status') in {'complete', 'failed'}:
        return current
    if process_is_running(pid):
        terminate_process(pid)
    return store.update_fields(
        job_id, status='paused', worker_pid=None, error='',
        message='转录已停止；尚未返回的当前片段已舍弃。',
    )


class WorkerLock(AbstractContextManager):
    """Hold one byte of a file until this process exits."""

    def __init__(self, path=DATA / 'transcription.lock'):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise WorkerBusy('另一个任务正在使用 CPU 转录。') from exc
        return self

    def __exit__(self, *_):
        if self.file is not None:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
        return False


def run_job(job_id, store=None, lock_path=DATA / 'transcription.lock'):
    store = store or JobStore()
    started = time.monotonic()
    try:
        with WorkerLock(lock_path):
            job = store.load(job_id)
            if store.stop_requested(job_id):
                job.update(status='paused', error='', message='转录已停止，可稍后继续。')
                store.save(job)
                return job

            job.update(status='running', error='', worker_pid=os.getpid(),
                       heartbeat=time.time(), message='正在准备转录…')
            store.save(job)

            def report(progress, message):
                job.update(progress=float(progress), message=message,
                           heartbeat=time.time())
                store.save(job)

            media_path = job.get('media_path')
            if not media_path and job.get('source') == 'youtube':
                media_path = download_audio(
                    job['url'], store.directory(job_id), report,
                )
                job['media_path'] = str(media_path)
                store.save(job)
            path = Path(media_path or '')
            if not path.is_file():
                raise FileNotFoundError('找不到任务的音频或视频文件。')

            result = transcribe(
                path, job.get('model', 'small'), job.get('language'),
                job.get('glossary', ''), store, job, report,
            )
            result['elapsed'] = round(
                result.get('elapsed', 0) + time.monotonic() - started, 1,
            )
            # Keep the PID until the process actually exits, including cleanup.
            result['worker_pid'] = os.getpid()
            store.save(result)
            try:
                return store.cleanup_media(job_id)
            except Exception as cleanup_exc:
                result.update(
                    cleanup_status='failed',
                    cleanup_error=str(cleanup_exc),
                    cleanup_at=time.time(),
                )
                try:
                    store.save(result)
                except Exception:
                    pass
                return result
    except WorkerBusy as exc:
        return store.load(job_id)
    except Exception as exc:
        job = store.load(job_id)
        job.update(status='failed', error=str(exc), message='转录失败。',
                   exception_type=type(exc).__name__,
                   traceback=traceback.format_exc())
        job['worker_pid'] = os.getpid()
        store.save(job)
        return job


def launch_worker(job_id, store=None):
    store = store or JobStore()
    store.clear_stop(job_id)
    job = store.load(job_id)
    job.update(status='queued', error='', message='任务已提交，正在启动…')
    store.save(job)
    flags = 0
    if os.name == 'nt':
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(
        [sys.executable, str(ROOT / 'worker.py'), job_id,
         '--jobs-root', str(store.root.resolve())],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
    )
    store.update_fields(job_id, worker_pid=process.pid)
    return process


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[2] == '--jobs-root':
        worker_store = JobStore(Path(sys.argv[3]))
        run_job(sys.argv[1], store=worker_store,
                lock_path=worker_store.root.parent / 'transcription.lock')
    elif len(sys.argv) == 2:
        run_job(sys.argv[1])
    else:
        raise SystemExit('用法：worker.py <任务编号>')
