# 用户确认的无界面拆件与交付

这是一条显式进入的分支，不是 3D 打印工作流的默认行为。模型超出床面只能先报告；
用户明确提出“拆件/分件/分盘”后，才可检查并设计切面。源模型始终只读，流程不上传、
不发送、不排队、不启动打印。GUI 可辅助用户批准的预览、定位或诊断，但不把界面
动作本身作为成功证据；无法独立复核时状态为 `needs_user_validation`。

## Gate A：冻结拆件计划

`split_plan.json` 必须逐项记录并由用户确认：

- 拆件原因、源模型路径、单位（当前仅 `mm`）。
- 每一刀的顺序、输入件、`point_mm`、`normal`、负/正侧 piece ID。
- `connection_strategy`：无连接件写 `none`；防转插销写
  `integrated-keyed-pin`，不能由 Agent 自行增加胶水、销钉或榫槽。
- 每个 leaf piece 的逻辑装配变换或 Bambu 分盘布局，以及最终交付目标。

`split_plan.py` 会拒绝重复 ID、无效切割树、未覆盖 leaf、同侧错误连接、缺少布局
或含默认猜测的计划。

## 一体防转插销契约

每个 `integrated-keyed-pin` connector 都必须显式提供：

| 字段 | 含义 |
|---|---|
| `id`, `cut_id`, `type` | 唯一连接件、所属切面和固定类型 |
| `male_piece`, `female_piece` | 分居该 cut 两侧的最终叶子件 |
| `center_mm` | 切面上的连接中心 |
| `axis` | 从公件切面指向母件内部；执行时不得自动翻转 |
| `key_direction` | 垂直于轴向的防转方向 |
| `width_mm`, `height_mm`, `corner_radius_mm` | 圆角矩形截面 |
| `engagement_mm`, `root_fillet_mm`, `tip_chamfer_mm` | 咬合、根部过渡和尖端倒角 |
| `clearance_per_side_mm`, `socket_bottom_clearance_mm` | 每侧装配间隙和孔底余量 |
| `minimum_wall_mm`, `minimum_edge_margin_mm` | 最低壁厚和切面边距 |

所有数值、方向及公母关系都属于 Gate A。Agent 可根据检查结果提出建议，但用户批准前
不得执行布尔；任何参数或脚本变更都会使旧批准失效。

## 可选视觉检查

### 1. 基础六视图

需要 Agent 辅助确认时，可运行不带候选计划的检查，生成 front/back/left/right/top/bottom PNG 和
`inspection.json`。它只记录源哈希、mm 范围与尺寸，不猜切面：

```powershell
& '<blender.exe>' --background --python `
  '<repo>/workflows/3d-printing/scripts/inspect_mesh.py' -- `
  --source '<source.3mf>' `
  --output-dir '<approved-run-dir>'
```

### 2. 候选计划复测

Agent 根据六视图提出完整候选 `split_plan.json` 后，可再加入：

```text
--candidate-plan <approved-run-dir>/split_plan.json
```

Blender MCP 可用于切面预览、候选连通体标色和插销方向显示，但不执行正式拆件。
GUI 也可用于用户批准的预览、导入、参数设置或诊断；无论使用 MCP 还是 GUI，都必须以
independent artifact evidence（输出文件、哈希、可解析结构、尺寸或连通体）复核，不能以
界面状态单独宣布成功。证据不可得时写入 `needs_user_validation` 并交给用户检查。
用户选择自行视觉校验时可跳过六视图/MCP，未检查项进入 `deferred_to_user`。如果执行
复测并发现壁厚/边距不足或伤及受保护特征，状态为 `needs_geometry_redesign`。

每次真正启动 Blender 前展示：精确命令、输入/输出路径、预计文件变化、主脚本及
全部本地 import 脚本的 SHA-256。已提交且哈希匹配的脚本不重复粘贴源码。

## 无界面切割和连接件执行

Gate A 通过后才能运行：

```powershell
& '<blender.exe>' --background --python `
  '<repo>/workflows/3d-printing/scripts/headless_cut.py' -- `
  --source '<source.3mf>' `
  --plan '<approved-run-dir>/split_plan.json' `
  --output-dir '<approved-run-dir>' `
  --validation-level light
```

脚本应用导入对象 transform 后按批准顺序切割封口。每刀对批准平面执行双向 BMesh
bisect：目标侧选择并提取批准连通体，反向侧生成余件；目标侧其他候选通过 Exact
`UNION` 归还余件并写入 `returned_components`，不按体积、面数或尺寸删除。
连接件种子必须命中同一候选；无法唯一归属时输出编号候选和诊断图，返回
`needs_user_component_assignment` 并等待用户，不执行猜测。无种子自动选择固定使用
0.02 mm 距离优势和 10 倍体积优势阈值。

公销使用 Exact `UNION`，母孔使用 Exact `DIFFERENCE`；不改变中心、轴向、防转方向
或间隙。只写 run 目录，不覆盖源模型。每个最终 STL 必须恰好一个连通体，
`connected_components != 1` 时停止为 `needs_geometry_repair`。

light 级别的 `cut-evidence.json` 和 `connector-evidence.json` 包含：

- 源模型、执行脚本和各 piece 的 SHA-256，以及切割顺序。
- 每个 piece 的连通体、boundary/non-manifold、体积和文件哈希；自交与完整最低壁厚
  标记为 `not_evaluated` 并进入 `deferred_to_user`。
- 每个 connector 的公母件、Exact 布尔结果、运算前后体积、理论/实测增减体积和
  有效长度/孔深。连接区域少量射线采样得到的壁厚/边距属于 light；全面壁厚遍历
  仍由用户选择 full 时执行。
- `piece_volume_sum - source_volume` 与连接件实测净体积变化一致。

full validation 才要求自交和逐面最低壁厚不得为 `not_evaluated`。任何已执行检查的
失败、布尔失败或证据不一致都进入 `needs_geometry_repair`；未执行的昂贵检查不能
宣称通过，只能由用户验收或显式升级为 full。

light evidence 中的 `geometry_evidence_passed=true` 只表示轻量自动检查通过，不表示
全面几何验证；其外部状态仍是 `generated_for_user_review`，直到 Gate C 记录用户意见。

## 三种互斥交付路径

### STL + 结构图

计划省略 `assembly_filename` 并显式指定本地 PNG
`structure_diagram_filename`。计划中的逻辑 assembly mapping 必须用 identity transform
覆盖全部 leaf；它只表达原装配关系，不是打印盘。每个 leaf 输出一个稳定命名的 STL；结构图由最终网格
副本渲染，标出 piece/STL 文件名、爆炸方向和插销配对编号。它不是打印分盘图，不
调用 slicer，也不要求 Gate B。manifest 校验完整 STL ID/哈希集合、PNG 签名、实际
尺寸和哈希，并记录 `delivery_target=stl+structure-diagram`。

### STL + 标准多对象 3MF

每个 leaf 输出一个稳定命名的 STL，并生成一个标准多对象 3MF。assembly 是逻辑装配 plate，
只表达对象和 build transform，不是打印分盘，也不触发自动排版或切片。

标准多对象 3MF 不需要 Bambu provider。校验必须证明：对象名恰好等于 leaf IDs、每件
恰好一个 build item、transform 与批准 layout 相同、单位为 millimeter、包哈希可复核、
mesh 非空且无 G-code。该分支不能同时记录 Bambu provider 或 plate package。

### Bambu G-code 3MF

仅当用户明确选择 Bambu G-code 3MF，且当前主机的 Bambu Studio CLI 已完成只读检测和
smoke，才进入 Gate B。每个确认盘必须独立生成：

```text
plate-<id>.gcode.3mf
```

`bambu_package.py` 校验单盘 G-code、MD5 sidecar、机型、喷嘴、工艺、对象映射和床面
边界。不能把多盘项目伪装为独立包，不能以裸 G-code 改名、PrusaSlicer/OrcaSlicer
输出或鼠标坐标操作替代 Bambu provider。失败为 `needs_provider_support`。

PrusaSlicer 和 OrcaSlicer 只是用户可选的通用切片 provider，不是拆件工具。

## 状态、验证级别与交付门禁

默认 light 路径：Gate A → 轻量证据 → `generated_for_user_review`。此时文件已经交付
给用户下载或导入 Bambu Studio；用户校验后 Gate C 写入 `accepted_by_user`，再关闭 run。

full 路径：Gate A → 全面几何证据 → STL/PNG 校验 → Gate C → deliver。

标准网格路径：Gate A → 几何证据 → STL/标准 3MF 校验 → Gate C → deliver。

Bambu 路径：Gate A → 几何证据 → Gate B → 每盘包校验 → Gate C → deliver。

三种交付格式互斥。manifest 只内嵌轻量摘要并以路径/哈希引用详细 evidence，固定为
`upload=false`、`send=false`、`queue=false`、`printer_started=false`。用户验收不等于
已经执行 full validation。
