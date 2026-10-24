# VideoScript — 本地视频转文字

个人学习用途的本地原型：提取 YouTube 已有字幕，或使用本地 Whisper 模型把视频 / 音频转成可编辑的文字稿，支持 TXT 和原始 SRT 导出。

## 从 GitHub 安装

### PowerShell 自动安装（Windows x64）

不需要预先安装 Python、Node.js 或 Git。打开普通 PowerShell，切换到你希望存放项目的文件夹，复制执行以下整段。它会建立一个新的 `VideoScript` 文件夹；如果已存在则停止，避免覆盖现有项目。

```powershell
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$installRoot = Join-Path (Get-Location) 'VideoScript'
if (Test-Path -LiteralPath $installRoot) { throw 'VideoScript already exists. Open that folder and run Install.ps1 instead.' }
$downloadId = [guid]::NewGuid().ToString('N')
$archive = Join-Path ([IO.Path]::GetTempPath()) "VideoScript-$downloadId.zip"
$unpack = Join-Path ([IO.Path]::GetTempPath()) "VideoScript-$downloadId"
Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/poloveoh/VideoScript/archive/refs/heads/main.zip' -OutFile $archive
Expand-Archive -LiteralPath $archive -DestinationPath $unpack
Copy-Item -LiteralPath (Join-Path $unpack 'VideoScript-main') -Destination $installRoot -Recurse
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $installRoot 'Install.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Installation did not finish. Check the error above before retrying.' }
```

已有完整项目时，在项目目录执行下面这一行即可，不必重新下载项目：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1
```

安装器下载 uv、便携版 Node.js LTS、Python 3.12、锁定的 Python 依赖（包括 Streamlit、yt-dlp/EJS、faster-whisper 和 FFmpeg），检查依赖，然后下载 Small 多语言模型并启动界面。运行环境保存在项目的 `.tools/` 和 `.venv/`，模型保存在 `data/models/`；不修改系统 PATH，不注册系统 Python，不要求 NVIDIA / CUDA，也不需要付费转录 API。

### 文件夹搬迁后，一行修复

先停止工具，再移动或改名整个项目文件夹，保留 `.tools/`、`data/` 和其他项目文件。在新位置的文件夹地址栏输入 `powershell` 并按 Enter，然后执行相同的一行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1
```

新版安装器会实际运行 Python，检查版本、当前项目路径和基础解释器路径。若 `.venv` 失效或指向旧位置，会将其改名为 `.venv.backup-<随机编号>`，然后重建环境、重装依赖并启动工具；健康环境会直接复用。失败后可以再次运行同一行，继续准备依赖。

模型和文字稿位于 `data/`，修复不会删除它们，也不会重新下载已经完整缓存的同一个模型。之前输入的本地媒体路径不会自动改写，外部文件搬迁后可能需要重新选择。旧环境备份会占用磁盘，确认工具正常后可以自行清理 `.venv.backup-*`，不要误删 `data/` 或 `.tools/`。

修复仍可能需要联网；网络、磁盘空间、Windows 文件权限或运行中的进程可能使它失败。若工具还在运行，先双击 `Stop.cmd` 再修复。该功能只存在于新版 `Install.ps1`；以前下载的 ZIP 不会自动更新，请先获取新版脚本（或下载新版 ZIP，保留自己的 `data/`）。

下载源为 Astral 官方 GitHub、nodejs.org、Python 包仓库及 Hugging Face 模型仓库。Node.js ZIP 会校验官方 SHA256。首次安装需要网络、足够磁盘空间和执行权限；学校 / 公司策略、网络限制或缺失的系统运行库可能使安装失败，脚本会报错停止。没有在所有 Windows 电脑上验证过。搬迁时请保留整个项目，并按上述步骤修复环境。

可选参数：`-SkipModel` 暂不下载模型，`-NoLaunch` 安装后不打开工具。默认只准备 Small，不会预先下载 Base、Medium 或 Large。当前界面最大选项是 Medium，首次使用才会下载；Large 不在当前界面中。

### 交给 ChatGPT Work / Codex 的安装指令

下面是可以复制到 ChatGPT Work 或 Codex 的任务说明，不是 PowerShell 代码。它们需要能访问你的电脑、执行本机命令、联网和写入选定文件夹。纯云端环境的安装不会装进你的电脑；仅有网页操作能力也不等于有本机命令执行权限。ChatGPT Work 的本机访问要求见 [官方说明](https://learn.chatgpt.com/docs/get-started-with-work)。

```text
请帮我在这台 Windows x64 电脑安装并启动 VideoScript：
https://github.com/poloveoh/VideoScript

先确认你是否拥有本机命令执行、文件写入和联网能力；若没有，请明确告诉我缺少哪项能力，并提供 README 中的 PowerShell 安装方法，不要把云端安装说成本机安装。

读取仓库 README.md 和 Install.ps1，在我选定的目录下载或复用完整项目。不要覆盖已有项目、data 中的个人文件或其他 Python 环境，也不要修改系统执行策略、获取浏览器 Cookie 或发布网站。

在项目目录运行：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1

让安装器准备 uv、便携 Node.js LTS、Python 3.12、全部锁定依赖、FFmpeg 和 Small 多语言模型。默认不要下载 Medium 或 Large。所需系统授权交由我处理。

检查依赖和模型加载是否成功，再确认 http://127.0.0.1:8501/_stcore/health 返回 200 / ok。若无法访问本机服务，要说明未验证，不能只凭安装命令结束就声称界面可用。

最后告诉我项目完整位置、界面地址，以及 Start.cmd / Stop.cmd 的使用方法。若安装失败，请报告失败步骤和具体错误，不要反复重试或关闭安全校验。
```

### 手动安装

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

**限流不是固定时段。** 429 可能与一段时间内的请求频率、出口 IP / 会话及所请求的字幕接口有关；部分自动翻译字幕也有已知 429 问题。暂停请求后可能恢复，但没有保证的等待分钟数或「晚上一定能用」规则。不要连续点击重试；可优先选原语言字幕，或使用已有本地音视频。当前工具没有实现自动限流冷却或重试队列。参考 [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ) 和 [字幕 429 问题记录](https://github.com/yt-dlp/yt-dlp/issues/13831)。

`WinError 10013` 则表示连接被本机权限 / 安全策略阻止，不等同于 YouTube 429；应检查运行环境的联网许可，不能只靠等待。通常建议从 Windows 双击 `Start.cmd` 启动，避免继承受限代理环境。

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
