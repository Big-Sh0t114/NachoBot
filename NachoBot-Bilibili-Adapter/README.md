# NachoBot-Bilibili-Adapter

将 NachoBot 桥接到哔哩哔哩（Bilibili）直播弹幕与评论回复系统。

## 功能特性
- 多直播间弹幕接收 / 发送（支持弹幕回复）。
- 评论回复发送（支持所有评论类型）。
- 回复通知轮询与转发。
- 私信轮询与发送，并可通过 Core 处理私信图片。
- 本地麦克风连续 VAD / PTT 流式语音识别。
- 直播场景的二阶段联网查询在适配器内完成，不再向 Core 注入 Bilibili 专属搜索链路。
- 活动窗口截图 VLM 与独立 Live2D WebSocket 联动。
- 二维码登录辅助工具，用于更新 `config.toml` 中的 Cookies。

## 安装与运行
1. 安装 [uv](https://docs.astral.sh/uv/) 并同步依赖：
   - `uv sync`
2. 编辑 `config.toml`，填写 `SESSDATA`、`bili_jct`、`buvid3` 以及直播间 ID。
3. 如需 Live2D，设置 `enable_live2D = true`；根目录的 `launch_bilibili.bat` 会复用或启动独立 Live2D Adapter。
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

## 实时画面 VLM

实时画面的 system/user prompt 固定维护在
`bili_src/visual_policy.py` 的 `BILIBILI_SCREEN_*` 常量中，不写入 TOML。
`[live.screen_monitor.vlm]` 只管理预处理与模型执行参数：

- `max_image_dimension`、`jpeg_quality`：截图预处理设置。
- `models`：按顺序尝试的模型及各自的 `max_tokens`、`temperature`、
  超时、重试和 `extra_params`。例如 Qwen 可在这里关闭思考。

模型名称、模型标识、服务地址和 API Key 仍统一注册在
`NachoBot/config/model_config.toml`；本适配器只引用模型名称，不复制连接信息。
普通 VLM 会消费上面的自然语言 prompt；Florence-2 只支持它定义的任务 token，
其服务端固定使用 `<MORE_DETAILED_CAPTION>`、256 输出 token 和 3 beams，
适配器配置不会覆盖这项详细描述策略。
若没有配置 `live.screen_monitor.vlm.models`，当前版本会临时回退读取旧的
核心 VLM 任务组并输出迁移警告。

```toml
[live.screen_monitor.vlm]
max_image_dimension = 896
jpeg_quality = 75
message_max_chars = 300
models = [
  { name = "qwen/qwen3-vl-4b", max_tokens = 128, temperature = 0.1, timeout = 20, max_retry = 1, extra_params = { enable_thinking = false } },
  { name = "Florence-2", timeout = 15, max_retry = 1 },
]
```

## 私信图片 VLM

Bilibili 私信图片 prompt 固定在 `bili_src/visual_policy.py` 的 `BILIBILI_PRIVATE_IMAGE_PROMPT`；
`[private_message.visual.image]` 仅保存
`temperature`、`max_tokens` 和 `extra_params`。适配器会把代码中的 prompt 与
推理参数通过 `visual_policy` 交给 Core。修改代码 prompt 或参数后缓存指纹会
自动变化，无需清理旧图片缓存。

`[live.screen_monitor.vlm]` 与它相互独立：前者服务直播活动窗口截图，
后者只服务用户私信图片，各自在适配器代码中维护场景 prompt。

## 麦克风流式 ASR

开启麦克风 ASR 后，采集到的 PCM 会按 100ms 音频块持续送入
Multimodal-Adapter 的共享 `StreamingASR`。连续模式由 VAD 控制流的开始与
结束，PTT 模式在按键释放时结束流；句末只读取最终结果，不会再生成整段 WAV
或请求 `/audio/transcriptions`。ASR 模型、CPU provider 和线程数统一由
`NachoBot-Multimodal-Adapter/configs/perception.toml` 管理。

## 私信（Private messages）
- `private_message.sessions`：用于固定（pin）指定的会话（talker ID）。
- 设置 `private_message.auto_sessions = true` 可自动轮询所有最近会话（推荐用于“任意用户”私信场景）。
- `private_message.auto_session_types` 默认值为 `4`（表示所有会话类型）。
- `private_message.auto_session_refresh_seconds` 控制会话列表的刷新频率（秒）。

## 二维码登录
- 运行：`uv run python qr_login.py`
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
- **共享 ASR**：构建时会从相邻的 `NachoBot-Multimodal-Adapter` 复制共享 ASR 源码与配置；首次运行时若模型不存在，会按 `auto_download` 配置下载 CPU INT8 模型。
- **屏幕监控**：本适配器的 Docker 镜像使用 `Xvfb` 支持无显示设备环境下的 `mss` 截屏。
- **Live2D**：渲染不再由本容器负责。请单独部署 `NachoBot-Live2D-Adapter`，并将 `live.live2d_url` 指向其可访问的 WebSocket 地址；容器内不能使用 `127.0.0.1` 访问宿主机上的独立适配器。
- **配置文件**：容器挂载使用宿主机的 `config.toml` 和日志目录，修改配置后重启容器即可生效。
- **构建上下文**：Compose 通过 `additional_contexts` 注入相邻的 `NachoBot/ncnk_message`、多模态源码、配置和模型目录；核心配置目录以只读卷挂载。
- **Linux 音频**：Linux 容器不提供 Windows `winsound`，因此本地播放回退会跳过；请配置远程 Live2D 播放，或在宿主机运行本适配器以使用本地音频设备。
- **核心连接**：确保 `NachoBot` 核心服务已启动在 `nacho_bot` 网络中，且 `config.toml` 里的 `nachobot_server.host` 设置为 `core`（或宿主机 IP）。
