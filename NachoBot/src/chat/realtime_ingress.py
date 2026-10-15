"""Provider-neutral ingress and row scheduling for authorized realtime turns."""

from __future__ import annotations

import inspect
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, TYPE_CHECKING

from src.chat.runtime_capabilities import additional_config_from_message

if TYPE_CHECKING:
    from src.chat.realtime_speaker import RealtimeSpeakerContext

logger = logging.getLogger(__name__)

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_OPAQUE_MAX_LENGTH = 256
_DEFAULT_SESSION_TTL_SECONDS = 120.0
_MAX_SESSION_TTL_SECONDS = 3600.0
_ROW_LEDGER_LIMIT = 4096
_TURN_LEDGER_LIMIT = 4096
_SESSION_LEDGER_LIMIT = 512


@dataclass(frozen=True, slots=True)
class RealtimeSession:
    version: int
    provider: str
    session_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class RealtimeLifecycleChange:
    """A provider-verified change to an opaque session lease."""

    provider: str
    session_id: str
    chat_id: str
    active: bool
    ttl_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RealtimeProvider:
    name: str
    verify: Callable[[Any, str, RealtimeSession], bool | Awaitable[bool]]
    lifecycle: Callable[
        [Any, str],
        RealtimeLifecycleChange | None | Awaitable[RealtimeLifecycleChange | None],
    ] | None = None
    enabled: Callable[[], bool] = lambda: True


@dataclass(frozen=True, slots=True)
class RealtimeIngressDecision:
    declared: bool
    authorized: bool = False
    session: RealtimeSession | None = None
    speaker: RealtimeSpeakerContext | None = None


@dataclass(slots=True)
class _SessionLease:
    provider: str
    session_id: str
    chat_id: str
    expires_at: float


@dataclass(slots=True)
class _PendingRow:
    row_id: int
    provider: str
    session_id: str
    turn_id: str
    message_id: str
    created_at: float
    speaker: RealtimeSpeakerContext | None = None
    revalidated: bool = False
    revalidated_provider: RealtimeProvider | None = None
    revalidated_provider_generation: int | None = None


def parse_realtime_session(message: Any) -> tuple[bool, RealtimeSession | None]:
    """Return whether a declaration exists and its strictly parsed value."""

    try:
        additional = additional_config_from_message(message)
    except Exception:
        return False, None
    if "realtime_session" not in additional:
        return False, None
    raw = additional.get("realtime_session")
    if not isinstance(raw, dict) or set(raw) - {"version", "provider", "session_id", "turn_id"}:
        return True, None
    if type(raw.get("version")) is not int or raw.get("version") != 1:
        return True, None
    provider = raw.get("provider")
    session_id = raw.get("session_id")
    turn_id = raw.get("turn_id")
    if (
        not isinstance(provider, str)
        or not _PROVIDER_PATTERN.fullmatch(provider)
        or not _valid_opaque_id(session_id)
        or not _valid_opaque_id(turn_id)
    ):
        return True, None
    return True, RealtimeSession(1, provider, session_id, turn_id)


def _valid_opaque_id(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= _OPAQUE_MAX_LENGTH
        and value == value.strip()
        and not any(ord(char) < 32 for char in value)
    )


def _awaitable_result(value: Any) -> Awaitable[Any] | None:
    return value if inspect.isawaitable(value) else None


class RealtimeIngressManager:
    """Validate provider sessions and deliver their rows ahead of recovery."""

    def __init__(
        self,
        *,
        session_ttl_seconds: float = _DEFAULT_SESSION_TTL_SECONDS,
        row_limit: int = _ROW_LEDGER_LIMIT,
        turn_limit: int = _TURN_LEDGER_LIMIT,
        session_limit: int = _SESSION_LEDGER_LIMIT,
    ) -> None:
        self.session_ttl_seconds = max(1.0, float(session_ttl_seconds))
        self.row_limit = max(16, int(row_limit))
        self.turn_limit = max(16, int(turn_limit))
        self.session_limit = max(8, int(session_limit))
        self._providers: dict[str, RealtimeProvider] = {}
        self._provider_generations: dict[str, int] = {}
        self._sessions: OrderedDict[tuple[str, str], _SessionLease] = OrderedDict()
        self._revocation_epochs: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._latest_turns: OrderedDict[tuple[str, str], tuple[str, int]] = OrderedDict()
        self._request_sequence = 0
        self._pending: dict[str, OrderedDict[int, _PendingRow]] = {}
        self._excluded: dict[str, OrderedDict[int, float]] = {}
        self._processed: dict[str, OrderedDict[int, float]] = {}
        self._turns: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    def register_provider(self, provider: RealtimeProvider) -> None:
        if not _PROVIDER_PATTERN.fullmatch(provider.name):
            raise ValueError("Realtime provider name is invalid")
        if self._providers.get(provider.name) is not provider:
            self._provider_generations[provider.name] = self._provider_generations.get(provider.name, 0) + 1
        self._providers[provider.name] = provider

    def unregister_provider(self, name: str) -> None:
        self._providers.pop(name, None)
        self._provider_generations[name] = self._provider_generations.get(name, 0) + 1
        for key in tuple(self._sessions):
            if key[0] == name:
                self._revoke_session(key)

    async def shutdown(self) -> None:
        for key in tuple(self._sessions):
            self._revoke_session(key)
        self._pending.clear()
        self._excluded.clear()
        self._processed.clear()
        self._turns.clear()
        self._latest_turns.clear()

    async def inspect_message(self, message: Any, chat_id: str) -> RealtimeIngressDecision:
        """Verify lifecycle envelopes and any declared realtime session."""

        from src.chat.realtime_speaker import (
            anonymous_voice_candidate,
            attach_verified_realtime_speaker,
            strip_core_speaker_claim,
        )

        # The reserved marker is Core provenance. Never let an adapter-supplied
        # value influence lifecycle callbacks or provider verification.
        strip_core_speaker_claim(message)
        self._expire_sessions()
        for provider in tuple(self._providers.values()):
            if provider.lifecycle is None or not self._provider_enabled(provider):
                continue
            generation = self._provider_generations.get(provider.name, 0)
            revocation_snapshot = {
                key: epoch for key, epoch in self._revocation_epochs.items() if key[0] == provider.name
            }
            try:
                result = provider.lifecycle(message, chat_id)
                pending = _awaitable_result(result)
                change = await pending if pending is not None else result
                revocation_changed = any(
                    self._revocation_epochs.get(key, 0) != epoch
                    for key, epoch in revocation_snapshot.items()
                ) or any(
                    key not in revocation_snapshot and key[0] == provider.name and epoch != 0
                    for key, epoch in self._revocation_epochs.items()
                )
                if (
                    self._providers.get(provider.name) != provider
                    or self._provider_generations.get(provider.name, 0) != generation
                    or revocation_changed
                ):
                    continue
                self._apply_lifecycle_change(provider, change, chat_id)
            except Exception:
                logger.debug("Realtime lifecycle verification failed provider=%s", provider.name, exc_info=True)

        declared, session = parse_realtime_session(message)
        if not declared:
            return RealtimeIngressDecision(False)
        if session is None:
            return RealtimeIngressDecision(True)
        provider = self._providers.get(session.provider)
        if provider is None or not self._provider_enabled(provider):
            return RealtimeIngressDecision(True, session=session)
        binding = (session.provider, session.session_id)
        turn_key = (*binding, session.turn_id)
        if turn_key in self._turns:
            self._turns.move_to_end(turn_key)
            return RealtimeIngressDecision(True, session=session)
        self._request_sequence += 1
        request_sequence = self._request_sequence
        provider_generation = self._provider_generations.get(session.provider, 0)
        revocation_epoch = self._revocation_epochs.get(binding, 0)
        try:
            result = provider.verify(message, chat_id, session)
            pending = _awaitable_result(result)
            verified = await pending if pending is not None else result
        except Exception:
            logger.info("Realtime session proof failed provider=%s", session.provider, exc_info=True)
            return RealtimeIngressDecision(True, session=session)
        if (
            verified is not True
            or self._providers.get(session.provider) != provider
            or self._provider_generations.get(session.provider, 0) != provider_generation
            or self._revocation_epochs.get(binding, 0) != revocation_epoch
        ):
            return RealtimeIngressDecision(True, session=session)
        latest_turn = self._latest_turns.get(binding)
        if latest_turn is not None and latest_turn[1] > request_sequence:
            return RealtimeIngressDecision(True, session=session)
        if turn_key in self._turns:
            return RealtimeIngressDecision(True, session=session)
        self._advance_latest_turn(binding, session.turn_id, request_sequence)
        self._activate_session(session.provider, session.session_id, chat_id, self.session_ttl_seconds)
        speaker = anonymous_voice_candidate(message)
        if speaker is not None and not attach_verified_realtime_speaker(message, speaker):
            speaker = None
        return RealtimeIngressDecision(True, True, session, speaker)

    def record_message(self, decision: RealtimeIngressDecision, stored_ref: Any) -> bool:
        """Attach an authorized row to its session or quarantine its ID."""

        if not decision.declared:
            return False
        chat_id = str(getattr(stored_ref, "chat_id", "") or "")
        row_id = _positive_row_id(getattr(stored_ref, "row_id", None))
        if not chat_id or row_id is None:
            return False
        if not decision.authorized or decision.session is None:
            self._exclude_row(chat_id, row_id)
            return False
        message_id = str(getattr(stored_ref, "message_id", "") or "")
        session = decision.session
        key = (session.provider, session.session_id)
        latest_turn = self._latest_turns.get(key)
        if latest_turn is None or latest_turn[0] != session.turn_id:
            self._exclude_row(chat_id, row_id)
            return False
        if (*key, session.turn_id) in self._turns:
            self._exclude_row(chat_id, row_id)
            return False
        lease = self._fresh_session(key)
        if lease is None or lease.chat_id != chat_id:
            self._exclude_row(chat_id, row_id)
            return False
        if self._is_row_known(chat_id, row_id):
            return False
        turn_key = (session.provider, session.session_id, session.turn_id)
        if turn_key in self._turns:
            self._turns.move_to_end(turn_key)
            self._exclude_row(chat_id, row_id)
            return False
        pending = self._pending.setdefault(chat_id, OrderedDict())
        pending[row_id] = _PendingRow(
            row_id=row_id,
            provider=session.provider,
            session_id=session.session_id,
            turn_id=session.turn_id,
            message_id=message_id,
            created_at=time.monotonic(),
            speaker=decision.speaker,
        )
        self._turns[turn_key] = "pending"
        self._trim_ledgers(chat_id)
        return True

    async def revalidate_message(self, message: Any, chat_id: str) -> bool:
        """Repeat the provider proof immediately before Replyer dispatch."""

        from src.chat.realtime_speaker import stored_speaker_marker_matches

        row_id = _row_id_from_message(message)
        row = self._pending.get(chat_id, {}).get(row_id) if row_id is not None else None
        if row is not None and row.speaker is not None:
            row.revalidated = False
            row.revalidated_provider = None
            row.revalidated_provider_generation = None
            if (
                not row.message_id
                or str(getattr(message, "message_id", "") or "") != row.message_id
                or str(getattr(message, "chat_id", "") or "") != chat_id
                or not stored_speaker_marker_matches(message, row.speaker)
            ):
                self._exclude_row(chat_id, row_id)
                return False

        declared, session = parse_realtime_session(message)
        if not declared or session is None:
            return False
        if row is not None and (
            session.provider != row.provider
            or session.session_id != row.session_id
            or session.turn_id != row.turn_id
        ):
            self._exclude_row(chat_id, row_id)
            return False
        provider = self._providers.get(session.provider)
        if provider is None or not self._provider_enabled(provider):
            self._revoke_session((session.provider, session.session_id))
            return False
        binding = (session.provider, session.session_id)
        latest_turn = self._latest_turns.get(binding)
        if latest_turn is None or latest_turn[0] != session.turn_id:
            self._exclude_row(chat_id, _row_id_from_message(message))
            return False
        lease = self._fresh_session(binding)
        if lease is None or lease.chat_id != chat_id:
            self._exclude_row(chat_id, _row_id_from_message(message))
            return False
        provider_generation = self._provider_generations.get(session.provider, 0)
        revocation_epoch = self._revocation_epochs.get(binding, 0)
        try:
            result = provider.verify(message, chat_id, session)
            pending = _awaitable_result(result)
            verified = await pending if pending is not None else result
        except Exception:
            verified = False
        if self._latest_turns.get(binding) != latest_turn:
            self._exclude_row(chat_id, _row_id_from_message(message))
            return False
        if (
            verified is not True
            or self._providers.get(session.provider) is not provider
            or self._provider_generations.get(session.provider, 0) != provider_generation
            or self._revocation_epochs.get(binding, 0) != revocation_epoch
        ):
            self._revoke_session((session.provider, session.session_id))
            return False
        lease.expires_at = time.monotonic() + self.session_ttl_seconds
        self._sessions.move_to_end((session.provider, session.session_id))
        if row is not None and row.speaker is not None:
            if self._pending.get(chat_id, {}).get(row.row_id) is not row:
                return False
            row.revalidated = True
            row.revalidated_provider = provider
            row.revalidated_provider_generation = provider_generation
        return True

    def is_authorized_anonymous_speaker_message(self, message: Any) -> bool:
        """Require a current, exact pending-row proof for direct speaker routing."""

        try:
            from src.chat.realtime_speaker import stored_speaker_marker_matches
            from src.common.data_models.database_data_model import DatabaseMessages

            if not isinstance(message, DatabaseMessages):
                return False
            row_id = _row_id_from_message(message)
            chat_id = str(getattr(message, "chat_id", "") or "")
            message_id = str(getattr(message, "message_id", "") or "")
            if row_id is None or not chat_id or not message_id:
                return False
            row = self._pending.get(chat_id, {}).get(row_id)
            if (
                row is None
                or row.speaker is None
                or not row.revalidated
                or self._providers.get(row.provider) is not row.revalidated_provider
                or self._provider_generations.get(row.provider, 0) != row.revalidated_provider_generation
                or row.revalidated_provider is None
                or not self._provider_enabled(row.revalidated_provider)
                or row.message_id != message_id
                or str(getattr(message, "user_id", "") or "")
                or getattr(message, "user_info", None) is not None
                or not stored_speaker_marker_matches(message, row.speaker)
            ):
                return False
            declared, session = parse_realtime_session(message)
            if (
                not declared
                or session is None
                or session.provider != row.provider
                or session.session_id != row.session_id
                or session.turn_id != row.turn_id
                or self._latest_turns.get((row.provider, row.session_id), (None, 0))[0] != row.turn_id
                or self._turns.get((row.provider, row.session_id, row.turn_id)) != "pending"
            ):
                return False
            lease = self._fresh_session((row.provider, row.session_id))
            return lease is not None and lease.chat_id == chat_id
        except Exception:
            return False

    def is_active_message(self, message: Any, chat_id: str) -> bool:
        """Fast cached check for Focus routing after asynchronous verification."""

        declared, session = parse_realtime_session(message)
        if not declared or session is None:
            return False
        provider = self._providers.get(session.provider)
        if provider is None or not self._provider_enabled(provider):
            self._revoke_session((session.provider, session.session_id))
            return False
        lease = self._fresh_session((session.provider, session.session_id))
        return lease is not None and lease.chat_id == chat_id

    def has_active_session(self, chat_id: str) -> bool:
        self._expire_sessions()
        return any(lease.chat_id == chat_id for lease in self._sessions.values())

    def is_current_turn_message(self, message: Any, chat_id: str) -> bool:
        """Check an already verified turn before starting its Replyer request."""
        if not self.is_active_message(message, chat_id):
            return False
        _, session = parse_realtime_session(message)
        if session is None:
            return False
        latest = self._latest_turns.get((session.provider, session.session_id))
        return latest is not None and latest[0] == session.turn_id

    def session_expiry_delay(self, chat_id: str) -> float | None:
        self._expire_sessions()
        deadlines = [lease.expires_at - time.monotonic() for lease in self._sessions.values() if lease.chat_id == chat_id]
        return max(0.0, min(deadlines)) if deadlines else None

    def pending_row_ids(self, chat_id: str) -> tuple[int, ...]:
        self._expire_sessions()
        pending = self._pending.get(chat_id)
        if not pending:
            return ()
        return tuple(
            row_id
            for row_id, row in sorted(pending.items())
            if self._fresh_session((row.provider, row.session_id)) is not None
        )

    def has_pending_rows(self, chat_id: str) -> bool:
        return bool(self.pending_row_ids(chat_id))

    def complete_rows(self, chat_id: str, row_ids: Iterable[int]) -> None:
        self._finish_rows(chat_id, row_ids, completed=True)

    def discard_rows(self, chat_id: str, row_ids: Iterable[int]) -> None:
        self._finish_rows(chat_id, row_ids, completed=False)

    def excluded_row_ids_for_chat(self, chat_id: str) -> set[int]:
        self._expire_sessions()
        result = set(self._excluded.get(chat_id, ()))
        result.update(self._processed.get(chat_id, ()))
        result.update(self._pending.get(chat_id, ()))
        return result

    def filter_ordinary_messages(self, chat_id: str, messages: Iterable[Any]) -> list[Any]:
        excluded = self.excluded_row_ids_for_chat(chat_id)
        result = []
        for message in messages:
            declared, _ = parse_realtime_session(message)
            if declared or _row_id_from_message(message) in excluded:
                continue
            result.append(message)
        return result

    def _provider_enabled(self, provider: RealtimeProvider) -> bool:
        try:
            return provider.enabled() is True
        except Exception:
            return False

    def _apply_lifecycle_change(
        self,
        provider: RealtimeProvider,
        change: Any,
        chat_id: str,
    ) -> None:
        if (
            not isinstance(change, RealtimeLifecycleChange)
            or change.provider != provider.name
            or change.chat_id != chat_id
            or not _valid_opaque_id(change.session_id)
        ):
            return
        key = (change.provider, change.session_id)
        if change.active:
            if not self._provider_enabled(provider):
                return
            ttl = self.session_ttl_seconds if change.ttl_seconds is None else _bounded_ttl(change.ttl_seconds)
            self._activate_session(change.provider, change.session_id, chat_id, ttl)
        else:
            self._revoke_session(key)

    def _activate_session(self, provider: str, session_id: str, chat_id: str, ttl: float) -> None:
        key = (provider, session_id)
        previous = self._sessions.get(key)
        if previous is not None and previous.chat_id != chat_id:
            self._revoke_session(key)
        self._sessions[key] = _SessionLease(provider, session_id, chat_id, time.monotonic() + _bounded_ttl(ttl))
        self._sessions.move_to_end(key)
        while len(self._sessions) > self.session_limit:
            oldest = next(iter(self._sessions))
            self._revoke_session(oldest)

    def _fresh_session(self, key: tuple[str, str]) -> _SessionLease | None:
        lease = self._sessions.get(key)
        if lease is None:
            return None
        if lease.expires_at <= time.monotonic():
            self._revoke_session(key)
            return None
        provider = self._providers.get(lease.provider)
        if provider is None or not self._provider_enabled(provider):
            self._revoke_session(key)
            return None
        return lease

    def _expire_sessions(self) -> None:
        now = time.monotonic()
        for key, lease in tuple(self._sessions.items()):
            provider = self._providers.get(lease.provider)
            if lease.expires_at <= now or provider is None or not self._provider_enabled(provider):
                self._revoke_session(key)

    def _revoke_session(self, key: tuple[str, str]) -> None:
        lease = self._sessions.pop(key, None)
        self._bump_revocation_epoch(key)
        if lease is None:
            return
        pending = self._pending.get(lease.chat_id)
        removed: set[int] = set()
        if pending is not None:
            for row_id, row in tuple(pending.items()):
                if (row.provider, row.session_id) == key:
                    pending.pop(row_id, None)
                    removed.add(row_id)
                    self._exclude_row(lease.chat_id, row_id)
                    self._mark_turn(row, "revoked")
            if not pending:
                self._pending.pop(lease.chat_id, None)
        if removed:
            self._notify_focus("clear_realtime_priority_rows", lease.chat_id, removed)

    def _advance_latest_turn(self, binding: tuple[str, str], turn_id: str, sequence: int) -> None:
        previous = self._latest_turns.get(binding)
        if previous is not None and previous[0] == turn_id:
            self._latest_turns.move_to_end(binding)
            self._latest_turns[binding] = (turn_id, max(previous[1], sequence))
            return
        if previous is not None:
            self._mark_turn_key((*binding, previous[0]), "superseded")
            for chat_id, pending in tuple(self._pending.items()):
                removed = set()
                for row_id, row in tuple(pending.items()):
                    if (row.provider, row.session_id) == binding and row.turn_id != turn_id:
                        pending.pop(row_id, None)
                        removed.add(row_id)
                        self._exclude_row(chat_id, row_id)
                        self._mark_turn(row, "superseded")
                if not pending:
                    self._pending.pop(chat_id, None)
                if removed:
                    self._notify_focus("clear_realtime_priority_rows", chat_id, removed)
        self._latest_turns[binding] = (turn_id, sequence)
        self._latest_turns.move_to_end(binding)
        while len(self._latest_turns) > self.turn_limit:
            self._latest_turns.popitem(last=False)

    def _bump_revocation_epoch(self, key: tuple[str, str]) -> None:
        self._revocation_epochs[key] = self._revocation_epochs.get(key, 0) + 1
        self._revocation_epochs.move_to_end(key)
        while len(self._revocation_epochs) > self.session_limit * 2:
            self._revocation_epochs.popitem(last=False)

    def _finish_rows(self, chat_id: str, row_ids: Iterable[int], *, completed: bool) -> None:
        ids = {_positive_row_id(row_id) for row_id in row_ids}
        ids.discard(None)
        if not ids:
            return
        pending = self._pending.get(chat_id)
        completed_ids: set[int] = set()
        for row_id in ids:
            row = pending.pop(row_id, None) if pending is not None else None
            if row is not None:
                self._mark_turn(row, "processed" if completed else "rejected")
            if completed:
                self._remember(self._processed, chat_id, row_id, time.monotonic())
                completed_ids.add(row_id)
            else:
                self._exclude_row(chat_id, row_id)
        if pending is not None and not pending:
            self._pending.pop(chat_id, None)
        if completed_ids:
            self._notify_focus("complete_realtime_priority_rows", chat_id, completed_ids)
        elif ids:
            self._notify_focus("clear_realtime_priority_rows", chat_id, ids)

    def _mark_turn(self, row: _PendingRow, state: str) -> None:
        key = (row.provider, row.session_id, row.turn_id)
        self._mark_turn_key(key, state)

    def _mark_turn_key(self, key: tuple[str, str, str], state: str) -> None:
        self._turns[key] = state
        self._turns.move_to_end(key)
        while len(self._turns) > self.turn_limit:
            self._turns.popitem(last=False)

    def _exclude_row(self, chat_id: str, row_id: int | None) -> None:
        if row_id is not None:
            self._remember(self._excluded, chat_id, row_id, time.monotonic())

    def _trim_ledgers(self, chat_id: str) -> None:
        for mapping in (self._pending, self._excluded, self._processed):
            ledger = mapping.get(chat_id)
            if ledger is None:
                continue
            while len(ledger) > self.row_limit:
                ledger.popitem(last=False)
            if not ledger:
                mapping.pop(chat_id, None)

    def _remember(self, mapping: dict[str, OrderedDict[int, float]], chat_id: str, row_id: int, value: float) -> None:
        ledger = mapping.setdefault(chat_id, OrderedDict())
        ledger[row_id] = value
        ledger.move_to_end(row_id)
        while len(ledger) > self.row_limit:
            ledger.popitem(last=False)

    def _is_row_known(self, chat_id: str, row_id: int) -> bool:
        return (
            row_id in self._pending.get(chat_id, ())
            or row_id in self._excluded.get(chat_id, ())
            or row_id in self._processed.get(chat_id, ())
        )

    @staticmethod
    def _notify_focus(method_name: str, chat_id: str, row_ids: set[int]) -> None:
        """Resolve Focus lazily so its coordinator can import this manager."""

        try:
            from src.chat.focus.coordinator import focus_coordinator

            method = getattr(focus_coordinator, method_name)
            method(chat_id, row_ids)
        except Exception:
            logger.debug("Could not notify Focus about realtime rows", exc_info=True)


def _bounded_ttl(value: Any) -> float:
    if isinstance(value, bool):
        return _DEFAULT_SESSION_TTL_SECONDS
    try:
        ttl = float(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_SESSION_TTL_SECONDS
    if ttl <= 0 or ttl != ttl:
        return _DEFAULT_SESSION_TTL_SECONDS
    return min(ttl, _MAX_SESSION_TTL_SECONDS)


def _positive_row_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        row_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return row_id if row_id > 0 else None


def _row_id_from_message(message: Any) -> int | None:
    value = getattr(message, "focus_row_id", None)
    if value is None:
        value = getattr(message, "id", None)
    return _positive_row_id(value)


realtime_ingress_manager = RealtimeIngressManager()
