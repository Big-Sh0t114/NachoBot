import asyncio
from typing import List, Tuple, Type

from src.plugin_system import BasePlugin, register_plugin, ComponentInfo
from src.plugin_system.base.config_types import ConfigField

from .actions import SendFeedAction, ReadFeedAction
from .commands import SendFeedCommand
from .scheduled_tasks import FeedMonitor, ScheduleSender


@register_plugin
class MaizonePlugin(BasePlugin):
    """Maizone插件 - 让麦麦发QQ空间"""

    plugin_name = "MaizonePlugin"
    plugin_description = "让麦麦实现QQ空间点赞、评论、发说说"
    plugin_version = "2.4.2"
    plugin_author = "internetsb"
    enable_plugin = True
    config_file_name = "config.toml"
    dependencies = []
    python_dependencies = ["httpx", "Pillow", "bs4", "json5"]

    config_section_descriptions = {
        "plugin": "插件启用配置",
        "models": "插件模型配置",
        "send": "发送说说配置",
        "read": "阅读说说配置",
        "monitor": "自动刷空间配置",
        "schedule": "定时发送说说配置",
    }

    config_schema = {
        "plugin": {
            "enable": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "models": {
            "text_model": ConfigField(
                type=str, default="replyer", description="生成文本的模型（从麦麦model_config读取），默认即可"
            ),
            "image_provider": ConfigField(
                type=str, default="ModelScope", description="图片生成服务提供商（ModelScope或SiliconFlow）"
            ),
            "image_model": ConfigField(
                type=str,
                default="Qwen/Qwen-Image-Edit",
                description="图片生成模型（从对应服务商官网获取），如SiliconFlow的Kwai-Kolors/Kolors",
            ),
            "image_ref": ConfigField(
                type=bool,
                default=False,
                description="是否启用人设参考图（请重命名为done_ref，后缀不变，放入images文件夹）",
            ),
            "api_key": ConfigField(type=str, default="", description="相应提供商的API密钥（用于生成说说配图）"),
            "show_prompt": ConfigField(type=bool, default=False, description="是否显示生成prompt内容"),
        },
        "send": {
            "enable_image": ConfigField(type=bool, default=False, description="是否启用带图片的说说"),
            "image_mode": ConfigField(
                type=str,
                default="random",
                description="图片使用方式: only_ai(仅AI生成)/only_emoji(仅表情包)/random(随机混合)",
            ),
            "ai_probability": ConfigField(type=float, default=0.5, description="random模式下使用AI图片的概率(0-1)"),
            "image_number": ConfigField(
                type=int, default=1, description="使用的图片或表情包数量(范围1至4)(仅部分模型支持多图，如Kolors)"
            ),
            "history_number": ConfigField(
                type=int, default=5, description="生成说说时参考的历史说说数量，越多越能避免重复内容"
            ),
            "prompt": ConfigField(
                type=str,
                default="你是'{bot_personality}'，你想写一条主题是'{topic}'的说说发表在qq空间上，{bot_expression}不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，可以适当使用颜文字，只输出一条说说正文的内容，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )",
                description="生成说说的提示词，占位符包括{bot_personality}（人格），{topic}（说说主题），{bot_expression}（表达方式）",
            ),
        },
        "read": {
            "read_number": ConfigField(type=int, default=5, description="一次读取最新的几条说说"),
            "like_possibility": ConfigField(type=float, default=1.0, description="麦麦读说说后点赞的概率（0到1）"),
            "comment_possibility": ConfigField(type=float, default=1.0, description="麦麦读说说后评论的概率（0到1）"),
        },
        "monitor": {
            "enable_auto_monitor": ConfigField(
                type=bool, default=False, description="是否启用刷空间（自动阅读所有好友说说）"
            ),
            "enable_auto_reply": ConfigField(
                type=bool, default=False, description="是否启用自动回复自己说说的评论（当enable_auto_monitor为True）"
            ),
            "interval_minutes": ConfigField(type=int, default=15, description="阅读间隔(分钟)"),
        },
        "schedule": {
            "enable_schedule": ConfigField(type=bool, default=False, description="是否启用定时发送说说"),
            "schedule_times": ConfigField(
                type=list, default=["08:00", "20:00"], description="定时发送时间列表，按照示例添加修改"
            ),
            "fluctuation_minutes": ConfigField(
                type=int, default=0, description="发送时间上下浮动范围（分钟），0表示不浮动"
            ),
            "random_topic": ConfigField(
                type=bool,
                default=True,
                description="是否使用随机主题（可能会导致重复说说的发布，请关注history_number的设置）",
            ),
            "fixed_topics": ConfigField(
                type=list,
                default=[
                    "今日穿搭",
                    "日常碎片PLOG",
                    "生活仪式感",
                    "治愈系天空",
                    "理想的家",
                    "周末去哪儿",
                    "慢生活",
                    "今天吃什么呢",
                    "懒人食谱",
                    "居家咖啡馆",
                    "探店美食",
                    "说走就走的旅行",
                    "小众旅行地",
                    "治愈系风景",
                    "一起去露营",
                    "逛公园",
                    "博物馆奇遇",
                    "穿搭灵感",
                    "复古穿搭",
                    "今日妆容",
                    "护肤日常",
                    "小众品牌",
                    "我家宠物好可爱",
                    "阳台花园",
                    "运动打卡",
                    "瑜伽日常",
                    "轻食记",
                    "看书打卡",
                    "我的观影报告",
                    "咖啡店日记",
                    "手帐分享",
                    "画画日常",
                    "手工DIY",
                    "沙雕日常",
                    "沉浸式体验",
                    "开箱视频",
                    "提升幸福感的小物",
                    "圣诞氛围感",
                    "冬日限定快乐",
                    "灵感碎片",
                    "艺术启蒙",
                    "色彩美学",
                    "每日一诗",
                    "哲学小谈",
                    "存在主义咖啡馆",
                    "艺术史趣闻",
                    "审美积累",
                    "现代主义漫步",
                    "东方美学",
                ],
                description="固定主题列表（当random_topic为False时从中随机选择）",
            ),
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitor = None
        self.scheduler = None

        if self.get_config("plugin.enable", True):
            self.enable_plugin = True

            if self.get_config("monitor.enable_auto_monitor", False):
                self.monitor = FeedMonitor(self)
                asyncio.create_task(self._start_monitor_after_delay())

            if self.get_config("schedule.enable_schedule", False):
                self.scheduler = ScheduleSender(self)
                asyncio.create_task(self._start_scheduler_after_delay())
        else:
            self.enable_plugin = False

    async def _start_monitor_after_delay(self):
        """延迟启动监控任务"""
        await asyncio.sleep(10)
        if self.monitor:
            await self.monitor.start()

    async def _start_scheduler_after_delay(self):
        """延迟启动日程任务"""
        await asyncio.sleep(10)
        if self.scheduler:
            await self.scheduler.start()

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (SendFeedCommand.get_command_info(), SendFeedCommand),
            (SendFeedAction.get_action_info(), SendFeedAction),
            (ReadFeedAction.get_action_info(), ReadFeedAction),
        ]
