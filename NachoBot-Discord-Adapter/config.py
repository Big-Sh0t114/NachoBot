from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import tomlkit

from identity_map import IdentityMap
from config_validation import ConfigValidationError, validate_config_mapping


DEFAULT_VOICE_REPLYER_PROMPT = """{identity}
现在请你读读之前的聊天记录，然后给出日常且口语化的回复，平淡一些，
说话简短一些，单次回复控制在50字以内。请自然地回应用户，不要回复得太有条理。
{reply_style}
请只输出一段自然、口语化的中文回复，作为单一的语音文本字符串，控制在50字以内。使用规范标点断句，例如：“今天过得怎么样？我刚才也听了一首歌。”不要输出JSON、Markdown、额外分析、颜文字或表情符号，只写实际要说的话。

你正在Discord语音频道里聊天，下面是语音频道里的聊天内容:
{background_dialogue_prompt}
{core_dialogue_prompt}
{time_block}

{reply_target_block}。{keywords_reaction_prompt}
{knowledge_prompt}{tool_info_block}
{expression_habits_block}
{moderation_prompt}
"""

_LEGACY_STOCK_TTS_CONTRACT = (
    "请严格只输出一个 JSON 对象，不要 Markdown、代码围栏或额外解释，格式为：\n"
    '{"reply":"给用户看的显示文本","tts_text":"要播放的中文语音文本"}\n'
    "其中 reply 是给聊天记录显示的简短回复，tts_text 是明确交给 Core TTS 的中文文本；"
    "两者通常相同，也可以让显示文本和朗读文本略有不同。两个字段都必须是非空字符串。\n"
    'tts_text 将经过 Core TTS 作为语音播放，请使用规范的标点，使用逗号和句号断句，不要使用"~"等语气符，'
    "颜文字不可用来代替逗号或句号断句。"
)
_PLAIN_TTS_CONTRACT = (
    "请只输出一段自然、口语化的中文回复，作为单一的语音文本字符串，控制在50字以内。"
    "使用规范标点断句，例如：“今天过得怎么样？我刚才也听了一首歌。”"
    "不要输出JSON、Markdown、额外分析、颜文字或表情符号，只写实际要说的话。"
)


def normalize_legacy_voice_reply_prompt(prompt: str) -> str:
    """Normalize only the recognized stock JSON contract in memory."""
    if prompt.count(_LEGACY_STOCK_TTS_CONTRACT) != 1:
        return prompt
    return prompt.replace(_LEGACY_STOCK_TTS_CONTRACT, _PLAIN_TTS_CONTRACT, 1)


@dataclass
class DiscordConfig:
    token: str = ""
    app_id: str = ""
    proxy_enabled: bool = False
    proxy_url: str = ""


@dataclass
class NachoBotConfig:
    host: str = "localhost"
    port: int = 8000


@dataclass
class VoiceConfig:
    enabled: bool = True
    silence_threshold: float = 0.8
    vad_threshold: int = 500
    sample_rate: int = 48000
    use_tts: bool = True
    allowed_channel_list_type: str = "blacklist"
    allowed_channel_list: list[str] = field(default_factory=list)


@dataclass
class ChatConfig:
    group_list_type: str = "blacklist"
    group_list: list[str] = field(default_factory=list)
    private_list_type: str = "blacklist"
    private_list: list[str] = field(default_factory=list)
    ban_user_id: list[str] = field(default_factory=list)


@dataclass
class VisualImageConfig:
    temperature: float = 0.1
    max_tokens: int = 240
    extra_params: dict[str, Any] = field(default_factory=lambda: {"enable_thinking": False})


@dataclass
class PromptsConfig:
    planner_prompt: str = ""
    replyer_prompt: str = ""
    variables: dict[str, str] = field(default_factory=dict)


@dataclass
class AdapterConfig:
    discord: DiscordConfig
    nachobot: NachoBotConfig
    voice: VoiceConfig
    chat: ChatConfig
    visual_image: VisualImageConfig
    prompts: PromptsConfig
    identity_map: IdentityMap
    identity_required: bool
    media_proxy_url: str
    max_attachment_bytes: int
    log_level: str = "INFO"
    disable_network_search: bool = False


def _mapping(data: Any, role: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ValueError(f"Invalid {role} section in Discord adapter config")
    return dict(data)


def _string_list(value: Any, role: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Invalid {role} list in Discord adapter config")
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or item is None:
            raise ValueError(f"Invalid {role} list entry in Discord adapter config")
        text = str(item).strip()
        if not text or len(text) > 256 or any(ord(char) < 32 for char in text):
            raise ValueError(f"Invalid {role} list entry in Discord adapter config")
        result.append(text)
    return result


def _proxy(value: Any, role: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid {role} proxy URL in Discord adapter config")
    return text


def _resolve_nachobot_config(data: dict[str, Any]) -> NachoBotConfig:
    host = os.environ.get("NACHOBOT_CORE_HOST") or str(data.get("host", "localhost"))
    raw_port = os.environ.get("NACHOBOT_CORE_PORT") or data.get("port", 8000)
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Core port in Discord adapter config") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Invalid Core port in Discord adapter config")
    if not host or any(char in host for char in "\r\n/@"):
        raise ValueError("Invalid Core host in Discord adapter config")
    return NachoBotConfig(host=host.strip(), port=port)


def _resolve_prompts_from_core(nachobot_config_dir: Path) -> dict[str, str]:
    """Read Core personality variables without rewriting Core configuration."""
    bot_config_path = nachobot_config_dir / "bot_config.toml"
    if not bot_config_path.exists():
        return {}
    try:
        data = tomlkit.parse(bot_config_path.read_text(encoding="utf-8"))
        personality_data = data.get("personality", {})
        if not isinstance(personality_data, Mapping):
            return {}
        return {
            key: str(personality_data[key])
            for key in ("personality", "reply_style", "emotion_style", "interest")
            if personality_data.get(key)
        }
    except Exception:
        logging.getLogger("DiscordAdapter").warning(
            "Core prompt variables could not be loaded; local prompt variables remain active"
        )
        return {}


def load_config(path: Path) -> AdapterConfig:
    if not path.is_file():
        raise FileNotFoundError(f"Discord adapter config is missing: {path}")
    try:
        data = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError) as exc:
        raise ValueError("Discord adapter config is unreadable or malformed TOML") from exc
    try:
        validate_config_mapping(data, require_token=True)
    except ConfigValidationError as exc:
        raise ValueError(str(exc)) from exc

    root_dir = path.parent.parent
    identity_data = _mapping(data.get("identity", {}), "identity")
    identity_required = identity_data.get("required", False)
    identity_path = path.parent / "data" / "identity_map.json"
    if identity_required and not identity_path.is_file():
        raise ValueError("Required Discord identity map is missing; rerun the one-time migration")
    identity_map = IdentityMap.load(identity_path)

    discord_data = _mapping(data.get("discord", {}), "discord")
    token = str(discord_data.get("token", "") or "").strip()
    proxy_enabled = discord_data.get("proxy_enabled", False)
    discord_config = DiscordConfig(
        token=token,
        app_id=str(discord_data.get("app_id", "") or "").strip(),
        proxy_enabled=proxy_enabled,
        proxy_url=_proxy(discord_data.get("proxy_url", ""), "Discord"),
    )

    nachobot_data = _mapping(data.get("nachobot", {}), "Core")
    core_config = _resolve_nachobot_config(nachobot_data)
    voice_data = _mapping(data.get("voice", {}), "voice")
    voice_config = VoiceConfig(
        enabled=bool(voice_data.get("enabled", True)),
        silence_threshold=float(voice_data.get("silence_threshold", 0.8)),
        vad_threshold=int(voice_data.get("vad_threshold", 500)),
        sample_rate=int(voice_data.get("sample_rate", 48000)),
        use_tts=bool(voice_data.get("use_tts", True)),
        allowed_channel_list_type=str(
            voice_data.get("allowed_channel_list_type", "blacklist")
        ).lower(),
        allowed_channel_list=_string_list(
            voice_data.get("allowed_channel_list", []), "voice channel"
        ),
    )

    chat_data = _mapping(data.get("chat", {}), "chat")
    chat_config = ChatConfig(
        group_list_type=str(chat_data.get("group_list_type", "blacklist")).lower(),
        group_list=_string_list(chat_data.get("group_list", []), "group"),
        private_list_type=str(chat_data.get("private_list_type", "blacklist")).lower(),
        private_list=_string_list(chat_data.get("private_list", []), "private user"),
        ban_user_id=_string_list(chat_data.get("ban_user_id", []), "banned user"),
    )

    visual_data = _mapping(data.get("visual", {}), "visual")
    visual_image_data = _mapping(visual_data.get("image", {}), "visual image")
    extra_params = _mapping(visual_image_data.get("extra_params", {}), "visual extra_params")
    visual_image = VisualImageConfig(
        temperature=float(visual_image_data.get("temperature", 0.1)),
        max_tokens=max(1, int(visual_image_data.get("max_tokens", 240))),
        extra_params=dict(extra_params),
    )

    prompts_data = _mapping(data.get("prompts", {}), "prompts")
    local_variables = _mapping(prompts_data.get("variables", {}), "prompt variables")
    core_variables = _resolve_prompts_from_core(root_dir / "NachoBot" / "config")
    merged_variables = {**core_variables, **{key: str(value) for key, value in local_variables.items()}}
    prompts = PromptsConfig(
        planner_prompt=str(prompts_data.get("planner_prompt", "") or ""),
        replyer_prompt=normalize_legacy_voice_reply_prompt(
            str(prompts_data.get("replyer_prompt", "") or "")
        ),
        variables=merged_variables,
    )

    network_data = _mapping(data.get("network", {}), "network")
    media_data = _mapping(data.get("media", {}), "media")
    max_attachment_bytes = int(media_data.get("max_attachment_bytes", 20 * 1024 * 1024))
    if not 1_024 <= max_attachment_bytes <= 25 * 1024 * 1024:
        raise ValueError("Attachment size limit must be between 1 KiB and 25 MiB")

    return AdapterConfig(
        discord=discord_config,
        nachobot=core_config,
        voice=voice_config,
        chat=chat_config,
        visual_image=visual_image,
        prompts=prompts,
        identity_map=identity_map,
        identity_required=identity_required,
        media_proxy_url=(
            discord_config.proxy_url
            if discord_config.proxy_enabled and discord_config.proxy_url
            else _proxy(network_data.get("proxy", ""), "media")
        ),
        max_attachment_bytes=max_attachment_bytes,
        log_level=str(data.get("log_level", "INFO")).upper(),
        disable_network_search=bool(data.get("disable_network_search", False)),
    )
