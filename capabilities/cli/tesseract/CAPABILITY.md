---
spec_version: "1.0"
id: "cli.tesseract"
type: "cli"
locked_version: "git:3b7c70e34dea179549ed3e995872e2e019eb8477"
version_requirement: ">=5.5.1"
recommended_version: "5.5.1"
official_source: "https://github.com/tesseract-ocr/tesseract"
official_docs: "https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html"
license: "Apache-2.0"
last_verified: "2026-07-15"
integrity:
  method: "git-commit"
  value: "3b7c70e34dea179549ed3e995872e2e019eb8477"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "documented", arm64: "conditional-local-smoke"}
  runtimes: ["Tesseract >=5.5.1", "explicit tessdata language directory"]
  hardware: ["CPU"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "<TESSERACT_EXE> --version"
permissions: ["read explicitly selected local images", "read explicitly supplied tessdata directory", "write only explicitly requested OCR text output"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["explicit local image inputs", "explicit local executable", "explicit local tessdata directory", "explicit output path when requested"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
---
# Tesseract

## 能力用途和非目标

Tesseract 是 `image-ocr` 的可选本地后备 OCR 引擎，只有用户明确选择它且 doctor 已确认可执行文件和语言数据时才可调用。它不替代 `cli.umi-ocr` 的默认选择，也不提供图像编辑或云端能力。

## 官方获取与文档

源码与命令行用法以官方仓库和 tessdoc 为准。锁定版本对应 Tesseract 5.5.1 源码 tag 的不可变提交；使用者需记录自己实际安装二进制与语言数据的来源和哈希。

## 系统、架构、运行时和硬件支持

支持情况以本机 `--version`、可执行路径和 `tessdata` 目录的只读检查为准。所有语言包由使用者管理，不由 Agent 自动安装。

## 五种宿主兼容矩阵

五种宿主均尚未验证。宿主只可执行用户明确提供的本地可执行文件，使用参数列表调用，禁止 shell 拼接。

## 只读检测

doctor 运行指定可执行文件的 `--version` 并读取显式 `tessdata` 目录；不会安装语言包、写入系统目录或联网。

## 各系统安装

使用者可按官方方式、系统包管理器或固定官方构件安装，并准备所需语言数据。若路径或语言数据缺失，Agent 仅给出修复指导并返回 `needs-dependency`。

## 调用示例和成功判据

工作流只在用户明确选择 `--engine tesseract` 后执行 `<exe> <image> stdout -l <language> --tessdata-dir <dir>`。成功仅指本地命令返回文本；无可读文本仍应标记“无法可靠识别”。

## 权限、网络、数据和遥测

核心调用不需要网络，只读取明确输入图片和语言目录。默认不写文本；显式请求新 `.txt` 或 `.md` 路径才可写入。不得读取 Cookie、凭据或未选中的本地数据。

## 卸载或回滚

使用者管理系统安装和语言数据。工作流只可清理自己按显式输出路径创建的文本，不得移除源图片或系统软件。

## 已知限制

识别率依赖语言数据、清晰度和排版。复杂表格、手写或艺术字体不保证可靠，且不会输出坐标、表格结构或视觉推断。

## 替代能力

默认优先使用已就绪的 `cli.umi-ocr`。本地失败不能自动外发图片到任何云端服务。
