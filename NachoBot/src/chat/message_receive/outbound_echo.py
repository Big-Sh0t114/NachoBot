"""Bounded coordination for platform message-ID echoes during outbound sends.

Adapters can acknowledge a queued message before the sender reaches storage.
This registry holds only the in-flight message plus a bounded, short-lived set
of stored sends so an early echo can be applied after storage and a late echo
can still update the mutable outgoing object.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


_DEFAULT_MAX_RECORDS = 2048
_DEFAULT_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class OutboundEchoHandle:
    key: tuple[str, str]
    token: object


@dataclass(frozen=True)
class OutboundEchoMatch:
    matched: bool = False
    stored: bool = False
    duplicate: bool = False
    conflict: bool = False


@dataclass
class _OutboundRecord:
    token: object
    message: Any
    message_id: str
    platform: str
    expires_at: float
    actual_message_id: str | None = None
    stored: bool = False


class OutboundEchoRegistry:
    """Match echoes by exact core message ID and platform with bounded lifetime."""

    def __init__(
        self,
        *,
        max_records: int = _DEFAULT_MAX_RECORDS,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        if max_records < 1 or ttl_seconds <= 0:
            raise ValueError("outbound echo bounds must be positive")
        self._max_records = int(max_records)
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._records: OrderedDict[tuple[str, str], _OutboundRecord] = OrderedDict()
        self._lock = threading.RLock()

    def register(self, message: Any) -> OutboundEchoHandle | None:
        info = getattr(message, "message_info", None)
        message_id = getattr(info, "message_id", None)
        platform = getattr(info, "platform", None)
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(platform, str)
            or not platform
        ):
            return None

        key = (platform, message_id)
        with self._lock:
            now = self._clock()
            self._prune(now)
            # A duplicate in-flight identity is ambiguous. Fail closed instead
            # of allowing an echo to mutate the wrong outgoing object.
            if key in self._records:
                return None
            token = object()
            self._records[key] = _OutboundRecord(
                token=token,
                message=message,
                message_id=message_id,
                platform=platform,
                expires_at=now + self._ttl_seconds,
            )
            self._trim()
            if self._records.get(key) is None or self._records[key].token is not token:
                return None
            return OutboundEchoHandle(key=key, token=token)

    def observe_echo(
        self,
        message_id: Any,
        platform: Any,
        actual_message_id: Any,
    ) -> OutboundEchoMatch:
        if isinstance(actual_message_id, int) and not isinstance(actual_message_id, bool):
            actual_message_id = str(actual_message_id)
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(platform, str)
            or not platform
            or not isinstance(actual_message_id, str)
            or not actual_message_id
        ):
            return OutboundEchoMatch()

        key = (platform, message_id)
        with self._lock:
            now = self._clock()
            self._prune(now)
            record = self._records.get(key)
            if record is None:
                return OutboundEchoMatch()

            duplicate = record.actual_message_id is not None
            if duplicate and record.actual_message_id != actual_message_id:
                return OutboundEchoMatch(conflict=True, stored=record.stored)
            if not duplicate:
                record.actual_message_id = actual_message_id

            record.expires_at = now + self._ttl_seconds
            self._records.move_to_end(key)
            if record.stored:
                self._set_message_id(record.message, actual_message_id)
            return OutboundEchoMatch(
                matched=True,
                stored=record.stored,
                duplicate=duplicate,
            )

    def mark_stored(self, handle: OutboundEchoHandle | None) -> str | None:
        """Mark a persisted send and apply any echo that arrived before storage."""
        with self._lock:
            record = self._get_handle_record(handle)
            if record is None:
                return None
            record.stored = True
            record.expires_at = self._clock() + self._ttl_seconds
            self._records.move_to_end(handle.key)
            actual_message_id = record.actual_message_id
            if actual_message_id is not None:
                self._set_message_id(record.message, actual_message_id)
            self._trim()
            return actual_message_id

    def finish_unstored(self, handle: OutboundEchoHandle | None) -> str | None:
        """Finish without persistence and release the in-flight reference."""
        with self._lock:
            record = self._get_handle_record(handle)
            if record is None:
                return None
            actual_message_id = record.actual_message_id
            if actual_message_id is not None:
                self._set_message_id(record.message, actual_message_id)
            self._records.pop(handle.key, None)
            return actual_message_id

    def discard(self, handle: OutboundEchoHandle | None) -> None:
        """Drop failed or cancelled sends so a later echo cannot be misapplied."""
        with self._lock:
            record = self._get_handle_record(handle)
            if record is not None:
                self._records.pop(handle.key, None)

    def record_count(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._records)

    def _get_handle_record(self, handle: OutboundEchoHandle | None) -> _OutboundRecord | None:
        if handle is None:
            return None
        record = self._records.get(handle.key)
        if record is None or record.token is not handle.token:
            return None
        return record

    def _prune(self, now: float) -> None:
        expired = [key for key, record in self._records.items() if record.expires_at <= now]
        for key in expired:
            self._records.pop(key, None)

    def _trim(self) -> None:
        while len(self._records) > self._max_records:
            # Prefer evicting completed rows, keeping in-flight sends available
            # for their immediate adapter ACK whenever possible.
            evict_key = next(
                (key for key, record in self._records.items() if record.stored),
                next(iter(self._records)),
            )
            self._records.pop(evict_key, None)

    @staticmethod
    def _set_message_id(message: Any, message_id: str) -> None:
        info = getattr(message, "message_info", None)
        if info is not None:
            info.message_id = message_id


outbound_echo_registry = OutboundEchoRegistry()
