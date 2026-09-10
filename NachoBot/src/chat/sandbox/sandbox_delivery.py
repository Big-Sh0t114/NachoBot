"""In-process delivery gate for sandbox handoffs.

This is intentionally an in-process idempotency fence. It is not a crash-safe
outbox: a process crash between an adapter ACK and task creation can lose the
handoff, which is preferable to starting from a non-delivered/suppressed send.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable, Iterable, Optional

from src.chat.sandbox.sandbox_agent import SandboxAgentCoordinator
from src.chat.sandbox.sandbox_handoff import SandboxEditHandoff
from src.common.logger import get_logger
from src.plugin_system.apis.send_api import SendStatus

logger = get_logger("sandbox_delivery")


class SandboxDeliveryGate:
    """Claim one delivered acknowledgement and schedule one agent task."""

    def __init__(
        self,
        *,
        runner: Optional[Callable[[SandboxEditHandoff], Awaitable[Any]]] = None,
        authorize: Optional[Callable[[SandboxEditHandoff], bool | Awaitable[bool]]] = None,
    ) -> None:
        if runner is None:
            coordinator = SandboxAgentCoordinator()
            self.runner = coordinator.run_handoff
            self._default_authorize = authorize or coordinator._authorized
        else:
            self.runner = runner
            self._default_authorize = authorize
        self._lock = threading.Lock()
        self._claimed_pairs: set[tuple[str, str]] = set()
        self._claimed_handoffs: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    def _observe_task(self, task: asyncio.Task[Any]) -> None:
        """Consume background outcomes so failures are never unhandled/silent."""

        self._tasks.discard(task)
        try:
            result = task.result()
        except asyncio.CancelledError:
            logger.info("sandbox delivery task completed: outcome=CANCELLED")
        except Exception as exc:
            logger.error("sandbox delivery task failed: %s", type(exc).__name__)
        else:
            outcome = getattr(getattr(result, "outcome", None), "value", None) or str(
                getattr(result, "outcome", "UNKNOWN")
            )
            rounds = max(0, int(getattr(result, "rounds", 0) or 0))
            tool_calls = max(0, int(getattr(result, "tool_calls", 0) or 0))
            path_count = len(getattr(result, "changed_paths", ()) or ())
            logger.info(
                "sandbox delivery task completed: outcome=%s rounds=%d tool_calls=%d path_count=%d",
                outcome,
                rounds,
                tool_calls,
                path_count,
            )

    @property
    def claimed_pairs(self) -> frozenset[tuple[str, str]]:
        with self._lock:
            return frozenset(self._claimed_pairs)

    @staticmethod
    def _delivered_message_id(receipts: Iterable[Any], expected_stream_id: str) -> Optional[str]:
        for receipt in receipts:
            if getattr(receipt, "stream_id", None) != expected_stream_id:
                continue
            # A truthy convenience property on an adapter fake is not enough:
            # the delivery gate is intentionally keyed to the real receipt
            # status so failed/suppressed/stale sends cannot start editing.
            if getattr(receipt, "status", None) is SendStatus.DELIVERED:
                message_id = getattr(receipt, "message_id", None)
                if message_id:
                    return str(message_id)
        return None

    async def claim_after_delivery(
        self,
        handoff: Optional[SandboxEditHandoff],
        receipts: Iterable[Any],
        *,
        authorize: Optional[Callable[[SandboxEditHandoff], bool | Awaitable[bool]]] = None,
        delivered_content: Optional[str] = None,
    ) -> bool:
        """Schedule only after a real DELIVERED receipt and an atomic claim."""

        if handoff is None or not handoff.binding_is_valid():
            return False
        delivered_message_id = self._delivered_message_id(receipts, handoff.stream_id)
        if delivered_message_id is None:
            return False
        # A production handoff must carry the server-approved acknowledgement,
        # and the delivered text must match it exactly.  In particular, do not
        # treat the optional wrapper argument's ``None`` default as proof that
        # the acknowledged reply was delivered.
        if not str(handoff.acknowledgement_fingerprint or "").strip():
            return False
        if delivered_content is None or not handoff.matches_acknowledgement(delivered_content):
            return False
        effective_authorize = authorize or self._default_authorize
        if effective_authorize is not None:
            allowed = effective_authorize(handoff)
            if asyncio.iscoroutine(allowed):
                allowed = await allowed
            if not allowed:
                return False
        pair = (handoff.handoff_id, delivered_message_id)
        with self._lock:
            # Keep both the exact ACK pair and a handoff-wide fence. The latter
            # prevents a retry that received a different adapter message ID
            # from launching the same edit twice.
            if pair in self._claimed_pairs or handoff.handoff_id in self._claimed_handoffs:
                return False
            self._claimed_pairs.add(pair)
            self._claimed_handoffs.add(handoff.handoff_id)
        try:
            task = asyncio.create_task(self.runner(handoff))
        except Exception:
            with self._lock:
                self._claimed_pairs.discard(pair)
                self._claimed_handoffs.discard(handoff.handoff_id)
            return False
        self._tasks.add(task)
        task.add_done_callback(self._observe_task)
        return True

    async def wait_for_tasks(self) -> None:
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


sandbox_delivery_gate = SandboxDeliveryGate()


async def schedule_sandbox_after_delivery(
    handoff: Optional[SandboxEditHandoff],
    receipts: Iterable[Any],
    *,
    gate: SandboxDeliveryGate = sandbox_delivery_gate,
    delivered_content: Optional[str] = None,
) -> bool:
    return await gate.claim_after_delivery(handoff, receipts, delivered_content=delivered_content)


__all__ = ["SandboxDeliveryGate", "sandbox_delivery_gate", "schedule_sandbox_after_delivery"]
