# Android 真机安全验收运行手册

本手册只验证 `mobile-device-automation` 的最小安全闭环，不授权真实业务写入或破坏性设备操作。

## 前提与禁止事项

- 连接一台非生产 Android 真机或专用模拟器，记录 transport 是 USB ADB、无线 ADB 还是模拟器；报告中不得记录真实 serial 或无线地址。
- 设备画面不得包含账号、通知、聊天、照片、支付、验证码或其它个人信息。只使用 Settings（系统设置）或另一个无害测试应用。
- 验收期间禁止卸载应用、清除应用数据、删除用户文件、恢复出厂设置、账户变更、支付、发送消息、发布内容和采集个人截图。
- 若设备不唯一、离线、viewport 改变、截图受保护或输入被设备策略拒绝，停止并记录，不绕过安全机制。

## 验收步骤

1. 从工作流目录运行 `doctor --mode full`，记录 Node、agent-device、Airtest 和 runtime lock 状态。然后运行 `devices`，以显式 serial 唯一绑定目标。
2. 用 agent-device 打开 Settings 或无害测试应用，获取新鲜无障碍快照。使用稳定 selector 完成一次无副作用导航，并通过新快照中的 selector 验证结果；保存脱敏 before/after 证据。
   若验收包含中文或其它非 ASCII 输入，先核对 doctor 的 `text_input_mode`。`direct-ime` 使用 `open --test-ime` 后直接 `fill` 中文；`pinyin-fallback` 使用 ASCII 拼音输入，按可访问文本唯一点击目标中文候选或应用内建议，再以新快照验证输入框值或业务结果等于目标中文。后者可静默执行，但候选缺失、不唯一或结果不一致时必须失败。安装 Helper 遇到 `INSTALL_FAILED_USER_RESTRICTED` 时由用户在真机上确认，不得重复轰炸安装请求或绕过厂商安全限制。
3. 发送 Android Home，确认返回无敏感信息的测试桌面。不要依赖或点击主机上的模拟器窗口。
4. 用 `capture` 从当前 Android serial 保存一张新鲜截图，并裁剪一个非敏感、稳定且有辨识度的应用图标。保存源截图哈希、裁剪框、viewport、方向、density 和阈值。
5. 先执行 `locate-image`，要求只有一个匹配达到阈值。随后明确强制执行一次 Airtest `click-image`；它必须重新截图定位、保存 before/after，并仍然向同一 Android serial 注入触摸。
6. 用新的 agent-device selector、前台包名或独立图片断言验证 Airtest 动作结果。图片未找到或不唯一时停止，禁止回退到坐标。
7. 将已成功的 selector `.ad` 片段与 Airtest 图片步骤写入请求，执行 `compile` 生成新 bundle。
8. 执行 `preview`，核对 serial 绑定、transport、viewport、步骤顺序、effects、资产哈希和 `plan_sha256`。本验收不得包含破坏性步骤。
9. 关闭当前执行会话，建立 fresh session，再执行 `replay`。不得复用探索阶段的元素引用、截图坐标或匹配结果。
10. 核验 `run-result.json` 为 `replay-verified`，证据包含两个 runner 的步骤顺序、动作前后截图、模板哈希、匹配分数、唯一候选数、设备 transport 和脱敏工具结果。
11. 关闭会话，确认没有遗留 agent-device/Airtest 进程或设备农场租约。私有截图、日志、serial 和路径只保留在忽略的 workspace/outputs 中；验证记录只写脱敏摘要。
    若使用过 `--test-ime`，同时核验系统默认输入法已经恢复为执行前记录的值；若使用拼音降级，证据必须包含拼音输入、唯一中文候选和最终中文结果三个状态。

## 同应用组件复用验收

在上述安全边界内，选择一个包含查询输入但无外部写入的私有应用场景，把公共步骤拆成可验证的状态转换组件，并使用精确版本引用。执行 `compose` 生成一个 bundle 后，必须用同一个 bundle 完成两个不同查询参数的 fresh session 回放；不得为第二个查询重新组合或修改组件脚本。

两次回放必须满足：

- manifest 中使用相同组件 ID 和版本，组件描述哈希与 `plan_sha256` 完全一致；只有外部参数文件中的查询值不同。
- 每次都重新检查 already-complete、precondition 和 postcondition；步骤结果只能是 `component-verified` 或 `already-complete`。
- 需要恢复时最多执行一次，并在执行主要动作前重新验证 precondition；证据明确记录组件分支、恢复结果和最终业务结果。
- 两个不同查询参数都产生独立 evidence 目录和 `replay-verified`，且最终界面验证值分别等于各自查询值。
- 私有 catalog、真实 selector、设备身份、截图和参数文件只留在忽略的 `workspace/`，公开文件只保留脱敏合同和示例。

Hub 在第一次 `agent-device replay --keep-session` 前分配本次运行专用的显式 `state-dir`，并在后续 selector 断言、语义步骤和最终 `close` 中复用。即使 replay 在返回成功响应前失败，`finally` 也必须持有该目录并尝试关闭会话。如果异常中断后仍有 claim，先用 `agent-device device status` 检查 owner；只有状态明确为 stale 时才使用 `agent-device device release --stale`，不得强制接管 live 或 uncertain owner。

任一步失败都保留现有证据并停止后续输入。只有 selector 路径、图片路径和 fresh session 混合回放全部通过，才可把该主机与 transport 标为真实验收通过。
