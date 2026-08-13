# Bilibili 视频发送插件

识别 Bilibili 视频链接、`b23.tv` 短链，以及 QQ 中的 Bilibili 小程序/分享卡片，下载后通过 NapCat 发送视频。群聊和私聊均可用。

## 安装与配置

1. 在 `NachoBot` 目录执行 `uv sync --locked`。
2. 编辑本目录的 `config.toml`，先保持 `plugin.enabled = false` 完成配置和测试，再按需启用。
3. 如需登录画质，在 `[bilibili]` 中填写自己的 `sessdata` 和可选 `buvid3`；不要提交 Cookie。
4. `[api].port` 必须与 NapCat 中为本插件配置的正向 HTTP Server 端口一致。

```toml
[plugin]
enabled = true

[bilibili]
max_video_duration_minutes = 10 # 0 表示关闭时长预检
enable_video_splitting = true
enable_video_compression = true
compression_quality = 28

[wsl]
enable_path_conversion = false

[api]
port = 5700
```

如果不了解 WSL，请保持 `enable_path_conversion = false`。

## 当前处理链路

- 先解析 BV/AV/短链或 NapCat 已解析的卡片信息。
- 在下载前按 `max_video_duration_minutes` 拒绝过长视频。
- 优先 DASH，按账号权限选择画质；文件过大时可压缩或分片。
- FFmpeg / FFprobe 由 `static-ffmpeg` 获取，不再随插件提交整套二进制目录。
- 可自动检测 NVENC、QSV、AMF、VideoToolbox，失败时回退 `libx264`。

Cookie 属于账号凭据；日志、截图和问题反馈中都应先脱敏。
