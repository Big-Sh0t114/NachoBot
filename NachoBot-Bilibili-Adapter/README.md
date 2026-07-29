# NachoBot-Bilibili-Adapter

将 NachoBot 桥接到哔哩哔哩（Bilibili）直播弹幕与评论回复系统。

## 功能特性
- 多直播间弹幕接收 / 发送（支持弹幕回复）。
- 评论回复发送（支持所有评论类型）。
- 回复通知轮询与转发。
- 私信轮询与发送（仅支持文本）。
- 二维码登录辅助工具，用于更新 `config.toml` 中的 Cookies。

## 安装与运行
1. 安装 [uv](https://docs.astral.sh/uv/) 并同步依赖：
   - `uv sync`
2. 编辑 `config.toml`，填写 `SESSDATA`、`bili_jct`、`buvid3` 以及直播间 ID。
3. 启动独立的 `NachoBot-Live2D-Adapter`，或在工作区根目录运行 `launch_bilibili.bat` 由脚本自动启动。
4. 单独启动本适配器：
   - `uv run python main.py`

## Live2D 远程适配器
Live2D 渲染已拆分到独立的 `NachoBot-Live2D-Adapter`，本项目只通过 WebSocket 发送平台无关的状态、情感和动作事件。

`[live]` 下仅保留以下配置：
- `enable_live2D`：是否启用远程 Live2D 连接。
- `live2d_url`：独立适配器地址，默认 `ws://127.0.0.1:8766`。
- `live2d_token`：可选鉴权令牌，须与独立适配器配置一致。
- `live2d_reconnect_seconds`：连接中断后的重试间隔。

当 `enable_live2D = false` 时，Bilibili 适配器不会连接 Live2D，即使独立 Live2D 已由用户手动启动。设为 `true` 后，`launch_bilibili.bat` 会优先复用已经监听的 Live2D 实例，只在实例尚未运行时启动一个新实例。

模型路径、窗口尺寸、透明背景、抗锯齿、缩放、鼠标追踪和动作映射均由 `NachoBot-Live2D-Adapter/config.toml` 管理。

## 兼容性设置
- 设置 `compat.disable_video_sender_plugin = true`，以避免在核心中触发 Bilibili 视频发送插件。
- 设置 `compat.disable_command_trigger = true`，以防止 Bilibili 消息触发指令。

## 直播回复提示词（Live reply prompt）
- 设置 `live.reply_prompt` 可覆盖直播间回复提示词（使用 `replyer_prompt` 模板名）。
- 设置 `live.planner_prompt` 可覆盖在核心日志中显示的规划器提示词（使用 `planner_prompt` 模板名）。
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
- **屏幕监控**：本适配器的 Docker 镜像使用 `Xvfb` 支持无显示设备环境下的 `mss` 截屏。
- **Live2D**：渲染不再由本容器负责。请单独部署 `NachoBot-Live2D-Adapter`，并将 `live.live2d_url` 指向其可访问的 WebSocket 地址；容器内不能使用 `127.0.0.1` 访问宿主机上的独立适配器。
- **配置文件**：容器挂载使用宿主机的 `config.toml` 和日志目录，修改配置后重启容器即可生效。
- **核心连接**：确保 `NachoBot` 核心服务已启动在 `nacho_bot` 网络中，且 `config.toml` 里的 `nachobot_server.host` 设置为 `core`（或宿主机 IP）。
