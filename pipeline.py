"""Local job storage, YouTube access, and CPU recognition."""
import functools
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from core import canonical_youtube_url, parse_json3, plain_text, to_srt

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
os.environ.setdefault('HF_HOME', str(DATA / 'models' / 'hub'))
MODEL_LOCK = threading.Lock()
STORE_LOCK = threading.RLock()


class JobStore:
    def __init__(self, root=DATA / 'jobs'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, job_id):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise ValueError('无效的任务编号。')
        return self.root / job_id

    def create(self, title, source, url):
        job = {'id': uuid.uuid4().hex, 'title': title, 'source': source,
               'url': url, 'created': time.time(), 'status': 'running',
               'segments': [], 'error': '', 'edited_text': None}
        self.save(job)
        return job

    def save(self, job):
        with STORE_LOCK:
            path = self.directory(job['id']) / 'job.json'
            if path.exists():
                current = json.loads(path.read_text('utf-8'))
                job['edited_text'] = current.get('edited_text')
            self._write(job)

    def save_edits(self, job_id, text):
        with STORE_LOCK:
            job = self.load(job_id)
            job['edited_text'] = text
            self._write(job)

    def _write(self, job):
        folder = self.directory(job['id'])
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in [
            ('job.json', json.dumps(job, ensure_ascii=False, indent=2)),
            ('transcript.txt', job.get('edited_text') if job.get('edited_text') is not None else plain_text(job['segments'])),
            ('original.srt', to_srt(job['segments'])),
        ]:
            temp = folder / (name + '.tmp')
            temp.write_text(content, encoding='utf-8')
            temp.replace(folder / name)

    def load(self, job_id):
        return json.loads((self.directory(job_id) / 'job.json').read_text('utf-8'))

    def list(self):
        jobs = []
        for path in self.root.glob('*/job.json'):
            try:
                jobs.append(json.loads(path.read_text('utf-8')))
            except (OSError, ValueError):
                continue
        return sorted(jobs, key=lambda job: job['created'], reverse=True)


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
                        cpu_threads=min(8, os.cpu_count() or 4),
                        download_root=str(DATA / 'models'))


def process_transcription(store, job, chunks, duration, report):
    try:
        for chunk in chunks:
            job['segments'].append(chunk)
            store.save(job)
            progress = min(0.99, chunk['end'] / duration) if duration else 0
            report(progress, f"已识别 {chunk['end'] / 60:.1f} / {duration / 60:.1f} 分钟音频")
        if not job['segments']:
            raise ValueError('没有识别到语音；请检查文件音轨或尝试指定语言。')
        job['status'] = 'complete'
        store.save(job)
        report(1.0, '文字稿已生成并保存。')
        return job
    except BaseException as exc:
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
        segments, info = model.transcribe(
            str(path), language=language or None, beam_size=5, vad_filter=True,
            initial_prompt=glossary.strip() or None, task='transcribe',
        )
        job.update(language=info.language, method=f'本地 Whisper {size} / CPU int8')
        def chunks():
            for segment in segments:
                if segment.text.strip():
                    yield {'start': segment.start, 'end': segment.end, 'text': segment.text.strip()}
        return process_transcription(store, job, chunks(), info.duration, report)
    finally:
        MODEL_LOCK.release()
