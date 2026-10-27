# 验证记录

## 2026-10-28：Playlist MVP 集成

- 完整自动测试：245 项通过、1 项跳过（本机目录符号链接不可用），1 项现有第三方 pkg_resources 弃用警告。专项 UI / lifecycle：25 项通过。
- 临时 SQLite / JobStore 的 Streamlit AppTest 覆盖 URL 校验、添加、重复重新启用、停用 / 启用、确认软移除、Sync Now 标记、错误 / 数量 / 最近发现与只读 FIFO。网络获取注入在 PlaylistService 下方，没有访问真实 YouTube。
- PowerShell 行为测试覆盖完整进程身份、陈旧 / 复用 PID、同一 Python 的其他命令、重复 Start 只启动一个隐藏 server / tray、Stop 不结束真实无关 Python；托盘测试覆盖安全退出、启动失败和运行时原生故障均保留诊断网页。
- Windows 虚拟环境 python.exe 会启动实际解释器子进程。验证 PID 元数据保存实际子进程，避免只结束启动器而遗留 Streamlit。
- 实际隔离 smoke：端口 18519，Streamlit 健康检查 HTTP 200 / ok；启动器 PID 17188，验证后的实际 server PID 33048；停止后两者均不存在。
- 实际 Stop.ps1 隔离复验：复制当前脚本至临时目录，端口 18519，启动器 PID 22568、验证后的 server PID 528；health HTTP 200 / ok，执行 Stop.ps1 -ServerOnly 后两者均不存在，server.pid 已移除。原生创建时间保留 UTC ticks 精度，用同秒不同 ticks 的 PID 复用回归测试验证拒绝。
- 正式 8501 smoke 受限：已有其他安装的服务占用 127.0.0.1:8501（PID 14744），健康 HTTP 200；本次 Start.ps1 正确拒绝接管，没有新增本分支进程，没有停止该服务。共享测试虚拟环境缺少 pystray，因此未验证真实通知区域图标、真实双进程 Start / Stop 或托盘点击；受控生命周期测试不能替代这一原生 smoke。
- 复验：补齐本项目锁定依赖，确认 8501 空闲后运行 Start.cmd；检查 health、两个 JSON 元数据与命令 / 创建时间；重复 Start 验证 PID 不变；用托盘 Exit 或 Stop.cmd 验证两进程退出；再启动确认 videoscript.db 与文字稿仍在。测试命令：`.venv\Scripts\python.exe -m pytest -q`。

## 2026-10-27：影片标题文件名 QA

- 完整自动测试：69 项通过，覆盖标题命名、非法字符与 Windows 保留名称、同名任务隔离、编辑保存、下载文件名、旧任务兼容、自动清理及转录恢复。
- Windows 实际文件写入：中文标题、`CONIN$`、`CONOUT$`、带空格的设备名称和超长 emoji 标题，共 6 种标题均成功保存 TXT / SRT。
- 独立代码审查：修复 Windows 保留名称边界后复查，无阻塞问题。
- 未重新运行真实 YouTube 下载或 Whisper 推理；本次改动仅涉及文字稿 / 字幕文件命名。测试仍有一项第三方 `pkg_resources` 弃用警告。

日期：2026-10-24。环境：本机 Windows / Intel i7-12700K / Python 3.12.15 / CPU int8。

- 自动测试：24 项通过。涵盖链接校验、字幕转换、滚动字幕去重、Unicode / SRT、音频解码、部分结果保留、完整历史、并发保存编辑、界面初始化与错误提示。
- 真实 YouTube：公开短片 `jNQXAC9IVRw` 的信息读取、英文字幕提取、音轨下载成功。
- 真实本地识别：下载的约 19 秒音轨，用 Tiny 与 Small 多语言模型均成功输出文字并保存。
- Small 短片试跑：约 3.95 秒（含该次模型加载）；这不是两小时课程的速度基准，不能直接保证长视频速度或准确度。
- 浏览器端：Edge 无头浏览器验证了 YouTube 链接输入、错误提示、字幕生成、编辑保存、TXT 实际下载、本地音频上传、Small 模型转录与本地播放器显示。
- 启动后健康检查：`http://127.0.0.1:8501/_stcore/health` 返回 HTTP 200 / ok。
- 依赖检查通过；锁定 PyAV 16.1.0，避免 PyAV 19 移除 metadata_errors 参数导致解码失败。

未验证：两小时长视频、所有字幕语言、所有视频编码、受限 / 登录视频、整机重启后的后台自动运行（工具无自动开机功能）。转录结果仍需核对；字幕或音轨获取是否成功受 YouTube 当时的限制影响。

模型：Tiny 与 Small 已缓存。Base 与 Medium 首次选择时仍需要联网下载。
