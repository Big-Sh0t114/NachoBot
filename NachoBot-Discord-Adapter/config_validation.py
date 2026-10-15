"""Pure validation shared by migration and runtime config loading."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


class ConfigValidationError(ValueError):
    pass


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigValidationError(f"Invalid {key} table in Discord adapter config")
    return value


def _string(
    value: Any,
    role: str,
    *,
    allow_empty: bool = True,
    allow_multiline: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ConfigValidationError(f"Invalid {role} in Discord adapter config")
    invalid_control = any(
        ord(char) < 32 and not (allow_multiline and char in "\r\n\t")
        for char in value
    )
    if (not allow_empty and not value.strip()) or invalid_control:
        raise ConfigValidationError(f"Invalid {role} in Discord adapter config")
    return value


def _string_list(value: Any, role: str) -> None:
    if not isinstance(value, list):
        raise ConfigValidationError(f"Invalid {role} list in Discord adapter config")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ConfigValidationError(f"Invalid {role} list entry in Discord adapter config")
        text = str(item).strip()
        if not text or len(text) > 256 or any(ord(char) < 32 for char in text):
            raise ConfigValidationError(f"Invalid {role} list entry in Discord adapter config")


def _filter(table: Mapping[str, Any], prefix: str, default_type: str) -> None:
    kind = table.get(f"{prefix}_list_type", default_type)
    values = table.get(f"{prefix}_list", [])
    if not isinstance(kind, str) or kind.lower() not in {"whitelist", "blacklist"}:
        raise ConfigValidationError(f"Invalid {prefix} filter type in Discord adapter config")
    _string_list(values, prefix)


def _proxy(value: Any, role: str) -> None:
    text = _string(value, f"{role} proxy URL")
    if not text:
        return
    try:
        parsed = urlsplit(text)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigValidationError(f"Invalid {role} proxy URL in Discord adapter config")


def validate_config_mapping(data: Mapping[str, Any], *, require_token: bool = False) -> None:
    version = data.get("config_version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in {2, 3}:
        raise ConfigValidationError("Discord adapter config requires schema version 2 or 3")

    identity = _table(data, "identity")
    if not isinstance(identity.get("required", False), bool):
        raise ConfigValidationError("Invalid identity.required setting in Discord adapter config")

    discord = _table(data, "discord")
    token = _string(discord.get("token", ""), "Discord bot token")
    if require_token and (not token.strip() or token.strip().lower() in {"your_discord_bot_token", "your_app_id"}):
        raise ConfigValidationError("Discord bot token is not configured")
    _string(discord.get("app_id", ""), "Discord application ID")
    if not isinstance(discord.get("proxy_enabled", False), bool):
        raise ConfigValidationError("Invalid Discord proxy_enabled setting")
    _proxy(discord.get("proxy_url", ""), "Discord")
    if discord.get("proxy_enabled", False) and not discord.get("proxy_url", ""):
        raise ConfigValidationError("Discord proxy is enabled without a proxy URL")

    core = _table(data, "nachobot")
    host = _string(core.get("host", "localhost"), "Core host", allow_empty=False).strip()
    if not host or any(char in host for char in "\r\n/@"):
        raise ConfigValidationError("Invalid Core host in Discord adapter config")
    port = core.get("port", 8000)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigValidationError("Invalid Core port in Discord adapter config")

    voice = _table(data, "voice")
    if not isinstance(voice.get("enabled", True), bool) or not isinstance(voice.get("use_tts", True), bool):
        raise ConfigValidationError("Invalid voice enablement settings in Discord adapter config")
    sample_rate = voice.get("sample_rate", 48_000)
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate != 48_000:
        raise ConfigValidationError("Discord voice sample_rate must be 48000 Hz")
    for name, lower, upper in (("vad_threshold", 0, 32_767),):
        value = voice.get(name, 500)
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ConfigValidationError(f"Invalid voice {name} in Discord adapter config")
    silence = voice.get("silence_threshold", 0.8)
    if isinstance(silence, bool) or not isinstance(silence, (int, float)) or not math.isfinite(float(silence)) or not 0 <= float(silence) <= 10:
        raise ConfigValidationError("Invalid voice silence threshold in Discord adapter config")
    _filter(voice, "allowed_channel", "blacklist")

    chat = _table(data, "chat")
    _filter(chat, "group", "blacklist")
    _filter(chat, "private", "blacklist")
    _string_list(chat.get("ban_user_id", []), "banned user")

    visual = _table(data, "visual")
    image = _table(visual, "image")
    temperature = image.get("temperature", 0.1)
    tokens = image.get("max_tokens", 240)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2:
        raise ConfigValidationError("Invalid visual temperature in Discord adapter config")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= 32_000:
        raise ConfigValidationError("Invalid visual max_tokens in Discord adapter config")
    if not isinstance(image.get("extra_params", {}), Mapping):
        raise ConfigValidationError("Invalid visual extra_params in Discord adapter config")

    network = _table(data, "network")
    _proxy(network.get("proxy", ""), "media")
    media = _table(data, "media")
    max_bytes = media.get("max_attachment_bytes", 20 * 1024 * 1024)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1_024 <= max_bytes <= 25 * 1024 * 1024:
        raise ConfigValidationError("Invalid attachment size limit in Discord adapter config")

    prompts = _table(data, "prompts")
    _string(prompts.get("planner_prompt", ""), "planner prompt", allow_multiline=True)
    _string(prompts.get("replyer_prompt", ""), "replyer prompt", allow_multiline=True)
    variables = _table(prompts, "variables")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in variables.items()):
        raise ConfigValidationError("Invalid prompt variables in Discord adapter config")
    level = data.get("log_level", "INFO")
    if not isinstance(level, str) or level.upper() not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigValidationError("Invalid log level in Discord adapter config")
    if not isinstance(data.get("disable_network_search", False), bool):
        raise ConfigValidationError("Invalid disable_network_search setting in Discord adapter config")
