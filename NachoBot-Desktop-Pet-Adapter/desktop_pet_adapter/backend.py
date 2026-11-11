"""Renderer backend contracts and lazy backend selection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from typing import Any, Protocol

from .config import AdapterConfig


class BackendSelectionError(ValueError):
    """Raised when the requested renderer backend cannot be selected."""


class BackendRenderer(Protocol):
    running: bool
    pcm_audio_ready: bool

    def run(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackendContext:
    config: AdapterConfig
    logger: Any
    command_queue: Queue
    voice_stream_queue: Queue
    on_click: Callable[[int], None]
    on_ready: Callable[[], None]
    on_exit: Callable[[], None]


class RendererBackend(Protocol):
    name: str
    model_adapter: Any

    def prepare(self) -> Any: ...

    def resolve_action(self, action_id: str) -> str | None: ...

    def create_renderer(self, context: BackendContext) -> BackendRenderer: ...

    def capabilities(self, renderer: BackendRenderer) -> dict[str, Any]: ...


BackendFactory = Callable[[AdapterConfig, Any], RendererBackend]
_BACKEND_FACTORIES: dict[str, BackendFactory] = {}


def register_backend(
    name: str,
    factory: BackendFactory,
    *,
    replace: bool = False,
) -> None:
    """Register an optional backend factory, primarily for plugins and tests."""

    normalized = str(name).strip().casefold()
    if not normalized:
        raise ValueError("backend name cannot be empty")
    if normalized in _BACKEND_FACTORIES and not replace:
        raise ValueError(f"renderer backend already registered: {normalized}")
    _BACKEND_FACTORIES[normalized] = factory


def unregister_backend(name: str) -> None:
    _BACKEND_FACTORIES.pop(str(name).strip().casefold(), None)


def registered_backends() -> tuple[str, ...]:
    names = set(_BACKEND_FACTORIES)
    names.update(("live2d", "sprite"))
    return tuple(sorted(names))


def create_backend(name: str, config: AdapterConfig, logger: Any) -> RendererBackend:
    """Create one backend, importing implementation modules only on demand."""

    normalized = str(name).strip().casefold()
    factory = _BACKEND_FACTORIES.get(normalized)
    if factory is None and normalized == "live2d":
        # Keep the base package neutral. The provider is imported only when a
        # Live2D backend is explicitly selected.
        from .live2d_backend import create_backend as create_live2d_backend

        factory = create_live2d_backend
    if factory is None and normalized == "sprite":
        from .sprite_backend import create_backend as create_sprite_backend

        factory = create_sprite_backend
    if factory is None:
        known = ", ".join(registered_backends()) or "(none)"
        raise BackendSelectionError(
            f"Unknown renderer backend '{normalized or name}'. Registered backends: {known}."
        )
    return factory(config, logger)
