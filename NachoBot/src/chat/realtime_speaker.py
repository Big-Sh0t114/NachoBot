"""Provider-neutral, display-only speaker metadata for verified realtime voice."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

REALTIME_SPEAKER_KEY = "realtime_speaker"
CORE_REALTIME_SPEAKER_KEY = "_core_realtime_speaker"
_SPEAKER_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_LABEL_LENGTH = 64


@dataclass(frozen=True, slots=True)
class RealtimeSpeakerContext:
    """A short-lived speaker cluster label; never a platform identity."""

    version: int
    speaker_ref: str
    label: str

    def to_mapping(self) -> dict[str, Any]:
        return {"version": self.version, "speaker_ref": self.speaker_ref, "label": self.label}


def parse_realtime_speaker(value: Any) -> RealtimeSpeakerContext | None:
    """Parse the strict adapter declaration used by a verified voice turn."""

    if not isinstance(value, Mapping) or set(value) != {"version", "speaker_ref", "label"}:
        return None
    version = value.get("version")
    speaker_ref = value.get("speaker_ref")
    label = value.get("label")
    if (
        type(version) is not int
        or version != 1
        or not isinstance(speaker_ref, str)
        or not _SPEAKER_REF.fullmatch(speaker_ref)
        or not isinstance(label, str)
        or not label
        or label != label.strip()
        or len(label) > _MAX_LABEL_LENGTH
        or not any(char.isalnum() for char in label)
        or any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in label)
    ):
        return None
    return RealtimeSpeakerContext(version, speaker_ref, label)


def anonymous_voice_candidate(message: Any) -> RealtimeSpeakerContext | None:
    """Return a bounded anonymous speaker only for senderless group voice rows."""

    try:
        info = _field(message, "message_info")
        user = _field(message, "user_info") or _field(info, "user_info")
        group = _field(message, "group_info") or _field(info, "group_info")
        group_id = _field(group, "group_id") if group is not None else None
        segment = _field(message, "message_segment")
        segment_type = _field(segment, "type")
        if isinstance(segment, Mapping):
            segment_type = segment.get("type")
        is_voice = _field(message, "is_voice") is True or segment_type == "voice"
        if user is not None or not group_id or not is_voice:
            return None

        additional = _additional_config_mapping(message)
        # Structured platform/system events keep their established senderless
        # semantics and are not reinterpreted as voice speakers.
        if "system_event" in additional:
            return None
        from src.chat.runtime_capabilities import runtime_capabilities_from_message

        capabilities = runtime_capabilities_from_message(message)
        if not (
            capabilities.planner_bypass
            and capabilities.planner_bypass_declared
            and capabilities.reply_delivery == "tts_text"
            and capabilities.voice_stream
        ):
            return None
        return parse_realtime_speaker(additional.get(REALTIME_SPEAKER_KEY))
    except Exception:
        return None


def _config_mapping(message: Any) -> dict[str, Any]:
    value = _additional_config_mapping(message)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def _additional_config_mapping(message: Any) -> Mapping[str, Any]:
    """Read message metadata without importing Core routing modules."""

    if isinstance(message, Mapping):
        direct = _as_mapping(message.get("additional_config"))
        if direct:
            return direct
        additional_data = message.get("additional_data")
        if isinstance(additional_data, Mapping) and additional_data:
            return additional_data
        for key in ("message_base_info", "message_info"):
            info = message.get(key)
            if isinstance(info, Mapping):
                nested = _as_mapping(info.get("additional_config"))
                if nested is not None:
                    return nested
        return {}

    direct = _as_mapping(getattr(message, "additional_config", None))
    if direct:
        return direct
    additional_data = getattr(message, "additional_data", None)
    if isinstance(additional_data, Mapping) and additional_data:
        return additional_data
    for key in ("message_base_info", "message_info"):
        info = getattr(message, key, None)
        if info is not None:
            nested = _as_mapping(_field(info, "additional_config"))
            if nested is not None:
                return nested
    return {}


def _write_config_mapping(message: Any, value: dict[str, Any]) -> bool:
    """Write normalized config back to the same supported message envelope."""

    if isinstance(message, MutableMapping):
        if "additional_config" in message:
            message["additional_config"] = value
            return True
        if "additional_data" in message:
            message["additional_data"] = value
            return True
        for key in ("message_base_info", "message_info"):
            info = message.get(key)
            if isinstance(info, MutableMapping):
                info["additional_config"] = value
                return True
        message["additional_config"] = value
        return True

    for attribute in ("additional_config", "additional_data"):
        try:
            current = getattr(message, attribute)
        except Exception:
            continue
        if current is not None:
            try:
                setattr(message, attribute, value)
                return True
            except Exception:
                pass
    for attribute in ("message_base_info", "message_info"):
        info = getattr(message, attribute, None)
        if info is not None:
            try:
                setattr(info, "additional_config", value)
                return True
            except Exception:
                continue
    return False


def strip_core_speaker_claim(message: Any) -> None:
    """Remove adapter-supplied Core provenance before lifecycle/provider checks."""

    try:
        delattr(message, "_core_realtime_speaker")
    except Exception:
        pass

    def clean(container: Any, key: str) -> None:
        if isinstance(container, MutableMapping):
            raw = container.get(key)
        else:
            try:
                raw = getattr(container, key)
            except Exception:
                return
        if isinstance(raw, Mapping):
            updated = dict(raw)
        elif isinstance(raw, str) and raw.strip():
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            if not isinstance(decoded, Mapping):
                return
            updated = dict(decoded)
        else:
            return
        if CORE_REALTIME_SPEAKER_KEY not in updated:
            return
        updated.pop(CORE_REALTIME_SPEAKER_KEY, None)
        if isinstance(container, MutableMapping):
            container[key] = updated
        else:
            try:
                setattr(container, key, updated)
            except Exception:
                pass

    if isinstance(message, MutableMapping):
        clean(message, "additional_config")
        clean(message, "additional_data")
        for key in ("message_base_info", "message_info"):
            info = message.get(key)
            if info is not None:
                clean(info, "additional_config")
    else:
        clean(message, "additional_config")
        clean(message, "additional_data")
        for key in ("message_base_info", "message_info"):
            info = getattr(message, key, None)
            if info is not None:
                clean(info, "additional_config")


def attach_verified_realtime_speaker(message: Any, speaker: RealtimeSpeakerContext) -> bool:
    """Persist a normalized Core-owned display marker after provider proof."""

    additional = _config_mapping(message)
    raw_speaker = parse_realtime_speaker(additional.get(REALTIME_SPEAKER_KEY))
    if raw_speaker != speaker:
        return False
    additional[REALTIME_SPEAKER_KEY] = speaker.to_mapping()
    additional[CORE_REALTIME_SPEAKER_KEY] = speaker.to_mapping()
    if not _write_config_mapping(message, additional):
        return False
    try:
        setattr(message, "_core_realtime_speaker", speaker)
    except Exception:
        pass
    return True


def stored_realtime_speaker(message: Any) -> RealtimeSpeakerContext | None:
    """Read persisted display provenance from a database model, never a raw map."""

    try:
        from src.common.data_models.database_data_model import DatabaseMessages

        if not isinstance(message, DatabaseMessages) or getattr(message, "user_info", None) is not None:
            return None
        additional = _additional_config_mapping(message)
        raw = additional.get(CORE_REALTIME_SPEAKER_KEY)
        stored = parse_realtime_speaker(raw)
        declared = parse_realtime_speaker(additional.get(REALTIME_SPEAKER_KEY))
        return stored if stored is not None and stored == declared else None
    except Exception:
        return None


def stored_speaker_marker_matches(message: Any, expected: RealtimeSpeakerContext) -> bool:
    """Require raw and Core-persisted metadata to match an ingress ledger entry."""

    return stored_realtime_speaker(message) == expected


def has_realtime_speaker_claim(message: Any) -> bool:
    """Identify reserved speaker metadata so it cannot use ordinary bypass rules."""

    additional = _additional_config_mapping(message)
    return REALTIME_SPEAKER_KEY in additional or CORE_REALTIME_SPEAKER_KEY in additional


def strip_speaker_fields_from_additional_config(value: Any) -> dict[str, Any]:
    """Utility for tests and trusted callers that need a safe config copy."""

    if isinstance(value, Mapping):
        result = dict(value)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        result = dict(parsed) if isinstance(parsed, Mapping) else {}
    else:
        result = {}
    result.pop(CORE_REALTIME_SPEAKER_KEY, None)
    return result
