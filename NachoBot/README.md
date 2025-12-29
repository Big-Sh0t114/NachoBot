# NachoBot (基于 MaiBot 0.10.3 Beta)

NachoBot 是在上游 **MaiBot 0.10.3 Beta** 基础上定制的角色扮演聊天机器人，保留了上游插件体系与架构，同时调整了人设、对话风格，并新增针对特定角色扮演场景的内容。本文档帮助你快速了解本项目、配置要点，以及如何溯源到上游项目。

## 项目背景与溯源
- 上游项目：MaiBot（https://github.com/MaiM-with-u/MaiBot），版本基线 0.10.3 Beta。
- 许可证：沿用 MaiBot 的 GPLv3；请遵守本仓库的 `LICENSE` 以及各插件/第三方组件的许可证。
- 主要差异：定制的人设与回复风格，简化的配置示例，清理了用户私有数据与密钥。

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
- 可选的高级模式（独立模型组回复，默认为Grok4/3）。
- 大量原创插件/预设内容，为了更好的角色扮演而生。
- 更多小细节等你发现。

## 快速开始
1) 拉取代码后，先复制/编辑配置：
   - `config/bot_config.toml`：填写 `qq_account`、按需设置人设、表达学习、权限白名单等。
   - `config/model_config.toml`：为各 `api_providers` 填入你的 `api_key`，按需调整模型映射。
   - 插件配置：
     - `plugins/diary_plugin/config.toml`：填入 Napcat `napcat_token` / 自定义模型 `api_key`，配置目标聊天列表。
     - `plugins/poke_plugin/config.toml`：Napcat 连接与鉴权。
     - `plugins/Maizone/config.toml`：Napcat token、权限列表、图片生成 `api_key`。
     - `plugins/bilibili_video_sender_plugin/config.toml`：如需高画质/登录，填写 `sessdata`/`buvid3`。
   所有密钥/账号均已清空占位，请使用你自己的值。
   默认第三方模型拉取启航API以及硅基流动，直接使用需自行注册充值。

2) 依赖安装/运行：与上游 MaiBot 流程一致（参考上游文档或本仓库脚本），确保 Python 环境、依赖和 Napcat/OneBot 相关服务就绪。

3) launchbot.bat一键启动依赖tts_adapter文件夹中的ttslaunch.bat需自行进入正确配置两者路径才可使用

## 安全与隐私提示
- 请勿将真实密钥、Cookie、个人账号信息提交到仓库；部署前在本地/环境变量中填充。
- 本项目会调用第三方模型/服务；使用时需遵守各自的服务条款与隐私政策。
- 如启用日志、统计或持久化存储，请确认符合你的合规要求。

## 贡献与致谢
- 上游：MaiBot 项目团队与贡献者。
- 定制与维护：BigSh0t（本仓库）。
- 贡献方式：遵循 GPLv3；提交 PR 前请先清理私密信息，并保持对上游的致谢与链接。 ***!
