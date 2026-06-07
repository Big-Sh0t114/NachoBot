from typing import Any

from src.common.logger import get_logger
from src.config.config import global_config


logger = get_logger("prompt_variables")


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
