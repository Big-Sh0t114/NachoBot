# 本地画作插件（Artwork）

用户明确提出“发张图 / 看看画作”等请求时，Planner 可从本地画夹随机发送一张图片。仅讨论绘画或表达“我想画画”不会触发。

## 配置

```toml
[components]
enable_send_artwork = true

[artwork]
directory = "artwork"
allowed_extensions = [".png", ".jpg", ".jpeg", ".gif", ".webp"]

[access_control]
discord_artwork_whitelist = ["Discord用户ID"]
```

相对路径以 `NachoBot` 运行目录解析。Discord 群聊不发送画作，Discord 私聊仅对白名单用户开放；其他平台按各自图片发送能力处理。
