"""Bounded daemon worker for native scene sampling and desktop/UIA actions."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DesktopWorkResult:
    job_id: str
    kind: str
    generation: int
    submitted_at: float
    completed_at: float
    deadline: float
    value: Any = None
    error: str | None = None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class _DesktopWorkItem:
    job_id: str
    kind: str
    generation: int
    submitted_at: float
    deadline: float
    payload: Any
    survive_invalidation: bool = False


class DesktopInteractionWorker:
    """Run all native COM and UIA calls off the pygame thread.

    The daemon may remain stuck if a broken native provider never returns, but
    the renderer never joins it without a short timeout and continues to draw.
    Main-thread polling emits one timeout result at the deadline; any later
    completion is discarded. Generation invalidation rejects paused/stale work.
    """

    def __init__(
        self,
        *,
        own_hwnd: int | None,
        own_pid: int | None,
        timeout_seconds: float = 3.0,
        scene_provider_factory: Callable[..., Any] | None = None,
        uia: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        queue_size: int = 12,
    ) -> None:
        self.own_hwnd = own_hwnd
        self.own_pid = own_pid
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._clock = clock
        self._scene_provider_factory = scene_provider_factory
        self._uia = uia
        self._tasks: queue.Queue[_DesktopWorkItem | None] = queue.Queue(
            maxsize=max(2, int(queue_size))
        )
        self._results: queue.Queue[DesktopWorkResult] = queue.Queue(maxsize=32)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._generation = 0
        self._active_item: _DesktopWorkItem | None = None
        self._timed_out_job_ids: set[str] = set()
        self._pending_kinds: set[str] = set()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._run,
                name="nachobot-desktop-interactions",
                daemon=True,
            )
            self._thread.start()

    def invalidate(self) -> int:
        """Invalidate every queued result/action and return the new generation."""

        with self._lock:
            self._generation += 1
            return self._generation

    def submit(
        self,
        kind: str,
        payload: Any = None,
        *,
        timeout_seconds: float | None = None,
        generation: int | None = None,
        survive_invalidation: bool = False,
    ) -> str | None:
        if kind not in {
            "snapshot",
            "inspect_gui",
            "move_window",
            "move_icon",
            "gui_action",
            "restore",
        }:
            raise ValueError(f"unsupported desktop work kind: {kind}")
        self.start()
        now = self._clock()
        timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else max(0.1, float(timeout_seconds))
        )
        with self._lock:
            if self._stop_requested or kind in self._pending_kinds:
                return None
            selected_generation = self._generation if generation is None else generation
            item = _DesktopWorkItem(
                job_id=uuid.uuid4().hex,
                kind=kind,
                generation=selected_generation,
                submitted_at=now,
                deadline=now + timeout,
                payload=payload,
                survive_invalidation=survive_invalidation,
            )
            try:
                self._tasks.put_nowait(item)
            except queue.Full:
                return None
            self._pending_kinds.add(kind)
            return item.job_id

    def poll(
        self,
        *,
        now: float | None = None,
        allow_stale_kinds: set[str] | None = None,
    ) -> list[DesktopWorkResult]:
        current_time = self._clock() if now is None else now
        output: list[DesktopWorkResult] = []
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._pending_kinds.discard(result.kind)
                is_timed_out = result.job_id in self._timed_out_job_ids
                if is_timed_out:
                    self._timed_out_job_ids.discard(result.job_id)
                is_current = (
                    result.generation == self._generation
                    or result.kind in (allow_stale_kinds or set())
                )
            if not is_timed_out and is_current:
                if result.completed_at > result.deadline:
                    output.append(
                        DesktopWorkResult(
                            job_id=result.job_id,
                            kind=result.kind,
                            generation=result.generation,
                            submitted_at=result.submitted_at,
                            completed_at=result.deadline,
                            deadline=result.deadline,
                            error="desktop operation timed out",
                            timed_out=True,
                        )
                    )
                else:
                    output.append(result)

        with self._lock:
            active = self._active_item
            if (
                active is not None
                and active.generation == self._generation
                and not self._stop_requested
                and active.deadline <= current_time
                and active.job_id not in self._timed_out_job_ids
            ):
                self._timed_out_job_ids.add(active.job_id)
                self._pending_kinds.discard(active.kind)
                output.append(
                    DesktopWorkResult(
                        job_id=active.job_id,
                        kind=active.kind,
                        generation=active.generation,
                        submitted_at=active.submitted_at,
                        completed_at=current_time,
                        deadline=active.deadline,
                        error="desktop operation timed out",
                        timed_out=True,
                    )
                )
        return output

    def stop(self, *, join_timeout: float = 0.2) -> None:
        with self._lock:
            self._stop_requested = True
            self._generation += 1
            thread = self._thread
        while True:
            try:
                self._tasks.get_nowait()
            except queue.Empty:
                break
        try:
            self._tasks.put_nowait(None)
        except queue.Full:
            pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, min(1.0, join_timeout)))

    def _run(self) -> None:
        pythoncom = None
        try:
            try:
                import pythoncom as _pythoncom

                pythoncom = _pythoncom
                pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            except ImportError:
                pythoncom = None
            from .desktop_scene import DesktopSceneProvider

            provider_factory = self._scene_provider_factory or DesktopSceneProvider
            provider = provider_factory(
                self.own_hwnd,
                self.own_pid,
                include_icons=True,
            )
            if self._uia is None:
                from .desktop_uia import DesktopUIA

                self._uia = DesktopUIA()
            while True:
                item = self._tasks.get()
                if item is None:
                    break
                with self._lock:
                    self._active_item = item
                    should_stop = self._stop_requested
                if not should_stop and self._action_allowed(item):
                    result = self._perform(provider, item)
                    self._post_result(result)
                elif not should_stop and item.generation == self.generation:
                    self._post_result(self._timeout_result(item))
                with self._lock:
                    self._active_item = None
                    self._pending_kinds.discard(item.kind)
                    self._timed_out_job_ids.discard(item.job_id)
        except Exception as exc:
            # Initialization failures become an explicit result and leave the
            # render loop usable. No native access is attempted on that thread.
            with self._lock:
                active = self._active_item
                self._active_item = None
                if active is not None:
                    self._pending_kinds.discard(active.kind)
            if active is not None:
                self._post_result(
                    DesktopWorkResult(
                        active.job_id,
                        active.kind,
                        active.generation,
                        active.submitted_at,
                        self._clock(),
                        active.deadline,
                        error=f"worker initialization failed: {exc}",
                    )
                )
            while True:
                try:
                    item = self._tasks.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    continue
                with self._lock:
                    self._pending_kinds.discard(item.kind)
                    is_current = item.generation == self._generation
                if is_current:
                    self._post_result(
                        DesktopWorkResult(
                            item.job_id,
                            item.kind,
                            item.generation,
                            item.submitted_at,
                            self._clock(),
                            item.deadline,
                            error=f"worker initialization failed: {exc}",
                        )
                    )
        finally:
            provider = locals().get("provider")
            if provider is not None:
                try:
                    provider.close()
                except Exception:
                    pass
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _perform(self, provider: Any, item: _DesktopWorkItem) -> DesktopWorkResult:
        try:
            if not self._action_allowed(item):
                return self._timeout_result(item)
            payload = item.payload
            if item.kind == "snapshot":
                value = provider.snapshot()
            elif item.kind == "inspect_gui":
                payload = item.payload
                if not isinstance(payload, tuple) or len(payload) != 4:
                    value = ()
                else:
                    value = self._uia.inspect_window_identity(*payload)
            elif item.kind == "move_window":
                value = provider.move_window(
                    **payload,
                    action_allowed=lambda: self._action_allowed(item),
                )
            elif item.kind == "move_icon":
                value = provider.move_icon(
                    **payload,
                    action_allowed=lambda: self._action_allowed(item),
                )
            elif item.kind == "gui_action":
                value = self._uia.execute(
                    payload,
                    action_allowed=lambda: self._action_allowed(item),
                )
            else:
                value = (provider.restore_windows(), provider.restore_icons())
            if self._clock() > item.deadline:
                return self._timeout_result(item)
            return DesktopWorkResult(
                item.job_id,
                item.kind,
                item.generation,
                item.submitted_at,
                self._clock(),
                item.deadline,
                value=value,
            )
        except Exception as exc:
            return DesktopWorkResult(
                item.job_id,
                item.kind,
                item.generation,
                item.submitted_at,
                self._clock(),
                item.deadline,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _action_allowed(self, item: _DesktopWorkItem) -> bool:
        with self._lock:
            return (
                not self._stop_requested
                and (item.generation == self._generation or item.survive_invalidation)
                and item.deadline >= self._clock()
            )

    def _timeout_result(self, item: _DesktopWorkItem) -> DesktopWorkResult:
        return DesktopWorkResult(
            item.job_id,
            item.kind,
            item.generation,
            item.submitted_at,
            self._clock(),
            item.deadline,
            error="desktop operation timed out or was cancelled",
            timed_out=True,
        )

    def _post_result(self, result: DesktopWorkResult) -> None:
        try:
            self._results.put_nowait(result)
        except queue.Full:
            try:
                self._results.get_nowait()
                self._results.put_nowait(result)
            except (queue.Empty, queue.Full):
                pass
