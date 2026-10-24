from pathlib import Path
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / 'app.py'


def test_interface_opens_and_rejects_invalid_link_without_network():
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert any('VideoScript' in title.value for title in app.title)
    app.text_input(key='youtube_url').set_value('https://example.com/video')
    app.button(key='inspect').click().run()
    assert not app.exception
    assert any('YouTube' in error.value for error in app.error)


def test_local_mode_explains_first_model_download():
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.radio(key='source').set_value('本地视频 / 音频').run()
    assert not app.exception
    assert any('首次' in item.value for item in app.info)
