# NachoBot (基于 MaiBot 0.10.3 Beta)

NachoBot 是在上游 **MaiBot 0.10.3 Beta** 基础上研发的角色扮演聊天机器人，保留了上游插件体系与架构，可以直接沿用上游项目的 [麦麦插件商店](https://plugins.maibot.chat/)。本文档帮助你快速了解本项目、配置要点，以及如何溯源到上游项目。

## 项目背景与溯源
- 上游项目：[MaiBot](https://github.com/MaiM-with-u/)，版本基线 0.10.3 Beta。
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

#### 核心配置
1. **拉取代码后，先复制/编辑主干配置**
   - 从 `template/` 中复制 `bot_config_template.toml` 和 `model_config_template.toml` 到 `config/`，并删除 `_template` 后缀。
   - `config/bot_config.toml`：填写 `qq_account`、按需设置人设、表达学习、权限白名单等。
   - `config/model_config.toml`：为各 `api_providers` 填入你的 `api_key`，按需调整模型映射。
     *(提示：所有密钥/账号均已清空占位，需使用你自己的值。默认第三方模型拉取启航 API 以及硅基流动。)*
   - **插件配置**：直接运行 bot 后会自动生成所有插件的默认配置文件模板，自行填写配置后重启 bot 即可。

2. **自动依赖安装与环境启动**
   - 本项目已实现**一键启动与依赖自动管理**。
   - 打开根目录下的 `launchbot.bat` 脚本，找到第 36 行的 `SOVITS_DIR` 变量，配置为你本地 GPT-SoVITS 的绝对路径。(如不使用语音可忽略)
   - 双击运行 `launchbot.bat` 即可全自动安装依赖并拉起所有底层组件。

#### Bilibili配置
- 从项目根目录 `config-save/` 中复制 `config-biliadapter.toml` 移动至 `NachoBot-Bilbili-Adapter` 目录中，删除 `-biliadapter.toml` 后缀。
- cmd 运行 `python qr_login.py` 扫描二维码。
- `config.toml`：填写直播间 id / 违禁词等。
- 双击 `launch_bili.bat` 运行适配器。

#### Discord配置
- 从项目根目录 `config-save/` 中复制 `koishi.yml` 移动至 `koishi-app/` 目录中。
- 双击 `koishi-app/launch Koishi.bat` 启动 Koishi，在 webUI 中填写自己的 Discord Bot token 和 self id。
- 双击 `launch_koishi_adapter.bat` 运行适配器。
- 复制 DiscordVC-Adapter 中的 `config.toml.example`，并重命名为 `config.toml`，填写相关配置。

#### TTS配置
*详见TTS适配器文档*

``保持所有终端运行！！！在全平台服务跑起来的情况下应该是有6+3+1个终端窗口在运行``

##### 安全与隐私提示
- 请勿将真实密钥、Cookie、个人账号信息提交到仓库；部署前在本地/环境变量中填充。
- 本项目会调用第三方模型/服务；使用时需遵守各自的服务条款与隐私政策。

###### 贡献与致谢
- 上游：MaiBot 项目团队贡献者与插件制作者。
- Napcat / GPT-SoVITS / Koishi 团队。
- 贡献方式：遵循 GPLv3；提交 PR 前请先清理私密信息，并保持对上游的致谢与链接。
