"""Validated cross-adapter identity for incoming QQ friend messages.

This module is intentionally dependency-free so both adapters and Core can
construct and independently verify the same narrow QQ history identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any


QQ_HISTORY_BACKENDS = frozenset({"napcat", "snowluma"})
QQ_PRIVATE_IDENTITY_VERSION = 1
_ASCII_DECIMAL = re.compile(r"[0-9]+\Z")
_SIGNED_ASCII_DECIMAL = re.compile(r"-?[0-9]+\Z")
_MAX_NATIVE_NT_SEQUENCE = 2**32 - 1


def positive_qq_id(value: Any) -> str | None:
    """Normalize an unsigned decimal QQ account or peer ID."""

    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _ASCII_DECIMAL.fullmatch(value):
        try:
            number = int(value, 10)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    return str(number) if number > 0 else None


def native_nt_sequence(value: Any) -> int | None:
    """Accept QQNT's uint32 sequence as an integer or exact decimal string."""

    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _ASCII_DECIMAL.fullmatch(value):
        try:
            number = int(value, 10)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    return number if 1 <= number <= _MAX_NATIVE_NT_SEQUENCE else None


def snow_message_id(value: Any) -> str | None:
    """Normalize SnowLuma's nonzero signed int32 private-message ID."""

    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _SIGNED_ASCII_DECIMAL.fullmatch(value):
        try:
            number = int(value, 10)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    if number == 0 or not -(2**31) <= number < 2**31:
        return None
    return str(number)


def snow_message_id_for_native_sequence(native_sequence: Any, peer_id: Any) -> str | None:
    """Recompute SnowLuma's signed hash for a canonical QQNT C2C sequence."""

    sequence = native_nt_sequence(native_sequence)
    peer = positive_qq_id(peer_id)
    if sequence is None or peer is None:
        return None
    digest = hashlib.sha1(f"{sequence}:{peer}:private_message_nt".encode("utf-8")).digest()
    result = int.from_bytes(digest[:4], byteorder="big", signed=True)
    return str(result or 1)


def _positive_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= 100_000_000_000:
        number /= 1000.0
    return number if math.isfinite(number) and number > 0 else None


def build_qq_private_identity(
    *,
    backend: str,
    account_id: Any,
    platform: str,
    session_kind: str,
    session_id: Any,
    raw: Mapping[str, Any],
    original_timestamp: Any,
) -> dict[str, Any] | None:
    """Create proof only for an incoming, timestamp-verified friend message."""

    if backend not in QQ_HISTORY_BACKENDS or platform != "qq" or session_kind != "private":
        return None
    account = positive_qq_id(account_id)
    peer = positive_qq_id(session_id)
    if account is None or peer is None or not isinstance(raw, Mapping):
        return None
    if str(raw.get("message_type") or "").strip().lower() != "private":
        return None
    if str(raw.get("sub_type") or "").strip().lower() != "friend" or raw.get("group_id") is not None:
        return None

    sender = raw.get("sender")
    sender_value = sender.get("user_id") if isinstance(sender, Mapping) else raw.get("user_id")
    sender_id = positive_qq_id(sender_value)
    if sender_id is None or sender_id != peer or sender_id == account:
        return None
    self_id = raw.get("self_id")
    if self_id is not None and positive_qq_id(self_id) != account:
        return None

    original = _positive_timestamp(original_timestamp)
    raw_time = _positive_timestamp(raw.get("time"))
    if original is None or raw_time is None or raw_time != original:
        return None

    identity: dict[str, Any] = {
        "version": QQ_PRIVATE_IDENTITY_VERSION,
        "scope": "qq_private_friend",
        "account_id": account,
        "platform": platform,
        "peer_id": peer,
        "sender_id": sender_id,
        "original_timestamp": original,
    }
    if backend == "napcat":
        sequence = native_nt_sequence(raw.get("real_seq"))
        if sequence is None:
            return None
        alias = snow_message_id_for_native_sequence(sequence, peer)
        if alias is None:
            return None
        identity["native_nt_seq"] = sequence
        identity["snow_message_id"] = alias
    else:
        alias = snow_message_id(raw.get("message_id"))
        if alias is None:
            return None
        identity["snow_message_id"] = alias
    return identity


def validate_qq_private_identity(
    identity: Any,
    *,
    backend: Any,
    account_id: Any,
    platform: Any,
    session_kind: Any,
    session_id: Any,
    message_id: Any,
    original_timestamp: Any,
) -> dict[str, Any] | None:
    """Normalize and verify optional QQ proof against Core delivery fields."""

    if not isinstance(identity, Mapping):
        return None
    backend_name = str(backend or "").strip()
    account = positive_qq_id(account_id)
    peer = positive_qq_id(session_id)
    stamp = _positive_timestamp(original_timestamp)
    snow_id = snow_message_id(identity.get("snow_message_id"))
    identity_account = positive_qq_id(identity.get("account_id"))
    identity_peer = positive_qq_id(identity.get("peer_id"))
    sender = positive_qq_id(identity.get("sender_id"))
    identity_stamp = _positive_timestamp(identity.get("original_timestamp"))
    if (
        type(identity.get("version")) is not int
        or identity.get("version") != QQ_PRIVATE_IDENTITY_VERSION
        or identity.get("scope") != "qq_private_friend"
        or backend_name not in QQ_HISTORY_BACKENDS
        or str(platform or "").strip() != "qq"
        or str(identity.get("platform") or "").strip() != "qq"
        or str(session_kind or "").strip() != "private"
        or account is None
        or peer is None
        or identity_account != account
        or identity_peer != peer
        or sender != peer
        or stamp is None
        or identity_stamp != stamp
        or snow_id is None
    ):
        return None

    normalized: dict[str, Any] = {
        "version": QQ_PRIVATE_IDENTITY_VERSION,
        "scope": "qq_private_friend",
        "account_id": account,
        "platform": "qq",
        "peer_id": peer,
        "sender_id": sender,
        "original_timestamp": stamp,
        "snow_message_id": snow_id,
    }
    if backend_name == "napcat":
        sequence = native_nt_sequence(identity.get("native_nt_seq"))
        if sequence is None or snow_message_id_for_native_sequence(sequence, peer) != snow_id:
            return None
        normalized["native_nt_seq"] = sequence
    else:
        if "native_nt_seq" in identity or snow_message_id(message_id) != snow_id:
            return None
    return normalized


def qq_canonical_identity_key(identity: Mapping[str, Any]) -> str | None:
    sequence = native_nt_sequence(identity.get("native_nt_seq"))
    account = positive_qq_id(identity.get("account_id"))
    peer = positive_qq_id(identity.get("peer_id"))
    if sequence is None or account is None or peer is None or identity.get("platform") != "qq":
        return None
    return json.dumps((account, "qq", peer, sequence), ensure_ascii=False, separators=(",", ":"))


def qq_snow_alias_key(identity: Mapping[str, Any]) -> str | None:
    account = positive_qq_id(identity.get("account_id"))
    peer = positive_qq_id(identity.get("peer_id"))
    alias = snow_message_id(identity.get("snow_message_id"))
    if account is None or peer is None or alias is None or identity.get("platform") != "qq":
        return None
    return json.dumps((account, "qq", peer, alias), ensure_ascii=False, separators=(",", ":"))


def resolve_qq_reply_segments(
    segments: Any,
    *,
    identity: Any,
    backend: str,
    account_id: Any,
    platform: str,
    peer_id: Any,
    is_private: bool,
) -> list[Any]:
    """Resolve one serialized QQ reply segment to this adapter's native ID.

    The helper intentionally receives a plain OneBot segment list and imports
    no adapter/Core code. If a scoped hint is present but cannot be proven for
    this exact route/account, its target reply segment is omitted while body
    segments are preserved.
    """

    if not isinstance(segments, list):
        return []
    if not is_private or platform != "qq" or identity is None:
        return segments
    if not isinstance(identity, Mapping):
        identity = {}

    target_value = identity.get("original_reply_id")
    target = str(target_value).strip() if target_value is not None else ""
    version_ok = type(identity.get("version")) is int and identity.get("version") == 1
    account = positive_qq_id(account_id)
    peer = positive_qq_id(peer_id)
    scope_ok = (
        version_ok
        and identity.get("scope") == "qq_private_friend"
        and identity.get("platform") == "qq"
        and account is not None
        and peer is not None
        and positive_qq_id(identity.get("account_id")) == account
        and positive_qq_id(identity.get("peer_id")) == peer
        and backend in QQ_HISTORY_BACKENDS
        and bool(target)
    )
    native_ids = identity.get("native_ids")
    native = native_ids.get(backend) if isinstance(native_ids, Mapping) else None
    native_id = str(native).strip() if isinstance(native, (str, int)) and not isinstance(native, bool) else ""
    if backend == "snowluma" and snow_message_id(native_id) is None:
        native_id = ""
    elif backend == "napcat" and (not native_id.isascii() or not native_id.isdecimal()):
        native_id = ""

    rewritten: list[Any] = []
    for segment in segments:
        if not isinstance(segment, Mapping) or segment.get("type") != "reply":
            rewritten.append(segment)
            continue
        data = segment.get("data")
        segment_target = data.get("id") if isinstance(data, Mapping) else data
        if target and str(segment_target).strip() == target:
            if not scope_ok or not native_id:
                continue
            updated = dict(segment)
            if isinstance(data, Mapping):
                updated_data = dict(data)
                updated_data["id"] = native_id
                updated["data"] = updated_data
            else:
                updated["data"] = native_id
            rewritten.append(updated)
            continue
        # A scoped hint means any unrecognized reply target is unresolved too;
        # retaining it could quote an unrelated ID after an adapter switch.
    return rewritten


__all__ = [
    "QQ_HISTORY_BACKENDS",
    "QQ_PRIVATE_IDENTITY_VERSION",
    "build_qq_private_identity",
    "native_nt_sequence",
    "positive_qq_id",
    "qq_canonical_identity_key",
    "qq_snow_alias_key",
    "resolve_qq_reply_segments",
    "snow_message_id",
    "snow_message_id_for_native_sequence",
    "validate_qq_private_identity",
]
