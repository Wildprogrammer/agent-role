---
spec_version: "1.0"
id: "python.lancedb"
type: "python"
locked_version: "0.37.1"
version_requirement: ">=0.37.1"
recommended_version: "0.37.1"
official_source: "https://pypi.org/project/lancedb/0.37.1/"
official_docs: "https://docs.lancedb.com/"
license: "Apache-2.0"
last_verified: "2026-09-01"
integrity:
  method: "sha256"
  value: "488eca15361dfc34439500c9e2607c4fb2b8bf190fa1003bd54b1d6eb40e0316"
  locked_version: "0.37.1"
  source: "https://files.pythonhosted.org/packages/42/55/65c69307373b7f05dea38465b7d3836c86390f0ebc79b5c56d2c0919f229/lancedb-0.37.1-cp310-abi3-win_amd64.whl"
systems:
  os: {windows: "verified-wheel", macos: "documented", linux: "documented"}
  arch: {x64: "verified-wheel", arm64: "documented"}
  runtimes: ["CPython 3.13 Windows x64 for agent-managed install", "Python >= 3.11 source compatibility"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "python -c \"import lancedb; print(lancedb.__version__)\""}
permissions: ["read normalized knowledge", "write one agent-private LanceDB directory"]
network: {required_for_install: true, required_for_core_use: false}
data_access: ["configured local documents", "committed Git blobs", "user-confirmed experience", "agent-private index"]
installation: {policy: "agent-managed", scope: "workspace-workflow", methods: ["existing", "uv"]}
automation_status: "conditional"
---

# LanceDB

## 能力用途和非目标

用于把一个知识解答 Agent 的规范化文档、代码事实、向量和来源状态写入独立本地数据库。它不读取未配置目录，不提供云数据库，也不替代 Git、网页采集、Jenkins、禅道或宿主部署工作流。

## 官方获取与文档

Windows x64 自动准备只使用 frontmatter 锁定的 LanceDB 0.37.1 官方 CPython abi3 wheel，以及 `workflows/knowledge-support-agent/references/runtime-windows-py313.lock` 中完整的哈希依赖集。不得使用 latest、第三方镜像或源码分支替代。

## 系统、架构、运行时和硬件支持

已核对 CPython 3.13、Windows x64 官方 wheel。其他系统或架构只提供中文安装指导，并由用户环境自行验证；没有 LanceDB 时不得静默换成 SQLite 或内存数据库完成生产声明。

## 五种宿主兼容矩阵

五种宿主都必须在各自宿主内运行 CLI doctor 和最小写读 smoke 后才可标记为可执行。Python 包可导入不代表宿主 Agent 已部署。

## 只读检测

执行 `python -c "import lancedb; print(lancedb.__version__)"`。检测不创建数据库、不联网、不安装包。

## 各系统安装

安装前报告专用运行时绝对路径、锁定版本、哈希锁、PyPI 网络访问、预计写入和删除该专用运行时的回滚方式。得到普通环境准备确认后，在工作流私有虚拟环境中执行：

```powershell
uv pip install --python <WORKFLOW_PYTHON> --require-hashes -r <HUB_ROOT>/workflows/knowledge-support-agent/references/runtime-windows-py313.lock
```

## 调用示例和成功判据

成功判据：版本为 0.37.1；在临时 Agent 目录创建 `knowledge` 和 `source_state` 表；写入合成数据后能够读回；随后只删除该临时目录。

## 权限、网络、数据和遥测

运行时不需要网络，除本地配置的 Ollama 回环调用外不上传知识内容。写入范围固定为单个 Agent 的 `<workdir>/knowledge-support/lancedb`。LanceDB 本身不负责遥测或外部凭据。

## 卸载或回滚

回滚时删除工作流私有 Python 运行时或用同一包管理器卸载锁定依赖。不得把删除运行时扩大为删除 Agent 配置、知识源或其他 Agent 的数据库；数据库清理由工作流单独显式处理。

## 已知限制

第一版面向小到中等本地知识集。检索融合在 Python 层执行，因此大规模数据下需要后续评估原生索引和分片；该限制不能通过把数据发送到未配置云服务来规避。Windows 上 LanceDB 0.37.1 的内部数据文件仍受传统路径长度影响，数据库根路径超过 170 字符时工作流会在写入前拒绝，并要求选择更短的 Agent workdir；不得自动修改注册表或宿主长路径策略。

## 替代能力

缺少 LanceDB 时返回 `needs_dependency` 和中文准备指导。Embedding 缺失可降级全文检索，但 LanceDB 本身没有生产替代后端。
