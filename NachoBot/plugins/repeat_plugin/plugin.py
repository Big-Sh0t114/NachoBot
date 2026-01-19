from typing import Tuple, Optional, List, Type, Dict
from collections import deque
import re
import random

from src.plugin_system import (
    BasePlugin,
    BaseEventHandler,
    register_plugin,
    ComponentInfo,
    ConfigField,
    EventType,
    NachoMessages,
)
from src.common.logger import get_logger

logger = get_logger("repeat_plugin")


# ---------------- 工具函数 ----------------
def _safe_str(x) -> str:
    return str(x) if x is not None else ""


def _dig(obj, path: str, default=None):
    """支持属性或字典路径访问"""
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return default
        if hasattr(cur, seg):
            cur = getattr(cur, seg)
        elif isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return default
    return cur


def _first_text(*vals) -> str:
    """返回第一个非空字符串"""
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


# ---------------- 复读处理器 ----------------
class RepeatHandler(BaseEventHandler):
    event_type = EventType.ON_MESSAGE
    handler_name = "repeat_handler"
    handler_description = "检测群聊中连续重复消息并进行复读"

    chat_history: Dict[str, deque] = {}
    last_repeated_message: Optional[str] = None

    async def execute(self, message: NachoMessages | None) -> Tuple[bool, bool, Optional[str], None]:
        """复读逻辑主函数"""
        debug_mode: bool = self.get_config("repeat.debug_mode", False)
        repeat_probability: float = self.get_config("repeat.repeat_probability", 1.0)
        skip_probability: float = self.get_config("repeat.skip_probability", 0.0)

        if message is None:
            if debug_mode:
                logger.info("[repeat_plugin][repeat_handler] message is None")
            return True, True, None, None, None

        # ========== 自动调试 stream_id 字段 ==========
        if debug_mode:
            logger.info("[repeat_plugin][debug] message 类型: %s", type(message))
            attrs = dir(message)
            possible_streams = [a for a in attrs if "stream" in a.lower()]
            logger.info("[repeat_plugin][debug] message 中包含的字段: %s", possible_streams)
            # 进一步打印一些可能的值
            for field in ["stream_id", "streamInfo", "stream_info", "message_base_info.stream_id"]:
                val = _dig(message, field)
                if val:
                    logger.info(f"[repeat_plugin][debug] 可能的 stream_id 来源 {field} = {val}")

        # 尝试获取框架内部 stream_id
        stream_id = (
            getattr(message, "stream_id", None)
            or _dig(message, "stream_info.stream_id")
            or _dig(message, "message_base_info.stream_id")
        )

        if not stream_id:
            if debug_mode:
                logger.warning("[repeat_plugin][repeat_handler] 未找到有效的 stream_id，无法发送消息。")
            return True, True, None, None, None

        stream_id = _safe_str(stream_id)

        # 获取群号（可选，仅日志用）
        group_id = _first_text(
            _dig(message, "message_base_info.group_id"),
            _dig(message, "group_id"),
            _dig(message, "ctx.group_id"),
            _dig(message, "context.group_id"),
        )
        group_id = _safe_str(group_id)

        # 获取消息文本
        text = _first_text(
            _dig(message, "processed_plain_text"),
            _dig(message, "message_content"),
            _dig(message, "content"),
            _dig(message, "message_base_info.content"),
            _dig(message, "raw_message"),
            _dig(message, "text"),
        )

        if not text:
            if debug_mode:
                logger.info(f"[repeat_plugin] 群={group_id} 消息文本为空，跳过。")
            return True, True, None, None, None

        # 丢弃通知消息
        if re.search(r'"post_type"\s*:\s*"notice"', text):
            if debug_mode:
                logger.info(f"[repeat_plugin] 群={group_id} 消息为通知事件，跳过。")
            return True, True, None, None, None

        # 丢弃 CQ 码消息（图片、表情等）
        if text.startswith("[CQ:"):
            if debug_mode:
                logger.info(f"[repeat_plugin] 群={group_id} 特殊格式消息，跳过。")
            return True, True, None, None, None

        # 不复读机器人自己的消息
        if getattr(message, "is_self", False):
            if text == self.last_repeated_message:
                self.last_repeated_message = None
            if debug_mode:
                logger.info(f"[repeat_plugin] 群={group_id} 是机器人自己的消息，跳过。")
            return True, True, None, None, None

        # 初始化队列
        if stream_id not in self.chat_history:
            self.chat_history[stream_id] = deque(maxlen=3)
        history = self.chat_history[stream_id]

        # 检测重复
        reply_text = None
        if len(history) >= 2 and history[-1] == history[-2] == text:
            # 判断跳过概率
            if random.random() <= skip_probability:
                if debug_mode:
                    logger.info(f"[repeat_plugin] 群={group_id} 命中跳过概率，不复读。")
                history.append(text)
                return True, True, None, None, None

            # 判断复读概率
            if random.random() <= repeat_probability:
                reply_text = text

        # 执行复读
        if reply_text and reply_text != self.last_repeated_message:
            reply_text_cleaned = re.sub(r'@<([^:]+?):\d+>', r'@\1', reply_text)

            # ✅ 用框架的消息发送接口
            success = await self.send_text(stream_id=stream_id, text=reply_text_cleaned)
            if debug_mode:
                logger.info(f"[repeat_plugin] 复读到 stream_id={stream_id} 群={group_id} 内容='{reply_text_cleaned}' 成功={success}")

            self.last_repeated_message = reply_text

        # 更新队列
        history.append(text)
        return True, True, None, None, None


# ---------------- 插件注册 ----------------
@register_plugin
class RepeatPlugin(BasePlugin):
    plugin_name: str = "repeat_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "repeat": "复读功能配置",
    }

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="repeat_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "repeat": {
            "debug_mode": ConfigField(type=bool, default=True, description="是否开启调试模式"),
            "repeat_probability": ConfigField(type=float, default=0.7, description="复读概率 (0~1)"),
            "skip_probability": ConfigField(type=float, default=0.1, description="不复读概率 (0~1)"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (RepeatHandler.get_handler_info(), RepeatHandler),
        ]
