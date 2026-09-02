---
name: meeting-notes
description: 将已获准的本地会议音视频转写、经人工审核后由当前 Agent 生成摘要，并在用户批准后写入 Obsidian；可选本地 VoxCPM 文字转语音与声音克隆。
compatibility: Agent Workflow Hub spec 1.0；核心路径需要 FFmpeg、FunASR 和 Obsidian，语音分支额外需要用户自管的 VoxCPM 本地环境。
metadata:
  spec-version: "1.0"
  workflow-version: "0.2.1"
  display-name: "Meeting Notes"
  execution-modes: '["single-agent","multi-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "explicit"
  multi-agent-write-policy: "separate-output-main-integrates"
  approval-owner: "main-agent"
  required-capabilities: '["cli.ffmpeg","model.funasr","app.obsidian"]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"doctor":"python <HUB_ROOT>/workflows/meeting-notes/scripts/meeting_notes.py doctor --hub-root <HUB_ROOT>"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---
# Meeting Notes

> 下文的 `meeting-notes` 是 `python workflows/meeting-notes/scripts/meeting_notes.py` 的简写；实际使用时以该 Python 命令替换前缀。所有路径都必须是绝对路径。

## 用途与触发条件

用于已获得参与者知情同意的会议音频/视频：先在本地转写，用户审核和脱敏，再由当前 Agent 根据审核后的转写文本生成 Markdown 摘要，最后仅在用户批准后写入指定 Obsidian Vault。

可选语音分支用于把用户直接提供的文字，或用户明确选择的 Vault 笔记片段，生成本地 WAV。它不影响会议记录主链路，也不属于必需能力。

## 非目标

- 不提供实时字幕、说话人分离、说话人身份识别；不得推断负责人或截止日期。
- 不会静默上传音视频、转写、笔记、参考音频或模型数据到云端。
- 不会下载 FunASR/VoxCPM 模型权重，也不会启动 `app.py`、Gradio、FastAPI 或局域网服务。FunASR 与 VoxCPM 运行时仅可按各自的 Windows/Python 3.12 哈希锁受控安装；模型权重仍不自动下载。
- 不会主动上传原始会议录音文件；本地语音分支也不会启动网络服务。

## 输入

核心转写输入为一份已获准的本地音频/视频、语言、唯一 run ID、用户管理的 FFmpeg/FunASR Python、模型目录及模型 manifest。首次录音前还必须明确设备、时长和保留策略；普通文件导入不得改写源文件。

写入 Obsidian 时，用户还要明确 Vault 根目录、目标相对路径，以及 `new`、`append` 或 `overwrite` 模式。`overwrite` 需要额外的明确批准。

语音输入二选一：直接文字，或 Vault 内指定 Markdown 文件。后者默认读取 `## 摘要`，用户可以明确给出其他章节名。声音克隆还需一个可读取的参考音频文件、固定模型 revision 和本地模型 manifest。

## 输出与命名规则

私有、可恢复的运行状态保存在 `workspace/workflows/meeting-notes/runs/<run-id>/`；它包含原始输入路径、SHA-256、处理状态、审核后的转写和摘要草稿。

已批准的工作流输出保存在 `workflows/meeting-notes/outputs/<run-id>/`：审核通过的 `transcript.md`、批准后的 `meeting-notes.md`，以及可选的 `speech.wav` 和 `voice-request.json`。生成的音频不会自动写回或链接到 Obsidian。

## 依赖和运行前检查

先做只读检查；下面示例中的路径由使用者提供：

```powershell
python workflows/meeting-notes/scripts/meeting_notes.py doctor --hub-root "C:\Hub" `
  --ffmpeg "C:\tools\ffmpeg.exe" `
  --funasr-python "C:\envs\funasr\Scripts\python.exe" `
  --funasr-model "D:\models\paraformer" `
  --funasr-model-manifest "D:\models\paraformer.manifest.json"
```

只有本地 FFmpeg、FunASR Python 导入、模型目录和 manifest 都通过后，才可执行 `meeting-notes transcribe`。doctor 不安装包、不下载模型、不生成音频、不启动服务。
当 doctor 仅报告 FunASR 运行时缺失时，Agent 可在先报告专用 Python 3.12 运行时、锁定版本与哈希、两个下载源和预估磁盘占用后，用 `uv venv --python 3.12` 创建 `<HUB_ROOT>\workspace\shared\runtimes\funasr-py312`，再使用 `uv pip install --require-hashes -r workflows/meeting-notes/references/funasr-windows-py312.lock` 安装；不得向已有通用 Python 写入、不得用裸 `pip`、系统包管理器或 Git。模型目录和 manifest 仍由用户管理，缺失时只提供指导，不下载模型。

VoxCPM 与 FunASR 不共享 Python 环境或模型缓存。VoxCPM 运行时可按 [安装指引](references/voxcpm-install.md) 和 `voxcpm-windows-py312.lock` 受控安装；模型目录、不可变 revision 和 manifest 仍由用户管理。语音检查额外使用 `--with-voice --voxcpm-python --model-path --model-revision --model-manifest`，并要求该解释器中的 CUDA 实际可用。

## 系统修改与权限影响

核心处理只读源媒体，FFmpeg 只写新的规范化副本；绝不覆盖源媒体。转写、审核和摘要在本地运行目录中保存，Vault 写入仅由 `write-obsidian` 显式触发。

VoxCPM 推理只读显式模型目录、文字或已选笔记、以及参考音频；只写 `outputs/<run-id>/`。本地 provenance 记录输入 hash、模型 revision 和 manifest hash。

## 执行步骤

1. 确认参与者知情、媒体来源、语言、保留策略和是否只保留转写。
2. 运行 doctor。缺少本地依赖时收到 `needs-dependency`，补齐环境后重新 doctor；不要切换到云端服务。
3. 执行转写：

   ```powershell
   meeting-notes transcribe --hub-root "C:\Hub" --run-id "meeting-001" --input "D:\media\meeting.mp4" `
     --ffmpeg "C:\tools\ffmpeg.exe" --funasr-python "C:\envs\funasr\Scripts\python.exe" `
     --funasr-model "D:\models\paraformer" --funasr-model-manifest "D:\models\paraformer.manifest.json"
   ```

   它会写新的 16 kHz 单声道 WAV 副本，调用一次性本地 FunASR worker，并去除任何 `speaker` 字段。空白或无效文本标记为 `[听不清 HH:MM:SS]`。
4. 用户审核并脱敏转写；然后显式提交：

   ```powershell
   meeting-notes accept-transcript --hub-root "C:\Hub" --run-id "meeting-001" --reviewed-transcript "D:\review\meeting-001.md"
   ```

5. 当前 Agent 只依据这份审核后的转写生成摘要文件；CLI 不会调用隐藏的本地或云端摘要模型。提交草稿并等待用户批准：

   ```powershell
   meeting-notes summarize --hub-root "C:\Hub" --run-id "meeting-001" --summary-file "D:\review\meeting-001-summary.md"
   meeting-notes approve-summary --hub-root "C:\Hub" --run-id "meeting-001"
   ```

6. 用户选择 Vault 位置和写入模式后，才可交付：

   ```powershell
   meeting-notes write-obsidian --hub-root "C:\Hub" --run-id "meeting-001" `
     --vault "D:\Notes" --relative "Meetings\meeting-001.md" --mode new
   ```

7. 如果用户选择仅保留转写，到第 4 步即停止：不生成 meeting-notes.md（不生成`meeting-notes.md`），不写入 Obsidian（不写入`Obsidian`）。

8. 仅在用户明确请求音频时使用语音分支：

   ```powershell
   meeting-notes speak --hub-root "C:\Hub" --run-id "voice-001" --text "需要朗读的文字" `
     --voxcpm-python "C:\envs\voxcpm\Scripts\python.exe" --model-path "D:\models\VoxCPM2" `
     --model-revision "<immutable-revision>" --model-manifest "D:\models\VoxCPM2.manifest.json"
   ```

   从笔记朗读时改用 `--vault "D:\Notes" --relative "Meetings\meeting-001.md"`；默认章节是 `摘要`，可用 `--section "决策"` 替换。声音克隆使用 `--clone --reference-audio <path>`。
   长文本会在 worker 内按自然标点无损切块、只加载一次模型后按序合成并合并为一个 WAV；`speak` 默认按文本长度估算总超时，也可用 `--timeout-seconds <秒>` 调大，超时返回 `processing-failed` 并清理本次新输出。

   参考音频推荐使用 3–15 秒、单一说话人、语音清晰且没有重叠人声或背景音乐的片段；这是质量建议，不是声音克隆的确认门。若使用派生参考文件，本次 provenance 同时记录原始来源和派生文件的 SHA-256。

## 人工确认门

- Gate A：录音/导入前，确认参与者知情、来源、语言和保留策略。
- Gate B：转写完成后，用户审核错误和敏感内容；未通过时当前 Agent 不得生成摘要。
- Gate C：用户批准摘要后，才允许写 Obsidian；未经批准的摘要草稿只能保留在私有 run 中。

## 失败恢复

`needs-dependency` 表示本地环境或模型证据缺失，补齐后重新运行 doctor。`invalid-input` 表示路径、章节、状态门或参数不满足要求。`processing-failed` 表示 FFmpeg 或 FunASR worker 失败；源媒体和已批准输出保持不变。

语音 worker 或模型检查失败会删除本次新建的语音输出目录，避免留下部分音频。VoxCPM 不可用不阻塞转写、摘要和 Obsidian 核心流程。

## 重跑、幂等与覆盖策略

run ID 不可复用，避免把不同媒体或配置混入同一 manifest。审核、摘要批准和 Vault 交付可以使用同一 run 继续；需要重新转写时使用新的 run ID。`new` 自动生成唯一文件名；`append` 和 `overwrite` 均为显式选择，已有文件的 `overwrite` 必须加 `--overwrite-approved`。

语音 run ID 也不可复用；模型 revision 不能使用 `main`、`master`、`latest` 或 `head` 等浮动别名。

## 验收标准

核心转写至少有：原媒体 SHA-256、审核后的 `transcript.md`、无说话人身份字段和可读的状态 manifest。完成的笔记交付还必须有：摘要来源 hash、用户批准、Vault 目标路径/hash 和 `complete` 状态。

语音输出至少有：`speech.wav`、`voice-request.json`、文字/笔记来源 hash、模型 revision 和 manifest hash。克隆输出还包含参考音频 hash；不存在 `speaker` 身份字段，也没有网络服务。

## 清理方式

经用户确认需要清理后，可删除私有 checkpoints、规范化副本和可重建的工作流输出。不得删除原始媒体、用户 Vault 文件、参考音频或模型缓存。清理前先保留用户需要的审核转写、摘要或音频副本。
