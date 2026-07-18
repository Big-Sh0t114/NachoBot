import re
from typing import Any

from src.common.logger import get_logger
from src.config.config import global_config


logger = get_logger("prompt_variables")


def get_latest_session_name(chat_stream: Any) -> str:
    """Return a bounded current-session label suitable for inline prompts."""

    group_info = getattr(chat_stream, "group_info", None)
    raw_name = getattr(group_info, "group_name", "") or getattr(chat_stream, "stream_id", "")
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", str(raw_name))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:160] or "当前群聊"


def get_dynamic_prompt_variables() -> dict[str, Any]:
    return {
        "owner_name": getattr(global_config.bot, "owner_name", "") or "",
        "bot_name": getattr(global_config.bot, "nickname", "") or "",
    }


def render_dynamic_prompt_template(template: str) -> str:
    if not template or "{" not in template:
        return template

    try:
        return template.format(**get_dynamic_prompt_variables())
    except KeyError as exc:
        logger.warning(f"动态提示词变量缺失: {exc}; template={template[:120]}")
        return template
