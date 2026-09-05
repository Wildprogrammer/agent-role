# 独立运行副本

新的完整部署使用请求顶层 `runtime`，DSH 和 Hermes 共用此机制。默认模式是 `system-source`：系统 Python 运行版本化 Hub 源码副本，不创建 venv。只有用户明确要求隔离时才使用 `isolated` 私有环境。省略 runtime 只为兼容旧版引用式部署，不能称为完整独立部署。能力入口、依赖版本仍取自各工作流及项目 `pyproject.toml`，不维护第二份依赖 YAML。

## 准备与输入

主 Agent 选择宿主所在机器支持的系统 Python，不能选开发仓库里的虚拟环境。默认请求包含 `mode: system-source`、`python`、已准备的 `wheelhouse` 和新的 `destination`。工作流使用系统 Python 把依赖离线安装到版本副本的 `packages/`；不安装、升级或卸载系统全局包。

首次使用或依赖变化时，用 `runtime-wheels` 将项目声明的依赖及构建依赖下载到 Hub workspace；此操作从 PyPI 下载 wheel，不安装全局包、不修改宿主。`system-source` 与 `isolated` 都使用 wheelhouse；已有适配当前 Python/系统的 wheelhouse 可以复用，不必重复下载。

```text
python <HUB_ROOT>/workflows/specialized-agent-deployment/scripts/specialized_agent_deployment.py runtime-wheels --hub-root <HUB_ROOT> --python <ABSOLUTE_BASE_PYTHON> --destination <NEW_ABSOLUTE_WHEELHOUSE>
```

请求示例（路径换成实际值）：

```json
"runtime": {
  "mode": "system-source",
  "python": "C:/Python313/python.exe",
  "wheelhouse": "C:/agent-runtime-inputs/wheels-py313",
  "destination": "C:/agent-runtimes/my-agent/20260905-v1"
}
```

`destination` 是不存在的新版本目录，与开发 Hub、宿主 Profile/Preset 分开；更新时使用新的版本目录。wheelhouse 必须包含当前 Python/平台可用的完整 wheel 集。模式、解释器、版本和文件摘要纳入本次预览；apply 不临时联网解析版本。

## 交付内容与切换

- `hub/`：根 Skill、包元数据、完整 `src/agent_workflow_hub/` 实现、capability/adapter 说明，以及本次所选 Skill 的脚本、模板、角色、参考文件等固定快照。
- `run-workflow-hub.py`：system-source 启动器，强制从副本 `hub/src` 导入 Hub，并优先从副本 `packages/` 导入依赖。
- `wheelhouse/`：两种模式都复制预览绑定的 wheel；system-source 以 `pip --target` 安装到副本 `packages/`。
- `venv/`：仅 isolated 模式生成；以普通安装而非 editable 安装 Hub，不复制开发 `.venv`。
- `runtime-ready.json`：安装、导入、文件摘要与 Hub MCP 握手/工具发现结果。

不复制开发历史、测试、outputs、workspace、私有 `.env`。外置配置仍按路径引用；模型、数据库、用户数据和第三方应用不混入程序副本，其迁移与选用依赖按所属工作流及用户范围另行处理。此模块安装的是 Hub 的 Python 依赖，不声称自动安装任意工作流的外部工具或模型。

preview 只读源文件并输出清单、摘要、体积、安装命令和目标路径。原有 `deployment_review` 一次涵盖运行副本准备与宿主切换，不增加安装确认或逐文件确认。apply 先重新核对预览，再复制资源、离线安装、检查 Hub 与依赖包位置/CLI，且对迁移的 Hub MCP 做 initialize 与 tools/list；以上成功后才由适配器修改宿主引用。此步骤不调用任何业务工具。

Persona 指明副本的 `HUB_ROOT` 和 Python，宿主发现用的 `skills/` 仍由同一组快照自动生成。DSH/Hermes 的显式 `host_options.mcp_servers` 中，`-m agent_workflow_hub.cli ...` 入口自动改用系统 Python 加副本启动器；isolated 模式才改用私有 Python。INI 参数保持外部引用，其它第三方 MCP 不擅自改写。

## 验证与恢复

`verification.json` 的 `static.runtime` 单独记录副本检查和 Hub MCP 工具发现；system-source 还要求实际模块路径位于副本 `hub/src`。这不等于宿主 Agent 已实际调用工具。目标 Agent 内的最小只读调用仍按所属工作流执行，未验证时保留 `partially_verified`。

安装失败不切换现有 Agent；失败的新版本目录保留供诊断，不自动删除。宿主切换期间失败沿用已有文件/配置事务回滚。旧版本不会被覆盖或自动清理，可用于恢复；需要恢复已成功切换的版本时按其记录生成明确的恢复操作，不直接重放旧 planned manifest。

“独立”表示不依赖开发仓库和开发虚拟环境，并非可随意复制到其它操作系统的可执行程序：基础 Python、DSH/Hermes 本身、外置配置及服务仍须存在。更换机器、Python 或操作系统时，用匹配的新 wheelhouse 重新创建运行副本，不搬运旧 `packages/` 或 venv。
