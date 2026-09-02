---
name: bead-pattern
description: Use when a user wants a local JPG or PNG image converted into a reviewed bead-pattern candidate and one coded PNG chart using a fixed selected palette.
compatibility: Agent Workflow Hub spec 1.0; requires the selected python.pillow capability to pass read-only detection.
metadata:
  spec-version: "1.0"
  workflow-version: "0.1.0"
  display-name: "Bead Pattern"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '["python.pillow"]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"plan":"python <HUB_ROOT>/workflows/bead-pattern/scripts/bead_pattern.py plan --hub-root <HUB_ROOT> --input <ABSOLUTE_INPUT> --run-id <RUN_ID> --columns <COLUMNS> --rows <ROWS>"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# 拼豆图纸

## 用途与触发条件

用于把用户明确提供的 JPG 或 PNG 原图转换成拼豆候选，并在用户确认候选后交付一张单页编码 PNG 图纸。用户需选择固定 A–M 色卡档位、网格尺寸和可选背景色号；默认使用 144 色档和完整保留画幅策略。

## 非目标

不提供 PDF、打印、采购、库存判断、网页应用、外部在线图像服务、自动拆分多板或未经确认的最终图纸。不得将包装上的参考颗数推断为用户实际库存。

## 输入

输入是只读的本地 JPG/PNG 原图、固定调色板档位 `24 / 48 / 72 / 96 / 120 / 144 / 221`、板型 `52×52 / 78×78 / 104×104` 或明确的自定义列×行。调色板档位是固定递增子集，不按原图动态抽取任意 N 种颜色。画幅默认完整保留（contain）、居中留白；透明像素和留白默认是空格，不计入颗数。

## 输出与命名规则

私有候选和冻结数据仅写入 `workspace/workflows/bead-pattern/runs/<run-id>/`。用户交付物仅为 `workflows/bead-pattern/outputs/<run-id>/pattern.png`：包含行列坐标、逐格色号、像素预览、色号/RGB 色块/颗数和总颗数。每个 run-id 只能创建一次，绝不覆盖既有候选或交付物。

## 依赖和运行前检查

先按 `python.pillow` capability 执行只读版本检测，确认 Pillow 满足 `>=12.3.0`。再验证输入文件是单帧 JPG/PNG、路径可读、图像未损坏、像素数在上限内，且所选网格不超过板型上限。缺依赖时只按 capability 的安装契约处理，不以其它安装器或在线服务替代。

## 系统修改与权限影响

核心处理只读取用户选定原图，并写入工作流私有 run 或用户确认的输出 PNG。Pillow 仅可依据 capability 的 `agent-managed`、`global-runtime` 契约安装；不得修改宿主配置、系统包、打印机、用户色卡文件或原图。

## 执行步骤

1. 读取调色板 revision、档位、网格和背景规则，并运行 `plan` 创建唯一私有候选 run：

   ```powershell
   python <HUB_ROOT>/workflows/bead-pattern/scripts/bead_pattern.py plan --hub-root <HUB_ROOT> --input <ABSOLUTE_INPUT> --run-id <run-id> --preset 144 --columns <N> --rows <N> [--board 52|78|104] [--background-code CODE]
   ```

2. `plan` 先作 Pillow 只读版本检查，再以 contain 规则进行本地解码、方向校正、缩放和固定调色板量化。它仅写候选矩阵、候选摘要和色号用量，不写交付 PNG。
3. 向用户展示 `plan` 返回的调色板、网格、空格数、用量、总颗数和像素预览；不能以 Agent 自行判断代替确认。
4. 只有获得用户对当前候选的明确确认后，才可调用：

   ```powershell
   python <HUB_ROOT>/workflows/bead-pattern/scripts/bead_pattern.py accept --hub-root <HUB_ROOT> --run-id <run-id>
   ```

5. 冻结成功后调用：

   ```powershell
   python <HUB_ROOT>/workflows/bead-pattern/scripts/bead_pattern.py render --hub-root <HUB_ROOT> --run-id <run-id>
   ```

   `render` 只能读取冻结 `pattern.json` 并生成一张 `pattern.png`；不得重新读取或量化原图。

## 人工确认门

用户必须确认候选后才能冻结或渲染最终 PNG。改变原图、调色板档位、网格、板型、背景色号或画幅策略时必须新建候选，不能复用旧确认。若要将空格填为背景色，背景色号必须在当前固定档位内并由用户明确指定。

## 失败恢复

输入无效、损坏、超限、色号不在所选档位、板型越界、依赖不满足或 run-id 已存在时停止，不产生最终交付物。CLI 以 `invalid-input`、`needs-dependency`、`needs-user-confirmation` 或 `output-exists` 返回可判定错误类别。保留已完整写入的候选或冻结数据；不要覆盖源图、旧候选或旧交付 PNG。

## 重跑、幂等与覆盖策略

同一 run-id 的重复 plan、accept 或 render 均拒绝。使用新 run-id 重跑；只有相同冻结数据才能渲染同一候选，最终渲染不重新量化。输出路径必须在 Hub 根目录内，且不能穿越符号链接、联接点或重解析点。

## 验收标准

最终 PNG 只使用所选固定档位中的色号；非空格色号用量之和等于总颗数；候选矩阵和冻结后渲染矩阵一致；透明与 contain 留白默认不计珠；104×104 图纸保持格内色号可读，超出可读性或资源上限时明确拒绝而非静默缩小。

## 清理方式

默认不删除任何候选、冻结数据或交付物。清理私有可重建缓存前先列出精确绝对路径；删除交付物须指定 workflow、精确文件路径并再次获得用户确认。始终保留原图和用户提供的色卡资料。
