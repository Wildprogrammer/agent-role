# VoxCPM 运行时与模型核验

VoxCPM 是会议记录工作流的可选本地文字转语音和声音克隆分支；它不影响转写、摘要和 Obsidian 主链路。

## 运行时边界

- Agent 可管理 Windows x64 / Python 3.12 的 VoxCPM 运行时，唯一安装输入是 `voxcpm-windows-py312.lock`。
- 先对用户指定的全局 Python 做 `uv pip install --dry-run --require-hashes`。只有输出为零卸载、零版本替换时，才可以写入该全局环境。
- 有冲突时，使用 `<HUB_ROOT>\workspace\shared\runtimes\voxcpm-py312` 专用运行时；不得通过系统包管理器、裸 `pip` 或 Git 安装。已有 `%LOCALAPPDATA%\agent-workflow-hub\runtimes\voxcpm-py312` legacy location 可只读检测并继续使用，但不得自动迁移或删除。
- 运行时锁定 `voxcpm==2.0.3`、CUDA 12.1 的 `torch==2.5.1+cu121` 与 `torchaudio==2.5.1+cu121`，并包含全部传递依赖的 SHA-256。
- 由于 PyTorch CUDA 索引与 PyPI 的包覆盖需要合并，安装命令必须显式使用 `--index-strategy unsafe-best-match`；该例外只允许这两个官方索引，所有工件仍受 SHA-256 锁校验。
- 安装前必须报告目标解释器、锁定版本与哈希、下载源和预计磁盘占用。下载源仅限 PyTorch CUDA 12.1 索引与 PyPI。

## 模型边界

模型权重仍由用户管理。运行时安装不得下载、替换或解析模型：

- 使用一个已存在的官方 `OpenBMB/VoxCPM2` 快照目录。
- 每次调用显式传入不可变 revision 和本地 SHA-256 manifest。
- 不使用 `main`、`latest` 或任何可变别名。
- 不启动 `app.py`、Gradio、FastAPI、sLLM、Nano-vLLM 或 HTTP 服务。

## 启用前检查

```powershell
python workflows/meeting-notes/scripts/meeting_notes.py doctor --with-voice `
  --voxcpm-python "C:\path\to\voxcpm\Scripts\python.exe" `
  --model-path "D:\models\VoxCPM2" `
  --model-revision "<immutable-revision>" `
  --model-manifest "D:\models\VoxCPM2.manifest.json"
```

`doctor` 只验证解释器、VoxCPM 分发版本、CUDA 可用性、模型目录和 manifest；不下载权重、不生成音频、不启动服务。通过后，`speak` 才能调用一次性本地 worker，将 WAV 仅写入 `workflows/meeting-notes/outputs/<run-id>/`。

## 声音克隆的额外条件

普通文字转语音不使用参考音频。声音克隆必须另行提供参考音频和本次有效的 JSON 授权记录，至少包括 `voice_owner`、`permitted_purpose`、`audience`、`text_scope`、`valid_from`、`valid_until` 和 `reference_audio_authorized: true`。Windows 下推荐把 UTF-8 JSON 文件的绝对路径传给 `--clone-consent-file`；`--clone-consent` 仅保留为内联兼容方式。

参考音频推荐为 3–15 秒、单一说话人、清晰且无重叠人声或背景音乐的片段。较长或多人来源必须先由用户确认准确时间段，再生成 16 kHz 单声道派生参考文件，并同时记录原始来源和派生文件的 SHA-256。不得从会议录音或历史输出中自动提取声音来源。
