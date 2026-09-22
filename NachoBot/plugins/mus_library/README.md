# 本地曲库插件（Mus Library）

从核心目录的 `music/` 读取标准 WAV 文件，通过核心平台媒体能力播放。把 MP3 仅改名为 `.wav` 无效。

## 使用

- `点歌 <关键词>`、`播放 <关键词>`、`来首 <关键词>`：按歌名、作者和别名模糊匹配。
- `#mus_rand`：随机播放一首。
- 新增 WAV 会自动登记到 `music_library.json`；需要别名或作者时可手动编辑该文件。
- 未命中的请求会去重后写入 `list.txt`，便于后续补充曲库。

## 配置

```toml
[plugin]
enable = true
prefer_silk = true
silk_bitrate = 24000
cache_ttl_hours = 0 # 0 关闭缓存过期；按需设置小时数
debug_timing = false
```

默认优先编码为 SILK 并使用磁盘缓存；失败时会尝试核心提供的嵌入式语音或本地文件媒体能力，并等待平台确认。依赖已纳入 Core 的 `pyproject.toml`，在 `NachoBot` 目录执行 `uv sync --locked` 即可。
