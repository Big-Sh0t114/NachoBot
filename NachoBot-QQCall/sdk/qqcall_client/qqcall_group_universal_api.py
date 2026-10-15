"""Single-scope QQCall group binding to UniversalVC's frozen public API.

This is only a lifecycle/identity fence. A valid local scope is not proof that
QQ joined a room and does not authorize group audio. The caller must supply a
live lease validator backed by the independent native admission proof.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from qqcall_pcm.group_protocol import (
    anonymous_speaker_annotation,
    group_session_id,
    universal_group_options,
    validate_group_scope,
)
from .qqcall_group_wire import validate_group_audio_claim

from universalvc_client import (
    MAX_PCM_FRAME_BYTES, PROTOCOL_VERSION, UniversalVCDisconnected,
    UniversalVCProtocolError, UniversalVCServiceError,
)


_SCOPE_FIELDS = (
    "kind",
    "serviceInstance",
    "hostGeneration",
    "connectionGeneration",
    "groupId",
    "roomId",
    "roomGeneration",
)
_UVC_ID_FIELDS = ("service_instance_id", "session_id", "session_generation", "source_id")
_SAFE_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LEASE_TIMEOUT = 2.0
_CREATE_TIMEOUT = 10.0
_ACTION_TIMEOUT = 5.0
_CALLBACK_TIMEOUT = 5.0
_CLEANUP_TIMEOUT = 3.0
_MAX_TRANSCRIPT_BYTES = 16 * 1024
_MAX_ASR_RESULT_ID_BYTES = 256
_MAX_ASR_SCOPE_BYTES = 2048
_MAX_SPEECH_WAV_BYTES = 60 * 16_000 * 2 + 4096
_MAX_SPEECH_WAV_BASE64_CHARS = ((_MAX_SPEECH_WAV_BYTES + 2) // 3) * 4
_LEASE_MONITOR_INTERVAL = 1.0
_MAX_SEEN_TURNS = 8192
_LOGGER = logging.getLogger(__name__)
_LOGGED_FAILURE_CODES = frozenset({
    "binding_failed", "lease_not_current", "session_identity_changed", "lease_monitor_failed",
    "speech_start_callback_timeout", "speech_start_callback_failed", "speech_result_invalid",
    "deduplication_cap_reached", "event_identity_invalid", "service_capture_discontinuity",
    "service_event_too_large", "service_event_error", "event_stream_failed", "event_stream_ended",
    "pcm_send_timeout", "pcm_send_disconnected", "pcm_send_protocol_invalid", "pcm_send_invalid_request",
    "pcm_send_service_unavailable", "pcm_send_operation_failed", "pcm_send_failed",
})


class QQCallGroupBindingError(RuntimeError):
    """Sanitized lifecycle error; it never includes scope or event values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


LeaseValidator = Callable[[dict[str, Any]], Awaitable[object]]
SpeechResultCallback = Callable[[dict[str, Any]], Awaitable[object]]
SpeechStartCallback = Callable[[], Awaitable[object]]


class QQCallGroupUniversalBinding:
    """Own one UniversalVC session for one immutable, independently leased scope.

    The binding owns only the session returned by ``client.create_session``.
    The injected client is shared/caller-owned and is never closed here.
    """

    def __init__(
        self,
        client: Any,
        current_lease: LeaseValidator,
        on_speech_result: SpeechResultCallback,
        on_speech_start: SpeechStartCallback | None = None,
    ) -> None:
        if client is None or not callable(getattr(client, "create_session", None)):
            raise TypeError("an injected UniversalVC client is required")
        if not callable(current_lease):
            raise TypeError("an asynchronous current-lease validator is required")
        if not callable(on_speech_result):
            raise TypeError("an asynchronous speech-result callback is required")
        if on_speech_start is not None and not callable(on_speech_start):
            raise TypeError("an asynchronous speech-start callback is required")
        self._client = client
        self._current_lease = current_lease
        self._on_speech_result = on_speech_result
        self._on_speech_start = on_speech_start
        self._lock = asyncio.Lock()
        self._state = "unbound"
        self._failure_code: str | None = None
        self._scope_values: tuple[Any, ...] | None = None
        self._scope_ref: str | None = None
        self._session: Any | None = None
        self._session_identity: tuple[str, str, str, str] | None = None
        self._create_task: asyncio.Task[Any] | None = None
        self._event_task: asyncio.Task[Any] | None = None
        self._lease_monitor_task: asyncio.Task[Any] | None = None
        self._lease_monitor_stop: asyncio.Event | None = None
        self._callback_task: asyncio.Task[Any] | None = None
        self._send_tasks: set[asyncio.Task[Any]] = set()
        self._background_tasks: set[asyncio.Future[Any]] = set()
        self._create_cleanup_ids: set[int] = set()
        self._session_close_tasks: dict[int, asyncio.Task[Any]] = {}
        self._seen_turns: OrderedDict[str, str] = OrderedDict()

    @property
    def state(self) -> str:
        """Return the binding lifecycle state without exposing identifiers."""
        return self._state

    @property
    def failure_code(self) -> str | None:
        """Return a fixed, sanitized failure code for lifecycle owners."""
        return self._failure_code

    @staticmethod
    def _scope_tuple(scope: Any) -> tuple[Any, ...]:
        try:
            validated = validate_group_scope(scope)
        except (TypeError, ValueError):
            raise QQCallGroupBindingError("scope_invalid") from None
        return tuple(validated[field] for field in _SCOPE_FIELDS)

    def _scope_dict(self) -> dict[str, Any]:
        if self._scope_values is None:
            raise QQCallGroupBindingError("scope_unbound")
        return dict(zip(_SCOPE_FIELDS, self._scope_values, strict=True))

    async def _bind_or_check_scope(self, scope: Any, *, allow_new: bool) -> None:
        requested = self._scope_tuple(scope)
        async with self._lock:
            if self._state in {"closing", "closed", "failed"}:
                raise QQCallGroupBindingError("binding_closed")
            if self._scope_values is None:
                if not allow_new:
                    raise QQCallGroupBindingError("scope_unbound")
                self._scope_values = requested
                self._scope_ref = group_session_id(self._scope_dict())
                self._state = "bound"
            elif requested != self._scope_values:
                raise QQCallGroupBindingError("scope_mismatch")

    async def _lease_is_current(self) -> bool:
        check_task: asyncio.Future[Any] | None = None
        try:
            result = self._current_lease(self._scope_dict())
            if not inspect.isawaitable(result):
                return False
            check_task = asyncio.ensure_future(result)
            done, _pending = await asyncio.wait({check_task}, timeout=_LEASE_TIMEOUT)
            if not done:
                check_task.cancel()
                self._track_background(check_task)
                return False
            try:
                observed = check_task.result()
            except asyncio.CancelledError:
                return False
            return observed is True
        except asyncio.CancelledError:
            if check_task is not None and not check_task.done():
                check_task.cancel()
                self._track_background(check_task)
            raise
        except Exception:
            return False

    async def verify_result(self, scope: Any, claim: Any) -> bool:
        """Read-only proof that a claimed turn was accepted in this live lease."""

        try:
            requested_scope = self._scope_tuple(scope)
            requested_claim = validate_group_audio_claim(claim)
        except (TypeError, ValueError, QQCallGroupBindingError):
            return False

        session: Any | None = None
        identity: tuple[str, str, str, str] | None = None
        try:
            async with self._lock:
                if (
                    self._state != "active"
                    or requested_scope != self._scope_values
                    or self._session is None
                    or self._session_identity is None
                    or self._seen_turns.get(requested_claim["turn_id"]) != requested_claim["speaker_ref"]
                ):
                    return False
                session = self._session
                identity = self._session_identity
                current_uvc = dict(zip(_UVC_ID_FIELDS, identity, strict=True))
                if (
                    requested_claim["uvc_session"] != current_uvc
                    or not self._session_identity_matches(session)
                ):
                    return False

            if not await self._lease_is_current():
                return False

            async with self._lock:
                return bool(
                    self._state == "active"
                    and self._scope_values == requested_scope
                    and self._session is session
                    and self._session_identity == identity
                    and self._seen_turns.get(requested_claim["turn_id"]) == requested_claim["speaker_ref"]
                    and requested_claim["uvc_session"]
                    == dict(zip(_UVC_ID_FIELDS, identity or (), strict=True))
                    and session is not None
                    and self._session_identity_matches(session)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _bounded_utf8(value: Any, max_bytes: int) -> bool:
        if type(value) is not str or not value:
            return False
        try:
            return len(value.encode("utf-8", errors="strict")) <= max_bytes
        except UnicodeError:
            return False

    @classmethod
    def _speech_text(cls, event: dict[str, Any]) -> str | None:
        text = event.get("text")
        legacy_text = event.get("precomputed_asr_text")
        text_ok = cls._bounded_utf8(text, _MAX_TRANSCRIPT_BYTES) and bool(text.strip())
        if legacy_text is not None:
            if not cls._bounded_utf8(legacy_text, _MAX_TRANSCRIPT_BYTES):
                return None
            if text_ok and legacy_text.strip() != text.strip():
                return None
            if not text_ok and legacy_text.strip():
                text = legacy_text
                text_ok = True
        return text if text_ok else None

    @staticmethod
    def _speech_wav_is_valid(value: Any) -> bool:
        if type(value) is not str or not value or len(value) > _MAX_SPEECH_WAV_BASE64_CHARS:
            return False
        try:
            decoded = base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError):
            return False
        return 0 < len(decoded) <= _MAX_SPEECH_WAV_BYTES

    def _read_session_identity(self, session: Any) -> tuple[str, str, str, str]:
        try:
            identity = {
                "service_instance_id": session.service_instance_id,
                "session_id": session.session_id,
                "session_generation": session.session_generation,
                "source_id": "main",
            }
            # Reuse the protocol helper's strict session-ID validation without
            # exposing a synthetic speaker annotation to any caller.
            anonymous_speaker_annotation(self._scope_dict(), "session-check", identity)
        except Exception:
            raise QQCallGroupBindingError("session_identity_invalid") from None
        return tuple(identity[field] for field in _UVC_ID_FIELDS)  # type: ignore[return-value]

    def _session_identity_matches(self, session: Any) -> bool:
        try:
            return self._session_identity is not None and self._read_session_identity(session) == self._session_identity
        except QQCallGroupBindingError:
            return False

    async def start(self, scope: Any) -> None:
        """Create one speaker-mode session after exact positive lease checks."""
        await self._bind_or_check_scope(scope, allow_new=True)
        async with self._lock:
            if self._state == "active":
                active_session = self._session
                active = True
            elif self._state != "bound":
                raise QQCallGroupBindingError("start_unavailable")
            else:
                active_session = None
                active = False
                self._state = "starting"

        if active:
            # Idempotent start still proves the independent lease is current;
            # a stale active state must never authorize continued use.
            if not await self._lease_is_current():
                await self._shutdown("failed", failure_code="lease_not_current")
                raise QQCallGroupBindingError("lease_not_current")
            async with self._lock:
                if self._state != "active" or self._session is not active_session:
                    raise QQCallGroupBindingError("binding_closed")
                if active_session is None or not self._session_identity_matches(active_session):
                    identity_changed = True
                else:
                    identity_changed = False
            if identity_changed:
                await self._shutdown("failed", failure_code="session_identity_changed")
                raise QQCallGroupBindingError("session_identity_changed")
            return

        create_task: asyncio.Task[Any] | None = None
        try:
            if not await self._lease_is_current():
                async with self._lock:
                    if self._state == "starting":
                        self._state = "bound"
                raise QQCallGroupBindingError("lease_not_current")

            options = universal_group_options(self._scope_dict())
            async with self._lock:
                if self._state != "starting":
                    raise QQCallGroupBindingError("binding_closed")
                create_task = asyncio.create_task(self._client.create_session(options))
                self._create_task = create_task
            try:
                session = await asyncio.wait_for(asyncio.shield(create_task), timeout=_CREATE_TIMEOUT)
            except asyncio.TimeoutError:
                await self._shutdown("failed")
                raise QQCallGroupBindingError("session_create_timeout") from None
            except asyncio.CancelledError:
                await self._shutdown("failed")
                raise
            except Exception:
                await self._shutdown("failed")
                raise QQCallGroupBindingError("session_create_failed") from None

            try:
                identity = self._read_session_identity(session)
            except QQCallGroupBindingError:
                async with self._lock:
                    self._state = "failed"
                await self._close_session_once(session)
                raise

            async with self._lock:
                if self._create_task is create_task:
                    self._create_task = None
                if self._state != "starting":
                    rejected = True
                else:
                    self._session = session
                    self._session_identity = identity
                    rejected = False
            if rejected:
                await self._close_session_once(session)
                raise QQCallGroupBindingError("binding_closed")

            # This second lease read fences a lease transition while the client
            # was awaiting HTTP creation and WebSocket readiness.
            if not await self._lease_is_current():
                await self._shutdown("failed")
                raise QQCallGroupBindingError("lease_not_current")

            async with self._lock:
                if self._state != "starting" or self._session is not session:
                    raise QQCallGroupBindingError("binding_closed")
                self._state = "active"
                self._lease_monitor_stop = asyncio.Event()
                self._lease_monitor_task = asyncio.create_task(
                    self._lease_monitor(session, self._lease_monitor_stop),
                    name="qqcall-group-lease-monitor",
                )
                self._event_task = asyncio.create_task(
                    self._event_loop(session, identity),
                    name="qqcall-group-universal-events",
                )
        except asyncio.CancelledError:
            await self._shutdown("failed")
            raise
        except QQCallGroupBindingError:
            raise
        except Exception:
            await self._shutdown("failed")
            raise QQCallGroupBindingError("session_start_failed") from None
        finally:
            if create_task is not None and create_task.done():
                async with self._lock:
                    if self._create_task is create_task:
                        self._create_task = None

    async def _lease_monitor(self, session: Any, stop: asyncio.Event) -> None:
        """Recheck an idle binding's lease and revoke all owned work on loss."""
        try:
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=_LEASE_MONITOR_INTERVAL)
                    return
                except asyncio.TimeoutError:
                    pass

                if not await self._lease_is_current():
                    await self._shutdown("failed", failure_code="lease_not_current")
                    return
                async with self._lock:
                    if self._state != "active" or self._session is not session:
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._shutdown("failed", failure_code="lease_monitor_failed")

    async def send_pcm(self, scope: Any, pcm: bytes) -> None:
        """Pass one bounded PCM16 frame through the owned `main` source."""
        await self._bind_or_check_scope(scope, allow_new=False)
        if type(pcm) is not bytes or not pcm or len(pcm) > MAX_PCM_FRAME_BYTES or len(pcm) % 2:
            raise QQCallGroupBindingError("pcm_frame_invalid")
        current_task = asyncio.current_task()
        if current_task is None:
            raise QQCallGroupBindingError("send_unavailable")
        async with self._lock:
            if self._state != "active" or self._session is None:
                raise QQCallGroupBindingError("session_not_active")
            session = self._session
            self._send_tasks.add(current_task)
        try:
            if not await self._lease_is_current():
                await self._shutdown("failed", failure_code="lease_not_current")
                raise QQCallGroupBindingError("lease_not_current")
            identity_changed = False
            async with self._lock:
                if self._state != "active" or self._session is not session:
                    raise QQCallGroupBindingError("session_not_active")
                identity_changed = not self._session_identity_matches(session)
            if identity_changed:
                await self._shutdown("failed", failure_code="session_identity_changed")
                raise QQCallGroupBindingError("session_identity_changed")
            try:
                await asyncio.wait_for(session.send_pcm(pcm, source_id="main"), timeout=_ACTION_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    failure = "pcm_send_timeout"
                elif isinstance(exc, UniversalVCDisconnected):
                    failure = "pcm_send_disconnected"
                elif isinstance(exc, UniversalVCProtocolError):
                    failure = "pcm_send_protocol_invalid"
                elif isinstance(exc, UniversalVCServiceError):
                    failure = {
                        "invalid_request": "pcm_send_invalid_request",
                        "unavailable": "pcm_send_service_unavailable",
                        "operation_failed": "pcm_send_operation_failed",
                    }.get(exc.code, "pcm_send_failed")
                else:
                    failure = "pcm_send_failed"
                await self._shutdown("failed", failure_code=failure)
                raise QQCallGroupBindingError("pcm_send_failed") from None
            if not await self._lease_is_current():
                await self._shutdown("failed", failure_code="lease_not_current")
                raise QQCallGroupBindingError("lease_not_current")
            identity_changed = False
            async with self._lock:
                if self._state != "active" or self._session is not session:
                    raise QQCallGroupBindingError("session_not_active")
                identity_changed = not self._session_identity_matches(session)
            if identity_changed:
                await self._shutdown("failed", failure_code="session_identity_changed")
                raise QQCallGroupBindingError("session_identity_changed")
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                self._send_tasks.discard(current_task)

    async def _event_loop(self, session: Any, identity: tuple[str, str, str, str]) -> None:
        try:
            async for event in session.events():
                async with self._lock:
                    if self._state != "active" or self._session is not session:
                        return
                if not self._session_identity_matches(session) or not self._event_matches(event, identity):
                    await self._shutdown("failed", failure_code="event_identity_invalid")
                    return
                event_name = event.get("event")
                if event_name == "error":
                    failure = {
                        "capture_discontinuity": "service_capture_discontinuity",
                        "event_too_large": "service_event_too_large",
                    }.get(event.get("code"), "service_event_error")
                    await self._shutdown("failed", failure_code=failure)
                    return
                if event_name == "speech_start":
                    received_at = time.monotonic()
                    _LOGGER.info("QQCall group barge stage=event_received at_ms=%s", round(time.time() * 1000))
                    lease_current = await self._lease_is_current()
                    _LOGGER.info(
                        "QQCall group barge stage=lease_checked at_ms=%s elapsed_ms=%s current=%s",
                        round(time.time() * 1000),
                        min(60000, max(0, round((time.monotonic() - received_at) * 1000))),
                        lease_current,
                    )
                    if not lease_current:
                        await self._shutdown("failed", failure_code="lease_not_current")
                        return
                    async with self._lock:
                        if self._state != "active" or self._session is not session:
                            return
                        callback = self._on_speech_start
                        if callback is None:
                            continue
                        callback_task = asyncio.create_task(callback())
                        self._callback_task = callback_task
                    done, _pending = await asyncio.wait({callback_task}, timeout=_CALLBACK_TIMEOUT)
                    if not done:
                        callback_task.cancel()
                        self._track_background(callback_task)
                        await self._shutdown("failed", failure_code="speech_start_callback_timeout")
                        return
                    try:
                        callback_task.result()
                    except asyncio.CancelledError:
                        if self._state not in {"closing", "closed", "failed"}:
                            await self._shutdown("failed")
                        return
                    except Exception:
                        await self._shutdown("failed", failure_code="speech_start_callback_failed")
                        return
                    finally:
                        async with self._lock:
                            if self._callback_task is callback_task:
                                self._callback_task = None
                    continue
                if event_name != "speech_result":
                    continue
                turn_id = event.get("turn_id")
                if type(turn_id) is not str or _SAFE_EVENT_ID.fullmatch(turn_id) is None:
                    await self._shutdown("failed")
                    return
                if turn_id in self._seen_turns:
                    continue
                speaker_id = event.get("speaker_id")
                transcript = self._speech_text(event)
                voice_base64 = event.get("voice_base64")
                asr_result_id = event.get("precomputed_asr_result_id")
                asr_scope = event.get("asr_scope")
                receipt_valid = (
                    transcript is not None
                    and self._bounded_utf8(asr_result_id, _MAX_ASR_RESULT_ID_BYTES)
                    and self._bounded_utf8(asr_scope, _MAX_ASR_SCOPE_BYTES)
                )
                # The frozen API emits the complete WAV without a receipt when
                # streaming ASR is unavailable. Core owns ordinary ASR fallback.
                fallback_valid = (
                    asr_result_id is None
                    and event.get("text") in (None, "")
                    and event.get("precomputed_asr_text") in (None, "")
                    and self._bounded_utf8(asr_scope, _MAX_ASR_SCOPE_BYTES)
                )
                wav_valid = self._speech_wav_is_valid(voice_base64)
                if not wav_valid or not (receipt_valid or fallback_valid):
                    _LOGGER.warning(
                        "QQCall group speech validation failed wav_valid=%s receipt_valid=%s fallback_valid=%s text_valid=%s",
                        wav_valid, receipt_valid, fallback_valid, transcript is not None,
                    )
                    await self._shutdown("failed", failure_code="speech_result_invalid")
                    return
                try:
                    transcript_size = len(transcript.encode("utf-8", errors="strict")) if transcript is not None else 0
                    annotation = anonymous_speaker_annotation(
                        self._scope_dict(),
                        speaker_id,
                        dict(zip(_UVC_ID_FIELDS, identity, strict=True)),
                    )
                except Exception:
                    await self._shutdown("failed")
                    return
                if transcript_size > _MAX_TRANSCRIPT_BYTES:
                    await self._shutdown("failed")
                    return

                # Keep deduplication session-scoped and bounded. Once the
                # budget is exhausted, fail closed instead of evicting an old
                # turn ID that could later be redelivered.
                if len(self._seen_turns) >= _MAX_SEEN_TURNS:
                    await self._shutdown("failed", failure_code="deduplication_cap_reached")
                    return
                if not await self._lease_is_current():
                    await self._shutdown("failed")
                    return
                envelope = {
                    "type": "qqcall_group_speech_result",
                    "scope": self._scope_dict(),
                    "session_ref": self._scope_ref,
                    "session_identity": dict(zip(_UVC_ID_FIELDS, identity, strict=True)),
                    "source_id": "main",
                    "speaker_annotation": annotation,
                    "speech_result": {
                        "turn_id": turn_id,
                        "text": transcript,
                        "voice_base64": voice_base64,
                        "precomputed_asr_result_id": asr_result_id,
                        "asr_scope": asr_scope,
                    },
                }
                async with self._lock:
                    if self._state != "active" or self._session is not session:
                        return
                    # Publish only the bounded projected speaker reference,
                    # after the current lease passes and before the trusted
                    # callback can ask Core to verify this result.
                    self._seen_turns[turn_id] = annotation["speaker_ref"]
                    callback_task = asyncio.create_task(self._on_speech_result(envelope))
                    self._callback_task = callback_task
                done, _pending = await asyncio.wait({callback_task}, timeout=_CALLBACK_TIMEOUT)
                if not done:
                    callback_task.cancel()
                    self._track_background(callback_task)
                    await self._shutdown("failed")
                    return
                try:
                    try:
                        callback_task.result()
                    except asyncio.CancelledError:
                        if self._state not in {"closing", "closed", "failed"}:
                            await self._shutdown("failed")
                        return
                    except Exception:
                        await self._shutdown("failed")
                        return
                finally:
                    async with self._lock:
                        if self._callback_task is callback_task:
                            self._callback_task = None
                turn_ref = hashlib.sha256(
                    f"{self._scope_ref}:{turn_id}".encode("utf-8")
                ).hexdigest()[:16]
                _LOGGER.info(
                    "QQCall group UniversalVC speech_result session_ref=%s speaker_ref=%s speaker_label=%s turn_ref=qqt-%s",
                    self._scope_ref,
                    annotation["speaker_ref"],
                    annotation["label"],
                    turn_ref,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._shutdown("failed", failure_code="event_stream_failed")
            return
        async with self._lock:
            still_active = self._state == "active" and self._session is session
        if still_active:
            await self._shutdown("failed", failure_code="event_stream_ended")

    @staticmethod
    def _event_matches(event: Any, identity: tuple[str, str, str, str]) -> bool:
        if type(event) is not dict or event.get("type") != "event":
            return False
        if type(event.get("protocol_version")) is not int or event.get("protocol_version") != PROTOCOL_VERSION:
            return False
        for field, expected in zip(_UVC_ID_FIELDS, identity, strict=True):
            if event.get(field) != expected:
                return False
        if event.get("source_id") != "main":
            return False
        return event.get("event") in {"speech_start", "speech_end", "speech_result", "error", "playback"}

    def _track_background(self, task: asyncio.Future[Any]) -> None:
        self._background_tasks.add(task)

        def consume(done: asyncio.Future[Any]) -> None:
            self._background_tasks.discard(done)
            try:
                done.exception()
            except BaseException:
                pass

        task.add_done_callback(consume)

    def _schedule_late_create_cleanup(self, create_task: asyncio.Task[Any]) -> None:
        key = id(create_task)
        if key in self._create_cleanup_ids:
            return
        self._create_cleanup_ids.add(key)
        create_task.cancel()

        async def drain() -> None:
            try:
                session = await create_task
            except BaseException:
                return
            await self._close_session_once(session)

        task = asyncio.create_task(drain(), name="qqcall-group-late-session-cleanup")
        self._track_background(task)

    async def _close_session_once(self, session: Any) -> bool:
        key = id(session)
        async with self._lock:
            task = self._session_close_tasks.get(key)
            if task is None:
                async def close_session() -> bool:
                    try:
                        operation = session.close()
                        if not inspect.isawaitable(operation):
                            return False
                        close_task = asyncio.create_task(operation, name="qqcall-group-session-close-call")
                        self._track_background(close_task)
                        done, _pending = await asyncio.wait({close_task}, timeout=_CLEANUP_TIMEOUT)
                        if not done:
                            close_task.cancel()
                            return False
                        try:
                            close_task.result()
                            return True
                        except BaseException:
                            return False
                    except BaseException:
                        return False

                task = asyncio.create_task(close_session(), name="qqcall-group-session-cleanup")
                self._session_close_tasks[key] = task
                self._track_background(task)
        done, _pending = await asyncio.wait({task}, timeout=_CLEANUP_TIMEOUT + 0.1)
        if not done:
            return False
        try:
            return task.result() is True
        except BaseException:
            return False

    async def _shutdown(self, terminal_state: str, *, failure_code: str | None = None) -> None:
        current = asyncio.current_task()
        first_failure = False
        async with self._lock:
            if terminal_state == "closed" or self._state not in {"closed", "failed"}:
                self._state = terminal_state
            if terminal_state == "failed" and self._failure_code is None:
                self._failure_code = failure_code or "binding_failed"
                first_failure = True
            event_task, self._event_task = self._event_task, None
            lease_monitor_task, self._lease_monitor_task = self._lease_monitor_task, None
            lease_monitor_stop, self._lease_monitor_stop = self._lease_monitor_stop, None
            callback_task, self._callback_task = self._callback_task, None
            create_task, self._create_task = self._create_task, None
            session, self._session = self._session, None
            self._session_identity = None
            sends = tuple(self._send_tasks)
        if first_failure:
            logged = self._failure_code if self._failure_code in _LOGGED_FAILURE_CODES else "unclassified"
            _LOGGER.warning("QQCall group UniversalVC binding failed code=%s", logged)
        if lease_monitor_stop is not None:
            lease_monitor_stop.set()
        if create_task is not None:
            if not create_task.done():
                self._schedule_late_create_cleanup(create_task)
            elif not create_task.cancelled():
                try:
                    late_session = create_task.result()
                except BaseException:
                    pass
                else:
                    if late_session is not session:
                        await self._close_session_once(late_session)
        tasks = [
            task
            for task in (lease_monitor_task, event_task, callback_task, *sends)
            if task is not None and task is not current
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if session is not None:
            await self._close_session_once(session)
        if tasks:
            await asyncio.wait(tasks, timeout=_CLEANUP_TIMEOUT)
        pending_background = tuple(self._background_tasks)
        if pending_background:
            await asyncio.wait(pending_background, timeout=_CLEANUP_TIMEOUT)

    async def close(self) -> None:
        """Idempotently stop events and close only the session this object owns."""
        await self._shutdown("closed")


__all__ = ["QQCallGroupBindingError", "QQCallGroupUniversalBinding"]
