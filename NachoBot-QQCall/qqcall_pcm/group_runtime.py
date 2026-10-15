"""Owned PCM and UniversalVC lifecycle for one verified QQCall group room.

This module is loaded only when the optional group-audio path is activated.
It contains no signal processing or recognition logic: the bounded PCM backend
transports canonical frames and UniversalVC owns the pipeline.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .group_protocol import validate_group_scope
from qqcall_client.qqcall_group_wire import validate_group_audio_claim


_QQ_UIN = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)
_SCOPE_FIELDS = (
    "kind",
    "serviceInstance",
    "hostGeneration",
    "connectionGeneration",
    "groupId",
    "roomId",
    "roomGeneration",
)
_PROOF_TTL_SECONDS = 2.0
# Leave almost the entire two-second validity window for a fresh native scan.
# A 250 ms idle wait plus a valid 1.85 s scan creates an avoidable expiry gap.
_WATCH_INTERVAL_SECONDS = 0.05
_CLOSE_TIMEOUT_SECONDS = 4.0
_STARTUP_DRAIN_TIMEOUT_SECONDS = 20.0
# Reuse the shared message library's configured handler without enabling the
# application's root logger or changing other PCM modules' verbosity.
_LOGGER = logging.getLogger(f"ncnk_message.{__name__}")
_STARTUP_FAILURE_CODES = frozenset({
    "audio_already_started", "audio_backend_busy", "audio_cleanup_failed", "audio_dependency_unavailable",
    "audio_device_ambiguous", "audio_endpoint_not_virtual", "audio_format_invalid", "audio_start_failed",
    "audio_start_invalid", "audio_start_timeout", "capture_device_unavailable", "capture_failed",
    "audio_fence_stale", "capture_callback_failed", "capture_device_overflow", "capture_processing_failed",
    "capture_queue_overrun", "group_audio_watch_failed", "universal_api_capture_unsupported",
    "group_audio_lease_lost", "group_audio_lease_unavailable", "group_audio_stopped",
    "lease_not_current", "session_create_failed", "session_create_timeout", "universal_api_unavailable",
})


class QQCallGroupAudioRuntimeError(RuntimeError):
    """A sanitized group-audio failure code."""

    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code or "") else "group_audio_failed"
        super().__init__(self.code)


FreshLease = Callable[[dict[str, Any]], Awaitable[object]]
ClientFactory = Callable[[], Any]
MessageSender = Callable[[Any], Awaitable[Any]]
LocalFence = Callable[[], bool]
FailureCallback = Callable[[str], Any]


class GroupAudioRuntime:
    """Own one group scope's client, session, backend, proof cache, and turn."""

    def __init__(
        self,
        scope: Mapping[str, Any],
        requester_user_id: str,
        backend: Any,
        client_factory: ClientFactory,
        fresh_lease: FreshLease,
        send_message: MessageSender,
        local_fence: LocalFence,
        on_failure: FailureCallback,
        *,
        group_name: str = "",
    ) -> None:
        try:
            self._scope = validate_group_scope(dict(scope))
        except (TypeError, ValueError):
            raise ValueError("invalid group audio scope") from None
        if type(requester_user_id) is not str or _QQ_UIN.fullmatch(requester_user_id) is None:
            raise ValueError("invalid group audio requester")
        if backend is None or not callable(client_factory) or not callable(fresh_lease):
            raise TypeError("invalid group audio owner")
        if not callable(send_message) or not callable(local_fence) or not callable(on_failure):
            raise TypeError("invalid group audio callback")
        if type(group_name) is not str or len(group_name) > 256:
            raise ValueError("invalid group name")
        self.requester_user_id = requester_user_id
        self.group_id = self._scope["groupId"]
        self.backend = backend
        self._client_factory = client_factory
        self._fresh_lease = fresh_lease
        self._send_message = send_message
        self._local_fence = local_fence
        self._on_failure = on_failure
        self._group_name = group_name
        self._lock = threading.RLock()
        self._stop_lock = asyncio.Lock()
        self._lease_refresh_lock = asyncio.Lock()
        self._state = "new"
        self._failure_reason: str | None = None
        self._proof_at: float | None = None
        self._refresh_elapsed_ms = 0
        self._refresh_inflight_at: float | None = None
        self._loop_tick_at: float | None = None
        self._max_loop_gap_ms = 0
        self._binding: Any | None = None
        self._client: Any | None = None
        self._watch_task: asyncio.Task[Any] | None = None
        self._loop_probe_task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        self._startup_settled: asyncio.Event | None = None
        self._accepted_claim: dict[str, Any] | None = None
        self._accepted_turn_id: str | None = None
        self._turn_generation = 0
        self._accepted_turn_count = 0
        self._speech_result_count = 0
        self._forwarded_result_count = 0
        self._last_speech_outcome: str | None = None
        self._playback_count = 0
        self._last_playback_drained = False

    @property
    def scope(self) -> dict[str, Any]:
        return dict(self._scope)

    @property
    def binding(self) -> Any | None:
        return self._binding

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict[str, Any]:
        """Return counts and sanitized state only."""
        with self._lock:
            return {
                "state": self._state,
                "ready": self._state == "active" and self.is_current(),
                "acceptedTurns": self._accepted_turn_count,
                "speechResults": self._speech_result_count,
                "forwardedResults": self._forwarded_result_count,
                "lastSpeechOutcome": self._last_speech_outcome,
                "playbacks": self._playback_count,
                "lastPlaybackDrained": self._last_playback_drained,
                "lastFailureCode": self._failure_reason,
            }

    def _context_current(self) -> bool:
        try:
            result = self._local_fence()
        except Exception:
            return False
        return result is True and not inspect.isawaitable(result)

    def is_current(self) -> bool:
        """Synchronous fence used by the native capture/playback callbacks."""
        with self._lock:
            proof_at = self._proof_at
            state = self._state
        return bool(
            state in {"starting", "active"}
            and proof_at is not None
            and 0 <= time.monotonic() - proof_at <= _PROOF_TTL_SECONDS
            and self._context_current()
        )

    async def _refresh_lease(self) -> bool:
        # Startup and the background watcher share one native observer. Each
        # caller still obtains a fresh proof, but never races another refresh.
        async with self._lease_refresh_lock:
            return await self._refresh_lease_serialized()

    async def _refresh_lease_serialized(self) -> bool:
        started_at = time.monotonic()
        with self._lock:
            self._refresh_inflight_at = started_at
        try:
            result = self._fresh_lease(dict(self._scope))
            if not inspect.isawaitable(result):
                return False
            result = await asyncio.wait_for(result, timeout=_PROOF_TTL_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            _LOGGER.warning("QQCall group audio refresh failed code=lease_refresh_timeout")
            result = False
        except Exception:
            _LOGGER.warning("QQCall group audio refresh failed code=lease_refresh_error")
            result = False
        finally:
            with self._lock:
                self._refresh_inflight_at = None
        self._refresh_elapsed_ms = round((time.monotonic() - started_at) * 1000)
        context_current = self._context_current() if result is True else None
        if result is not True or not context_current:
            _LOGGER.warning("QQCall group audio refresh rejected code=%s",
                            "lease_refresh_rejected" if result is not True else "lease_refresh_context_lost")
            with self._lock:
                self._proof_at = None
            return False
        with self._lock:
            stop_event = self._stop_event
            if self._state not in {"starting", "active"} or (stop_event is not None and stop_event.is_set()):
                self._proof_at = None
                return False
            self._proof_at = time.monotonic()
        return True

    def _require_starting(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            if self._state != "starting" or (stop_event is not None and stop_event.is_set()):
                raise QQCallGroupAudioRuntimeError("group_audio_stopped")

    def _finish_startup(self) -> None:
        with self._lock:
            event = self._startup_settled
            if event is not None:
                event.set()

    async def _close_client(self, client: Any) -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            pass

    async def start(self, *, on_binding: Callable[[Any], Any] | None = None) -> None:
        """Start only after an external fresh native, ACL, route, and Core proof."""
        with self._lock:
            if self._state != "new":
                raise QQCallGroupAudioRuntimeError("group_audio_already_started")
            self._state = "starting"
            self._stop_event = asyncio.Event()
            self._startup_settled = asyncio.Event()
        stage = "lease"
        try:
            if not await self._refresh_lease():
                raise QQCallGroupAudioRuntimeError("group_audio_lease_unavailable")
            self._require_starting()
            # Keep the independent native/ACL/Core/audio-route proof alive
            # while UniversalVC creates its cold speaker session and the
            # selected-device backend resolves and opens WASAPI resources.
            self._watch_task = asyncio.create_task(
                self._watch_lease(), name="qqcall-group-audio-lease-watch",
            )
            self._loop_probe_task = asyncio.create_task(
                self._probe_loop(), name="qqcall-group-audio-loop-probe",
            )
            self._require_starting()
            stage = "universal_client"
            client = self._client_factory()
            if inspect.isawaitable(client):
                client = await asyncio.wait_for(client, timeout=_CLOSE_TIMEOUT_SECONDS)
            with self._lock:
                install_client = self._state == "starting" and not self._stop_event.is_set()
                if install_client:
                    self._client = client
            if not install_client:
                await self._close_client(client)
                self._require_starting()
            if client is None or not callable(getattr(client, "create_session", None)):
                raise QQCallGroupAudioRuntimeError("universal_api_unavailable")
            self._require_starting()

            # This import is intentionally inside the audio-enabled path. Core
            # request parsing and status-only use never load PCM runtime code.
            from qqcall_client.qqcall_group_universal_api import QQCallGroupUniversalBinding

            binding = QQCallGroupUniversalBinding(
                client,
                self._cached_lease,
                self._on_speech_result,
                on_speech_start=self._on_speech_start,
            )
            with self._lock:
                install_binding = self._state == "starting" and not self._stop_event.is_set()
                if install_binding:
                    self._binding = binding
            if not install_binding:
                await binding.close()
                self._require_starting()
            if on_binding is not None:
                on_binding(binding)
            self._require_starting()
            stage = "universal_session"
            await binding.start(dict(self._scope))
            self._require_starting()
            if not await self._refresh_lease():
                raise QQCallGroupAudioRuntimeError("group_audio_lease_lost")

            async def send_pcm(pcm: bytes, *, source_id: str = "main") -> dict[str, bool]:
                current_binding = self._binding
                if source_id != "main" or current_binding is None or not self.is_current():
                    raise QQCallGroupAudioRuntimeError("group_audio_lease_lost")
                await current_binding.send_pcm(dict(self._scope), pcm)
                if not self.is_current():
                    raise QQCallGroupAudioRuntimeError("group_audio_lease_lost")
                return {"accepted": True}

            self._require_starting()
            stage = "pcm_capture"
            await self.backend.start(
                None,
                self._on_backend_failure,
                dict(self._scope),
                send_pcm=send_pcm,
                capture_fence=self.is_current,
            )
            self._require_starting()
            if not await self._refresh_lease():
                raise QQCallGroupAudioRuntimeError("group_audio_lease_lost")
            with self._lock:
                if self._state != "starting" or self._stop_event.is_set():
                    raise QQCallGroupAudioRuntimeError("group_audio_stopped")
                self._state = "active"
        except asyncio.CancelledError:
            self._finish_startup()
            await self._shutdown("stopped")
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            _LOGGER.warning("QQCall group audio startup failed stage=%s code=%s owner_failure=%s",
                            stage, code if type(code) is str and code in _STARTUP_FAILURE_CODES else "unclassified",
                            self._failure_reason if self._failure_reason in _STARTUP_FAILURE_CODES else "unclassified")
            if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code) is None:
                code = "group_audio_start_failed"
            self._finish_startup()
            await self._shutdown(code)
            raise QQCallGroupAudioRuntimeError(code) from None
        else:
            self._finish_startup()

    async def _cached_lease(self, scope: dict[str, Any]) -> bool:
        if scope != self._scope:
            return False
        return self.is_current()

    async def _watch_lease(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        try:
            while True:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_WATCH_INTERVAL_SECONDS)
                    return
                except asyncio.TimeoutError:
                    pass
                if not await self._refresh_lease():
                    with self._lock:
                        if self._state not in {"starting", "active"}:
                            return
                    await self._fail("group_audio_lease_lost")
                    return
                with self._lock:
                    if self._state not in {"starting", "active"}:
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail("group_audio_watch_failed")

    async def _probe_loop(self) -> None:
        """Timing only: never renew or extend an audio authorization proof."""
        with self._lock:
            self._loop_tick_at = time.monotonic()
        while self._stop_event is not None and not self._stop_event.is_set():
            await asyncio.sleep(_WATCH_INTERVAL_SECONDS)
            now = time.monotonic()
            with self._lock:
                gap = round((now - self._loop_tick_at) * 1000)
                self._max_loop_gap_ms = max(self._max_loop_gap_ms, gap)
                self._loop_tick_at = now

    def _timing_diagnostics(self) -> dict[str, int | bool]:
        with self._lock:
            now = time.monotonic()
            return {
                "refresh_inflight_ms": -1 if self._refresh_inflight_at is None else
                    round((now - self._refresh_inflight_at) * 1000),
                "loop_tick_age_ms": -1 if self._loop_tick_at is None else
                    round((now - self._loop_tick_at) * 1000),
                "max_loop_gap_ms": self._max_loop_gap_ms,
                "watch_done": self._watch_task is None or self._watch_task.done(),
            }

    async def _on_backend_failure(self, code: str) -> None:
        safe_code = code if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", code) else "audio_backend_failed"
        with self._lock:
            proof_age_ms = -1 if self._proof_at is None else round((time.monotonic() - self._proof_at) * 1000)
            state = self._state
        logged_code = safe_code if safe_code in _STARTUP_FAILURE_CODES else "unclassified"
        _LOGGER.warning("QQCall group audio backend failure code=%s state=%s proof_age_ms=%s context_current=%s refresh_elapsed_ms=%s timing=%s",
                        logged_code, state, proof_age_ms, self._context_current(), self._refresh_elapsed_ms,
                        self._timing_diagnostics())
        await self._fail(safe_code)

    async def _on_speech_start(self) -> None:
        with self._lock:
            if self._state not in {"starting", "active"}:
                return
            self._accepted_claim = None
            self._accepted_turn_id = None
            self._turn_generation += 1
        started_at = time.monotonic()
        _LOGGER.info("QQCall group barge stage=claim_invalidated at_ms=%s", round(time.time() * 1000))
        cancel = getattr(self.backend, "cancel_playback", None)
        outcome = "unavailable"
        try:
            if callable(cancel):
                outcome = "incomplete"
                result = cancel()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=_CLOSE_TIMEOUT_SECONDS)
                outcome = "completed"
        finally:
            _LOGGER.info(
                "QQCall group barge stage=cancel_settled at_ms=%s elapsed_ms=%s outcome=%s",
                round(time.time() * 1000),
                min(60000, max(0, round((time.monotonic() - started_at) * 1000))),
                outcome,
            )

    def _speech_outcome(self, code: str) -> bool:
        # Only internal, fixed codes reach this method. Do not log transcripts,
        # raw session identifiers, speaker identities, or exception messages.
        with self._lock:
            self._last_speech_outcome = code
            received = self._speech_result_count
            forwarded = self._forwarded_result_count
        _LOGGER.info(
            "QQCall group speech delivery outcome=%s received=%s forwarded=%s",
            code, received, forwarded,
        )
        return code == "forwarded_current"

    async def _on_speech_result(self, envelope: dict[str, Any]) -> bool:
        from qqcall_client.qqcall_group_message import build_qqcall_group_voice_message

        with self._lock:
            self._speech_result_count += 1
        if not self.is_current():
            return self._speech_outcome("scope_stale_before_delivery")
        try:
            if type(envelope) is not dict or envelope.get("scope") != self._scope:
                return self._speech_outcome("envelope_scope_invalid")
            annotation = envelope.get("speaker_annotation")
            result = envelope.get("speech_result")
            session_identity = envelope.get("session_identity")
            if type(annotation) is not dict or type(result) is not dict or type(session_identity) is not dict:
                return self._speech_outcome("envelope_invalid")
            claim = validate_group_audio_claim({
                "turn_id": result.get("turn_id"),
                "uvc_session": session_identity,
                "speaker_ref": annotation.get("speaker_ref"),
            })
            message = build_qqcall_group_voice_message(
                envelope,
                self.requester_user_id,
                self._group_name,
            )
        except (TypeError, ValueError):
            return self._speech_outcome("envelope_invalid")
        with self._lock:
            if self._state != "active":
                return self._speech_outcome("runtime_inactive")
        binding = self._binding
        if binding is None or not await binding.verify_result(self._scope, claim) or not self.is_current():
            return self._speech_outcome("binding_stale_before_delivery")

        with self._lock:
            if self._state != "active":
                return self._speech_outcome("runtime_inactive")
            self._accepted_claim = claim
            self._accepted_turn_id = claim["turn_id"]
            self._turn_generation += 1
            generation = self._turn_generation

        outcome = "send_rejected"
        try:
            sent = await asyncio.wait_for(self._send_message(message), timeout=8.0)
        except asyncio.CancelledError:
            self._speech_outcome("send_cancelled")
            raise
        except asyncio.TimeoutError:
            sent = False
            outcome = "send_timeout"
        except Exception:
            sent = False
            outcome = "send_failed"
        if sent is True:
            with self._lock:
                self._forwarded_result_count += 1
            # A successful sender callback proves transport handoff only,
            # not Core processing, a Replyer response, or remote playback.
            outcome = "forwarded_then_stale"
        binding_current = await binding.verify_result(self._scope, claim) if sent is True else False
        with self._lock:
            still_current = (
                generation == self._turn_generation
                and self._accepted_claim == claim
                and self._accepted_turn_id == claim["turn_id"]
                and self._state in {"starting", "active"}
            )
            if sent is True and binding_current is True and still_current and self.is_current():
                self._accepted_turn_count += 1
                return self._speech_outcome("forwarded_current")
            if generation == self._turn_generation and self._accepted_claim == claim:
                self._accepted_claim = None
                self._accepted_turn_id = None
                self._turn_generation += 1
        return self._speech_outcome(outcome)

    async def verify_result(self, scope: Any, claim: Any) -> bool:
        try:
            validated_scope = validate_group_scope(scope)
            validated_claim = validate_group_audio_claim(claim)
        except (TypeError, ValueError):
            return False
        if validated_scope != self._scope or not self.is_current():
            return False
        with self._lock:
            current_claim = self._accepted_claim
            turn_generation = self._turn_generation
        if current_claim != validated_claim:
            return False
        binding = self._binding
        if binding is None or not await binding.verify_result(validated_scope, validated_claim):
            return False
        with self._lock:
            return bool(
                self._accepted_claim == validated_claim
                and self._turn_generation == turn_generation
                and self._state == "active"
                and self.is_current()
            )

    async def verify_current_turn(self, scope: Any, requester: str, turn_id: str) -> bool:
        if type(requester) is not str or requester != self.requester_user_id or type(turn_id) is not str:
            return False
        try:
            validated_scope = validate_group_scope(scope)
        except (TypeError, ValueError):
            return False
        if validated_scope != self._scope or not self.is_current():
            return False
        with self._lock:
            claim = self._accepted_claim
        if claim is None or claim.get("turn_id") != turn_id:
            return False
        binding = self._binding
        return bool(
            binding is not None
            and await binding.verify_result(validated_scope, claim)
            and self.is_current()
        )

    async def play_core_segment(self, scope: Any, requester: str, turn_id: str, kind: str, data: Any) -> dict[str, Any]:
        if (
            type(requester) is not str
            or requester != self.requester_user_id
            or type(turn_id) is not str
        ):
            raise QQCallGroupAudioRuntimeError("stale_group_scope")
        try:
            validated_scope = validate_group_scope(scope)
        except (TypeError, ValueError):
            raise QQCallGroupAudioRuntimeError("stale_group_scope") from None
        if validated_scope != self._scope or not self.is_current():
            raise QQCallGroupAudioRuntimeError("group_audio_unavailable")
        with self._lock:
            claim = self._accepted_claim
            generation = self._turn_generation
        if claim is None or claim.get("turn_id") != turn_id:
            raise QQCallGroupAudioRuntimeError("stale_group_scope")
        binding = self._binding
        if binding is None or not await binding.verify_result(validated_scope, claim) or not self.is_current():
            raise QQCallGroupAudioRuntimeError("stale_group_scope")

        def playback_fence() -> bool:
            with self._lock:
                still_current = (
                    self._state == "active"
                    and self._accepted_claim == claim
                    and self._turn_generation == generation
                )
            return still_current and self.is_current()

        if not callable(getattr(self.backend, "play_core_segment", None)):
            raise QQCallGroupAudioRuntimeError("audio_playback_unavailable")
        result = await self.backend.play_core_segment(kind, data, playback_fence)
        if not playback_fence():
            raise QQCallGroupAudioRuntimeError("stale_group_scope")
        with self._lock:
            self._playback_count += 1
            self._last_playback_drained = bool(
                isinstance(result, Mapping)
                and result.get("render_drained") is True
                and result.get("drain_confirmed") is True
            )
        return dict(result) if isinstance(result, Mapping) else {}

    async def _fail(self, code: str) -> None:
        logged_code = code if code in _STARTUP_FAILURE_CODES or code in {
            "group_audio_lease_lost", "group_audio_watch_failed",
        } else "unclassified"
        _LOGGER.warning("QQCall group audio stopped code=%s", logged_code)
        await self._shutdown(code)
        try:
            result = self._on_failure(code)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            pass

    async def stop(self, reason: str = "stopped") -> None:
        await self._shutdown(reason)

    async def _shutdown(self, reason: str) -> None:
        async with self._stop_lock:
            with self._lock:
                if self._state in {"stopped", "failed"}:
                    return
                self._state = "stopping"
                self._failure_reason = (
                    reason if reason != "stopped" and re.fullmatch(r"[a-z][a-z0-9_:-]{1,63}", reason or "")
                    else self._failure_reason
                )
                self._proof_at = None
                self._accepted_claim = None
                self._accepted_turn_id = None
                self._turn_generation += 1
            stop_event = self._stop_event
            if stop_event is not None:
                stop_event.set()
            watch_task, self._watch_task = self._watch_task, None
            loop_probe, self._loop_probe_task = self._loop_probe_task, None
            current = asyncio.current_task()
            if loop_probe is not None and loop_probe is not current and not loop_probe.done():
                loop_probe.cancel()
                await asyncio.gather(loop_probe, return_exceptions=True)
            if watch_task is not None and watch_task is not current and not watch_task.done():
                watch_task.cancel()
                await asyncio.gather(watch_task, return_exceptions=True)
            startup_settled = self._startup_settled
            if startup_settled is not None and not startup_settled.is_set():
                try:
                    await asyncio.wait_for(
                        startup_settled.wait(),
                        timeout=_STARTUP_DRAIN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Every production startup operation has its own shorter
                    # bound. Even if an injected factory resists cancellation,
                    # state=stopping fences its return from opening the backend.
                    pass
            try:
                cancel = getattr(self.backend, "cancel_playback", None)
                if callable(cancel):
                    value = cancel()
                    if inspect.isawaitable(value):
                        await asyncio.wait_for(value, timeout=_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
            binding, self._binding = self._binding, None
            if binding is not None:
                try:
                    await asyncio.wait_for(binding.close(), timeout=_CLOSE_TIMEOUT_SECONDS)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self.backend.stop(), timeout=_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
            client, self._client = self._client, None
            await self._close_client(client)
            with self._lock:
                self._state = "failed" if self._failure_reason is not None else "stopped"


__all__ = ["GroupAudioRuntime", "QQCallGroupAudioRuntimeError"]
