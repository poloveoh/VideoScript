import pytest

from core import (canonical_youtube_url, parse_json3, plain_text,
                  readable_legacy_text, to_srt)


@pytest.mark.parametrize('title, expected', [
    ('影片标题', '影片标题'),
    ('Lecture.mp4', 'Lecture'),
    ('../影片: "标题"?* ', '_影片_ _标题___'),
    ('CON', '_CON'),
    ('CONIN$', '_CONIN$'),
    ('CONOUT$', '_CONOUT$'),
    ('CON .foo', '_CON .foo'),
    ('COM1 .test', '_COM1 .test'),
    ('NUL.txt', '_NUL.txt'),
    (' ... ', 'transcript'),
    ('a' * 200, 'a' * 80),
])
def test_output_filename_stem_is_readable_and_windows_safe(title, expected):
    from core import output_filename_stem
    assert output_filename_stem(title) == expected


@pytest.mark.parametrize('title, job_id, expected', [
    ('影片标题.mp4', 'a' * 32, '影片标题_aaaaaaaa'),
    ('../影片: "标题"?* ', 'b' * 32, '_影片_ _标题____bbbbbbbb'),
    ('CON.mp4', 'c' * 32, '_CON_cccccccc'),
    (' ... ', 'd' * 32, 'transcript_dddddddd'),
])
def test_job_folder_name_is_readable_windows_safe_and_unique(
        title, job_id, expected):
    from core import job_folder_name
    assert job_folder_name(title, job_id) == expected


def test_job_folder_name_reserves_suffix_inside_windows_length_limit():
    from core import job_folder_name
    result = job_folder_name('😀' * 100, '12345678' + 'a' * 24)
    assert result.endswith('_12345678')
    assert len(result.encode('utf-16-le')) // 2 <= 80


def test_job_folder_name_rejects_invalid_full_job_id():
    from core import job_folder_name
    with pytest.raises(ValueError, match='任务编号'):
        job_folder_name('Lecture', '../outside')


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=BaW_jenozKc&list=abc',
    'https://youtu.be/BaW_jenozKc?t=10',
    'https://www.youtube.com/shorts/BaW_jenozKc',
])
def test_normalizes_supported_video_links(url):
    assert canonical_youtube_url(url) == 'https://www.youtube.com/watch?v=BaW_jenozKc'


@pytest.mark.parametrize('url', [
    'https://youtube.com.evil.test/watch?v=BaW_jenozKc',
    'file:///etc/passwd', 'https://youtube.com/playlist?list=abc',
    'https://youtube.com/watch?v=x', 'https://user:pass@youtube.com/watch?v=BaW_jenozKc',
])
def test_rejects_invalid_or_untrusted_links(url):
    with pytest.raises(ValueError):
        canonical_youtube_url(url)


def test_caption_parser_keeps_unicode_and_decodes_entities():
    data = {'events': [{'tStartMs': 1250, 'dDurationMs': 2500,
                        'segs': [{'utf8': '你好 &amp; '}, {'utf8': 'world'}]}]}
    assert parse_json3(data) == [{'start': 1.25, 'end': 3.75, 'text': '你好 & world'}]


def test_rolling_captions_remove_overlapping_prefix_only():
    data = {'events': [
        {'tStartMs': 0, 'dDurationMs': 2000, 'segs': [{'utf8': 'Hello world'}]},
        {'tStartMs': 1000, 'dDurationMs': 2000, 'segs': [{'utf8': 'Hello world again'}]},
        {'tStartMs': 5000, 'dDurationMs': 1000, 'segs': [{'utf8': 'Hello world'}]},
    ]}
    assert [s['text'] for s in parse_json3(data)] == ['Hello world', 'again', 'Hello world']


def test_empty_caption_is_rejected():
    with pytest.raises(ValueError):
        parse_json3({'events': [{'segs': [{'utf8': '\n'}]}]})


def test_exports_preserve_words_and_srt_milliseconds():
    segments = [{'start': 1.25, 'end': 3.75, 'text': '你好 world.'},
                {'start': 70, 'end': 72.005, 'text': 'Next passage.'}]
    assert plain_text(segments) == '你好 world.\n\nNext passage.'
    assert to_srt(segments) == ('1\n00:00:01,250 --> 00:00:03,750\n你好 world.\n\n'
                                '2\n00:01:10,000 --> 00:01:12,005\nNext passage.\n')


def test_repairs_legacy_mojibake_without_changing_valid_chinese():
    assert readable_legacy_text('æœ¬åœ° Whisper small / CPU int8') == (
        '本地 Whisper small / CPU int8'
    )
    assert readable_legacy_text('本地 Whisper small / CPU int8') == (
        '本地 Whisper small / CPU int8'
    )
