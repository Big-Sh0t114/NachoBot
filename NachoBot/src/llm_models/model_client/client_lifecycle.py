"""Request-local ownership for temporary provider clients."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from ..exceptions import ReqAbortException
from .base_client import _consume_task_exception, _track_detached_request_task


_CLIENT_CLEANUP_TIMEOUT_SECONDS = 0.25
_CACHED_CLIENT_LIFETIMES: dict[int, dict[str, Any]] = {}


def _schedule_cached_client_close(client: Any) -> None:
    async def close_client() -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    close_coroutine = close_client()
    try:
        task = asyncio.create_task(close_coroutine)
    except RuntimeError:
        close_coroutine.close()
        return
    _track_detached_request_task(task)


def retain_cached_client(client: Any) -> None:
    """Keep a cached client alive while a candidate request is using it."""
    entry = _CACHED_CLIENT_LIFETIMES.setdefault(
        id(client), {"client": client, "users": 0, "retired": False}
    )
    entry["users"] += 1


def release_cached_client(client: Any) -> None:
    """Release one request's cached-client lease and close retired clients at zero."""
    entry = _CACHED_CLIENT_LIFETIMES.get(id(client))
    if entry is None or entry["client"] is not client:
        return
    entry["users"] = max(0, entry["users"] - 1)
    if entry["users"]:
        return
    if entry["retired"]:
        _CACHED_CLIENT_LIFETIMES.pop(id(client), None)
        _schedule_cached_client_close(client)
    else:
        _CACHED_CLIENT_LIFETIMES.pop(id(client), None)


def retire_cached_client(client: Any) -> None:
    """Close a removed/replaced cache entry once all captured requests finish."""
    entry = _CACHED_CLIENT_LIFETIMES.setdefault(
        id(client), {"client": client, "users": 0, "retired": False}
    )
    if entry["client"] is not client:
        return
    entry["retired"] = True
    if not entry["users"]:
        _CACHED_CLIENT_LIFETIMES.pop(id(client), None)
        _schedule_cached_client_close(client)


class _ClientSelectionScope:
    """Capture and retire only the fresh client selected by one candidate."""

    def __init__(self, selector: object, interrupt_flag: asyncio.Event | None) -> None:
        self.selector = selector
        self.interrupt_flag = interrupt_flag
        self.client: Any = None
        self.owned = False
        self.captured = False
        self.retired = False
        self.dependencies: set[asyncio.Future[Any]] = set()
        self.close_task: asyncio.Task[None] | None = None
        self.shared_client_retained = False

    def capture(self, selector: object, client: Any, owned: bool) -> None:
        if self.retired or self.captured or selector is not self.selector:
            return
        self.captured = True
        self.client = client
        self.owned = owned
        if not owned:
            retain_cached_client(client)
            self.shared_client_retained = True

    def retain_until(self, client: Any, completion: asyncio.Future[Any]) -> None:
        if self.retired or client is not self.client or completion.done():
            return
        self.dependencies.add(completion)

        def completed(done: asyncio.Future[Any]) -> None:
            self.dependencies.discard(done)
            self._start_close_if_ready()

        completion.add_done_callback(completed)

    def retire(self) -> None:
        self.retired = True
        self._start_close_if_ready()

    def _start_close_if_ready(self) -> None:
        if self.dependencies:
            self.dependencies.difference_update(
                dependency for dependency in tuple(self.dependencies) if dependency.done()
            )
        if not self.retired or self.client is None or self.dependencies:
            return

        client = self.client
        if not self.owned:
            if self.shared_client_retained:
                release_cached_client(client)
                self.shared_client_retained = False
            self.client = None
            return
        if self.close_task is not None:
            return

        async def close_client() -> None:
            # Keep client hooks inside detached cleanup so a faulty descriptor
            # cannot replace the request's primary result or failure.
            close = getattr(client, "close", None)
            if not callable(close):
                return
            result = close()
            if inspect.isawaitable(result):
                await result

        close_coroutine = close_client()
        try:
            task = asyncio.create_task(close_coroutine)
        except BaseException:
            close_coroutine.close()
            self.client = None
            self.owned = False
            return

        self.close_task = task
        _track_detached_request_task(task)

        def close_finished(done: asyncio.Task[None]) -> None:
            _consume_task_exception(done)
            if self.close_task is done:
                self.close_task = None
                self.client = None
                self.owned = False

        task.add_done_callback(close_finished)


_current_client_scope: contextvars.ContextVar[_ClientSelectionScope | None] = contextvars.ContextVar(
    "current_model_client_selection_scope",
    default=None,
)


def capture_selected_client(selector: object, client: Any, *, owned: bool) -> None:
    """Capture the first selection made by the active matching candidate scope."""
    scope = _current_client_scope.get()
    if scope is not None:
        scope.capture(selector, client, owned)


def retain_client_until(client: Any, completion_future: asyncio.Future[Any]) -> None:
    """Keep the active owned client alive until one SDK operation really ends."""
    scope = _current_client_scope.get()
    if scope is not None:
        scope.retain_until(client, completion_future)


@asynccontextmanager
async def model_client_selection(
    selector: object,
    select_callable: Callable[[], tuple[Any, Any, Any]],
    interrupt_flag: asyncio.Event | None = None,
) -> AsyncIterator[tuple[Any, Any, Any]]:
    """Scope selection and execution together so every owned client is retired."""
    scope = _ClientSelectionScope(selector, interrupt_flag)
    token = _current_client_scope.set(scope)
    owner_task = asyncio.current_task()
    cancelling_at_entry = owner_task.cancelling() if owner_task is not None else 0
    primary_failure: BaseException | None = None
    try:
        selected = select_callable()
        yield selected
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        _current_client_scope.reset(token)
        scope.retire()
        close_task = scope.close_task
        if close_task is not None:
            try:
                done, _ = await asyncio.wait(
                    {close_task},
                    timeout=_CLIENT_CLEANUP_TIMEOUT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # The tracked close must outlive caller cancellation.
                raise
            except Exception:
                # Cleanup cannot replace the request's result or failure.
                pass
            else:
                if close_task in done:
                    _consume_task_exception(close_task)

        # A synchronous selection hook can schedule cancellation and raise an
        # abort before cancellation reaches an await. Compare with entry state so
        # an earlier handled cancellation count is not resurrected.
        cancellation_was_requested = (
            owner_task is not None and owner_task.cancelling() > cancelling_at_entry
        )
        if cancellation_was_requested and not isinstance(primary_failure, asyncio.CancelledError):
            # First give a pending Task.cancel() one checkpoint to deliver its
            # original CancelledError (including its caller-supplied message).
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            raise asyncio.CancelledError

        # An Event set during ordinary SDK or cleanup work wins over success,
        # while any already-active exception (especially Task.cancel) is kept.
        if primary_failure is None and interrupt_flag is not None and interrupt_flag.is_set():
            raise ReqAbortException("请求被外部信号中断")
