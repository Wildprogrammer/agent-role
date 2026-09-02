---
name: git-operations
description: Use when an agent must run general Git repository operations such as committed snapshot reads, add, commit, push, shallow clone, branch create/checkout, merge, status, diff, or log through a minimal argv-only CLI with Git's own credentials and permissions as the boundary.
compatibility: Agent Workflow Hub spec 1.0; requires a local git executable and explicit absolute repository paths.
metadata:
  spec-version: "1.0"
  workflow-version: "0.2.0"
  display-name: "Git Operations"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '[]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---
# 通用 Git 操作

## 用途与触发条件

当 Agent 需要对任意本地 Git 仓库执行通用、非生命周期专用的操作时使用本工作流：查看状态（status）、查看差异（diff）、查看提交历史（log）、读取 HEAD 提交（head-sha）、列出提交树（list-tree）、读取提交 Blob（show-file）、浅克隆、暂存、提交、推送、创建/切换/合并分支。提交快照命令只读取指定提交对象，不读取工作树内容。精确、非强制 refspec 与这些只读原语可供其他工作流组合使用，但本工作流自身不引入其业务审批或生命周期约束。

## 非目标

- 不引入任何 Gate 审批门、ApprovalReceipt 或自定义确认 token 之外的策略层。
- 不自动 force：不生成 force push、force-with-lease 或任何改写历史的参数；Git 对非快进推送的拒绝原样透传。
- 不清理、不 stash、不 reset、不 clean、不 restore，也不以任何方式丢弃或覆盖用户的未提交修改。
- 不做 merge --no-ff、rebase、amend、squash、cherry-pick 或历史改写。
- 不修改远端配置、凭据、hook 或仓库之外的任何文件；凭据与权限完全交给 Git 自身处理。

## 输入

必须提供仓库的显式绝对路径。所有子命令只接受普通位置参数和最小选项，不拼接 shell 字符串。推荐入口：

```powershell
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py status --repo "C:\path\to\repo"
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py add --repo "C:\path\to\repo" "C:\path\to\repo\new file.txt"
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py commit --repo "C:\path\to\repo" -m "message"
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py push --repo "C:\path\to\repo" origin master
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py clone --depth 1 "https://example.invalid/repo.git" "C:\dest\repo"
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py branch-create --repo "C:\path\to\repo" feature
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py checkout --repo "C:\path\to\repo" feature
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py merge --repo "C:\path\to\repo" feature
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py push-exact --repo "C:\path\to\repo" --url "https://git.example.test/org/repo.git" --sha <FULL_SHA> --branch "test/test-20260821-001"
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py ls-remote-ref --url "https://git.example.test/org/repo.git" --branch "test/test-20260821-001"
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py head-sha --repo "C:\path\to\repo"
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py list-tree --repo "C:\path\to\repo" --sha <FULL_SHA>
python <HUB_ROOT>/workflows/git-operations/scripts/git_operations.py show-file --repo "C:\path\to\repo" --sha <FULL_SHA> --path "docs/guide.md"
```

相对仓库路径会被拒绝（错误信息说明必须是绝对路径）。clone 的目标目录同样必须是显式绝对路径。push-exact 是仓库绑定命令：它需要把本地提交写入远端分支，必须提供显式绝对 --repo 绑定本地对象库，并把共享 Python API（agent_workflow_hub.git_operations）返回的 argv 以 git -C <绝对仓库路径> 形式原样透传给 git；ls-remote-ref 是纯远端只读查询，只查询远端 ref，不要求也不接受本地仓库。共享 API 只接受完整小写 40 位 commit SHA 与规范分支 ref，且是受合约测试保护的公共面，任何改动都必须先更新对应合约测试。

## 输出与命名规则

脚本用 subprocess argv（绝不 shell=True）调用 git，并把 git 的 stdout、stderr 和退出码原样透传给调用方。list-tree 保留 Git 的 mode/type/object/path 及 NUL 路径分隔，show-file 原样输出 Blob 字节；调用方据此拒绝非 blob、symlink 或 submodule。脚本不创建额外输出文件。

## 依赖和运行前检查

- 本机可用 git 可执行文件（git --version 成功）。
- 对已有仓库执行 status/diff/log/add/commit/branch-create/checkout/merge/push 时，仓库路径必须存在且是显式绝对路径；脚本不自动初始化仓库。
- clone 的 URL 或本地路径由调用方提供，目标目录必须遵循 Git 自身的克隆规则。
- push-exact 需要显式绝对 --repo（本地对象库绑定），ls-remote-ref 不需要本地仓库；二者都使用共享 Python API 校验 HTTPS URL、完整 commit SHA 与规范分支 ref，不接受符号 ref、force 参数或 shell 字符串。
- head-sha、list-tree、show-file 都要求显式绝对仓库路径；后两者只接受完整小写 40 位 SHA，show-file 只接受规范的 `/` 分隔仓库相对路径。
- 无 Python 第三方依赖，使用标准库 argparse/subprocess/pathlib；共享 Python API 来自本仓库的 agent_workflow_hub.git_operations 包，脚本直接调用时会基于自身路径自动加入本仓库 src，不依赖 PYTHONPATH 或已安装发行版。

## 系统修改与权限影响

写入操作只发生在调用方指定的仓库及其远端：add/commit 修改本地仓库索引与对象库，push 由 Git 根据本地凭据和远端权限决定是否允许，branch-create/checkout/merge 只做普通分支操作。Git 自身在 checkout/merge 遇到未提交修改会拒绝并保留用户内容；脚本透传该拒绝，绝不代为解决或丢弃。凭据提示、远端权限、保护分支和 hook 均由 Git 自身处理，脚本不存储、不校验、不绕过任何凭据。

## 执行步骤

1. 解析子命令与参数；仓库路径必须是显式绝对路径，否则以非 0 退出并输出错误。
2. 按子命令构造 argv 数组；提交快照读取固定使用 rev-parse、ls-tree 和 cat-file，不 checkout，也不读取工作树文件。
3. 用 subprocess.run(argv)（无 shell）执行并把 stdout/stderr/退出码原样返回。
4. 不做任何事后清理、重试、force 或自动修复；git 返回非 0 时由调用方自行决策。

## 人工确认门

无自定义确认门。操作权限来自调用方对 Git 命令的显式选择以及 Git 自身的凭据/权限机制；本工作流不新增确认 token、不引入审批门，也不代表任何生命周期批准。

## 失败恢复

git 失败时脚本原样返回 git 的退出码和 stderr，不做自动重试。非快进推送、checkout/merge 拒绝覆盖本地修改、clone 目标已存在等情况都按 git 原样失败返回；需要恢复时由调用方基于 git 输出决定下一步，脚本不自动删除、不自动 force、不自动清理。

## 重跑、幂等与覆盖策略

每个子命令都是单次幂等调用，不维护状态。重复执行 add/commit 按 git 规则处理（无变更时 commit 返回非 0 并提示 nothing to commit）；重复 push 在已同步时是 no-op。脚本永不覆盖已有提交、永不改写历史、永不覆盖用户未提交修改。

## 验收标准

- status/diff/log 返回 git 原始退出码，stdout/stderr 原样透传。
- head-sha 返回完整提交 SHA；list-tree 保留 NUL 分隔树元数据；show-file 只返回精确 SHA 下的 Blob，即使工作树同路径已修改也不受影响。
- clone --depth N 生成对应深度的浅克隆（存在 .git/shallow）。
- add/commit/push 能把本地提交推送到本地 bare remote，远端分支与本地 HEAD 一致。
- push-exact 只生成 git -C <绝对仓库路径> push --porcelain <HTTPS_URL> <FULL_SHA>:refs/heads/<BRANCH> 形式的非 force 精确 argv，ls-remote-ref 只生成只读 ref 查询 argv，二者行为由 argv spy 合约测试锁定。
- branch-create/checkout/merge 完成普通分支流程；合并前有分歧时产生正常 merge commit。
- 未提交用户修改在 checkout/merge 被 git 拒绝时原样保留，脚本不执行任何清理。
- 仓库路径为相对路径时被拒绝；命令行不提供任何 force 选项。
- 不包含任何生命周期审批门或 lifecycle 集成逻辑。

## 清理方式

脚本不创建任何输出文件，也不删除或移动任何文件、分支、远端或用户内容。工作区、临时分支和用户修改全部由调用方或用户显式管理；本工作流不自动清理。
