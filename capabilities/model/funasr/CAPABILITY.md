---
spec_version: "1.0"
id: "model.funasr"
type: "model"
locked_version: "1.3.14"
version_requirement: ">=1.3.14"
recommended_version: "1.3.14"
official_source: "https://github.com/modelscope/FunASR"
official_docs: "https://pypi.org/project/funasr/"
license: "MIT"
last_verified: "2026-07-16"
integrity:
  method: "sha256"
  value: "fd2d451a323ce0d1bda0566bc9ca4224b4429bf46d55882cf35ad482118173fd"
  locked_version: "1.3.14"
  source: "https://files.pythonhosted.org/packages/cb/f9/cda21e7a12d12889774191267b0348379ed5ab8d894d13cd239acd4538dc/funasr-1.3.14-py3-none-any.whl"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "unverified-until-model-smoke", arm64: "unverified-until-model-smoke"}
  runtimes: ["Python >= 3.8", "PyTorch", "torchaudio"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "<FUNASR_PYTHON> -c \"import funasr, torch, torchaudio; print(funasr.__version__, torch.__version__, torchaudio.__version__)\""}
permissions: ["read approved audio", "read/write approved model cache", "write approved run directory"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["local audio", "local transcript", "local model cache"]
installation:
  policy: "agent-managed"
  scope: "global-runtime"
  methods: ["existing", "pip", "uv"]
automation_status: "conditional"
data_policy: "local-default"
cloud_upload: "explicit-opt-in-only"
model_policy:
  mandarin_default: ["paraformer-zh", "fsmn-vad", "ct-punc"]
  multilingual: ["FunAudioLLM/Fun-ASR-Nano-2512", "SenseVoice provider after local verification"]
  speaker_diarization: "disabled-v1"
---
# FunASR

## Purpose

Provide local-first ASR for meeting audio, with resumable chunk transcription
and no speaker diarization in v1.

## Install

The Python runtime is `agent-managed` for Windows x64 with CPython 3.12 only.
Use the checked-in, fully hashed lock
`workflows/meeting-notes/references/funasr-windows-py312.lock`; it pins
`funasr==1.3.14`, `torch==2.5.1`, `torchaudio==2.5.1`, and every resolved
transitive dependency. Install only to a dedicated, globally reusable Python
3.12 runtime. Its recommended Windows destination is
`<HUB_ROOT>\workspace\shared\runtimes\funasr-py312`; create it with
`uv venv --python 3.12` when it does not already exist. Do not install this
lock into a pre-populated Python because its pinned dependency graph may replace
unrelated packages. Install into the dedicated target with:

```powershell
uv pip install --python "<FUNASR_PYTHON>" --require-hashes -r "<HUB_ROOT>\workflows\meeting-notes\references\funasr-windows-py312.lock"
```

The lock permits downloads only from `https://download.pytorch.org/whl/cpu` and
`https://pypi.org/simple`. Before an install, report the exact target runtime,
the locked versions and hashes, the two network sources, and estimated free
disk requirement. Do not use a bare `pip install funasr`, a system package
manager, Git, or another OS/runtime combination under this contract.

The legacy location `%LOCALAPPDATA%\agent-workflow-hub\runtimes\funasr-py312`
may be detected read-only and used after the same version and integrity
checks. The Agent never moves or deletes a legacy runtime automatically.

Model weights remain `user-managed`: the Agent does not automatically download
models. Separately resolve each model ID, immutable model revision, model-card
license, and cached-file hash manifest before use. 模型权重仍不自动下载。

## Security

Inference is local by default. Cloud upload or remote summarization requires a
separate approval naming provider, destination, and data scope.

## Success

The locked package imports, the selected model revision is cached with matching
hashes, and a synthetic audio smoke test transcribes locally with diarization
disabled.

## Known limitations

The FunASR code license is not the same as each model license. Timestamps and
language coverage depend on the selected model.

## Alternatives

If no local model fits the confirmed language/license/hardware, stop with
`needs_dependency`; do not silently use a cloud ASR API.

## Rollback

Remove only the model cache and workflow checkpoints after retention review.
Preserve user source media and approved transcript outputs.

## 能力用途和非目标

用途是本地转写；非目标是说话人身份识别、声纹保存或静默云上传。

## 官方获取与文档

官方来源为 ModelScope FunASR GitHub，包记录来自 PyPI FunASR 1.3.14。

## 系统、架构、运行时和硬件支持

Python/PyTorch/torchaudio、CPU/CUDA 和模型硬件组合必须本地 smoke 后才可 promoted。

## 五种宿主兼容矩阵

五种宿主均为 `unverified`。

## 只读检测

只读检测命令导入包并打印版本，不下载模型。

## 各系统安装

安装 wheel 后按 `model-selection.md` 下载模型，并保存 hash manifest。

## 调用示例和成功判据

成功判据是合成音频本地转写、无 `speaker` 字段、时间片段可恢复。

## 权限、网络、数据和遥测

模型首次下载需要网络；推理默认本地。云上传仅显式 opt-in。

## 卸载或回滚

清理模型缓存和私有 checkpoint 前必须经过保留策略确认。

## 已知限制

v1 禁用 speaker diarization。

## 替代能力

可选本地验证的 SenseVoice 或 Fun-ASR-Nano provider。
