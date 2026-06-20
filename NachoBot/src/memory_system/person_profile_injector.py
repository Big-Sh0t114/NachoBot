"""人物画像自动注入服务 — 在 Planner 决策前注入对话参与者的画像信息。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, TYPE_CHECKING

from src.common.logger import get_logger
from src.config.config import global_config
from src.person_info.person_info import get_person_id, get_person_id_by_person_name
from src.A_memorix.core.utils.profile_text import build_profile_injection_text

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("person_profile_injector")

PROFILE_QUERY_LIMIT = 4
PROFILE_TEXT_MAX_CHARS = 900


@dataclass
class _PersonCandidate:
    person_id: str
    person_name: str = ""
    user_id: str = ""
    source: str = ""


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _resolve_person_id(platform: str, user_id: str = "", person_name: str = "") -> str:
    """解析人物 ID，优先按名字查，回退到 platform+user_id 生成。"""
    clean_name = _clean_text(person_name)
    if clean_name:
        try:
            if by_name := get_person_id_by_person_name(clean_name):
                return by_name
        except Exception:
            pass

    clean_platform = _clean_text(platform)
    clean_user_id = _clean_text(user_id)
    if clean_platform and clean_user_id:
        try:
            return get_person_id(clean_platform, clean_user_id)
        except Exception:
            pass

    return ""


def _is_bot_user_id(user_id: str) -> bool:
    bot_user_id = _clean_text(getattr(global_config.bot, "qq_account", ""))
    return bool(user_id and bot_user_id and user_id == bot_user_id)


def collect_person_candidates(
    messages: Sequence["DatabaseMessages"],
    *,
    is_group_chat: bool = True,
    max_profiles: int = 3,
) -> List[_PersonCandidate]:
    """从消息批次中收集可注入画像的候选人。"""
    limit = max(1, int(max_profiles or 1))
    candidates: List[_PersonCandidate] = []
    seen_person_ids: set[str] = set()

    def add(candidate: Optional[_PersonCandidate]) -> bool:
        if candidate is None or not candidate.person_id:
            return len(candidates) >= limit
        if candidate.person_id in seen_person_ids:
            return len(candidates) >= limit
        if _is_bot_user_id(candidate.user_id):
            return len(candidates) >= limit
        seen_person_ids.add(candidate.person_id)
        candidates.append(candidate)
        return len(candidates) >= limit

    for message in messages:
        if not message.user_info:
            continue
        platform = message.chat_info_platform or getattr(message.user_info, "platform", "") or ""
        user_id = str(getattr(message.user_info, "user_id", "") or "")
        person_name = (
            getattr(message.user_info, "user_cardname", None)
            or getattr(message.user_info, "user_nickname", "")
            or user_id
        )
        person_id = _resolve_person_id(
            platform=platform,
            user_id=user_id,
            person_name=person_name,
        )
        source = "group_speaker" if is_group_chat else "private_user"
        if add(_PersonCandidate(
            person_id=person_id,
            person_name=person_name,
            user_id=user_id,
            source=source,
        )):
            break

    return candidates[:limit]


def _truncate_profile_text(profile_text: str) -> str:
    normalized = profile_text.strip()
    if len(normalized) <= PROFILE_TEXT_MAX_CHARS:
        return normalized
    return normalized[:PROFILE_TEXT_MAX_CHARS].rstrip() + "..."


async def build_injection_text(
    messages: Sequence["DatabaseMessages"],
    *,
    chat_id: str = "",
    is_group_chat: bool = True,
    max_profiles: int = 3,
) -> str:
    """构造 Plannner 注入的人物画像参考文本。"""
    try:
        candidates = collect_person_candidates(
            messages,
            is_group_chat=is_group_chat,
            max_profiles=max_profiles,
        )
    except Exception as exc:
        logger.debug(f"收集人物画像候选失败: {exc}")
        return ""

    if not candidates:
        return ""

    from src.memory_system.memory_service import memory_service

    blocks: List[str] = []
    for candidate in candidates:
        try:
            payload = await memory_service.get_person_profile(
                person_id=candidate.person_id,
                chat_id=chat_id,
                limit=PROFILE_QUERY_LIMIT,
            )
        except Exception as exc:
            logger.debug(f"查询人物画像失败: person_id={candidate.person_id!r} err={exc}")
            continue

        if not isinstance(payload, dict) or not payload.get("success"):
            continue

        profile_text = str(payload.get("profile_text", "") or "").strip()
        if not profile_text:
            continue

        injection_text = build_profile_injection_text(profile_text)
        if not injection_text:
            continue

        display_name = (_clean_text(payload.get("person_name"))
                        or candidate.person_name
                        or candidate.person_id)
        blocks.append(
            f"- {display_name}（person_id: {candidate.person_id}，来源: {candidate.source}）\n"
            f"  {_truncate_profile_text(injection_text).replace(chr(10), chr(10) + '  ')}"
        )

    if not blocks:
        return ""

    return (
        "\n【人物画像-内部参考】\n"
        "以下内容仅供内部推理，不要向用户逐字复述。\n\n"
        + "\n".join(blocks)
        + "\n\n"
        "使用时把它当作对当前人物的背景理解；若与当前对话冲突，以当前对话为准。\n"
    )


async def inject_person_profiles(
    *,
    chat_id: str = "",
    chat_content_block: str = "",
    messages: Sequence["DatabaseMessages"],
    is_group_chat: bool = True,
) -> str:
    """将人物画像注入文本追加到 chat_content_block 尾部。"""
    config = global_config.chat
    if not getattr(config, "person_profile_injection_enabled", False):
        return chat_content_block

    try:
        max_profiles = int(getattr(config, "person_profile_injection_max_profiles", 3) or 3)
    except (TypeError, ValueError):
        max_profiles = 3

    try:
        profile_block = await build_injection_text(
            messages,
            chat_id=chat_id,
            is_group_chat=is_group_chat,
            max_profiles=max_profiles,
        )
    except Exception as exc:
        logger.debug(f"人物画像注入跳过: {exc}")
        return chat_content_block

    if not profile_block:
        return chat_content_block

    return f"{chat_content_block}\n{profile_block}"
