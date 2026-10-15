"""Discord text and voice allow/deny decisions over logical Core IDs."""

from __future__ import annotations

from config import AdapterConfig


VOICE_PAYLOAD_FORMATS = ("wav",)


def voice_payload_formats() -> list[str]:
    """Declare accepted Core input formats for Discord's outbound audio paths."""
    return list(VOICE_PAYLOAD_FORMATS)


def _filtered(value: str, kind: str, entries: list[str]) -> bool:
    listed = str(value) in set(entries)
    if kind == "whitelist":
        return listed
    return not listed


def is_chat_allowed(config: AdapterConfig, user_id: str, group_id: str | None) -> bool:
    """Apply migrated text/DM filters and global user bans."""

    user = str(user_id)
    if user in set(config.chat.ban_user_id):
        return False
    if group_id is not None:
        return _filtered(
            str(group_id), config.chat.group_list_type, config.chat.group_list
        )
    return _filtered(
        user, config.chat.private_list_type, config.chat.private_list
    )


def is_voice_allowed(config: AdapterConfig, user_id: str, channel_id: str) -> bool:
    """Voice capture has independent channel policy plus global user bans."""

    if not config.voice.enabled or str(user_id) in set(config.chat.ban_user_id):
        return False
    return _filtered(
        str(channel_id),
        config.voice.allowed_channel_list_type,
        config.voice.allowed_channel_list,
    )


def accept_formats(config: AdapterConfig, *, voice_context: bool = False) -> list[str]:
    supported = ["text", "image", "emoji", "file", "video", "reply", "voicefile", "videofile"]
    # Text and slash replies can attach Core-generated speech only when text
    # TTS is enabled. Voice capture uses the current VC session for playback.
    speech_output_enabled = (
        bool(getattr(config.voice, "enabled", False))
        if voice_context
        else bool(getattr(config.voice, "use_tts", False))
    )
    if speech_output_enabled:
        supported.extend(("voice", "tts_text"))
    if voice_context and speech_output_enabled:
        supported.append("voice_stream")
    return supported
