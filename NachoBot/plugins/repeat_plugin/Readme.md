# 复读插件

按聊天流检测连续重复的纯文本消息，并以设定概率自动复读。当前版本为 `1.0.0`，消息发送统一走 NachoBot 的适配器接口，无需额外配置 HTTP 端口。

## 行为

- 同一聊天流连续出现第三条完全相同的消息时尝试复读。
- 跳过通知、以 `[CQ:` 开头的图片或表情消息，以及机器人自己发送的消息。
- 同一内容成功复读后不会立即重复发送，避免形成循环。
- 复读前会将 `@<昵称:QQ号>` 简化为 `@昵称`。

## 配置

编辑插件目录下的 `config.toml`：

```toml
[plugin]
enabled = true

[repeat]
debug_mode = false
repeat_probability = 0.7
skip_probability = 0.3
```

- `repeat_probability`：通过跳过判定后执行复读的概率，范围 `0~1`。
- `skip_probability`：优先判定的不复读概率，范围 `0~1`。
- `debug_mode`：输出消息字段、聊天流和发送结果等调试信息。

插件放入 `plugins/repeat_plugin` 并启用后即可工作，无需命令触发。

许可证：MIT。
