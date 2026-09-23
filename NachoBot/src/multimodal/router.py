"""Core multimodal routing and reply materialization."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional

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
from .remote import RemotePerceptionProvider

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
        self.remote = remote if remote is not None else RemotePerceptionProvider()
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
        # Canonicalize and bound the payload before either local or remote
        # provider sees it.  This keeps the fallback path from bypassing the
        # transport limits enforced by LocalMultimodalClient.
        encoded, _ = normalize_operation_payload(request.operation, request.data)
        request = replace(request, data=encoded)
        attempts: list[str] = []
        providers = ([("local", self.local), ("remote", self.remote)] if self.profile.allows_local_perception else [("remote", self.remote)])
        for provider_name, provider in providers:
            attempts.append(provider_name)
            try:
                result = await provider.perceive(request)
                if result.text.strip():
                    return replace(result, attempted=tuple(attempts), degraded=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep errors bounded and never include media payloads/tokens.
                logger.warning("%s perception provider %s failed: %s", request.operation, provider_name, type(exc).__name__)
        return PerceptionResult(
            operation=request.operation,
            text=_failure_text(request.operation),
            provider="degraded",
            degraded=True,
            attempted=tuple(attempts),
            error="perception providers unavailable",
        )

    async def transcribe(self, audio_base64: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=AUDIO_TRANSCRIBE_V1, data=audio_base64, **kwargs))

    async def describe_image(self, image_base64: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=IMAGE_DESCRIBE_V1, data=image_base64, **kwargs))

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
