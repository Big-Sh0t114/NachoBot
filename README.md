# NachoBot (基于 MaiBot 0.10.3 Beta)

NachoBot 是在上游 **MaiBot 0.10.3 Beta** 基础上定制的角色扮演聊天机器人，保留了上游插件体系与架构，同时调整了人设、对话风格，并新增针对特定角色扮演场景的内容。本文档帮助你快速了解本项目、配置要点，以及如何溯源到上游项目。

## 项目背景与溯源
- 上游项目：MaiBot（https://github.com/MaiM-with-u/MaiBot），版本基线 0.10.3 Beta。
- 许可证：沿用 MaiBot 的 GPLv3；请遵守本仓库的 `LICENSE` 以及各插件/第三方组件的许可证。
- 主要差异：见下文。

## bot核心变动
- 截止至0.10.3 Beta的大部分原版内容。
- 精选插件的整合/兼容性修复。
- 角色扮演向的多轮对话与记忆管理。
- 防注入系统，为角色设定保驾护航。
- #help菜单帮助你理清所有的指令。
- tts菜单支持双语种指令无缝切换。
- 更智能的情景注入系统，给角色人设释放空间。
- 誓约系统让笨蛋bot记住你们的单独会话中每一个约定。
- Napcat适配器的定时心跳检测，异常自动断线促进重连。
- 原版插件体系兼容（麦麦插件商店[https://plugins.maibot.chat/]）。
- 生成回复前智能检测信息时效需求并选择性联网生成回复。
- 可选的高级模式（独立模型组回复，默认为Grok4/3）。
- 自动抓取解析信息中的url并将内容整合注入给llm。
- 大量原创插件/预设内容，为了更好的角色扮演而生。
- 支持QQ，Discord，Bilibili等多平台同时接入。
- 可客制化的群聊/私聊设置。
- 更多小细节等你发现。

`Discord平台改动`
- 适配了Discord内的语音发送，格式由QQ的silk变为ogg
- 翻译#help菜单中有效指令至英语
- 适配了slash commands

`Bilibili平台改动`
- 支持私聊/评论区本楼回复/评论区@回复/直播间弹幕回复
- 禁用了bilibili_video_sender_plugin以避免从系统通知中错误抓取视频url
- 评论区回复加入防刷屏，限制同一用户在相同评论区中的最多会话轮数
- 过滤掉了TTS信息（戳一戳仍会触发并报错，但对使用无影响）
- 将emoji调整为图片类型发送以兼容b站私聊
- 支持对不同直播间客制相互独立的prompt
- 额外的过滤器
- 禁用了指令

## 快速开始
`核心配置`
1) 拉取代码后，先复制/编辑配置：
   - 从 `template/` 中复制`bot_config_template.toml``model_config_template.toml`到`config/`，并删除`_template`后缀
   - `config/bot_config.toml`：填写 `qq_account`、按需设置人设、表达学习、权限白名单等。
   - `config/model_config.toml`：为各 `api_providers` 填入你的 `api_key`，按需调整模型映射。
   - 插件配置：
     - 从项目根目录`config-save/`中获取各插件的配置文件，移动到各插件目录中并删除后缀。
     - `plugins/diary_plugin/config.toml`：填入 Napcat `napcat_token` / 管理员列表 `admin_qqs` ，配置目标聊天列表。
     - `plugins/poke_plugin/config.toml`：Napcat 连接与鉴权。
     - `plugins/group_muter_plugin/config.toml`：管理员列表填写。
     - `plugins/chat_summary_plugin/config.toml`：管理员列表填写。
     - `plugins/owner_auth_plugin/config.toml`：主人/其他角色填写。
     - `plugins/bilibili_video_sender_plugin/config.toml`：浏览器打开b站，F12进入控制台获取sessdata填入配置文件。
   所有密钥/账号均已清空占位，请使用你自己的值。
   默认第三方模型拉取启航API以及硅基流动，直接使用需自行注册充值。

2) 依赖安装/运行：与上游 MaiBot 流程一致（参考上游文档[https://docs.mai-mai.org/manual/deployment/]），确保 Python 环境、依赖和 Napcat/OneBot 相关服务就绪。

3) launchbot.bat一键启动依赖独立的GPT-SoVITS项目[https://www.yuque.com/baicaigongchang1145haoyuangong/ib3g1e](本仓库同款)，因内容较大，本仓库未提供本体，请自行下载后配置launchbot.bat内路径**line36** (使用绝对路径)。

`Bilibili配置`
  - 从项目根目录`config-save/`中复制`config-biliadapter.toml`移动至`NachoBot-Bilbili-Adapter`目录中，删除`-biliadapter.toml`后缀
  - cmd运行 **python qr_login.py** 扫描二维码 
  - `config.toml`：填写直播间id/违禁词等
  - 双击`launch_bili.bat`运行适配器

`Discord配置`
  - 双击`koishi-app/launch Koishi.bat`启动Koishi，在webUI中填写自己的Discord Bot token 和 self id
  - 双击`launch_koishi_adapter.bat`运行适配器

## 安全与隐私提示
- 请勿将真实密钥、Cookie、个人账号信息提交到仓库；部署前在本地/环境变量中填充。
- 本项目会调用第三方模型/服务；使用时需遵守各自的服务条款与隐私政策。
- 如启用日志、统计或持久化存储，请确认符合你的合规要求。

## 贡献与致谢
- 上游：MaiBot 项目团队与贡献者。
- Napcat/GPT-SoVITS/Koishi团队。
- 贡献方式：遵循 GPLv3；提交 PR 前请先清理私密信息，并保持对上游的致谢与链接。
