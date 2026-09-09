# 主人身份验证插件（Owner Auth）

在回复提示构建前，根据 QQ 用户 ID 注入主人或额外角色身份，避免仅凭昵称认定主人。当前版本为 `1.2.0`。

## 功能

- 以 QQ 号为主键验证主人，昵称只用于展示。
- 可配置作者、管理员等额外角色，每个角色可附带单独说明。
- 验证结果短时缓存，并可输出调试日志。
- 对不提供 QQ 身份的本地聊天、WebUI、UniversalVC、DiscordVC，以及声明外部身份模式的平台跳过 QQ 验证。
- 插件卸载时清理其提示词补丁。

> 该插件提供的是对话提示层身份信息，不应替代 Core 的命令权限、管理员白名单或外部平台鉴权。

## 配置

```toml
[plugin]
enabled = true

[owner_auth]
owner_qq = 123456789
owner_nickname = "主人"
enable_auth = true
log_auth_result = true

[role_auth]
enable_role_auth = true
role_list = [
  "作者|987654321|显示名|可选说明"
]

[debug]
enable_debug = false
show_detailed_info = false
```

角色格式为 `角色名|QQ号|显示名|提示语（可选）`。如需自定义 `role_prompt_template`，可使用 `{role}`、`{display_name}`、`{qq}`、`{owner_nickname}`、`{owner_qq}` 和 `{note}` 占位符。

## 排查

身份未识别时依次检查：

1. `owner_qq` 是否为真实发送者 QQ，而不是群号或昵称。
2. `plugin.enabled` 与 `owner_auth.enable_auth` 是否开启。
3. 当前平台是否能提供标准 QQ 身份。
4. 开启 `[debug]` 后查看单条消息的解析结果。

插件采用 GPL-v3.0-or-later。
