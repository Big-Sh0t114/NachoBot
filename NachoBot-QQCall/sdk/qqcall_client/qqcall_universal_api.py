"""QQCall binding to UniversalVC's frozen public audio API.

QQCall owns call controls, route proof, and the small PCM device transport.
UniversalVC owns VAD, ASR, and speech-result production. This module never
imports an audio/ML runtime and never decodes a speech payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ncnk_message.message_base import BaseMessageInfo, MessageBase, Seg, UserInfo

TRUSTED_INVITE_CORRELATIONS = frozenset({
    "kernel_from_uid_matched",
    "kernel_relation_uid_matched",
})
_CALL_KEYS = ("serviceInstance", "hostGeneration", "connectionGeneration", "callId", "callGeneration")
_SCOPE_KEYS = frozenset(_CALL_KEYS)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_ROUTE_AGE = 2.5
_ROUTE_CHECK_TIMEOUT = 1.0
_ROUTE_VERIFIER_TIMEOUT = 2.75
_ROUTE_REFRESH_MAX_DELAY = 0.25
_ROUTE_REFRESH_MIN_DELAY = 0.05
_CREATE_TIMEOUT = 20.0
_STARTUP_ROUTE_TIMEOUT = 20.0
_STARTUP_ROUTE_RETRY_DELAY = 0.25
_CLEANUP_TIMEOUT = 8.0
_MAX_TOKEN_BYTES = 64 * 1024
_MAX_ROUTE_DIAGNOSTIC_COUNT = (1 << 31) - 1
_MAX_ROUTE_DIAGNOSTIC_MS = 60_000
_ROUTE_DIAGNOSTIC_STAGES = frozenset({
    "scope_validation",
    "pre_context",
    "helper_request",
    "post_context",
    "proof_validation",
    "completed",
})
_ROUTE_HELPER_REJECTION_CODES = frozenset({
    "request_invalid",
    "platform_unsupported",
    "observer_busy",
    "observer_timeout",
    "com_initialization_failed",
    "observer_unavailable",
    "target_process_unavailable",
    "target_process_stale",
    "endpoint_not_found",
    "endpoint_ambiguous",
    "endpoint_metadata_unavailable",
    "session_enumeration_failed",
    "session_attribution_unavailable",
    "session_state_unavailable",
    "session_identity_unavailable",
    "shared_session_unverified",
    "routes_not_distinct",
    "host_input_not_active",
    "host_output_not_active",
    "endpoint_session_conflict",
    "observer_report_invalid",
    "observer_report_missing",
    "observer_aborted",
    "host_route_unverified",
})
_ROUTE_HELPER_TRANSPORT_ERROR_CODES = frozenset({
    "adapter_lease_required",
    "bridge_status_unavailable",
    "call_not_connected",
    "call_scope_stale",
    "call_occupied",
    "group_scope_stale",
    "request_timeout",
    "service_busy",
    "service_error_unclassified",
    "service_not_started",
    "service_redirect_rejected",
    "service_response_invalid",
    "service_stopped",
    "service_timeout",
    "service_unavailable",
    "unauthorized",
})
_STARTUP_ROUTE_PENDING_CODES = frozenset({"host_input_not_active", "host_output_not_active"})
_ROUTE_DIAGNOSTIC_FAILURES = frozenset({
    "capture_lifecycle_fence",
    "desired_identity_mismatch",
    "desired_scope_mismatch",
    "identity_check_error",
    "identity_check_false",
    "identity_check_timeout",
    "route_monitor_error",
    "route_check_timeout",
    "route_verifier_unavailable",
    "route_proof_stale",
    "route_proof_invalid",
    "route_verifier_error",
    "route_helper_transport_error",
    "route_helper_rejected_unclassified",
    "route_response_invalid",
    "route_scope_invalid",
    "route_scope_mismatch",
    "route_scope_unverified",
    "route_identity_unavailable",
    "route_identity_mismatch",
    "route_context_unverified",
    "route_plan_unavailable",
    "route_plan_changed",
    "route_plan_mismatch",
    "route_capture_mode_unverified",
    "route_device_scope_invalid",
    *_ROUTE_HELPER_REJECTION_CODES,
})
_CAPABILITIES = {"schema_version": 1, "reply_delivery": "tts_text", "voice_stream": True,
                 "planner_bypass": True, "mcp_enabled": False, "tts_language": "zh"}
_PCM_BACKEND_FAILURE_CODES = frozenset({
    "audio_fence_invalid",
    "audio_scope_stale",
    "capture_callback_failed",
    "capture_device_overflow",
    "capture_processing_failed",
    "capture_queue_overrun",
    "playback_callback_failed",
    "universal_api_capture_frame_invalid",
    "universal_api_capture_unsupported",
    "universal_api_protocol_invalid",
})


class QQCallVoiceError(RuntimeError):
    """Safe code-only failure raised by the QQCall voice binding."""

    def __init__(self, code: str):
        safe = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code) else "voice_failed"
        super().__init__(safe)
        self.code = safe


class _RouteCheckRejected(QQCallVoiceError):
    def __init__(self, reason: str):
        self.diagnostic_reason = reason
        super().__init__("universal_api_route_unverified")


class _StartupRoutePending(Exception):
    """A typed inactive endpoint that may be retried before first readiness."""

    def __init__(self, reason: str):
        self.reason = reason if reason in _STARTUP_ROUTE_PENDING_CODES else "host_input_not_active"


class _StartupScopeObsolete(Exception):
    """Internal signal preventing an obsolete startup from blocking its successor."""


class _RouteCheckObsolete(_StartupScopeObsolete):
    """A route check completed after its desired call turn became obsolete."""


class _RouteScopeRetired(Exception):
    """The current route verifier proved that this audio scope is unusable."""


@dataclass(frozen=True, slots=True)
class QQCallRouteProof:
    observed_at_monotonic: float


@dataclass(frozen=True, slots=True)
class QQCallRouteRetired:
    """Internal route-verifier result for an exact stale Bridge call scope."""


@dataclass(frozen=True, slots=True)
class QQCallRouteRejection:
    """Optional fixed-code detail for a false route-verifier outcome."""

    reason: str
    _helper_http_status: int | None = None
    _helper_error_code: str | None = None

    @classmethod
    def from_helper_code(cls, value: Any) -> "QQCallRouteRejection":
        if type(value) is str and value in _ROUTE_HELPER_REJECTION_CODES:
            return cls(value)
        return cls("route_helper_rejected_unclassified")

    def __post_init__(self) -> None:
        reason = self.reason
        if type(reason) is not str or reason not in _ROUTE_DIAGNOSTIC_FAILURES:
            reason = "route_helper_rejected_unclassified"
            object.__setattr__(self, "reason", reason)

        status = self._helper_http_status
        if reason != "route_helper_transport_error" or type(status) is not int or not 100 <= status <= 599:
            status = None
        object.__setattr__(self, "_helper_http_status", status)

        code = self._helper_error_code
        if reason != "route_helper_transport_error" or code is None:
            code = None
        elif type(code) is not str or code not in _ROUTE_HELPER_TRANSPORT_ERROR_CODES:
            code = "service_error_unclassified"
        object.__setattr__(self, "_helper_error_code", code)

    def __bool__(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class QQCallAudioTurn:
    scope: Mapping[str, Any]
    peer_user_id: str
    identity: Mapping[str, Any]

    @property
    def scope_key(self) -> tuple[Any, ...]:
        return tuple(self.scope.get(key) for key in _CALL_KEYS)


def is_trusted_invite_correlation(value: Any) -> bool:
    return isinstance(value, str) and value in TRUSTED_INVITE_CORRELATIONS


def _valid_scope(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != _SCOPE_KEYS:
        return None
    service, host, connection, call_id, generation = (value.get(key) for key in _CALL_KEYS)
    if (
        not isinstance(service, str) or not _SAFE_ID.fullmatch(service)
        or type(host) is not int or host < 1
        or type(connection) is not int or connection < 1
        or not isinstance(call_id, str) or not _SAFE_ID.fullmatch(call_id)
        or type(generation) is not int or generation < 1
    ):
        return None
    return {key: value[key] for key in _CALL_KEYS}


def _scope_key(scope: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(scope.get(key) for key in _CALL_KEYS)


def _realtime_session_id(scope: Mapping[str, Any]) -> str:
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _segments(raw: Any) -> list[tuple[str, Any]]:
    if isinstance(raw, Mapping):
        pending = [raw.get("message_segment")]
    else:
        pending = [getattr(raw, "message_segment", None)]
    result: list[tuple[str, Any]] = []
    while pending:
        current = pending.pop(0)
        if isinstance(current, Seg):
            kind, data = current.type, current.data
        elif isinstance(current, Mapping) and isinstance(current.get("type"), str):
            kind, data = current["type"], current.get("data")
        else:
            continue
        if kind == "seglist" and isinstance(data, list):
            pending[0:0] = data
        else:
            result.append((kind, data))
    return result


def _route_time(value: Any, *, on_rejection: Callable[[str], Any] | None = None) -> float:
    now = time.monotonic()
    if value is True:
        return now
    if isinstance(value, QQCallRouteProof):
        observed = value.observed_at_monotonic
    else:
        if on_rejection is not None:
            on_rejection("route_proof_invalid")
        raise QQCallVoiceError("universal_api_route_unverified")
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        if on_rejection is not None:
            on_rejection("route_proof_invalid")
        raise QQCallVoiceError("universal_api_route_unverified")
    try:
        finite = math.isfinite(observed)
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite or observed > now + 0.25:
        if on_rejection is not None:
            on_rejection("route_proof_invalid")
        raise QQCallVoiceError("universal_api_route_unverified")
    if now - observed > _MAX_ROUTE_AGE:
        if on_rejection is not None:
            on_rejection("route_proof_stale")
        raise QQCallVoiceError("universal_api_route_unverified")
    return float(observed)


def _read_token(path_value: str) -> str:
    if not isinstance(path_value, str) or not path_value.strip():
        raise QQCallVoiceError("universal_api_token_unavailable")
    try:
        with Path(path_value).open("rb") as stream:
            raw = stream.read(_MAX_TOKEN_BYTES + 1)
    except OSError:
        raise QQCallVoiceError("universal_api_token_unavailable") from None
    if len(raw) > _MAX_TOKEN_BYTES:
        raise QQCallVoiceError("universal_api_token_invalid")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise QQCallVoiceError("universal_api_token_invalid") from None
    if not token or len(token) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise QQCallVoiceError("universal_api_token_invalid")
    return token


def _awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


class QQCallVoiceRuntime:
    """One call-scoped UniversalVC session and QQPCM transport lease."""

    def __init__(
        self,
        *,
        platform: str,
        send_message: Callable[[MessageBase], Awaitable[Any]],
        admit_peer: Callable[[str], Any],
        identity_is_current: Callable[[Mapping[str, Any]], Any],
        universal_api: Mapping[str, Any] | None = None,
        backend: Any | None = None,
        backend_factory: Callable[[QQCallAudioTurn], Any] | None = None,
        route_verifier: Callable[[Mapping[str, Any], Any], Any] | None = None,
        on_status: Callable[[dict[str, Any]], Any] | None = None,
        asr_client: Any | None = None,
        vad: Any | None = None,
        session_factory: Callable[..., Any] | None = None,
        resolve_peer_info: Callable[[str], Any] | None = None,
    ) -> None:
        # asr_client and vad are accepted only for control-test source compatibility.
        del asr_client, vad
        self.platform = platform
        self._send_message = send_message
        self._admit_peer = admit_peer
        self._identity_is_current = identity_is_current
        self._api = dict(universal_api or {})
        self._backend = backend
        self._backend_factory = backend_factory
        self._route_verifier = route_verifier
        self._on_status = on_status or (lambda _status: None)
        self._session_factory = session_factory
        self._resolve_peer_info = resolve_peer_info
        self._peer_names: OrderedDict[tuple[Any, ...], str] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._supervisor: asyncio.Task[Any] | None = None
        self._startup_task: asyncio.Task[Any] | None = None
        self._route_task: asyncio.Task[Any] | None = None
        self._events_task: asyncio.Task[Any] | None = None
        self._stop = False
        self._desired: QQCallAudioTurn | None = None
        self._active_scope_key: tuple[Any, ...] | None = None
        self._ever_ready_scope_key: tuple[Any, ...] | None = None
        self._blocked_scope_key: tuple[Any, ...] | None = None
        self._retired_scope_key: tuple[Any, ...] | None = None
        self._route_diagnostic_scope_key: tuple[Any, ...] | None = None
        self._turn_lock = threading.RLock()
        self._route_diagnostics_lock = threading.Lock()
        self._route_diagnostics: dict[str, Any] = {
            "monitorStepMs": None,
            "proofAgeMs": None,
            "monitorChecks": 0,
            "identityChecks": 0,
            "failureCount": 0,
            "firstFailure": None,
            "routeCheckStage": None,
            "firstFailureStage": None,
            "routeHelperHttpStatus": None,
            "routeHelperErrorCode": None,
        }
        self._route_diagnostic_failures_seen: set[str] = set()
        self._turn_id: str | None = None
        self._turn_scope_key: tuple[Any, ...] | None = None
        self._capture_enabled = False
        self._session: Any | None = None
        self._client: Any | None = None
        self._create_task: asyncio.Task[Any] | None = None
        self._late_create_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._route_verified_at: float | None = None
        self._route_failed = False
        self._ready = False
        self._failure_code: str | None = None
        self._unavailable_reason = "audio_route_pending" if backend_factory is not None else "audio_disabled"
        self._scope_text: str | None = None
        self._active_turn: QQCallAudioTurn | None = None
        self._seen_results: OrderedDict[tuple[tuple[Any, ...], str], None] = OrderedDict()
        self._utterances_sent = 0
        self._utterances_dropped = 0
        self._playback_drained = 0
        self._last_failure_code: str | None = None

    def status(self) -> dict[str, Any]:
        diagnostic_scope_key = self._current_route_diagnostic_scope_key()
        if self._ready and not self._route_is_fresh():
            self._record_current_route_age(diagnostic_scope_key)
            self._record_route_diagnostic_failure("route_proof_stale", diagnostic_scope_key)
            self._revoke_route_sync(
                "universal_api_route_unverified",
                expected_scope_key=diagnostic_scope_key,
            )
        backend_status = getattr(self._backend, "status", {})
        if not isinstance(backend_status, Mapping):
            backend_status = {}
        return {
            "configured": self._backend is not None or self._backend_factory is not None,
            "ready": self._ready,
            "pipeline": "universal_api",
            "unavailableReason": self._unavailable_reason,
            "lastFailureCode": self._failure_code,
            "capturing": self._capture_enabled,
            "activeTurn": self.current_turn_id(),
            "framesReceived": backend_status.get("capture_sample_counter", 0),
            "framesSent": backend_status.get("uplink_frames_sent", 0),
            "routeDiagnostics": self._route_diagnostics_snapshot(),
            "utterancesSent": self._utterances_sent,
            "utterancesDropped": self._utterances_dropped,
            "playbackDrainCount": self._playback_drained,
        }

    def current_turn_id(self, scope: Mapping[str, Any] | None = None) -> str | None:
        with self._turn_lock:
            if scope is not None and self._turn_scope_key != _scope_key(scope):
                return None
            return self._turn_id

    def fence(self, scope: Mapping[str, Any], peer_user_id: str, turn_id: str) -> bool:
        diagnostic_scope_key = self._current_route_diagnostic_scope_key()
        if self._ready and not self._route_is_fresh():
            self._record_current_route_age(diagnostic_scope_key)
            self._record_route_diagnostic_failure("route_proof_stale", diagnostic_scope_key)
            self._revoke_route_sync(
                "universal_api_route_unverified",
                expected_scope_key=_scope_key(scope),
            )
        with self._turn_lock:
            desired = self._desired
            return bool(
                self._ready and self._capture_enabled and self._route_is_fresh()
                and desired is not None and desired.scope_key == _scope_key(scope)
                and desired.peer_user_id == peer_user_id
                and self._turn_scope_key == desired.scope_key and self._turn_id == turn_id
            )

    def update_call(self, call: Mapping[str, Any] | None, identity: Mapping[str, Any] | None) -> None:
        next_turn: QQCallAudioTurn | None = None
        if isinstance(call, Mapping) and isinstance(identity, Mapping):
            scope = _valid_scope(call.get("scope"))
            peer = call.get("peer_user_id")
            if (
                scope is not None and isinstance(peer, str) and peer.isdigit()
                and call.get("phase") == "connected" and call.get("uncertain") is False
                and call.get("inviteCallbackReady") is True
                and is_trusted_invite_correlation(call.get("inviteCorrelation"))
                and isinstance(call.get("peer_uid"), str)
            ):
                next_turn = QQCallAudioTurn(scope, peer, dict(identity))
        with self._route_diagnostics_lock:
            if next_turn is not None and next_turn.scope_key == self._retired_scope_key:
                # Ignore repeated lagging polls while this exact Bridge scope
                # remains the single retirement fence.
                return
            if next_turn is not None and self._retired_scope_key is not None:
                # Keep one retirement fence only. Once a different valid
                # scope takes ownership, the desired/session/diagnostic gates
                # continue to reject work bound to the retired scope.
                self._retired_scope_key = None
            old = self._desired
            changed = (
                (old.scope_key if old else None) != (next_turn.scope_key if next_turn else None)
                or bool(old and next_turn and (
                    old.identity != next_turn.identity or old.peer_user_id != next_turn.peer_user_id
                ))
            )
            if next_turn is not None:
                self._bind_route_diagnostics_scope_locked(next_turn.scope_key)
                if (
                    self._ever_ready_scope_key is not None
                    and self._ever_ready_scope_key != next_turn.scope_key
                ):
                    self._ever_ready_scope_key = None
            if changed:
                self._invalidate_turn_sync()
                self._route_verified_at = None
                self._route_failed = False
                self._ready = False
                self._failure_code = None
                self._unavailable_reason = "audio_route_pending" if next_turn else "call_not_connected"
                if next_turn is None:
                    self._active_turn = None
            self._desired = next_turn
        self._wake_from_sync()

    def start_supervisor(self) -> None:
        if self._stop:
            self._unavailable_reason = "runtime_stopped"
            return
        self._loop = asyncio.get_running_loop()
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise(), name="qqcall-universal-api-supervisor")

    def _wake_from_sync(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            if asyncio.get_running_loop() is loop:
                self._wake.set()
            else:
                loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            try:
                loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    def _desired_matches(self, desired: QQCallAudioTurn) -> bool:
        current = self._desired
        return bool(not self._stop and current is not None and current.scope_key == desired.scope_key
                    and current.peer_user_id == desired.peer_user_id and current.identity == desired.identity)

    def _route_check_current(self, desired: QQCallAudioTurn) -> bool:
        # update_call serializes desired-turn replacement with diagnostic writes.
        with self._route_diagnostics_lock:
            return self._desired_matches(desired)

    def _desired_mismatch_reason(self, desired: QQCallAudioTurn) -> str:
        if self._stop:
            return "capture_lifecycle_fence"
        current = self._desired
        if current is None or current.scope_key != desired.scope_key or current.peer_user_id != desired.peer_user_id:
            return "desired_scope_mismatch"
        if current.identity != desired.identity:
            return "desired_identity_mismatch"
        return "capture_lifecycle_fence"

    @staticmethod
    def _bounded_route_diagnostic_ms(value: float) -> int:
        if not math.isfinite(value):
            return _MAX_ROUTE_DIAGNOSTIC_MS
        return max(-250, min(_MAX_ROUTE_DIAGNOSTIC_MS, int(value)))

    def _route_proof_age_ms(self) -> int | None:
        observed = self._route_verified_at
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            return None
        try:
            observed_at = float(observed)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(observed_at):
            return None
        return self._bounded_route_diagnostic_ms((time.monotonic() - observed_at) * 1000)

    def _current_route_diagnostic_scope_key(self) -> tuple[Any, ...] | None:
        desired = self._desired
        if desired is not None:
            return desired.scope_key
        return self._active_scope_key

    def _record_current_route_age(self, scope_key: tuple[Any, ...] | None) -> None:
        age_ms = self._route_proof_age_ms()
        with self._route_diagnostics_lock:
            if scope_key is None or scope_key != self._route_diagnostic_scope_key:
                return
            self._route_diagnostics["proofAgeMs"] = age_ms

    def _record_route_diagnostic_failure(
        self,
        reason: str,
        scope_key: tuple[Any, ...] | None,
        *,
        desired: QQCallAudioTurn | None = None,
        route_helper_http_status: Any = None,
        route_helper_error_code: Any = None,
    ) -> bool:
        if not isinstance(reason, str) or reason not in _ROUTE_DIAGNOSTIC_FAILURES:
            return False
        with self._route_diagnostics_lock:
            if scope_key is None or scope_key != self._route_diagnostic_scope_key:
                return False
            if scope_key == self._retired_scope_key:
                return False
            if desired is not None and not self._desired_matches(desired):
                return False
            if self._route_diagnostics["firstFailure"] is None:
                self._route_diagnostics["firstFailure"] = reason
                self._route_diagnostics["firstFailureStage"] = (
                    self._route_diagnostics["routeCheckStage"]
                    if reason == "route_check_timeout"
                    else None
                )
                if reason == "route_helper_transport_error":
                    self._route_diagnostics["routeHelperHttpStatus"] = (
                        route_helper_http_status
                        if type(route_helper_http_status) is int and 100 <= route_helper_http_status <= 599
                        else None
                    )
                    if route_helper_error_code is None:
                        safe_error_code = None
                    elif (
                        type(route_helper_error_code) is str
                        and route_helper_error_code in _ROUTE_HELPER_TRANSPORT_ERROR_CODES
                    ):
                        safe_error_code = route_helper_error_code
                    else:
                        safe_error_code = "service_error_unclassified"
                    self._route_diagnostics["routeHelperErrorCode"] = safe_error_code
            if reason not in self._route_diagnostic_failures_seen:
                self._route_diagnostic_failures_seen.add(reason)
                self._route_diagnostics["failureCount"] = min(
                    _MAX_ROUTE_DIAGNOSTIC_COUNT,
                    self._route_diagnostics["failureCount"] + 1,
                )
            return True

    def _record_route_check_stage(self, stage: Any, scope_key: tuple[Any, ...] | None) -> None:
        if type(stage) is not str or stage not in _ROUTE_DIAGNOSTIC_STAGES:
            return
        with self._route_diagnostics_lock:
            if scope_key is None or scope_key != self._route_diagnostic_scope_key:
                return
            desired = self._desired
            if desired is None or desired.scope_key != scope_key:
                return
            self._route_diagnostics["routeCheckStage"] = stage

    def make_route_stage_recorder(self, scope: Any) -> Callable[[str], None]:
        """Create a stage writer permanently bound to one validated call scope."""
        try:
            valid_scope = _valid_scope(scope)
            if valid_scope is None:
                return lambda _stage: None
            scope_key = _scope_key(valid_scope)
            with self._route_diagnostics_lock:
                if scope_key != self._route_diagnostic_scope_key:
                    return lambda _stage: None
        except Exception:
            return lambda _stage: None

        def record(stage: str) -> None:
            if type(stage) is not str or stage not in _ROUTE_DIAGNOSTIC_STAGES:
                return
            try:
                self._record_route_check_stage(stage, scope_key)
            except Exception:
                # Diagnostic instrumentation must never interfere with routing.
                return

        return record

    def _reject_route_check(
        self,
        reason: Any,
        *,
        scope_key: tuple[Any, ...] | None,
        desired: QQCallAudioTurn | None = None,
        route_helper_http_status: Any = None,
        route_helper_error_code: Any = None,
    ) -> None:
        safe_reason = reason if type(reason) is str and reason in _ROUTE_DIAGNOSTIC_FAILURES else "route_proof_invalid"
        recorded = self._record_route_diagnostic_failure(
            safe_reason,
            scope_key,
            desired=desired,
            route_helper_http_status=route_helper_http_status,
            route_helper_error_code=route_helper_error_code,
        )
        if desired is not None and not recorded:
            raise _RouteCheckObsolete
        raise _RouteCheckRejected(safe_reason)

    def _begin_identity_diagnostic(self, scope_key: tuple[Any, ...] | None) -> None:
        with self._route_diagnostics_lock:
            if scope_key is None or scope_key != self._route_diagnostic_scope_key:
                return
            self._route_diagnostics["identityChecks"] = min(
                _MAX_ROUTE_DIAGNOSTIC_COUNT,
                self._route_diagnostics["identityChecks"] + 1,
            )

    def _finish_monitor_diagnostic(
        self,
        scope_key: tuple[Any, ...] | None,
        started_at: float,
        *,
        observed_at: float | None = None,
    ) -> None:
        elapsed_ms = self._bounded_route_diagnostic_ms((time.monotonic() - started_at) * 1000)
        if observed_at is None:
            proof_age_ms = self._route_proof_age_ms()
        else:
            proof_age_ms = self._bounded_route_diagnostic_ms((time.monotonic() - observed_at) * 1000)
        with self._route_diagnostics_lock:
            if scope_key is None or scope_key != self._route_diagnostic_scope_key:
                return
            self._route_diagnostics["monitorStepMs"] = elapsed_ms
            self._route_diagnostics["proofAgeMs"] = proof_age_ms
            self._route_diagnostics["monitorChecks"] = min(
                _MAX_ROUTE_DIAGNOSTIC_COUNT,
                self._route_diagnostics["monitorChecks"] + 1,
            )

    def _route_diagnostics_snapshot(self) -> dict[str, Any]:
        with self._route_diagnostics_lock:
            return dict(self._route_diagnostics)

    def _bind_route_diagnostics_scope(self, scope_key: tuple[Any, ...]) -> None:
        if not isinstance(scope_key, tuple) or len(scope_key) != len(_CALL_KEYS):
            return
        with self._route_diagnostics_lock:
            self._bind_route_diagnostics_scope_locked(scope_key)

    def _bind_route_diagnostics_scope_locked(self, scope_key: tuple[Any, ...]) -> None:
        if not isinstance(scope_key, tuple) or len(scope_key) != len(_CALL_KEYS):
            return
        if scope_key == self._route_diagnostic_scope_key:
            return
        self._route_diagnostic_scope_key = scope_key
        self._route_diagnostics.update({
            "monitorStepMs": None,
            "proofAgeMs": None,
            "monitorChecks": 0,
            "identityChecks": 0,
            "failureCount": 0,
            "firstFailure": None,
            "routeCheckStage": None,
            "firstFailureStage": None,
            "routeHelperHttpStatus": None,
            "routeHelperErrorCode": None,
        })
        self._route_diagnostic_failures_seen.clear()

    async def _turn_current(
        self,
        desired: QQCallAudioTurn,
        *,
        deadline: float | None = None,
    ) -> bool:
        scope_key = desired.scope_key
        self._begin_identity_diagnostic(scope_key)
        if not self._desired_matches(desired):
            self._record_route_diagnostic_failure(self._desired_mismatch_reason(desired), scope_key)
            self._record_current_route_age(scope_key)
            return False
        try:
            check = self._identity_is_current(desired.identity)
            if _awaitable(check):
                timeout = _ROUTE_CHECK_TIMEOUT
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                if timeout <= 0:
                    raise asyncio.TimeoutError
                check = await asyncio.wait_for(check, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._record_route_diagnostic_failure("identity_check_timeout", scope_key, desired=desired)
            self._record_current_route_age(scope_key)
            return False
        except Exception:
            self._record_route_diagnostic_failure("identity_check_error", scope_key, desired=desired)
            self._record_current_route_age(scope_key)
            return False
        if check is not True:
            self._record_route_diagnostic_failure("identity_check_false", scope_key, desired=desired)
            self._record_current_route_age(scope_key)
            return False
        still_current = self._desired_matches(desired)
        if not still_current:
            self._record_route_diagnostic_failure(self._desired_mismatch_reason(desired), scope_key, desired=desired)
            self._record_current_route_age(scope_key)
            return False
        return check is True and still_current

    async def _admitted(self, desired: QQCallAudioTurn, *, deadline: float | None = None) -> bool:
        try:
            result = self._admit_peer(desired.peer_user_id)
            if _awaitable(result):
                timeout = _ROUTE_CHECK_TIMEOUT
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                if timeout <= 0:
                    return False
                result = await asyncio.wait_for(result, timeout=timeout)
        except Exception:
            return False
        return result is True and self._desired_matches(desired)

    async def _make_backend(self, desired: QQCallAudioTurn) -> Any:
        backend = self._backend
        if backend is None:
            if self._backend_factory is None:
                raise QQCallVoiceError("universal_api_capture_unsupported")
            result = self._backend_factory(desired)
            if _awaitable(result):
                result = await asyncio.wait_for(result, timeout=_CREATE_TIMEOUT)
            if result is None:
                raise QQCallVoiceError("universal_api_capture_unsupported")
            if not self._desired_matches(desired):
                await self._dispose_backend(result)
                raise QQCallVoiceError("audio_scope_stale")
            self._backend = result
            backend = result
        return backend

    async def _dispose_backend(self, backend: Any) -> None:
        try:
            result = backend.stop()
            if _awaitable(result):
                await asyncio.wait_for(result, timeout=_CLEANUP_TIMEOUT)
        except Exception:
            pass

    async def _verify_route(
        self,
        desired: QQCallAudioTurn,
        *,
        startup_pending: bool = False,
        deadline: float | None = None,
    ) -> float:
        scope_key = desired.scope_key
        if not self._route_check_current(desired):
            raise _RouteCheckObsolete
        self._record_route_check_stage("scope_validation", scope_key)
        if self._route_verifier is None:
            self._reject_route_check("route_verifier_unavailable", scope_key=scope_key, desired=desired)
        if startup_pending and deadline is not None and deadline - time.monotonic() <= 0:
            self._reject_route_check("route_check_timeout", scope_key=scope_key, desired=desired)
        try:
            value = self._route_verifier(desired.scope, getattr(self._backend, "config", None))
            if _awaitable(value):
                timeout = _ROUTE_VERIFIER_TIMEOUT
                if startup_pending and deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                value = await asyncio.wait_for(value, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._reject_route_check("route_check_timeout", scope_key=scope_key, desired=desired)
        except Exception:
            self._reject_route_check("route_verifier_error", scope_key=scope_key, desired=desired)
        if not self._route_check_current(desired):
            raise _RouteCheckObsolete
        if isinstance(value, QQCallRouteRetired):
            if not self._retire_route_scope(desired):
                raise _RouteCheckObsolete
            raise _RouteScopeRetired
        if isinstance(value, QQCallRouteRejection):
            reason = value.reason
            if type(reason) is not str or reason not in _ROUTE_DIAGNOSTIC_FAILURES:
                reason = "route_helper_rejected_unclassified"
            if (
                startup_pending
                and reason in _STARTUP_ROUTE_PENDING_CODES
                and self._ever_ready_scope_key != scope_key
            ):
                raise _StartupRoutePending(reason)
            self._reject_route_check(
                reason,
                scope_key=scope_key,
                desired=desired,
                route_helper_http_status=value._helper_http_status,
                route_helper_error_code=value._helper_error_code,
            )
        return _route_time(
            value,
            on_rejection=lambda reason: self._reject_route_check(
                reason, scope_key=scope_key, desired=desired,
            ),
        )

    def _retire_route_scope(self, desired: QQCallAudioTurn) -> bool:
        """Retire one still-current audio scope without classifying it as a failure."""
        scope_key = desired.scope_key
        with self._route_diagnostics_lock:
            if not self._desired_matches(desired) or self._route_failed:
                return False
            self._retired_scope_key = scope_key
            self._blocked_scope_key = scope_key
            self._desired = None
            self._route_verified_at = None
            self._ready = False
            self._capture_enabled = False
            self._failure_code = None
            self._unavailable_reason = "call_not_connected"
            self._active_turn = None
            self._invalidate_turn_sync()

        # The serialized supervisor owns session/client/backend teardown. This
        # synchronous fence revokes old output immediately and wakes that owner.
        self._cancel_old_playback()
        self._wake_from_sync()
        self._emit_status("audio_unavailable")
        return True

    async def _verify_startup_route(self, desired: QQCallAudioTurn, *, deadline: float) -> float:
        """Wait briefly for first native endpoint readiness under one startup budget."""
        scope_key = desired.scope_key
        last_inactive: str | None = None

        async def check_identity_and_acl() -> None:
            if not self._desired_matches(desired):
                raise _StartupScopeObsolete
            if not await self._turn_current(desired, deadline=deadline):
                if not self._desired_matches(desired):
                    raise _StartupScopeObsolete
                raise QQCallVoiceError("audio_scope_stale")
            if not self._desired_matches(desired):
                raise _StartupScopeObsolete
            if not await self._admitted(desired, deadline=deadline):
                if not self._desired_matches(desired):
                    raise _StartupScopeObsolete
                raise QQCallVoiceError("peer_not_admitted")
            if not self._desired_matches(desired):
                raise _StartupScopeObsolete

        while True:
            if not self._desired_matches(desired):
                raise _StartupScopeObsolete
            if time.monotonic() >= deadline:
                if last_inactive is not None:
                    self._reject_route_check(last_inactive, scope_key=scope_key, desired=desired)
                raise QQCallVoiceError("universal_api_route_unverified")

            await check_identity_and_acl()
            if time.monotonic() >= deadline:
                if last_inactive is not None:
                    self._reject_route_check(last_inactive, scope_key=scope_key, desired=desired)
                raise QQCallVoiceError("universal_api_route_unverified")
            try:
                observed = await self._verify_route(
                    desired,
                    startup_pending=True,
                    deadline=deadline,
                )
            except _RouteScopeRetired:
                raise
            except _StartupRoutePending as pending:
                last_inactive = pending.reason
                if not self._desired_matches(desired):
                    raise _StartupScopeObsolete from None
                if time.monotonic() >= deadline:
                    self._reject_route_check(last_inactive, scope_key=scope_key, desired=desired)
                await check_identity_and_acl()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._reject_route_check(last_inactive, scope_key=scope_key, desired=desired)
                delay = min(_STARTUP_ROUTE_RETRY_DELAY, remaining)
                await asyncio.sleep(delay)
                continue

            if not self._desired_matches(desired):
                raise _StartupScopeObsolete
            if time.monotonic() >= deadline:
                raise QQCallVoiceError("universal_api_route_unverified")
            await check_identity_and_acl()
            if time.monotonic() >= deadline:
                raise QQCallVoiceError("universal_api_route_unverified")
            return observed

    def _route_is_fresh(self) -> bool:
        observed = self._route_verified_at
        return bool(
            isinstance(observed, (int, float)) and not isinstance(observed, bool)
            and math.isfinite(observed) and -0.25 <= time.monotonic() - observed <= _MAX_ROUTE_AGE
        )

    def _route_refresh_delay(self) -> float:
        """Refresh near the proof-age limit of the observer's positive cache."""
        observed = self._route_verified_at
        if (
            isinstance(observed, bool) or not isinstance(observed, (int, float))
            or not math.isfinite(observed)
        ):
            return _ROUTE_REFRESH_MIN_DELAY
        age = max(0.0, time.monotonic() - observed)
        # Reuse is permitted only while the native observation is at most
        # 250ms old. Refresh toward that boundary with a small lower bound to
        # prevent a tight loop when the proof is already aged.
        until_due = 0.25 - age
        return max(_ROUTE_REFRESH_MIN_DELAY, min(_ROUTE_REFRESH_MAX_DELAY, until_due))

    async def _new_client(self) -> Any:
        if self._session_factory is not None:
            value = self._session_factory(self._api)
            if _awaitable(value):
                value = await value
            return value
        from universalvc_client import UniversalVCClient
        url = self._api.get("url", "http://127.0.0.1:8016")
        token_file = self._api.get("token_file", "")
        token = _read_token(token_file) if isinstance(token_file, str) and token_file.strip() else None
        return UniversalVCClient(url, token=token)

    def _session_options(self, desired: QQCallAudioTurn) -> dict[str, Any]:
        session_id = _realtime_session_id(desired.scope)
        peer = desired.peer_user_id
        return {
            "platform": "qq",
            "scope": session_id,
            "mode": "pipeline",
            "sources": [{
                "id": "main",
                "input": "external",
                "format": "s16le",
                "sample_rate": 16_000,
                "channels": 1,
                "capture": {},
                "identity": {"mode": "fixed", "id": peer, "name": peer},
            }],
            "output": {"mode": "none"},
            "denoise": False,
            "speaker": False,
            "speaker_log": False,
            "max_duration_seconds": 60,
        }

    async def _late_create_cleanup(self, create_task: asyncio.Task[Any], client: Any | None) -> None:
        try:
            session = await create_task
        except BaseException:
            session = None
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass

    def _schedule_late_create_cleanup(
        self,
        create_task: asyncio.Task[Any],
        client: Any | None,
    ) -> asyncio.Task[Any]:
        cleanup = asyncio.get_running_loop().create_task(
            self._late_create_cleanup(create_task, client),
            name="qqcall-universal-late-create-cleanup",
        )
        self._late_create_cleanup_tasks.add(cleanup)

        def forget(done: asyncio.Task[Any]) -> None:
            self._late_create_cleanup_tasks.discard(done)
            _consume_task_result(done)

        cleanup.add_done_callback(forget)
        return cleanup

    async def _wait_for_late_create_cleanups(self) -> None:
        pending = tuple(self._late_create_cleanup_tasks)
        if pending:
            # asyncio.wait leaves unfinished cleanup tasks running and tracked;
            # stop waits only within the existing resource-cleanup budget.
            await asyncio.wait(pending, timeout=_CLEANUP_TIMEOUT)

    async def _close_rejected_startup_session(self, client: Any, session: Any) -> None:
        """Finish cleanup of a completed but rejected session despite owner cancellation."""
        async def close_owned_resources() -> None:
            try:
                await asyncio.wait_for(session.interrupt(), timeout=2.0)
            except Exception:
                pass
            try:
                await asyncio.wait_for(session.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass
            try:
                await asyncio.wait_for(client.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass

        cleanup = asyncio.create_task(close_owned_resources())
        cancellation_requested = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The cleanup task owns the SDK resources until closure finishes.
                cancellation_requested = True
            except Exception:
                break
        try:
            cleanup.result()
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception:
            pass

        if self._session is session:
            self._session = None
        if self._client is client:
            self._client = None
        if cancellation_requested:
            raise asyncio.CancelledError

    async def _create_session(self, desired: QQCallAudioTurn, *, deadline: float) -> tuple[Any, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QQCallVoiceError("universal_api_route_unverified")
        try:
            client = await asyncio.wait_for(self._new_client(), timeout=remaining)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise QQCallVoiceError("universal_api_route_unverified") from None
        # Keep ownership visible to _stop_resources before any further await.
        self._client = client
        if not self._desired_matches(desired):
            try:
                await asyncio.wait_for(client.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass
            if self._client is client:
                self._client = None
            raise _StartupScopeObsolete

        current = await self._turn_current(desired, deadline=deadline)
        admitted = await self._admitted(desired, deadline=deadline)
        if not current or not admitted:
            try:
                await asyncio.wait_for(client.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass
            if self._client is client:
                self._client = None
            if not self._desired_matches(desired):
                raise _StartupScopeObsolete
            raise QQCallVoiceError("audio_scope_stale" if not current else "peer_not_admitted")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                await asyncio.wait_for(client.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass
            if self._client is client:
                self._client = None
            raise QQCallVoiceError("universal_api_route_unverified")

        create = asyncio.create_task(client.create_session(self._session_options(desired)))
        self._create_task = create
        cancelled_while_waiting = False
        try:
            session = await asyncio.wait_for(
                asyncio.shield(create),
                timeout=min(_CREATE_TIMEOUT, remaining),
            )
        except asyncio.CancelledError:
            # Keep the completed-or-pending result owned until stop_resources
            # transfers it to late cleanup. Clearing a completed task here can
            # lose a session returned concurrently with this cancellation.
            cancelled_while_waiting = True
            raise
        except Exception:
            deadline_expired = time.monotonic() >= deadline
            cleanup = self._schedule_late_create_cleanup(create, client)
            if self._client is client:
                self._client = None
            if self._create_task is create:
                self._create_task = None
            if create.done():
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), timeout=_CLEANUP_TIMEOUT)
                except asyncio.TimeoutError:
                    pass
            if deadline_expired:
                raise QQCallVoiceError("universal_api_route_unverified") from None
            raise
        finally:
            if not cancelled_while_waiting and self._create_task is create and create.done():
                self._create_task = None

        self._session = session
        current = await self._turn_current(desired, deadline=deadline)
        admitted = await self._admitted(desired, deadline=deadline)
        if not current or not admitted:
            await self._close_rejected_startup_session(client, session)
            if not self._desired_matches(desired):
                raise _StartupScopeObsolete
            raise QQCallVoiceError("audio_scope_stale" if not current else "peer_not_admitted")
        if time.monotonic() >= deadline:
            raise QQCallVoiceError("universal_api_route_unverified")
        return client, session

    async def _start_scope(self, desired: QQCallAudioTurn) -> None:
        if not await self._turn_current(desired):
            return
        if not await self._admitted(desired):
            raise QQCallVoiceError("peer_not_admitted")
        backend = await self._make_backend(desired)
        if not await self._turn_current(desired):
            await self._dispose_backend(backend)
            if self._backend is backend:
                self._backend = None
            return
        # The bounded native-readiness trial starts only after backend creation
        # and its identity recheck; backend creation keeps its existing budget.
        startup_deadline = time.monotonic() + _STARTUP_ROUTE_TIMEOUT
        self._route_verified_at = await self._verify_startup_route(desired, deadline=startup_deadline)
        if not await self._turn_current(desired, deadline=startup_deadline) or not self._route_is_fresh():
            raise QQCallVoiceError("universal_api_route_unverified")
        client, session = await self._create_session(desired, deadline=startup_deadline)
        self._client, self._session = client, session
        # Session creation may wait on the service. Revalidate the proven host
        # route immediately before the first device open.
        self._route_verified_at = await self._verify_startup_route(desired, deadline=startup_deadline)
        if (
            time.monotonic() >= startup_deadline
            or not await self._turn_current(desired, deadline=startup_deadline)
            or not await self._admitted(desired, deadline=startup_deadline)
            or not self._route_is_fresh()
        ):
            raise QQCallVoiceError("universal_api_route_unverified")
        if time.monotonic() >= startup_deadline:
            raise QQCallVoiceError("universal_api_route_unverified")
        if not self._desired_matches(desired):
            raise _StartupScopeObsolete
        self._scope_text = json.dumps(desired.scope, sort_keys=True, separators=(",", ":"))
        self._active_turn = desired
        try:
            if time.monotonic() >= startup_deadline:
                raise QQCallVoiceError("universal_api_route_unverified")
            self._capture_enabled = True
            await backend.start(
                None,
                lambda error: self._backend_failed(error, desired=desired),
                scope=self._scope_text,
                send_pcm=session.send_pcm,
                capture_fence=lambda: self._capture_transport_fence(desired, session),
            )
            if not await self._turn_current(desired) or not self._route_is_fresh():
                raise QQCallVoiceError("universal_api_route_unverified")
            self._active_scope_key = desired.scope_key
            self._ever_ready_scope_key = desired.scope_key
            self._failure_code = None
            self._last_failure_code = None
            self._unavailable_reason = ""
            self._ready = True
            self._events_task = asyncio.create_task(self._events_worker(desired, session), name="qqcall-universal-events")
            self._route_task = asyncio.create_task(self._route_monitor(desired), name="qqcall-universal-route")
            self._emit_status("audio_ready")
        except BaseException:
            self._capture_enabled = False
            await self._stop_resources(interrupt=True)
            raise

    async def _start_scope_until_obsolete(self, desired: QQCallAudioTurn) -> bool:
        """Run one startup and cancel it promptly when its desired turn changes."""
        task = asyncio.create_task(self._start_scope(desired), name="qqcall-universal-startup")
        self._startup_task = task
        wake_task: asyncio.Task[Any] | None = None

        async def cancel_obsolete_startup() -> None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self._stop_resources(interrupt=True)

        try:
            while True:
                if not self._desired_matches(desired):
                    await cancel_obsolete_startup()
                    return False
                if task.done():
                    try:
                        task.result()
                    except _RouteScopeRetired:
                        # Startup still owns these references. Drain them here
                        # before the supervisor can start a replacement scope.
                        await self._stop_resources(interrupt=True)
                        return False
                    except _StartupScopeObsolete:
                        await self._stop_resources(interrupt=True)
                        return False
                    except asyncio.CancelledError:
                        if self._stop or not self._desired_matches(desired):
                            await self._stop_resources(interrupt=True)
                            return False
                        raise
                    except Exception:
                        if not self._desired_matches(desired):
                            await self._stop_resources(interrupt=True)
                            return False
                        raise
                    if not self._desired_matches(desired):
                        await self._stop_resources(interrupt=True)
                        return False
                    return True

                self._wake.clear()
                if not self._desired_matches(desired):
                    continue
                wake_task = asyncio.create_task(self._wake.wait())
                done, _pending = await asyncio.wait(
                    (task, wake_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
                    wake_task = None
                    continue
                wake_task = None
        except asyncio.CancelledError:
            await cancel_obsolete_startup()
            raise
        finally:
            if wake_task is not None and not wake_task.done():
                wake_task.cancel()
                await asyncio.gather(wake_task, return_exceptions=True)
            if self._startup_task is task:
                self._startup_task = None

    async def _supervise(self) -> None:
        while not self._stop:
            desired = self._desired
            if self._active_scope_key is not None and (
                desired is None or desired.scope_key != self._active_scope_key or self._route_failed
            ):
                await self._stop_resources(interrupt=True)
            desired = self._desired
            if desired is not None and self._active_scope_key is None and desired.scope_key != self._blocked_scope_key:
                self._unavailable_reason = "audio_route_pending"
                try:
                    started = await self._start_scope_until_obsolete(desired)
                    if not started:
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._desired_matches(desired):
                        await self._stop_resources(interrupt=True)
                        continue
                    code = exc.code if isinstance(exc, QQCallVoiceError) else getattr(exc, "code", None)
                    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code):
                        code = "universal_api_unavailable"
                    if code in {"audio_plan_unavailable", "audio_plan_invalid", "audio_backend_unavailable"}:
                        code = "universal_api_route_unverified"
                    self._failure_code = self._unavailable_reason = code
                    self._blocked_scope_key = desired.scope_key
                    self._ready = False
                    self._capture_enabled = False
                    await self._stop_resources(interrupt=True)
                    self._emit_status("audio_unavailable")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
        await self._stop_resources(interrupt=True)

    async def _route_monitor(self, desired: QQCallAudioTurn) -> None:
        step_started: float | None = None
        try:
            while not self._stop and self._ready and self._active_scope_key == desired.scope_key:
                step_started = None
                await asyncio.sleep(self._route_refresh_delay())
                step_started = time.monotonic()
                if not self._route_check_current(desired):
                    self._finish_monitor_diagnostic(desired.scope_key, step_started)
                    return
                if not await self._turn_current(desired):
                    self._finish_monitor_diagnostic(desired.scope_key, step_started)
                    if not self._route_check_current(desired):
                        return
                    self._revoke_route_for_desired(desired, "audio_scope_stale")
                    return
                observed = await self._verify_route(desired)
                if not self._route_check_current(desired):
                    self._finish_monitor_diagnostic(desired.scope_key, step_started)
                    return
                if not await self._turn_current(desired):
                    self._finish_monitor_diagnostic(desired.scope_key, step_started)
                    if not self._route_check_current(desired):
                        return
                    self._revoke_route_for_desired(desired, "universal_api_route_unverified")
                    return
                if self._active_scope_key != desired.scope_key:
                    self._record_route_diagnostic_failure("desired_scope_mismatch", desired.scope_key, desired=desired)
                    self._finish_monitor_diagnostic(desired.scope_key, step_started)
                    if not self._route_check_current(desired):
                        return
                    self._revoke_route_for_desired(desired, "universal_api_route_unverified")
                    return
                self._route_verified_at = observed
                if not self._route_is_fresh():
                    self._record_route_diagnostic_failure("route_proof_stale", desired.scope_key, desired=desired)
                    self._finish_monitor_diagnostic(desired.scope_key, step_started, observed_at=observed)
                    if not self._route_check_current(desired):
                        return
                    self._revoke_route_for_desired(desired, "audio_scope_stale")
                    return
                self._finish_monitor_diagnostic(desired.scope_key, step_started, observed_at=observed)
        except asyncio.CancelledError:
            raise
        except _RouteScopeRetired:
            if step_started is not None:
                self._finish_monitor_diagnostic(desired.scope_key, step_started)
            return
        except _RouteCheckObsolete:
            if step_started is not None:
                self._finish_monitor_diagnostic(desired.scope_key, step_started)
            return
        except _RouteCheckRejected:
            if step_started is not None:
                self._finish_monitor_diagnostic(desired.scope_key, step_started)
            if not self._route_check_current(desired):
                return
            self._revoke_route_for_desired(desired, "universal_api_route_unverified")
            return
        except Exception:
            self._record_route_diagnostic_failure("route_verifier_error", desired.scope_key, desired=desired)
            if step_started is not None:
                self._finish_monitor_diagnostic(desired.scope_key, step_started)
            if not self._route_check_current(desired):
                return
            self._revoke_route_for_desired(desired, "universal_api_route_unverified")

    def _revoke_route_for_desired(self, desired: QQCallAudioTurn, code: str) -> bool:
        """Revoke only the turn whose failed route check is still current."""
        with self._route_diagnostics_lock:
            if not self._desired_matches(desired) or self._route_failed:
                return False
            self._route_failed = True
            self._ready = False
            self._capture_enabled = False
            self._route_verified_at = None
            self._failure_code = self._unavailable_reason = code
            self._blocked_scope_key = self._active_scope_key or desired.scope_key
            self._invalidate_turn_sync()
            backend = self._backend
            loop = self._loop

        # Keep event-loop scheduling, backend work, and status callbacks outside
        # the diagnostics lock. update_call can now linearize either before this
        # commit (making it obsolete) or after it (and reset the revoked state).
        self._wake_from_sync()
        if backend is not None and loop is not None:
            def cancel() -> None:
                task = asyncio.create_task(backend.cancel_playback())
                task.add_done_callback(_consume_task_result)
            try:
                loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                pass
        self._emit_status("audio_route_unverified")
        return True

    def _revoke_route_sync(
        self,
        code: str,
        *,
        desired: QQCallAudioTurn | None = None,
        expected_scope_key: tuple[Any, ...] | None = None,
    ) -> None:
        with self._route_diagnostics_lock:
            scope_key = self._active_scope_key or (self._desired.scope_key if self._desired else None)
            if (
                self._route_failed
                or self._retired_scope_key is not None and scope_key == self._retired_scope_key
                or expected_scope_key is not None and expected_scope_key != scope_key
                or desired is not None and not self._desired_matches(desired)
            ):
                return
            self._route_failed = True
            self._ready = False
            self._capture_enabled = False
            self._route_verified_at = None
            self._failure_code = self._unavailable_reason = code
            self._blocked_scope_key = scope_key
            self._invalidate_turn_sync()
        self._wake_from_sync()
        backend = self._backend
        loop = self._loop
        if backend is not None and loop is not None:
            def cancel() -> None:
                task = asyncio.create_task(backend.cancel_playback())
                task.add_done_callback(_consume_task_result)
            try:
                loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                pass
        self._emit_status("audio_route_unverified")

    def _capture_transport_fence(self, desired: QQCallAudioTurn, session: Any) -> bool:
        """Synchronous scope fence injected into the QQCall PCM dispatcher."""
        scope_key = desired.scope_key
        allowed = bool(
            self._capture_enabled and not self._stop and not self._route_failed
            and self._session is session and self._desired_matches(desired)
            and self._route_is_fresh()
        )
        if not allowed:
            if not self._capture_enabled or self._stop or self._route_failed or self._session is not session:
                reason = "capture_lifecycle_fence"
            elif not self._desired_matches(desired):
                reason = self._desired_mismatch_reason(desired)
            else:
                self._record_current_route_age(scope_key)
                reason = "route_proof_stale"
            self._record_route_diagnostic_failure(reason, scope_key)
        return allowed

    def _backend_failed(self, error: Any, *, desired: QQCallAudioTurn | None = None) -> None:
        code = error if isinstance(error, str) else getattr(error, "code", None)
        if (
            not isinstance(code, str)
            or not re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code)
            or code not in _PCM_BACKEND_FAILURE_CODES
        ):
            code = "audio_backend_failed"
        self._service_failed(code, desired=desired)

    async def _events_worker(self, desired: QQCallAudioTurn, session: Any) -> None:
        scope_key = desired.scope_key
        try:
            async for event in session.events():
                if self._session is not session:
                    self._record_route_diagnostic_failure("capture_lifecycle_fence", scope_key)
                    return
                if not self._desired_matches(desired):
                    self._record_route_diagnostic_failure(self._desired_mismatch_reason(desired), scope_key)
                    return
                if not self._ready:
                    self._record_route_diagnostic_failure("capture_lifecycle_fence", scope_key)
                    return
                if not await self._turn_current(desired):
                    self._record_current_route_age(scope_key)
                    self._revoke_route_sync("audio_scope_stale", desired=desired)
                    return
                if not self._route_is_fresh():
                    self._record_current_route_age(scope_key)
                    self._record_route_diagnostic_failure("route_proof_stale", scope_key)
                    self._revoke_route_sync("audio_scope_stale", desired=desired)
                    return
                await self._handle_event(event, desired, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._session is session and self._desired_matches(desired):
                self._service_failed("universal_api_disconnected", desired=desired)

    async def _handle_event(self, event: Any, desired: QQCallAudioTurn, session: Any) -> None:
        if not isinstance(event, Mapping) or event.get("type") != "event":
            raise QQCallVoiceError("universal_api_event_invalid")
        if event.get("platform") != self.platform:
            raise QQCallVoiceError("universal_api_platform_invalid")
        if event.get("source_id") != "main":
            raise QQCallVoiceError("universal_api_source_invalid")
        kind = event.get("event")
        turn_id = event.get("turn_id")
        if not isinstance(turn_id, str) or not _SAFE_ID.fullmatch(turn_id):
            raise QQCallVoiceError("universal_api_turn_invalid")
        if kind == "speech_start":
            old_turn = self.current_turn_id(desired.scope)
            if old_turn == turn_id:
                return
            self._set_turn_sync(desired.scope_key, turn_id)
            self._cancel_old_playback()
            self._emit_status("speech_started")
            return
        if kind == "speech_end":
            if self.current_turn_id(desired.scope) != turn_id:
                return
            return
        if kind == "error":
            raise QQCallVoiceError("universal_api_pipeline_error")
        if kind != "speech_result":
            return
        key = (desired.scope_key, turn_id)
        if key in self._seen_results or self.current_turn_id(desired.scope) != turn_id:
            self._utterances_dropped += 1
            return
        self._seen_results[key] = None
        while len(self._seen_results) > 256:
            self._seen_results.popitem(last=False)
        if not self.fence(desired.scope, desired.peer_user_id, turn_id) or self._session is not session:
            self._utterances_dropped += 1
            return
        voice = event.get("voice_base64")
        if not isinstance(voice, str) or not voice:
            raise QQCallVoiceError("universal_api_result_audio_missing")
        if not await self._admitted(desired) or not self.fence(desired.scope, desired.peer_user_id, turn_id):
            self._utterances_dropped += 1
            return
        identity_ok = self._identity_is_current(desired.identity)
        if _awaitable(identity_ok):
            identity_ok = await identity_ok
        if identity_ok is not True or not self.fence(desired.scope, desired.peer_user_id, turn_id):
            self._utterances_dropped += 1
            return
        additional: dict[str, Any] = {
            "qqcall_scope": dict(desired.scope),
            "peer_user_id": desired.peer_user_id,
            "delivery_target": {
                "kind": "qqcall", **dict(desired.scope),
                "peer_user_id": desired.peer_user_id, "turn_id": turn_id,
            },
            "runtime_capabilities": dict(_CAPABILITIES),
            "realtime_session": {
                "version": 1,
                "provider": "qqcall",
                "session_id": _realtime_session_id(desired.scope),
                "turn_id": turn_id,
            },
        }
        text = event.get("text")
        legacy_text = event.get("precomputed_asr_text")
        result_id = event.get("precomputed_asr_result_id")
        asr_scope = event.get("asr_scope")
        text_ok = isinstance(text, str) and text.strip() and len(text) <= 16_384
        if legacy_text is not None:
            if not isinstance(legacy_text, str) or (text_ok and legacy_text.strip() != text.strip()):
                text_ok = False
            elif not text_ok and legacy_text.strip():
                text, text_ok = legacy_text, len(legacy_text) <= 16_384
        if (
            text_ok and isinstance(result_id, str) and 1 <= len(result_id) <= 256
            and isinstance(asr_scope, str) and 1 <= len(asr_scope) <= 2048
        ):
            additional["precomputed_asr_result_id"] = result_id
            additional["precomputed_asr_scope"] = asr_scope
        name_key = (*desired.scope_key, desired.peer_user_id)
        nickname = self._peer_names.get(name_key)
        if nickname is None and self._resolve_peer_info is not None:
            try:
                info = self._resolve_peer_info(desired.peer_user_id)
                if _awaitable(info):
                    info = await asyncio.wait_for(info, timeout=2.0)
                name = info.get("nickname") if isinstance(info, Mapping) else None
                peer = info.get("user_id") if isinstance(info, Mapping) else None
                if (not isinstance(peer, bool) and str(peer) == desired.peer_user_id
                        and isinstance(name, str) and 0 < len(name.strip()) <= 128
                        and not any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    nickname = name.strip()
                    self._peer_names[name_key] = nickname
                    while len(self._peer_names) > 64:
                        self._peer_names.popitem(last=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        # Failed profile lookup must never invent a generic caller identity.
        # A later turn may retry; the existing post-lookup fences still apply.
        nickname = nickname or desired.peer_user_id
        identity_ok = self._identity_is_current(desired.identity)
        if _awaitable(identity_ok):
            identity_ok = await identity_ok
        if identity_ok is not True or not self.fence(desired.scope, desired.peer_user_id, turn_id) or self._session is not session:
            self._utterances_dropped += 1
            return
        message = MessageBase(
            message_info=BaseMessageInfo(
                platform=self.platform,
                message_id=f"qqcall:voice:{desired.scope['callId']}:{desired.scope['callGeneration']}:{turn_id}",
                time=time.time(),
                additional_config=additional,
                user_info=UserInfo(platform=self.platform, user_id=desired.peer_user_id,
                                   user_nickname=nickname, user_cardname=""),
                group_info=None, sender_info=None, receiver_info=None,
            ),
            message_segment=Seg(type="voice", data=voice),
            raw_message=None,
        )
        if not self.fence(desired.scope, desired.peer_user_id, turn_id) or self._session is not session:
            self._utterances_dropped += 1
            return
        try:
            sent = await asyncio.wait_for(self._send_message(message), timeout=8.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            sent = False
        if self._session is not session or not self.fence(desired.scope, desired.peer_user_id, turn_id):
            self._utterances_dropped += 1
            return
        if sent is True:
            self._utterances_sent += 1
            self._emit_status("utterance_submitted")
        else:
            self._utterances_dropped += 1
            self._service_failed("core_message_delivery_failed", desired=desired)

    def _set_turn_sync(self, key: tuple[Any, ...], turn_id: str) -> None:
        with self._turn_lock:
            self._turn_scope_key = key
            self._turn_id = turn_id

    def _invalidate_turn_sync(self) -> None:
        with self._turn_lock:
            self._turn_id = None
            self._turn_scope_key = None

    def _cancel_old_playback(self) -> None:
        backend, loop = self._backend, self._loop
        if backend is None or loop is None:
            return
        def cancel() -> None:
            task = asyncio.create_task(backend.cancel_playback())
            task.add_done_callback(_consume_task_result)
        try:
            if asyncio.get_running_loop() is loop:
                cancel()
            else:
                loop.call_soon_threadsafe(cancel)
        except RuntimeError:
            try:
                loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                pass

    def _service_failed(self, code: str, *, desired: QQCallAudioTurn | None = None) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code):
            code = "universal_api_unavailable"
        with self._route_diagnostics_lock:
            key = desired.scope_key if desired is not None else (
                self._active_scope_key or (self._desired.scope_key if self._desired else None)
            )
            if (
                self._retired_scope_key is not None and key == self._retired_scope_key
                or desired is not None and not self._desired_matches(desired)
            ):
                return
            if key is not None:
                self._blocked_scope_key = key
            self._failure_code = self._unavailable_reason = code
            self._ready = False
            self._capture_enabled = False
            self._route_failed = True
            self._invalidate_turn_sync()
        self._cancel_old_playback()
        self._wake_from_sync()
        self._emit_status("audio_unavailable")

    async def intercept_outbound(self, raw: Any, target: Mapping[str, Any]) -> dict[str, Any]:
        scope = _valid_scope({key: target.get(key) for key in _CALL_KEYS})
        peer = target.get("peer_user_id")
        turn_id = target.get("turn_id")
        if scope is None or not isinstance(peer, str) or not peer.isdigit() or not isinstance(turn_id, str) or not _SAFE_ID.fullmatch(turn_id):
            return {"accepted": False, "code": "stale_call_scope", "drain_confirmed": False}
        if not self.fence(scope, peer, turn_id):
            return {"accepted": False, "code": "stale_audio_turn", "drain_confirmed": False}
        segments = _segments(raw)
        receipt: dict[str, Any] = {"accepted": True, "code": "display_text_acknowledged", "drain_confirmed": False}
        for kind, data in segments:
            if not self.fence(scope, peer, turn_id):
                return {"accepted": False, "code": "stale_audio_turn", "drain_confirmed": False}
            if kind in {"text", "tts_text", "reply"}:
                continue
            if kind not in {"voice", "voice_stream"}:
                receipt = {"accepted": True, "code": "unsupported_call_segment_consumed", "drain_confirmed": False}
                continue
            backend = self._backend
            if backend is None or not callable(getattr(backend, "play_core_segment", None)):
                return {"accepted": False, "code": "universal_api_capture_unsupported", "drain_confirmed": False}
            try:
                result = await backend.play_core_segment(kind, data, lambda: self.fence(scope, peer, turn_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.fence(scope, peer, turn_id):
                    return {"accepted": False, "code": "stale_audio_turn", "drain_confirmed": False}
                code = getattr(exc, "code", None)
                if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code):
                    code = "playback_failed"
                return {"accepted": False, "code": code, "drain_confirmed": False}
            if not self.fence(scope, peer, turn_id):
                return {"accepted": False, "code": "stale_audio_turn", "drain_confirmed": False}
            drained = isinstance(result, Mapping) and result.get("render_drained") is True and result.get("drain_confirmed") is True
            if drained:
                self._playback_drained += 1
                receipt = {"accepted": True, "code": "local_endpoint_drained", "drain_confirmed": True}
            else:
                receipt = {"accepted": True, "code": "local_endpoint_queued", "drain_confirmed": False}
        self._emit_status("outbound_consumed")
        return receipt

    async def _stop_resources(self, *, interrupt: bool) -> None:
        self._ready = False
        self._capture_enabled = False
        route_task, self._route_task = self._route_task, None
        events_task, self._events_task = self._events_task, None
        current = asyncio.current_task()
        tasks = [task for task in (route_task, events_task) if task is not None and task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        backend, self._backend = self._backend, None
        session, self._session = self._session, None
        client, self._client = self._client, None
        create_task, self._create_task = self._create_task, None
        self._active_scope_key = None
        self._scope_text = None
        self._active_turn = None
        self._route_verified_at = None
        self._route_failed = False
        self._invalidate_turn_sync()
        # Revoke the local render path before waiting on any remote API cleanup.
        # cancel_playback invalidates the native callback synchronously before
        # its first await, so even a slow WS interrupt cannot render stale audio.
        if backend is not None:
            try:
                await asyncio.wait_for(backend.cancel_playback(), timeout=2.0)
            except Exception:
                pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if session is not None:
            if interrupt:
                try:
                    await asyncio.wait_for(session.interrupt(), timeout=2.0)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(session.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass
        if create_task is not None:
            self._schedule_late_create_cleanup(create_task, client)
            client = None
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=_CLEANUP_TIMEOUT)
            except Exception:
                pass
        if backend is not None:
            await self._dispose_backend(backend)

    def _emit_status(self, kind: str) -> None:
        try:
            value = self._on_status({"kind": kind, "audio": self.status()})
            if _awaitable(value):
                task = asyncio.create_task(value)
                task.add_done_callback(_consume_task_result)
        except Exception:
            pass

    async def stop(self) -> None:
        self._stop = True
        with self._route_diagnostics_lock:
            self._desired = None
        self._ready = False
        self._capture_enabled = False
        self._unavailable_reason = "runtime_stopped"
        self._invalidate_turn_sync()
        self._wake.set()
        task, self._supervisor = self._supervisor, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self._stop_resources(interrupt=True)
        await self._wait_for_late_create_cleanups()


__all__ = [
    "QQCallAudioTurn", "QQCallRouteProof", "QQCallVoiceError", "QQCallVoiceRuntime",
    "TRUSTED_INVITE_CORRELATIONS", "is_trusted_invite_correlation",
]
