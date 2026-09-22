# 日记插件（Diary Plugin）

汇总一天内的群聊/私聊记录生成角色日记，可选发布到 QQ 空间，并支持按时区和过滤规则执行每日任务。

## 主要能力

- `diary`、`qqzone` 或自定义 prompt 三种生成风格。
- 按总消息数和单会话消息数过滤低信息量记录。
- 使用 Core 默认回复模型，或独立的 OpenAI 兼容模型。
- 可选 QQ 空间发布、随机时间浮动、白名单/黑名单会话过滤。
- 提供 `diary_generator` Action、情感分析 Tool 和管理命令，可分别开关。

## 命令

| 命令 | 说明 |
| --- | --- |
| `#diary_generate [日期]` | 生成当天或指定日期日记 |
| `#diary_generate_all [日期]` | 忽略会话过滤生成 |
| `#diary_list [日期/all]` | 查看列表与统计 |
| `#diary_view [日期] [编号]` | 查看日记内容 |
| `#diary_debug [日期]` | 查看消息读取与过滤信息 |
| `#diary_help` | 查看帮助 |

生成、调试与忽略过滤等管理操作需要 `plugin.admin_qqs` 中的权限。

## 配置重点

```toml
[plugin]
enabled = true
admin_qqs = [123456789]
enable_action = true
enable_tool = true
enable_command = true

[diary_generation]
min_message_count = 50
min_messages_per_chat = 25
style = "diary"

[qzone_publishing]
qzone_min_word_count = 150
qzone_max_word_count = 500

[schedule]
schedule_time = "23:30"
fluctuation_minutes = 10
timezone = "Asia/Shanghai"
filter_mode = "whitelist"
target_chats = ["group:群号", "private:QQ号"]
```

过滤规则：

- `whitelist` + 空列表：禁用定时任务；有内容时只处理列出的会话。
- `blacklist` + 空列表：处理全部会话；有内容时排除列出的会话。

## 平台能力与模型

- QQ 空间 Cookie 由核心平台能力统一获取，插件只负责发布内容。
- WebUI 部署向导只配置核心 WebSocket 客户端，并保留用户已有的其他服务配置。
- `[custom_model]` 仅支持 OpenAI 兼容接口。API Key 不要写入 README、日志或提交记录。

依赖已纳入 Core 的 `pyproject.toml`，在 `NachoBot` 目录执行 `uv sync --locked` 即可。定时调度发生异常时会重试，但仍应检查时区、过滤列表、最少消息数和平台适配器连通性。

本插件基于 [bockegai/diary_plugin](https://github.com/bockegai/diary_plugin)，采用 MIT 许可证。
