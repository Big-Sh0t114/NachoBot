"""Core multimodal routing and reply materialization."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from ncnk_message import Seg

from .client import LocalMultimodalClient
from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    ASR_RESULT_TTL_SECONDS,
    ASR_STREAM_CHANNELS,
    ASR_STREAM_CHUNK_MAX_BYTES,
    ASR_STREAM_IDLE_TIMEOUT_SECONDS,
    ASR_STREAM_MAX_CHUNKS,
    ASR_STREAM_MAX_CONCURRENCY,
    ASR_STREAM_MAX_DURATION_SECONDS,
    ASR_STREAM_MAX_PCM_BYTES,
    ASR_STREAM_PLATFORMS,
    ASR_STREAM_SAMPLE_RATE,
    IMAGE_DESCRIBE_V1,
    VIDEO_UNDERSTAND_V1,
    MAX_TEXT_CHARS,
    MediaInput,
    PerceptionResult,
    TTSResult,
    TTSStreamChunk,
    TTSStreamError,
    decode_bounded_base64,
    normalize_asr_stream_platform,
    normalize_asr_stream_scope,
    normalize_operation_payload,
)
from .profile import RuntimeProfile, get_runtime_profile, normalize_runtime_profile

logger = logging.getLogger("core.multimodal")


class AudioStreamError(RuntimeError):
    """An expected audio-stream API failure with its HTTP status."""

    def __init__(self, status_code: int, code: str):
        self.status_code = status_code
        self.code = code
        super().__init__(code)


@dataclass
class _ModelLease:
    request: Any
    model_name: str
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.request.release_model_lease(self.model_name)


@dataclass
class _AudioStreamSession:
    stream_id: str
    runtime_id: str
    platform: str
    request: Any
    model_name: str
    provider_name: str
    model_identifier: str
    lease: _ModelLease
    created_at: float
    last_activity: float
    scope: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_seq: int = 0
    chunk_digests: dict[int, str] = field(default_factory=dict)
    chunk_count: int = 0
    bytes_received: int = 0
    partial_text: str = ""


@dataclass(frozen=True)
class _ASRReceipt:
    text: str
    context: str
    expires_at: float
    scope: str = ""


def _failure_text(operation: str) -> str:
    return {
        AUDIO_TRANSCRIBE_V1: "[语音识别失败，请稍后重试]",
        IMAGE_DESCRIBE_V1: "[图片(解析失败)]",
        VIDEO_UNDERSTAND_V1: "[视频(解析失败)]",
    }.get(operation, "[多媒体解析失败]")


def _nonempty_tts_text(data: Any) -> str:
    if isinstance(data, Mapping):
        value = data.get("text")
    else:
        value = data
    return str(value or "").strip()[:MAX_TEXT_CHARS]


def _tts_fallback_text(data: Any) -> str:
    if isinstance(data, Mapping):
        value = data.get("display_text")
        if value is None:
            value = data.get("text")
    else:
        value = data
    return str(value or "")[:MAX_TEXT_CHARS]


class CoreMultimodalRouter:
    """Single Core facade for all perception and explicit reply TTS."""

    def __init__(
        self,
        *,
        profile: RuntimeProfile | str | None = None,
        local: Any = None,
        remote: Any = None,
        model_config: Any = None,
        local_endpoint: str | None = None,
        tts_endpoint: str | None = None,
        local_timeout: float = 30.0,
    ):
        self.profile = normalize_runtime_profile(profile) if profile is not None else get_runtime_profile()
        self.local = (
            local
            if local is not None
            else LocalMultimodalClient(
                local_endpoint,
                tts_endpoint=tts_endpoint,
                timeout=local_timeout,
            )
        )
        # The legacy remote injection remains accepted for callers that only
        # construct this facade. Perception dispatch is owned by the model
        # group, not by a local/remote provider chain.
        self.remote = remote
        self._model_config = model_config
        self._model_requests: dict[str, tuple[Any, tuple[str, ...], Any]] = {}
        self._observed_health: Mapping[str, Any] | None = None
        self._audio_streams: dict[str, _AudioStreamSession] = {}
        self._asr_receipts: dict[str, _ASRReceipt] = {}
        self._audio_stream_lock = asyncio.Lock()
        self._audio_stream_cleanup_task: asyncio.Task | None = None
        self._pending_audio_stream_starts = 0
        self._audio_stream_starts_idle = asyncio.Event()
        self._audio_stream_starts_idle.set()
        self._audio_streams_closing = False

    @property
    def desired_profile(self) -> str:
        return self.profile.value

    @property
    def observed_health(self) -> Mapping[str, Any] | None:
        return self._observed_health

    @staticmethod
    def _component_ready(observed: Mapping[str, Any]) -> bool:
        return bool(observed.get("ready", observed.get("status") == "ok"))

    async def _probe_component(self, method_name: str) -> dict[str, Any]:
        try:
            method = getattr(self.local, method_name)
            observed = await method()
            if not isinstance(observed, Mapping):
                raise TypeError("health response is not an object")
            return {
                "required": True,
                "ready": self._component_ready(observed),
                "observed": dict(observed),
            }
        except Exception as exc:
            return {
                "required": True,
                "ready": False,
                "error": type(exc).__name__,
            }

    async def health(self) -> Mapping[str, Any]:
        """Probe only components required by the explicit product profile.

        FULL requires eager local perception (9874) and the public TTS engine
        (9880). LITE intentionally has no local perception process, so only
        9880 is probed.
        POTATO is a healthy text-only Core mode and requires neither model
        component; the compatibility listener may exist but does not gate it.
        """

        perception_required = self.profile.allows_local_perception
        tts_required = self.profile.allows_tts
        perception: dict[str, Any] = {"required": False, "ready": True}
        tts: dict[str, Any] = {"required": False, "ready": True}

        probes: list[Any] = []
        probe_names: list[str] = []
        if perception_required:
            probes.append(self._probe_component("health"))
            probe_names.append("perception")
        if tts_required:
            probes.append(self._probe_component("tts_health"))
            probe_names.append("tts")
        if probes:
            for name, result in zip(probe_names, await asyncio.gather(*probes)):
                if name == "perception":
                    perception = result
                else:
                    tts = result

        self._observed_health = {
            "ready": bool(perception["ready"] and tts["ready"]),
            "profile": self.desired_profile,
            "perception": perception,
            "tts": tts,
        }
        return self._observed_health

    async def perceive(self, request: MediaInput) -> PerceptionResult:
        # Keep the semantic task before format conversion or transport.
        encoded, _ = normalize_operation_payload(request.operation, request.data)
        request = replace(request, data=encoded)
        task_name = request.task or {
            AUDIO_TRANSCRIBE_V1: "voice",
            IMAGE_DESCRIBE_V1: "vlm",
            VIDEO_UNDERSTAND_V1: "video",
        }[request.operation]
        attempts: list[str] = []
        if self.profile is not RuntimeProfile.POTATO:
            try:
                from src.llm_models.exceptions import ModelAttemptFailed
                from src.llm_models.model_client.base_client import APIResponse
                from src.llm_models.utils_model import ModelCandidateUnavailable

                config, llm = self._request_for_task(task_name)

                async def execute_candidate(model: Any, provider: Any, default_attempt: Any) -> Any:
                    attempts.append(model.name)
                    if not self._is_local_perception_provider(provider):
                        response = await default_attempt()
                        if not str(response.content or "").strip():
                            raise ModelAttemptFailed(f"model '{model.name}' returned empty perception text")
                        return response
                    if not self.profile.allows_local_perception:
                        raise ModelCandidateUnavailable("local perception disabled by runtime profile")
                    try:
                        capabilities = await self.local.health()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise ModelAttemptFailed("local runtime unavailable", exc) from exc
                    if not isinstance(capabilities, Mapping) or not self._component_ready(capabilities):
                        raise ModelCandidateUnavailable("local runtime is not ready")
                    operations = capabilities.get("operations")
                    if not isinstance(operations, (list, tuple, set)) or request.operation not in operations:
                        raise ModelCandidateUnavailable("local runtime does not provide the operation")
                    loaded_models = capabilities.get("models")
                    loaded_identifier = loaded_models.get(request.operation) if isinstance(loaded_models, Mapping) else None
                    selected_identifier = getattr(model, "model_identifier", model.name)
                    if str(loaded_identifier or "").casefold() != str(selected_identifier).casefold():
                        raise ModelCandidateUnavailable("local runtime does not serve the selected model")
                    try:
                        result = await self.local.perceive(request)
                        if not result.text.strip():
                            raise RuntimeError("empty local perception text")
                        return APIResponse(content=result.text)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        reason = getattr(exc, "reason", "inference_failure")
                        raise ModelAttemptFailed(f"local perception {reason}", exc) from exc

                if request.operation == AUDIO_TRANSCRIBE_V1:
                    text, winner = await llm.generate_response_for_voice_with_model(
                        request.data, candidate_executor=execute_candidate
                    )
                elif request.operation == IMAGE_DESCRIBE_V1:
                    text, (_, winner, _) = await llm.generate_response_for_image(
                        request.prompt,
                        request.data,
                        request.media_format or "png",
                        temperature=self._metadata_number(request.metadata, "temperature"),
                        max_tokens=self._metadata_int(request.metadata, "max_tokens"),
                        extra_params=self._extra_params(request.metadata),
                        candidate_executor=execute_candidate,
                    )
                else:
                    text, (_, winner, _) = await llm.generate_response_for_video(
                        request.prompt,
                        request.data,
                        request.media_format or "mp4",
                        temperature=self._metadata_number(request.metadata, "temperature"),
                        max_tokens=self._metadata_int(request.metadata, "max_tokens"),
                        extra_params=self._extra_params(request.metadata),
                        candidate_executor=execute_candidate,
                    )
                provider = config.get_provider(config.get_model_info(winner).api_provider)
                backend = "local" if self._is_local_perception_provider(provider) else "remote"
                return PerceptionResult(
                    operation=request.operation,
                    text=str(text or "").strip()[:MAX_TEXT_CHARS],
                    provider=backend,
                    attempted=tuple(attempts),
                    metadata={"model": winner, "task": task_name},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s model group %s exhausted: %s", request.operation, task_name, type(exc).__name__)
        return PerceptionResult(
            operation=request.operation,
            text=_failure_text(request.operation),
            provider="degraded",
            degraded=True,
            attempted=tuple(attempts),
            error="perception model group unavailable",
        )

    def _request_for_task(self, task_name: str) -> tuple[Any, Any]:
        if self._model_config is None:
            from src.config.config import model_config

            self._model_config = model_config
        config = self._model_config
        task = getattr(config.model_task_config, task_name, None)
        if task is None or not getattr(task, "model_list", None):
            raise ValueError(f"perception model group {task_name} is empty")
        model_names = tuple(task.model_list)
        cached = self._model_requests.get(task_name)
        if cached is None or cached[0] is not task or cached[1] != model_names:
            from src.llm_models.utils_model import LLMRequest

            cached = (task, model_names, LLMRequest(model_set=task, request_type=task_name, config=config))
            self._model_requests[task_name] = cached
        return config, cached[2]

    @staticmethod
    def _is_local_perception_provider(provider: Any) -> bool:
        try:
            url = urlsplit(str(getattr(provider, "base_url", "") or ""))
            return url.port == 9874 and url.hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            return False

    @staticmethod
    def _stream_error(exc: Exception) -> AudioStreamError:
        status = getattr(exc, "status_code", 503)
        if status not in {400, 404, 409, 501, 503}:
            status = 503
        code = str(getattr(exc, "reason", "runtime_unavailable"))
        return AudioStreamError(status, code)

    def _local_voice_candidates(self, config: Any, request: Any) -> set[str]:
        candidates: set[str] = set()
        for model_name in tuple(request.model_for_task.model_list):
            model = config.get_model_info(model_name)
            provider = config.get_provider(model.api_provider)
            if self._is_local_perception_provider(provider):
                candidates.add(model_name)
        return candidates

    async def start_audio_stream(
        self,
        *,
        sample_rate: int = ASR_STREAM_SAMPLE_RATE,
        channels: int = ASR_STREAM_CHANNELS,
        platform: str = "universal_vc",
        scope: str = "",
    ) -> Mapping[str, Any]:
        """Start a stream pinned to one configured local voice candidate."""

        if sample_rate != ASR_STREAM_SAMPLE_RATE or channels != ASR_STREAM_CHANNELS:
            raise AudioStreamError(400, "invalid_audio_format")
        normalized_platform = normalize_asr_stream_platform(platform)
        if normalized_platform not in ASR_STREAM_PLATFORMS:
            raise AudioStreamError(400, "invalid_platform")
        normalized_scope = normalize_asr_stream_scope(scope)
        if normalized_scope is None:
            raise AudioStreamError(400, "invalid_scope")
        if not self.profile.allows_local_perception:
            raise AudioStreamError(501, "local_asr_streaming_unavailable")

        async with self._audio_stream_lock:
            if self._audio_streams_closing:
                raise AudioStreamError(503, "core_shutting_down")
            if len(self._audio_streams) + self._pending_audio_stream_starts >= ASR_STREAM_MAX_CONCURRENCY:
                raise AudioStreamError(503, "busy")
            self._pending_audio_stream_starts += 1
            self._audio_stream_starts_idle.clear()

        lease: _ModelLease | None = None
        runtime_id: str | None = None
        try:
            config, request = self._request_for_task("voice")
            eligible_models = self._local_voice_candidates(config, request)
            if not eligible_models:
                raise AudioStreamError(501, "no_local_asr_stream_candidate")

            try:
                model, provider, _client = request.acquire_model_lease(eligible_models)
            except Exception as exc:
                raise AudioStreamError(501, "no_local_asr_stream_candidate") from exc
            model_name = str(model.name)
            lease = _ModelLease(request=request, model_name=model_name)
            model_identifier = str(getattr(model, "model_identifier", "") or "").strip()
            if not model_identifier:
                raise AudioStreamError(501, "unrecognized_asr_model")

            try:
                capabilities = await self.local.health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise self._stream_error(exc) from exc
            if not isinstance(capabilities, Mapping) or not self._component_ready(capabilities):
                raise AudioStreamError(503, "local_runtime_unavailable")
            operations = capabilities.get("operations")
            if not isinstance(operations, (list, tuple, set)) or AUDIO_TRANSCRIBE_V1 not in operations:
                raise AudioStreamError(501, "asr_operation_unavailable")
            loaded_models = capabilities.get("models")
            loaded_identifier = (
                loaded_models.get(AUDIO_TRANSCRIBE_V1)
                if isinstance(loaded_models, Mapping)
                else None
            )
            if loaded_identifier != model_identifier:
                raise AudioStreamError(501, "unrecognized_asr_model")
            if capabilities.get("streaming_asr") is not True:
                raise AudioStreamError(501, "asr_streaming_unavailable")

            try:
                runtime_id = await self.local.start_audio_stream(model_identifier)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise self._stream_error(exc) from exc

            now = time.monotonic()
            stream_id = secrets.token_urlsafe(24)
            provider_name = str(getattr(provider, "name", "") or getattr(model, "api_provider", "local"))
            session = _AudioStreamSession(
                stream_id=stream_id,
                runtime_id=runtime_id,
                platform=normalized_platform,
                request=request,
                model_name=model_name,
                provider_name=provider_name,
                model_identifier=model_identifier,
                lease=lease,
                created_at=now,
                last_activity=now,
                scope=normalized_scope,
            )
            async with self._audio_stream_lock:
                if self._audio_streams_closing:
                    should_abort = True
                else:
                    self._audio_streams[stream_id] = session
                    should_abort = False
            if should_abort:
                try:
                    await self.local.abort_audio_stream(runtime_id)
                except Exception:
                    pass
                runtime_id = None
                raise AudioStreamError(503, "core_shutting_down")
            lease = None  # Ownership moved into the registered stream session.
            self._ensure_audio_stream_cleanup_task()
            return {"stream_id": stream_id}
        except AudioStreamError:
            if runtime_id:
                try:
                    await self.local.abort_audio_stream(runtime_id)
                except Exception:
                    pass
            if lease is not None:
                lease.release()
            raise
        except asyncio.CancelledError:
            if runtime_id:
                try:
                    await asyncio.shield(self.local.abort_audio_stream(runtime_id))
                except Exception:
                    pass
            if lease is not None:
                lease.release()
            raise
        except Exception as exc:
            if runtime_id:
                try:
                    await self.local.abort_audio_stream(runtime_id)
                except Exception:
                    pass
            if lease is not None:
                lease.release()
            raise self._stream_error(exc) from exc
        finally:
            async with self._audio_stream_lock:
                self._pending_audio_stream_starts = max(0, self._pending_audio_stream_starts - 1)
                if self._pending_audio_stream_starts == 0:
                    self._audio_stream_starts_idle.set()

    def _ensure_audio_stream_cleanup_task(self) -> None:
        if self._audio_stream_cleanup_task is None or self._audio_stream_cleanup_task.done():
            self._audio_stream_cleanup_task = asyncio.create_task(self._audio_stream_cleanup_loop())

    async def _audio_stream_cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                await self.cleanup_expired_audio_streams()
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _audio_stream_expired(session: _AudioStreamSession, now: float) -> bool:
        return (
            now - session.created_at >= ASR_STREAM_MAX_DURATION_SECONDS
            or now - session.last_activity >= ASR_STREAM_IDLE_TIMEOUT_SECONDS
        )

    async def _remove_audio_stream(
        self,
        session: _AudioStreamSession,
        *,
        abort_runtime: bool,
    ) -> None:
        async with self._audio_stream_lock:
            if self._audio_streams.get(session.stream_id) is session:
                self._audio_streams.pop(session.stream_id, None)
        try:
            if abort_runtime:
                try:
                    await self.local.abort_audio_stream(session.runtime_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("Unable to abort expired local ASR stream")
        finally:
            session.lease.release()

    async def cleanup_expired_audio_streams(self, *, now: float | None = None) -> None:
        """Abort idle/overlong streams and discard expired ASR receipts."""

        current = time.monotonic() if now is None else now
        async with self._audio_stream_lock:
            sessions = list(self._audio_streams.values())
            for result_id, receipt in list(self._asr_receipts.items()):
                if receipt.expires_at <= current:
                    self._asr_receipts.pop(result_id, None)
        for session in sessions:
            if not self._audio_stream_expired(session, current) or session.lock.locked():
                continue
            # Lock.acquire completes without yielding while the lock is free.
            # A slow request on another stream must not delay this sweep.
            await session.lock.acquire()
            try:
                if (
                    self._audio_streams.get(session.stream_id) is session
                    and self._audio_stream_expired(session, current)
                ):
                    await self._remove_audio_stream(session, abort_runtime=True)
            finally:
                session.lock.release()

    async def _audio_stream_session(self, stream_id: str) -> _AudioStreamSession:
        async with self._audio_stream_lock:
            session = self._audio_streams.get(stream_id)
        if session is None:
            raise AudioStreamError(404, "stream_not_found")
        return session

    async def append_audio_stream_chunk(
        self,
        *,
        stream_id: str,
        seq: int,
        pcm_base64: str,
    ) -> Mapping[str, Any]:
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise AudioStreamError(400, "invalid_sequence")
        try:
            pcm = decode_bounded_base64(pcm_base64, max_bytes=ASR_STREAM_CHUNK_MAX_BYTES)
        except ValueError as exc:
            raise AudioStreamError(400, "invalid_pcm_payload") from exc
        if len(pcm) % 2:
            raise AudioStreamError(400, "invalid_pcm_payload")

        session = await self._audio_stream_session(stream_id)
        digest = hashlib.sha256(pcm).hexdigest()
        async with session.lock:
            if self._audio_streams.get(stream_id) is not session:
                raise AudioStreamError(404, "stream_not_found")
            now = time.monotonic()
            if self._audio_stream_expired(session, now):
                await self._remove_audio_stream(session, abort_runtime=True)
                raise AudioStreamError(404, "stream_expired")
            if seq < session.next_seq:
                if session.chunk_digests.get(seq) != digest:
                    raise AudioStreamError(409, "sequence_conflict")
                session.last_activity = now
                return {"seq": seq, "partial_text": session.partial_text}
            if seq > session.next_seq:
                raise AudioStreamError(409, "sequence_conflict")
            if (
                session.chunk_count >= ASR_STREAM_MAX_CHUNKS
                or session.bytes_received + len(pcm) > ASR_STREAM_MAX_PCM_BYTES
            ):
                await self._remove_audio_stream(session, abort_runtime=True)
                raise AudioStreamError(400, "stream_duration_exceeded")

            session.last_activity = now
            try:
                partial = await self.local.append_audio_stream_chunk(
                    session.runtime_id,
                    seq,
                    pcm,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._remove_audio_stream(session, abort_runtime=True)
                raise self._stream_error(exc) from exc
            session.chunk_digests[seq] = digest
            session.chunk_count += 1
            session.bytes_received += len(pcm)
            session.next_seq += 1
            session.partial_text = str(partial or "").strip()[:MAX_TEXT_CHARS]
            session.last_activity = time.monotonic()
            return {"seq": seq, "partial_text": session.partial_text}

    async def finish_audio_stream(self, stream_id: str) -> Mapping[str, Any]:
        session = await self._audio_stream_session(stream_id)
        async with session.lock:
            if self._audio_streams.get(stream_id) is not session:
                raise AudioStreamError(404, "stream_not_found")
            if self._audio_stream_expired(session, time.monotonic()):
                await self._remove_audio_stream(session, abort_runtime=True)
                raise AudioStreamError(404, "stream_expired")
            session.last_activity = time.monotonic()
            try:
                payload = await self.local.finish_audio_stream(session.runtime_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._remove_audio_stream(session, abort_runtime=True)
                raise self._stream_error(exc) from exc

            if not isinstance(payload, Mapping):
                await self._remove_audio_stream(session, abort_runtime=True)
                raise AudioStreamError(503, "invalid_runtime_response")
            status = payload.get("status")
            status_failed = status is not None and str(status).casefold() not in {
                "ok",
                "success",
                "finished",
                "complete",
            }
            if (
                payload.get("ok") is False
                or payload.get("success") is False
                or payload.get("degraded") is True
                or payload.get("error")
                or status_failed
            ):
                await self._remove_audio_stream(session, abort_runtime=True)
                raise AudioStreamError(503, "runtime_finish_failed")
            text = payload.get("text")
            if not isinstance(text, str):
                await self._remove_audio_stream(session, abort_runtime=True)
                raise AudioStreamError(503, "invalid_runtime_response")
            text = text.strip()[:MAX_TEXT_CHARS]
            await self._remove_audio_stream(session, abort_runtime=False)
            if not text:
                return {"text": "", "result_id": None}

            result_id = secrets.token_urlsafe(32)
            async with self._audio_stream_lock:
                self._asr_receipts[result_id] = _ASRReceipt(
                    text=text,
                    context=session.platform,
                    expires_at=time.monotonic() + ASR_RESULT_TTL_SECONDS,
                    scope=session.scope,
                )
            return {"text": text, "result_id": result_id}

    async def abort_audio_stream(self, stream_id: str) -> Mapping[str, Any]:
        session = await self._audio_stream_session(stream_id)
        async with session.lock:
            if self._audio_streams.get(stream_id) is not session:
                raise AudioStreamError(404, "stream_not_found")
            await self._remove_audio_stream(session, abort_runtime=True)
        return {"aborted": True}

    async def consume_precomputed_asr_result(
        self,
        result_id: str | None,
        *,
        context: str,
        scope: str = "",
    ) -> str | None:
        if not isinstance(result_id, str) or not result_id.strip() or len(result_id) > 128:
            return None
        normalized_context = normalize_asr_stream_platform(context)
        if normalized_context not in ASR_STREAM_PLATFORMS:
            return None
        normalized_scope = normalize_asr_stream_scope(scope)
        if normalized_scope is None:
            return None
        async with self._audio_stream_lock:
            receipt = self._asr_receipts.get(result_id)
            if receipt is None:
                return None
            if receipt.expires_at <= time.monotonic():
                self._asr_receipts.pop(result_id, None)
                return None
            if receipt.context != normalized_context:
                return None
            if receipt.scope != normalized_scope:
                return None
            self._asr_receipts.pop(result_id, None)
            return receipt.text

    async def shutdown(self) -> None:
        """Abort owned runtime streams, release leases, and close owned HTTP."""

        async with self._audio_stream_lock:
            self._audio_streams_closing = True
        await self._audio_stream_starts_idle.wait()
        cleanup_task = self._audio_stream_cleanup_task
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            self._audio_stream_cleanup_task = None
        async with self._audio_stream_lock:
            sessions = list(self._audio_streams.values())
            self._asr_receipts.clear()
        for session in sessions:
            async with session.lock:
                if self._audio_streams.get(session.stream_id) is session:
                    await self._remove_audio_stream(session, abort_runtime=True)
        close = getattr(self.local, "aclose", None)
        if close is not None:
            await close()

    @staticmethod
    def _metadata_number(metadata: Mapping[str, Any], key: str) -> float | None:
        try:
            return float(metadata.get(key))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _metadata_int(metadata: Mapping[str, Any], key: str) -> int | None:
        try:
            value = int(metadata.get(key))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _extra_params(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        value = metadata.get("extra_params") if isinstance(metadata, Mapping) else None
        return dict(value) if isinstance(value, Mapping) else None

    async def transcribe(
        self,
        audio_base64: str,
        *,
        precomputed_asr_result_id: str | None = None,
        precomputed_asr_context: str = "universal_vc",
        precomputed_asr_scope: str = "",
        **kwargs: Any,
    ) -> PerceptionResult:
        receipt_text = await self.consume_precomputed_asr_result(
            precomputed_asr_result_id,
            context=precomputed_asr_context,
            scope=precomputed_asr_scope,
        )
        if receipt_text is not None:
            return PerceptionResult(
                operation=AUDIO_TRANSCRIBE_V1,
                text=receipt_text,
                provider="local-stream",
                metadata={"precomputed": True},
            )
        return await self.perceive(MediaInput(operation=AUDIO_TRANSCRIBE_V1, data=audio_base64, **kwargs))

    async def describe_image(self, image_base64: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=IMAGE_DESCRIBE_V1, data=image_base64, task="vlm", **kwargs))

    async def describe_emoji(self, image_base64: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=IMAGE_DESCRIBE_V1, data=image_base64, task="vlm", **kwargs))

    async def describe_image_fast(self, image_base64: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=IMAGE_DESCRIBE_V1, data=image_base64, task="vlm_fast", **kwargs))

    async def understand_video(self, video_base64: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=VIDEO_UNDERSTAND_V1, data=video_base64, **kwargs))

    async def synthesize_tts(
        self,
        text: str,
        *,
        platform: str = "core",
        text_lang: Optional[str] = None,
    ) -> TTSResult:
        text = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            return TTSResult(text="", error="tts text is empty")
        if not self.profile.allows_tts:
            # Potato is intentionally pure text, even if a local runtime is up.
            return TTSResult(text=text, provider="text-only")
        try:
            return await self.local.synthesize_tts(text, platform=platform, text_lang=text_lang)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TTS synthesis failed: %s", type(exc).__name__)
            return TTSResult(text=text, provider="text-only", error="tts unavailable")

    async def synthesize_tts_stream(
        self,
        text: str,
        *,
        platform: str = "core",
        text_lang: Optional[str] = None,
    ) -> AsyncIterator[TTSStreamChunk]:
        """Yield typed local PCM chunks while keeping Core profile policy."""

        text = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            raise TTSStreamError("invalid_request")
        if not self.profile.allows_tts:
            # Potato is intentionally text-only, even if a local runtime is up.
            raise TTSStreamError("text_only")

        iterator = None
        audio_started = False
        try:
            iterator = self.local.synthesize_tts_stream(
                text,
                platform=platform,
                text_lang=text_lang,
            )
            async for chunk in iterator:
                if not isinstance(chunk, TTSStreamChunk):
                    raise TTSStreamError(
                        "invalid_stream_chunk",
                        audio_started=audio_started,
                    )
                audio_started = True
                yield chunk
        except asyncio.CancelledError:
            raise
        except TTSStreamError as exc:
            if audio_started and not exc.audio_started:
                raise TTSStreamError(
                    exc.code,
                    audio_started=True,
                    status_code=exc.status_code,
                ) from exc
            raise
        except Exception as exc:
            raise TTSStreamError(
                "midstream_failure" if audio_started else "unavailable",
                audio_started=audio_started,
            ) from exc
        finally:
            closer = getattr(iterator, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("TTS stream cleanup failed: %s", type(exc).__name__)

    @staticmethod
    def _iter_segments(segment: Seg) -> Iterable[Seg]:
        if segment.type == "seglist" and isinstance(segment.data, list):
            for child in segment.data:
                if isinstance(child, Seg):
                    yield from CoreMultimodalRouter._iter_segments(child)
            return
        yield segment

    def has_explicit_tts_text(self, segment: Seg) -> bool:
        return any(seg.type == "tts_text" and bool(_nonempty_tts_text(seg.data)) for seg in self._iter_segments(segment))

    def has_tts_text_field(self, segment: Seg) -> bool:
        """Return whether a reply contains any ``tts_text`` field.

        Potato uses this broader predicate to convert even an empty legacy
        field to plain text, while normal profiles synthesize only nonempty
        fields through :meth:`has_explicit_tts_text`.
        """

        return any(seg.type == "tts_text" for seg in self._iter_segments(segment))

    def should_materialize_reply(self, segment: Seg) -> bool:
        return self.has_explicit_tts_text(segment) or (
            self.profile is RuntimeProfile.POTATO
            and self.has_tts_text_field(segment)
        )

    async def materialize_reply(
        self,
        segment: Seg,
        *,
        platform: str = "core",
        text_lang: Optional[str] = None,
    ) -> Seg:
        """Add compatible voice segments only beside nonempty ``tts_text``.

        Ordinary text is never synthesized.  Failed synthesis turns the
        explicit TTS field into an ordinary text reply so adapters do not drop
        the only user-visible response.  Potato converts only explicit
        ``tts_text`` fields to ordinary text; prebuilt media remains untouched
        and in its original order.
        """

        async def walk(current: Seg) -> list[Seg]:
            if current.type == "seglist" and isinstance(current.data, list):
                output: list[Seg] = []
                for child in current.data:
                    if isinstance(child, Seg):
                        output.extend(await walk(child))
                return [Seg(type="seglist", data=output)]
            if current.type != "tts_text":
                return [current]
            text = _nonempty_tts_text(current.data)
            if self.profile is RuntimeProfile.POTATO:
                return [Seg(type="text", data=_tts_fallback_text(current.data))]
            if not text:
                return [current]
            segment_lang = None
            if isinstance(current.data, Mapping):
                raw_lang = current.data.get("lang")
                segment_lang = str(raw_lang).strip()[:32] if raw_lang else None
            if segment_lang is None:
                segment_lang = text_lang
            result = await self.synthesize_tts(text, platform=platform, text_lang=segment_lang)
            original = current
            if not result.audio_base64:
                return [Seg(type="text", data=_tts_fallback_text(current.data))]
            return [
                original,
                Seg(type="voice", data=result.audio_base64),
            ]

        materialized = await walk(segment)
        if not materialized:
            return Seg(type="text", data="")
        if len(materialized) == 1:
            return materialized[0]
        return Seg(type="seglist", data=materialized)


_router: CoreMultimodalRouter | None = None


def get_multimodal_router() -> CoreMultimodalRouter:
    global _router
    if _router is None:
        _router = CoreMultimodalRouter()
    return _router


def reset_multimodal_router() -> None:
    """Test/deployment hook; does not alter process or live configuration."""

    global _router
    _router = None
