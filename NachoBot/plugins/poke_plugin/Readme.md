# 戳一戳插件

为 QQ 聊天提供主动“戳一戳”能力。当前版本为 `0.4.2`，支持群聊与私聊，目标会优先根据当前聊天上下文和近期消息解析。

## 功能

- 注册 `active_poke` 动作，由聊天规划器决定何时主动戳人。
- Focus 模式始终可用；可通过配置决定是否允许 Normal 模式调用。
- 通过适配器的 `SEND_POKE` 命令发送，不再直连 NapCat HTTP，也不需要额外开放 4999 端口。
- 当前版本仅提供主动戳一戳，不包含旧版自动回戳逻辑。

## 配置

编辑插件目录下的 `config.toml`：

```toml
[plugin]
enabled = true

[poke]
debug = true
allow_normal_active_poke = true
```

- `debug`：记录目标、聊天流和适配器执行结果。
- `allow_normal_active_poke`：是否允许 Normal 模式主动戳人。

现有配置中的 `host`、`port`、`token` 是旧版 HTTP 实现遗留字段，当前代码不会读取。

## 使用前提

需启用聊天规划器，并使用支持 `SEND_POKE` 命令的适配器（如 NachoBot NapCat Adapter）。若无法解析目标或聊天流，动作会失败并记录原因。

许可证：GPL-3.0-or-later。
