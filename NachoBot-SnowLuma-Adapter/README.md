# NachoBot-SnowLuma-Adapter

面向 NachoBot `dev` 分支当前 Core（structured `system_event` 协议）的 SnowLuma QQ 适配器。

## 架构

```text
QQ <-> SnowLuma <-> 本适配器 <-> ncnk_message Router <-> NachoBot Core
```

本适配器不依赖 `maibot_sdk`。SnowLuma 一侧沿用上游适配器实际使用的协议：

- WebSocket 客户端连接 SnowLuma；默认 `ws://127.0.0.1:3001`
- token 通过 `?access_token=...` 传递
- 动作调用为 `{ "action": ..., "params": ..., "echo": ... }`
- 使用 `echo` 匹配动作响应
- 接收 OneBot 风格 `post_type=message/notice` 事件

NachoBot 一侧沿用现有 NapCat Adapter 的 Core 链路，直接连接 `ws://127.0.0.1:8000/ws`：

- `Router + RouteConfig + TargetConfig`
- `MessageBase / BaseMessageInfo / Seg`
- 平台名固定为 `qq`
- 发送成功后回写 `message_id_echo`
- SnowLuma 上下线时上报 `platform_status`

## 放置位置

建议直接解压到 NachoBot 仓库根目录，使目录结构为：

```text
Nacho-with-u/
├─ NachoBot/
├─ NachoBot-Napcat-Adapter/
└─ NachoBot-SnowLuma-Adapter/
```

这样 `main.py` 会自动复用相邻 `NachoBot/ncnk_message`，不需要单独发布 `ncnk_message` 包。

## 配置

编辑 `config.toml`：

```toml
[snowluma]
host = "127.0.0.1"
port = 3001
token = ""

[nachobot_server]
host = "127.0.0.1"
port = 8000
```

`nachobot_server.port` 直连 NachoBot Core，默认并要求使用 `8000`。适配器不会改写现有 `config.toml`；如 Core 设置了 token，适配器会通过 `NACHOBOT_CORE_TOKEN` / 当前 `ncnk_message` 的 `get_core_token_from_env()` 读取。

聊天准入默认保持 fail-closed：启用过滤时，`whitelist` 的空名单不会放行任何会话。
SnowLuma 当前配置的 `[chat]` 共享策略应与现有 NapCat Adapter 同步；WebUI
首次生成 SnowLuma 配置时只迁移这七个准入键，不会迁移 NapCat 的连接、令牌或
其他适配器设置。重新生成已有 SnowLuma 配置时会保留用户拥有的适配器表和键
（包括 `snowluma`、`nachobot_server`、`chat`、`send`、`visual`、`debug`），
向导只改写 `voice.use_tts`。

如希望先测试任意群，请明确使用黑名单模式：

```toml
group_list_type = "blacklist"
group_list = []
```

### 离线历史恢复

适配器登录并连接 Core 后，会主动通过 SnowLuma OneBot `get_friend_msg_history` 拉取私聊离线消息；SnowLuma 或 Core 重连后也会重试。首次启用只保存恢复基线，不会导入基线之前的旧消息。恢复使用当前私聊准入名单、用户封禁和现有消息过滤，保留 OneBot 原始时间，并等待 Core 的持久化回执后再推进游标。同步状态和待投递消息保存在适配器 `data/offline_history.sqlite3`。对于账号、好友路由、发送者和原始时间均验证通过的入站好友私聊，Core 还会登记 SnowLuma 的原生消息 ID；NapCat 可以用已验证的 QQNT `real_seq` 推导出同一个 ID。切换适配器后，Core 会恢复已有任务或回执完成状态，并保留本次投递使用的适配器 key/token。只按已验证的消息身份共享，不以文本或时间单独去重；两个适配器的分页游标、checkpoint 和 outbox 保持各自独立。群聊仍通过普通实时收发和 SnowLuma 平台自动补投处理，适配器不会主动请求群聊历史。

```toml
[offline_recovery]
enabled = true
page_size = 50
max_pages = 200
retry_delay_sec = 15.0
receipt_timeout_sec = 8.0
```

私聊恢复从 SnowLuma 服务端最新历史向旧消息分页。群聊不参与适配器主动历史补拉；群聊普通实时收发和平台自动补投保持不变。

共享身份仅适用于入站好友私聊。SnowLuma 不把 `message_seq` 当作 QQNT 原生序号；它使用服务端给出的带符号 32 位消息 ID。若旧 SnowLuma Core 行已有显式账号/路由/消息 ID 元数据，NapCat 仅在推导 ID 和原始时间完全匹配时才能接管；缺少这份证明时沿用适配器各自的历史处理。

NapCat 的已登记历史扫描还会用仅包含消息 ID、私聊路由、原始时间和验证后 QQNT 身份的限长记录，为已有 NapCat Core 回执补登记 SnowLuma 别名。Core 只接受被当前完整 run 准入的私聊目标，以及 key、账号、消息 ID、时间均精确匹配的 pending/complete 回执；它不会新增消息行或改变原回执状态。扫描经过本地 checkpoint 边界时也能为那条已接收记录补证，因此之后切到 SnowLuma 可直接识别；没有覆盖到或缺少 `real_seq` 证明的旧记录仍保持未确认。

该身份迁移不会删除或合并在获得精确证明前已经写入 Core 的重复消息行。

## 启动

使用 uv：

```powershell
cd NachoBot-SnowLuma-Adapter
uv sync
uv run python main.py
```

也可双击 `run.bat`（前提是已有环境依赖）。

### 与 NachoBot WebUI / 启动器集成

根目录 `NachoBot/.env` 的 `qq_adapter` 是 QQ 后端的唯一选择器：

```dotenv
qq_adapter=snowluma
```

可选值为 `napcat` 或 `snowluma`；省略该键会兼容地使用 NapCat。WebUI
向导和三个根目录启动器只会安装、启动当前选中的本地适配器。选择
SnowLuma 时，WebUI 不会启动或管理 SnowLuma 外部运行时，也不会执行
NapCat Shell 路径配置；请先在 SnowLuma 中开启 OneBot WebSocket 服务。
切换选择器前应先停止 WebUI 管理的 QQ 适配器进程。

仓库提供的 `template_config.toml` 仅用于配置生成；访问令牌不会显示在
WebUI 默认值、日志或状态详情中。

## 日志与安全

生命周期、连接、动作完成和 Core handoff 使用 `INFO`；边界分类、段转换和
echo 关联使用 `DEBUG`；只有显式打开 `debug.raw_payload` 或
`debug.raw_outbound` 时才会在 `TRACE` 输出递归脱敏、限长的原始摘要。默认
日志不会输出完整消息、动作参数、媒体正文、cookie 或凭据。预期的准入丢弃、
降级、重试和超时使用 `WARNING`，必需边界失败使用 `ERROR`。日志包含模块、
函数和行号，并保留单独的 `ncnk_message` Core 路由。

## 已实现

- SnowLuma WebSocket 自动重连与 token 验证
- OneBot action/echo Future 池
- 群聊 / 私聊文本
- @、reply、QQ face
- 图片 / 动画表情下载并转 NachoBot base64 segment
- 语音 `get_record(file, out_format=wav)` 路径
- 视频 / 文件元数据透传
- QQ notice 转 structured `system_event`：poke、禁言/解除禁言、撤回、成员/管理员/名片/头衔等群系统事件
- 系统事件严格 senderless：`user_info=None`，操作者仅进入 `system_event.actor`
- 私聊系统事件使用 `system_event_route` 定位私聊 ChatStream
- 与 dev NapCat Adapter 对齐的 poke 快速回戳结果写入 `system_event.data.fast_poke`
- `ban_qq_bot` 准入配置与 NapCat Adapter 对齐
- Core -> QQ 文本、图片、表情、语音、视频、音乐、回复
- `upload_group_file/upload_private_file` 文件上传
- `send_group_forward_msg/send_private_forward_msg` 合并转发发送
- Core command：禁言、全体禁言、踢人、戳一戳、撤回、AI 语音、消息表情、群头衔
- `message_id_echo`
- `platform_status`
- NachoBot 视觉策略参数兼容

## 当前边界

1. 入站合并转发目前仍降级为文本占位；出站合并转发使用 OneBot forward action。
2. 复杂 JSON/XML 卡片当前降级为文本占位。
3. 本包不捆绑 SnowLuma 运行时；SnowLuma 应独立启动并开启 OneBot WebSocket 服务端。
4. v0.2.0 要求相邻 Core 来自包含 `ncnk_message.build_system_event/build_system_event_route` 的 `dev` 协议版本；旧 Core 不兼容。

## 协议基准

v0.2.0 构建时按以下协议基准对齐：

- `Big-Sh0t114/NachoBot` `dev` @ `fab9fdd8b371008e07bd736e05888cbbaf958818`：`NachoBot-Napcat-Adapter`、`NachoBot/ncnk_message/system_event.py` 与 Core system-event ingress
- `SnowLuma/SnowLuma` v1.14.x：OneBot WebSocket/action/event 以及 NapCat-parity action
