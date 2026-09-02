---
spec_version: "1.0"
id: "model.voxcpm"
type: "model"
locked_version: "voxcpm-2.0.3"
version_requirement: ">=2.0.3"
recommended_version: "2.0.3"
official_source: "https://github.com/OpenBMB/VoxCPM"
official_docs: "https://voxcpm.readthedocs.io/en/latest/"
license: "Apache-2.0"
last_verified: "2026-07-16"
integrity:
  method: "sha256"
  value: "24da58a30d094a9e9a7ead450ae9cffda0d31eaeba620b61ad99179dd87e486b"
  locked_version: "voxcpm-2.0.3"
  source: "https://files.pythonhosted.org/packages/f5/50/76e912427684f7e71d443d9542802ad33df8764ef3bba954b96feeab41ba/voxcpm-2.0.3-py3-none-any.whl"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "unverified-until-local-smoke", arm64: "unverified-until-local-smoke"}
  runtimes: ["Python >=3.10,<3.13", "PyTorch >=2.5", "CUDA >=12 recommended by upstream"]
  hardware: ["VoxCPM2 reference workload: about 8 GB VRAM", "CPU and MPS require local smoke verification"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "<VOXCPM_PYTHON> -c \"from importlib.metadata import version; import torch, voxcpm; print(version('voxcpm'), torch.__version__, torch.cuda.is_available())\""
permissions: ["read explicit text or selected Vault note", "read local reference audio", "read local model cache", "write workflow output audio"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["local text", "selected local Vault note", "local reference audio", "local model cache", "workflow output audio"]
installation:
  policy: "agent-managed"
  scope: "global-runtime"
  methods: ["existing", "pip", "uv"]
automation_status: "conditional"
data_policy: "local-only"
cloud_upload: "forbidden"
---
# VoxCPM

## 能力用途和非目标

VoxCPM 是会议记录工作流的可选本地文字转语音提供方：它可以把用户直接给出的文字，或指定的 Obsidian 笔记片段，生成音频；提供参考音频时也可执行声音克隆。

它不是会议记录主链路的依赖，也不提供实时会议转写、说话人识别、云端合成或网络服务。

## 官方获取与文档

代码与发布包以 OpenBMB 官方 GitHub、PyPI 和官方文档为准。当前锁定的 Python 轮子为 `voxcpm==2.0.3`，其 SHA-256 记录在 frontmatter；模型权重由使用者从一个官方来源自行取得，并记录不可变 revision 与本地缓存 hash manifest。

## 系统、架构、运行时和硬件支持

上游要求 Python 3.10 至 3.12、PyTorch 2.5 或更高，并建议 CUDA 12 或更高。VoxCPM2 的上游参考显存约为 8 GB；Windows、macOS 和 Linux 的实际可用设备须通过本地 doctor 与合成烟雾测试确认。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes、OpenCode 当前均为 `unverified`。宿主只调用提供的本地 Python 解释器，不改变其环境，也不替代本地 smoke 结果。

## 只读检测

Doctor 只运行 `<VOXCPM_PYTHON> -c "from importlib.metadata import version; import torch, voxcpm; assert torch.cuda.is_available(); print(version('voxcpm'), torch.__version__)"`，并读取提供的模型目录和 hash manifest；不得下载权重、写入缓存、启动推理或生成声音。

## 各系统安装

The Python runtime is `agent-managed` for Windows x64 with CPython 3.12 and
CUDA 12.1 wheels only. Use the fully hashed lock
`workflows/meeting-notes/references/voxcpm-windows-py312.lock`; it pins
`voxcpm==2.0.3`, `torch==2.5.1+cu121`, `torchaudio==2.5.1+cu121`, `torchcodec==0.15.0`,
and all resolved dependencies.

An existing global Python may be used only after this read-only preflight shows
zero uninstalls and no version replacements:

```powershell
uv pip install --dry-run --python "<EXISTING_PYTHON>" --index-strategy unsafe-best-match --require-hashes -r "<HUB_ROOT>\workflows\meeting-notes\references\voxcpm-windows-py312.lock"
```

If that check reports an uninstall or replacement, create and use the dedicated
global runtime `<HUB_ROOT>\workspace\shared\runtimes\voxcpm-py312`:

```powershell
uv venv --python 3.12 "<HUB_ROOT>\workspace\shared\runtimes\voxcpm-py312"
uv pip install --python "<HUB_ROOT>\workspace\shared\runtimes\voxcpm-py312\Scripts\python.exe" --index-strategy unsafe-best-match --require-hashes -r "<HUB_ROOT>\workflows\meeting-notes\references\voxcpm-windows-py312.lock"
```

The lock permits downloads only from `https://download.pytorch.org/whl/cu121`
and `https://pypi.org/simple`. `unsafe-best-match` is limited to these two
official indexes because their package coverage must be combined; every
resolved artifact remains SHA-256 locked. Before installation, report the
exact target, locked versions and hashes, network sources, and estimated disk
requirement. Do not use bare `pip`, a system package manager, or Git.

The legacy location `%LOCALAPPDATA%\agent-workflow-hub\runtimes\voxcpm-py312`
may be detected read-only and used after the same version and integrity
checks. The Agent never moves or deletes a legacy runtime automatically.

Model weights remain `user-managed`: do not download the model, resolve a
floating revision, or change the model hash manifest. Do not start `app.py`,
Gradio, FastAPI, or a LAN service（不得启动局域网服务）。

## 调用示例和成功判据

工作流通过独立 worker 进程调用本地 VoxCPM Python API，输出只写到 `workflows/meeting-notes/outputs/<run-id>/`。成功条件是：doctor 通过、文本和模型绑定记录完整、输出音频存在且有 provenance；声音克隆另记录参考音频 hash。

## 权限、网络、数据和遥测

正常推理仅处理本地数据，禁止向云端上传文字、笔记或参考音频。不得自动将生成音频写回或链接到 Obsidian。

## 卸载或回滚

由使用者移除独立 Python 环境和模型缓存。工作流清理只删除可重建的输出；不得删除原始参考音频或 Vault 笔记。

## 已知限制

模型权重、设备支持与推理速度依赖本机硬件。VoxCPM 的声音克隆记录本地输入 provenance；参考音频宜为 3–15 秒的清晰单一说话人片段，以获得较稳定的效果。没有本地 VoxCPM 时，会议记录核心路径仍可运行。

## 替代能力

不需要声音克隆时，可以只使用直接文字转语音或跳过本分支。若本地环境不满足要求，返回 `needs_dependency`，不得静默切换到云端 TTS。
