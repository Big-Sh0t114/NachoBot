# 消息撤回插件

在消息进入后续处理前检查文本和图片，命中规则时通过适配器撤回消息。当前版本为 `1.0.0`。

## 功能

- 使用 Python 正则表达式匹配违规文本。
- 使用 256-bit dHash 比对违规图片和表情，对轻微缩放、重压缩或格式转换有一定容忍度。
- 支持 QQ 白名单和可选的撤回提示语。
- 将命中的规则、图片文件名、次数和触发用户记录到 `data/moderation_stats.json`。

## 配置

编辑插件目录下的 `config.toml`：

```toml
[plugin]
enabled = true

[moderation]
ban_regex_list = ["违规词", "广告.*链接"]
whitelist_qq = ["123456789"]
banned_images_dir = "data/banned_images"
recall_message = "检测到违规内容，已自动撤回"
```

- `ban_regex_list`：逐条执行的正则列表；无效正则会记录错误并跳过。
- `whitelist_qq`：完全跳过检查的 QQ 号列表，字符串或数字均可。
- `banned_images_dir`：违规图片样本目录，相对路径以 NachoBot 运行目录为基准。
- `recall_message`：撤回后发送的提示语；设为空字符串可关闭提示。

图片样本首次检查时载入内存，增删样本后需重启 NachoBot 才会生效。撤回还要求消息包含 `stream_id`、`message_id`，且当前适配器支持 `DELETE_MSG` 命令。

许可证：GPL-3.0-or-later。
