# VideoScript — 本地视频转文字

个人学习用途的本地原型：提取 YouTube 已有字幕，或使用本地 Whisper 模型把视频 / 音频转成可编辑的文字稿，支持 TXT 和原始 SRT 导出。

## 从 GitHub 安装

需要 Windows、Python 3.12 和 Node.js。克隆或下载源码后，在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

安装完成后双击 `Start.cmd`。依赖包含 FFmpeg，无需单独安装。首次使用每种识别模型需要联网下载；GitHub 仓库不包含模型、个人文字稿、媒体文件或虚拟环境。

项目通过 AI 辅助开发，目前是个人原型，不保证所有 YouTube 链接都能成功获取。暂不支持直接读取 Google Classroom / Google Drive 链接。

## 开始使用

1. 双击同目录的 **Start.cmd**。工具准备好后会自动打开浏览器，服务在后台运行。
2. 浏览器访问 **http://127.0.0.1:8501**。
3. 在「YouTube 链接」粘贴链接，点击「检查视频与字幕」，选择字幕后生成文字稿。
4. 没有字幕或字幕质量不好时，手动切换「本地识别音频」。也可以上传本地视频 / 音频，或输入大文件的完整路径。
5. 阅读并修改，点击「保存修改」，或者下载 TXT / 原始 SRT。

默认 Small 多语言模型；首次使用一个模型会联网下载。Tiny 适合先测试流程，Small 是质量与速度的起点，Medium 更慢。模型运行在 CPU 上，不需要 NVIDIA 显卡或付费 API。专业术语输入框只是识别提示，不能保证准确。请核对重要文字。

若启动后浏览器没有自动打开，手动访问上述地址即可。已有安装可能缓存了模型；新安装需要下载。

双击 **Stop.cmd** 可停止服务。关闭浏览器不会主动停止后台服务。重新打开后，从左侧历史记录选择任务并点击「打开记录」。未保存的编辑不会保留，关闭页面也不保证转录任务继续。

## 本地保存

- `data/jobs/<任务编号>/job.json`：任务信息、分段和保存的编辑。
- `transcript.txt`：文字稿；`original.srt`：原始识别字幕。
- `captions.json3` / `audio.*` / `input.*`：获取的字幕或媒体。
- `data/models/`：下载的语音模型。

识别过程中逐段保存。中断时能取回已保存内容，但没有自动断点续跑功能。文件不会自动删除；关闭应用后可手动清理不再需要的任务目录。使用本地路径时原文件不被修改或删除。

TXT 使用当前编辑文字；SRT 使用原始分段文字，两者明确分开。工具只调整字幕换行和阅读段落，不自动改写、翻译、总结或推测缺失内容。

## 网络与限制

字幕提取与 YouTube 音频下载需要联网，首次模型下载也需要联网。已有模型处理本地文件时不需要网络。语音不会发送给云端转录服务；应用仅监听本机地址，未发布网站。

YouTube 可能限制请求，出现 403、429、登录或验证要求。工具不读取浏览器 Cookie，不自动绕过限制，也不保证所有视频都可下载。失败时可稍后重试，或使用你已有的本地文件。仅支持单个视频，不支持直播中的视频或整份播放列表。字幕失败后不会自动启动耗时的音频识别。

## 环境与维护

此工作目录已经准备本地 Python 虚拟环境，依赖版本见 `requirements.lock.txt`。它引用同一工作目录下 `../../work/runtime/python/` 的 Python，请勿单独移动这个文件夹后仍期待原虚拟环境可用。

上述路径只适用于最初的本机安装；从 GitHub 安装时，使用本页开头的步骤重新创建虚拟环境，不依赖这个路径。

如果复制到其他电脑，安装 Python 3.12 和 Node.js 后，在此目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

FFmpeg 由 imageio-ffmpeg 提供，不必单独安装。YouTube 的 JavaScript 检查使用现有 Node.js；yt-dlp 默认依赖包含 EJS 组件。

若 YouTube 行为变化，可能需要更新 yt-dlp。更新属于联网维护，不在每次启动时自动执行：

```powershell
.venv\Scripts\python.exe -m pip install --upgrade "yt-dlp[default]"
```

当前虚拟环境由 uv 创建，没有预装 pip；此电脑可使用 `../../work/runtime/uv/uv.exe pip install --python .venv/Scripts/python.exe --upgrade "yt-dlp[default]"` 更新。

## 检查

```powershell
.venv\Scripts\python.exe -m pytest -q
```

测试依赖 pytest。首次在其他电脑安装测试工具：`python -m pip install pytest`（使用对应虚拟环境的 Python）。

本工具不包含摘要、知识库、云同步或订阅计费。
