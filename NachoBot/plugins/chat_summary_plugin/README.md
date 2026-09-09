# 聊天记录总结插件（Chat Summary Plugin）

总结 QQ 群当天或指定日期的聊天记录，可针对单个用户生成画像，并以图片发送；图片失败时自动回退文本。

## 功能

- 群聊总结与单用户总结。
- 群友称号、金句、情绪/“炫压抑”指数与 24 小时活跃分布。
- 每日定时总结，支持时区、最少消息数和目标群过滤。
- 默认使用 Core 的回复模型，也可配置 OpenAI 兼容模型。

## 使用

```text
#summary
#summary 2026-08-11
#summary 123456789 2026-08-11
```

分别表示：总结今天、本群指定日期、指定 QQ 用户在该日期的聊天。插件默认关闭，需要先设置 `plugin.enabled = true`。

## 配置重点

```toml
[plugin]
enabled = true

[summary]
group_summary_max_words = 400
user_summary_max_words = 300
enable_user_summary = true
enable_user_titles = true
enable_golden_quotes = true
enable_depression_index = true

[auto_summary]
enabled = false
time = "23:00"
timezone = "Asia/Shanghai"
min_messages = 10
target_chats = [123456789]

[custom_model]
use_custom_model = false
api_url = "https://example.com/v1"
api_key = ""
model_name = "your-model"
```

`target_chats` 为空时对所有有消息的群生效。自定义模型配置包含敏感凭据，不要提交真实 API Key。

## 图片与排查

Pillow、pytz 等依赖已纳入 Core 的 `pyproject.toml`：

```bash
cd NachoBot
uv sync --locked
```

图片生成需要可用的中文字体；Windows 通常使用微软雅黑，Linux 建议安装文泉驿或 Noto CJK。失败时依次检查：

1. 日志中的字体路径与 Pillow 错误。
2. `summary_backgrounds/` 等装饰资源是否存在。
3. 日期/QQ 参数是否正确、当天消息是否达到 `min_messages`。
4. 自定义模型 URL、Key、超时设置，或 Core 默认模型是否可用。

本插件基于 [saberlights/chat_summary_plugin](https://github.com/saberlights/chat_summary_plugin) 修改，采用 GPL-3.0-or-later。
