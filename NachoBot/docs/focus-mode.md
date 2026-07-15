# Focus 短期跨会话模式

Focus 在一个显式配置的会话组内只允许一个会话处于活动状态。后台会话收到消息时只聚合为事件，当前会话的 Planner 可以用终止动作 `switch_chat` 切换活动会话；切换时生成一份有界、短期、不可直接执行的 handoff，并在目标会话下一次由 Replyer 生成回复时注入。

## 当前支持范围

- 同一 Focus 组内 `群聊 -> 群聊`。
- 同一 Focus 组内 `群聊 -> 私聊`，需开启 `allow_group_to_private`。
- 私聊内容不允许导出到群聊或其他私聊；v1 不提供 `私聊 -> 群聊` 和 `私聊 -> 私聊` handoff。
- 私聊可以通过不携带 handoff 的元数据控制路径安全返回组内群聊。
- `off` 完全保持原有回复路径；`active` 启用 Focus。`observe` 当前是安全占位模式，不改变消息路由。

群聊和私聊必须显式列入同一个 Focus 组，成员的 `allow_export` / `allow_import` 还会在服务端再次校验。模型不能指定真实 chat id、epoch、策略版本或 handoff id。

## 运行流程

1. 接收层先把消息写入数据库，获得不可变的消息行号。
2. 当前活动会话的消息唤醒其运行时；后台会话只更新未读事件，不启动后台回复循环。
3. Planner 看到由服务端生成的 `focus_events`，只能引用其中的 `event_id` 和 `revision`。
4. `switch_chat` 校验当前 lease、事件 revision、组成员关系和隐私策略，并等待旧会话正在发送的副作用结束。
5. SQLite 事务原子更新活动会话和 epoch，同时保存 handoff；旧 lease 随即失效。
6. 目标会话由 Replyer 正常生成回复。handoff 被放在当前消息之后、长期记忆之前，内容经过转义、长度限制并标记为不可信上下文。
7. 只有适配器确认至少一段回复实际送达后才 ACK handoff。生成失败、取消、静默抑制或 lease 过期都会释放 reservation，不会误消费上下文。

消息读取使用数据库主键行号而不是时间戳。一次 Focus turn 只有在完成、无操作或被策略丢弃时提交 cursor；失败、取消、过期或切换会安全重试。活动会话、epoch、cursor、事件和 handoff 均由独立的 Focus 表持久化，启动时在注册消息处理器前恢复；若进程在消息入库和 Focus event 落库之间退出，启动扫描会从 row-id cursor 补建事件。

## 配置

在 `config/bot_config.toml` 中配置；完整注释示例也可参考 `template/bot_config_template.toml`：

```toml
[focus]
mode = "active"
allow_group_to_private = true
membership_migration = "idle_safe"
unread_event_threshold = 5
unviewed_event_seconds = 180
max_events_per_prompt = 5
max_unread_messages = 20
switch_cooldown_seconds = 2
handoff_ttl_seconds = 600
handoff_successful_cycles = 3
handoff_prompt_tokens = 512
reservation_ttl_seconds = 120
bypass_gate_enabled = true
bypass_gate_timeout_seconds = 8.0
bypass_gate_max_tokens = 160
bypass_gate_retry_seconds = 3.0
bypass_gate_max_attempts = 2

[[focus.groups]]
id = "main-focus"
initial_member = "home-group"

[[focus.groups.members]]
key = "home-group"
platform = "qq"
kind = "group"
external_id = "123456789"
display_name = "主群"
allow_import = true
allow_export = true

[[focus.groups.members]]
key = "owner-private"
platform = "qq"
kind = "private"
external_id = "987654321"
display_name = "主人私聊"
allow_import = true
allow_export = false
```

每个成员对应的 ChatStream 必须已经被 NachoBot 记录过；建议先保持 `mode = "off"`，在每个目标群聊和私聊各收一条消息，再改为 `active` 并重启。默认 `membership_migration = "idle_safe"`：组空闲时自动迁移新增、移除及成员权限变化，保留仍在组内的 cursor，新成员从最新消息建立基线，移除成员删除其 Focus cursor；若 active 成员被移除，则原子回退到 `initial_member`。`strict` 拒绝全部变化，`additive` 仅允许纯新增。任何模式遇到 pending 事件、active handoff 或保留中的投递时都拒绝迁移。

Focus Gate 在单个 turn 内最多尝试 `bypass_gate_max_attempts` 次。全部失败时不再重放整个 turn：带 `mentioned`/`at` 信号，或源/目标任一侧是 Bilibili、Discord VC、Universal VC 等 bypass Planner 会话的事件，在通过组策略校验后确定性降级为 `switch`；其他仅有普通 `unread` 的事件降级为 `stay`。bypass 边界规则双向生效，因此既能切入直播/语音会话，也能在 Gate 不可用时切出，同时不会绕过成员关系、导入导出或私聊安全返回规则。

## 重要语义

- `switch_chat` 是终止动作：选中后，本轮其余回复、工具和动作全部丢弃。
- Focus 管理的发送必须携带当前 turn lease；切换后的旧会话发送会得到 `STALE_LEASE`，不会到达适配器。
- 一条逻辑回复即使拆成多段也只结算一次 handoff；部分送达按成功结算，全失败才释放。
- HeartFlow 中所有携带 Focus 事件的 turn 都先由轻量 Gate 路由，输出域只有 `stay`/`switch`；Bilibili/Discord VC/Universal VC 仍跳过完整 Planner。
- Gate 不接收目标 stream、revision、epoch 或策略版本；`switch` 继续由服务端事件解析、策略校验和 CAS 执行。
- Gate 超时或输出非法时仅在同一 turn 内做有界重试；耗尽后使用上述确定性降级，不再无限重放。纯事件唤醒选择 `stay` 时不会对历史消息重复回复。
- Gate 选择 `stay` 后，普通 Planner 看不到 `<focus_events>`，也不能输出 `switch_chat`；它只负责当前会话的普通动作。
- 标准 Planner 会话的纯 Focus event 唤醒也使用同一个轻量 Gate，不再允许 Planner 从完整历史记录中挑选旧消息回复。
- 执行层对纯事件 turn 还有独立围栏：即使 Gate 被关闭或 Planner 输出违规动作，也只允许 `switch_chat`，其余回复、工具和插件动作全部丢弃。
- handoff 有 TTL、成功回复周期上限和 prompt 大小上限；它不是长期记忆，也不会写入人物关系或知识库。
- handoff 由服务端记录源成员配置的 `display_name`，并携带源会话最新最多 10 条消息；Replyer 中以“源会话名称”和“源会话近期内容”显示。
- 群聊 handoff 进入私聊前仍应避免收集不必要的原文。默认只传任务摘要、已知事实、待办和近期结果，并把所有内容视为不可信用户输入。

## 运维边界

v1 的私聊是内容导出的终点，因此不会把私聊 handoff 带回群聊。组内群聊出现待处理事件时，私聊 Planner 只能看到不含消息正文的安全返回事件，并可通过无 handoff 的 `switch_chat` 返回。普通插件或定时任务若直接向 Focus 管理的会话发送且没有当前 lease，会被拒绝；这类来源应接入 Focus 的系统事件桥，而不是绕过协调器。
