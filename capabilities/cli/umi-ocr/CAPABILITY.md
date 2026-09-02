---
spec_version: "1.0"
id: "cli.umi-ocr"
type: "cli"
locked_version: "sha256:965e5249ba6bf883004532434e7983de41d737b70335d1d64c56788822a019b6"
version_requirement: ">=2.1.5"
recommended_version: "2.1.5"
official_source: "https://github.com/hiroi-sora/Umi-OCR"
official_docs: "https://github.com/hiroi-sora/PaddleOCR-json"
license: "MIT"
last_verified: "2026-07-16"
integrity:
  method: "sha256"
  value: "965e5249ba6bf883004532434e7983de41d737b70335d1d64c56788822a019b6"
  locked_version: "sha256:965e5249ba6bf883004532434e7983de41d737b70335d1d64c56788822a019b6"
  source: "local Umi-OCR 2.1.5 PaddleOCR-json.exe smoke-tested on Windows x64"
systems:
  os: {windows: "verified", linux: "conditional", macos: "unsupported"}
  arch: {x64: "verified", arm64: "unsupported"}
  runtimes: ["Umi-OCR package containing PaddleOCR-json executable and bundled local models"]
  hardware: ["CPU", "local smoke test required"]
hosts:
  codex: "verified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "<PADDLEOCR_JSON_EXE> starts its local stdin/stdout JSON pipe; no image is submitted by doctor"
permissions: ["read explicitly selected local images", "read explicitly supplied PaddleOCR-json executable and adjacent local models", "write only explicitly requested OCR text output"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["explicit local image inputs", "explicit local PaddleOCR-json executable", "adjacent bundled local models", "explicit output path when requested"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "git"]
---
# Umi-OCR / PaddleOCR-json

## 能力用途和非目标

Umi-OCR 随附的 `PaddleOCR-json` 是 `image-ocr` 工作流的默认本地 OCR 引擎。Agent 以隐藏的 stdin/stdout JSON 管道调用可执行文件，仅返回阅读顺序文本；不操作 Umi-OCR GUI、不触发剪贴板识别、不上传图片，也不提供图片编辑、去水印、对象识别或视觉描述。

## 官方获取与文档

上游项目和 JSON 引擎文档以以上链接为准。本机验收的是 Umi-OCR 2.1.5 中的 Windows x64 `PaddleOCR-json.exe`，其 SHA-256 记录在前置契约中。其他版本或平台必须经过本地 doctor 与实际图片 smoke test 后才可视为可用。

## 系统、架构、运行时和硬件支持

当前只在 Windows x64 的 Codex 宿主完成验证。Linux x64 仅为条件支持：使用者必须提供可执行入口及同一目录下的本地模型；macOS 与 ARM64 不在本契约支持范围内。运行时不需要 Python 包、模型 manifest 或网络，但必须包含完整的本地 `PaddleOCR-json` 目录。

## 五种宿主兼容矩阵

Codex 的 Windows x64 本机已通过 doctor 和真实截图识别。OpenClaw、Claude Code、Hermes、OpenCode 尚未验证；它们必须使用相同的显式绝对路径、先通过 doctor、再进行本地图片 smoke test，不能把 Codex 结果外推为已兼容。

## 只读检测

doctor 使用使用者明确给出的绝对可执行文件路径及同目录 `PPOCR_api.py` 启动本地 JSON 管道，确认初始化后立即关闭，不提交图片、不写入文件、不联网。运行时通过该随包 API 启动引擎、关闭 MKL-DNN、限制长边至 960、限制 CPU 线程为 8；输入只允许工作流已验证的本地图片路径。

## 各系统安装

这是使用者管理的软件。可使用已安装版本、上游发行包、手动部署或受控 `git clone` 获得；Agent 不自动下载、安装或更新 Umi-OCR、PaddleOCR-json 或模型。Windows x64 使用对应 `PaddleOCR-json.exe` 和同目录 API；Linux x64 使用相应入口和同目录模型；不支持的 macOS/ARM64 不提供安装承诺。

## 调用示例和成功判据

先执行 `python workflows/image-ocr/scripts/ocr_assist.py doctor --umiocr-executable <绝对路径>`，成功时返回 `engine=umiocr`、`local_transport=stdin-stdout-json` 和 `mkldnn=false`。识别时添加一个或多个 `--input <绝对图片路径>`；成功仅指本地管道返回按阅读顺序的文本，低置信行会被过滤并附加“部分文字置信度不足”。

## 权限、网络、数据和遥测

核心调用只读取用户明确选择的图片及引擎路径，默认不保存文本；仅在用户明确指定不存在的新 `.txt` 或 `.md` 路径时写入结果。不上传图片、不读取 Cookie、凭据或会话、不采集遥测，也不联网取得模型。

## 卸载或回滚

使用者管理 Umi-OCR 和其模型目录。工作流只能停止自己启动的本地引擎进程，且只可清理由使用者显式指定、由工作流新建的文本输出；绝不删除源图片、Umi-OCR、模型或系统软件。

## 已知限制

默认使用该安装包的中英语言配置；低清晰度、手写、艺术字体或低置信度文本会返回“无法可靠识别”或“部分文字置信度不足”。引擎失败不会自动切换到其他本地引擎或云端服务。

## 替代能力

其他语言或 Umi-OCR 未就绪时，使用者可明确选择 `model.paddleocr` 或 `cli.tesseract` 并准备对应的本地运行时与语言数据。不得因默认引擎失败而自动切换，更不得自动上传至云端 OCR。
