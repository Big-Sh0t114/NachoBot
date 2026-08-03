from typing import Any, Optional


def resolve_sender_name(
    *,
    user_info: Any = None,
    user_cardname: Optional[str] = None,
    user_nickname: Optional[str] = None,
    person_name: Optional[str] = None,
    user_id: Optional[str] = None,
    fallback: str = "某人",
) -> str:
    """解析 prompt 中的当前显示名，稳定人物名仅作为资料缺失时的降级值。"""
    if user_info is not None:
        if user_cardname is None:
            user_cardname = getattr(user_info, "user_cardname", None)
        if user_nickname is None:
            user_nickname = getattr(user_info, "user_nickname", None)
        if person_name is None:
            person_name = getattr(user_info, "person_name", None)
        if user_id is None:
            user_id = getattr(user_info, "user_id", None)

    for value in (user_cardname, user_nickname, person_name, user_id):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return fallback
