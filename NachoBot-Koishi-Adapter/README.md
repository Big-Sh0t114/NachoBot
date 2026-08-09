# NachoBot Koishi Adapter

NachoBot 的 Koishi 适配器，允许 NachoBot 通过 Koishi 接入多种平台（如 Discord 文字频道等）。

## 配置说明

1. 启动 `koishi-app`
2. 复制并重命名 `config.toml` 配置为适配器所用
3. 确保配置中的 `onebot_server` 信息与 Koishi OneBot 插件的监听地址一致
4. 音频转码默认由 `static-ffmpeg` 自动获取 FFmpeg；通常无需设置 `[ffmpeg].path`

如需使用系统安装或自定义构建，可在 `config.toml` 中把 `[ffmpeg].path` 设置为 FFmpeg 可执行文件或其所在目录。留空时使用 `static-ffmpeg`。

## 图片视觉策略

Koishi 入站图片 prompt 固定维护在 `visual_policy.py` 的
`KOISHI_IMAGE_PROMPT`，不会从 TOML 读取。`[visual.image]` 只保留
`temperature`、`max_tokens` 和 `extra_params` 等推理参数。
适配器通过消息的 `visual_policy` 元数据把代码中的 prompt 与这些参数交给
Core 执行。prompt 或参数变化后，策略指纹会自动形成新的缓存命名空间，
无需清理旧图片缓存。

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
- 该服务会自动连接到名为 `nacho_bot` 的独立 docker 网络。
- `config.toml` 等文件会被挂载到容器中，如果修改了配置，请执行 `docker compose restart` 使其生效。
- **关联服务地址**：在 `config.toml` 中，如果你需要连接到其他 Docker 容器（如核心的 `8000` 端口），请将 `127.0.0.1` 替换为对应的服务名（如 `core`）。如果 Koishi 没有运行在当前的 Docker 网络中，请为其指定宿主机 IP 或者是相应的互相可达的地址。
- 构建镜像时通过 Compose 的 `additional_contexts` 从相邻的 `NachoBot/ncnk_message` 注入核心消息包，不需要在本目录复制一份。
