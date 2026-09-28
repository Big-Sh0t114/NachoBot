"""Core multimodal routing and reply materialization."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from ncnk_message import Seg

from .client import LocalMultimodalClient
from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    IMAGE_DESCRIBE_V1,
    VIDEO_UNDERSTAND_V1,
    MAX_TEXT_CHARS,
    MediaInput,
    PerceptionResult,
    TTSResult,
    normalize_operation_payload,
)
from .profile import RuntimeProfile, get_runtime_profile, normalize_runtime_profile

logger = logging.getLogger("core.multimodal")


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

    async def transcribe(self, audio_base64: str, **kwargs: Any) -> PerceptionResult:
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
