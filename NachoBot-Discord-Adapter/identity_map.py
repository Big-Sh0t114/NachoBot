"""Private, one-time identity import for Discord transport IDs.

Runtime code reads this exported file only.  It has no Koishi/database
dependency and new Discord identities fall back to their native Snowflake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


IDENTITY_SCHEMA_VERSION = 1


def _snowflake(value: Any, *, label: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid {label} in Discord identity map")
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or not 0 < int(text) < 2**64:
        raise ValueError(f"invalid {label} in Discord identity map")
    return text


def _logical_id(value: Any, *, label: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid {label} in Discord identity map")
    text = str(value).strip()
    if not text or len(text) > 256 or any(ord(char) < 32 for char in text):
        raise ValueError(f"invalid {label} in Discord identity map")
    return text


def _validated_bijection(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid {label} mapping in Discord identity map")
    result: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for native, logical in value.items():
        native_id = _snowflake(native, label=f"{label} native ID")
        logical_id = _logical_id(logical, label=f"{label} logical ID")
        if native_id in result and result[native_id] != logical_id:
            raise ValueError(f"conflicting {label} native ID in Discord identity map")
        if logical_id in reverse and reverse[logical_id] != native_id:
            raise ValueError(f"non-bijective {label} mapping in Discord identity map")
        result[native_id] = logical_id
        reverse[logical_id] = native_id
    return result


@dataclass(frozen=True)
class IdentityMap:
    """The frozen legacy-ID bridge used by text, slash, and voice transports."""

    user_native_to_logical: dict[str, str] = field(default_factory=dict)
    channel_native_to_logical: dict[str, str] = field(default_factory=dict)
    private_channels: tuple[dict[str, str], ...] = ()
    bot_self_ids: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "IdentityMap":
        if payload.get("schema_version") != IDENTITY_SCHEMA_VERSION:
            raise ValueError("unsupported Discord identity map version")
        users = _validated_bijection(
            payload.get("user_native_to_logical", {}), label="user"
        )
        channels = _validated_bijection(
            payload.get("channel_native_to_logical", {}), label="channel"
        )

        raw_private = payload.get("private_channels", [])
        if not isinstance(raw_private, list):
            raise ValueError("invalid private channel mappings in Discord identity map")
        private_channels: list[dict[str, str]] = []
        for item in raw_private:
            if not isinstance(item, Mapping):
                raise ValueError("invalid private channel mapping in Discord identity map")
            user_id = _snowflake(item.get("user_id"), label="private user ID")
            channel_id = _snowflake(item.get("channel_id"), label="private channel ID")
            bot_id = item.get("bot_self_id", "")
            if bot_id not in (None, ""):
                bot_id = _snowflake(bot_id, label="bot self ID")
            else:
                bot_id = ""
            private_channels.append(
                {"user_id": user_id, "channel_id": channel_id, "bot_self_id": bot_id}
            )

        raw_bots = payload.get("bot_self_ids", [])
        if not isinstance(raw_bots, list):
            raise ValueError("invalid bot IDs in Discord identity map")
        bot_ids = frozenset(
            _snowflake(value, label="bot self ID") for value in raw_bots if value not in (None, "")
        )
        return cls(users, channels, tuple(private_channels), bot_ids)

    @classmethod
    def load(cls, path: Path) -> "IdentityMap":
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("could not read Discord identity map") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("invalid Discord identity map")
        return cls.from_mapping(payload)

    @classmethod
    def empty(cls) -> "IdentityMap":
        return cls()

    @property
    def user_logical_to_native(self) -> dict[str, str]:
        return {logical: native for native, logical in self.user_native_to_logical.items()}

    @property
    def channel_logical_to_native(self) -> dict[str, str]:
        return {logical: native for native, logical in self.channel_native_to_logical.items()}

    def logical_user(self, native_id: Any) -> str:
        native = str(native_id)
        return self.user_native_to_logical.get(native, native)

    def native_user(self, logical_id: Any) -> str | None:
        return self.user_logical_to_native.get(str(logical_id))

    def logical_channel(self, native_id: Any) -> str:
        native = str(native_id)
        return self.channel_native_to_logical.get(native, native)

    def native_channel(self, logical_id: Any) -> str | None:
        return self.channel_logical_to_native.get(str(logical_id))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "user_native_to_logical": dict(sorted(self.user_native_to_logical.items())),
            "channel_native_to_logical": dict(sorted(self.channel_native_to_logical.items())),
            "private_channels": [dict(item) for item in self.private_channels],
            "bot_self_ids": sorted(self.bot_self_ids),
        }
