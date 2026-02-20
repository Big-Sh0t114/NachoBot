import json
import logging
import re
from typing import Any, Dict, Optional
from config import AdapterConfig

# RegEx for Bilibili URL detection
BILIBILI_URL_RE = re.compile(
    r"https?://(?:www\.)?bilibili\.com/video/(?P<bv>BV[\w]+|av\d+)",
    re.IGNORECASE,
)
B23_SHORT_RE = re.compile(r"https?://b23\.tv/[\w]+", re.IGNORECASE)


def maybe_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def ws_is_closed(ws: Any) -> bool:
    if ws is None:
        return True
    closed_attr = getattr(ws, "closed", None)
    if closed_attr is not None:
        return bool(closed_attr)
    try:
        from websockets.protocol import State
    except Exception:
        return False
    state = getattr(ws, "state", None)
    return state in (State.CLOSING, State.CLOSED)


def is_allowed(config: AdapterConfig, user_id: str, group_id: Optional[str]) -> bool:
    if user_id in config.ban_user_id:
        return False
    if group_id:
        if config.group_list_type == "whitelist" and group_id not in config.group_list:
            return False
        if config.group_list_type == "blacklist" and group_id in config.group_list:
            return False
    else:
        if (
            config.private_list_type == "whitelist"
            and user_id not in config.private_list
        ):
            return False
        if config.private_list_type == "blacklist" and user_id in config.private_list:
            return False
    return True


def allow_reply(config: AdapterConfig) -> bool:
    return str(config.platform).lower() != "discord"


def extract_group_name(data: Dict[str, Any], group_id: str) -> str:
    for key in ("group_name", "channel_name", "guild_name"):
        value = data.get(key)
        if value:
            return str(value)
    return str(group_id) if group_id else ""


def mask_bilibili_raw_data(
    data: Dict[str, Any], logger: logging.Logger
) -> Dict[str, Any]:
    """Deep copy and mask Bilibili URLs in raw data to prevent plugin triggering on Discord."""
    try:
        cloned = json.loads(json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.warning("Failed to clone raw data for masking, using original")
        return data

    def _recursive_mask(obj: Any) -> Any:
        if isinstance(obj, dict):
            for k, v in obj.items():
                obj[k] = _recursive_mask(v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                obj[i] = _recursive_mask(v)
        elif isinstance(obj, str):
            if BILIBILI_URL_RE.search(obj) or B23_SHORT_RE.search(obj):
                s = BILIBILI_URL_RE.sub("[Bilibili Link]", obj)
                s = B23_SHORT_RE.sub("[Bilibili Link]", s)
                return s
        return obj

    return _recursive_mask(cloned)
