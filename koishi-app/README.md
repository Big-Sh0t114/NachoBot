# NachoBot Koishi App

NachoBot 的 Koishi 网关工程，当前主要承载 Discord 文字接入、Slash Command 桥接和 OneBot v11 Server；Python 侧由 [NachoBot-Koishi-Adapter](../NachoBot-Koishi-Adapter/README.md) 连接。

## 启动

要求 Node.js，并使用仓库声明的 Yarn 4：

```bash
corepack enable
corepack yarn install --immutable
corepack yarn start
```

Windows 也可运行 `launch Koishi.bat`，或在仓库根目录运行 `launch_discord.bat` 同时启动完整 Discord 链路。

## 配置要点

- 在 `koishi.yml` 的 Discord Adapter 中填写 Bot Token 等平台配置，不要提交真实凭据。
- OneBot Server 默认监听 `5140`，路径为 `/onebot/v11/ws`。
- `NachoBot-Koishi-Adapter/config.toml` 的 `onebot_server.ws_url` 必须与上述地址一致。
- 需要代理时同时检查 Koishi 与 DiscordVC 的代理设置；容器/远程环境中不要把 `127.0.0.1` 误当成宿主机。

通用 Koishi 使用方式见 [官方文档](https://koishi.chat/manual/starter/boilerplate.html)。
