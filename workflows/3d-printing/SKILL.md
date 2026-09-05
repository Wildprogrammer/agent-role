---
name: 3d-printing
description: Use when a user needs to design, validate, split on confirmed planes, slice, or deliver reviewed 3D-print artifacts without controlling a printer.
compatibility: Agent Workflow Hub spec 1.0; selected host and providers must pass read-only detection and smoke checks.
metadata:
  spec-version: "1.0"
  workflow-version: "0.4.10"
  display-name: "3D Printing"
  execution-modes: '["single-agent","multi-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "explicit"
  multi-agent-write-policy: "separate-output-main-integrates"
  approval-owner: "main-agent"
  required-capabilities: '["app.blender"]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"preflight":"python <HUB_ROOT>/workflows/3d-printing/scripts/preflight.py --hub-root <HUB_ROOT>"}'
  capability-slots: '{"interactive-modeling":["mcp.blender"],"slicer":["app.bambu-studio","app.prusaslicer","app.orcaslicer"]}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---
# 3D Printing

## 用途与触发条件

Use this workflow for a new or revised printable object that needs explicit
dimensions, manufacturing constraints, mesh checks, slicing, and reviewed
artifacts.

The workflow ends at reviewed artifact delivery. It never uploads, queues, sends, or starts a print.

本工作流的终点是生成并按所选验证级别检查拓竹可读取的交付文件，例如 STL、标准 3MF，或在
Bambu provider 已通过主机 smoke 时生成的 Bambu G-code 3MF；不进入上传、排队、
发送或启动实体打印流程。

不得发送或启动打印、排队打印、控制实体打印机或猜测 printer/material
profile。最终交付只能是用户批准目录中的文件。Blender 建模成功不等于可打印。

Split-and-plate is opt-in only. 模型超出床面本身不会触发拆件、切面选择或分盘。

## 非目标

本工作流不负责发送、排队或启动打印，不自动决定切面、连接件、打印盘
或装箱布局，也不把任意 slicer 输出冒充为 Bambu G-code 3MF。

## 输入

Collect purpose, units, dimensions, tolerances, material, target printer,
nozzle, references, preferred output format, and whether multi-agent assistance
is explicitly approved.

- Gate A：确认制造约束；若进入拆件，还必须确认拆件原因、源文件、单位、
  切割平面顺序、piece IDs 和连接策略。只有交付打印盘时才确认件到盘映射及
  多件同盘的 position/rotation。采用 `integrated-keyed-pin` 时，先完成只读检查；
  Gate A must confirm every exact connector parameter，不能由执行脚本猜测或翻转。
- Gate B：仅在用户明确要求切片或 Bambu G-code 3MF 时，确认 slicer provider、
  printer/nozzle/material/profile 和输出格式。只交付 STL、标准 3MF 或
  STL + 结构图时不要求 Gate B。
- Gate C：检查所有输出；仅在进入切片分支时检查预览/警告和配置快照，其他分支只
  检查所选验证级别的摘要、用户意见和交付范围。
  主 Agent 负责所有批准。

## 依赖和运行前检查

### 运行入口与部署验证

宿主预检脚本 `scripts/preflight.py` 需要项目支持的 Python 和可导入的 Hub 包（使用 `agent_workflow_hub.frontmatter`）；正式建模/拆件脚本由 Blender 自带 Python 提供 `bpy`/`bmesh`，不是向系统 Python 安装同名包。部署时保留配套 scripts、references 和所选能力资产，只复制 Skill 文本不够。

首次使用或环境变化时，在目标 Agent 命令环境检查预检脚本 `--help`，再按下文以实际 Hub 根目录和应用路径预检。只有切片分支才检查对应切片器；仅导出模型不要求把全部 provider 都安装好。若选择可选 Blender MCP，还需同时具备 Blender 侧桥接/插件和目标宿主映射，并通过实际只读场景查询验证连接；仅发现 MCP 名称不算可用。缺少该通道仍按现有 headless 路径执行，不自动改宿主配置。

`app.blender` 是本工作流的核心能力。`mcp.blender` 是可选的
MCP-assisted inspection 通道，用于交互式建模、切面预览、候选连通体标色和结果
视觉复核；正式拆件仍使用 headless Blender。没有 MCP 不阻塞已有网格拆件，改用
带编号的诊断 PNG。MCP 视觉结果不能替代文件、连通体和哈希检查。

GUI 可作为辅助通道，用于预览、导入、参数设置、交互修改或没有稳定接口的操作；
它不能成为正式拆件、切片或最终成功的唯一依据。GUI 操作本身不得作为成功证据；
必须以输出文件、哈希、可解析结构、尺寸/连通体等独立证据复核。无法独立验证时状态为
`needs_user_validation`，交付已有产物和简短检查项，由用户确认；不得静默成功或重复
尝试 GUI。

Blender and slicer applications are `user-managed` system setup. Bambu Studio,
PrusaSlicer 和 OrcaSlicer 都先只读检测，再由用户选择并完成安装。检测结果
必须记录每个 capability 的 `version_requirement` minimum 和
`recommended_version`；推荐版本不否定已经验证的兼容版本。

若没有可用环境，只能在明确批准的运行目录生成 `INSTALLATION-GUIDE.md`，
使用当前用户语言调用：

~~~text
scripts/preflight.py --hub-root <absolute HUB_ROOT> --guide <approved path> --language <current-user-language>
~~~

若已安装程序不在 PATH，使用只读绝对路径覆盖，例如
--tool-path app.blender=C:\path\blender.exe；该参数只用于检测，不复制、安装
或修改软件。
Bambu G-code 3MF 交付目标另加 --mode split-and-slice-bambu；未通过 Bambu provider smoke
时预检必须阻塞，不得以 PrusaSlicer 或 OrcaSlicer 代替。

安装指导不执行安装器、系统包管理器、Git clone 或 MCP 宿主配置。执行后由
Agent 重新做只读检测；`DISABLE_TELEMETRY=true`。

## 系统修改与权限影响

Agent 不安装 user-managed 桌面软件，不修改包管理器、MCP 宿主或打印机配置。
执行任何工具前说明网络、磁盘、遥测和写入范围。

## 执行步骤

1. Gate A 确认制造约束，建立唯一 run 目录和不可变源文件记录。
2. 选择 Blender 建模策略，按小步保存 .blend checkpoint。
3. 默认使用 `--validation-level light`，只检查源文件未变、输出存在、piece
   数量/名称、单连通、boundary/non-manifold、连接件体积变化、连接区域的少量
   壁厚/边距采样和文件哈希。
   self-intersection、完整壁厚、overhang、orientation、bed bounds、切片导入由
   用户要求 full validation 时执行，否则列为用户校验项。
4. 若目标包含切片，Gate B 确认 provider/profile 并记录不可变 snapshot。
5. 仅在目标包含切片时检查 preview、warnings、时间、耗材、床面边界和兼容性。
6. 轻量检查通过后以 `generated_for_user_review` 交付文件；用户校验通过后记录
   `accepted_by_user`。Gate C 后不上传、发送、排队或启动打印。

执行仓库中已提交且哈希匹配的 Blender Python 前，只展示脚本路径、SHA-256、
受影响文件、the exact Blender background command 和预计场景变化；不重复粘贴完整
源码。只有脚本内容或哈希发生变化、脚本未提交，或用户要求审阅时才展示代码正文并
重新批准。

切换 slicer provider 后必须重新确认门 B；旧 G-code 和 final-review evidence
必须失效。失败时保留源模型、checkpoint、报告和诊断证据。

## 人工确认门

Gate A、Gate B、Gate C 的批准都由主 Agent 持有；拆件分盘只有用户明确提出才可
进入，不能把尺寸超限当作隐式批准。

## Headless split-and-plate branch

拆件分盘不是默认能力；它仅在用户明确提出时进入。

纯 STL/结构图拆件只要求经过验证的 headless cutter；package verifier 只在对应
3MF 交付分支需要。缺少当前交付格式所需 verifier 时只能保留实验/诊断证据并停止，
不把临时脚本当作已安装的工作流能力。

### Gate A proposal-only mode

When a user asks the Agent to propose a split, read-only mesh statistics and
diagnostic renders may support hypotheses, but coordinate axes do not establish
semantic labels such as left/right, front/back, head/limb, or a safe interface.
Until a user confirms the model orientation or an independently inspectable
diagnostic supplies that mapping, use neutral candidate labels and explicitly
state the uncertainty; never turn an axis-density guess into a cut instruction.
Each candidate must retain its evidence source and the exact missing
confirmation. For `integrated-keyed-pin`, propose only its real fields
(`width_mm`, `height_mm`, `corner_radius_mm`, `engagement_mm`, clearance and
wall/edge limits); it must not use a pin diameter or apply connector geometry.
An autonomous Gate A proposal is allowed only after a visual-capable host has
inspected source-derived diagnostic renders (at least three orthogonal views)
or another independently inspectable diagnostic establishes the semantic
mapping. It may then publish `proposed-cuts` and `proposed-connectors`, with
their render paths, observations, confidence, and assumptions. Every proposed
coordinate and connector field remains a hypothesis tagged
`requires_gate_a_confirm`; it is not an approved cut instruction.

对于已经确认拆件和装配需求的方案，优先推荐 `integrated-keyed-pin`，胶水仅作为备选：
只有当接合面、壁厚、边距、打印方向、装配次数和公差表明插销不适用时，才提出胶水
备选。推荐不等于 Gate A 批准；实际几何仍须由 Gate A 确认每一个精确连接件参数。

For a `.3mf` diagnostic render, use the committed
`scripts/three_mf_import.py` `load_3mf_mesh` parser and build a temporary
Blender mesh from its vertices and triangles. Do not invent or depend on
`bpy.ops.import_scene.*` import operators: they are not a reliable 3MF path in
the supported headless Blender runtime.

If the visual-capable host or its configured vision backend fails, retain only
the raw diagnostics and the exact failure in `needs_host_vision_support`. It
must not publish semantic labels, proposed cuts, or proposed connectors from
axis statistics or an uninspected render. Repair or select a working vision
capability, then restart proposal generation with a new run ID.

For each detachable limb or other local module, a Gate A proposal must include
component-selection evidence: a target-side plus a seed point, bounded region,
or other inspectable rule that selects the intended connected component after
the bisect, and it must state that unselected components are returned to the
source remainder. A global plane alone must not be labelled as isolating an arm
or leg; it only becomes an implementation candidate once this local selection
is specified and confirmed.

For every local selection, record the signed seed-to-plane distance
`dot(seed_point_mm - point_mm, normal)`. Its sign must match `target_side`
(`positive` is greater than zero; `negative` is less than zero). A mismatch is
an invalid Gate A proposal, not a harmless narrative label; correct it before
asking the user to confirm.

End at `needs_user_split_plan` / Gate A confirmation. Proposal artifacts are
diagnostic only: do not call the formal cutter or create final STL, cut evidence,
structure diagrams, or manifests. A machine-readable proposal may be saved for
review, but it must never be named `split_plan.json` and may not be passed to
the formal cutter.

- 未明确提出时，即使模型超出床面，也只报告
  `needs_user_split_request`， 不得自动切割、旋转、缩放、建盘、装箱或分配。
- 用户请求但缺少切面或连接策略时停止并请求确认；只有交付目标包含打印盘时，
  缺少 piece-to-plate 映射或多件盘布局才阻塞。不得把“中间”解释成某个默认轴，
  不得自动添加销钉、榫槽或胶水结构。
- 计划以 N 个 piece 和 M 个由用户确认的 plate 表示；N 不必等于 M，
  不自动把两件/两盘当成默认规则。
- 对已有 STL/3MF 使用 headless Blender：
  `blender --background --python <approved-script> -- ...`。
  The source model remains immutable，输出使用稳定的 `piece-<id>.stl`，记录源哈希、
  脚本版本/哈希、plane 参数、输出哈希、体积和拓扑证据。
- Before invoking a formal tool, resolve the Hub root once and use absolute
  plan and output paths. The plan, formal evidence, and artifacts for one run
  must remain under `workflows/3d-printing/outputs/<run-id>`; never `cd` into
  `scripts/` and create a relative duplicate of a run file.
- 对每个切面执行双向 BMesh bisect：目标侧分析候选并提取已确认连通体，反向侧
  生成余件；目标侧未选候选使用 Exact `UNION` 归还余件并记录
  `returned_components`。远处不相接壳体保留并归还原部位。不得按体积、面数或尺寸
  静默删除、合并或重归属壳体。
- 连接件种子点必须命中同一候选。无种子时固定使用
  `distance_tolerance_mm=0.02` 和 `dominance_ratio=10.0`：最近候选须比次近候选
  多出 0.02 mm 以上距离优势，且体积至少为其 10 倍，才可自动选择；否则停止为
  `needs_user_component_assignment`，输出带
  编号候选及诊断图并询问用户，不得用赶工或“看起来像”作为决胜规则。
- 每个最终 STL 必须恰好包含一个连通体；闭合、流形和无自交不能替代该检查。
  任一 STL 的 `connected_components != 1` 时停止为 `needs_geometry_repair`。
- `integrated-keyed-pin` 的 six-view read-only inspection is optional。Agent 可建议
  六视图或 MCP 预览；用户选择自行校验时，只冻结精确参数并保留未检查项目。
  精确字段和证据格式见 `references/split-and-slice-bambu.md`。
- 默认 light validation 仍要求每个最终 STL 恰好一个连通体，并检查
  boundary/non-manifold；全面自交和逐面最低壁厚可以为 `not_evaluated`，但必须明确
  列入 `deferred_to_user`，不得伪装成 full validation 通过。
- 只有用户明确选择 Bambu G-code 3MF 时才使用 Bambu Studio。Bambu Studio is required only for Bambu G-code 3MF delivery；CLI 必须先通过当前主机
  和版本的 smoke。每个确认盘交付一个独立的
  `plate-<id>.gcode.3mf`，并验证包内单盘 G-code、MD5、机型、喷嘴、工艺、
  对象映射和床面边界。
- CLI 无法复现已确认布局或包结构时，状态为 `needs_provider_support`。GUI 可以辅助
  用户批准的预览、导入或诊断，但鼠标坐标、GUI Cut、裸 G-code 改名或多盘包都不能
  单独证明独立 Bambu 包；仍须由可解析包、MD5、profile 与对象/床面证据复核。

### STL + 结构图交付

只有计划显式给出本地 PNG 文件名 `structure_diagram_filename` 时才生成结构图。
它必须由最终待导出网格的副本生成，标明全部 piece/STL 文件名、爆炸方向和插销
配对编号；渲染不得改变交付几何。结构图不是打印分盘图，也不表示 slicer 布局。

该交付目标只包含全部 leaf STL、PNG、哈希和验证证据，不生成标准多对象 3MF，
不调用 slicer，不要求 Gate B，也不要求打印用 piece-to-plate 布局。为满足当前
计划契约，STL + 结构图计划必须保留逻辑 assembly mapping，并以 identity transform
覆盖全部 leaf；该映射只用于装配关系校验，不代表打印盘。manifest 必须记录
`delivery_target=stl+structure-diagram`，验证 PNG 签名、实际尺寸、文件哈希和
完整 STL ID/哈希集合。标准多对象 3MF 只有用户或计划显式要求时才生成。

Formal artifact boundary: when a plan requests `structure_diagram_filename`,
the committed `structure_diagram.py` must render the PNG from the final leaf
meshes and `headless_cut.py` must record it in `cut-evidence.json`. A formal
renderer failure is a blocked delivery: retain diagnostics and partial files,
but do not create a substitute PNG or manifest, and do not report
`generated_for_user_review`. Repair the approved renderer and rerun the formal
chain instead.

## 验证预算与用户验收

默认运行级别为 `light`。一次轻量检查完成后不得重复执行完整验证、仓库全量测试、
三个 Blender smoke 或 MCP 二次复核。full validation 只在用户明确要求或轻量检查发现
具体异常时定向执行；用户要求切片只增加相关 slicer 检查，不自动升级全面网格验证。

轻量输出使用 `generated_for_user_review`，并分别列出 `automated_checks` 与
`deferred_to_user`。如果轻量检查本身超时或工具无法完成，保留已生成文件，状态为
`needs_user_validation`，向用户给出简短检查清单，不循环重试。用户确认结果正确后，
Gate C 记录 `accepted_by_user`；用户指出问题后只复查相关部件和相关检查。

`generated_for_user_review` is an artifact-review state: final artifacts and
their formal evidence must exist, but it does not require a manifest. Before
explicit user acceptance, an Agent must not create a manifest containing
`accepted_by_user`, `gate-c-confirmed`, or `delivered`; those are user-review
and delivery events, not results inferred from light validation.

manifest 只保存验证级别、状态、证据文件路径/哈希、piece 哈希、自动检查摘要和
用户验收状态，不内嵌全部逐面或候选记录。详细数据留在独立 evidence 文件中，Agent
正常成功时不把整份 evidence 读回上下文；只有失败时读取异常字段和相关候选。

正式替换顺序固定为 `replacement → final`：先在 replacement 生成，完成所选级别
检查后复制到 final。`generated_for_user_review` 文件立即提供给用户下载或导入
Bambu Studio 校验；仅在用户明确 `accepted_by_user` 后才记录 Gate C、写入交付
manifest。新产物获得 `accepted_by_user` 前不得删除旧产物；即使用户已验收，删除
旧文件仍必须取得包含目标文件的精确路径授权，且只删除该路径。

## Provider choice

PrusaSlicer and OrcaSlicer are optional。它们可以在用户明确选择时切片或
做普通 G-code 预览，但不是拆件工具，也不能把其输出静默声明为 Bambu
G-code 3MF。若目标是 Bambu 专用交付，缺少可验证的 Bambu provider 就停在
`needs_provider_support`。

如果用户只需要 STL 或标准 3MF，可在完成网格与文件校验后结束；只有目标明确为
Bambu G-code 3MF 时，才要求 Bambu Studio provider smoke 和单盘包校验。
Standard multi-object 3MF does not require a Bambu provider；它必须逐 leaf
验证对象名、build transform、文件哈希和无 G-code，不能与 Bambu 包路径混用。

## 失败恢复

缺少用户拆件请求或计划参数时保持源文件不变并返回 needs_user_split_request。
连通体归属不明确时保留所有候选并返回 `needs_user_component_assignment`；
几何证据失败时保留 checkpoint 和诊断报告；provider 或包结构失败时返回
needs_provider_support。GUI 可辅助用户批准的诊断或复核，但必须以独立证据确认；
无法确认时记录 `needs_user_validation`，不静默将 GUI 结果声明为成功。

## 重跑、幂等与覆盖策略

每次运行使用新的 run ID；只有输入哈希、切割脚本及本地导入脚本哈希、切面计划和 provider
profile 均未变化时才可复用证据。切换 provider/profile 会清除旧 G-code 和
final-review 状态。

## 输出与命名规则

每次运行写入 workflows/3d-printing/outputs/<run-id>/，至少记录源文件、
checkpoint 或导出网格、validation report，并适用时记录 provider/profile snapshot、
manifest 和最终文件。不同运行使用不同 run ID，不覆盖已有输出。

## 验收标准

所有预期文件存在；自动检查已通过，或未完成项已明确交由用户且获得
`accepted_by_user`。适用的 Gate A/B/C 有记录，且 `upload=false`、`send=false`、
`queue=false`、`printer_started=false`。只有 full validation 才可声明全面几何验证通过。

## 清理方式

普通清理只处理私有 workspace 状态。删除 outputs 必须得到明确的、带精确
路径的确认；不得为了清理删除用户验收的模型、G-code、3MF 或验证证据。
