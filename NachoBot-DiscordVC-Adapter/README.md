# NachoBot DiscordVC Adapter

NachoBot 的 Discord 语音频道适配器，支持在 Discord 语音频道中进行实时语音对话。

## 功能

- Discord 语音频道实时语音对话
- 语音识别（ASR）
- TTS 语音合成回复
- 自动 FFmpeg 路径检测
- 代理支持

## 配置

复制 `config.toml.example` 为 `config.toml` 并填写以下配置：

- `discord.token`：Discord Bot Token
- `discord.app_id`：Discord Application ID
- `discord.proxy_url`：代理地址（如需）
- `nachobot.host` / `nachobot.port`：NachoBot 核心地址

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

> **网络依赖**：Docker 构建过程中需要通过网络下载 `ffmpeg`、`libsodium-dev` 等系统依赖。如果你的部署环境有网络限制（如在国内服务器上构建），请确保：
> - Docker 配置了可用的镜像加速器或代理
> - 或使用预构建的镜像

> **Discord 代理**：在 Docker 环境中，Discord Bot 仍需要网络代理才能连接到 Discord 服务器。请在 `config.toml` 中配置 `proxy_url`，并确保容器可以访问该代理。Docker 环境下 `proxy_url` 中的 `127.0.0.1` 应改为宿主机的实际 IP 或使用 `host.docker.internal`。

## 本地运行

```bash
uv sync --locked
python main.py
```
