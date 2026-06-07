"""
启发式长期记忆注入器

在 HFC 构建 prompt 时，从 A_Memorix 检索与当前聊天上下文相关的长期记忆，
并以 [长期记忆] 标签包裹注入到 chat_content_block 尾部（紧接在最近消息之后），
以避免破坏 KV Cache 前缀。

用法：
    from src.memory_system.heuristic_memory_injector import inject_memory_context
    chat_content_block = await inject_memory_context(
        chat_id=self.stream_id,
        chat_content_block=chat_content_block,
        messages=message_list_before_now,
    )
"""

from __future__ import annotations

import time
from typing import Any, List, Optional, TYPE_CHECKING

from src.common.logger import get_logger

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("memory_injector")

_SOURCE_CHAT_SUMMARY_PREFIX = "chat_summary:"

# 最多注入的记忆片段数
MAX_MEMORY_SNIPPETS = 5
# 单个记忆片段最大字符数（截断保护）
MAX_SNIPPET_LENGTH = 300
# 注入冷却时间（秒）：同一 chat_id 两次注入之间的最小间隔
INJECT_COOLDOWN_SECONDS = 30

# 注入冷却缓存：chat_id -> last_inject_time
_inject_cooldown: dict[str, float] = {}


async def inject_memory_context(
    chat_id: str,
    chat_content_block: str,
    messages: Optional[List["DatabaseMessages"]] = None,
    max_snippets: int = MAX_MEMORY_SNIPPETS,
) -> str:
    """
    尝试从 A_Memorix 检索长期记忆，并注入到 chat_content_block 尾部。

    Args:
        chat_id: 聊天流 ID
        chat_content_block: 已经构建好的聊天内容块
        messages: 当前上下文中的消息列表，用于提取查询关键词
        max_snippets: 最多注入的记忆片段数

    Returns:
        str: 注入了记忆上下文的 chat_content_block
    """
    try:
        from src.memory_system.memory_service import memory_service

        if not memory_service.is_enabled():
            return chat_content_block
    except Exception:
        return chat_content_block

    # 冷却检查
    now = time.time()
    last_time = _inject_cooldown.get(chat_id, 0.0)
    if now - last_time < INJECT_COOLDOWN_SECONDS:
        return chat_content_block

    # 从最近消息中提取查询文本
    query_text = _build_query_from_messages(messages)
    if not query_text:
        return chat_content_block

    try:
        cross_chat_enabled = _is_cross_chat_memory_enabled()
        search_limit = max_snippets * 4 if cross_chat_enabled else max_snippets
        result = await memory_service.search(
            query=query_text,
            chat_id=chat_id,
            limit=search_limit,
            respect_filter=True,
        )

        if not result:
            return chat_content_block

        if isinstance(result, dict):
            if result.get("disabled", False):
                return chat_content_block
            if result.get("success") is False:
                return chat_content_block
        elif not getattr(result, "success", False):
            return chat_content_block

        snippets = _extract_snippets(
            result,
            max_snippets,
            current_chat_id=chat_id,
            cross_chat_enabled=cross_chat_enabled,
        )
        if not snippets:
            return chat_content_block

        # 更新冷却时间
        _inject_cooldown[chat_id] = now

        # 构建注入块
        memory_block = _format_memory_block(snippets)
        logger.info(f"[{chat_id}] 注入 {len(snippets)} 条长期记忆到 prompt 尾部")

        # 注入到尾部
        return f"{chat_content_block}\n{memory_block}"

    except Exception as e:
        logger.debug(f"[{chat_id}] 长期记忆注入失败（不影响主流程）: {e}")
        return chat_content_block


def _build_query_from_messages(
    messages: Optional[List["DatabaseMessages"]],
) -> str:
    """从最近的消息中提取查询文本（取最近 5 条非 bot 消息的纯文本拼接）"""
    if not messages:
        return ""

    from src.memory_system.chat_history_summarizer import is_bot_self

    recent_texts: List[str] = []
    for msg in reversed(messages):
        if is_bot_self(msg.user_info.platform, msg.user_info.user_id):
            continue
        text = getattr(msg, "processed_plain_text", "") or ""
        text = text.strip()
        if text and len(text) > 2:
            recent_texts.append(text)
            if len(recent_texts) >= 5:
                break

    return " ".join(reversed(recent_texts))


def _is_cross_chat_memory_enabled() -> bool:
    """读取是否启用统一跨会话记忆策略。"""
    try:
        from src.A_memorix._compat import config_manager

        return bool(getattr(config_manager.a_memorix.cross_chat_memory, "enabled", False))
    except Exception:
        return False


def _extract_snippets(
    result,
    max_snippets: int,
    *,
    current_chat_id: str = "",
    cross_chat_enabled: bool = False,
) -> List[str]:
    """从 A_Memorix 的搜索结果中提取可用的记忆片段"""
    snippets: List[str] = []

    data = _extract_hit_items(result)
    if not data:
        return []

    if isinstance(data, list):
        for item in data:
            if cross_chat_enabled and not _is_hit_allowed(
                item,
                current_chat_id=current_chat_id,
            ):
                continue
            text = ""
            if isinstance(item, dict):
                text = (
                    item.get("text", "")
                    or item.get("content", "")
                    or item.get("summary", "")
                    or item.get("snippet", "")
                )
            elif isinstance(item, str):
                text = item
            else:
                text = str(getattr(item, "text", "") or getattr(item, "content", "") or "")

            text = text.strip()
            if text:
                if len(text) > MAX_SNIPPET_LENGTH:
                    text = text[:MAX_SNIPPET_LENGTH] + "..."
                snippets.append(text)
                if len(snippets) >= max_snippets:
                    break

    return snippets


def _extract_hit_items(result) -> List[Any]:
    """兼容 A_Memorix 当前 hits 结构和旧格式 data/results。"""
    data = getattr(result, "hits", None) or getattr(result, "data", None) or getattr(result, "results", None)
    if not data and isinstance(result, dict):
        data = result.get("hits") or result.get("data") or result.get("results") or []
    return data if isinstance(data, list) else []


def _is_hit_allowed(item: Any, *, current_chat_id: str) -> bool:
    source_chat_id = _resolve_hit_chat_id(item)
    if not source_chat_id:
        return False
    if source_chat_id == current_chat_id:
        return True
    try:
        from src.A_memorix._compat import AMemorixConfigUtils

        return source_chat_id in set(AMemorixConfigUtils.get_shared_memory_session_ids(current_chat_id))
    except Exception:
        return False


def _resolve_hit_chat_id(item: Any) -> str:
    metadata = _get_hit_metadata(item)
    chat_id = str(metadata.get("chat_id", "") or "").strip()
    if chat_id:
        return chat_id

    source = str(metadata.get("source", "") or "").strip()
    if not source and isinstance(item, dict):
        source = str(item.get("source", "") or "").strip()
    if source.startswith(_SOURCE_CHAT_SUMMARY_PREFIX):
        return source[len(_SOURCE_CHAT_SUMMARY_PREFIX):].strip()
    return ""


def _get_hit_metadata(item: Any) -> dict[str, Any]:
    metadata = item.get("metadata", {}) if isinstance(item, dict) else getattr(item, "metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _format_memory_block(snippets: List[str]) -> str:
    """将记忆片段格式化为可注入的文本块"""
    lines = ["[长期记忆回忆 - 以下是你对过去事件的模糊记忆，可能与当前话题相关]"]
    for i, snippet in enumerate(snippets, 1):
        lines.append(f"  记忆{i}: {snippet}")
    lines.append("[长期记忆回忆结束]")
    return "\n".join(lines)
