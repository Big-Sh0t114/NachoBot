# NachoBot Koishi Adapter

把 Koishi 的 OneBot v11 消息桥接到 NachoBot，主要用于 Discord 等文字平台。Discord 语音频道由相邻的 `NachoBot-DiscordVC-Adapter` 处理。

## 快速开始

1. 在 `koishi-app` 中配置平台账号，并启用 OneBot Server。
2. 编辑本目录的 `config.toml`：
   - `[onebot_server].ws_url` 与 Koishi 的监听地址一致，默认 `ws://127.0.0.1:5140/onebot/v11/ws`。
   - `[nachobot_server]` 指向 Multimodal Adapter，默认 `127.0.0.1:8070`。
   - 在 `[chat]` 中配置群聊、私聊与用户过滤。
3. 启动：

```bash
uv sync --locked
uv run python main.py
```

也可在仓库根目录运行 `launch_discord.bat`，同时启动 Koishi、文字适配器与 DiscordVC。

## 图片与音频

入站图片 prompt 固定维护在 `visual_policy.py` 的 `KOISHI_IMAGE_PROMPT`；`[visual.image]` 只保存 `temperature`、`max_tokens` 和 `extra_params`。prompt 或参数变化会形成新的缓存指纹，无需手动清理旧图片缓存。

音频转码默认由 `static-ffmpeg` 提供。只有使用系统安装或自定义构建时，才需要设置 `[ffmpeg].path`。

## Docker

```bash
docker network create nacho_bot
docker compose up -d
```

- Compose 通过 `additional_contexts` 读取相邻的 `NachoBot/ncnk_message`，无需复制消息包。
- 容器内的 Core/Multimodal 地址应使用服务名，不要使用指向容器自身的 `127.0.0.1`。
- Koishi 若不在 `nacho_bot` 网络中，应改用宿主机或其他可达地址。
- 修改挂载的 `config.toml` 后执行 `docker compose restart`。
