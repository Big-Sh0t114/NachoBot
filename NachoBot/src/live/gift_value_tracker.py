"""Track monetary support and supporter status from live-platform events."""

import json
from typing import TYPE_CHECKING, Any

from src.common.logger import get_logger

if TYPE_CHECKING:
    from src.chat.message_receive.message import MessageRecv


logger = get_logger("gift_value_tracker")


def _decode_raw_message(raw_message: Any) -> dict[str, Any] | None:
    if not raw_message:
        return None
    if isinstance(raw_message, str):
        try:
            raw_message = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            return None
    return raw_message if isinstance(raw_message, dict) else None


def _get_platform_variants(platform: str, user_id: str) -> set[str]:
    """Return existing platform aliases that represent the same live user."""
    platforms = {platform}
    if not platform.startswith("bilibili"):
        return platforms

    from src.common.database.database_model import PersonInfo

    try:
        records = PersonInfo.select().where(
            PersonInfo.platform.startswith("bilibili"),
            PersonInfo.user_id == str(user_id),
        )
        for record in records:
            if record.platform:
                platforms.add(record.platform)
    except Exception as exc:
        logger.debug(f"Failed to look up Bilibili platform aliases: {exc}")
    return platforms


async def track_gift_value(message: "MessageRecv") -> None:
    """Record SuperChat value and guard VIP status without blocking HeartFlow."""
    try:
        raw = _decode_raw_message(message.raw_message)
        if raw is None:
            return

        message_info = getattr(message, "message_info", None)
        user_info = getattr(message_info, "user_info", None)
        if message_info is None or user_info is None:
            return

        event_type = str(raw.get("type", ""))
        if event_type not in {"superchat", "guard"}:
            return

        platform = str(message_info.platform or "bilibili.live")
        user_id = str(user_info.user_id)
        user_name = str(user_info.user_nickname or user_id)
        price = float(raw.get("price", 0))

        from src.person_info.person_info import Person

        for platform_variant in _get_platform_variants(platform, user_id):
            person = Person(platform=platform_variant, user_id=user_id)
            if not person.is_known:
                person = Person.register_person(
                    platform=platform_variant,
                    user_id=user_id,
                    nickname=user_name,
                )
            if not person or not person.is_known:
                continue
            if price > 0:
                person.update_gift_value(price)
            if event_type == "guard":
                person.set_vip(duration_days=30)
    except Exception as exc:
        logger.error(f"Failed to track live support/VIP status: {exc}", exc_info=True)
