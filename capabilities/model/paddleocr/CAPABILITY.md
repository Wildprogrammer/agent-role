---
spec_version: "1.0"
id: "model.paddleocr"
type: "model"
locked_version: "3.4.0"
version_requirement: ">=3.4.0"
recommended_version: "3.4.0"
official_source: "https://github.com/PaddlePaddle/PaddleOCR"
official_docs: "https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html"
license: "Apache-2.0"
last_verified: "2026-07-15"
integrity:
  method: "sha256"
  value: "67b28a98c8ce58668473702fa22a6053d959df1105ec430a578280356b889ba1"
  locked_version: "3.4.0"
  source: "https://files.pythonhosted.org/packages/32/25/75a7aa8409d9a6ece6bd7ff9a896a25b75c5a78b2948d2119ab7a99db2f2/paddleocr-3.4.0-py3-none-any.whl"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "documented", arm64: "conditional-local-smoke"}
  runtimes: ["Python >=3.11", "PaddlePaddle runtime compatible with PaddleOCR 3.4.0"]
  hardware: ["CPU or supported accelerator", "local smoke test required"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "<PADDLE_PYTHON> -c \"import os; os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'; from importlib.metadata import version; print(version('paddleocr'))\""
permissions: ["read explicitly selected local images", "read explicitly supplied local model directories and manifest", "write only explicitly requested manifest or OCR text output"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["explicit local image inputs", "explicit local model directories", "explicit local model manifest", "explicit output path when requested"]
installation:
  policy: "user-managed"
  scope: "global-runtime"
  methods: ["existing", "manual", "pip", "uv"]
---
# PaddleOCR

## 能力用途和非目标

PaddleOCR 是 `image-ocr` 工作流的显式本地备用 OCR 引擎，只将用户明确选定的本地图片转为阅读顺序文本。默认引擎是 `cli.umi-ocr`；只有用户明确选择此能力时才调用 Python PaddleOCR。它不是图像编辑、去水印、视觉描述、对象识别、表格结构化或云端 OCR 的能力。

## 官方获取与文档

代码、发布包和 OCR pipeline 文档以上游链接为准。本契约锁定 Python wheel `paddleocr==3.4.0` 的 SHA-256；模型权重不是 wheel 的一部分，必须由使用者自行取得并建立本地 hash manifest。

## 系统、架构、运行时和硬件支持

Windows、macOS、Linux 的可用性均以本地 doctor 和实际 smoke test 为准。当前契约不承诺 GPU、CPU、特定 Python 版本或特定模型组合已在某个宿主上验证。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes、OpenCode 均处于 `unverified` 状态。宿主只能调用用户明确指定的 Python、模型目录和图片路径，不能改变这些环境。

## 只读检测

doctor 先检查显式 Python、模型目录和 manifest，再在导入 `paddleocr` 前设置 `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True`。检测或运行不得下载模型、探测模型托管端或回退到远程模型。

## 各系统安装

这是使用者管理的运行时。可由使用者用手动方式、`pip` 或 `uv` 安装固定版本，并自行准备检测、识别和可选方向模型目录。Agent 缺少环境时只提供中文指导，不执行安装或模型下载。

## 调用示例和成功判据

成功判据是：显式模型路径和其 manifest 哈希匹配，离线保护变量已生效，OCR worker 仅返回读取顺序文本。任何模型缺失、哈希不匹配或模型源联网迹象都返回 `needs-dependency` 或失败，不使用默认模型名称。

## 权限、网络、数据和遥测

核心处理没有网络需求。只读取用户明示的本地图片、模型目录和 manifest；默认不保存 OCR 文本，只有用户明确给出新 `.txt` 或 `.md` 输出路径时才写入。不得读取、上传或记录 Cookie、凭据、会话或未选中的文件。

## 卸载或回滚

使用者自行移除 Python 环境和模型目录。工作流只可删除其显式创建的 manifest 或文本输出，不能删除源图片、使用者模型或其他文件。

## 已知限制

低清晰度、手写、艺术字体、复杂表格和没有可读文字的图片可能只能返回“无法可靠识别”。OCR 文本不是图像语义理解，不能据此推断没有文字支持的视觉事实。

## 替代能力

使用者明确选择且本地就绪时，可使用 `cli.umi-ocr` 或 `cli.tesseract`。本地失败不会自动切换到其他引擎或云端 OCR。
