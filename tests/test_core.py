import pytest

from core import (canonical_youtube_url, parse_json3, plain_text,
                  readable_legacy_text, to_srt)


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
