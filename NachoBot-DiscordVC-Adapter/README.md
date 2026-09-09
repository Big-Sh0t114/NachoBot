# NachoBot DiscordVC Adapter

NachoBot 的 Discord 语音频道适配器，支持在 Discord 语音频道中进行实时语音对话。

Discord 文字消息由 Koishi Adapter 负责；本目录只处理语音频道、共享 ASR 与 TTS 播放。

## 功能

- Discord 语音频道实时语音对话
- 多用户逐块流式语音识别（ASR）
- TTS 语音合成回复
- 通过 `static-ffmpeg` 自动获取并使用 FFmpeg
- 代理支持

## 配置

复制 `config.toml.example` 为 `config.toml` 并填写以下配置：

- `discord.token`：Discord Bot Token
- `discord.app_id`：Discord Application ID
- `discord.proxy_url`：代理地址（如需）
- `nachobot.host` / `nachobot.port`：NachoBot 核心地址

推荐在仓库根目录运行 `launch_discord.bat`，一次启动 Koishi、文字适配器和 DiscordVC；单独调试本适配器时再使用文末命令。

语音包在说话期间持续送入
`NachoBot-Multimodal-Adapter/src/asr/streaming.py`，VAD 结束时只读取
当前流的最终文本，不再拼接 WAV 后调用 `/audio/transcriptions`。
模型、CPU provider 和线程数统一由 Multimodal-Adapter 的
`configs/perception.toml` 管理；本适配器只使用 `[voice].enabled` 作为启停开关。

## Docker 部署

### 构建镜像

```bash
docker compose build
```

### 启动服务

```bash
# 确保 NachoBot 核心已启动且 nacho_bot 网络已创建
docker compose up -d
```

### 注意事项

> **共享 ASR**：构建上下文会从相邻的
> `NachoBot-Multimodal-Adapter` 复制共享 ASR 源码与配置；首次运行时若模型
> 不存在，会按 `auto_download` 配置下载 CPU INT8 模型。

> **构建上下文**：Compose 还会从相邻的 `NachoBot` 注入 canonical
> `ncnk_message` 包；核心配置和多模态模型目录通过运行时卷挂载。

> **网络依赖**：Docker 构建过程中需要通过网络下载 `ffmpeg`、`libsodium-dev` 等系统依赖。如果你的部署环境有网络限制（如在国内服务器上构建），请确保：
> - Docker 配置了可用的镜像加速器或代理
> - 或使用预构建的镜像

> **Discord 代理**：在 Docker 环境中，Discord Bot 仍需要网络代理才能连接到 Discord 服务器。请在 `config.toml` 中配置 `proxy_url`，并确保容器可以访问该代理。Docker 环境下 `proxy_url` 中的 `127.0.0.1` 应改为宿主机的实际 IP 或使用 `host.docker.internal`。

## 本地运行

```bash
uv sync
uv run python main.py
```

本地运行时无需手动安装 FFmpeg。首次播放语音时，`static-ffmpeg` 会自动获取当前平台对应的 FFmpeg 可执行文件。Docker 镜像仍使用系统包形式安装 FFmpeg。
