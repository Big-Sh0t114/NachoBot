"""
表情API模块

提供表情包相关功能，采用标准Python包设计模式
使用方式：
    from src.plugin_system.apis import emoji_api
    result = await emoji_api.get_by_description("开心")
    count = emoji_api.get_count()
"""

import os
import random
import re

from dataclasses import dataclass
from typing import Optional, Tuple, List
from rapidfuzz.fuzz import partial_ratio
from src.common.logger import get_logger
from src.chat.emoji_system.emoji_manager import get_emoji_manager
from src.chat.utils.utils_image import image_path_to_base64

logger = get_logger("emoji_api")


@dataclass(frozen=True)
class EmojiCandidate:
    """A stable reference to an emoji considered for visual selection."""

    emoji_hash: str
    full_path: str
    description: str
    emotions: tuple[str, ...]
    image_format: str
    matched_tag: bool


def _normalize_emotion(emotion: str) -> str:
    return emotion.strip().casefold()


def _get_valid_emojis():
    emoji_manager = get_emoji_manager()
    return [emoji for emoji in emoji_manager.emoji_objects if not emoji.is_deleted and os.path.isfile(emoji.full_path)]


def get_available_emotions() -> List[str]:
    """Return all usable emotion tags, normalized for duplicate detection."""
    emotions_by_key: dict[str, str] = {}
    for emoji in _get_valid_emojis():
        for emotion in emoji.emotion:
            cleaned = emotion.strip()
            if cleaned:
                emotions_by_key.setdefault(cleaned.casefold(), cleaned)
    return sorted(emotions_by_key.values(), key=lambda value: value.casefold())


def _search_terms(text: str) -> set[str]:
    normalized = text.casefold()
    terms = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", normalized))
    terms.update(
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
        if "\u4e00" <= normalized[index] <= "\u9fff" and "\u4e00" <= normalized[index + 1] <= "\u9fff"
    )
    return terms


def _relevance_score(query: str, searchable_text: str) -> float:
    if not query.strip() or not searchable_text.strip():
        return 0.0
    query_terms = _search_terms(query)
    text_terms = _search_terms(searchable_text)
    overlap = len(query_terms & text_terms) / max(len(query_terms), 1)
    return partial_ratio(query.casefold(), searchable_text.casefold()) / 100 + overlap * 2


def get_relevant_emotions(query: str, limit: int = 30) -> List[str]:
    """Return a compact tag list ranked against the current user request."""
    emotions = get_available_emotions()
    if not query.strip():
        return emotions[:limit]
    return sorted(emotions, key=lambda emotion: _relevance_score(query, emotion), reverse=True)[:limit]


def sample_candidates_by_emotion(
    emotion: str,
    query: str = "",
    count: int = 10,
    backup_count: int = 5,
    max_tag_matches: int = 4,
) -> List[EmojiCandidate]:
    """Mix bounded tag matches with candidates relevant to the user request.

    Extra candidates are returned as decode fallbacks. The collage builder still
    displays at most ``count`` successfully decoded images.
    """
    if not isinstance(emotion, str):
        raise TypeError("情感标签必须是字符串类型")
    if not emotion.strip():
        raise ValueError("情感标签不能为空")
    if not isinstance(query, str):
        raise TypeError("query 必须是字符串类型")
    if not all(isinstance(value, int) for value in (count, backup_count, max_tag_matches)):
        raise TypeError("count、backup_count 和 max_tag_matches 必须是整数类型")
    if count <= 0 or backup_count < 0 or max_tag_matches < 0:
        raise ValueError("count 必须大于0，backup_count 和 max_tag_matches 不能为负数")

    target_tag = _normalize_emotion(emotion)
    valid_emojis = _get_valid_emojis()
    matching = [
        emoji
        for emoji in valid_emojis
        if target_tag in {_normalize_emotion(tag) for tag in emoji.emotion if tag.strip()}
    ]
    pool_size = min(len(valid_emojis), count + backup_count)

    tag_limit = min(len(matching), max_tag_matches, pool_size)
    ranked_matches = sorted(
        matching,
        key=lambda emoji: _relevance_score(query, " ".join((emoji.description, *emoji.emotion))),
        reverse=True,
    )
    selected_tag_matches = ranked_matches[:tag_limit]
    ranked_non_matching = sorted(
        (emoji for emoji in valid_emojis if emoji not in matching),
        key=lambda emoji: _relevance_score(query, " ".join((emoji.description, *emoji.emotion))),
        reverse=True,
    )
    ranked_remaining = ranked_non_matching + ranked_matches[tag_limit:]

    primary = selected_tag_matches + ranked_remaining[: max(0, count - tag_limit)]
    random.shuffle(primary)
    backup = ranked_remaining[max(0, count - tag_limit) : max(0, count - tag_limit) + backup_count]
    selected = (primary + backup)[:pool_size]

    return [
        EmojiCandidate(
            emoji_hash=emoji.hash,
            full_path=emoji.full_path,
            description=emoji.description,
            emotions=tuple(emoji.emotion),
            image_format=emoji.format,
            matched_tag=emoji in matching,
        )
        for emoji in selected
    ]


def record_usage(emoji_hash: str) -> None:
    """Record one successfully delivered emoji."""
    get_emoji_manager().record_usage(emoji_hash)


# =============================================================================
# 表情包获取API函数
# =============================================================================


async def get_by_description(description: str) -> Optional[Tuple[str, str, str]]:
    """根据描述选择表情包

    Args:
        description: 表情包的描述文本，例如"开心"、"难过"、"愤怒"等

    Returns:
        Optional[Tuple[str, str, str]]: (base64编码, 表情包描述, 匹配的情感标签) 或 None

    Raises:
        ValueError: 如果描述为空字符串
        TypeError: 如果描述不是字符串类型
    """
    if not description:
        raise ValueError("描述不能为空")
    if not isinstance(description, str):
        raise TypeError("描述必须是字符串类型")
    try:
        logger.debug(f"[EmojiAPI] 根据描述获取表情包: {description}")

        emoji_manager = get_emoji_manager()
        emoji_result = await emoji_manager.get_emoji_for_text(description)

        if not emoji_result:
            logger.warning(f"[EmojiAPI] 未找到匹配描述 '{description}' 的表情包")
            return None

        emoji_path, emoji_description, matched_emotion = emoji_result
        emoji_base64 = image_path_to_base64(emoji_path)

        if not emoji_base64:
            logger.error(f"[EmojiAPI] 无法将表情包文件转换为base64: {emoji_path}")
            return None

        logger.debug(f"[EmojiAPI] 成功获取表情包: {emoji_description}, 匹配情感: {matched_emotion}")
        return emoji_base64, emoji_description, matched_emotion

    except Exception as e:
        logger.error(f"[EmojiAPI] 获取表情包失败: {e}")
        return None


async def get_random(count: Optional[int] = 1) -> List[Tuple[str, str, str]]:
    """随机获取指定数量的表情包

    Args:
        count: 要获取的表情包数量，默认为1

    Returns:
        List[Tuple[str, str, str]]: 包含(base64编码, 表情包描述, 随机情感标签)的元组列表，失败则返回空列表

    Raises:
        TypeError: 如果count不是整数类型
        ValueError: 如果count为负数
    """
    if not isinstance(count, int):
        raise TypeError("count 必须是整数类型")
    if count < 0:
        raise ValueError("count 不能为负数")
    if count == 0:
        logger.warning("[EmojiAPI] count 为0，返回空列表")
        return []

    try:
        emoji_manager = get_emoji_manager()
        all_emojis = emoji_manager.emoji_objects

        if not all_emojis:
            logger.warning("[EmojiAPI] 没有可用的表情包")
            return []

        # 过滤有效表情包
        valid_emojis = [emoji for emoji in all_emojis if not emoji.is_deleted]
        if not valid_emojis:
            logger.warning("[EmojiAPI] 没有有效的表情包")
            return []

        if len(valid_emojis) < count:
            logger.warning(
                f"[EmojiAPI] 有效表情包数量 ({len(valid_emojis)}) 少于请求的数量 ({count})，将返回所有有效表情包"
            )
            count = len(valid_emojis)

        # 随机选择
        selected_emojis = random.sample(valid_emojis, count)

        results = []
        for selected_emoji in selected_emojis:
            emoji_base64 = image_path_to_base64(selected_emoji.full_path)

            if not emoji_base64:
                logger.error(f"[EmojiAPI] 无法转换表情包为base64: {selected_emoji.full_path}")
                continue

            matched_emotion = random.choice(selected_emoji.emotion) if selected_emoji.emotion else "随机表情"

            # 记录使用次数
            emoji_manager.record_usage(selected_emoji.hash)
            results.append((emoji_base64, selected_emoji.description, matched_emotion))

        if not results and count > 0:
            logger.warning("[EmojiAPI] 随机获取表情包失败，没有一个可以成功处理")
            return []

        logger.debug(f"[EmojiAPI] 成功获取 {len(results)} 个随机表情包")
        return results

    except Exception as e:
        logger.error(f"[EmojiAPI] 获取随机表情包失败: {e}")
        return []


async def get_by_emotion(emotion: str) -> Optional[Tuple[str, str, str]]:
    """根据情感标签获取表情包

    Args:
        emotion: 情感标签，如"happy"、"sad"、"angry"等

    Returns:
        Optional[Tuple[str, str, str]]: (base64编码, 表情包描述, 匹配的情感标签) 或 None

    Raises:
        ValueError: 如果情感标签为空字符串
        TypeError: 如果情感标签不是字符串类型
    """
    if not emotion:
        raise ValueError("情感标签不能为空")
    if not isinstance(emotion, str):
        raise TypeError("情感标签必须是字符串类型")
    try:
        logger.info(f"[EmojiAPI] 根据情感获取表情包: {emotion}")

        emoji_manager = get_emoji_manager()
        all_emojis = emoji_manager.emoji_objects

        # 筛选匹配情感的表情包
        matching_emojis = []
        matching_emojis.extend(
            emoji_obj
            for emoji_obj in all_emojis
            if not emoji_obj.is_deleted and emotion.lower() in [e.lower() for e in emoji_obj.emotion]
        )
        if not matching_emojis:
            logger.warning(f"[EmojiAPI] 未找到匹配情感 '{emotion}' 的表情包")
            return None

        # 随机选择匹配的表情包
        selected_emoji = random.choice(matching_emojis)
        emoji_base64 = image_path_to_base64(selected_emoji.full_path)

        if not emoji_base64:
            logger.error(f"[EmojiAPI] 无法转换表情包为base64: {selected_emoji.full_path}")
            return None

        # 记录使用次数
        emoji_manager.record_usage(selected_emoji.hash)

        logger.info(f"[EmojiAPI] 成功获取情感表情包: {selected_emoji.description}")
        return emoji_base64, selected_emoji.description, emotion

    except Exception as e:
        logger.error(f"[EmojiAPI] 根据情感获取表情包失败: {e}")
        return None


# =============================================================================
# 表情包信息查询API函数
# =============================================================================


def get_count() -> int:
    """获取表情包数量

    Returns:
        int: 当前可用的表情包数量
    """
    try:
        emoji_manager = get_emoji_manager()
        return emoji_manager.emoji_num
    except Exception as e:
        logger.error(f"[EmojiAPI] 获取表情包数量失败: {e}")
        return 0


def get_info():
    """获取表情包系统信息

    Returns:
        dict: 包含表情包数量、最大数量、可用数量信息
    """
    try:
        emoji_manager = get_emoji_manager()
        return {
            "current_count": emoji_manager.emoji_num,
            "max_count": emoji_manager.emoji_num_max,
            "available_emojis": len([e for e in emoji_manager.emoji_objects if not e.is_deleted]),
        }
    except Exception as e:
        logger.error(f"[EmojiAPI] 获取表情包信息失败: {e}")
        return {"current_count": 0, "max_count": 0, "available_emojis": 0}


def get_emotions() -> List[str]:
    """获取所有可用的情感标签

    Returns:
        list: 所有表情包的情感标签列表（去重）
    """
    try:
        emoji_manager = get_emoji_manager()
        emotions = set()

        for emoji_obj in emoji_manager.emoji_objects:
            if not emoji_obj.is_deleted and emoji_obj.emotion:
                emotions.update(emoji_obj.emotion)

        return sorted(list(emotions))
    except Exception as e:
        logger.error(f"[EmojiAPI] 获取情感标签失败: {e}")
        return []


def get_descriptions() -> List[str]:
    """获取所有表情包描述

    Returns:
        list: 所有可用表情包的描述列表
    """
    try:
        emoji_manager = get_emoji_manager()
        descriptions = []

        descriptions.extend(
            emoji_obj.description
            for emoji_obj in emoji_manager.emoji_objects
            if not emoji_obj.is_deleted and emoji_obj.description
        )
        return descriptions
    except Exception as e:
        logger.error(f"[EmojiAPI] 获取表情包描述失败: {e}")
        return []
