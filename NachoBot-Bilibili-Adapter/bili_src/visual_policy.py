"""Code-owned visual prompts for Bilibili private messages and live screen."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Protocol


BILIBILI_PRIVATE_VISUAL_PROFILE = "bilibili-private-v1"
BILIBILI_PRIVATE_IMAGE_PROMPT = (
    "你正在理解 Bilibili 私信中用户发送的图片。重点识别聊天或动态截图、视频封面、二创梗图、"
    "UP主或直播相关信息、人物动作和关键可见文字；只描述可确认且对私信对话有用的内容。"
    "用中文输出一段不超过90字的纯文本，不分点。"
)
BILIBILI_SCREEN_SYSTEM_PROMPT = (
    "你负责为直播回复提供实时视觉上下文。只报告截图中可确认的信息，"
    "不要猜测看不清的文字或画面。"
)
BILIBILI_SCREEN_PROMPT = """分析当前直播活动窗口截图。
优先提取当前应用或游戏状态、关键人物/物体/动作、重要可见文字，以及与当前弹幕直接相关的画面变化；忽略无关的固定界面。
窗口标题：{window_title}
窗口进程：{window_executable}
当前弹幕：{message_text}
只输出一段60到120字的中文纯文本，不要使用Markdown，不要复述要求。"""


class ImageInferenceSettings(Protocol):
    temperature: float
    max_tokens: int
    extra_params: Mapping[str, Any]


def build_private_visual_policy(settings: ImageInferenceSettings) -> Dict[str, Any]:
    """Combine the private-message prompt with configurable model settings."""
    return {
        "version": 1,
        "profile": BILIBILI_PRIVATE_VISUAL_PROFILE,
        "image": {
            "prompt": BILIBILI_PRIVATE_IMAGE_PROMPT,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "extra_params": dict(settings.extra_params),
        },
    }
