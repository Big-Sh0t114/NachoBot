"""Track adapter-declared support and membership events."""

from typing import TYPE_CHECKING

from src.chat.runtime_capabilities import platform_event_from_message
from src.common.logger import get_logger
from ncnk_message import get_system_event

if TYPE_CHECKING:
    from src.chat.message_receive.message import MessageRecv


logger = get_logger("platform_event_tracker")


async def track_platform_event(message: "MessageRecv") -> None:
    """Apply a normalized platform event to the sender's person record."""

    try:
        message_info = getattr(message, "message_info", None)
        if message_info is None:
            return

        event = platform_event_from_message(message)
        if event is None:
            return

        user_info = getattr(message_info, "user_info", None)
        actor = None
        system_event = get_system_event(message)
        if system_event is not None:
            candidate_actor = system_event.get("actor")
            actor = candidate_actor if isinstance(candidate_actor, dict) else None

        platform = str(message_info.platform or "").strip()
        user_id_value = getattr(user_info, "user_id", None) if user_info is not None else None
        if not user_id_value and actor is not None:
            user_id_value = actor.get("user_id") or actor.get("name")
        user_id = str(user_id_value or "").strip()
        if not platform or not user_id:
            return
        user_name_value = getattr(user_info, "user_nickname", None) if user_info is not None else None
        if not user_name_value and actor is not None:
            user_name_value = actor.get("name")
        user_name = str(user_name_value or user_id)

        from src.person_info.person_info import Person

        person = Person(platform=platform, user_id=user_id)
        if not person.is_known:
            person = Person.register_person(
                platform=platform,
                user_id=user_id,
                nickname=user_name,
            )
        if not person or not person.is_known:
            return
        if event.amount > 0:
            person.update_gift_value(event.amount)
        if event.kind == "membership" and event.membership_days > 0:
            person.set_vip(duration_days=event.membership_days)
    except Exception as exc:
        logger.error(f"Failed to track platform support event: {exc}", exc_info=True)
