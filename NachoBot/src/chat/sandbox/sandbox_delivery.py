"""ACK-gated durable sandbox task scheduling and artifact delivery."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from src.chat.sandbox.sandbox_agent import SandboxAgentCoordinator
from src.chat.sandbox.sandbox_handoff import SandboxEditHandoff
from src.chat.sandbox.sandbox_manager import SandboxManager, sandbox_manager, validate_relative_path
from src.chat.sandbox.sandbox_task_store import SandboxTaskStore, SandboxTaskStoreError
from src.common.logger import get_logger
from src.plugin_system.apis.send_api import SendStatus

logger = get_logger("sandbox_delivery")

_PROVEN_PRE_SEND_FAILURES = {
    "missing_reply_message",
    "stream_not_found",
    "cross_stream_reply",
    "invalid_ack_timeout",
}


def _outcome_label(value: Any) -> str:
    """Convert a terminal outcome to a concise console label."""

    labels = {
        "FINALIZED": "已完成",
        "PUBLICATION_FAILED": "文件已修改，附件送达未确认",
        "NO_FINALIZE": "未完成最终确认",
        "BUDGET_EXHAUSTED": "达到处理上限",
        "DUPLICATE_STALL": "重复操作导致中止",
        "MODEL_ERROR": "模型处理失败",
        "CANCELLED": "已取消",
        "TIMEOUT": "等待模型超时",
    }
    raw = getattr(value, "value", value)
    text = str(raw or "未知")
    return labels.get(text, text)


class SandboxDeliveryGate:
    """Claim a delivered acknowledgement once, then track model and file work."""

    def __init__(
        self,
        *,
        runner: Optional[Callable[[SandboxEditHandoff], Awaitable[Any]]] = None,
        coordinator: Optional[SandboxAgentCoordinator] = None,
        authorize: Optional[Callable[[SandboxEditHandoff], bool | Awaitable[bool]]] = None,
        store: Optional[SandboxTaskStore] = None,
        manager: SandboxManager = sandbox_manager,
    ) -> None:
        self._coordinator: Optional[SandboxAgentCoordinator] = coordinator
        if runner is None and coordinator is not None:
            self.runner = coordinator.run_handoff
            self._default_authorize = authorize or coordinator._authorized
        elif runner is None:
            self._coordinator = SandboxAgentCoordinator(manager=manager)
            self.runner = self._coordinator.run_handoff
            self._default_authorize = authorize or self._coordinator._authorized
        else:
            self.runner = runner
            self._default_authorize = authorize
        self.manager = manager
        self.store: Optional[SandboxTaskStore] = None
        self._transport_ready: Optional[Callable[[SandboxEditHandoff], Any]] = None
        self._lock = threading.Lock()
        self._claimed_pairs: set[tuple[str, str]] = set()
        self._claimed_handoffs: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._publisher_task: Optional[asyncio.Task[Any]] = None
        self._publisher_started = False
        self._accepting = True
        if store is not None:
            self.attach_store(store)

    def attach_store(
        self,
        store: SandboxTaskStore,
        *,
        transport_ready: Optional[Callable[[SandboxEditHandoff], Any]] = None,
    ) -> None:
        """Enable durable claims after startup has acquired the store owner."""

        if self._tasks:
            raise RuntimeError("cannot replace sandbox task store while jobs are active")
        self.store = store
        self._transport_ready = transport_ready
        self._accepting = True
        if self._coordinator is not None:
            self._coordinator.publish = self._publish_committed

    def _observe_task(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        try:
            result = task.result()
        except asyncio.CancelledError:
            logger.info("沙盒交付任务已取消")
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
                "沙盒交付任务已结束：结果为 %s，共执行 %d 轮、调用工具 %d 次、涉及 %d 个文件",
                _outcome_label(outcome),
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
            if getattr(receipt, "status", None) is SendStatus.DELIVERED:
                message_id = getattr(receipt, "message_id", None)
                if message_id:
                    return str(message_id)
        return None

    @staticmethod
    def _delivered_receipt(receipts: Iterable[Any], expected_stream_id: str) -> Any:
        for receipt in receipts:
            if (
                getattr(receipt, "stream_id", None) == expected_stream_id
                and getattr(receipt, "status", None) is SendStatus.DELIVERED
                and getattr(receipt, "message_id", None)
            ):
                return receipt
        return None

    async def _is_authorized(self, handoff: SandboxEditHandoff) -> bool:
        if not handoff.binding_is_valid():
            return False
        authorize = self._default_authorize
        if authorize is None:
            return True
        result = authorize(handoff)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def _create_runner_task(self, handoff: SandboxEditHandoff) -> bool:
        try:
            task = asyncio.create_task(self._run_claimed(handoff))
        except Exception as exc:
            logger.error("sandbox task scheduling failed: %s", type(exc).__name__)
            return False
        self._tasks.add(task)
        task.add_done_callback(self._observe_task)
        return True

    async def _run_claimed(self, handoff: SandboxEditHandoff) -> Any:
        store = self.store
        if store is not None and not store.mark_running(handoff.handoff_id):
            return None
        try:
            allowed = await self._is_authorized(handoff)
        except Exception as exc:
            logger.error("sandbox run authorization failed: %s", type(exc).__name__)
            if store is not None:
                store.mark_denied(handoff.handoff_id)
            return None
        if not allowed:
            if store is not None:
                store.mark_denied(handoff.handoff_id)
            return None
        try:
            result = await self.runner(handoff)
        except asyncio.CancelledError:
            if store is not None:
                try:
                    store.mark_interrupted(handoff.handoff_id, error_code="shutdown_cancelled")
                except Exception as exc:
                    logger.error("sandbox task interruption record failed: %s", type(exc).__name__)
            raise
        except Exception as exc:
            if store is not None:
                try:
                    store.mark_failed(handoff.handoff_id, error_code=type(exc).__name__)
                except Exception as store_exc:
                    logger.error("sandbox task failure record failed: %s", type(store_exc).__name__)
            raise
        else:
            if store is not None:
                store.record_result(handoff.handoff_id, result)
            return result

    async def claim_after_delivery(
        self,
        handoff: Optional[SandboxEditHandoff],
        receipts: Iterable[Any],
        *,
        authorize: Optional[Callable[[SandboxEditHandoff], bool | Awaitable[bool]]] = None,
        delivered_content: Optional[str] = None,
    ) -> bool:
        """Persist a task claim only for a valid handoff and real ACK receipt."""

        if handoff is None or not handoff.binding_is_valid() or not self._accepting:
            return False
        receipt_items = tuple(receipts)
        accepted_receipt = self._delivered_receipt(receipt_items, handoff.stream_id)
        delivered_message_id = (
            str(accepted_receipt.message_id) if accepted_receipt is not None else None
        )
        if delivered_message_id is None:
            return False
        if not handoff.acknowledgement_fingerprint or delivered_content is None:
            return False
        if not handoff.matches_acknowledgement(delivered_content):
            return False
        effective_authorize = authorize or self._default_authorize
        if effective_authorize is not None:
            try:
                allowed = effective_authorize(handoff)
                if inspect.isawaitable(allowed):
                    allowed = await allowed
            except Exception as exc:
                logger.error("sandbox admission authorization failed: %s", type(exc).__name__)
                return False
            if not allowed or not self._accepting:
                return False

        pair = (handoff.handoff_id, delivered_message_id)
        with self._lock:
            if pair in self._claimed_pairs or handoff.handoff_id in self._claimed_handoffs:
                return False
        if self.store is not None:
            try:
                claimed = self.store.enqueue_after_ack(handoff, accepted_receipt, delivered_content)
            except Exception as exc:
                logger.error("sandbox durable claim failed: %s", type(exc).__name__)
                return False
            if not claimed:
                return False
        with self._lock:
            if pair in self._claimed_pairs or handoff.handoff_id in self._claimed_handoffs:
                # The SQLite unique key remains authoritative across gate objects.
                return False
            self._claimed_pairs.add(pair)
            self._claimed_handoffs.add(handoff.handoff_id)
        if not self._create_runner_task(handoff):
            # A durable QUEUED row is intentionally retained for recovery.
            if self.store is not None:
                return True
            with self._lock:
                self._claimed_pairs.discard(pair)
                self._claimed_handoffs.discard(handoff.handoff_id)
            return False
        return True

    async def resume_queued(self) -> int:
        """Reauthorize and launch durable QUEUED jobs; never resume RUNNING work."""

        if self.store is None or not self._accepting:
            return 0
        launched = 0
        for handoff in self.store.queued_handoffs():
            if not self._accepting:
                break
            try:
                allowed = await self._is_authorized(handoff)
            except Exception as exc:
                logger.error("sandbox queued authorization failed: %s", type(exc).__name__)
                allowed = False
            if not self._accepting:
                break
            if not allowed:
                self.store.mark_denied(handoff.handoff_id)
                continue
            with self._lock:
                self._claimed_handoffs.add(handoff.handoff_id)
            if self._create_runner_task(handoff):
                launched += 1
        return launched

    async def resume_queued_when_connected(self, *, poll_interval_seconds: float = 1.0) -> int:
        """Defer restart work until its original platform route is connected."""

        if self.store is None or not self._accepting:
            return 0
        launched = 0
        for handoff in self.store.queued_handoffs():
            if not self._accepting:
                break
            try:
                allowed = await self._is_authorized(handoff)
            except Exception as exc:
                logger.error("sandbox queued authorization failed: %s", type(exc).__name__)
                allowed = False
            if not self._accepting:
                break
            if not allowed:
                self.store.mark_denied(handoff.handoff_id)
                continue
            with self._lock:
                if handoff.handoff_id in self._claimed_handoffs:
                    continue
                self._claimed_handoffs.add(handoff.handoff_id)
            try:
                task = asyncio.create_task(
                    self._run_when_connected(
                        handoff,
                        poll_interval_seconds=max(0.05, float(poll_interval_seconds)),
                    )
                )
            except Exception as exc:
                logger.error("sandbox queued task scheduling failed: %s", type(exc).__name__)
                continue
            self._tasks.add(task)
            task.add_done_callback(self._observe_task)
            if task:
                launched += 1
        return launched

    async def _run_when_connected(
        self,
        handoff: SandboxEditHandoff,
        *,
        poll_interval_seconds: float,
    ) -> Any:
        while self._accepting:
            try:
                connected = await self._transport_is_ready(handoff)
            except Exception as exc:
                logger.error("sandbox transport readiness check failed: %s", type(exc).__name__)
                await asyncio.sleep(poll_interval_seconds)
                continue
            if connected:
                if not self._accepting:
                    return None
                try:
                    allowed = await self._is_authorized(handoff)
                except Exception as exc:
                    logger.error("sandbox queued authorization failed: %s", type(exc).__name__)
                    allowed = False
                if not allowed:
                    if self._accepting and self.store is not None:
                        self.store.mark_denied(handoff.handoff_id)
                    return None
                if not self._accepting:
                    return None
                return await self._run_claimed(handoff)
            await asyncio.sleep(poll_interval_seconds)
        return None

    async def _transport_is_ready(self, handoff: SandboxEditHandoff) -> bool:
        if self._transport_ready is None:
            return True
        result = self._transport_ready(handoff)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def _snapshot_is_valid(self, handoff_id: str, snapshot_path: str, expected_sha256: str) -> bool:
        try:
            root = (self.manager.storage_root / ".outbox" / handoff_id)
            self.manager._check_storage_path(root)
            resolved_root = root.resolve(strict=True)
            snapshot = Path(snapshot_path)
            self.manager._check_storage_path(snapshot)
            resolved = snapshot.resolve(strict=True)
            if os.path.commonpath((str(resolved_root), str(resolved))) != str(resolved_root):
                return False
            if not resolved.is_file():
                return False
            digest = hashlib.sha256()
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == expected_sha256
        except (OSError, ValueError, RuntimeError):
            return False

    async def _send_artifact(self, handoff: SandboxEditHandoff, artifact: dict[str, Any]) -> None:
        store = self.store
        if store is None:
            return
        path = validate_relative_path(artifact["relative_path"])
        if not self._snapshot_is_valid(handoff.handoff_id, artifact["snapshot_path"], artifact["sha256"]):
            store.mark_artifact_invalid_snapshot(handoff.handoff_id, path)
            return
        if not store.claim_artifact_send(handoff.handoff_id, path):
            return
        try:
            if self._coordinator is not None:
                sender = self._coordinator._completion_sender()
                source_message = await self._coordinator._resolve_source_message(handoff)
            else:
                from src.plugin_system.apis import send_api as sender

                source_message = None
            receipt = await sender.custom_to_stream_receipt(
                message_type="file",
                content=artifact["snapshot_path"],
                stream_id=handoff.stream_id,
                display_message=f"已完成文件：{Path(path).name}",
                typing=False,
                reply_message=source_message,
            )
        except asyncio.CancelledError:
            store.mark_artifact_unknown(handoff.handoff_id, path, detail_code="shutdown_in_flight")
            raise
        except Exception as exc:
            store.mark_artifact_unknown(handoff.handoff_id, path, detail_code=type(exc).__name__)
            return
        status = getattr(receipt, "status", None)
        receipt_stream = getattr(receipt, "stream_id", None)
        message_id = getattr(receipt, "message_id", None)
        if (
            status is SendStatus.DELIVERED
            and receipt_stream == handoff.stream_id
            and message_id
        ):
            store.mark_artifact_delivered(handoff.handoff_id, path, str(message_id))
            return
        if self._is_proven_pre_send_rejection(receipt):
            detail = getattr(receipt, "detail", "")
            code = str(detail) if detail else str(getattr(status, "value", status) or "pre_send_rejection")
            store.mark_artifact_retryable(handoff.handoff_id, path, detail_code=code)
            return
        # Bool results, timeouts, adapter failures, missing IDs, and arbitrary
        # FAILED receipts do not prove that the platform did not receive it.
        store.mark_artifact_unknown(
            handoff.handoff_id,
            path,
            detail_code=str(getattr(status, "value", status) or "invalid_receipt"),
        )

    @staticmethod
    def _is_proven_pre_send_rejection(receipt: Any) -> bool:
        status = getattr(receipt, "status", None)
        if status is SendStatus.STALE_LEASE:
            return True
        detail = str(getattr(receipt, "detail", "") or "")
        if status is SendStatus.SUPPRESSED:
            return detail == "reply_policy"
        return status is SendStatus.FAILED and detail in _PROVEN_PRE_SEND_FAILURES

    async def run_outbox_pump(
        self,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        """Publish only committed snapshots, waiting for the platform route."""

        if self.store is None:
            return
        self._publisher_task = asyncio.current_task()
        self._publisher_started = True
        try:
            while self._accepting:
                try:
                    artifacts = self.store.pending_artifacts()
                except Exception as exc:
                    logger.error("sandbox outbox scan failed: %s", type(exc).__name__)
                    await asyncio.sleep(max(0.05, float(poll_interval_seconds)))
                    continue
                for artifact in artifacts:
                    if not self._accepting:
                        break
                    try:
                        handoff = self.store.get_handoff(artifact["handoff_id"])
                        if handoff is None:
                            continue
                        allowed = await self._is_authorized(handoff)
                        if not self._accepting:
                            break
                        if not allowed:
                            self.store.block_artifact_unauthorized(handoff.handoff_id, artifact["relative_path"])
                            continue
                        if artifact["status"] == "BLOCKED_AUTHORIZATION":
                            self.store.unblock_authorized_artifact(handoff.handoff_id, artifact["relative_path"])
                        if not await self._transport_is_ready(handoff):
                            if self._accepting:
                                self.store.defer_artifact_until_transport_ready(
                                    handoff.handoff_id,
                                    artifact["relative_path"],
                                )
                            continue
                        if not self._accepting:
                            break
                        await self._send_artifact(handoff, artifact)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error("sandbox outbox artifact deferred: %s", type(exc).__name__)
                await asyncio.sleep(max(0.05, float(poll_interval_seconds)))
        except asyncio.CancelledError:
            raise
        finally:
            self._publisher_started = False
            self._publisher_task = None

    async def _publish_committed(self, handoff: SandboxEditHandoff, _paths: tuple[str, ...]) -> bool:
        """Use the manager journal as the only source for outbox postimages."""

        store = self.store
        if store is None:
            return False
        record = self.manager.transaction_record(handoff.handoff_id)
        if not isinstance(record, dict):
            raise SandboxTaskStoreError("committed transaction journal is unavailable")
        store.record_commit(handoff.handoff_id, record)
        if not record.get("outputs"):
            return True
        while self._accepting:
            task = store.get_task(handoff.handoff_id)
            if task is None:
                return False
            statuses = [artifact["status"] for artifact in task["artifacts"]]
            if statuses and all(status == "DELIVERED" for status in statuses):
                return True
            if any(
                status in {
                    "DELIVERY_UNKNOWN",
                    "INVALID_SNAPSHOT",
                    "BLOCKED_AUTHORIZATION",
                    "RETRY_EXHAUSTED",
                }
                for status in statuses
            ):
                return False
            if not self._publisher_started:
                return False
            await asyncio.sleep(0.1)
        return False

    async def wait_for_tasks(self) -> None:
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def retry_artifact(
        self,
        handoff_id: str,
        relative_path: str,
        *,
        allow_possible_duplicate: bool = False,
    ) -> bool:
        """Explicit retry entry point; unknown delivery needs a duplicate-risk override."""

        if self.store is None or not self._accepting:
            return False
        handoff = self.store.get_handoff(handoff_id)
        if handoff is None or not await self._is_authorized(handoff):
            return False
        return self.store.retry_artifact(
            handoff_id,
            relative_path,
            allow_possible_duplicate=allow_possible_duplicate,
        )

    def status(self, handoff_id: str) -> Optional[dict[str, Any]]:
        """Return persisted status without exposing any mutable target paths."""

        return None if self.store is None else self.store.get_task(handoff_id)

    async def shutdown(self, *, drain_timeout_seconds: float = 10.0) -> None:
        """Close admission, cancel/drain tracked work, then release ownership."""

        self._accepting = False
        publisher = self._publisher_task
        if publisher is not None and not publisher.done():
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        tasks = tuple(self._tasks)
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(0.1, float(drain_timeout_seconds)),
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        if self.store is not None:
            try:
                self.store.release_owner()
            except Exception as exc:
                logger.error("sandbox task store release failed: %s", type(exc).__name__)


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
