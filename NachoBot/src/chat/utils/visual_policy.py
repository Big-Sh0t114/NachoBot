"""Resolve code-owned visual prompts and per-call inference settings.

Platform adapters attach a ``visual_policy`` mapping to
``BaseMessageInfo.additional_config``. Core executes the registered model.
Most prompts live in adapter code; QQ is intentionally coupled to Core and
selects Core-owned prompts through a fixed profile marker.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

from dataclasses import dataclass
from typing import Any, Mapping, Optional


VISUAL_POLICY_KEY = "visual_policy"
VISUAL_POLICY_VERSION = 1
_PROFILE_SANITIZER = re.compile(r"[^a-zA-Z0-9_.-]+")
QQ_CORE_VISUAL_PROFILE = "qq-core-v1"

CORE_GENERIC_IMAGE_PROMPT = (
    "请用中文描述这张图片的内容。如果有文字，请把文字描述概括出来，请留意其主题，直观感受，"
    "输出为一段平文本，最多30字，请注意不要分点，就输出一段文本"
)
CORE_GENERIC_EMOJI_PROMPT = (
    "这是一个表情包，请详细描述它表达的情感、画面内容和文字，并从互联网梗、meme 的角度分析。"
)
CORE_GENERIC_GIF_EMOJI_PROMPT = (
    "这是一个动态图表情包的关键帧拼图，每一格代表一帧，黑色背景代表透明。"
    "请详细描述动作变化、画面内容、文字和表达的情感，并从互联网梗、meme 的角度分析。"
)
CORE_GENERIC_VIDEO_PROMPT = (
    "这是用户发送的一段视频，请仔细观看并用中文清晰描述主要场景、人物或物体、"
    "关键动作、可见文字和视频想表达的意思。直接输出一段纯文本。"
)

QQ_CORE_VISUAL_PROMPTS: Mapping[str, Mapping[str, str]] = {
    "image": {
        "prompt": (
            "你正在理解 QQ 私聊或群聊中用户发送的普通图片。用中文概括主体、场景、动作和与对话有关的可见文字；"
            "聊天截图要说明对话关系，梗图要点出梗意。看不清的内容不要猜。只输出一段不超过80字的纯文本，不分点。"
        )
    },
    "emoji": {
        "prompt": (
            "这是 QQ 聊天中的静态表情包。请识别人物或物体、表情、姿态、文字和梗点，"
            "重点说明它表达的情绪与适用语境。从中文互联网 meme 的角度分析，直接输出简洁描述。"
        ),
        "gif_prompt": (
            "这是 QQ 动态表情包的关键帧拼图，每格是一帧，黑色背景代表透明。"
            "请按顺序识别动作变化、文字、梗点和最终情绪，从中文互联网 meme 的角度输出简洁描述。"
        ),
    },
    "video": {
        "prompt": (
            "这是 QQ 用户在聊天中发送的视频。请用中文概括主要场景、人物或物体、关键动作、"
            "可见文字及视频想表达的意思。优先提供对当前聊天有用的信息，只输出一段不超过120字的纯文本。"
        )
    },
}


@dataclass(frozen=True)
class VisualTaskPolicy:
    """A normalized visual task policy used by Core for one request."""

    task: str
    prompt: str
    gif_prompt: Optional[str]
    temperature: float
    max_tokens: int
    extra_params: Mapping[str, Any]
    profile: str
    adapter_owned: bool
    cache_type: str


def _coerce_temperature(value: Any, default: float) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return default
    return converted if math.isfinite(converted) else default


def _coerce_max_tokens(value: Any, default: int) -> int:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return default
    return converted if converted > 0 else default


def _clean_profile(value: Any) -> str:
    profile = _PROFILE_SANITIZER.sub("-", str(value or "adapter")).strip("-._")
    return (profile or "adapter")[:80]


def _adapter_policy_root(additional_config: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(additional_config, Mapping):
        return None
    raw = additional_config.get(VISUAL_POLICY_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        version = int(raw.get("version", VISUAL_POLICY_VERSION))
    except (TypeError, ValueError):
        return None
    if version != VISUAL_POLICY_VERSION:
        return None
    return raw


def resolve_visual_task_policy(
    additional_config: Any,
    task: str,
    *,
    default_prompt: str,
    default_gif_prompt: Optional[str] = None,
    default_temperature: float = 0.4,
    default_max_tokens: int = 300,
) -> VisualTaskPolicy:
    """Resolve one task, falling back only for legacy/invalid adapter data.

    The cache type contains a deterministic fingerprint of every setting that
    can affect the result. Editing a code-owned prompt therefore invalidates
    only the affected visual cache entries.
    """

    root = _adapter_policy_root(additional_config)
    task_config = root.get(task) if root is not None else None
    adapter_owned = isinstance(task_config, Mapping)
    if not adapter_owned:
        return VisualTaskPolicy(
            task=task,
            prompt=default_prompt,
            gif_prompt=default_gif_prompt,
            temperature=default_temperature,
            max_tokens=default_max_tokens,
            extra_params={},
            profile="legacy-core",
            adapter_owned=False,
            cache_type=task,
        )

    assert isinstance(task_config, Mapping)
    profile = _clean_profile(root.get("profile"))
    core_prompt = (
        QQ_CORE_VISUAL_PROMPTS.get(task)
        if profile == QQ_CORE_VISUAL_PROFILE
        else None
    )
    if core_prompt is not None:
        prompt = core_prompt["prompt"]
        gif_prompt = core_prompt.get("gif_prompt", default_gif_prompt)
    else:
        raw_prompt = task_config.get("prompt")
        prompt = (
            str(raw_prompt).strip()
            if isinstance(raw_prompt, str) and raw_prompt.strip()
            else default_prompt
        )
        raw_gif_prompt = task_config.get("gif_prompt")
        gif_prompt = (
            str(raw_gif_prompt).strip()
            if isinstance(raw_gif_prompt, str) and raw_gif_prompt.strip()
            else default_gif_prompt
        )
    temperature = _coerce_temperature(
        task_config.get("temperature"),
        default_temperature,
    )
    max_tokens = _coerce_max_tokens(
        task_config.get("max_tokens"),
        default_max_tokens,
    )
    raw_extra_params = task_config.get("extra_params")
    extra_params = dict(raw_extra_params) if isinstance(raw_extra_params, Mapping) else {}
    fingerprint_payload = {
        "prompt": prompt,
        "gif_prompt": gif_prompt,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "extra_params": extra_params,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:12]

    return VisualTaskPolicy(
        task=task,
        prompt=prompt,
        gif_prompt=gif_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_params=extra_params,
        profile=profile,
        adapter_owned=True,
        cache_type=f"{task}:{profile}:{fingerprint}",
    )


def scoped_media_hash(raw_hash: str, policy: VisualTaskPolicy) -> str:
    """Return a profile-specific storage hash for ordinary images."""

    if not policy.adapter_owned:
        return raw_hash
    return hashlib.md5(f"{raw_hash}\0{policy.cache_type}".encode("utf-8"), usedforsecurity=False).hexdigest()
