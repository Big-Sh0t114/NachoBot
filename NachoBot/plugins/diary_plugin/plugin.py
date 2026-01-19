"""
日记插件主入口文件

这是日记插件的主入口文件，负责插件的注册和组件管理。
所有核心功能已拆分到core模块中，本文件仅包含插件框架相关的代码。

主要功能：
- 插件注册和配置管理
- 组件信息提供
- 定时任务调度器管理
- 插件状态监控和日志记录

核心模块：
- core.storage: 数据存储和API集成
- core.actions: 日记生成核心逻辑
- core.commands: 命令处理器
- core.scheduler: 定时任务调度器

Author: MaiBot Team
Version: 2.1.0
"""

import asyncio
from typing import List, Tuple, Type

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    ComponentInfo,
    ConfigField
)
from src.plugin_system.apis import (
    get_logger
)

# 从core模块导入所有必要的组件
from .core import (
    DiaryGeneratorAction,
    DiaryScheduler,
    DiaryStorage,
    DiaryGenerateCommand,
    DiaryGenerateAllCommand,
    DiaryListCommand,
    DiaryViewCommand,
    DiaryDebugCommand,
    DiaryHelpCommand,
    DiaryManageCommand,
)

# 导入工具组件
from .core import EmotionAnalysisTool

logger = get_logger("diary_plugin")


@register_plugin
class DiaryPlugin(BasePlugin):
    """
    日记插件主类 - 插件系统入口
    
    这是日记插件的主要入口类，负责插件的初始化、配置管理和组件注册。
    所有核心业务逻辑都已拆分到core模块中，本类专注于插件框架相关的功能。
    
    主要职责：
    - 插件注册和配置schema定义
    - 组件信息提供和管理
    - 定时任务调度器的生命周期管理
    - 插件状态监控和日志记录
    
    配置结构：
    - plugin: 插件基础配置
    - diary_generation: 日记生成相关配置
    - qzone_publishing: QQ空间发布配置
    - custom_model: 自定义模型配置
    - schedule: 定时任务配置
    
    组件列表：
    - DiaryGeneratorAction: 日记生成Action
    - EmotionAnalysisTool: 情感分析工具
    - DiaryManageCommand: 日记管理命令
    
    特性：
    - 自动启动定时任务调度器
    - 智能配置状态检测和日志记录
    - 完整的错误处理和异常管理
    - 支持插件热重载和状态恢复
    """
    
    plugin_name = "diary_plugin"
    enable_plugin = True
    dependencies = []
    python_dependencies = ["httpx", "pytz", "openai"]
    config_file_name = "config.toml"
    
    config_section_descriptions = {
        "plugin": "插件基础配置",
        "diary_generation": "日记生成相关配置",
        "qzone_publishing": "QQ空间发布配置",
        "custom_model": "自定义模型配置（仅支持OpenAI格式）",
        "schedule": "定时任务配置"
    }
    
    config_schema = {
        "plugin": {
            "_section_description": "# diary_plugin - 日记插件配置\n# 让麦麦能够回忆和记录每一天的聊天,生成个性化的日记内容\n\n# 插件基础配置",
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="2.2.4", description="配置文件版本"),
            "admin_qqs": ConfigField(type=list, default=[], description="管理员QQ号列表,用于使用测试命令 (示例:[111,222])"),
            # 新增：组件级开关
            "enable_action": ConfigField(type=bool, default=False, description="是否注册并启用 DiaryGeneratorAction"),
            "enable_tool": ConfigField(type=bool, default=False, description="是否注册并启用 EmotionAnalysisTool"),
            "enable_command": ConfigField(type=bool, default=True, description="是否注册并启用 DiaryManageCommand")
        },
        "diary_generation": {
            "_section_description": "\n# 日记生成相关配置",
            "min_message_count": ConfigField(type=int, default=3, description="生成日记所需的最少消息总数"),
            "min_messages_per_chat": ConfigField(type=int, default=3, description="单个群组聊天条数少于此值时该群组消息不参与日记生成"),
            "style": ConfigField(type=str, default="diary", description="生成样式: diary | qqzone | custom"),
            "custom_prompt": ConfigField(
                type=str,
                default="",
                description="当 style=custom 时使用的模板。可用占位符: {date},{timeline},{date_with_weather},{target_length},{personality_desc},{style},{interest},{name}"
            ),
            "enable_syle_send": ConfigField(type=bool, default=False, description="是否开启回复改写")
        },
        "qzone_publishing": {
            "_section_description": "\n# QQ空间发布配置",
            "qzone_min_word_count": ConfigField(type=int, default=150, description="最小字数，范围20-8000"),
            "qzone_max_word_count": ConfigField(type=int, default=350, description="最大字数，范围20-8000，必须≥最小值"),
            "napcat_host": ConfigField(type=str, default="127.0.0.1", description="Napcat服务地址,Docker环境可使用'napcat'"),
            "napcat_port": ConfigField(type=str, default="9998", description="Napcat服务端口"),
            "napcat_token": ConfigField(type=str, default="", description="Napcat服务认证Token,在Napcat WebUI的网络配置中设置,为空则不使用token")
        },
        "custom_model": {
            "_section_description": "\n# 自定义模型配置",
            "use_custom_model": ConfigField(type=bool, default=False, description="自定义模型（不启用则默认使用系统首要回复模型）"),
            "api_url": ConfigField(type=str, default="http://rinkoai.com/v1", description="仅支持OpenAI API格式的模型服务,不支持Google Gemini、Anthropic Claude等原生格式\n# 推荐使用的站点: http://rinkoai.com/pricing\n# 咨询答疑群: 1054544611"),
            "api_key": ConfigField(type=str, default="your-rinko-key-here", description="API密钥"),
            "model_name": ConfigField(type=str, default="Pro/deepseek-ai/DeepSeek-V3", description="模型名称"),
            "temperature": ConfigField(type=float, default=0.7, description="生成温度"),
            "api_timeout": ConfigField(type=int, default=300, description="API调用超时时间（秒），大量聊天记录时建议设置更长时间"),
            "max_context_tokens": ConfigField(type=int, default=256, description="模型上下文长度（单位：k）,填写模型的真实上限")
        },
        "schedule": {
            "_section_description": "\n# 定时任务配置",
            "schedule_time": ConfigField(type=str, default="23:30", description="每日生成日记的时间 (HH:MM格式)"),
            "timezone": ConfigField(type=str, default="Asia/Shanghai", description="时区设置"),
            "filter_mode": ConfigField(type=str, default="whitelist", description="过滤模式，可选值：whitelist(白名单), blacklist(黑名单)"),
            "target_chats": ConfigField(type=list, default=[], description="目标列表，格式：[\"group:群号\", \"private:用户qq号\"]\n# 示例：[\"group:123456789\", \"private:987654321\"]\n# 白名单模式：空列表=禁用定时任务，有内容=只处理列表中的聊天\n# 黑名单模式：空列表=处理全部聊天，有内容=处理除列表外的聊天")
        }
    }
    
    def __init__(self, plugin_dir: str, **kwargs):
        """
        初始化日记插件
        
        Args:
            plugin_dir (str): 插件目录路径
            **kwargs: 其他插件初始化参数
        """
        super().__init__(plugin_dir, **kwargs)
        self.scheduler = None
        self.logger = get_logger("DiaryPlugin")
        self.storage = DiaryStorage()
        
        # 显示插件配置状态
        self._log_plugin_status()
        
        # 启动定时任务
        self.scheduler = DiaryScheduler(self)
        asyncio.create_task(self._start_scheduler_after_delay())
    
    def _log_plugin_status(self):
        """
        显示插件配置状态（info级别）
        
        读取并显示插件的关键配置信息，包括管理员配置、过滤模式、
        定时任务状态和模型配置等。用于插件启动时的状态检查。
        """
        try:
            # 读取基本配置
            admin_qqs = [str(admin_id) for admin_id in self.get_config("plugin.admin_qqs", [])]
            filter_mode = self.get_config("schedule.filter_mode", "whitelist")
            target_chats = self.get_config("schedule.target_chats", [])
            use_custom_model = self.get_config("custom_model.use_custom_model", False)
            
            # 显示管理员配置
            if admin_qqs:
                self.logger.info(f"管理员已配置: {len(admin_qqs)}个")
            else:
                self.logger.info("管理员未配置,所有用户无权限使用命令")
            
            # 显示过滤模式和定时任务状态
            if filter_mode == "whitelist":
                if target_chats:
                    self.logger.info(f"白名单模式: 已配置{len(target_chats)}个目标聊天,定时任务将启动")
                else:
                    self.logger.info("白名单模式: 目标列表未配置,定时任务已禁用")
            elif filter_mode == "blacklist":
                if target_chats:
                    self.logger.info(f"黑名单模式: 排除{len(target_chats)}个聊天,定时任务将启动")
                else:
                    self.logger.info("黑名单模式: 无排除列表,处理全部聊天,定时任务将启动")
            
            # 显示Napcat token配置状态
            napcat_token = self.get_config("qzone_publishing.napcat_token", "")
            if napcat_token:
                self.logger.info("Napcat Token已配置,QQ空间发布功能启用安全验证")
            else:
                self.logger.info("Napcat Token未配置,将使用无Token模式连接")
            
            # 显示模型配置
            if use_custom_model:
                model_name = self.get_config("custom_model.model_name", "未知模型")
                api_key = self.get_config("custom_model.api_key", "")
                if api_key and api_key != "your-rinko-key-here":
                    self.logger.info(f"自定义模型已启用: {model_name}")
                else:
                    self.logger.info("自定义模型已启用但API密钥未配置,将使用默认模型")
            else:
                self.logger.info("使用系统默认模型")
                
        except Exception as e:
            self.logger.error(f"读取插件配置失败: {e}")
    
    async def _start_scheduler_after_delay(self):
        """
        延迟启动定时任务调度器
        
        在插件初始化完成后，延迟10秒再启动定时任务，确保插件完全初始化
        后再开始定时任务，避免初始化过程中的竞争条件。
        
        该方法通过asyncio.create_task在插件初始化时调用，是定时任务启动的
        标准流程。
        
        Note:
            延迟10秒是为了确保所有插件组件都已正确初始化，特别是数据库
            连接和消息API等依赖服务已就绪。
        """
        await asyncio.sleep(10)
        if self.scheduler:
            await self.scheduler.start()

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """
        返回插件包含的组件列表
        
        提供插件系统需要的组件信息，包括Action、Tool和Command组件。
        这些组件将被插件系统自动注册和管理。
        
        Returns:
            List[Tuple[ComponentInfo, Type]]: 组件信息和类型的元组列表
                - DiaryGeneratorAction: 日记生成Action组件
                - EmotionAnalysisTool: 情感分析工具组件
                - DiaryManageCommand: 日记管理命令组件
        
        Note:
            所有组件都已在core模块中实现，本方法仅负责向插件系统注册
        """
        components: List[Tuple[ComponentInfo, Type]] = []
        try:
            enable_action = self.get_config("plugin.enable_action", True)
            enable_tool = self.get_config("plugin.enable_tool", True)
            enable_command = self.get_config("plugin.enable_command", True)
        except Exception:
            enable_action = enable_tool = enable_command = True

        if enable_action:
            components.append((DiaryGeneratorAction.get_action_info(), DiaryGeneratorAction))
        if enable_tool:
            components.append((EmotionAnalysisTool.get_tool_info(), EmotionAnalysisTool))
        if enable_command:
            command_specs = [
                (DiaryGenerateCommand, "admin"),
                (DiaryGenerateAllCommand, "admin"),
                (DiaryListCommand, "admin"),
                (DiaryDebugCommand, "admin"),
                (DiaryViewCommand, "public"),
                (DiaryHelpCommand, "public"),
            ]
            for cmd_cls, perm in command_specs:
                info = cmd_cls.get_command_info()
                info.metadata["permission"] = perm
                components.append((info, cmd_cls))

        return components
