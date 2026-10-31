"""SnowLuma active history recovery integration.

The shared SQLite engine is opened only after a verified SnowLuma account is
observed. Importing this module never creates adapter state or reads config.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ncnk_message.offline_history import (
    HistoryPage,
    HistoryPageError,
    HistoryTarget,
    OfflineHistoryStore,
    OfflineHistorySynchronizer,
    PendingHistoryMessage,
    ScanPlan,
    build_delivery_metadata,
    build_history_run_metadata,
)
from ncnk_message.offline_recovery import build_recovery_metadata
from ncnk_message.qq_history_identity import build_qq_private_identity


BACKEND = "snowluma"
OFFLINE_HISTORY_SOURCE_INSTANCE = uuid4().hex
_ADAPTER_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_PATH = _ADAPTER_ROOT / "data" / "offline_history.sqlite3"


def _logger():
    from src.logger import logger

    return logger


def _valid_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text or not text.isdecimal() or int(text) <= 0:
        return None
    return text


def _snow_message_id(value: Any) -> str | None:
    """SnowLuma message IDs are signed 32-bit hashes; route IDs remain positive."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    try:
        number = int(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if number == 0 or not -(2**31) <= number < 2**31:
        return None
    return str(number)


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    if result >= 100_000_000_000:
        result /= 1000.0
    return result if math.isfinite(result) and result > 0 else None


def core_connection_identity(router: Any, platform: str) -> tuple[bool, tuple[Any, ...] | None]:
    """Observe the real Router client/socket identity, including reconnects."""

    clients = getattr(router, "clients", None)
    client = clients.get(platform) if isinstance(clients, Mapping) else None
    if client is None:
        return False, None
    connection = getattr(client, "connection", None)
    is_connected = getattr(client, "is_connected", None)
    try:
        connected = bool(is_connected()) if callable(is_connected) else False
    except Exception:
        connected = False
    connection_uuid = getattr(connection, "_connection_uuid", None)
    network_driver = getattr(connection, "network_driver", None)
    active = getattr(network_driver, "active_connections", None)
    socket = active.get(connection_uuid) if isinstance(active, Mapping) and connection_uuid else None
    connected = connected and (socket is not None or active is None)
    generation = getattr(connection, "_connected_count", None)
    return connected, (id(client), id(connection), connection_uuid, id(socket) if socket is not None else None, generation)


def _extract_messages(response: Mapping[str, Any]) -> list[Any]:
    data = response.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        messages = data.get("messages")
        if isinstance(messages, list):
            return messages
    raise HistoryPageError("SnowLuma history response data was malformed")


def normalize_snowluma_messages(
    messages: Any,
    *,
    target: HistoryTarget,
    account_id: str,
    page_size: int,
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Validate and normalize one SnowLuma OneBot history response page."""

    if target.kind != "private":
        raise HistoryPageError("SnowLuma active offline history supports private targets only")
    if not isinstance(messages, list) or not messages or len(messages) > page_size:
        raise HistoryPageError("SnowLuma history response was empty or malformed")
    normalized: list[tuple[float, int, str, dict[str, Any]]] = []
    for index, source in enumerate(messages):
        if not isinstance(source, Mapping):
            raise HistoryPageError("SnowLuma history page contained a malformed message")
        raw = dict(source)
        cursor_id = _snow_message_id(raw.get("message_id"))
        message_id = cursor_id or ""
        timestamp = _timestamp(raw.get("time"))
        if not message_id or cursor_id is None or timestamp is None:
            raise HistoryPageError("SnowLuma history message lacked a numeric message_id or time")
        self_id = raw.get("self_id")
        if self_id is not None and _valid_id(self_id) != account_id:
            raise HistoryPageError("SnowLuma history response account did not match the verified login")
        message_type = str(raw.get("message_type") or "").strip().lower()
        expected_type = "private"
        if message_type and message_type != expected_type:
            raise HistoryPageError("SnowLuma history response message type did not match the target")
        raw["post_type"] = "message"
        raw["message_type"] = expected_type
        raw["self_id"] = account_id
        raw["message_id"] = message_id
        raw["time"] = timestamp
        sender = raw.get("sender")
        if not isinstance(sender, Mapping):
            sender = {}
        else:
            sender = dict(sender)
        sender_id = _valid_id(sender.get("user_id") or raw.get("user_id"))
        if sender_id is None:
            raise HistoryPageError("SnowLuma history message lacked a sender identity")
        sender["user_id"] = int(sender_id)
        raw["sender"] = sender
        raw["sub_type"] = "friend"
        if raw.get("group_id") is not None:
            raise HistoryPageError("SnowLuma friend history unexpectedly contained a group route")
        if sender_id == account_id:
            target_id = _valid_id(raw.get("target_id"))
            if target_id != target.session_id:
                raise HistoryPageError("SnowLuma outgoing friend history target did not match the requested friend")
            raw["target_id"] = int(target.session_id)
            raw["_offline_history_outgoing"] = True
            raw["_offline_history_external_self"] = True
        elif sender_id != target.session_id:
            raise HistoryPageError("SnowLuma friend history sender did not match the requested friend")
        normalized.append((timestamp, index, cursor_id, raw))
    # Private history is requested newest-to-oldest. Keep ordering among
    # equal-second messages exactly as returned by SnowLuma.
    normalized.sort(key=lambda item: (item[0], item[1]))
    next_cursor = normalized[0][2]
    return tuple(item[3] for item in normalized), next_cursor


class SnowLumaOfflineHistory:
    """Connection monitor, API normalizer, Core receipt bridge, and live identity hook."""

    def __init__(
        self,
        *,
        platform: str,
        settings: Any,
        state_path: str | Path | None = None,
        source_instance: str = OFFLINE_HISTORY_SOURCE_INSTANCE,
        store: OfflineHistoryStore | None = None,
        call_action: Callable[..., Any] | None = None,
        send_custom: Callable[..., Any] | None = None,
        emit_message: Callable[..., Any] | None = None,
        admission_check: Callable[..., Any] | None = None,
    ):
        self.platform = str(platform).strip()
        self.settings = settings
        self.source_instance = str(source_instance)
        self.store = store or OfflineHistoryStore(state_path or _DEFAULT_STATE_PATH)
        self._generation_cutoff = time.time()
        self._active_cutoff = self._generation_cutoff
        self.synchronizer = OfflineHistorySynchronizer(
            self.store,
            backend=BACKEND,
            platform=self.platform,
            page_size=max(1, int(getattr(settings, "page_size", 50))),
            max_pages=max(1, int(getattr(settings, "max_pages", 200))),
            retry_delay=max(0.1, float(getattr(settings, "retry_delay_sec", 15.0))),
            marker_refresh_sec=30.0,
            clock=lambda: self._generation_cutoff,
        )
        self._call_action = call_action
        self._send_custom = send_custom
        self._emit_message = emit_message
        self._admission_check = admission_check
        self._receipt_expectations: dict[str, dict[str, str]] = {}
        self._delivery_waiters: dict[str, asyncio.Future[str]] = {}
        self._early_delivery_receipts: dict[str, str] = {}
        self._run_waiters: dict[tuple[str, str, str], asyncio.Future[str]] = {}
        self._run_expectations: dict[tuple[str, str, str], dict[str, str]] = {}
        self._active_run: asyncio.Task[Any] | None = None
        self._rerun_event = asyncio.Event()
        self._last_generation: tuple[Any, ...] | None = None
        self._monitor_task: asyncio.Task[Any] | None = None
        self._prepared_accounts: set[tuple[str, str, str]] = set()
        self._epoch_started_at = time.time()
        self._stopping = False
        self._action_lock = asyncio.Lock()
        self._last_history_action_at = 0.0
        self._current_run_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "enabled", True))

    def bind(
        self,
        *,
        call_action: Callable[..., Any],
        send_custom: Callable[..., Any],
        emit_message: Callable[..., Any],
        admission_check: Callable[..., Any],
    ) -> None:
        self._call_action = call_action
        self._send_custom = send_custom
        self._emit_message = emit_message
        self._admission_check = admission_check

    def register_core_handlers(self, router: Any) -> None:
        router.register_custom_message_handler("offline_history_receipt", self.handle_delivery_receipt)
        router.register_custom_message_handler("offline_history_run_receipt", self.handle_run_receipt)

    def handle_delivery_receipt(self, raw_data: Any) -> None:
        payload = raw_data.get("content") if isinstance(raw_data, Mapping) else None
        if not isinstance(payload, Mapping) or type(payload.get("version")) is not int or payload.get("version") != 1:
            return
        key = str(payload.get("delivery_key") or "").strip()
        expected = self._receipt_expectations.get(key)
        if expected is None:
            return
        required = ("backend", "account_id", "platform", "session_kind", "session_id", "message_id", "delivery_key", "receipt_token")
        if any(str(payload.get(field) or "") != expected[field] for field in required):
            return
        outcome = str(payload.get("outcome") or "").strip()
        if outcome not in {"accepted", "complete", "filtered"}:
            return
        waiter = self._delivery_waiters.get(key)
        if waiter is not None and not waiter.done():
            waiter.set_result(outcome)
        else:
            self._early_delivery_receipts[key] = outcome

    def handle_run_receipt(self, raw_data: Any) -> None:
        payload = raw_data.get("content") if isinstance(raw_data, Mapping) else None
        if not isinstance(payload, Mapping) or type(payload.get("version")) is not int or payload.get("version") != 1:
            return
        run_id = str(payload.get("run_id") or "").strip()
        run_token = str(payload.get("run_token") or "").strip()
        phase = str(payload.get("phase") or "").strip()
        expected_key = (run_id, run_token, phase)
        waiter = self._run_waiters.get(expected_key)
        if waiter is None:
            return
        expected = self._run_expectations.get(expected_key)
        if not isinstance(expected, dict):
            return
        required = ("backend", "account_id", "platform", "run_id", "run_token", "phase")
        if any(str(payload.get(field) or "") != expected[field] for field in required):
            return
        status = str(payload.get("status") or "").strip()
        if status not in {"ok", "error"}:
            return
        if not waiter.done():
            waiter.set_result(status)

    async def prepare_account(self, account_id: str) -> None:
        account_id = str(account_id).strip()
        if _valid_id(account_id) is None:
            raise ValueError("SnowLuma history requires a verified numeric account ID")
        scope = (BACKEND, account_id, self.platform)
        if scope not in self._prepared_accounts:
            # Persist first-enable baseline before waiting for Core connectivity.
            self.store.initialize_scope(BACKEND, account_id, self.platform)
            self._prepared_accounts.add(scope)

    async def decorate_live_message(
        self,
        raw: Mapping[str, Any],
        context: dict[str, Any],
        *,
        account_id: str,
    ) -> dict[str, Any]:
        """Add the same stable identity to ordinary live ingress and save its anchor."""

        normalized_account = str(account_id).strip()
        if not normalized_account:
            raise HistoryPageError("SnowLuma live message arrived before login verification")
        self_id = raw.get("self_id")
        if self_id is not None and _valid_id(self_id) != normalized_account:
            raise HistoryPageError("SnowLuma live message account did not match verified login")
        target = self.target_from_live(raw)
        message_id = _snow_message_id(raw.get("message_id"))
        original_at = _timestamp(context.get("metadata", {}).get("original_timestamp"))
        if message_id is None or original_at is None:
            raise HistoryPageError("SnowLuma live message lacked a stable ID or original timestamp")
        metadata = context.setdefault("metadata", {})
        metadata["source_instance"] = self.source_instance
        qq_identity = build_qq_private_identity(
            backend=BACKEND,
            account_id=normalized_account,
            platform=self.platform,
            session_kind=target.kind,
            session_id=target.session_id,
            raw=raw,
            original_timestamp=original_at,
        )
        metadata["delivery"] = build_delivery_metadata(
            backend=BACKEND,
            account_id=normalized_account,
            platform=self.platform,
            target=target,
            message_id=message_id,
            recovered=False,
            qq_identity=qq_identity,
        )
        # Keep the live identity on group ingress, but do not persist or
        # schedule group recovery state.
        if self.enabled and target.kind == "private":
            await self.prepare_account(normalized_account)
            self.store.record_live_seen(
                BACKEND,
                normalized_account,
                self.platform,
                target,
                message_id,
                original_at,
            )
            self._rerun_event.set()
        return context

    @staticmethod
    def target_from_live(raw: Mapping[str, Any]) -> HistoryTarget:
        kind = str(raw.get("message_type") or "").strip().lower()
        sender = raw.get("sender") if isinstance(raw.get("sender"), Mapping) else {}
        if kind == "group":
            session_id = _valid_id(raw.get("group_id"))
        elif kind == "private" and str(raw.get("sub_type") or "") == "friend":
            session_id = _valid_id(sender.get("user_id") or raw.get("user_id"))
        else:
            raise HistoryPageError("SnowLuma message route is unsupported for history identity")
        if session_id is None:
            raise HistoryPageError("SnowLuma message route identity is malformed")
        return HistoryTarget(kind, session_id)

    async def _action(self, action: str, params: dict[str, Any], connection: Any) -> dict[str, Any]:
        if self._call_action is None:
            raise RuntimeError("SnowLuma offline history action callback is not bound")
        async with self._action_lock:
            delay = 0.1 - (time.monotonic() - self._last_history_action_at)
            if delay > 0:
                await asyncio.sleep(delay)
            response = await self._call_action(action, params, expected_connection=connection)
            self._last_history_action_at = time.monotonic()
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            raise HistoryPageError(f"SnowLuma {action} did not return an authoritative success response")
        retcode = response.get("retcode")
        if isinstance(retcode, int) and retcode not in {0, 1}:
            raise HistoryPageError(f"SnowLuma {action} returned an unsuccessful action code")
        return dict(response)

    async def discover_targets(self, account_id: str, connection: Any) -> tuple[HistoryTarget, ...]:
        from src.config import global_config

        chat = global_config.chat
        known_private = {
            target.session_id
            for target in self.store.known_targets(BACKEND, account_id, self.platform)
            if target.kind == "private"
        }
        if chat.enable_chat_list_filter and chat.private_list_type == "whitelist":
            private = {_valid_id(value) for value in chat.private_list}
            private.discard(None)
        elif not chat.enable_chat_list_filter or chat.private_list_type == "blacklist":
            response = await self._action("get_friend_list", {}, connection)
            rows = response.get("data")
            if not isinstance(rows, list):
                raise HistoryPageError("SnowLuma friend list response was malformed")
            private = {_valid_id(row.get("user_id")) for row in rows if isinstance(row, Mapping)}
            private.discard(None)
            private.update(known_private)
            if chat.enable_chat_list_filter:
                private.difference_update(str(value) for value in chat.private_list)
        else:
            raise HistoryPageError("SnowLuma private admission policy is invalid")
        private.difference_update(str(value) for value in chat.ban_user_id)

        return tuple(HistoryTarget("private", item) for item in sorted(private))

    async def fetch_page(
        self,
        *,
        target: HistoryTarget,
        cursor: str | None,
        page_size: int,
        connection: Any,
        account_id: str,
    ) -> HistoryPage:
        if target.kind != "private":
            raise HistoryPageError("SnowLuma active offline history supports private targets only")
        params: dict[str, Any] = {
            "user_id": int(target.session_id),
            "count": page_size,
            "reverse_order": True,
        }
        if cursor:
            params["message_id"] = int(cursor)
        response = await self._action("get_friend_msg_history", params, connection)
        page_messages = _extract_messages(response)
        if not page_messages:
            # Snow private history directly reports the server response; unlike
            # the group API it does not silently substitute a local cached page.
            return HistoryPage(messages=(), next_cursor=None, end_of_history=True, authoritative=True)
        normalized, next_cursor = normalize_snowluma_messages(
            page_messages,
            target=target,
            account_id=account_id,
            page_size=page_size,
        )
        return HistoryPage(
            messages=normalized,
            next_cursor=next_cursor,
            end_of_history=len(normalized) < page_size,
            authoritative=True,
        )

    def plan_target(
        self,
        target: HistoryTarget,
        checkpoint_id: str | None,
        checkpoint_at: float | None,
        checkpoint_ids: frozenset[str],
        live_anchor_id: str | None,
        _live_anchor_at: float | None,
        baseline_at: float,
    ) -> ScanPlan:
        if target.kind != "private":
            raise HistoryPageError("SnowLuma active offline history supports private targets only")
        return ScanPlan(
            direction="backward",
            initial_cursor=None,
            boundary_id=checkpoint_id,
            boundary_ids=checkpoint_ids,
            boundary_at=checkpoint_at if checkpoint_at is not None else baseline_at,
        )

    async def send_hold(
        self,
        *,
        target: HistoryTarget,
        progress_id: str,
        cutoff: float,
        active: bool,
        run_id: str,
        route_total: int,
        account_id: str,
        connection_epoch_started_at: float,
    ) -> None:
        if target.kind != "private":
            raise HistoryPageError("SnowLuma active offline recovery cannot hold group targets")
        if self._send_custom is None:
            raise RuntimeError("SnowLuma offline history Core callback is not bound")
        history_run = build_history_run_metadata(run_id, cutoff)
        payload: dict[str, Any] = {
            "version": 1,
            "backend": BACKEND,
            "account_id": account_id,
            "platform": self.platform,
            "source_instance": self.source_instance,
            "progress_id": progress_id,
            "active": active,
            "connection_epoch_started_at": connection_epoch_started_at,
            "received_at": time.time(),
            "history_run": history_run,
            "route_total": route_total,
            "group_id": None,
            "user_id": int(target.session_id),
        }
        await self._send_custom(
            platform=self.platform,
            message_type_name="offline_recovery_progress",
            message=payload,
        )

    async def send_run(
        self,
        *,
        run_id: str,
        run_token: str,
        cutoff: float,
        targets: tuple[HistoryTarget, ...],
        phase: str,
        account_id: str,
        account_is_current: Callable[[], bool],
    ) -> None:
        if any(target.kind != "private" for target in targets):
            raise HistoryPageError("SnowLuma active offline history runs may contain private targets only")
        if self._send_custom is None:
            raise RuntimeError("SnowLuma offline history Core callback is not bound")
        key = (run_id, run_token, phase)
        payload = {
            "version": 1,
            "backend": BACKEND,
            "account_id": account_id,
            "platform": self.platform,
            "source_instance": self.source_instance,
            "run_id": run_id,
            "run_token": run_token,
            "cutoff_at": cutoff,
            "phase": phase,
            "targets": [
                {"session_kind": target.kind, "session_id": target.session_id}
                for target in targets
            ],
        }
        timeout = max(1.0, float(getattr(self.settings, "receipt_timeout_sec", 8.0)))
        retry_delay = max(0.1, float(getattr(self.settings, "retry_delay_sec", 15.0)))
        while account_is_current():
            waiter = asyncio.get_running_loop().create_future()
            self._run_expectations[key] = {
                "backend": BACKEND,
                "account_id": account_id,
                "platform": self.platform,
                "run_id": run_id,
                "run_token": run_token,
                "phase": phase,
            }
            self._run_waiters[key] = waiter
            try:
                try:
                    # The send return value is transport evidence only. Advance
                    # after the exact durable Core receipt arrives.
                    await self._send_custom(
                        platform=self.platform,
                        message_type_name="offline_history_run",
                        message=payload,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                try:
                    status = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
                except asyncio.TimeoutError:
                    status = "timeout"
                if status == "ok":
                    return
            finally:
                if self._run_waiters.get(key) is waiter:
                    self._run_waiters.pop(key, None)
                self._run_expectations.pop(key, None)
            if not account_is_current():
                break
            await asyncio.sleep(retry_delay)
        raise asyncio.CancelledError("SnowLuma or Core connection generation changed during run receipt wait")

    def _remember_pending_receipts(self, account_id: str) -> None:
        for target in self.store.known_targets(BACKEND, account_id, self.platform):
            if target.kind != "private":
                continue
            for item in self.store.pending_for_target(BACKEND, account_id, self.platform, target):
                self._receipt_expectations[item.delivery_key] = self._expected_delivery(
                    item, account_id, self.platform
                )

    @staticmethod
    def _expected_delivery(item: PendingHistoryMessage, account_id: str, platform: str) -> dict[str, str]:
        return {
            "backend": BACKEND,
            "account_id": account_id,
            "platform": platform,
            "session_kind": item.target.kind,
            "session_id": item.target.session_id,
            "message_id": item.message_id,
            "delivery_key": item.delivery_key,
            "receipt_token": item.receipt_token,
        }

    async def emit_history_item(
        self,
        item: PendingHistoryMessage,
        *,
        run_id: str,
        cutoff: float,
        account_id: str,
        connection: Any,
        connection_epoch_started_at: float,
        account_is_current: Callable[[], bool],
    ) -> bool:
        if not account_is_current():
            return False
        if item.target.kind != "private":
            return False
        raw = dict(item.raw_message)
        expected_self = raw.get("self_id")
        if expected_self is not None and _valid_id(expected_self) != account_id:
            raise HistoryPageError("staged SnowLuma row account did not match the verified login")
        raw.pop("_offline_history_skip", None)
        raw.pop("_offline_history_outgoing", None)
        target = item.target
        qq_identity = build_qq_private_identity(
            backend=BACKEND,
            account_id=account_id,
            platform=self.platform,
            session_kind=target.kind,
            session_id=target.session_id,
            raw=raw,
            original_timestamp=item.original_timestamp,
        )
        delivery = build_delivery_metadata(
            backend=BACKEND,
            account_id=account_id,
            platform=self.platform,
            target=target,
            message_id=item.message_id,
            recovered=True,
            receipt_token=item.receipt_token,
            qq_identity=qq_identity,
        )
        metadata = build_recovery_metadata(raw, connection_epoch_started_at, time.time(), uuid4().hex)
        metadata["source_instance"] = self.source_instance
        metadata["delivery"] = delivery
        metadata["history_run"] = build_history_run_metadata(item.run_id or run_id, item.cutoff_at or cutoff)
        context = {
            "metadata": metadata,
            "platform": self.platform,
            "group_id": None,
            "user_id": target.session_id,
            "connection": connection,
            "verified_account_id": account_id,
        }
        expected = {
            "backend": BACKEND,
            "account_id": account_id,
            "platform": self.platform,
            "session_kind": target.kind,
            "session_id": target.session_id,
            "message_id": item.message_id,
            "delivery_key": item.delivery_key,
            "receipt_token": item.receipt_token,
        }
        self._receipt_expectations[item.delivery_key] = expected
        early = self._early_delivery_receipts.pop(item.delivery_key, None)
        if early in {"accepted", "complete", "filtered"}:
            return True
        waiter = asyncio.get_running_loop().create_future()
        self._delivery_waiters[item.delivery_key] = waiter
        try:
            if self._emit_message is None:
                raise RuntimeError("SnowLuma history ingress callback is not bound")
            status = await self._emit_message(raw, context)
            if status == "filtered":
                return True
            if status != "sent":
                return False
            timeout = max(1.0, float(getattr(self.settings, "receipt_timeout_sec", 8.0)))
            try:
                outcome = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
            except asyncio.TimeoutError:
                return False
            return outcome in {"accepted", "complete", "filtered"}
        finally:
            if self._delivery_waiters.get(item.delivery_key) is waiter:
                self._delivery_waiters.pop(item.delivery_key, None)

    async def run_generation(
        self,
        *,
        account_id: str,
        connection: Any,
        connection_epoch_started_at: float,
        account_is_current: Callable[[], bool],
    ) -> dict[str, Any]:
        await self.prepare_account(account_id)
        self._remember_pending_receipts(account_id)
        targets = tuple(
            target
            for target in await self.discover_targets(account_id, connection)
            if target.kind == "private"
        )
        for target in targets:
            self.store.register_target(BACKEND, account_id, self.platform, target)
        self._active_cutoff = self._generation_cutoff
        self.store.initialize_scope(
            BACKEND, account_id, self.platform, now=self._active_cutoff
        )

        async def fetch(target: HistoryTarget, cursor: str | None, page_size: int, direction: str) -> HistoryPage:
            if direction != "backward":
                raise HistoryPageError(f"SnowLuma {target.kind} history does not support {direction} paging")
            if not account_is_current():
                raise asyncio.CancelledError("SnowLuma/Core connection generation changed")
            return await self.fetch_page(
                target=target,
                cursor=cursor,
                page_size=page_size,
                connection=connection,
                account_id=account_id,
            )

        async def hold(target: HistoryTarget, progress_id: str, cutoff: float, active: bool, run_id: str, route_total: int) -> None:
            await self.send_hold(
                target=target,
                progress_id=progress_id,
                cutoff=cutoff,
                active=active,
                run_id=run_id,
                route_total=route_total,
                account_id=account_id,
                connection_epoch_started_at=connection_epoch_started_at,
            )

        async def run_marker(run_id: str, run_token: str, cutoff: float, run_targets: tuple[HistoryTarget, ...], phase: str) -> None:
            if phase == "begin":
                self._current_run_id = run_id
                self.store.initialize_scope(
                    BACKEND, account_id, self.platform, now=cutoff
                )
            await self.send_run(
                run_id=run_id,
                run_token=run_token,
                cutoff=cutoff,
                targets=run_targets,
                phase=phase,
                account_id=account_id,
                account_is_current=account_is_current,
            )

        async def emit(item: PendingHistoryMessage, run_id: str, cutoff: float) -> bool:
            if not account_is_current():
                return False
            return await self.emit_history_item(
                item,
                run_id=run_id,
                cutoff=cutoff,
                account_id=account_id,
                connection=connection,
                connection_epoch_started_at=connection_epoch_started_at,
                account_is_current=account_is_current,
            )

        async def retry_wait() -> None:
            await asyncio.sleep(max(0.1, float(getattr(self.settings, "retry_delay_sec", 15.0))))

        result = await self.synchronizer.run(
            account_id=account_id,
            targets=targets,
            fetch_page=fetch,
            send_hold=hold,
            emit=emit,
            plan_target=self.plan_target,
            send_run=run_marker,
            account_is_current=account_is_current,
            wait_retry=retry_wait,
            on_session_error=lambda target, exc: _logger().warning(
                "SnowLuma offline history session incomplete kind={} session_id={} error={}",
                target.kind,
                target.session_id,
                str(exc)[:300],
            ),
        )
        self._current_run_id = None
        return result

    async def _recover_until_complete(
        self,
        *,
        account_id: str,
        connection: Any,
        platform_identity: tuple[Any, ...],
        core_identity: tuple[Any, ...],
        connection_epoch_started_at: float,
        get_platform_state: Callable[[], dict[str, Any]],
        router: Any,
    ) -> None:
        retry_delay = max(0.1, float(getattr(self.settings, "retry_delay_sec", 15.0)))

        def current() -> bool:
            platform_state = get_platform_state()
            core_connected, observed_core_identity = core_connection_identity(router, self.platform)
            return (
                platform_state.get("connected") is True
                and str(platform_state.get("account_id") or "") == account_id
                and tuple(platform_state.get("identity") or ()) == platform_identity
                and core_connected
                and observed_core_identity == core_identity
            )

        while current() and not self._stopping:
            self._rerun_event.clear()
            try:
                result = await self.run_generation(
                    account_id=account_id,
                    connection=connection,
                    connection_epoch_started_at=connection_epoch_started_at,
                    account_is_current=current,
                )
                failures = result.get("failed_sessions")
                if not failures:
                    if self._rerun_event.is_set():
                        continue
                    try:
                        await asyncio.wait_for(
                            self._rerun_event.wait(),
                            timeout=max(30.0, retry_delay * 2.0),
                        )
                    except asyncio.TimeoutError:
                        return
                    continue
                _logger().warning(
                    "SnowLuma offline history retry scheduled failed_sessions={} account_id={}",
                    len(failures),
                    account_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger().warning("SnowLuma offline history run failed error={}", str(exc)[:300])
            if current():
                await asyncio.sleep(retry_delay)

    async def monitor(
        self,
        *,
        get_platform_state: Callable[[], dict[str, Any]],
        router: Any,
        on_core_generation: Callable[[str], Any] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        """Start once per platform/Core socket generation and cancel stale runs."""

        self._stopping = False
        self._monitor_task = asyncio.current_task()
        try:
            while not self._stopping:
                state = get_platform_state()
                account_id = str(state.get("account_id") or "").strip()
                platform_connected = state.get("connected") is True and bool(account_id)
                if platform_connected and self.enabled:
                    try:
                        await self.prepare_account(account_id)
                    except Exception as exc:
                        _logger().warning("SnowLuma offline history baseline persistence failed error={}", str(exc)[:240])
                core_connected, core_identity = core_connection_identity(router, self.platform)
                identity = tuple(state.get("identity") or ())
                generation = (account_id, identity, core_identity) if platform_connected and core_connected and core_identity else None
                if generation != self._last_generation:
                    if self._active_run is not None and not self._active_run.done():
                        self._active_run.cancel()
                        await asyncio.gather(self._active_run, return_exceptions=True)
                    self._active_run = None
                    self._last_generation = generation
                    if generation is not None and self.enabled:
                        self._epoch_started_at = float(state.get("connection_epoch_started_at") or time.time())
                        self._generation_cutoff = time.time()
                        self._active_cutoff = self._generation_cutoff
                        if on_core_generation is not None:
                            outcome = on_core_generation(account_id)
                            if asyncio.iscoroutine(outcome):
                                await outcome
                        connection = state.get("connection")
                        self._active_run = asyncio.create_task(
                            self._recover_until_complete(
                                account_id=account_id,
                                connection=connection,
                                platform_identity=identity,
                                core_identity=core_identity,
                                connection_epoch_started_at=self._epoch_started_at,
                                get_platform_state=get_platform_state,
                                router=router,
                            ),
                            name=f"offline-history-snowluma-{account_id}",
                        )
                await asyncio.sleep(max(0.1, poll_interval))
        finally:
            self._stopping = True
            if self._active_run is not None and not self._active_run.done():
                self._active_run.cancel()
                await asyncio.gather(self._active_run, return_exceptions=True)
            self._monitor_task = None

    async def stop(self) -> None:
        self._stopping = True
        if self._active_run is not None and not self._active_run.done():
            self._active_run.cancel()
            await asyncio.gather(self._active_run, return_exceptions=True)
        monitor_task = self._monitor_task
        if monitor_task is not None and monitor_task is not asyncio.current_task() and not monitor_task.done():
            await asyncio.gather(monitor_task, return_exceptions=True)
        self.store.close()


_runtime: SnowLumaOfflineHistory | None = None


def get_offline_history_runtime(*, source_instance: str | None = None) -> SnowLumaOfflineHistory:
    """Lazily read configuration and construct the runtime without opening SQLite."""

    global _runtime
    if _runtime is None:
        from src.config import global_config

        _runtime = SnowLumaOfflineHistory(
            platform=global_config.nachobot.platform_name,
            settings=global_config.offline_recovery,
            source_instance=source_instance or OFFLINE_HISTORY_SOURCE_INSTANCE,
        )
    return _runtime


__all__ = [
    "BACKEND",
    "OFFLINE_HISTORY_SOURCE_INSTANCE",
    "SnowLumaOfflineHistory",
    "core_connection_identity",
    "get_offline_history_runtime",
    "normalize_snowluma_messages",
]
