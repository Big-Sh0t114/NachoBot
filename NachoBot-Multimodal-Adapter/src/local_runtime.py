"""The eager local ASR/VLM runtime owned by the 9874 perception service.

9874 deliberately has no TTS model or TTS transport ownership.  The owning
process calls :meth:`preload` during its lifespan before it accepts requests;
the request handlers only dispatch to the already-loaded model functions.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import os
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


class UnsupportedOperation(RuntimeError):
    """The local perception runtime cannot serve a typed operation."""


class RuntimeUnavailable(UnsupportedOperation):
    """The local model process is disabled or not ready."""


class LocalBusy(RuntimeError):
    """The Florence worker cannot accept another bounded request."""


@dataclass(frozen=True)
class LocalCapabilities:
    """Public capability/readiness state for the perception-only listener."""

    operations: tuple[str, ...]
    ready: bool
    models: Mapping[str, str] = field(default_factory=dict)
    profile: str = "local"
    error: Optional[str] = None
    perception_enabled: bool = True
    perception_disabled: bool = False
    models_loaded: bool = False
    no_local_models: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": list(self.operations),
            "models": dict(self.models),
            "ready": self.ready,
            "profile": self.profile,
            "error": self.error,
            "perception_enabled": self.perception_enabled,
            "perception_disabled": self.perception_disabled,
            "models_loaded": self.models_loaded,
            "no_local_models": self.no_local_models,
        }


Loader = Callable[[], Any]


class LocalMultimodalRuntime:
    """Bounded dispatcher for eager local ASR and VLM models.

    ``asr_transcriber`` and ``image_captioner`` are test/deployment injection
    points.  Production instances leave them unset, so :meth:`preload`
    imports both model modules and calls their explicit ``load_model``
    functions before marking this runtime ready.
    """

    AUDIO = "audio.transcribe.v1"
    IMAGE = "image.describe.v1"
    VIDEO = "video.understand.v1"

    def __init__(
        self,
        *,
        config_dir: Path | str | None = None,
        no_local_models: bool | None = None,
        disable_perception: bool | None = None,
        perception_enabled: bool | None = None,
        asr_transcriber: Any = None,
        image_captioner: Any = None,
        asr_loader: Loader | None = None,
        vlm_loader: Loader | None = None,
    ):
        # Keep config_dir as a harmless compatibility value for callers that
        # construct the perception runtime alongside other adapter services.
        self.config_dir = (
            Path(config_dir)
            if config_dir
            else Path(__file__).resolve().parents[1] / "configs"
        )
        self.no_local_models = (
            bool(no_local_models)
            if no_local_models is not None
            else os.environ.get("NACHOBOT_NO_LOCAL_MODELS") == "1"
        )
        requested_perception = (
            bool(perception_enabled)
            if perception_enabled is not None
            else not (
                bool(disable_perception)
                if disable_perception is not None
                else os.environ.get("DISABLE_VLM_ASR") == "1"
            )
        )
        self.perception_enabled = not self.no_local_models and requested_perception
        self.disable_perception = not self.perception_enabled

        self._asr_transcriber = asr_transcriber
        self._image_captioner = image_captioner
        self._asr_loader = asr_loader
        self._vlm_loader = vlm_loader
        self._preload_lock = asyncio.Lock()
        # The semaphore follows the worker, not the HTTP request. A cancelled
        # request cannot release Florence while its to_thread call still runs.
        self._vlm_slot = asyncio.Semaphore(1)
        self._vlm_admission = asyncio.Lock()
        self._vlm_pending = 0
        self._vlm_max_pending = 2  # one running and at most one waiting
        self._vlm_queue_timeout = 0.5
        self._preload_started = False
        self._ready = False
        self._preload_error: str | None = (
            None if self.perception_enabled else "local perception is disabled"
        )

    @property
    def preload_error(self) -> str | None:
        """Return a bounded diagnostic for a failed or disabled preload."""

        return self._preload_error

    @property
    def models_loaded(self) -> bool:
        return self._ready

    async def _run_loader(self, loader: Loader) -> Any:
        value = await asyncio.to_thread(loader)
        if inspect.isawaitable(value):
            return await value
        return value

    async def _preload_asr(self) -> None:
        loader_ran = False
        if self._asr_loader is not None:
            loaded = await self._run_loader(self._asr_loader)
            loader_ran = True
            if self._asr_transcriber is None and callable(loaded):
                self._asr_transcriber = loaded
        if self._asr_transcriber is None:
            module = import_module(".asr.streaming", package=__package__)
            if not loader_ran:
                await self._run_loader(getattr(module, "load_model"))
            self._asr_transcriber = getattr(module, "transcribe")

        if not callable(self._asr_transcriber):
            raise RuntimeError("local ASR transcriber is unavailable after preload")

    async def _preload_vlm(self) -> None:
        loader_ran = False
        if self._vlm_loader is not None:
            loaded = await self._run_loader(self._vlm_loader)
            loader_ran = True
            if self._image_captioner is None and callable(loaded):
                self._image_captioner = loaded
        if self._image_captioner is None:
            module = import_module(".vlm.florence2", package=__package__)
            if not loader_ran:
                await self._run_loader(getattr(module, "load_model"))
            self._image_captioner = getattr(module, "caption_image_b64")

        if not callable(self._image_captioner):
            raise RuntimeError("local VLM captioner is unavailable after preload")

    async def preload(self) -> None:
        """Import and load ASR plus VLM before exposing readiness.

        A failure is latched.  The lifespan should propagate it so uvicorn
        never binds a falsely-ready listener; if a caller keeps a degraded
        listener alive, :meth:`perceive` still refuses to retry model loading.
        """

        async with self._preload_lock:
            if self._ready:
                return
            if self._preload_started:
                if self._preload_error:
                    raise RuntimeError(self._preload_error)
                raise RuntimeError("local perception preload did not complete")

            self._preload_started = True
            if not self.perception_enabled:
                self._preload_error = self._preload_error or "local perception is disabled"
                return

            try:
                # Sequential loading keeps memory pressure predictable while
                # still making both model loads part of startup readiness.
                await self._preload_asr()
                await self._preload_vlm()
            except Exception as exc:
                self._ready = False
                self._preload_error = f"local ASR/VLM preload failed: {type(exc).__name__}"
                raise RuntimeError("local ASR/VLM preload failed") from exc

            self._ready = True
            self._preload_error = None

    def capabilities(self) -> LocalCapabilities:
        operations = (self.AUDIO, self.IMAGE) if self.perception_enabled else ()
        return LocalCapabilities(
            operations=operations,
            ready=self._ready,
            models={
                self.AUDIO: "zh-xlarge-int8-2025-06-30",
                self.IMAGE: "Florence-2",
            } if self.perception_enabled else {},
            error=self._preload_error,
            perception_enabled=self.perception_enabled,
            perception_disabled=not self.perception_enabled,
            models_loaded=self._ready,
            no_local_models=self.no_local_models,
        )

    async def health(self) -> dict[str, Any]:
        return self.capabilities().to_dict()

    async def perceive(
        self,
        operation: str,
        data: str,
        *,
        media_format: str = "",
        prompt: str = "",
    ) -> str:
        del media_format, prompt
        if not self.perception_enabled:
            if self.no_local_models:
                raise RuntimeUnavailable("local multimodal models are disabled")
            raise RuntimeUnavailable("local perception is disabled")
        if not self._ready:
            # Do not call preload here: request-time lazy loading is expressly
            # forbidden, and a failed startup must remain latched.
            detail = self._preload_error or "local perception is not ready"
            raise RuntimeUnavailable(detail)
        if not isinstance(data, str) or not data.strip():
            raise ValueError("media payload is empty")

        if operation == self.AUDIO:
            raw = _decode_bounded(data, 16 * 1024 * 1024)
            value = await asyncio.to_thread(self._asr_transcriber, raw)
            if inspect.isawaitable(value):
                value = await value
            return str(value or "").strip()
        if operation == self.IMAGE:
            # Validate the encoded payload before passing it to Florence's
            # service-owned caption policy.
            _decode_bounded(data, 16 * 1024 * 1024)
            value = await self._run_florence(data)
            return str(value or "").strip()
        if operation == self.VIDEO:
            raise UnsupportedOperation("local video understanding is unsupported")
        raise UnsupportedOperation(f"unsupported local operation: {operation}")

    async def _run_florence(self, data: str) -> Any:
        async with self._vlm_admission:
            if self._vlm_pending >= self._vlm_max_pending:
                raise LocalBusy("Florence worker is busy")
            self._vlm_pending += 1
        try:
            await asyncio.wait_for(self._vlm_slot.acquire(), timeout=self._vlm_queue_timeout)
        except asyncio.TimeoutError as exc:
            self._vlm_pending -= 1
            raise LocalBusy("Florence queue wait exceeded") from exc
        except BaseException:
            self._vlm_pending -= 1
            raise

        async def infer() -> Any:
            value = await asyncio.to_thread(self._image_captioner, data)
            if inspect.isawaitable(value):
                value = await value
            return value

        try:
            worker = asyncio.create_task(infer())
        except BaseException:
            self._vlm_slot.release()
            self._vlm_pending -= 1
            raise

        def release_capacity(completed: asyncio.Task[Any]) -> None:
            self._vlm_slot.release()
            self._vlm_pending -= 1
            if not completed.cancelled():
                completed.exception()  # consume errors after client cancellation

        try:
            return await asyncio.shield(worker)
        finally:
            if worker.done():
                release_capacity(worker)
            else:
                worker.add_done_callback(release_capacity)


def _decode_bounded(data: str, limit: int) -> bytes:
    raw = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    max_encoded_chars = 4 * ((limit + 2) // 3)
    if len(raw) > max_encoded_chars:
        raise ValueError(f"media payload exceeds {limit} bytes")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError("media payload is not valid base64") from exc
    if not decoded:
        raise ValueError("media payload is empty")
    if len(decoded) > limit:
        raise ValueError(f"media payload exceeds {limit} bytes")
    return decoded
