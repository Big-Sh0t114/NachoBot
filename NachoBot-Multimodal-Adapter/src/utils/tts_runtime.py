"""Shared live TTS model lifecycle for all long-running callers."""

from contextlib import asynccontextmanager
import asyncio
from dataclasses import dataclass
import inspect
from pathlib import Path
import threading
from typing import Any, AsyncIterator, Optional

try:
    from .tts_resolver import TTSResolution, resolve_tts_model_snapshot
    _RESOLVER_AVAILABLE = True
except ImportError:  # pragma: no cover - reduced injected deployments
    @dataclass(frozen=True)
    class TTSResolution:  # type: ignore[no-redef]
        model_class: Optional[Any]
        error: Optional[str]
        fingerprint: Optional[str]
        plugin: Optional[str]
        base_config_path: Optional[Path]
        backend_config_path: Optional[Path]

    resolve_tts_model_snapshot = None  # type: ignore[assignment]
    _RESOLVER_AVAILABLE = False


class TTSRuntimeError(RuntimeError):
    """Raised when the current TTS configuration cannot provide a model."""

    def __init__(self, message: str, *, resolution: Optional[TTSResolution] = None):
        super().__init__(message)
        self.resolution = resolution


async def _drain_task(task: asyncio.Task) -> Any:
    """Drain a worker task even after the caller was cancelled."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep the runtime lock held until the worker has finished all
            # constructor/mutation side effects.
            continue
    return task.result()


class TTSRuntime:
    """Content-aware, serialized TTS model runtime.

    Synchronous ``ensure_tts_model`` remains available for startup and manual
    callers. Async callers should hold ``model_context`` across their complete
    synthesis or stream consumption; the context refreshes in a worker thread
    and keeps the async lock held until the caller exits.
    """

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        *,
        model_class: Any = None,
        import_error: Optional[str] = None,
        fixed_backend: Optional[str] = None,
    ):
        self.config_dir = Path(config_dir) if config_dir else None
        self.startup_model_class = model_class
        self.startup_import_error = import_error
        self.fixed_backend = fixed_backend

        self._model: Any = None
        self._model_class: Any = None
        self._fingerprint: Optional[str] = None
        self._plugin: Optional[str] = None
        self._error: Optional[str] = import_error
        self._resolution: Optional[TTSResolution] = None

        self._refresh_lock = threading.RLock()
        self._async_lock = asyncio.Lock()

    @property
    def model(self) -> Any:
        # Attribute publication is atomic under CPython and this read must not
        # wait behind a synchronous constructor when used by health metadata.
        return self._model

    @property
    def tts_model(self) -> Any:
        """Compatibility alias used by existing health/caller code."""

        return self.model

    @property
    def fingerprint(self) -> Optional[str]:
        return self._fingerprint

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def plugin(self) -> Optional[str]:
        return self._plugin

    @property
    def ready(self) -> bool:
        return self.model is not None and self.error is None

    def status(self) -> dict[str, Any]:
        """Return metadata without waiting behind an active stream."""

        model = self._model
        return {
            "ready": model is not None and self._error is None,
            "backend": type(model).__module__ if model else None,
            "plugin": self._plugin,
            "fingerprint": self._fingerprint,
            "error": self._error,
        }

    def desired_status(self) -> dict[str, Any]:
        """Read current selection metadata without constructing a model.

        Health clients use this to advertise a valid backend before the first
        synthesis (and after a disabled or broken configuration is repaired).
        Resolution only reads/parses the current TOML and imports the selected
        class; it never enters the model refresh lock or performs weight/HTTP
        initialization.
        """

        snapshot = self._resolve()
        backend = None
        if snapshot.model_class is not None and snapshot.error is None:
            backend = snapshot.model_class.__module__
        return {
            "backend": backend,
            "plugin": snapshot.plugin,
            "fingerprint": snapshot.fingerprint,
            "error": snapshot.error,
        }

    def _resolve(self) -> TTSResolution:
        """Resolve the current files, with reduced injected-class fallback."""

        if _RESOLVER_AVAILABLE:
            try:
                return resolve_tts_model_snapshot(self.config_dir, fixed_backend=self.fixed_backend)
            except Exception as exc:
                return TTSResolution(None, f"Unexpected error resolving TTS model: {exc}", None, None, None, None)

        # The startup class is only a compatibility fallback when the resolver
        # import itself is unavailable in a reduced deployment.
        if self.startup_model_class is None:
            return TTSResolution(None, self.startup_import_error or "TTS resolver not available", None, None, None, None)
        class_name = f"{self.startup_model_class.__module__}.{self.startup_model_class.__qualname__}"
        return TTSResolution(self.startup_model_class, None, f"injected:{class_name}", None, None, None)

    @staticmethod
    def _class_matches(snapshot: TTSResolution, model_class: Any) -> bool:
        # A supplied class is used only for reduced/fixed test deployments;
        # normal snapshots carry the actual dynamically imported class.
        return snapshot.model_class is model_class

    def _construct(self, snapshot: TTSResolution) -> Any:
        model_class = self.startup_model_class if snapshot.plugin is None and self.startup_model_class else snapshot.model_class
        if model_class is None:
            raise TTSRuntimeError(snapshot.error or "TTS model class unavailable", resolution=snapshot)

        if snapshot.backend_config_path is not None and snapshot.plugin in {"GPT_Sovits", "Vox"}:
            # Known backends must parse the same selected file that was hashed.
            constructor_kwargs = {"config_path": snapshot.backend_config_path}
            if snapshot.plugin == "Vox" and snapshot.base_config_path is not None:
                constructor_kwargs["base_config_path"] = snapshot.base_config_path
            return model_class(**constructor_kwargs)
        return model_class()

    def _clear(self, error: Optional[str], snapshot: Optional[TTSResolution] = None) -> None:
        self._model = None
        self._model_class = None
        self._fingerprint = None
        self._plugin = snapshot.plugin if snapshot else None
        self._resolution = snapshot
        self._error = error

    def ensure_tts_model(self) -> bool:
        """Refresh synchronously, publishing only a stable candidate."""

        with self._refresh_lock:
            for attempt in range(2):
                snapshot = self._resolve()
                if snapshot.model_class is None or snapshot.error or not snapshot.fingerprint:
                    self._clear(snapshot.error or "TTS configuration has no fingerprint", snapshot)
                    return False

                if (
                    self._model is not None
                    and self._fingerprint == snapshot.fingerprint
                    and self._plugin == snapshot.plugin
                    and self._class_matches(snapshot, self._model_class)
                ):
                    return True

                self._clear(None, snapshot)
                try:
                    candidate = self._construct(snapshot)
                except Exception as exc:
                    self._clear(f"Failed to initialize TTS Model: {exc}", snapshot)
                    return False

                current = self._resolve()
                if (
                    current.model_class is None
                    or current.error
                    or not current.fingerprint
                    or current.plugin != snapshot.plugin
                    or current.fingerprint != snapshot.fingerprint
                    or current.model_class is not snapshot.model_class
                ):
                    self._clear(
                        current.error or "TTS configuration changed during initialization",
                        current,
                    )
                    if attempt == 0:
                        continue
                    return False

                self._model = candidate
                self._model_class = type(candidate)
                self._fingerprint = snapshot.fingerprint
                self._plugin = snapshot.plugin
                self._resolution = snapshot
                self._error = None
                return True
            return False

    def invalidate(self, error: Optional[str] = None) -> None:
        """Drop the installed client after a partial external mutation."""

        with self._refresh_lock:
            self._clear(error or "TTS runtime invalidated")

    @asynccontextmanager
    async def model_context(self) -> AsyncIterator[Any]:
        """Yield a fresh model while serializing complete synth/stream use."""

        await self._async_lock.acquire()
        ensure_task = asyncio.create_task(asyncio.to_thread(self.ensure_tts_model))
        try:
            try:
                # Shield the worker task so cancellation cannot detach a
                # running constructor from the runtime serialization lock.
                ready = await asyncio.shield(ensure_task)
            except asyncio.CancelledError:
                await _drain_task(ensure_task)
                raise
            if not ready:
                raise TTSRuntimeError(self.error or "TTS model unavailable")
            model = self.model
            if model is None:
                raise TTSRuntimeError("TTS model disappeared after refresh")
            yield model
        finally:
            self._async_lock.release()

    async def synthesize(self, text: str, **kwargs) -> Any:
        """Convenience wrapper for one complete non-streaming synthesis."""

        async with self.model_context() as model:
            return await model.tts(text=text, **kwargs)

    async def call_blocking(self, function, *args, **kwargs) -> Any:
        """Run a blocking mutation while the caller holds ``model_context``.

        Cancellation is drained so a setter cannot continue mutating a client
        after the runtime serialization lock has been released.
        """

        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _drain_task(task)
            raise

    async def close_stream(self, stream) -> None:
        """Close an async/sync stream before the model context is released."""

        close = getattr(stream, "aclose", None)
        if close is None:
            close = getattr(stream, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            task = asyncio.create_task(result)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await _drain_task(task)
                raise
