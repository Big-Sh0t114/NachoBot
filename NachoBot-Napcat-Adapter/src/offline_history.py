"""NapCat active history recovery integration.

The shared SQLite engine is opened only after a verified NapCat account is
observed. Importing this module never creates adapter state or reads config.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import json
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
    scoped_delivery_key,
)
from ncnk_message.offline_recovery import build_recovery_metadata
from ncnk_message.qq_history_identity import build_qq_private_identity


BACKEND = "napcat"
OFFLINE_HISTORY_SOURCE_INSTANCE = uuid4().hex
_ADAPTER_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_PATH = _ADAPTER_ROOT / "data" / "offline_history.sqlite3"
_RECENT_NAVIGATION_CACHE_TTL_SEC = 300.0
_IDENTITY_SYNC_BATCH_SIZE = 100


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


def _message_id_text(value: Any) -> str | None:
    """Keep platform IDs exact; floating-point IDs have already lost precision."""

    if value is None or isinstance(value, (bool, float)):
        return None
    if not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class _RecentContactAnchor:
    target_id: str
    raw_message_id: str
    timestamp: float
    message_id: str | None = None
    message_seq: str | None = None


@dataclass(frozen=True, slots=True)
class _ScopedLiveAnchor:
    account_id: str
    generation_scope: tuple[Any, ...]
    anchor: _RecentContactAnchor


@dataclass(slots=True)
class _RecentNavigationCache:
    """Keep only validated navigation values within one socket generation."""

    ttl_sec: float = _RECENT_NAVIGATION_CACHE_TTL_SEC
    clock: Callable[[], float] = time.monotonic
    _scope: tuple[Any, ...] | None = None
    _loaded_at: float | None = None
    _admitted_target_ids: frozenset[str] = frozenset()
    _anchors: dict[str, _RecentContactAnchor] = field(default_factory=dict)
    _invalid_targets: dict[str, str] = field(default_factory=dict)

    def get(
        self, *, scope: tuple[Any, ...], admitted_target_ids: frozenset[str]
    ) -> tuple[dict[str, _RecentContactAnchor], dict[str, str]] | None:
        if (
            self._loaded_at is None
            or self._scope != scope
            or not admitted_target_ids.issubset(self._admitted_target_ids)
        ):
            return None
        age = self.clock() - self._loaded_at
        if not math.isfinite(age) or age < 0 or age >= self.ttl_sec:
            self.invalidate()
            return None
        return (
            {key: value for key, value in self._anchors.items() if key in admitted_target_ids},
            {key: value for key, value in self._invalid_targets.items() if key in admitted_target_ids},
        )

    def remember(
        self,
        *,
        scope: tuple[Any, ...],
        admitted_target_ids: frozenset[str],
        anchors: Mapping[str, _RecentContactAnchor],
        invalid_targets: Mapping[str, str],
    ) -> None:
        self._scope = scope
        self._loaded_at = self.clock()
        self._admitted_target_ids = frozenset(admitted_target_ids)
        self._anchors = dict(anchors)
        self._invalid_targets = dict(invalid_targets)

    def invalidate(self) -> None:
        self._scope = None
        self._loaded_at = None
        self._admitted_target_ids = frozenset()
        self._anchors.clear()
        self._invalid_targets.clear()


def _failed_private_target_ids(failures: Any) -> frozenset[str]:
    """Parse only exact private failure keys; malformed reports fail closed."""

    if not isinstance(failures, Mapping):
        raise HistoryPageError("NapCat history failure report was malformed")
    target_ids: set[str] = set()
    for key in failures:
        if not isinstance(key, str):
            raise HistoryPageError("NapCat history failure report contained a malformed target key")
        kind, separator, target_id = key.partition(":")
        if (
            not separator
            or kind != "private"
            or ":" in target_id
            or _valid_id(target_id) != target_id
        ):
            raise HistoryPageError("NapCat history failure report contained a non-private target key")
        target_ids.add(target_id)
    return frozenset(target_ids)


def _private_target_id_set(values: Any, *, label: str) -> frozenset[str]:
    if not isinstance(values, (set, frozenset, tuple, list)):
        raise HistoryPageError(f"NapCat {label} target list was malformed")
    target_ids: set[str] = set()
    for target_id in values:
        if not isinstance(target_id, str) or _valid_id(target_id) != target_id:
            raise HistoryPageError(f"NapCat {label} target list contained a malformed private ID")
        target_ids.add(target_id)
    return frozenset(target_ids)


def _next_retry_delay(current: float, base: float, *, progress: bool) -> float:
    if progress:
        return base
    return min(max(300.0, base), max(base, current) * 2.0)


def _parse_recent_message(value: Any) -> Mapping[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value if isinstance(value, Mapping) else None


def _validate_private_message_scope(
    message: Mapping[str, Any], *, target_id: str, account_id: str
) -> None:
    message_type = str(message.get("message_type") or "").strip().lower()
    if message_type and message_type != "private":
        raise HistoryPageError("NapCat recent message was outside the private route")
    if message.get("group_id") is not None:
        raise HistoryPageError("NapCat recent message unexpectedly contained a group route")
    subtype = str(message.get("sub_type") or "").strip().lower()
    if subtype and subtype != "friend":
        raise HistoryPageError("NapCat recent message used an unsupported private subtype")

    self_id = message.get("self_id")
    if self_id is not None and _valid_id(self_id) != account_id:
        raise HistoryPageError("NapCat recent message account did not match the verified login")
    target = message.get("target_id")
    if target is not None and _valid_id(target) != target_id:
        raise HistoryPageError("NapCat recent message target did not match the requested friend")

    sender = message.get("sender")
    if sender is not None and not isinstance(sender, Mapping):
        raise HistoryPageError("NapCat recent message sender was malformed")
    sender_value = sender.get("user_id") if isinstance(sender, Mapping) else None
    if sender_value is None:
        sender_value = message.get("user_id")
    if sender_value is not None:
        sender_id = _valid_id(sender_value)
        if sender_id is None:
            raise HistoryPageError("NapCat recent message sender ID was malformed")
        if sender_id == account_id:
            if target is not None and _valid_id(target) != target_id:
                raise HistoryPageError("NapCat outgoing recent message target did not match")
        elif sender_id != target_id:
            raise HistoryPageError("NapCat recent message sender did not match the requested friend")


def _parse_recent_contact_anchors(
    rows: Any, *, targets: tuple[HistoryTarget, ...], account_id: str
) -> tuple[dict[str, _RecentContactAnchor], dict[str, str]]:
    if not isinstance(rows, list):
        raise HistoryPageError("NapCat recent contact response was malformed")
    allowed = {target.session_id for target in targets if target.kind == "private"}
    anchors: dict[str, _RecentContactAnchor] = {}
    invalid: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        peer_id = _valid_id(row.get("peerUin"))
        if peer_id not in allowed:
            continue
        chat_type = row.get("chatType")
        if isinstance(chat_type, bool) or str(chat_type).strip() != "1":
            # Group and temporary-contact entries are never private history anchors.
            continue
        try:
            for field in ("self_id", "account_id"):
                value = row.get(field)
                if value is not None and _valid_id(value) != account_id:
                    raise HistoryPageError("NapCat recent contact account did not match the verified login")
            raw_message_id = _valid_id(row.get("msgId"))
            timestamp = _timestamp(row.get("msgTime"))
            if raw_message_id is None or timestamp is None:
                raise HistoryPageError("NapCat recent contact lacked a finite message ID or time")

            latest = _parse_recent_message(row.get("lastestMsg"))
            message_id = None
            message_seq = None
            if latest is not None:
                _validate_private_message_scope(latest, target_id=peer_id, account_id=account_id)
                latest_time = latest.get("time")
                if latest_time is not None and _timestamp(latest_time) != timestamp:
                    raise HistoryPageError("NapCat recent message time did not match its contact snapshot")
                if "message_id" in latest and latest.get("message_id") is not None:
                    message_id = _message_id_text(latest.get("message_id"))
                    if message_id is None:
                        raise HistoryPageError("NapCat recent message ID was malformed")
                if "message_seq" in latest and latest.get("message_seq") is not None:
                    message_seq = _valid_id(latest.get("message_seq"))
                    if message_seq is None:
                        raise HistoryPageError("NapCat recent message sequence was malformed")

            anchor = _RecentContactAnchor(
                target_id=peer_id,
                raw_message_id=raw_message_id,
                timestamp=timestamp,
                message_id=message_id,
                message_seq=message_seq,
            )
            previous = anchors.get(peer_id)
            if previous is not None and previous != anchor:
                raise HistoryPageError("NapCat recent contact contained conflicting rows for one friend")
            anchors[peer_id] = anchor
        except HistoryPageError as exc:
            invalid[peer_id] = str(exc)
            anchors.pop(peer_id, None)
    return anchors, invalid


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


def normalize_napcat_messages(
    messages: Any,
    *,
    target: HistoryTarget,
    account_id: str,
    page_size: int,
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Validate and normalize one successful NapCat history response page."""

    if target.kind != "private":
        raise HistoryPageError("NapCat active offline history supports private targets only")
    if not isinstance(messages, list) or not messages or len(messages) > page_size:
        raise HistoryPageError("NapCat history response was empty or malformed")
    normalized: list[tuple[float, int, str, dict[str, Any]]] = []
    for index, source in enumerate(messages):
        if not isinstance(source, Mapping):
            raise HistoryPageError("NapCat history page contained a malformed message")
        raw = dict(source)
        message_id = _message_id_text(raw.get("message_id"))
        sequence = _valid_id(raw.get("message_seq"))
        timestamp = _timestamp(raw.get("time"))
        if not message_id or sequence is None or timestamp is None:
            raise HistoryPageError("NapCat history message lacked message_id, message_seq, or time")
        self_id = raw.get("self_id")
        if self_id is not None and str(self_id).strip() != account_id:
            raise HistoryPageError("NapCat history response account did not match the verified login")
        message_type = str(raw.get("message_type") or "").strip().lower()
        expected_type = "private"
        if message_type and message_type != expected_type:
            raise HistoryPageError("NapCat history response message type did not match the target")
        raw["post_type"] = "message"
        raw["message_type"] = expected_type
        raw["self_id"] = account_id
        raw["message_id"] = message_id
        raw["message_seq"] = sequence
        raw["time"] = timestamp
        source_sub_type = str(raw.get("sub_type") or "").strip().lower()
        # The friend-history API can include C2C temporary sessions. These
        # rows still belong to the requested private peer, but ordinary
        # ingress explicitly filters sub_type=group, so keep them navigation
        # only and never convert them into ordinary friend messages.
        temporary_private = message_type == "private" and source_sub_type == "group"
        if source_sub_type not in {"", "friend", "group"}:
            raise HistoryPageError("NapCat friend history contained an unsupported private subtype")
        if source_sub_type == "group" and not temporary_private:
            raise HistoryPageError("NapCat temporary private row lacked an explicit private message type")
        sender = raw.get("sender")
        if not isinstance(sender, Mapping):
            sender = {}
        else:
            sender = dict(sender)
        sender_value = sender.get("user_id")
        if sender_value is None:
            sender_value = raw.get("user_id")
        sender_id = _valid_id(sender_value)
        if sender_id is None:
            raise HistoryPageError("NapCat history message lacked a sender identity")
        sender["user_id"] = int(sender_id)
        raw["sender"] = sender
        raw["sub_type"] = source_sub_type or "friend"
        if temporary_private:
            group_id = _valid_id(raw.get("group_id"))
            if raw.get("group_id") is not None and group_id is None:
                raise HistoryPageError("NapCat temporary private history had a malformed group scope")
            # This is private navigation metadata, not a group route. Remove
            # it from the copied message before shared private-route checks.
            raw.pop("group_id", None)
            raw["_offline_history_skip"] = True
        elif raw.get("group_id") is not None:
            raise HistoryPageError("NapCat friend history unexpectedly contained a group route")
        if sender_id == account_id:
            target_id = _valid_id(raw.get("target_id"))
            if temporary_private and raw.get("target_id") is not None and target_id is None:
                raise HistoryPageError("NapCat outgoing temporary private target was malformed")
            if target_id is not None and target_id != target.session_id:
                raise HistoryPageError("NapCat outgoing friend history target did not match the requested friend")
            raw["target_id"] = int(target.session_id)
            # Only bot-originated rows receive this routing flag. The ordinary
            # private handler still rejects temporary sub_type=group rows.
            raw["_offline_history_outgoing"] = True
            raw["_offline_history_skip"] = True
        elif sender_id != target.session_id:
            raise HistoryPageError("NapCat friend history sender did not match the requested friend")
        normalized.append((timestamp, index, sequence, raw))
    normalized.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[3] for item in normalized), normalized[0][2]


class NapcatOfflineHistory:
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
        self.synchronizer = OfflineHistorySynchronizer(
            self.store,
            backend=BACKEND,
            platform=self.platform,
            page_size=max(1, int(getattr(settings, "page_size", 50))),
            max_pages=max(1, int(getattr(settings, "max_pages", 200))),
            retry_delay=max(0.1, float(getattr(settings, "retry_delay_sec", 15.0))),
            marker_refresh_sec=30.0,
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
        self._pending_live_cues: dict[tuple[str, str], _RecentContactAnchor] = {}
        self._last_generation: tuple[Any, ...] | None = None
        self._prepared_accounts: set[tuple[str, str, str]] = set()
        self._epoch_started_at = time.time()
        self._stopping = False

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
        if not account_id:
            raise ValueError("NapCat history requires a verified account ID")
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
        if _valid_id(normalized_account) != normalized_account:
            raise HistoryPageError("NapCat live message arrived before login verification")
        target = self.target_from_live(raw)
        self_id = raw.get("self_id")
        if target.kind == "private":
            _validate_private_message_scope(
                raw,
                target_id=target.session_id,
                account_id=normalized_account,
            )
            if self_id is not None and _valid_id(self_id) != normalized_account:
                raise HistoryPageError("NapCat live message account did not match verified login")
        elif self_id is not None and _valid_id(self_id) != normalized_account:
            raise HistoryPageError("NapCat live message account did not match verified login")
        message_id = _message_id_text(raw.get("message_id"))
        metadata = context.get("metadata")
        original_at = _timestamp(metadata.get("original_timestamp")) if isinstance(metadata, Mapping) else None
        if not message_id or original_at is None:
            raise HistoryPageError("NapCat live message lacked a stable ID or original timestamp")
        live_time_matches_context = _timestamp(raw.get("time")) == original_at
        live_account_matches_context = _valid_id(self_id) == normalized_account
        if not isinstance(metadata, dict):
            metadata = {}
            context["metadata"] = metadata
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
        # Group messages still use the shared delivery identity, but no group
        # session is registered in the active-history store or pull scheduler.
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
            if live_account_matches_context and live_time_matches_context:
                cue_key = (normalized_account, target.session_id)
                previous = self._pending_live_cues.get(cue_key)
                if previous is None or original_at >= previous.timestamp:
                    self._pending_live_cues[cue_key] = _RecentContactAnchor(
                        target_id=target.session_id,
                        raw_message_id=message_id,
                        timestamp=original_at,
                        message_id=message_id,
                    )
                self._rerun_event.set()
        return context

    def _clear_pending_live_cues(self) -> None:
        self._pending_live_cues.clear()
        self._rerun_event.clear()

    def _take_pending_live_cues(
        self,
        *,
        account_id: str,
        excluded_target_ids: frozenset[str],
    ) -> dict[str, _RecentContactAnchor]:
        cues: dict[str, _RecentContactAnchor] = {}
        for key, anchor in tuple(self._pending_live_cues.items()):
            queued_account, target_id = key
            self._pending_live_cues.pop(key, None)
            if queued_account != account_id or target_id in excluded_target_ids:
                continue
            if anchor.target_id != target_id:
                continue
            cues[target_id] = anchor
        # This is a wakeup only. The concrete target set comes from cues and
        # still-eligible failures, never from the Event by itself.
        self._rerun_event.clear()
        return cues

    @staticmethod
    def target_from_live(raw: Mapping[str, Any]) -> HistoryTarget:
        kind = str(raw.get("message_type") or "").strip().lower()
        sender = raw.get("sender") if isinstance(raw.get("sender"), Mapping) else {}
        if kind == "group":
            session_id = _valid_id(raw.get("group_id"))
        elif kind == "private" and str(raw.get("sub_type") or "") == "friend":
            session_id = _valid_id(sender.get("user_id") or raw.get("user_id"))
        else:
            raise HistoryPageError("NapCat message route is unsupported for history identity")
        if session_id is None:
            raise HistoryPageError("NapCat message route identity is malformed")
        return HistoryTarget(kind, session_id)

    async def _action(self, action: str, params: dict[str, Any], connection: Any) -> dict[str, Any]:
        if self._call_action is None:
            raise RuntimeError("NapCat offline history action callback is not bound")
        response = await self._call_action(action, params, expected_connection=connection)
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise HistoryPageError(f"NapCat {action} did not return an authoritative success response")
        return response

    async def discover_targets(self, account_id: str, connection: Any) -> tuple[HistoryTarget, ...]:
        from src.config import global_config

        chat = global_config.chat
        known_private = {
            target.session_id
            for target in self.store.known_targets(BACKEND, account_id, self.platform)
            if target.kind == "private"
        }
        if chat.private_list_type == "whitelist":
            private = {_valid_id(value) for value in chat.private_list}
            private.discard(None)
        elif chat.private_list_type == "blacklist":
            response = await self._action("get_friend_list", {}, connection)
            rows = response.get("data")
            if not isinstance(rows, list):
                raise HistoryPageError("NapCat friend discovery response was malformed")
            private = {_valid_id(row.get("user_id")) for row in rows if isinstance(row, Mapping)}
            private.discard(None)
            private.update(known_private)
            private.difference_update(str(value) for value in chat.private_list)
        else:
            raise HistoryPageError("NapCat private admission policy is invalid")
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
            raise HistoryPageError("NapCat active offline history supports private targets only")
        response = await self._action(
            "get_friend_msg_history",
            self._history_params(target=target, cursor=cursor, page_size=page_size),
            connection,
        )
        page = self._history_page_from_response(
            response,
            target=target,
            account_id=account_id,
            page_size=page_size,
            allow_empty=False,
        )
        assert page is not None
        return page

    @staticmethod
    def _history_params(
        *, target: HistoryTarget, cursor: str | None, page_size: int
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "count": page_size,
            "reverse_order": bool(cursor),
            "user_id": int(target.session_id),
        }
        if cursor:
            # NapCat accepts the raw recent-contact ID when its short-ID map is
            # not populated. Preserve it as a decimal string without float use.
            params["message_seq"] = str(cursor)
        return params

    @staticmethod
    def _history_page_from_response(
        response: Mapping[str, Any],
        *,
        target: HistoryTarget,
        account_id: str,
        page_size: int,
        allow_empty: bool,
    ) -> HistoryPage | None:
        data = response.get("data")
        messages = data.get("messages") if isinstance(data, Mapping) else None
        if allow_empty and isinstance(messages, list) and not messages:
            return None
        normalized, next_cursor = normalize_napcat_messages(
            messages,
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

    @staticmethod
    def _page_covers_recent_anchor(page: HistoryPage, anchor: _RecentContactAnchor) -> bool:
        for message in page.messages:
            if _timestamp(message.get("time")) != anchor.timestamp:
                continue
            if anchor.message_id is not None and _message_id_text(message.get("message_id")) != anchor.message_id:
                continue
            if anchor.message_seq is not None and _valid_id(message.get("message_seq")) != anchor.message_seq:
                continue
            if anchor.message_id is not None or anchor.message_seq is not None:
                return True
        return False

    @staticmethod
    def _anchor_proof_from_response(
        response: Mapping[str, Any], *, anchor: _RecentContactAnchor, account_id: str
    ) -> tuple[str, str | None]:
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise HistoryPageError("NapCat get_msg anchor proof was malformed")
        # OneBot get_msg returns message fields in data itself. Its `message`
        # member contains user-controlled message content, not another trusted
        # envelope, so never mine headers or IDs from that nested payload.
        message = data
        _validate_private_message_scope(message, target_id=anchor.target_id, account_id=account_id)
        message_id = _message_id_text(message.get("message_id"))
        timestamp = _timestamp(message.get("time"))
        if message_id is None or timestamp != anchor.timestamp:
            raise HistoryPageError("NapCat get_msg did not prove the recent message ID and time")
        message_seq = None
        if message.get("message_seq") is not None:
            message_seq = _valid_id(message.get("message_seq"))
            if message_seq is None:
                raise HistoryPageError("NapCat get_msg anchor sequence was malformed")
        return message_id, message_seq

    async def _fetch_anchored_page(
        self,
        *,
        target: HistoryTarget,
        anchor: _RecentContactAnchor,
        page_size: int,
        connection: Any,
        account_id: str,
        initial_page: HistoryPage | None = None,
    ) -> HistoryPage:
        page = await self.fetch_page(
            target=target,
            cursor=anchor.raw_message_id,
            page_size=page_size,
            connection=connection,
            account_id=account_id,
        )
        if anchor.message_id is not None or anchor.message_seq is not None:
            if not self._page_covers_recent_anchor(page, anchor):
                raise HistoryPageError("NapCat anchored history page omitted the known recent message")
            return self._preserve_same_second_initial_rows(page, initial_page, anchor)

        # The raw 19-digit contact ID is navigation only. Resolve it after the
        # anchored page has populated NapCat's raw-to-short-ID mapping, then
        # require the resolved message itself to be present in that page.
        proof = await self._action(
            "get_msg", {"message_id": anchor.raw_message_id}, connection
        )
        message_id, message_seq = self._anchor_proof_from_response(
            proof, anchor=anchor, account_id=account_id
        )
        if not any(
            _message_id_text(message.get("message_id")) == message_id
            and _timestamp(message.get("time")) == anchor.timestamp
            and (message_seq is None or _valid_id(message.get("message_seq")) == message_seq)
            for message in page.messages
        ):
            raise HistoryPageError("NapCat anchored history page omitted the resolved recent message")
        return self._preserve_same_second_initial_rows(page, initial_page, anchor)

    @staticmethod
    def _preserve_same_second_initial_rows(
        anchor_page: HistoryPage,
        initial_page: HistoryPage | None,
        anchor: _RecentContactAnchor,
    ) -> HistoryPage:
        """Keep post-snapshot same-second rows from the ordinary first page."""

        if initial_page is None:
            return anchor_page
        rows: list[tuple[float, int, int, dict[str, Any]]] = []
        seen_ids: set[str] = set()
        for source_rank, page in ((0, anchor_page), (1, initial_page)):
            for index, message in enumerate(page.messages):
                timestamp = _timestamp(message.get("time"))
                if source_rank == 1 and timestamp != anchor.timestamp:
                    continue
                message_id = _message_id_text(message.get("message_id"))
                if message_id is None or message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                rows.append((timestamp or 0, source_rank, index, dict(message)))
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        if len(rows) == len(anchor_page.messages):
            return anchor_page
        return replace(anchor_page, messages=tuple(row[3] for row in rows))

    def plan_target(
        self,
        target: HistoryTarget,
        checkpoint_id: str | None,
        checkpoint_at: float | None,
        checkpoint_ids: frozenset[str],
        _live_anchor_id: str | None,
        _live_anchor_at: float | None,
        baseline_at: float,
    ) -> ScanPlan:
        if target.kind != "private":
            raise HistoryPageError("NapCat active offline history supports private targets only")
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
            raise HistoryPageError("NapCat active offline history cannot hold group targets")
        if self._send_custom is None:
            raise RuntimeError("NapCat offline history Core callback is not bound")
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
            raise HistoryPageError("NapCat active offline history runs may contain private targets only")
        if self._send_custom is None:
            raise RuntimeError("NapCat offline history Core callback is not bound")
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
        raise asyncio.CancelledError("NapCat or Core connection generation changed during run receipt wait")

    async def send_identity_sync(
        self,
        *,
        run_id: str,
        run_token: str,
        cutoff: float,
        account_id: str,
        identities: tuple[dict[str, Any], ...],
        account_is_current: Callable[[], bool],
    ) -> None:
        """Best-effort seed of old exact NapCat receipts; never waits on a run receipt."""

        if self._send_custom is None or not identities or not account_is_current():
            return
        for offset in range(0, len(identities), _IDENTITY_SYNC_BATCH_SIZE):
            if not account_is_current():
                return
            payload = {
                "version": 1,
                "backend": BACKEND,
                "account_id": account_id,
                "platform": self.platform,
                "source_instance": self.source_instance,
                "run_id": run_id,
                "run_token": run_token,
                "cutoff_at": cutoff,
                "phase": "identity_sync",
                "identities": list(identities[offset : offset + _IDENTITY_SYNC_BATCH_SIZE]),
            }
            try:
                await self._send_custom(
                    platform=self.platform,
                    message_type_name="offline_history_run",
                    message=payload,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger().warning(
                    "NapCat QQ identity seed delivery failed run_id={} error={}",
                    run_id[:12],
                    str(exc)[:200],
                )
                return

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
        connection_epoch_started_at: float,
        account_is_current: Callable[[], bool],
    ) -> bool:
        if not account_is_current():
            return False
        if item.target.kind != "private":
            return False
        raw = dict(item.raw_message)
        expected_self = raw.get("self_id")
        if expected_self is not None and str(expected_self).strip() != account_id:
            raise HistoryPageError("staged NapCat row account did not match the verified login")
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
        context = {"metadata": metadata, "platform": self.platform}
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
                raise RuntimeError("NapCat history ingress callback is not bound")
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
        _retry_target_ids: frozenset[str] | None = None,
        _excluded_target_ids: frozenset[str] = frozenset(),
        _recent_navigation_cache: _RecentNavigationCache | None = None,
        _recent_navigation_scope: tuple[Any, ...] | None = None,
        _live_anchors: Mapping[str, _ScopedLiveAnchor] | None = None,
    ) -> dict[str, Any]:
        await self.prepare_account(account_id)
        self._remember_pending_receipts(account_id)
        live_anchors: dict[str, _RecentContactAnchor] = {}
        if _live_anchors is not None:
            if not isinstance(_live_anchors, Mapping) or _recent_navigation_scope is None:
                raise HistoryPageError("NapCat live history anchors lacked a generation scope")
            for target_id, scoped_anchor in _live_anchors.items():
                if not isinstance(target_id, str) or _valid_id(target_id) != target_id:
                    raise HistoryPageError("NapCat live history anchor target was malformed")
                if not isinstance(scoped_anchor, _ScopedLiveAnchor):
                    raise HistoryPageError("NapCat live history anchor was malformed")
                anchor = scoped_anchor.anchor
                normalized_message_id = (
                    _message_id_text(anchor.message_id)
                    if isinstance(anchor, _RecentContactAnchor)
                    else None
                )
                normalized_raw_id = (
                    _message_id_text(anchor.raw_message_id)
                    if isinstance(anchor, _RecentContactAnchor)
                    else None
                )
                normalized_timestamp = (
                    _timestamp(anchor.timestamp)
                    if isinstance(anchor, _RecentContactAnchor)
                    else None
                )
                if (
                    scoped_anchor.account_id != str(account_id).strip()
                    or scoped_anchor.generation_scope != _recent_navigation_scope
                    or not isinstance(anchor, _RecentContactAnchor)
                    or anchor.target_id != target_id
                    or normalized_message_id is None
                    or normalized_message_id != anchor.message_id
                    or normalized_raw_id is None
                    or normalized_raw_id != anchor.message_id
                    or normalized_timestamp is None
                    or normalized_timestamp != anchor.timestamp
                    or anchor.message_seq is not None
                ):
                    raise HistoryPageError("NapCat live history anchor did not match its verified scope")
                live_anchors[target_id] = anchor
        admitted_targets = tuple(
            target
            for target in await self.discover_targets(account_id, connection)
            if target.kind == "private"
        )
        admitted_target_ids = frozenset(target.session_id for target in admitted_targets)
        excluded_target_ids = _private_target_id_set(_excluded_target_ids, label="excluded")
        active_admitted_targets = tuple(
            target for target in admitted_targets if target.session_id not in excluded_target_ids
        )
        if _retry_target_ids is None:
            targets = active_admitted_targets
        else:
            retry_ids = _private_target_id_set(_retry_target_ids, label="retry")
            targets = tuple(target for target in active_admitted_targets if target.session_id in retry_ids)
        live_anchors = {
            target.session_id: live_anchors[target.session_id]
            for target in targets
            if target.session_id in live_anchors
        }
        snapshot_target_ids = frozenset(
            target.session_id
            for target in targets
            if target.session_id not in live_anchors
        )
        if _recent_navigation_cache is not None and _recent_navigation_scope is None:
            raise ValueError("NapCat recent navigation cache requires a generation scope")
        recent_snapshot_loaded = False
        recent_anchors: dict[str, _RecentContactAnchor] = {}
        invalid_recent_targets: dict[str, str] = {}
        pending_snapshot_coverage: dict[str, _RecentContactAnchor] = {}
        snapshot_rows_seen: dict[str, set[tuple[str, str | None]]] = {}
        resolved_snapshot_ids: dict[str, tuple[str, str | None]] = {}
        snapshot_proof_attempts: dict[str, int] = {}
        identity_sync_context: dict[str, Any] = {}

        def target_boundary_at(target: HistoryTarget) -> float:
            _checkpoint_id, checkpoint_at, _checkpoint_ids = self.store.checkpoint(
                BACKEND, account_id, self.platform, target
            )
            if checkpoint_at is not None:
                return checkpoint_at
            return self.store.initialize_scope(BACKEND, account_id, self.platform)

        def page_crosses_target_boundary(target: HistoryTarget, page: HistoryPage) -> bool:
            checkpoint_id, checkpoint_at, checkpoint_ids = self.store.checkpoint(
                BACKEND, account_id, self.platform, target
            )
            boundary_at = checkpoint_at
            if boundary_at is None:
                boundary_at = self.store.initialize_scope(BACKEND, account_id, self.platform)
            known_boundary_ids = set(checkpoint_ids)
            if checkpoint_id is not None:
                known_boundary_ids.add(str(checkpoint_id))
            for message in page.messages:
                message_id = _message_id_text(message.get("message_id"))
                if message_id is not None and message_id in known_boundary_ids:
                    return True
                timestamp = _timestamp(message.get("time"))
                if timestamp is None:
                    continue
                if checkpoint_id is None and timestamp <= boundary_at:
                    return True
                if checkpoint_id is not None and timestamp < boundary_at:
                    return True
            return False

        async def load_recent_snapshot() -> None:
            nonlocal recent_snapshot_loaded, recent_anchors, invalid_recent_targets
            if recent_snapshot_loaded:
                return
            recent_snapshot_loaded = True
            if not account_is_current():
                raise asyncio.CancelledError("NapCat/Core connection generation changed")
            if not snapshot_target_ids:
                # A validated live message is already an exact ID/time anchor.
                # Do not refresh or age the global navigation cache for it.
                return
            if _recent_navigation_cache is not None:
                cached = _recent_navigation_cache.get(
                    scope=_recent_navigation_scope,
                    admitted_target_ids=snapshot_target_ids,
                )
                if cached is not None:
                    recent_anchors, invalid_recent_targets = cached
                    if invalid_recent_targets:
                        _logger().warning(
                            "NapCat recent contact snapshot ignored invalid private rows count={}",
                            len(invalid_recent_targets),
                        )
                    return
            try:
                response = await self._action("get_recent_contact", {"count": 100}, connection)
                if not account_is_current():
                    raise asyncio.CancelledError("NapCat/Core connection generation changed")
                recent_anchors, invalid_recent_targets = _parse_recent_contact_anchors(
                    response.get("data"), targets=active_admitted_targets, account_id=account_id
                )
                if _recent_navigation_cache is not None:
                    _recent_navigation_cache.remember(
                        scope=_recent_navigation_scope,
                        admitted_target_ids=frozenset(
                            target.session_id for target in active_admitted_targets
                        ),
                        anchors=recent_anchors,
                        invalid_targets=invalid_recent_targets,
                    )
                if invalid_recent_targets:
                    _logger().warning(
                        "NapCat recent contact snapshot ignored invalid private rows count={}",
                        len(invalid_recent_targets),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The snapshot improves coverage but never turns a valid
                # ordinary first page into an error. Failed first pages still
                # remain incomplete because no anchor is available.
                recent_anchors = {}
                invalid_recent_targets = {}
                _logger().warning(
                    "NapCat recent contact snapshot unavailable account_id={} error={}",
                    account_id,
                    str(exc)[:240],
                )

        def remember_snapshot_rows(target: HistoryTarget, page: HistoryPage, anchor: _RecentContactAnchor) -> None:
            rows = snapshot_rows_seen.setdefault(target.session_id, set())
            for message in page.messages:
                if _timestamp(message.get("time")) == anchor.timestamp:
                    message_id = _message_id_text(message.get("message_id"))
                    if message_id is not None:
                        rows.add((message_id, _valid_id(message.get("message_seq"))))

        def page_has_resolved_snapshot(target: HistoryTarget, anchor: _RecentContactAnchor) -> bool:
            resolved = resolved_snapshot_ids.get(target.session_id)
            if resolved is None:
                return False
            message_id, message_seq = resolved
            return any(
                seen_id == message_id and (message_seq is None or seen_seq == message_seq)
                for seen_id, seen_seq in snapshot_rows_seen.get(target.session_id, set())
            )

        async def continue_snapshot_coverage(target: HistoryTarget, page: HistoryPage) -> HistoryPage:
            anchor = pending_snapshot_coverage.get(target.session_id)
            if anchor is None:
                return page
            remember_snapshot_rows(target, page, anchor)
            if anchor.message_id is not None or anchor.message_seq is not None:
                if self._page_covers_recent_anchor(page, anchor):
                    pending_snapshot_coverage.pop(target.session_id, None)
                    return page
            elif page_has_resolved_snapshot(target, anchor):
                pending_snapshot_coverage.pop(target.session_id, None)
                return page
            elif any(_timestamp(message.get("time")) == anchor.timestamp for message in page.messages):
                attempts = snapshot_proof_attempts.get(target.session_id, 0)
                if attempts < 2:
                    snapshot_proof_attempts[target.session_id] = attempts + 1
                    try:
                        proof = await self._action(
                            "get_msg", {"message_id": anchor.raw_message_id}, connection
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        _logger().warning(
                            "NapCat recent message ID proof unavailable session_id={} error={}",
                            target.session_id,
                            str(exc)[:200],
                        )
                    else:
                        resolved_snapshot_ids[target.session_id] = self._anchor_proof_from_response(
                            proof, anchor=anchor, account_id=account_id
                        )
                        if page_has_resolved_snapshot(target, anchor):
                            pending_snapshot_coverage.pop(target.session_id, None)
                            return page

            latest_time = max(_timestamp(message.get("time")) or 0 for message in page.messages)
            if (
                latest_time < anchor.timestamp
                or (page.end_of_history and latest_time <= anchor.timestamp)
                or page.boundary_reached
                or page_crosses_target_boundary(target, page)
            ):
                raise HistoryPageError(
                    "NapCat backward paging passed the recent snapshot without proving its latest message"
                )
            # A short page newer than the known snapshot cannot prove the
            # history ended. Keep paging from its own cursor to the snapshot.
            return replace(page, end_of_history=False, boundary_reached=False)

        async def fetch_initial_page(target: HistoryTarget, page_size: int) -> HistoryPage:
            await load_recent_snapshot()
            anchor = live_anchors.get(target.session_id)
            if anchor is None and target.session_id not in invalid_recent_targets:
                anchor = recent_anchors.get(target.session_id)
            try:
                response = await self._action(
                    "get_friend_msg_history",
                    self._history_params(target=target, cursor=None, page_size=page_size),
                    connection,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if anchor is None:
                    raise
                return await self._fetch_anchored_page(
                    target=target,
                    anchor=anchor,
                    page_size=page_size,
                    connection=connection,
                    account_id=account_id,
                )

            # A successful but malformed page remains a strict failure. Only
            # an actually empty page is eligible for the anchored retry.
            page = self._history_page_from_response(
                response,
                target=target,
                account_id=account_id,
                page_size=page_size,
                allow_empty=True,
            )
            if page is None:
                if anchor is None:
                    raise HistoryPageError("NapCat initial friend-history page was empty without a recent anchor")
                return await self._fetch_anchored_page(
                    target=target,
                    anchor=anchor,
                    page_size=page_size,
                    connection=connection,
                    account_id=account_id,
                )
            if anchor is None:
                return page

            latest_time = max(_timestamp(message.get("time")) or 0 for message in page.messages)
            if latest_time > anchor.timestamp:
                # This independently valid first page contains messages newer
                # than the snapshot. Keep its cursor so the engine can walk
                # backward through the snapshot time and checkpoint boundary.
                if (
                    target.session_id not in live_anchors
                    and anchor.timestamp < target_boundary_at(target)
                ):
                    # The snapshot predates the saved stop boundary. The
                    # synchronizer may finish at that boundary without paging
                    # farther back solely to rediscover already-covered rows.
                    return page
                covered = (
                    self._page_covers_recent_anchor(page, anchor)
                    if anchor.message_id is not None or anchor.message_seq is not None
                    else False
                )
                if not covered:
                    pending_snapshot_coverage[target.session_id] = anchor
                    remember_snapshot_rows(target, page, anchor)
                    return await continue_snapshot_coverage(target, page)
                return page

            if anchor.message_id is not None or anchor.message_seq is not None:
                if self._page_covers_recent_anchor(page, anchor):
                    return page
                return await self._fetch_anchored_page(
                    target=target,
                    anchor=anchor,
                    page_size=page_size,
                    connection=connection,
                    account_id=account_id,
                    initial_page=page,
                )

            # Equality is not enough: distinct messages can share a second.
            # The anchored request plus get_msg resolves the short ID exactly.
            return await self._fetch_anchored_page(
                target=target,
                anchor=anchor,
                page_size=page_size,
                connection=connection,
                account_id=account_id,
                initial_page=page,
            )

        async def fetch(target: HistoryTarget, cursor: str | None, page_size: int, direction: str) -> HistoryPage:
            if direction != "backward":
                raise HistoryPageError("NapCat history supports only backward paging")
            if not account_is_current():
                raise asyncio.CancelledError("NapCat/Core connection generation changed")
            if cursor is None:
                page = await fetch_initial_page(target, page_size)
            else:
                page = await self.fetch_page(
                    target=target,
                    cursor=cursor,
                    page_size=page_size,
                    connection=connection,
                    account_id=account_id,
                )
                page = await continue_snapshot_coverage(target, page)
            if not account_is_current():
                raise asyncio.CancelledError("NapCat/Core connection generation changed")
            await sync_page_identities(target, page)
            return page

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
            await self.send_run(
                run_id=run_id,
                run_token=run_token,
                cutoff=cutoff,
                targets=run_targets,
                phase=phase,
                account_id=account_id,
                account_is_current=account_is_current,
            )
            if phase == "begin":
                identity_sync_context.update(
                    {"run_id": run_id, "run_token": run_token, "cutoff_at": cutoff}
                )

        async def sync_page_identities(target: HistoryTarget, page: HistoryPage) -> None:
            run_id = str(identity_sync_context.get("run_id") or "")
            run_token = str(identity_sync_context.get("run_token") or "")
            cutoff = _timestamp(identity_sync_context.get("cutoff_at"))
            if not run_id or not run_token or cutoff is None or target.kind != "private":
                return
            records_by_key: dict[str, dict[str, Any]] = {}
            conflicts: set[str] = set()
            for raw_value in page.messages:
                if not isinstance(raw_value, Mapping):
                    continue
                raw = dict(raw_value)
                if (
                    raw.get("_offline_history_skip")
                    or raw.get("_offline_history_outgoing")
                    or raw.get("message_type") != "private"
                    or raw.get("sub_type") != "friend"
                    or str(raw.get("self_id") or "").strip() != account_id
                ):
                    continue
                message_id = _message_id_text(raw.get("message_id"))
                original_timestamp = _timestamp(raw.get("time"))
                if (
                    message_id is None
                    or original_timestamp is None
                    or original_timestamp > cutoff
                ):
                    continue
                identity = build_qq_private_identity(
                    backend=BACKEND,
                    account_id=account_id,
                    platform=self.platform,
                    session_kind=target.kind,
                    session_id=target.session_id,
                    raw=raw,
                    original_timestamp=original_timestamp,
                )
                if identity is None:
                    continue
                key = scoped_delivery_key(BACKEND, account_id, self.platform, target, message_id)
                record = {
                    "session_kind": target.kind,
                    "session_id": target.session_id,
                    "message_id": message_id,
                    "delivery_key": key,
                    "original_timestamp": original_timestamp,
                    "qq_identity": identity,
                }
                previous = records_by_key.get(key)
                if previous is not None and previous != record:
                    conflicts.add(key)
                else:
                    records_by_key[key] = record
            records = tuple(
                record for key, record in records_by_key.items() if key not in conflicts
            )
            await self.send_identity_sync(
                run_id=run_id,
                run_token=run_token,
                cutoff=cutoff,
                account_id=account_id,
                identities=records,
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
                "NapCat offline history session incomplete kind={} session_id={} error={}",
                target.kind,
                target.session_id,
                str(exc)[:300],
            ),
            retry_delivery_failures=False,
        )
        result["_admitted_target_ids"] = admitted_target_ids
        result["_attempted_target_ids"] = frozenset(target.session_id for target in targets)
        return result

    async def _wait_for_retry_event(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._rerun_event.wait(), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            pass

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
        # A monitor generation owns its own cues and retry budget. Discard
        # signals saved before this QQ/Core socket pair became active.
        self._clear_pending_live_cues()
        retry_base = max(0.1, float(getattr(self.settings, "retry_delay_sec", 15.0)))
        retry_delay = retry_base
        previous_failure_ids: frozenset[str] | None = None
        eligible_failure_ids: frozenset[str] = frozenset()
        unreported_retry_ids: frozenset[str] = frozenset()
        failure_counts: dict[str, int] = {}
        retired_target_ids: set[str] = set()
        full_scan_pending = True
        generation_live_anchors: dict[str, _ScopedLiveAnchor] = {}
        recent_navigation_cache = _RecentNavigationCache()
        normalized_account_id = str(account_id).strip()
        recent_navigation_scope = (
            BACKEND,
            normalized_account_id,
            self.platform,
            id(connection),
            tuple(platform_identity),
            tuple(core_identity),
        )

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
            run_live_anchors: dict[str, _ScopedLiveAnchor] = {}
            if full_scan_pending:
                # The first scan for a socket generation is always full,
                # regardless of live traffic queued before it could start.
                run_retry_target_ids = None
                full_scan_pending = False
            else:
                queued_live_cues = self._take_pending_live_cues(
                    account_id=normalized_account_id,
                    excluded_target_ids=frozenset(retired_target_ids),
                )
                for target_id, anchor in queued_live_cues.items():
                    scoped = _ScopedLiveAnchor(
                        account_id=normalized_account_id,
                        generation_scope=recent_navigation_scope,
                        anchor=anchor,
                    )
                    previous = generation_live_anchors.get(target_id)
                    if previous is None or anchor.timestamp >= previous.anchor.timestamp:
                        generation_live_anchors[target_id] = scoped

                candidate_ids = (
                    set(eligible_failure_ids)
                    | set(unreported_retry_ids)
                    | set(queued_live_cues)
                ) - retired_target_ids
                if not candidate_ids:
                    if current() and not self._stopping:
                        await self._wait_for_retry_event(max(30.0, retry_delay * 2.0))
                    continue
                run_retry_target_ids = frozenset(candidate_ids)
                run_live_anchors = {
                    target_id: generation_live_anchors[target_id]
                    for target_id in candidate_ids
                    if target_id in generation_live_anchors
                }

            try:
                result = await self.run_generation(
                    account_id=account_id,
                    connection=connection,
                    connection_epoch_started_at=connection_epoch_started_at,
                    account_is_current=current,
                    _retry_target_ids=run_retry_target_ids,
                    _excluded_target_ids=frozenset(retired_target_ids),
                    _recent_navigation_cache=recent_navigation_cache,
                    _recent_navigation_scope=recent_navigation_scope,
                    _live_anchors=run_live_anchors,
                )
                unreported_retry_ids = frozenset()
                failure_ids = _failed_private_target_ids(result.get("failed_sessions"))
                admitted_ids = _private_target_id_set(
                    result.get("_admitted_target_ids"), label="admitted"
                )
                attempted_ids = _private_target_id_set(
                    result.get("_attempted_target_ids"), label="attempted"
                )
                if not failure_ids.issubset(attempted_ids):
                    raise HistoryPageError("NapCat history reported failure for a target it did not attempt")

                for target_id in tuple(failure_counts):
                    if target_id not in admitted_ids and target_id not in retired_target_ids:
                        failure_counts.pop(target_id, None)
                for target_id in attempted_ids - failure_ids:
                    failure_counts.pop(target_id, None)
                for target_id in failure_ids:
                    count = failure_counts.get(target_id, 0) + 1
                    failure_counts[target_id] = count
                    if count == 3 and target_id not in retired_target_ids:
                        retired_target_ids.add(target_id)
                        _logger().warning(
                            "NapCat offline history retries stopped after repeated failure session_id={} consecutive_failures={}",
                            target_id,
                            count,
                        )
                eligible_failure_ids = failure_ids - retired_target_ids

                for target_id in tuple(generation_live_anchors):
                    if (
                        target_id not in admitted_ids
                        or target_id in retired_target_ids
                        or target_id not in eligible_failure_ids
                    ):
                        generation_live_anchors.pop(target_id, None)

                if self._rerun_event.is_set():
                    # The current run's failure counts have already been
                    # applied. Drain the cue and union it with these failures
                    # on the next pass.
                    continue
                if eligible_failure_ids:
                    recovered_previous_targets = (
                        previous_failure_ids.intersection(attempted_ids - failure_ids)
                        if previous_failure_ids is not None
                        else frozenset()
                    )
                    made_progress = (
                        previous_failure_ids is not None
                        and (
                            bool(recovered_previous_targets)
                            or len(eligible_failure_ids) < len(previous_failure_ids)
                        )
                    )
                    retry_delay = (
                        retry_base
                        if previous_failure_ids is None
                        else _next_retry_delay(retry_delay, retry_base, progress=made_progress)
                    )
                    previous_failure_ids = eligible_failure_ids
                    _logger().warning(
                        "NapCat offline history retry scheduled failed_sessions={} retry_in_sec={:.1f} account_id={}",
                        len(eligible_failure_ids),
                        retry_delay,
                        account_id,
                    )
                    if current():
                        await self._wait_for_retry_event(retry_delay)
                    continue
                previous_failure_ids = None
                retry_delay = retry_base
                # Stay subscribed for the life of this connection generation;
                # idle timeouts only recheck identity and never rescan history.
                while current() and not self._stopping and not self._rerun_event.is_set():
                    await self._wait_for_retry_event(max(30.0, retry_delay * 2.0))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger().warning("NapCat offline history run failed error={}", str(exc)[:300])
                if previous_failure_ids is not None:
                    retry_delay = _next_retry_delay(retry_delay, retry_base, progress=False)
                if run_retry_target_ids is None:
                    full_scan_pending = True
                else:
                    unreported_retry_ids = frozenset(
                        set(unreported_retry_ids) | set(run_retry_target_ids)
                    )
                if self._rerun_event.is_set():
                    continue
                if current():
                    await self._wait_for_retry_event(retry_delay)

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
        try:
            while not self._stopping:
                state = get_platform_state()
                account_id = str(state.get("account_id") or "").strip()
                platform_connected = state.get("connected") is True and bool(account_id)
                if platform_connected and self.enabled:
                    try:
                        await self.prepare_account(account_id)
                    except Exception as exc:
                        _logger().warning("NapCat offline history baseline persistence failed error={}", str(exc)[:240])
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
                            name=f"offline-history-napcat-{account_id}",
                        )
                await asyncio.sleep(max(0.1, poll_interval))
        finally:
            self._stopping = True
            if self._active_run is not None and not self._active_run.done():
                self._active_run.cancel()
                await asyncio.gather(self._active_run, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping = True
        if self._active_run is not None and not self._active_run.done():
            self._active_run.cancel()
            await asyncio.gather(self._active_run, return_exceptions=True)
        self.store.close()


_runtime: NapcatOfflineHistory | None = None


def get_offline_history_runtime() -> NapcatOfflineHistory:
    """Lazily read configuration and construct the runtime without opening SQLite."""

    global _runtime
    if _runtime is None:
        from src.config import global_config

        _runtime = NapcatOfflineHistory(
            platform=global_config.nachobot_server.platform_name,
            settings=global_config.offline_recovery,
        )
    return _runtime


__all__ = [
    "BACKEND",
    "OFFLINE_HISTORY_SOURCE_INSTANCE",
    "NapcatOfflineHistory",
    "core_connection_identity",
    "get_offline_history_runtime",
    "normalize_napcat_messages",
]
