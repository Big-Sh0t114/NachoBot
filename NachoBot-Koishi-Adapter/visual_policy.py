"""Code-owned visual prompts for the Koishi adapter."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Protocol


KOISHI_VISUAL_PROFILE = "koishi-message-v1"
KOISHI_IMAGE_PROMPT = (
    "你正在理解经 Koishi/OneBot 从 Discord 等平台转发的用户图片。优先识别聊天或软件截图、代码与报错、"
    "人物或物体、动作和关键可见文字；只保留对当前对话有用的信息，看不清的内容不要猜。"
    "用中文输出一段不超过100字的纯文本，不分点。"
)


class ImageInferenceSettings(Protocol):
    temperature: float
    max_tokens: int
    extra_params: Mapping[str, Any]


def build_visual_policy(settings: ImageInferenceSettings) -> Dict[str, Any]:
    """Combine the code-owned prompt with configurable inference settings."""
    return {
        "version": 1,
        "profile": KOISHI_VISUAL_PROFILE,
        "image": {
            "prompt": KOISHI_IMAGE_PROMPT,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "extra_params": dict(settings.extra_params),
        },
    }
