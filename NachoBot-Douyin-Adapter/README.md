# NachoBot Douyin Adapter

通过抖音开放平台官方能力接入直播互动和一对一私信。直播回复输出到字幕、TTS 和 Live2D；私信回复直接通过官方 IM OpenAPI 发回用户，两条链路互不混用。

本适配器不会使用网页抓取、Cookie 或逆向 WebSocket，也不会模拟账号发送直播弹幕。它只接收已申请的官方数据回调。

## 当前功能

- 官方 `live_comment`、`live_gift`、`live_like`、`live_fansclub` HTTP 回调。
- 按抖音官方规则进行 MD5 + Base64 回调验签。
- 时间戳防重放、消息 ID 去重、快速 ACK 和异步消费队列。
- 将直播观众映射为 `douyin.live` 用户和直播间群聊，接入 NachoBot 人格与上下文。
- AI 回复写入 UTF-8 字幕文件，适合 OBS 文本源读取。
- 调用 `NachoBot-Multimodal-Adapter` 的 `/api/tts` 生成并播放 WAV。
- 可选向独立 Live2D Adapter 发送观看、回复和口型状态。
- 官方启动/停止数据推送任务的小工具。
- 官方私信 Webhook：`im_receive_msg` 接入 NachoBot 独立私聊会话。
- `SHA1(Webhook AppSecret + 原始请求体)` 验签、`verify_webhook` 挑战响应和消息去重。
- AI 文本通过 `POST https://open.douyin.com/im/send/msg/` 回复原用户。
- 忽略 `im_send_msg`，避免机器人回复自己造成循环。
- 回复上下文过期检查；私信不会进入直播字幕、TTS 或 Live2D。

## 官方前置条件

1. 在抖音开放平台创建“直播小玩法”。
2. 申请直播间评论、礼物、点赞或粉丝团互动数据能力。
3. 配置公网 HTTPS 回调地址，例如：
   `https://your-domain.example/douyin/live/callback`
4. 直播时必须挂载对应玩法，之后分别为每一种数据类型启动推送任务。

抖音要求回调在约 2 秒内返回 2XX。本适配器收到并验证数据后立即放入内存队列，不会等待大模型回复。

## 配置

```powershell
Copy-Item config.example.toml config.toml
```

编辑 `config.toml`：

- `douyin.app_id`：直播玩法 ID。
- `douyin.room_id`：当前直播间 ID。
- `douyin.callback_secret`：数据推送配置中的回调签名密钥。
- `douyin.tasks.access_token`：启动/停止推送任务使用的 token。
- `nachobot.host/port`：NachoBot Core 的 WebSocket 地址，默认 `127.0.0.1:8000`。
- `tts.url`：多模态适配器 TTS HTTP 地址，默认 `http://127.0.0.1:8070/api/tts`。
- `output.subtitle_file`：OBS 可读取的当前回复字幕文件。

密钥配置文件已被 `.gitignore` 排除，不要提交 `config.toml`。

## 私信自动回复配置

先在抖音开放平台为小程序经营者申请“发送私信”能力，并在“开发配置 -> Webhook”中订阅“接收私信消息事件”。公网回调地址填写：

```text
https://你的域名/douyin/im/webhook
```

在 `config.toml` 填写：

```toml
[im]
enabled = true
callback_path = "/douyin/im/webhook"
client_key = "小程序 AppID"
webhook_secret = "Webhook 页面显示的 AppSecret"
access_token = "经营者授权后获得的 access_token"
operator_open_id = "该经营者抖音号的 open_id"
```

当前实现仅自动处理用户发来的文本私信。图片、视频、表情会正常 ACK，但不会交给模型回复。抖音对回复消息有时效和数量限制；程序默认只使用最近一条入站消息的上下文，并在 24 小时后拒绝发送。

## 启动

先启动 NachoBot Core，以及按需启动 TTS 和 Live2D，然后在项目根目录双击：

```text
launch_douyin.bat
```

或手动运行：

```powershell
cd NachoBot-Douyin-Adapter
uv sync --python ">=3.11,<3.14"
uv run python main.py
```

健康检查：`GET http://127.0.0.1:8788/health`。返回值中的 `im_enabled` 可确认私信功能是否启用。

## 启动数据推送任务

每种事件需要分别启动：

```powershell
uv run python scripts/task_control.py start live_comment
uv run python scripts/task_control.py start live_gift
uv run python scripts/task_control.py start live_like
uv run python scripts/task_control.py start live_fansclub
```

结束直播前可将 `start` 换成 `stop`。

## 本地回调测试

仅在本机测试时，可临时设置：

```toml
[douyin]
allow_unsigned_local = true
```

然后发送模拟评论：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8788/douyin/live/callback `
  -Headers @{ 'x-msg-type'='live_comment'; 'x-roomid'='local-room' } `
  -ContentType 'application/json' `
  -Body '[{"msg_id":"local-1","sec_openid":"user-1","nickname":"测试观众","content":"主播你好","timestamp":1710000000000}]'
```

生产环境必须恢复 `allow_unsigned_local = false`，并把服务放在 HTTPS 反向代理之后。

## 输出边界

目前官方直播数据能力负责把观众互动推送给开发者，并不等价于允许机器人账号自动发送普通直播弹幕。因此第一版将 AI 回复用于主播 TTS、OBS 字幕和 Live2D 表演，这也是合规、稳定的虚拟主播方式。
