"""Flat discovery of YouTube Playlist entries for automatic synchronization."""
from dataclasses import dataclass
import re

from core import canonical_youtube_playlist_url


@dataclass(frozen=True)
class PlaylistVideo:
    video_id: str
    title: str
    url: str
    position: int
    duration: float | None
    language: str | None


@dataclass(frozen=True)
class PlaylistSnapshot:
    playlist_id: str
    title: str
    url: str
    videos: list[PlaylistVideo]


def fetch_playlist(url: str) -> PlaylistSnapshot:
    """Fetch a flat, cookie-free Playlist snapshot using yt-dlp."""
    canonical_url, playlist_id = canonical_youtube_playlist_url(url)
    from pipeline import _options
    from yt_dlp import YoutubeDL

    options = {
        **_options(),
        'noplaylist': False,
        'extract_flat': 'in_playlist',
        'socket_timeout': 25,
        'retries': 2,
        'fragment_retries': 2,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(canonical_url, download=False)
    if not isinstance(info, dict):
        raise ValueError('yt-dlp did not return Playlist metadata.')
    extracted_id = info.get('id')
    if extracted_id and extracted_id != playlist_id:
        raise ValueError('yt-dlp returned a different Playlist than requested.')

    videos = []
    seen_ids = set()
    for position, entry in enumerate(info.get('entries') or [], 1):
        if not isinstance(entry, dict):
            continue
        video_id = entry.get('id')
        title = entry.get('title')
        availability = entry.get('availability')
        if (not isinstance(video_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id)
                or not isinstance(title, str) or not title.strip()
                or availability in {'private', 'unavailable', 'deleted'}
                or title.strip().lower() in {'[deleted video]', '[private video]'}
                or video_id in seen_ids):
            continue
        seen_ids.add(video_id)
        videos.append(PlaylistVideo(
            video_id=video_id,
            title=title,
            url=f'https://www.youtube.com/watch?v={video_id}',
            position=position,
            duration=entry.get('duration'),
            language=entry.get('language'),
        ))

    return PlaylistSnapshot(
        playlist_id=playlist_id,
        title=info.get('title') or playlist_id,
        url=canonical_url,
        videos=videos,
    )
