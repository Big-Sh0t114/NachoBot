# NachoBot-Bilibili-Adapter

将 NachoBot 桥接到哔哩哔哩（Bilibili）直播弹幕与评论回复系统。

## 功能特性
- 多直播间弹幕接收 / 发送（支持弹幕回复）。
- 评论回复发送（支持所有评论类型）。
- 回复通知轮询与转发。
- 私信轮询与发送（仅支持文本）。
- 二维码登录辅助工具，用于更新 `config.toml` 中的 Cookies。

## 安装与运行
1. 安装依赖：
   - `pip install -r requirements.txt`
2. 编辑 `config.toml`，填写 `SESSDATA`、`bili_jct`、`buvid3` 以及直播间 ID。
3. 运行：
   - `python main.py`

## 兼容性设置
- 设置 `compat.disable_video_sender_plugin = true`，以避免在核心中触发 Bilibili 视频发送插件。
- 设置 `compat.disable_command_trigger = true`，以防止 Bilibili 消息触发指令。

## 直播回复提示词（Live reply prompt）
- 设置 `live.reply_prompt` 可覆盖直播间回复提示词（使用 `replyer_prompt` 模板名）。
- 设置 `live.planner_prompt` 可覆盖在核心日志中显示的规划器提示词（使用 `planner_prompt` 模板名）。
- `live.room_prompts."<room_id>"` 支持填写本场直播计划（注入 replyer）：
  - `live_category`：直播分类
  - `live_title`：直播标题
  - `live_content`：直播内容
  - `live_detail`：直播细节
  - 若四项均为空，则不进行注入。
- 如需换行，可使用 TOML 多行字符串（`"""..."""`）。
- `live.ws_proxy` 用于控制 WebSocket 代理：
  - `auto`：使用环境变量中的代理
  - `none`：禁用代理
  - 或直接指定代理 URL
- `live.open_timeout`：WebSocket 打开超时时间（秒），用于避免在受限网络下长时间卡住。
- `live.max_hosts`：限制从 `host_list` 中尝试的主机数量（0 表示不限制）。
- `live.max_attempts`：每次运行的最大连接尝试次数（0 表示不限制）。
- `live.proxy_pool_path`：代理池 JSON 文件路径（默认 `proxy.json`）。
- `live.proxy_check_url`：用于校验代理可用性的 URL。
- `live.proxy_check_timeout`：代理校验超时时间（秒）。
- 当 `live.ws_proxy = "pool"` 时，每次连接尝试都会轮换到下一个代理。

## 私信（Private messages）
- `private_message.sessions`：用于固定（pin）指定的会话（talker ID）。
- 设置 `private_message.auto_sessions = true` 可自动轮询所有最近会话（推荐用于“任意用户”私信场景）。
- `private_message.auto_session_types` 默认值为 `4`（表示所有会话类型）。
- `private_message.auto_session_refresh_seconds` 控制会话列表的刷新频率（秒）。

## 二维码登录
- 运行：`python qr_login.py`
- 该脚本会将 `SESSDATA`、`bili_jct` 和 `DedeUserID` 写入 `config.toml`。

## 指令用法
参见 `command_args.md`。

---

## Docker 部署

本适配器支持 Docker 容器化部署。

### 前置准备

确保已经创建了 NachoBot 的共享外部网络：

```bash
docker network create nacho_bot
```

### 启动服务

```bash
# 在本目录下执行
docker compose up -d
```

### Docker 注意事项
- **屏幕监控**：本适配器的 Docker 镜像已内置 `Xvfb`（虚拟帧缓冲），因此即使在无显示设备的 Linux 服务器上，也能正常运行依赖于屏幕渲染（mss 截屏 / Live2D）的功能。
- **配置文件**：容器会通过挂载使用宿主机的 `config.toml`、`resources/`、`logs/` 等路径，修改配置后重启容器即可生效。
- **核心连接**：确保 `NachoBot` 核心服务已启动在 `nacho_bot` 网络中，且 `config.toml` 里的 `nachobot_server.host` 设置为 `core`（或宿主机IP）。
