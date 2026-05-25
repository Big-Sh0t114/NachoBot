# NachoBot (基于 MaiBot 0.10.3 Beta)

NachoBot 是在上游 **MaiBot 0.10.3 Beta** 基础上研发的角色扮演聊天机器人，保留了上游插件体系与架构，可以直接沿用上游项目的 [麦麦插件商店](https://plugins.maibot.chat/)。本文档帮助你快速了解本项目、配置要点，以及如何溯源到上游项目。

## 项目背景与溯源
- 上游项目：[MaiBot](https://github.com/Mai-with-u/MaiBot)，版本基线 0.10.3 Beta。
- 许可证：沿用 MaiBot 的 GPLv3；请遵守本仓库的 `LICENSE` 以及各插件/第三方组件的许可证。
- 主要差异：见下文。

### NachoBot 核心变动
- 包含截至 0.10.3 Beta 的大部分原版内容。
- 精选插件的整合与兼容性修复。
- 角色扮演向的多轮对话与记忆管理。
- 防注入系统，为角色设定保驾护航。
- #help 菜单帮助你理清所有的指令。
- TTS 菜单支持双语种指令无缝切换。
- 更智能的情景注入系统，给角色人设释放空间。
- 誓约系统让笨蛋 bot 记住你们单独会话中的每一个约定。
- Napcat 适配器的定时心跳检测，异常自动断线促进重连。
- 原版插件体系兼容 [麦麦插件商店](https://plugins.maibot.chat/)。
- 生成回复前智能检测信息时效需求并选择性联网生成回复。
- 可选的高级模式（独立模型组回复，默认为 Grok4/3）。
- 自动抓取解析信息中的 URL 并将内容整合注入给 LLM。
- 大量原创插件/预设内容，为了更好的角色扮演而生。
- 修改核心对 notice 消息的处理逻辑，现在会回复“戳一戳”消息。
- 引入伪 Agent 系统沙盒，bot 可自主阅读、编辑并下发文件。
- 增加跨用户记忆检索，bot 可在其他用户会话中搜索到目标用户记忆（如存在）。
- 升级长时记忆系统，通过历史对话摘要持续迭代角色记忆。
- 添加预约系统，用户可以指定时间让 bot 提醒自己事情。
- 支持 QQ、Discord、Bilibili 等多平台同时接入。
- 可客制化的群聊/私聊设置。
- 更多小细节等你发现。

### Discord 平台改动
- 适配了 Discord 内的语音发送，格式由 QQ 的 silk 变为 ogg。
- 通过配置 DiscordVC-Adapter，实现 Discord 语音频道自由发言。
- 翻译 #help 菜单中有效指令至英语。
- 适配了 slash commands。
- 适配了沙盒系统。

### Bilibili 平台改动
- 支持私聊/评论区本楼回复/评论区@回复/直播间弹幕回复
- 禁用了 bilibili_video_sender_plugin 以避免从系统通知中错误抓取视频url
- 评论区回复加入防刷屏，限制同一用户在相同评论区中的最多会话轮数
- 过滤掉了 TTS 信息（戳一戳仍会触发并报错，但对使用无影响）
- 将 emoji 调整为图片类型发送以兼容b站私聊
- 额外的过滤器
- 支持对不同直播间客制相互独立的 prompt
- 支持直播间 TTS 回复/语音识别/字幕生成/屏幕捕捉/Live2D
- 设计了直播间专用的独立指令系统
- 设计了可选的 Bilibili 直播的虚拟主播回复模式，根据弹幕数量弹性生成思维链，但效果欠佳

### 多模态本地服务 (Control API)
本仓库架构内建启用了本地化的多模态识别与控制服务（通常随 TTS 适配器的 `9872` 端口启动）。这赋予了 bot 低延迟和本地免费的扩展处理能力：
- **ASR (语音识别)**：解析跨平台（如 Discord）语音通话信息为文本。
- **VLM (视觉大模型)**：配合沙盒与前台系统处理包含图像的用户消息。
- **TTS (语音管理)**：串接 GPT-SoVITS 及动态加载语音模型策略。
*首次运行可能涉及较大体积的本地模型下载或冷启动加载，请耐心关注后台终端进度。*

---

## WebUI 可视化一键部署与管理 (推荐)

NachoBot 现已配备完善的 **可视化 WebUI 部署向导与后台管理控制台**。  
如果你不希望处理繁琐的命令行、依赖管理和手动配置，推荐直接通过 WebUI 搞定一切。

### 1. 前置准备
1. 安装 **Python 3.11+** 环境 (推荐使用 3.11 或 3.12，不要安装 3.14)。
2. 下载并解压外部组件（如 [GPT-SoVITS 语音引擎](https://www.yuque.com/baicaigongchang1145haoyuangong/ib3g1e/dkxgpiy9zb96hob4)、[NapCatQQ (QQ协议端)](https://github.com/NapNeko/NapCatQQ) 等）。
3. 如果需要部署 Discord 语音陪玩等服务，请确保系统中已安装 **Node.js**。

### 2. 启动 WebUI
回到项目最外层目录，双击运行以下脚本：
*   **Windows**: 双击运行 **`launch_webui.bat`**。
*   脚本会全自动检查并配置 `uv` 环境，同步依赖包，并自动在浏览器拉起 WebUI 面板（默认监听端口：`8010`，即 `http://127.0.0.1:8010`）。

### 3. 一键可视化部署向导 (Setup Wizard)
进入浏览器中的 WebUI 界面，点击 **"一键部署"** 选项卡，按照以下五大步骤进行傻瓜式配置：

1.  **环境完整性自动检测 (Environment Checker)**：自动评估当前系统的 Python 版本、Node.js 版本以及硬件是否支持 CUDA/显卡加速。
2.  **外部路径校验 (Path Verifier)**：输入你的外部组件（如 NapCat Shell 目录、GPT-SoVITS 目录、VoxCPM 目录、Live2DCubismCore.dll 等）的绝对物理路径，系统会自动验证组件文件有效性，确保外部依赖无误。
3.  **多平台组件按需订阅**：勾选你想要启用的平台/模块（如 QQ、Discord、Bilibili、TTS、感知层等）。
4.  **模型参数可视化生成**：在 UI 界面输入大模型的 API 密钥、选择服务商 (如 OpenAI, Grok 等) 并选定底色人设。系统会自动为你生成并初始化 `bot_config.toml`、`model_config.toml`、`topics_config.toml` 等核心配置文件。
5.  **一键自动部署 (Auto-Deploy)**：点击“开始部署”。WebUI 将通过实时 WebSocket 日志输出流，全自动在各适配器的虚拟隔离环境内安装所需依赖。
6.  **NapCat 客户端自动打补丁 (NapCat Configurator)**：只需在 UI 中输入 Bot 账号，系统将全自动将 NapCat 核心配置 (`onebot11_<QQ>.json`) 修改为适配器对接的 WebSocket 和 HTTP 接口，**完全告别手动前往 NapCat 网页控制台配置网络配置的麻烦**。

### 4. 后台可视化运维与管理
部署完成后，你即可在 WebUI 中享受以下强大功能：
*   **进程控制台 (Process Manager)**：以服务组 (核心组、QQ组、B站组、Discord组) 或单个服务的形式，一键启动或停止所有子服务 (NachoBot 核心、Napcat 适配器、TTS 适配器、VLM/ASR 感知服务端等)，并以**实时滚动日志流**展现运行细节。
*   **可视化配置编辑器 (Config Manager)**：在浏览器中直接通过高亮文本编辑器微调任何 `.toml` 配置文件，每次修改皆有**自动历史备份**保护。
*   **智能数据库编辑器 (Database Manager)**：图形化浏览 Bot 的记忆数据库。支持**单列属性条件过滤 (Column Filters)** 与下拉快速刷选，方便定位与擦除 Bot 的特定记忆。
*   **沙盒与知识库热插拔 (Knowledge Base)**：在线安全地创建、修改与检索 Bot 的长期记忆、誓约 (约定项目) 及沙盒文件 (Bot 运行时修改会将安全锁定，确保核心服务停止时才能执行改动)。

---

## 传统手动配置与启动 (备选)

如果你更习惯使用命令行或者在无头服务器上进行纯文本手动配置，可沿用以下传统方式。

### 1. 主干配置初始化
*   从 `template/` 中复制 `bot_config_template.toml`、`model_config_template.toml`、`topics_config_template.toml` 到 `config/`，并删除 `_template` 后缀。
*   `config/bot_config.toml`：填写 `qq_account`、按需设置人设、表达学习、权限白名单等。
*   `config/model_config.toml`：为各 `api_providers` 填入你的 `api_key`，按需调整模型映射。
*   进入 `NachoBot-Napcat-Adapter` 目录，从 `template/` 中复制 `template_config.toml` 到 `NachoBot-Napcat-Adapter` 目录下，并删除 `template_` 前缀。

### 2. 自动启动脚本
*   记事本打开根目录下的 `launchbot.bat` 脚本，找到第 36 行的 `SOVITS_DIR` 变量，配置为你本地 GPT-SoVITS 的绝对路径。
*   双击运行 `launchbot.bat` 即可全自动安装依赖并拉起所有底层组件。
*   前往 `Napcat WebUI(http://127.0.0.1:6099)` 进行网络配置，添加 *Websocket客户端* ，配置URL为 *ws://localhost:8095* 并启用服务。

### 3. 平台高级手动配置

#### A. Bilibili 平台配置
*   从项目根目录 `config-save/` 中复制 `config-biliadapter.toml` 移动至 `NachoBot-Bilbili-Adapter` 目录中，删除 `-biliadapter.toml` 后缀。
*   进入目录运行 `python qr_login.py` 扫描二维码登录。
*   修改 `config.toml` 填写直播间 id / 违禁词等。
*   若要启用 Live2D 虚拟直播联动，需从官方下载 [Live2d Cubism SDK](https://www.live2d.com/zh-CHS/cubism/download/editor/)，提取 `Live2DCubismCore.dll` 并放入 `NachoBot-Bilibili-Adapter` 根目录下。

#### B. Discord 平台配置
*   从项目根目录 `config-save/` 中复制 `koishi.yml` 移动至 `koishi-app/` 目录中。
*   移除 `NachoBot-DiscordVC-Adapter` 目录下的 `config.toml.example` 文件的 `.example` 后缀。
*   双击 `koishi-app/launch Koishi.bat` 启动 Koishi，在 WebUI 中填写自己的 Discord Bot token 和 self id。
*   复制 `DiscordVC-Adapter` 中的 `config.toml.example` 重命名为 `config.toml`，填写频道对接配置。

#### C. TTS 平台配置
*   详见 [NachoBot-TTS-Adapter/README.md](../NachoBot-TTS-Adapter/README.md) 获取双语音合成引擎、DeBERTa 情感分析与 VLM/ASR 多模态服务的独立配置方法。

> ** 说明：** 
> * 保持所有终端运行！在全平台服务跑起来的情况下应该有多达 6+3+1 个终端窗口同时在后台工作。
> * `launchbot_lite.bat` 是轻量启动脚本，不包含本地 VLM/ASR 多模态服务，为电脑性能较低或不需要多模态的用户准备。

---

## Docker 部署

NachoBot 支持 Docker 容器化部署，各适配器可按需独立启动。

### 前置准备

```bash
# 创建共享外部网络（仅需首次执行）
docker network create nacho_bot
```

### 启动核心服务

```bash
cd NachoBot
docker compose up -d
```

核心服务包含：`core`（NachoBot 主进程）、`adapters`（Napcat 适配器）、`napcat`（QQ 协议端）、`sqlite-web`（数据库管理）。

### 按需启动平台适配器

每个适配器目录下都有独立的 `docker-compose.yml`，可按需启动：

```bash
# Bilibili 直播适配器（含屏幕监控/Live2D）
cd NachoBot-Bilibili-Adapter && docker compose up -d

# Discord 语音频道适配器
cd NachoBot-DiscordVC-Adapter && docker compose up -d

# Koishi 适配器（Discord 文字频道等）
cd NachoBot-Koishi-Adapter && docker compose up -d

# TTS 语音合成适配器
cd NachoBot-TTS-Adapter && docker compose up -d
```

### Docker 注意事项

- **网络**：所有服务通过 `nacho_bot` 外部网络互相通信。容器间使用服务名（如 `core`、`tts-adapter`）作为主机名，配置文件中的 `127.0.0.1` 需替换为对应服务名。
- **配置持久化**：各适配器通过卷挂载持久化 `config.toml` 等配置文件，修改配置后重启容器即可生效.
- **FFmpeg**：DiscordVC 适配器的 Docker 镜像已内置 FFmpeg，无需额外安装。
- **Xvfb**：Bilibili 适配器的 Docker 镜像已内置 Xvfb 虚拟帧缓冲，支持容器内屏幕监控。

---

##### 安全与隐私提示
- 请勿将真实密钥、Cookie、个人账号信息提交到仓库；部署前在本地/环境变量中填充。
- 本项目会调用第三方模型/服务；使用时需遵守各自的服务条款与隐私政策。

##### 贡献与致谢
- 上游：MaiBot 项目团队贡献者与插件制作者。
- Napcat / GPT-SoVITS / VoxCPM / Koishi 团队。
- 贡献方式：遵循 GPLv3；提交 PR 前请先清理私密信息，并保持对上游的致谢与链接。
