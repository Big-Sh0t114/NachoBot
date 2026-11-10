"""Shared, lazy lifecycle coordination for a single SDK plugin instance."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import math
from typing import Any, Coroutine


_LIVE_TASKS: set[asyncio.Task[Any]] = set()


def _observe_and_release(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except BaseException:
        pass
    _LIVE_TASKS.discard(task)


class _HookMarker:
    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


class SDKPluginLifecycle:
    """Coordinate one plugin instance's lazy load and bounded close."""

    def __init__(self, plugin: Any, *, close_timeout: float = 10.0) -> None:
        if isinstance(close_timeout, bool) or not isinstance(close_timeout, (int, float)):
            raise TypeError("close_timeout must be a finite positive number")
        if not math.isfinite(close_timeout) or close_timeout <= 0:
            raise ValueError("close_timeout must be a finite positive number")

        self.plugin = plugin
        self.close_timeout = float(close_timeout)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._load_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[bool] | None = None
        self._load_cancel_requested = False
        self._closed = False
        self._loaded = False
        self._unload_attempted = False
        self._hook_marker: contextvars.ContextVar[_HookMarker | None] = contextvars.ContextVar(
            f"sdk_plugin_lifecycle_hook_{id(self)}", default=None
        )

    def _reject_reentrant_call(self) -> None:
        marker = self._hook_marker.get()
        if marker is not None and marker.active:
            raise RuntimeError("SDK plugin lifecycle cannot be re-entered from its own hook")

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("SDK plugin lifecycle cannot be reused from another event loop")
        return loop

    def _create_retained_task(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        assert self._loop is not None
        try:
            task = self._loop.create_task(coroutine)
        except BaseException:
            try:
                coroutine.close()
            except BaseException:
                pass
            raise
        _LIVE_TASKS.add(task)
        task.add_done_callback(_observe_and_release)
        return task

    def _set_load_mirror(self, value: bool) -> None:
        try:
            self.plugin._on_load_called = value
        except Exception:
            # The mirror is compatibility-only; lifecycle state remains authoritative.
            pass

    async def ensure_loaded(self) -> None:
        """Join or start initialization, then grant permission to dispatch work."""
        self._reject_reentrant_call()
        self._get_loop()

        # Give a pre-cancelled caller a checkpoint before it can publish initialization.
        await asyncio.sleep(0)
        self._reject_reentrant_call()
        if self._closed:
            raise RuntimeError("SDK plugin lifecycle is closed")
        if self._loaded:
            return

        task = self._load_task
        if task is None or task.done():
            if self._closed:
                raise RuntimeError("SDK plugin lifecycle is closed")
            task = self._create_retained_task(self._run_load())
            self._load_task = task

        await asyncio.shield(task)
        # A factory or hook can request cancellation while an eager task finishes inline.
        await asyncio.sleep(0)
        self._reject_reentrant_call()
        if self._closed:
            raise RuntimeError("SDK plugin lifecycle is closed")
        if not self._loaded:
            raise RuntimeError("SDK plugin initialization completed without loading")

    async def _run_load(self) -> None:
        marker = _HookMarker()
        token = self._hook_marker.set(marker)
        self._set_load_mirror(False)
        try:
            callback = getattr(self.plugin, "on_load", None)
            if not callable(callback):
                raise TypeError("SDK plugin on_load hook is not callable")
            result = callback()
            if inspect.isawaitable(result):
                await result
            self._loaded = True
            self._set_load_mirror(True)
        except BaseException:
            self._loaded = False
            self._set_load_mirror(False)
            raise
        finally:
            marker.active = False
            self._hook_marker.reset(token)

    async def close(self) -> bool:
        """Seal dispatch, join one cleanup task, and bound only this wait."""
        self._reject_reentrant_call()
        self._get_loop()
        self._closed = True

        task = self._close_task
        if task is None:
            task = self._create_retained_task(self._run_close())
            self._close_task = task

        done, _pending = await asyncio.wait((task,), timeout=self.close_timeout)
        if not done:
            return False
        return bool(task.result())

    async def _run_close(self) -> bool:
        load_task = self._load_task
        if load_task is not None:
            if not load_task.done() and not self._load_cancel_requested:
                self._load_cancel_requested = True
                load_task.cancel()
            try:
                await asyncio.shield(load_task)
            except asyncio.CancelledError:
                if not load_task.cancelled():
                    raise
            except Exception:
                # Failed initialization never earned an unload hook.
                pass

        if not self._loaded or self._unload_attempted:
            return True

        self._unload_attempted = True
        marker = _HookMarker()
        token = self._hook_marker.set(marker)
        try:
            callback = getattr(self.plugin, "on_unload", None)
            if not callable(callback):
                return True
            result = callback()
            if inspect.isawaitable(result):
                await result
            return True
        except (asyncio.CancelledError, Exception):
            return False
        finally:
            marker.active = False
            self._hook_marker.reset(token)
