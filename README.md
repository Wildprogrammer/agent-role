# Agent Workflow Hub

Agent Workflow Hub 是一组面向多种 Agent 宿主的本地优先工作流。每个工作流声明自己的角色、能力、配置模板、运行前检查和交付物边界；实际能力仍由本机工具、目标系统权限和用户授权决定。

## 公开工作流

- `3d-printing`
- `bead-pattern`
- `daily-assistant`
- `git-operations`
- `image-ocr`
- `information-collection`
- `jenkins-operations`
- `knowledge-support-agent`
- `meeting-notes`
- `mysql-operations`
- `requirements-analysis`
- `ssh-operations`
- `test-reporting`

## 独立 Skill

- [Role-Gated Development](role-gated-development/README.md)：通用、风险驱动的四角色工作模式，可单独安装和使用，不依赖 Agent Workflow Hub 运行时；也可从[独立仓库](https://github.com/Wildprogrammer/role-gated-development)单独获取。

## 支持宿主

根工作流合约支持 Codex、OpenClaw、Claude Code、Hermes 和 OpenCode。具体工作流能否执行，以其 `SKILL.md`、当前宿主适配证据、本机能力探测和目标系统权限为准。

## 安装

要求 Python 3.11 或更高版本。开发安装：

```powershell
python -m pip install -e ".[dev]"
```

## 快速开始

```powershell
workflow-hub list "<absolute-hub-root>"
workflow-hub inspect git-operations --host codex "<absolute-hub-root>"
workflow-hub doctor --host codex "<absolute-hub-root>"
workflow-hub init-config mysql-operations "<absolute-config-directory>" "<absolute-hub-root>"
```

根 `SKILL.md` 负责工作流发现和通用运行规则；选定工作流后，其目录内 `SKILL.md` 是该领域步骤、确认点、输入和交付物的权威来源。

## 验证

```powershell
python -m pytest -q
workflow-hub validate "<absolute-hub-root>"
```

## 配置与凭据

真实服务配置应保存在仓库外的用户私有目录。仓库中的示例只包含占位符或环境变量名称；Agent Workflow Hub 不提供公共账号，也不绕过 Jenkins、MySQL、Git 或其他目标系统自身权限。

## 输出

运行输出默认位于工作流声明的 `outputs/` 或用户明确指定的位置。这些运行产物不属于源码，不应提交到仓库。

## 参考与致谢

本项目在角色设计过程中参考了 [agency-agents](https://github.com/msitarzewski/agency-agents) 项目；该项目采用 MIT 许可证。详细的固定来源与本地修改记录保存在项目内的角色来源文件中。该参考项目不是 Agent Workflow Hub 的预安装内容或运行时依赖。

## 许可证

MIT
