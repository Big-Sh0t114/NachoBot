"""Process-local bindings between slash ingress and ephemeral Core egress."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


INTERACTION_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_INTERACTION_TTL_SECONDS = 8 * 60
DEFAULT_MAX_PENDING_INTERACTIONS = 128


@dataclass(slots=True)
class SlashInteractionBinding:
    """Local-only interaction callback and the native route it is bound to."""

    key: str
    followup: Any
    channel_id: str
    user_id: str
    guild_id: str
    expires_at: float
    result: asyncio.Future[bool]
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SlashInteractionRegistry:
    """Bounded, expiring correlation store; only ``key`` crosses into Core."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_INTERACTION_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_PENDING_INTERACTIONS,
        clock=time.monotonic,
    ):
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("interaction registry bounds must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._entries: dict[str, SlashInteractionBinding] = {}

    def register(self, followup: Any, target: dict[str, str]) -> SlashInteractionBinding:
        now = self._clock()
        self._prune(now)
        while len(self._entries) >= self.max_entries:
            oldest_key = next(iter(self._entries))
            self._remove(oldest_key)

        key = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        binding = SlashInteractionBinding(
            key=key,
            followup=followup,
            channel_id=target["channel_id"],
            user_id=target["user_id"],
            guild_id=target.get("guild_id", ""),
            expires_at=now + self.ttl_seconds,
            result=loop.create_future(),
        )
        self._entries[key] = binding
        return binding

    def get(self, key: Any, target: dict[str, str]) -> SlashInteractionBinding | None:
        if not isinstance(key, str) or not INTERACTION_KEY_RE.fullmatch(key):
            return None
        now = self._clock()
        self._prune(now)
        binding = self._entries.get(key)
        if binding is None or binding.expires_at <= now:
            return None
        if (
            binding.channel_id != target.get("channel_id")
            or binding.user_id != target.get("user_id")
            or binding.guild_id != target.get("guild_id", "")
        ):
            return None
        return binding

    async def wait(self, binding: SlashInteractionBinding) -> bool:
        timeout = max(0.0, binding.expires_at - self._clock())
        try:
            return bool(await asyncio.wait_for(asyncio.shield(binding.result), timeout))
        except TimeoutError:
            self._remove(binding.key)
            return False

    def complete(self, binding: SlashInteractionBinding, delivered: bool) -> None:
        if not binding.result.done():
            binding.result.set_result(bool(delivered))
        if not delivered:
            self._remove(binding.key)

    def discard(self, key: str) -> None:
        self._remove(key)

    def clear(self) -> None:
        for key in tuple(self._entries):
            self._remove(key)

    def _prune(self, now: float) -> None:
        for key, binding in tuple(self._entries.items()):
            if binding.expires_at <= now:
                self._remove(key)

    def _remove(self, key: str) -> None:
        binding = self._entries.pop(key, None)
        if binding is not None and not binding.result.done():
            binding.result.set_result(False)

    def __len__(self) -> int:
        return len(self._entries)
