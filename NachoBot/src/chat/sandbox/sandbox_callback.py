"""In-process rendezvous for sandbox agent CALL_BACK requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from src.chat.sandbox.sandbox_handoff import SandboxEditHandoff
from src.common.logger import get_logger

logger = get_logger("sandbox_agent")


@dataclass(slots=True)
class PendingSandboxCallback:
    handoff_id: str
    stream_id: str
    platform: str
    actor_id: str
    query: str
    future: asyncio.Future[str]


class SandboxCallbackRegistry:
    """Own the single pending user callback for each sandbox handoff."""

    def __init__(self) -> None:
        self._pending_by_handoff: dict[str, PendingSandboxCallback] = {}
        self._handoff_by_identity: dict[tuple[str, str, str], str] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _identity(stream_id: str, platform: str, actor_id: str) -> tuple[str, str, str]:
        return (str(stream_id or ""), str(platform or ""), str(actor_id or ""))

    async def register(self, handoff: SandboxEditHandoff, query: str) -> PendingSandboxCallback:
        callback_query = str(query or "").strip()
        if not callback_query:
            raise ValueError("CALL_BACK query cannot be empty")

        loop = asyncio.get_running_loop()
        pending = PendingSandboxCallback(
            handoff_id=handoff.handoff_id,
            stream_id=handoff.stream_id,
            platform=handoff.platform,
            actor_id=handoff.actor_id,
            query=callback_query,
            future=loop.create_future(),
        )
        identity = self._identity(handoff.stream_id, handoff.platform, handoff.actor_id)

        async with self._lock:
            if handoff.handoff_id in self._pending_by_handoff:
                raise RuntimeError("sandbox handoff already has a pending CALL_BACK")
            existing = self._handoff_by_identity.get(identity)
            if existing is not None:
                raise RuntimeError("sandbox actor already has a pending CALL_BACK")
            self._pending_by_handoff[handoff.handoff_id] = pending
            self._handoff_by_identity[identity] = handoff.handoff_id
        return pending

    async def wait(self, pending: PendingSandboxCallback) -> str:
        identity = self._identity(pending.stream_id, pending.platform, pending.actor_id)
        try:
            return await pending.future
        finally:
            async with self._lock:
                current = self._pending_by_handoff.get(pending.handoff_id)
                if current is pending:
                    self._pending_by_handoff.pop(pending.handoff_id, None)
                    if self._handoff_by_identity.get(identity) == pending.handoff_id:
                        self._handoff_by_identity.pop(identity, None)

    async def wait_for_reply(self, handoff: SandboxEditHandoff, query: str) -> str:
        pending = await self.register(handoff, query)
        return await self.wait(pending)

    async def deliver_reply(self, *, stream_id: str, platform: str, actor_id: str, text: str) -> bool:
        reply_text = str(text or "").strip()
        if not reply_text:
            return False

        identity = self._identity(stream_id, platform, actor_id)
        async with self._lock:
            handoff_id = self._handoff_by_identity.get(identity)
            if handoff_id is None:
                return False
            pending = self._pending_by_handoff.get(handoff_id)
            if pending is None or pending.future.done():
                return False
            pending.future.set_result(reply_text)
            return True

    async def cancel(self, handoff_id: str) -> None:
        target_id = str(handoff_id or "")
        async with self._lock:
            pending = self._pending_by_handoff.pop(target_id, None)
            if pending is None:
                return
            identity = self._identity(pending.stream_id, pending.platform, pending.actor_id)
            if self._handoff_by_identity.get(identity) == target_id:
                self._handoff_by_identity.pop(identity, None)
            if not pending.future.done():
                pending.future.cancel()

    async def pending_query(self, handoff_id: str) -> Optional[str]:
        async with self._lock:
            pending = self._pending_by_handoff.get(str(handoff_id or ""))
            return pending.query if pending is not None else None


sandbox_callback_registry = SandboxCallbackRegistry()


__all__ = ["PendingSandboxCallback", "SandboxCallbackRegistry", "sandbox_callback_registry"]
