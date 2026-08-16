# 信使插件（Messenger）

让 Planner 在用户明确要求“告诉某人 / 问问某人 / 帮我传话”时，把内容转告到目标用户的 QQ 私聊。

## 当前行为

- 仅在 QQ 平台执行；按用户名、昵称进行模糊匹配。
- 目标用户必须已经与 Bot 建立过私聊记录。
- 转告前会显示匹配到的名称和 QQ，且只接受本次发起者在超时前给出的确认；群内其他人不能代为确认。
- `mute_user_list` 中的 QQ 不接受转告。
- 管理员可用 `#convey_<QQ号> <内容>` 让 Bot 以自己的语气向已有私聊发送消息。

## 配置

```toml
[plugin]
enabled = true

[components]
similarity_threshold = 0.4
confirmation_timeout = 60
mute_user_list = ["不接收转告的QQ号"]
```

`#convey` 使用 Core 的管理员权限判断。普通转告由 Planner 选择 `messenger_relay` 动作，不需要用户输入命令。
