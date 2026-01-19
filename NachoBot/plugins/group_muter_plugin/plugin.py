import time
import logging
import re
from typing import List, Tuple, Type, Dict, Optional, Set

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    BaseEventHandler,
    ComponentInfo,
    EventType,
    ConfigField,
    NachoMessages,
    CustomEventHandlerResult,
    config_api,
)
from src.common.logger import get_logger

logger = get_logger("group_muter_plugin")

# --- 核心状态管理器 ---
class MuteStatus:
    _mute_until: Dict[str, Optional[float]] = {}
    _group_names: Dict[str, str] = {}
    _last_summary_log_time: Dict[str, float] = {}

    @classmethod
    def _key(cls, platform: str, group_id: str) -> str:
        return f"{platform}:{group_id}"

    @classmethod
    def set_mute(cls, platform: str, group_id: str, seconds: Optional[int], group_name: Optional[str]):
        key = cls._key(platform, group_id)
        cls._mute_until[key] = float("inf") if seconds is None else time.time() + seconds
        if group_name:
            cls._group_names[key] = group_name
            GroupMuterLogFilter.add_group(group_name)
        if seconds is None:
            logger.info(f"[{group_name or key}] 进入永久静音模式。")
        else:
            logger.info(f"[{group_name or key}] 进入静音模式，持续 {seconds} 秒。")

    @classmethod
    def clear_mute(cls, platform: str, group_id: str):
        key = cls._key(platform, group_id)
        if cls._mute_until.pop(key, None):
            group_name = cls._group_names.pop(key, None)
            if group_name:
                GroupMuterLogFilter.remove_group(group_name)
            logger.info(f"[{group_name or key}] 已解除静音模式。")

    @classmethod
    def is_muted(cls, platform: str, group_id: str) -> bool:
        key = cls._key(platform, group_id)
        mute_end_time = cls._mute_until.get(key)
        if mute_end_time is None:
            return False
        if mute_end_time == float("inf"):
            return True
        if time.time() >= mute_end_time:
            logger.info(f"[{cls._group_names.get(key, key)}] 静音时间已到，自动解除。")
            cls.clear_mute(platform, group_id)
            return False
        return bool(mute_end_time)

    @classmethod
    def log_summary(cls, platform: str, group_id: str):
        key = cls._key(platform, group_id)
        now = time.time()
        if now - cls._last_summary_log_time.get(key, 0) < 30:
            return
        if mute_end_time := cls._mute_until.get(key):
            if mute_end_time == float("inf"):
                display_name = cls._group_names.get(key, key)
                logger.info(f"[{display_name}] 处于永久静音模式。")
                cls._last_summary_log_time[key] = now
                return
            remaining = int(mute_end_time - now)
            end_str = time.strftime("%H:%M:%S", time.localtime(mute_end_time))
            display_name = cls._group_names.get(key, key)
            logger.info(
                f"[{display_name}] 处于静音模式，剩余 {remaining} 秒，将在 {end_str} 结束。")
            cls._last_summary_log_time[key] = now

# --- 事件处理器 (核心拦截逻辑) ---
class MuteEventInterceptor(BaseEventHandler):
    handler_name = "mute_event_interceptor"
    handler_description = "在消息入口拦截静音群的消息，并处理管理员的唤醒操作"
    event_type = EventType.ON_MESSAGE
    weight = 10000
    intercept_message = True

    async def execute(self, message: Optional[NachoMessages]) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[NachoMessages]]:
        if not message:
            return True, True, None, None, None

        if not message.is_group_message:
            return True, True, "非群聊消息，放行", None, None

        info = message.message_base_info
        platform, group_id = str(info.get("platform", "")), str(
            info.get("group_id", ""))

        # 检查是否处于静音状态
        if not platform or not group_id or not MuteStatus.is_muted(platform, group_id):
            return True, True, "非静音群聊，放行", None, None

        user_id = str(info.get("user_id", ""))
        is_admin = GroupMuterPlugin.check_permission(
            user_id, self.plugin_config)

        # 如果不是管理员，拦截消息
        if not is_admin:
            MuteStatus.log_summary(platform, group_id)
            return True, False, "静音中，非管理员消息已拦截", None, None

        # 检查关键词唤醒
        unmute_keywords = self.get_config("mute.unmute_keywords", [])
        if self.get_config("mute.enable_unmute", True) and _is_keyword_in_text(message.plain_text or "", unmute_keywords):
            return True, True, "管理员解除指令，放行给Command处理", None, None

        # 检查@唤醒
        if self.get_config("mute.at_mention_break", True) and is_bot_mentioned(message):
            MuteStatus.clear_mute(platform, group_id)
            logger.info(f"管理员({user_id})通过'@提及'操作解除了群({group_id})的静音。")
            return True, True, "管理员@提及，解除静音并放行", None, None

        MuteStatus.log_summary(platform, group_id)
        return True, False, "静音中，管理员普通消息已拦截", None, None

# --- 命令组件 ---
class MuteCommand(BaseCommand):
    command_name = "mute"
    command_description = "进入静音模式，格式：#mute_<秒数> 或 #mute_true（永久）"
    command_pattern = ""

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        group_info = None
        if self.message.chat_stream:
            group_info = self.message.chat_stream.group_info
        if not group_info:
            group_info = getattr(self.message.message_info, "group_info", None)
        if not group_info:
            if self.is_force_command():
                await self.send_text("缺少群信息，无法执行该命令。")
                return False, "missing group info", 1
            return False, "私聊禁言吗，有点意思", 1

        user_id = str(self.message.chat_stream.user_info.user_id)
        if not self.is_force_command() and not GroupMuterPlugin.check_permission(user_id, self.plugin_config):
            logger.warning(f"用户 {user_id} 尝试执行静音命令失败：权限不足。")
            await self.send_text("这是只有主人才能做到的事情哦~")
            return False, "权限不足", 2

        platform = self.message.chat_stream.platform
        group_id = str(group_info.group_id)
        group_name = group_info.group_name

        duration_token = (self.matched_groups or {}).get("duration", "")
        duration_token = duration_token.strip().lower()
        duration_seconds: Optional[int]
        if duration_token == "true":
            duration_seconds = None
        else:
            try:
                duration_seconds = int(duration_token)
            except ValueError:
                duration_seconds = 0

        if duration_seconds is not None and duration_seconds <= 0:
            fallback_duration = self.get_config("mute.duration_seconds", 0)
            if isinstance(fallback_duration, int) and fallback_duration > 0:
                duration_seconds = fallback_duration
            else:
                await self.send_text("静音时长需要写成 #mute_<秒数>，或使用 #mute_true 设置永久静音。")
                return False, "invalid duration", 2

        MuteStatus.set_mute(platform, group_id, duration_seconds, group_name)
        await self.send_text("Zzz..")
        if duration_seconds is None:
            return True, f"已为群聊 {group_name or group_id} 开启永久静音模式。", 2
        return True, f"已为群聊 {group_name or group_id} 开启静音模式，持续 {duration_seconds} 秒。", 2


class MuteDefaultCommand(BaseCommand):
    command_name = "mute_default"
    command_description = "进入静音模式（默认 300 秒），格式：#mute"
    command_pattern = ""

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        group_info = None
        if self.message.chat_stream:
            group_info = self.message.chat_stream.group_info
        if not group_info:
            group_info = getattr(self.message.message_info, "group_info", None)
        if not group_info:
            if self.is_force_command():
                await self.send_text("缺少群信息，无法执行该命令。")
                return False, "missing group info", 1
            return False, "私聊禁言吗，有点意思", 1

        platform = self.message.chat_stream.platform
        group_id = str(group_info.group_id)
        group_name = group_info.group_name

        duration_seconds = self.get_config("mute.duration_seconds", 300)
        if not isinstance(duration_seconds, int) or duration_seconds <= 0:
            duration_seconds = 300

        MuteStatus.set_mute(platform, group_id, duration_seconds, group_name)
        await self.send_text("Zzz..")
        return True, f"已为群聊 {group_name or group_id} 开启静音模式，持续 {duration_seconds} 秒。", 2


class UnmuteCommand(BaseCommand):
    command_name = "unmute"
    command_description = "解除静音模式"
    command_pattern = ""

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        group_info = None
        if self.message.chat_stream:
            group_info = self.message.chat_stream.group_info
        if not group_info:
            group_info = getattr(self.message.message_info, "group_info", None)
        if not group_info:
            if self.is_force_command():
                await self.send_text("缺少群信息，无法执行该命令。")
                return False, "missing group info", 1
            return False, "该命令仅在群聊中有效。", 1

        user_id = str(self.message.chat_stream.user_info.user_id)
        if not self.is_force_command() and not GroupMuterPlugin.check_permission(user_id, self.plugin_config):
            logger.warning(f"用户 {user_id} 尝试执行解除静音命令失败：权限不足。")
            return False, "权限不足", 2

        platform = self.message.chat_stream.platform
        group_id = str(group_info.group_id)
        group_name = group_info.group_name

        MuteStatus.clear_mute(platform, group_id)
        return True, None, 2

# --- 日志过滤器 ---
class GroupMuterLogFilter(logging.Filter):
    muted_group_names: Set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if "group_muter_plugin" in record.name:
            return True

        msg = record.getMessage()
        is_chat_log = record.name in ("chat", "normal_chat", "memory", "events_manager")
        if not is_chat_log:
            return True

        for group_name in self.muted_group_names:
            if group_name in msg:
                return False
        return True

    @classmethod
    def add_group(cls, group_name: Optional[str]):
        if group_name:
            cls.muted_group_names.add(group_name)

    @classmethod
    def remove_group(cls, group_name: Optional[str]):
        cls.muted_group_names.discard(group_name)

# --- 注册插件 ---
@register_plugin
class GroupMuterPlugin(BasePlugin):
    plugin_name: str = "group_muter_plugin"
    plugin_description: str = "一个允许管理员通过聊天命令，让麦麦在指定群聊中临时进入“静音状态”的群组管理插件。"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List = []
    config_file_name: str = "config.toml"

    config_section_descriptions: Dict[str, str] = {
        "plugin": "插件基本设置",
        "mute": "静音功能相关配置",
        "user_control": "权限控制"
    }

    config_schema: Dict = {
        "plugin": {
            "name": ConfigField(type=str, default="group_muter_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.4.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用此插件"),
        },
        "mute": {
            "duration_seconds": ConfigField(
                type=int, default=300, description="命令未指定时的默认静音时长（秒），用于 #mute"
            ),
            "mute_keywords": ConfigField(
                type=list, default=["#mute_true"], description="触发静音的关键词列表"
            ),
            "unmute_keywords": ConfigField(
                type=list, default=["#mute_false"], description="解除静音的关键词列表"
            ),
            "enable_unmute": ConfigField(
                type=bool, default=True, description="是否启用 '解除静音' 关键词指令"
            ),
            "at_mention_break": ConfigField(
                type=bool, default=True, description="管理员@麦麦时是否自动解除静音"
            ),
        },
        "user_control": {
            "list_type": ConfigField(
                type=str, default="whitelist", description="权限列表类型",
                choices=["whitelist", "blacklist"]
            ),
            "list": ConfigField(
                type=list, default=["2146014839"], description="拥有权限的用户QQ号列表"
            ),
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self._initialize_plugin_settings()
            logger.info(f"群聊静音插件(v{self.get_config('plugin.version')})初始化完成。")
        except Exception as e:
            logger.error(f"群聊静音插件初始化失败: {e}", exc_info=True)
            self.enable_plugin = False

    def _initialize_plugin_settings(self):
        root_logger = logging.getLogger()
        if not any(isinstance(f, GroupMuterLogFilter) for f in root_logger.filters):
            root_logger.addFilter(GroupMuterLogFilter())

        unmute_kws = self.get_config("mute.unmute_keywords", [])

        mention_prefix = r"(?:\[CQ:at,[^\]]+\]\s*|@\S+\s*)*"

        MuteCommand.command_pattern = rf"^{mention_prefix}#mute_(?P<duration>\d+|true)\s*$"
        MuteDefaultCommand.command_pattern = rf"^{mention_prefix}#mute\s*$"
        if self.get_config("mute.enable_unmute", True):
            unmute_pattern = "|".join(re.escape(k) for k in unmute_kws if k.strip())
            UnmuteCommand.command_pattern = rf"^{mention_prefix}(?:{unmute_pattern})\s*$" if unmute_kws else "__NEVER_MATCH__"
        else:
            UnmuteCommand.command_pattern = r"__NEVER_MATCH__"

    @staticmethod
    def check_permission(user_id: str, config: Optional[Dict]) -> bool:
        """ 权限检查函数 """
        if not user_id or not config:
            return False

        user_control_config = config.get("user_control", {})
        list_type = user_control_config.get("list_type", "whitelist")
        user_list_raw = user_control_config.get("list", [])
        user_list = {str(u) for u in user_list_raw} if user_list_raw else set()

        if list_type == "whitelist":
            return user_id in user_list
        if list_type == "blacklist":
            return user_id not in user_list
        return False

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        mute_info = MuteCommand.get_command_info()
        mute_info.metadata["permission"] = "admin"

        components = [
            (MuteEventInterceptor.get_handler_info(), MuteEventInterceptor),
            (mute_info, MuteCommand),
        ]
        mute_default_info = MuteDefaultCommand.get_command_info()
        mute_default_info.metadata["permission"] = "public"
        components.append((mute_default_info, MuteDefaultCommand))
        if self.get_config("mute.enable_unmute", True):
            unmute_info = UnmuteCommand.get_command_info()
            unmute_info.metadata["permission"] = "admin"
            components.append((unmute_info, UnmuteCommand))
        return components

# --- 全局辅助函数 ---
def _is_keyword_in_text(text: str, keywords: List[str]) -> bool:
    if not text or not keywords:
        return False
    clean_text = re.sub(r"\[CQ:at,[^\]]+\]|@\S+", "", text).strip()
    return clean_text in keywords


def is_bot_mentioned(message: NachoMessages) -> bool:
    """
    检查消息是否以任何方式提及了麦麦。
    这包括:
    1. 平台原生的@ (CQ:at)
    2. QQ 你长按头像@ (@<昵称:QQ号>)
    3. 用户手动的文本@ (@昵称)
    """
    if not message:
        return False

    try:
        bot_qq = str(config_api.get_global_config("bot.qq_account"))

        # 检查所有消息段
        for segment in message.message_segments:
            # 方案1: 检查标准的 'at' 类型消息段
            if segment.type == "at":
                if str(segment.data.get("qq")) == bot_qq:
                    return True

            # 检查 QQ 特有的 '@<昵称:QQ号>' 格式
            elif segment.type == "text":
                pattern = rf'@<[^:]+:{re.escape(bot_qq)}>'
                if re.search(pattern, str(segment.data)):
                    return True

        # 降级检查纯文本，兼容用户手动输入 '@昵称'
        plain_text = message.plain_text or ""
        if plain_text.strip():
            bot_nickname = config_api.get_global_config("bot.nickname", "")
            alias_names = config_api.get_global_config("bot.alias_names", [])
            bot_names = {bot_nickname, *alias_names}

            for name in bot_names:
                if name and re.search(rf"@\s*{re.escape(name)}", plain_text):
                    return True

    except Exception as e:
        logger.error(f"检查 @提及 时发生异常: {e}", exc_info=True)

    return False
