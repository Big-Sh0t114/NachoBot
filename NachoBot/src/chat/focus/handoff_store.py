"""Handoff persistence boundary and a concurrency-safe in-memory implementation."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Protocol

from .models import FocusHandoff, HandoffStatus


class HandoffStore(Protocol):
    async def put(self, handoff: FocusHandoff) -> None: ...

    async def get(self, handoff_id: str) -> FocusHandoff | None: ...

    async def get_active(self, group_id: str, target_chat_id: str, target_epoch: int) -> tuple[FocusHandoff, ...]: ...

    async def acknowledge(self, handoff_id: str, cycle_id: str, delivery_id: str) -> bool: ...

    async def supersede(self, handoff_id: str) -> bool: ...

    async def expire(self, now: float | None = None) -> int: ...


class InMemoryHandoffStore:
    """Useful for tests and deployments where restart recovery is not enabled."""

    def __init__(self) -> None:
        self._handoffs: dict[str, FocusHandoff] = {}
        self._acked_cycles: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    async def put(self, handoff: FocusHandoff) -> None:
        async with self._lock:
            existing = self._handoffs.get(handoff.handoff_id)
            if existing is not None and existing != handoff:
                raise ValueError(f"Conflicting handoff id {handoff.handoff_id!r}")
            self._handoffs[handoff.handoff_id] = handoff
            self._acked_cycles.setdefault(handoff.handoff_id, {})

    async def get(self, handoff_id: str) -> FocusHandoff | None:
        async with self._lock:
            return self._live_handoff(handoff_id, time.time())

    async def get_active(self, group_id: str, target_chat_id: str, target_epoch: int) -> tuple[FocusHandoff, ...]:
        now = time.time()
        async with self._lock:
            result = []
            for handoff_id in self._handoffs:
                current = self._live_handoff(handoff_id, now)
                if current is None:
                    continue
                if (
                    current.group_id == group_id
                    and current.target_chat_id == target_chat_id
                    and current.target_epoch == target_epoch
                ):
                    result.append(current)
            return tuple(sorted(result, key=lambda item: (item.created_at, item.handoff_id)))

    async def acknowledge(self, handoff_id: str, cycle_id: str, delivery_id: str) -> bool:
        """Count one successfully delivered logical cycle, idempotently."""

        if not cycle_id or not delivery_id:
            raise ValueError("cycle_id and delivery_id are required for a handoff acknowledgement")
        async with self._lock:
            handoff = self._live_handoff(handoff_id, time.time())
            if handoff is None:
                return False
            acknowledgements = self._acked_cycles.setdefault(handoff_id, {})
            prior_delivery = acknowledgements.get(cycle_id)
            if prior_delivery is not None:
                return prior_delivery == delivery_id
            acknowledgements[cycle_id] = delivery_id
            if len(acknowledgements) >= handoff.max_successful_cycles:
                self._handoffs[handoff_id] = replace(handoff, status=HandoffStatus.CONSUMED)
            return True

    async def supersede(self, handoff_id: str) -> bool:
        async with self._lock:
            handoff = self._handoffs.get(handoff_id)
            if handoff is None:
                return False
            if handoff.status is HandoffStatus.ACTIVE:
                self._handoffs[handoff_id] = replace(handoff, status=HandoffStatus.SUPERSEDED)
            return True

    async def expire(self, now: float | None = None) -> int:
        deadline = time.time() if now is None else now
        count = 0
        async with self._lock:
            for handoff_id, handoff in tuple(self._handoffs.items()):
                if handoff.status is HandoffStatus.ACTIVE and handoff.expires_at <= deadline:
                    self._handoffs[handoff_id] = replace(handoff, status=HandoffStatus.EXPIRED)
                    count += 1
        return count

    def _live_handoff(self, handoff_id: str, now: float) -> FocusHandoff | None:
        handoff = self._handoffs.get(handoff_id)
        if handoff is None or handoff.status is not HandoffStatus.ACTIVE:
            return None
        if handoff.expires_at <= now:
            self._handoffs[handoff_id] = replace(handoff, status=HandoffStatus.EXPIRED)
            return None
        return handoff
