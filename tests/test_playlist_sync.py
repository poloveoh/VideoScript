import sys
import types

import pytest

from core import canonical_youtube_playlist_url


@pytest.mark.parametrize(('value', 'expected'), [
    ('https://www.youtube.com/playlist?list=PLabc_123&index=4',
     ('https://www.youtube.com/playlist?list=PLabc_123', 'PLabc_123')),
    ('https://youtube.com/playlist?list=PLabc_123&si=tracking',
     ('https://www.youtube.com/playlist?list=PLabc_123', 'PLabc_123')),
    ('https://music.youtube.com/playlist?list=PLabc_123',
     ('https://www.youtube.com/playlist?list=PLabc_123', 'PLabc_123')),
])
def test_canonicalizes_playlist_url(value, expected):
    assert canonical_youtube_playlist_url(value) == expected


@pytest.mark.parametrize('value', [
    'https://youtube.com/watch?v=BaW_jenozKc',
    'http://youtube.com/playlist?list=PLabc',
    'https://youtube.com.evil.test/playlist?list=PLabc',
    'https://user:pass@youtube.com/playlist?list=PLabc',
    'https://@youtube.com/playlist?list=PLabc',
    'https://youtube.com/playlist?list=',
    'https://youtube.com/playlist?list=bad%20id',
    'https://youtu.be/PLabc',
    'https://youtube.com:444/playlist?list=PLabc',
    'not a URL',
])
def test_rejects_invalid_playlist_urls(value):
    with pytest.raises(ValueError):
        canonical_youtube_playlist_url(value)


def _install_fake_ytdlp(monkeypatch, info, seen):
    monkeypatch.setitem(
        sys.modules, 'imageio_ffmpeg',
        types.SimpleNamespace(get_ffmpeg_exe=lambda: 'ffmpeg.exe'),
    )

    class FakeYoutubeDL:
        def __init__(self, options):
            seen['options'] = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            seen.update(url=url, download=download)
            return info

    monkeypatch.setitem(sys.modules, 'yt_dlp', types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))


def test_fetch_playlist_extracts_flat_entries_and_preserves_first_occurrences(monkeypatch):
    from playlist_sync import PlaylistSnapshot, PlaylistVideo, fetch_playlist

    seen = {}
    info = {
        'id': 'PLabc_123', 'title': 'Playlist title',
        'entries': [
            {'id': 'BaW_jenozKc', 'title': 'First', 'duration': 12, 'language': 'en'},
            None,
            {'id': 'BaW_jenozKc', 'title': 'Duplicate'},
            {'id': 'dQw4w9WgXcQ', 'title': 'Second'},
            {'id': 'Abcdefghijk', 'title': 'Unavailable',
             'availability': 'unavailable'},
            {'id': 'Lmnopqrstuv', 'title': 'Private', 'availability': 'private'},
            {'id': 'WxyzABCDEFG', 'title': 'Deleted', 'availability': 'deleted'},
            {'title': 'No ID'},
        ],
    }
    _install_fake_ytdlp(monkeypatch, info, seen)

    snapshot = fetch_playlist('https://www.youtube.com/playlist?list=PLabc_123&index=2')

    assert snapshot == PlaylistSnapshot(
        playlist_id='PLabc_123', title='Playlist title',
        url='https://www.youtube.com/playlist?list=PLabc_123',
        videos=[
            PlaylistVideo('BaW_jenozKc', 'First',
                          'https://www.youtube.com/watch?v=BaW_jenozKc', 1, 12, 'en'),
            PlaylistVideo('dQw4w9WgXcQ', 'Second',
                          'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 4, None, None),
        ],
    )
    assert seen['url'] == snapshot.url
    assert seen['download'] is False
    assert seen['options']['noplaylist'] is False
    assert seen['options']['extract_flat'] == 'in_playlist'
    assert seen['options']['retries'] > 0
    assert seen['options']['socket_timeout'] > 0
    assert 'cookiefile' not in seen['options']
    assert 'cookiesfrombrowser' not in seen['options']


def test_fetch_playlist_rejects_ytdlp_playlist_id_mismatch(monkeypatch):
    from playlist_sync import fetch_playlist

    seen = {}
    _install_fake_ytdlp(monkeypatch,
                        {'id': 'PLother', 'title': 'Wrong', 'entries': []}, seen)
    with pytest.raises(ValueError, match='(?i)playlist'):
        fetch_playlist('https://youtube.com/playlist?list=PLrequested')


@pytest.mark.parametrize('info', [
    {'title': 'Missing ID', 'entries': []},
    {'id': '', 'title': 'Empty ID', 'entries': []},
    {'id': None, 'title': 'Null ID', 'entries': []},
])
def test_fetch_playlist_rejects_missing_or_empty_ytdlp_playlist_id(
        monkeypatch, info):
    from playlist_sync import fetch_playlist

    seen = {}
    _install_fake_ytdlp(monkeypatch, info, seen)
    with pytest.raises(ValueError, match='(?i)playlist'):
        fetch_playlist('https://youtube.com/playlist?list=PLrequested')
