"""Local job storage, YouTube access, and CPU recognition."""
from contextlib import contextmanager
import functools
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from core import (canonical_youtube_url, job_folder_name,
                  output_filename_stem, parse_json3, plain_text, to_srt)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
os.environ.setdefault('HF_HOME', str(DATA / 'models' / 'hub'))
MODEL_LOCK = threading.Lock()
STORE_LOCK = threading.RLock()


def output_filenames(job):
    """Old jobs keep their existing files; new jobs use the video title."""
    if job.get('output_naming') == 'title':
        stem = output_filename_stem(job['title'])
        return stem + '.txt', stem + '.srt'
    return 'transcript.txt', 'original.srt'


@contextmanager
def _file_lock(path):
    """Serialize job reads/writes across Streamlit and worker processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class JobStore:
    def __init__(self, root=DATA / 'jobs'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.migrate_legacy_directories()

    def migrate_legacy_directories(self):
        """Rename valid ID-only job folders without blocking store startup."""
        with STORE_LOCK:
            for source in self.root.iterdir():
                if not source.is_dir() or not re.fullmatch(r'[a-f0-9]{32}', source.name):
                    continue
                try:
                    path = source / 'job.json'
                    job = json.loads(path.read_text('utf-8'))
                    if not isinstance(job, dict) or job.get('id') != source.name:
                        continue
                    destination = self.root / job_folder_name(
                        job['title'], job['id'],
                    )
                    if destination.exists():
                        continue

                    media_path = Path(job.get('media_path', ''))
                    rebase_media = (
                        job.get('media_managed', False)
                        and media_path.parent.resolve() == source.resolve()
                    )
                    source.rename(destination)
                    try:
                        if rebase_media:
                            job['media_path'] = str(destination / media_path.name)
                            self._write_job_json(job, destination)
                    except Exception:
                        destination.rename(source)
                except (OSError, ValueError, TypeError, AttributeError, KeyError,
                        json.JSONDecodeError):
                    continue

    @staticmethod
    def _validate_job_id(job_id):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise ValueError('无效的任务编号。')
        return job_id

    def _find_directory(self, job_id):
        self._validate_job_id(job_id)
        matches = []
        for path in self.root.glob('*/job.json'):
            try:
                job = json.loads(path.read_text('utf-8'))
                if job.get('id') != job_id:
                    continue
                canonical = job_folder_name(job['title'], job_id)
                matches.append((path.parent.name == canonical, path.parent))
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                continue
        if matches:
            return max(matches, key=lambda item: item[0])[1]
        return None

    def directory(self, job_id):
        self._validate_job_id(job_id)
        found = self._find_directory(job_id)
        if found is not None:
            return found
        return self.root / job_id

    def create(self, title, source, url):
        job = {'id': uuid.uuid4().hex, 'title': title, 'source': source,
               'url': url, 'created': time.time(), 'status': 'running',
               'segments': [], 'error': '', 'edited_text': None,
               'output_naming': 'title'}
        self.save(job)
        return job

    def save(self, job):
        with STORE_LOCK:
            self._validate_job_id(job['id'])
            folder = self._find_directory(job['id'])
            if folder is None:
                folder = self.root / job_folder_name(job['title'], job['id'])
            with _file_lock(folder / '.job.lock'):
                path = folder / 'job.json'
                if path.exists():
                    current = json.loads(path.read_text('utf-8'))
                    job['edited_text'] = current.get('edited_text')
                self._write(job, folder)

    def save_edits(self, job_id, text):
        with STORE_LOCK:
            folder = self.directory(job_id)
            with _file_lock(folder / '.job.lock'):
                job = json.loads((folder / 'job.json').read_text('utf-8'))
                job['edited_text'] = text
                self._write(job)

    def update_fields(self, job_id, **fields):
        with STORE_LOCK:
            folder = self.directory(job_id)
            with _file_lock(folder / '.job.lock'):
                job = json.loads((folder / 'job.json').read_text('utf-8'))
                job.update(fields)
                self._write(job)
                return job

    def _write(self, job, folder=None):
        folder = Path(folder) if folder is not None else self.directory(job['id'])
        folder.mkdir(parents=True, exist_ok=True)
        self._write_job_json(job, folder)
        text_name, subtitle_name = output_filenames(job)
        for name, content in [
            (text_name, job.get('edited_text') if job.get('edited_text') is not None else plain_text(job['segments'])),
            (subtitle_name, to_srt(job['segments'])),
        ]:
            temp = folder / (name + '.tmp')
            temp.write_text(content, encoding='utf-8')
            temp.replace(folder / name)

    def _write_job_json(self, job, folder=None):
        folder = Path(folder) if folder is not None else self.directory(job['id'])
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / 'job.json.tmp'
        temp.write_text(
            json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8',
        )
        temp.replace(folder / 'job.json')

    def load(self, job_id):
        folder = self.directory(job_id)
        with STORE_LOCK:
            with _file_lock(folder / '.job.lock'):
                return json.loads((folder / 'job.json').read_text('utf-8'))

    def list(self):
        jobs = {}
        for path in self.root.glob('*/job.json'):
            try:
                job = json.loads(path.read_text('utf-8'))
                job_id = self._validate_job_id(job['id'])
                canonical = job_folder_name(job['title'], job_id)
                score = path.parent.name == canonical
                previous = jobs.get(job_id)
                if previous is None or score > previous[0]:
                    jobs[job_id] = (score, job)
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                continue
        return sorted(
            (item[1] for item in jobs.values()),
            key=lambda job: job['created'], reverse=True,
        )

    def request_stop(self, job_id):
        marker = self.directory(job_id) / 'stop.requested'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
        self.update_fields(
            job_id, status='stopping',
            message='正在停止；当前识别片段返回后将退出。',
        )

    def clear_stop(self, job_id):
        (self.directory(job_id) / 'stop.requested').unlink(missing_ok=True)

    def stop_requested(self, job_id):
        return (self.directory(job_id) / 'stop.requested').is_file()

    def cleanup_media(self, job_id):
        """Delete one verified, VideoScript-managed media file after success."""
        with STORE_LOCK:
            folder = self.directory(job_id)
            with _file_lock(folder / '.job.lock'):
                job = json.loads((folder / 'job.json').read_text('utf-8'))
                if job.get('status') != 'complete':
                    return job
                if job.get('cleanup_status') == 'complete':
                    return job

                def finish(status, error='', deleted_bytes=0):
                    job.update(
                        cleanup_status=status,
                        cleanup_error=error,
                        cleanup_at=time.time(),
                        cleanup_deleted_bytes=deleted_bytes,
                    )
                    self._write_job_json(job)
                    return job

                if not job.get('auto_cleanup_media', False):
                    return finish('disabled')
                if not job.get('media_managed', False):
                    return finish('not_managed')

                try:
                    if not job.get('segments'):
                        raise ValueError('完成的任务没有可验证的文字分段。')
                    expected_text = (
                        job.get('edited_text')
                        if job.get('edited_text') is not None
                        else plain_text(job['segments'])
                    )
                    text_name, subtitle_name = output_filenames(job)
                    if (folder / text_name).read_text('utf-8') != expected_text:
                        raise ValueError('文字稿未通过完整性验证。')
                    if (folder / subtitle_name).read_text('utf-8') != to_srt(job['segments']):
                        raise ValueError('字幕文件未通过完整性验证。')

                    media_path = Path(job.get('media_path', ''))
                    if media_path.is_symlink():
                        raise ValueError('媒体文件是符号链接，已拒绝清理。')
                    media = media_path.resolve()
                    resolved_folder = folder.resolve()
                    if media.parent != resolved_folder:
                        raise ValueError('媒体文件不在当前任务目录内，已拒绝清理。')
                    if not media.name.startswith(('input.', 'audio.')):
                        raise ValueError('媒体文件不是可识别的临时文件，已拒绝清理。')
                    deleted_bytes = media.stat().st_size if media.is_file() else 0
                    media.unlink(missing_ok=True)
                    return finish('complete', deleted_bytes=deleted_bytes)
                except Exception as exc:
                    return finish('failed', str(exc))


def _options():
    import imageio_ffmpeg
    node = shutil.which('node')
    return {
        'quiet': True, 'no_warnings': False, 'noplaylist': True,
        'socket_timeout': 25, 'retries': 2, 'fragment_retries': 2,
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        'js_runtimes': {'node': {'path': node}} if node else {},
    }


def inspect_youtube(url):
    from yt_dlp import YoutubeDL
    url = canonical_youtube_url(url)
    with YoutubeDL(_options()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get('is_live'):
        raise ValueError('暂不支持正在直播的视频，请等待直播结束。')
    return info


def subtitle_choices(info):
    choices = []
    for kind, field in [('manual', 'subtitles'), ('auto', 'automatic_captions')]:
        for language, tracks in (info.get(field) or {}).items():
            if language != 'live_chat' and any(t.get('ext') == 'json3' for t in tracks):
                choices.append((kind, language))
    return choices


def read_caption(path):
    try:
        return parse_json3(json.loads(Path(path).read_text('utf-8-sig')))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('字幕无法读取；可能是网络限制或无效字幕，请尝试其他字幕或本地转录。') from exc


def extract_captions(info, choice, store, job):
    from yt_dlp import YoutubeDL
    kind, language = choice
    tracks = info['subtitles' if kind == 'manual' else 'automatic_captions'][language]
    track = next(t for t in tracks if t.get('ext') == 'json3')
    path = store.directory(job['id']) / 'captions.json3'
    try:
        with YoutubeDL(_options()) as ydl:
            with ydl.urlopen(track['url']) as response:
                payload = response.read(20 * 1024 * 1024 + 1)
        if len(payload) > 20 * 1024 * 1024:
            raise ValueError('字幕文件过大，请使用本地转录。')
        path.write_bytes(payload)
        job.update(segments=read_caption(path), status='complete', language=language,
                   method='人工字幕' if kind == 'manual' else 'YouTube 自动字幕')
        store.save(job)
        return job
    except Exception as exc:
        job.update(status='failed', error=str(exc))
        store.save(job)
        raise


def download_audio(url, folder, report):
    from yt_dlp import YoutubeDL
    folder = Path(folder)
    def hook(event):
        if event['status'] == 'downloading':
            total = event.get('total_bytes') or event.get('total_bytes_estimate') or 0
            fraction = min(0.99, event.get('downloaded_bytes', 0) / total) if total else 0
            report(fraction, '正在下载音频…')
    options = {**_options(), 'format': 'bestaudio/best',
               'outtmpl': str(folder / 'audio.%(ext)s'), 'progress_hooks': [hook]}
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(canonical_youtube_url(url), download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.is_file():
        raise ValueError('没有找到下载完成的音频文件。')
    return path


@functools.lru_cache(maxsize=1)
def load_model(size):
    from faster_whisper import WhisperModel
    if size not in {'tiny', 'base', 'small', 'medium'}:
        raise ValueError('不支持的模型。')
    return WhisperModel(size, device='cpu', compute_type='int8',
                        cpu_threads=4,
                        download_root=str(DATA / 'models'))


def process_transcription(store, job, chunks, duration, report):
    checkpoint = max((part.get('end', 0) for part in job['segments']), default=0)
    try:
        if store.stop_requested(job['id']):
            job.update(status='paused', error='', message='转录已停止，可稍后继续。')
            store.save(job)
            return job
        for chunk in chunks:
            if store.stop_requested(job['id']):
                job.update(status='paused', error='', message='转录已停止，可稍后继续。')
                store.save(job)
                return job
            if chunk['end'] <= checkpoint:
                continue
            job['segments'].append(chunk)
            progress = min(0.99, chunk['end'] / duration) if duration else 0
            message = f"已识别 {chunk['end'] / 60:.1f} / {duration / 60:.1f} 分钟音频"
            job.update(progress=progress, message=message)
            store.save(job)
            report(progress, message)
        if not job['segments']:
            raise ValueError('没有识别到语音；请检查文件音轨或尝试指定语言。')
        job.update(status='complete', progress=1.0, message='文字稿已生成并保存。')
        store.save(job)
        report(1.0, '文字稿已生成并保存。')
        return job
    except Exception as exc:
        import traceback
        traceback.print_exc()
        job['exception_type'] = type(exc).__name__
        job['traceback'] = traceback.format_exc()
        job.update(status='failed', error=str(exc) or '任务被中断；已完成的文字已保存。')
        store.save(job)
        raise


def transcribe(path, size, language, glossary, store, job, report):
    if not MODEL_LOCK.acquire(blocking=False):
        raise ValueError('另一个任务正在使用 CPU 转录，请等待完成。')
    try:
        report(0, '正在加载模型；首次使用会联网下载，终端窗口会显示下载进度…')
        model = load_model(size)
        report(0, '正在读取音频并检测语音…')
        checkpoint = max(
            (part.get('end', 0) for part in job['segments']), default=0,
        )
        segments, info = model.transcribe(
            str(path), language=language or None, beam_size=2, vad_filter=False,
            initial_prompt=glossary.strip() or None, task='transcribe',
            clip_timestamps=[checkpoint],
        )
        job.update(language=info.language, method=f'本地 Whisper {size} / CPU int8')
        def chunks():
            for segment in segments:
                if segment.text.strip():
                    yield {'start': segment.start, 'end': segment.end, 'text': segment.text.strip()}
        return process_transcription(store, job, chunks(), info.duration, report)
    finally:
        MODEL_LOCK.release()
