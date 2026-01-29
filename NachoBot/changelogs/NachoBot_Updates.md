1/29
    修复Discord私聊无法获取用户id导致回复回退至频道的问题
    对Bilibili直播礼物系统的接入作修复
    为对应礼物标注明确的金额，规范感谢用prompt
1/28
    修复一些已知的小bug，在Discord平台禁用了不支持的视频搬运功能
1/27
    开发DiscordVC-Adapter，支持Discord语音频道自由发言
    为Discord语音频道配置独立prompt
1/26
    Bilibili适配器与MaiBot s4u直播相关代码对接
        礼物/上舰/VIP队列等事件，未经过测试 (财力不足)
    Bilibili直播接入可选的TTS回复以及语音识别，同时打印中文字幕
    优化Bilibili Adapter整体架构
1/25
    接入MCP工具，本地部署MCP服务
1/22
    Bilibili直播间接入视觉模块，识别本地屏幕并注入直播
1/21
    Bilibili直播增加可编辑的直播内容，注入给replyer
1/20
    修复Discord slash commands与核心的桥接问题
1/18
    增加对Bilibili的可选反反爬措施，通过修改 ws_proxy="pool" 启用代理池
    为Bilibili增加了额外的blocked_markers
1/17
    基本完成对Bilibili的接入，现在支持三平台同时接入
1/16
    开发Bilibili Adapter
1/15
    完成了Discord的对接，同时对TTS与Discord进行了适配，现在支持双平台同时接入
1/14
    誓言系统更新，加入后处理步骤，优化注入内容
1/12
    整合修改群聊总结插件
    给指令系统引进了-force后缀
1/10
    制作Koishi-Adapter并尝试接入Discord
1/8
    联网搜索加入核心
    主人认证插件的升级，现可支持自定义添加角色
    解决了TTS语种回退问题
    增加Filter，可自行在bot_config中配置
1/7
    网站抓取加入核心
    改进分割器超限逻辑，现在会强制合并而不是回退
1/6
    NachoBot整体架构的重构
1/4
    为群聊/私聊启用了可独立设置的上下文条数
    移除了高级模式下动作池中除reply和no_reply以外的所有动作
1/2
    更新誓言系统注入逻辑并加入日志
    新增情景注入内容
    加入指令纠错系统
    修复了关系系统无法添加人物的bug
    
# 2026

12/31
    为各会话添加独立的talk_frequency
12/30
    Unfork了Maibot项目
12/28
    主要回复模型由Gemini 2.5 pro切换至Claude Sonnet 4-5
    修复高级模式退出后模型组无法恢复至默认组别的问题
12/27
    高级模式加入挂机检测，一段时间无响应自动退出
    artwork_plugin的开发与落地
12/26
    高级模式启用独立模型组回复
12/25
    删除超时信息，现在保持静默丢弃
12/24
    加入mus_library用户请求，未收入曲库音乐会被整理进list
    基本解决了TTS语种问题
12/20
    日记生成模型从Gemini 2.5 pro切换至Deepseek v3
12/18
    尝试通过设置thinking_budget修复Gemini的截断问题
12/16
    继续修复TTS的语种回退问题
12/14
    新增关键词情景注入系统
    继续修复TTS的语种回退问题
12/12
    加入高级模式
    新增#help菜单，自动扫描并注册新增指令
12/4
    日记插件新增可选过滤，新指令触发
    新增反注入
    修复TTS语种回退问题
11/30
    给mus_library曲库增加曲目
11/29
    第三方插件的指令统一规范
    誓言系统的缓存修复
    修改mus_library关键词匹配机制
    给mus_library曲库增加曲目
11/28
    修复了TTS的语种切换问题
    给mus_library曲库增加曲目
11/27
    誓言系统的开发与落地
    通过bilibili_video_sender_plugin中获取思路，完美解决了mus_library的链路问题
11/26
    部分插件的兼容性修复
11/25
    对插件商店的新增优质插件进行整合
11/22
    继续修复TTS的语种切换问题
11/20
    知识库启用
    修复TTS的语种切换问题
11/19
    新增#lang_switch命令，通过该命令可以切换TTS的语种（效果极不稳定）
    修改项目内MaiBot相关字段为NachoBot
11/11
    使用本地垫片与第三方api供应商通信，解决了Gemini字段格式不匹配的问题
    主要回复器更改为Gemini 2.5 pro
    创建了一键启动脚本
11/10
    继续尝试解决mus_library上传链路问题无果
    mus_library开发暂时搁置
11/8
    安装整合麦麦插件商店优质插件
11/6
    解决文件核心上传架构
    基本完成插件开发
    尝试解决文件上传链路过慢问题
11/3
    开始开发mus_library插件，可播放本地SVC变音后的歌曲
    解决了用户交互相关内容以及核心框架
10/26
    配置并修复了官方TTS插件，设定默认TTS语种为日语
10/24
    *Fork并本地部署了Maibot*
`Formal Update`

10/21
    langbot群聊触发器的修复
10/20
    着手于langbot的TTS开发
10/18
    clone了langbot repository并使用docker部署

# 2025
