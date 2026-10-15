# NachoBot Core

NachoBot Core 基于 **MaiBot 0.10.3 Beta** 演进，负责统一对话、记忆、插件、动作规划与消息协议。QQ、Discord、Bilibili、语音和 Live2D 等平台能力由相邻适配器接入，平台专属的高频或多阶段逻辑尽量留在各自适配器中。

## 与上游相比的重点能力

- 群聊与私聊分别规划，支持普通 Planner、Heart Flow、Brain Chat 与 Focus 会话切换。
- A_Memorix 长期记忆、中期对话摘要、人物画像、跨用户检索与跨平台账号绑定。
- 沙盒读写、回复打断、URL 抓取、预约、封禁等动作；高风险动作由规划与执行边界共同约束。
- 原生插件体系及 MaiBot 插件商店兼容，并整合 Messenger、日记、聊天总结、曲库、Moderation 等插件。
- 通过 canonical `ncnk_message` 与 runtime capabilities 统一不同平台的消息、回复和可用能力。
- 联网搜索不再要求模型声明专用搜索能力；Bilibili 二阶段搜索等平台逻辑已从 Core 解耦。

详细的按日变化见 [NachoBot_Updates.md](./changelogs/NachoBot_Updates.md)。

## 推荐部署：WebUI

在仓库根目录运行：

```text
launch_webui.bat
```

默认地址为 `http://127.0.0.1:8088`。WebUI 提供：

- 环境与路径检查、平台选择、模型与配置生成。
- Core、NapCat、TTS、多模态及各平台适配器的进程和日志管理。
- TOML 配置编辑与历史备份、数据库浏览、知识库/沙盒管理。
- 本地聊天；当前前端使用持久 WebSocket 接收 Core 的多段回复。
- NapCat 网络配置辅助，会同步 Adapter、日记和 Bilibili 视频插件的实际端口设置。

WebUI 生成配置前会备份已有文件；如果现有配置损坏、账号不匹配或目标端口冲突，会报告具体错误并保留原文件。


## 手动配置

要求 Python 3.11 或 3.12，推荐使用 [uv](https://docs.astral.sh/uv/)。

从 `template/` 复制以下文件到 `config/`，去掉 `_template` 后缀：

- `bot_config_template.toml` → `bot_config.toml`
- `model_config_template.toml` → `model_config.toml`
- `topics_config_template.toml` → `topics_config.toml`
- `mcp_config_template.toml` → `mcp_config.toml`

至少检查：

- `bot_config.toml`：账号、人设、权限、记忆与群聊/私聊策略。
- `model_config.toml`：API provider、密钥、模型映射与任务模型组。
- `topics_config.toml`：情景和主题注入。
- `mcp_config.toml`：MCP 服务器、权限、连接参数与工具调用预算。

启动 Core：

```bash
uv sync --locked
uv run python bot.py
```

默认服务端口为 `8000`。平台适配器需要使用与 Core 一致的主机、端口和消息协议配置。

### Discord 手动部署

Discord 群聊、私聊、线程、Slash 命令和语音由一个原生适配器进程处理。它建立一个主 Gateway 客户端；加入语音频道后，Discord 的语音 WebSocket/UDP 连接也由该进程管理。这条 Discord 链路不需要 Node.js、Yarn 或 OneBot sidecar。普通服务器文字消息需要在 Discord Developer Portal 的 Bot 设置中启用 <a href="https://support-dev.discord.com/hc/en-us/articles/6207308062871-What-are-Privileged-Intents">Message Content Intent</a>。

首次部署时，在仓库根目录复制模板：

```powershell
Copy-Item .\NachoBot-Discord-Adapter\config.toml.example .\NachoBot-Discord-Adapter\config.toml
Set-Location .\NachoBot-Discord-Adapter
uv sync --locked
```

在 `config.toml` 的 `[discord]` 填写 Bot Token 和 Application ID，并检查 `[nachobot]` 中的 Core 地址。随后在适配器目录运行：

```powershell
uv run python main.py
```

#### 从旧 Discord 链路迁移

迁移前停止旧 Discord 进程，并保留 `NachoBot-DiscordVC-Adapter`、`NachoBot-Koishi-Adapter` 和 `koishi-app` 的现有配置与数据，尤其是 `koishi-app/data/koishi.db`。迁移会读取旧 DiscordVC 语音配置、Koishi Adapter 配置、`koishi.yml` 和 SQLite 身份表；SQLite 以只读方式打开。它将旧 Discord Snowflake 用户/频道 ID 映射回 Core 已使用的逻辑 ID，保留已有文字聊天的身份与历史关联，转换聊天过滤配置，并生成私有的 `data/identity_map.json`。没有历史映射的新成员继续使用 Discord 原生 ID。

从仓库根目录运行一次性迁移：

```powershell
Set-Location .\NachoBot-Discord-Adapter
uv sync --locked
uv run python migration.py --root ..
```

迁移程序会读取已存在的 v1 DiscordVC 配置；若新配置尚不存在，则从旧 DiscordVC 配置读取。已有 v2 配置会被识别为已完成，重复运行不会覆盖它。源文件缺失或配置/身份映射冲突时会失败关闭；先解决错误并保留原文件，不要手动拼接映射。成功后它会生成 `config_version = 2` 配置，并保留旧语音配置副本 `config.toml.legacy-v1.bak`；身份映射文件和备份都应作为本地私有数据保管。

只有迁移成功后再启动 `uv run python main.py`。迁移不会改写 Core 数据库；已有 `discord_vc` 群聊历史继续保留在原平台命名空间，不会静默并入新的 `discord` 会话。旧的 Koishi/DiscordVC 文件应在迁移确认后再归档。

完成配置后，也可以在仓库根目录运行 `launch_discord.bat` 启动这一个 Discord 适配器进程。

### Bilibili 手动部署

旧的 `config-save/config-biliadapter.toml` 不再使用。首次部署时，在仓库根目录复制当前模板并进入适配器目录：

```powershell
Copy-Item .\NachoBot-Bilibili-Adapter\config_template.toml .\NachoBot-Bilibili-Adapter\config.toml
Set-Location .\NachoBot-Bilibili-Adapter
uv sync
```

使用适配器自己的虚拟环境运行二维码登录脚本，并按终端提示扫描二维码：

```powershell
.\.venv\Scripts\python.exe .\qr_login.py
```

扫码成功后，脚本会把 `SESSDATA`、`bili_jct` 和 `DedeUserID` 写入 `config.toml`。启动前还需要检查 `[nachobot_server]` 的 Core 地址，并在 `[live].room_ids` 中填写直播间 ID；如需 Live2D，再设置对应的 `enable_live2D` 和连接参数。

单独启动 Bilibili Adapter：

```powershell
uv run python main.py
```

也可以在配置和扫码完成后返回仓库根目录运行 `launch_bilibili.bat`。

## 根目录启动档位

| 脚本 | 本地模型策略 |
| --- | --- |
| `launchbot.bat` | Core 选择本地多模态运行时；本地失败后回退远端 API；使用 9880 TTS 引擎 |
| `launchbot_lite.bat` | Core 的感知走远端 API；使用 9880 TTS 引擎 |
| `launchbot_potato.bat` | Core 固定输出纯文本，不请求本地感知或 TTS |

TTS 已由 [Multimodal Adapter](../NachoBot-Multimodal-Adapter/README.md) 的托管运行时负责，不再要求用户准备独立 GPT-SoVITS 或 VoxCPM 客户端目录。首次启动会优先复用本地缓存，缺少的运行时和模型再按配置下载。

平台侧脚本：

- `launch_bilibili.bat`：Bilibili，按需联动独立 Live2D。
- `launch_discord.bat`：统一的 Discord 文字与语音适配器。
- `launch_universal_vc.bat`：Windows 进程音频采集与虚拟声卡；ASR 由 Core 统一调度。

## 多模态与平台边界

| 能力 | 所属组件 |
| --- | --- |
| Core 多模态 facade | `NachoBot/src/multimodal` |
| TTS、情感预设、ASR、VLM | `NachoBot-Multimodal-Adapter`（9880 TTS；9874 perception） |
| QQ / OneBot | `NachoBot-Napcat-Adapter` + NapCat |
| Bilibili 直播、评论、私信与二阶段搜索 | `NachoBot-Bilibili-Adapter` |
| Live2D 渲染与交互 | `NachoBot-Live2D-Adapter` |
| Discord 文字 / 私聊 / 线程 / Slash / 语音 | `NachoBot-Discord-Adapter` |
| 任意进程语音 | UniversalVC Adapter |

这一边界可避免 Core 为某个平台导入专属客户端、模型或原生运行库。

## Docker

```bash
python scripts/bootstrap_compose.py
python scripts/bootstrap_compose.py --check
docker compose build core
docker compose up -d
```

Bootstrap 命令只创建缺失的 bind source，不覆盖已有配置，并会从已跟踪模板初始化 Core 与 Multimodal 配置；请在启动前填写 `docker-config/mmc/` 中的模型和账号配置。仓库自带插件保留在 Core 镜像内，避免空 bind mount 将其隐藏。Core 宿主机端口默认仅绑定 `127.0.0.1`，Compose 自动创建名为 `nacho_bot` 的受信容器网络。同一 Docker daemon 上加入该网络的容器应视为具有 Core 访问权限。Core Compose 默认通过该网络访问 `multimodal-tts-emotion:9880` 与 `multimodal-perception:9874`，可用 `NACHOBOT_TTS_ENDPOINT`、`NACHOBOT_MULTIMODAL_ENDPOINT` 和 `NACHOBOT_RUNTIME_PROFILE` 覆盖。需要令牌隔离时，在 Core 与所有适配器的进程环境中设置同一个 `NACHOBOT_CORE_TOKEN`；Compose 会转发该变量。浏览器若需直接连接 Core WebSocket，还必须用逗号分隔的 `NACHOBOT_WS_ALLOWED_ORIGINS` 明确列出完整 Origin（协议、主机和端口）。

Core 镜像使用 Python 3.12、现有 `pyproject.toml` 与 `uv.lock` 构建，并包含 FFmpeg；启用网页搜索时使用 Playwright Chromium。SQLite Web 默认不启动；仅在本机调试时使用 `docker compose --profile debug up -d sqlite-web`。历史聚合 Adapter 镜像也不默认启动；使用前需手动提供 `docker-config/adapters/config.toml`，运行 `python scripts/bootstrap_compose.py --check --legacy-adapters`，然后启用 `legacy-adapters` profile。

各适配器有独立 Compose 文件。容器间不要使用 `127.0.0.1` 互联，应改用 `core`、`multimodal-adapter` 等服务名；配置和模型通过卷持久化。Live2D、UniversalVC 等 Windows 原生能力请阅读各自 README 的限制。

## 插件与文档

- 插件目录：`plugins/`
- 内置插件：`src/plugins/built_in/`
- A_Memorix：[src/A_memorix/README.md](./src/A_memorix/README.md)
- 上游项目：[MaiBot](https://github.com/Mai-with-u/MaiBot)

本项目沿用 GPLv3；第三方插件和模型仍以各自许可证为准。
