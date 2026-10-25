"""Transcript conversion without network or model dependencies."""
import html
import re
from urllib.parse import parse_qs, urlparse


def canonical_youtube_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('请输入有效的 HTTPS YouTube 视频链接。')
    video_id = ''
    if host in {'youtu.be', 'www.youtu.be'}:
        video_id = parsed.path.strip('/')
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        if parsed.path == '/watch':
            video_id = parse_qs(parsed.query).get('v', [''])[0]
        else:
            match = re.fullmatch(r'/(?:shorts|live|embed)/([\w-]{11})/?', parsed.path)
            video_id = match.group(1) if match else ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('请输入单个 YouTube 视频的链接；不支持播放列表或其他网站。')
    return f'https://www.youtube.com/watch?v={video_id}'


def parse_json3(data: dict) -> list[dict]:
    result = []
    previous_text, previous_end = '', -1
    for event in data.get('events', []):
        raw = ''.join(str(seg.get('utf8', '')) for seg in event.get('segs', []))
        text = re.sub(r'\s+', ' ', html.unescape(raw)).strip()
        if not text:
            continue
        start = max(0, float(event.get('tStartMs', 0)) / 1000)
        end = start + max(0.01, float(event.get('dDurationMs', 1000)) / 1000)
        original = text
        if start < previous_end:
            # Rolling captions may repeat the previous complete cue or its suffix.
            for length in range(min(len(previous_text), len(text)), 0, -1):
                if previous_text[-length:] == text[:length]:
                    if length == len(text) or length == len(previous_text) or text[length:length+1] == ' ':
                        text = text[length:].strip()
                        break
        previous_text, previous_end = original, end
        if text:
            result.append({'start': start, 'end': end, 'text': text})
    if not result:
        raise ValueError('字幕文件中没有可读文字，请尝试其他字幕或本地转录。')
    return result


def plain_text(segments: list[dict]) -> str:
    paragraphs, current = [], []
    last_end = 0
    for seg in segments:
        if current and (seg['start'] - last_end > 2 or len(' '.join(current)) > 550):
            paragraphs.append(' '.join(current))
            current = []
        current.append(seg['text'].strip())
        last_end = seg['end']
    if current:
        paragraphs.append(' '.join(current))
    return '\n\n'.join(paragraphs)


def readable_legacy_text(value):
    """Repair UTF-8 text that an older Windows run decoded as CP1252."""
    if not isinstance(value, str):
        return value
    try:
        return value.encode('cp1252').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def timestamp(seconds: float, srt: bool = False) -> str:
    millis = max(0, round(seconds * 1000))
    seconds, ms = divmod(millis, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    result = f'{hours:02}:{minute:02}:{sec:02}'
    return f'{result},{ms:03}' if srt else result


def to_srt(segments: list[dict]) -> str:
    return '\n'.join(
        f"{i}\n{timestamp(s['start'], True)} --> {timestamp(s['end'], True)}\n{s['text']}\n"
        for i, s in enumerate(segments, 1)
    )
