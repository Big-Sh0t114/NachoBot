# NachoBot Discord 适配器

本项目是 NachoBot 唯一的 Discord 适配器。一个 `main.py` 进程运行一个
Discord Bot / Gateway 客户端，同时处理服务器文字消息、私信、线程、斜杠命令
和可选语音。不要与旧 Koishi / DiscordVC 启动链并行登录，也不要用同一 Bot
Token 启动多个实例；适配器会按 Token 获取本地单例锁。

## 功能与 Core 契约

- 原生处理 Discord 服务器频道消息、私信和线程，并将消息路由到 NachoBot Core。
- 业务斜杠命令会在私密交互回复中返回 Core 的实际文字和附件；适配器使用本进程内
  的随机关联键将结果送回发起命令的用户，不会额外公开发送到频道。关联最多保留八分钟，
  Core 无结果或投递失败时命令会显示一次错误。
- 请在 Discord Developer Portal 为应用启用 **Message Content Intent**。使用语音
  时，Bot 还需要加入目标频道、接收语音和发言所需的权限。
- 语音采集使用 VAD，将有界语音片段发送给 Core。ASR、Planner 和 TTS 仍由 Core
  负责；适配器播放 Core 返回的语音片段和流，不另装一套 Discord ASR/TTS 模型。
- 旧逻辑用户和频道 ID 通过适配器私有的
  `data/identity_map.json` 保持映射。一次性迁移工具会先校验旧 Discord 与 Koishi
  来源，再生成 v3 配置和身份映射；原生 v2 配置继续有效且不触发 Koishi 迁移。
  映射统计由本地 CLI 显示，通用文档不记录
  特定部署的数据或原生 ID。
- 现有 Core 语音历史中的 `discord_vc` 平台标记保持原样。身份映射保留旧逻辑 ID，
  迁移不会改写或合并已有历史记录。
- Discord Gateway 和媒体请求支持配置代理。Core 连接使用 `[nachobot]`，并沿用
  `NACHOBOT_CORE_TOKEN`、`NACHOBOT_CORE_HOST` 和 `NACHOBOT_CORE_PORT` 环境变量。
- Discord 附件下载受配置的大小上限约束。Core 内联文件 payload 最大为 1 MiB；
  较大的音乐等媒体应通过共享本地路径传递，并使用对应的有界媒体处理流程。

## 配置

将 `config.toml.example` 复制为 `config.toml`，填写 Discord Bot Token 和 Application
ID。v3 TOML 配置中，`[discord]` 保存 Discord 凭据和代理选项，`[nachobot]` 保存
Core 路由，`[chat]` 保存消息过滤器，`[voice]` 保存语音选项，`[visual.image]`
保存图像设置，`[prompts]` 保存提示词覆盖项。

适配器会先替换本地提示词变量，再保护自定义提示词中的字面量花括号，同时保留
`{identity}` 等 Core 动态变量；已有的 `{{...}}` 和 `\{...\}` 写法也有效。语音默认提示
要求生成一段简短自然的口语文本，由 Core 合成播放。

`data/` 是私有运行数据目录。部署时请保留并安全备份 `identity_map.json`，以维持
原生 ID 到逻辑 ID 的映射。不要将身份映射提交到仓库或复制进镜像。

## 从旧版 Discord 部署升级

迁移完成前，请保留旧 DiscordVC 配置、Koishi 适配器配置、Koishi YAML 和 Koishi
数据库。从本目录执行：

```bash
uv sync --locked
uv run python migration.py --root ..
```

CLI 会执行一次性、失败即停止的迁移：校验旧 Discord / Koishi 配置和数据库，备份
原地 v1 Discord 配置，然后原子写入 v3 配置与身份映射。它只输出映射数量等汇总
信息和经过清理的错误。如果校验失败，请保留旧文件并修正问题后重试。迁移完成后，
只启动本适配器；WebUI 检测到旧 Discord / Koishi 进程时会阻止新实例启动，也不会
自动停止旧进程。

全新安装无需旧配置：复制示例、填写凭据，再执行下方启动命令。示例中的凭据均为
占位符。

## 本地运行

```bash
uv sync --locked
uv run python main.py
```

在仓库根目录运行 `launch_discord.bat`，脚本会锁定依赖版本、检查共享 FFmpeg，随后
只启动本适配器。脚本不会启动 NachoBot Core；请确保 Core 已经运行且网络可达。

## Docker Compose

先准备 `config.toml` 和宿主机 `data/` 目录。升级时请先在宿主机运行迁移 CLI；旧配置
和 Koishi 数据库不会复制到镜像。创建共享 Docker 网络 `nacho_bot`，并确保 Core 可从
Compose 服务地址 `core:8000` 访问，然后执行：

```bash
docker compose build
docker compose up -d
```

Compose 将配置以只读方式挂载，将 `data/` 持久化以保存身份映射，并只读挂载 Core
返回的本地媒体目录：共享的 `NachoBot/music`、`data/sandbox`、`data/video` 和
`data/media-tmp`。Core Compose 将 `TMPDIR` 指向共享的 `data/media-tmp`，并创建该临时
目录的宿主机 bind mount；这让 Core 通过系统临时目录交给适配器的视频路径也能被解析。
适配器镜像提供 `/NachoBot` 到 `/workspace/NachoBot` 的路径别名，以兼容 Core 返回的绝对
路径。凭据、身份数据、日志、备份和本地虚拟环境都不会打包进镜像。容器中的
`127.0.0.1` 指向容器自身；如需代理，请填写容器可访问的代理地址。
