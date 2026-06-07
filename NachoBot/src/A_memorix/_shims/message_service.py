"""垫片模块: 模拟 src.services.message_service 接口。

A_Memorix 中 sdk_memory_kernel.py 和 summary_importer.py 会导入此模块，
用于获取聊天记录。NachoBot 使用 plugin_system.apis.message_api 提供类似功能。
"""

from src.common.logger import get_logger

logger = get_logger("a_memorix.message_shim")


def get_messages_by_chat_id(chat_id: str, limit: int = 50, **kwargs):
    """获取指定聊天流的消息列表。"""
    try:
        from src.plugin_system.apis import message_api
        import time
        return message_api.get_messages_by_time_in_chat(
            chat_id=chat_id,
            start_time=0,
            end_time=time.time(),
            limit=limit,
            limit_mode="latest",
        )
    except Exception as e:
        logger.warning(f"获取消息失败: {e}")
        return []


def count_messages_since(chat_id: str, since_time: float) -> int:
    """统计指定时间后的消息数量。"""
    try:
        from src.plugin_system.apis import message_api
        import time
        return message_api.count_new_messages(
            chat_id=chat_id,
            start_time=since_time,
            end_time=time.time(),
        )
    except Exception:
        return 0
