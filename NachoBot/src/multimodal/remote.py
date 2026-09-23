"""Remote perception provider used by Core's lite/full fallback path."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    IMAGE_DESCRIBE_V1,
    VIDEO_UNDERSTAND_V1,
    MediaInput,
    PerceptionResult,
    MAX_TEXT_CHARS,
)


def _is_local_provider(provider: Any) -> bool:
    name = str(getattr(provider, "name", "") or "").strip().lower()
    base_url = str(getattr(provider, "base_url", "") or "").strip().lower()
    try:
        port = urlsplit(base_url).port
    except ValueError:
        port = None
    return (
        name.startswith("local")
        or port == 9874
    )


def remote_task_config(task_config: Any, model_config: Any) -> Any:
    """Copy a task config while excluding all local providers/models.

    Mixed task lists are common in deployments.  Filtering by provider URL as
    well as provider name prevents the remote fallback from accidentally
    selecting the LocalModel/9874 entry.
    """

    if task_config is None:
        raise ValueError("remote task configuration is missing")
    models = getattr(model_config, "models", []) or []
    providers = {
        str(getattr(provider, "name", "")): provider
        for provider in (getattr(model_config, "api_providers", []) or [])
    }
    allowed: list[str] = []
    for model_name in list(getattr(task_config, "model_list", []) or []):
        model = None
        try:
            model = model_config.get_model_info(model_name)
        except Exception:
            model = next((candidate for candidate in models if getattr(candidate, "name", None) == model_name), None)
        provider_name = getattr(model, "api_provider", None)
        provider = providers.get(str(provider_name))
        if provider is None and provider_name:
            try:
                provider = model_config.get_provider(provider_name)
            except Exception:
                provider = None
        if provider is not None and _is_local_provider(provider):
            continue
        # Unknown model entries are kept for normal LLMRequest resolution to
        # report a configuration error; only provably local entries are removed.
        allowed.append(str(model_name))
    if not allowed:
        raise ValueError("no remote models remain after excluding local providers")
    try:
        return replace(task_config, model_list=allowed)
    except TypeError:
        clone = copy.copy(task_config)
        clone.model_list = allowed
        return clone


class RemotePerceptionProvider:
    """Lazy wrapper around the existing remote LLM request abstraction."""

    def __init__(self, *, request_factory: Optional[Callable[..., Any]] = None, model_config: Any = None):
        self.request_factory = request_factory
        self._model_config = model_config

    def _get_model_config(self) -> Any:
        if self._model_config is None:
            from src.config.config import model_config

            self._model_config = model_config
        return self._model_config

    def _request(self, operation: str) -> Any:
        config = self._get_model_config()
        task_name = {
            AUDIO_TRANSCRIBE_V1: "voice",
            IMAGE_DESCRIBE_V1: "vlm",
            VIDEO_UNDERSTAND_V1: "video",
        }.get(operation)
        if task_name is None:
            raise ValueError(f"unsupported remote operation: {operation}")
        task = getattr(config.model_task_config, task_name, None)
        # Older Core configs expose video understanding through the VLM task
        # only.  Preserve that established remote semantics while still using
        # the typed video operation at the facade boundary.
        if task is None and task_name == "video":
            task = getattr(config.model_task_config, "vlm", None)
        filtered = remote_task_config(task, config)
        factory = self.request_factory
        if factory is None:
            from src.llm_models.utils_model import LLMRequest

            factory = LLMRequest
        try:
            return factory(model_set=filtered, request_type=task_name)
        except TypeError:
            return factory(filtered, request_type=task_name)

    async def perceive(self, request: MediaInput) -> PerceptionResult:
        llm = self._request(request.operation)
        if request.operation == AUDIO_TRANSCRIBE_V1:
            text = await llm.generate_response_for_voice(request.data)
        elif request.operation == IMAGE_DESCRIBE_V1:
            text, _ = await llm.generate_response_for_image(
                request.prompt,
                request.data,
                request.media_format or "png",
                temperature=_metadata_number(request.metadata, "temperature"),
                max_tokens=_metadata_int(request.metadata, "max_tokens"),
                extra_params=dict(request.metadata.get("extra_params", {}))
                if isinstance(request.metadata, Mapping)
                else None,
            )
        else:
            text, _ = await llm.generate_response_for_video(
                request.prompt,
                request.data,
                request.media_format or "mp4",
                temperature=_metadata_number(request.metadata, "temperature"),
                max_tokens=_metadata_int(request.metadata, "max_tokens"),
                extra_params=dict(request.metadata.get("extra_params", {}))
                if isinstance(request.metadata, Mapping)
                else None,
            )
        text = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            raise RuntimeError("remote perception returned empty text")
        return PerceptionResult(operation=request.operation, text=text, provider="remote")


def _metadata_number(metadata: Mapping[str, Any], key: str) -> float | None:
    try:
        value = float(metadata.get(key))
    except (TypeError, ValueError):
        return None
    return value


def _metadata_int(metadata: Mapping[str, Any], key: str) -> int | None:
    try:
        value = int(metadata.get(key))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
