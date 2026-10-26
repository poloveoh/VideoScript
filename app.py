from pathlib import Path

import streamlit as st

from core import (canonical_youtube_url, output_filename_stem, plain_text, readable_legacy_text,
                  timestamp, to_srt)
from pipeline import (DATA, JobStore, extract_captions, inspect_youtube,
                      subtitle_choices)
import worker

st.set_page_config(page_title='VideoScript · 视频转文字', page_icon='✍️', layout='wide')
st.markdown('''<style>
.stApp {background: #f6f8f7;}
.block-container {max-width: 1180px; padding-top: 2.7rem;}
h1,h2,h3 {letter-spacing: -.04em;}
[data-testid="stSidebar"] {background: #edf2ef;}
[data-testid="stTextArea"] textarea {font-size: 17px; line-height: 1.9;}
.eyebrow {color:#24785d; font-size:12px; font-weight:700; letter-spacing:2px;}
.intro {color:#607068; font-size:17px; margin-bottom:24px;}
.empty {padding:48px 24px; text-align:center; border:1px dashed #b8cbbf;
border-radius:14px; color:#64776b; background:#f0f5f1; margin-top:20px;}
</style>''', unsafe_allow_html=True)

store = JobStore()
with st.sidebar:
    st.markdown('### ✍️ VideoScript')
    st.caption('你的本地视频文字工作台')
    st.divider()
    st.markdown('**处理记录**')
    jobs = store.list()
    labels = {j['id']: f"{j['title'][:32]} · {'完成' if j['status'] == 'complete' else '部分 / 未完成'}" for j in jobs}
    selected = st.selectbox('打开已保存的文字稿', [''] + list(labels),
                            format_func=lambda value: labels.get(value, '选择一条记录…'), key='history')
    if st.button('打开记录', disabled=not selected, width='stretch'):
        st.session_state.active_job = selected
    st.divider()
    st.caption('本地 CPU 识别 · 无转录订阅费')
    st.caption('音视频和文字稿保存在本机。YouTube 与首次模型下载需要网络。')
    with st.expander('文件保存在哪里？'):
        st.code(str(DATA), language=None)
        st.caption('文字稿与 SRT 会永久保留；任务内的临时媒体在成功后可自动清理。外部原始文件不会被删除。')

st.markdown('<div class="eyebrow">WATCH LESS. READ MORE.</div>', unsafe_allow_html=True)
st.title('VideoScript')
st.markdown('<div class="intro">把视频变成可以阅读、修改和带走的文字。</div>', unsafe_allow_html=True)

source = st.radio('视频来源', ['YouTube 链接', '本地视频 / 音频'], horizontal=True, key='source')
media = None
info = None
if source == 'YouTube 链接':
    with st.form('youtube_form'):
        st.text_input('YouTube 视频链接', placeholder='https://www.youtube.com/watch?v=…', key='youtube_url')
        inspect = st.form_submit_button('检查视频与字幕', key='inspect', type='primary')
    if inspect:
        st.session_state.pop('video_info', None)
        try:
            url = canonical_youtube_url(st.session_state.youtube_url)
            with st.spinner('正在读取视频信息与字幕列表…'):
                st.session_state.video_info = inspect_youtube(url)
                st.session_state.video_url = url
        except Exception as exc:
            st.error(f'无法读取：{exc}')
            st.info('YouTube 有时会限制请求。可以稍后重试，或使用本地视频 / 音频文件。工具不会读取浏览器登录信息。')
    info = st.session_state.get('video_info')
    if info:
        st.subheader(info.get('title', 'YouTube 视频'))
        duration = info.get('duration')
        st.caption(f"{info.get('uploader', '')} · {timestamp(duration or 0)}")
        mode = st.radio('获取文字的方式', ['提取现有字幕（最快）', '本地识别音频（较慢）'], horizontal=True)
        if mode.startswith('提取'):
            choices = subtitle_choices(info)
            if choices:
                choice = st.selectbox('选择字幕语言', choices,
                                      format_func=lambda c: f"{c[1]} · {'创作者提供' if c[0] == 'manual' else '自动字幕'}")
                st.caption('文字内容来自所选字幕。分段只调整阅读格式，不会自动修正识别错误。')
                if st.button('生成文字稿', type='primary', key='get_captions'):
                    job = store.create(info.get('title', 'YouTube'), 'youtube', st.session_state.video_url)
                    st.session_state.active_job = job['id']
                    try:
                        with st.spinner('正在获取字幕并生成文字稿…'):
                            extract_captions(info, choice, store, job)
                        st.rerun()
                    except Exception as exc:
                        st.error(f'字幕获取失败：{exc}')
                        st.info('没有自动开始音频识别。你可以手动选择「本地识别音频」，或上传已有文件。')
            else:
                st.warning('未找到可用字幕。你可以选择「本地识别音频」，下载音频后重新识别。')
        else:
            media = 'youtube'
else:
    file = st.file_uploader('选择视频或音频文件（最大 1 GB）',
                            type=['mp4', 'mkv', 'mov', 'avi', 'webm', 'm4v', 'mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg'])
    if file:
        media = file
    with st.expander('文件太大？直接使用电脑里的文件路径'):
        local_path = st.text_input('完整文件路径', placeholder=r'C:\Videos\lecture.mp4')
        st.caption('直接读取文件，避免把大视频加载到浏览器内存。')
        if local_path.strip():
            candidate = Path(local_path.strip().strip('"'))
            if candidate.is_file() and candidate.suffix.lower() in {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'}:
                media = candidate
            else:
                st.warning('文件不存在，或扩展名不受支持。')

if media is not None or source == '本地视频 / 音频':
    st.info('本地识别使用你的 CPU。首次使用每个模型需要联网下载；长视频可能需要较长时间。识别过程中会保存已完成的文字。')
    left, right = st.columns(2)
    with left:
        model = st.selectbox('识别模型', ['tiny', 'base', 'small', 'medium'], index=2,
                             format_func=lambda name: {'tiny': 'Tiny · 快速试跑', 'base': 'Base · 较快',
                                                       'small': 'Small · 建议起点', 'medium': 'Medium · 更大、更慢'}[name])
    with right:
        languages = {'自动检测': None, '英语': 'en', '中文': 'zh', '马来语': 'ms',
                     '日语': 'ja', '韩语': 'ko', '西班牙语': 'es', '法语': 'fr', '德语': 'de'}
        language = st.selectbox('主要语音语言', list(languages))
    glossary = st.text_input('专业词汇 / 人名（可选）', placeholder='例如：photosynthesis, mitochondria, ATP')
    st.caption('保留原语言，不翻译。混合语言请先尝试自动检测；识别质量仍需人工检查。')
    auto_cleanup_media = st.checkbox(
        '成功转录后自动删除临时媒体', value=True,
        key='auto_cleanup_media',
        help='只删除任务目录中的上传副本或 YouTube 下载音频；不会删除通过完整路径读取的原始文件。',
    )
    if st.button('开始本地转录', type='primary', disabled=media is None):
        title = info.get('title', 'YouTube 视频') if isinstance(media, str) else media.name
        job = store.create(title, 'youtube' if isinstance(media, str) else 'local',
                           st.session_state.video_url if isinstance(media, str) else '')
        st.session_state.active_job = job['id']
        try:
            if isinstance(media, str):
                path = None
            elif isinstance(media, Path):
                path = media
            else:
                path = store.directory(job['id']) / ('input' + Path(media.name).suffix.lower())
                with path.open('wb') as target:
                    for offset in range(0, media.size, 1024 * 1024):
                        target.write(media.getbuffer()[offset:offset + 1024 * 1024])
            job.update(
                media_path=str(path) if path else '',
                model=model,
                language=languages[language],
                glossary=glossary,
                auto_cleanup_media=auto_cleanup_media,
                media_managed=not isinstance(media, Path),
                progress=0.0,
                message='任务已提交，正在启动…',
            )
            store.save(job)
            worker.launch_worker(job['id'], store)
            st.rerun()
        except Exception as exc:
            job.update(status='failed', error=str(exc))
            store.save(job)
            st.error(f'转录未完成：{exc}')
            st.info('已识别的文字保留在当前任务内。模型下载失败时请检查网络；YouTube 下载失败时可改用本地文件。')

st.divider()
active = st.session_state.get('active_job')
if not active:
    st.markdown('<div class="empty"><strong>你的下一份文字稿，从这里开始。</strong><br><br>粘贴视频链接，或选择一个本地文件。完成后可以编辑、复制和导出。</div>', unsafe_allow_html=True)
else:
    try:
        job = store.load(active)
    except (OSError, ValueError):
        st.error('无法读取保存的任务。请从左侧选择其他记录。')
        st.stop()
    st.subheader(job['title'])
    st.caption(f"{readable_legacy_text(job.get('method', '未完成'))} · {job.get('language', '语言待检测')} · {len(job['segments'])} 个文字片段")

    @st.fragment(run_every=2)
    def job_controls(job_id):
        current = store.load(job_id)
        status = current.get('status', 'failed')
        active_worker = worker.process_is_running(current.get('worker_pid'))
        active_status = status in {'queued', 'running', 'stopping'}
        if active_status and not active_worker:
            current = store.update_fields(
                job_id, status='paused', worker_pid=None,
                message='后台转录进程已退出，可以从已保存位置继续。',
            )
            status = 'paused'
            active_status = False
        names = {
            'queued': '排队中', 'running': '转录中', 'stopping': '停止中',
            'paused': '已暂停', 'complete': '已完成', 'failed': '失败',
        }
        progress = max(0.0, min(1.0, float(current.get('progress', 0))))
        st.progress(progress, text=current.get('message') or names.get(status, status))
        st.caption(
            f"状态：{names.get(status, status)} · "
            f"已保存 {len(current.get('segments', []))} 个文字片段"
        )
        state_key = f"worker_status_{job_id}"
        previous = st.session_state.get(state_key)
        st.session_state[state_key] = status
        if (
            previous in {'queued', 'running', 'stopping'}
            and status in {'paused', 'complete', 'failed'}
        ):
            st.rerun()
        if status in {'queued', 'running'} and active_worker:
            if st.button('停止转录', key='stop_job', type='secondary'):
                with st.spinner('正在安全停止，最多等待 5 秒…'):
                    worker.stop_worker(job_id, store, timeout=5)
                st.rerun()
        elif status in {'paused', 'failed'}:
            resumable = (
                current.get('source') == 'youtube'
                or Path(current.get('media_path', '')).is_file()
            )
            if st.button('继续转录', key='resume_job', disabled=not resumable):
                worker.launch_worker(job_id, store)
                st.success('任务已重新启动。')
                st.rerun()
        elif status == 'complete' and current.get('cleanup_status') == 'failed':
            st.warning(f"临时媒体清理失败：{current.get('cleanup_error', '未知错误')}")
            if st.button('重试清理临时媒体', key='retry_cleanup'):
                cleaned = store.cleanup_media(job_id)
                if cleaned.get('cleanup_status') == 'complete':
                    st.success('临时媒体已清理。')
                st.rerun()

    job_controls(job['id'])
    if job['status'] != 'complete':
        st.warning('这是一份未完成的文字稿。已保存部分可以导出，也可以停止后继续转录。')
        if job.get('error'):
            with st.expander('查看失败原因'):
                st.write(job['error'])
    if job['segments']:
        download_stem = output_filename_stem(job['title'])
        tab_read, tab_time = st.tabs(['阅读与编辑', '时间戳与原始字幕'])
        with tab_read:
            edited = st.text_area('文字稿', value=job.get('edited_text') if job.get('edited_text') is not None else plain_text(job['segments']),
                                  height=420, key=f"edit_{job['id']}")
            a, b, c = st.columns(3)
            with a:
                if st.button('保存修改', width='stretch'):
                    store.save_edits(job['id'], edited)
                    st.success('修改已保存到本机。')
            with b:
                st.download_button('下载文字稿 .txt', edited, file_name=download_stem + '.txt', mime='text/plain; charset=utf-8', width='stretch')
            with c:
                st.download_button('下载原始字幕 .srt', to_srt(job['segments']), file_name=download_stem + '.srt', mime='application/x-subrip', width='stretch')
            st.caption('TXT 包含当前编辑内容。SRT 保留原始识别文字与时间戳，不包含阅读区的修改。')
        with tab_time:
            st.dataframe([{'开始': timestamp(s['start']), '结束': timestamp(s['end']), '文字': s['text']}
                          for s in job['segments']], hide_index=True, width='stretch', height=350)
            if job.get('url'):
                st.link_button('在 YouTube 打开原视频', job['url'])
            elif job.get('media_path') and Path(job['media_path']).is_file():
                if st.checkbox('加载本地播放器（大文件会增加内存占用）'):
                    path = Path(job['media_path'])
                    if path.suffix.lower() in {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'}:
                        st.audio(str(path))
                    else:
                        st.video(str(path))
