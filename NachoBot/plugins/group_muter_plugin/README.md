# 群聊静音插件（GroupMuterPlugin）

让 Bot 在指定群聊中临时或永久保持静默。状态按“平台 + 群号”隔离；到期自动恢复，管理员也可通过配置的解除关键词或 `@Bot` 提前恢复。

## 命令

| 命令 | 作用 |
| --- | --- |
| `#mute` | 使用 `duration_seconds` 的默认时长 |
| `#mute_<秒数>` | 静音指定秒数 |
| `#mute_true` | 永久静音 |
| `#mute_false` | 按默认配置解除静音 |

参数化静音、永久静音和解除命令会检查 `user_control`；命令只在群聊中生效。静音期间非管理员消息被拦截，相关日志会合并降噪。

## 配置

```toml
[plugin]
enabled = true

[mute]
duration_seconds = 300
unmute_keywords = ["#mute_false"]
enable_unmute = true
at_mention_break = true

[user_control]
list_type = "whitelist"
list = ["管理员QQ号"]
```

白名单为空时没有受信任管理员；`blacklist` 模式表示列表之外的用户有权限，使用前请确认这符合你的安全预期。

本插件基于 [silent_mode_plugin](https://github.com/khiqwq/silent_mode_plugin) 二次开发，采用 AGPL-3.0。
