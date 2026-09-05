# Jenkins MCP 运行依赖与接入验证

首次使用、迁移宿主，或解释器、包版本、配置、MCP 映射变化时执行以下检查。安装和宿主写入仍须符合已有能力安装契约及用户授权；检查本身不授权安装。

## 运行依赖归属

`workflow-hub jenkins-mcp` 由 `agent-workflow-hub` Python 包提供，命令入口声明在仓库 `pyproject.toml`，服务源码位于 `src/agent_workflow_hub/jenkins_mcp/`。只复制 `jenkins-operations` Skill 目录不能获得该 Python 包或宿主中的 MCP 工具。

使用一个明确、满足项目 Python 版本要求且依赖齐全的解释器，优先复用已验证的独立环境。无须强制使用系统 Python，也不要为此修改 Hermes、DSH 等宿主的内置环境。检查、安装和服务启动必须使用同一个解释器。

普通包安装和 editable 安装都可提供入口。普通安装适合固定版本交付；editable 安装适合持续开发，但源仓库修改会影响后续启动的服务，因此需记录源码版本并在变更后重验。依赖和版本约束以所部署版本的 `pyproject.toml` 为准，不在本文件维护另一份清单。安装缺失时先按既有安装契约处理，不因 PATH 中找不到命令就直接安装。

## 第一层：包与命令可用

下列 PowerShell 示例中的 `<PYTHON>` 是所选解释器的绝对路径：

```powershell
& "<PYTHON>" -m agent_workflow_hub.cli --help
& "<PYTHON>" -m agent_workflow_hub.cli --version
& "<PYTHON>" -m pip show agent-workflow-hub
& "<PYTHON>" -c "import agent_workflow_hub; print(agent_workflow_hub.__file__)"
& "<PYTHON>" -m pip check
```

`--help` 应列出 `jenkins-mcp`，版本由 `--version` 单独输出。核对包元数据与实际模块来源是否对应预期部署，排除同名包或错误解释器。`pip show` 的 `Location` 不必指向源码 `src`；editable 安装时再核对 `Editable project location`，不能用 editable 专有字段判断普通安装失败。`pip check` 应无依赖冲突，但不替代后续服务启动验证。

## 第二层：MCP 服务可启动

宿主或验证客户端使用以下命令及分离的参数启动服务，无需依赖 PATH 中的 `workflow-hub`：

```text
<PYTHON> -m agent_workflow_hub.cli jenkins-mcp <INI绝对路径>
```

使用标准 MCP 客户端执行完整 stdio 会话：发送有效 `initialize` 请求、接收响应、发送 `notifications/initialized`，再调用 `tools/list`（包含必要的分页），最后正常关闭连接并回收由验证客户端启动的进程。验证 `serverInfo.name` 为 `jenkins-operations`，且所需的固定工具及输入协议存在。不要使用带省略号的 JSON 或单行 `echo initialize` 代替完整握手。

这一层只证明进程和协议可用，不证明 Jenkins 地址、账号权限或目标宿主接入正确。启动失败时记录脱敏错误，不回显 INI 凭据。

## 第三层：目标 Agent 实际可调用

由用户或用户授权的部署适配器配置目标宿主 MCP 映射；Jenkins 工作流自身不擅自修改宿主配置。映射仅引用已验证的解释器、启动参数和外置 INI 路径，不复制密码到 Skill、Persona、预览或日志。

在目标 Agent 的实际会话中确认所需工具已发现，再使用 Jenkins 工作流已有的只读 preflight 和一个范围明确的只读查询验证，例如列出用户指定 Folder 的 Job。核对目标、返回状态和权限；无权限时报告实际原因，不扩大查询范围或改用其他账号。此验证不创建或修改 Job、不触发构建。

分别报告三层结果与未完成项。仅 Skill 文件存在、独立 MCP 客户端握手成功，或 Agent 能回答自身身份，都不能宣称目标 Agent 的 Jenkins 能力已就绪。若用户明确授权部署适配器，由该适配器安排和汇总这些检查并负责宿主接入协议；Jenkins 操作和服务实现始终归本工作流。
