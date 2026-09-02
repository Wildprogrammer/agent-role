---
spec_version: "1.0"
id: "python.pillow"
type: "python"
locked_version: "12.3.0"
version_requirement: ">=12.3.0"
recommended_version: "12.3.0"
official_source: "https://pypi.org/project/pillow/12.3.0/"
official_docs: "https://pillow.readthedocs.io/en/stable/"
license: "HPND"
last_verified: "2026-07-14"
integrity:
  method: "sha256"
  value: "1cca606cd25738df4ed873d5ad46bbdb3d83b5cbca291f6b4ff13a4df6b0bbe8"
  locked_version: "12.3.0"
  source: "https://files.pythonhosted.org/packages/a6/9b/7a58e61d62be561da3a356fe2384d4059a6345fc130e23ef1c36a5b81d24/pillow-12.3.0-cp313-cp313-win_amd64.whl"
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
detect: {mode: "read-only", command: "python -c \"import PIL; print(PIL.__version__)\""}
permissions: ["read user-selected images", "write approved workflow run directories"]
network: {required_for_install: true, required_for_core_use: false}
data_access: ["local user-selected images", "workflow-private candidate data", "approved PNG outputs"]
installation: {policy: "agent-managed", scope: "global-runtime", methods: ["existing", "pip", "uv"]}
automation_status: "conditional"
---

# Pillow

## 能力用途和非目标

用于在本地解码、缩放、量化和渲染用户明确提供的图片；不上传图片，不读取未获授权的目录，也不执行打印、采购或云端图像处理。

## 官方获取与文档

只使用 frontmatter 中锁定的 Pillow 12.3.0 Windows x64 wheel 和官方文档。不得以 `latest`、未锁定的镜像或源码分支替代。

## 系统、架构、运行时和硬件支持

自动安装仅验证 CPython 3.13、Windows x64。其他系统或运行时只提供中文安装指导，并在实际使用前执行只读版本检查。

## 五种宿主兼容矩阵

五种宿主均需在其宿主 doctor 与图像 smoke 后才可标记为可执行；包安装成功不等于宿主已验证。

## 只读检测

执行 `python -c "import PIL; print(PIL.__version__)"`。检测不会下载、升级或写入任何文件。

## 各系统安装

先报告锁定版本、SHA-256、目标 Python 运行时、网络使用和预期写入位置。仅当 Windows x64 的 CPython 3.13 缺少或低于 12.3.0 时，Agent 可在全局 Python 运行时执行：

```powershell
python -m pip install --index-url https://pypi.org/simple --only-binary=:all: --no-deps --require-hashes -r <HUB_ROOT>/requirements/pillow-12.3.0.txt
```

## 调用示例和成功判据

成功判据是只读版本检查返回 `12.3.0`，并能在临时目录读取一个合成 PNG 后关闭文件句柄；不得把原图写回覆盖。

## 权限、网络、数据和遥测

核心处理离线完成。仅安装时访问 PyPI 官方 wheel；读取范围限用户选择的图片，写入范围限工作流私有 run 和已批准的 PNG 输出目录。

## 卸载或回滚

如需回滚，使用同一 Python 运行时的包管理器卸载或安装先前已锁定版本；不得删除用户原图、已交付 PNG 或其它 Python 包。

## 已知限制

屏幕色卡 RGB 只能近似实体拼豆颜色；损坏、动画、CMYK 或超像素输入必须在工作流预检中拒绝或转换，不能静默产出误导图纸。

## 替代能力

若无法满足锁定版本或平台条件，停止为 `needs-dependency` 并生成中文指导；不得改用在线图片服务。
