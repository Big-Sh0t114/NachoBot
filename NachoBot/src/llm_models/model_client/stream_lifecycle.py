"""Shared ownership for provider streaming responses."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from ..exceptions import ReqAbortException
from .base_client import (
    _cancel_and_drain_request_task,
    _consume_task_exception,
    _track_detached_request_task,
)


_STREAM_CLEANUP_TIMEOUT_SECONDS = 0.25
_NOT_ACQUIRED = object()


class _StreamOwner:
    """Retire one acquired SDK stream exactly once, including late acquisitions."""

    def __init__(self, completion_future: asyncio.Future[None] | None = None) -> None:
        self.stream: Any = _NOT_ACQUIRED
        self._operation_task: asyncio.Task[Any] | None = None
        self._operation_done = False
        self._handler_active = False
        self._retired = False
        self._close_started = False
        self._close_task: asyncio.Task[None] | None = None
        self._close_attempt_failed = False
        self._completion_future = completion_future

    def operation_setup_failed(self) -> None:
        """Roll back a completion lease if the operation task cannot be created."""
        self._operation_done = True
        self._check_full_completion()

    def bind_operation(self, task: asyncio.Task[Any]) -> None:
        self._operation_task = task

    def attach(self, stream: Any) -> None:
        self.stream = stream
        if self._retired:
            self._start_close_if_safe()

    def begin_handler(self) -> None:
        self._handler_active = True

    def finish_handler(self) -> None:
        self._handler_active = False
        self._start_close_if_safe()

    def retire(self) -> None:
        # This state change is synchronous so every later completion observes it.
        self._retired = True
        self._start_close_if_safe()

    def operation_finished(self, task: asyncio.Task[Any]) -> None:
        self._handler_active = False
        self.retire()
        self._operation_done = True
        _consume_task_exception(task)
        self._check_full_completion()

    def _check_full_completion(self) -> None:
        future = self._completion_future
        if future is None or future.done() or not self._operation_done:
            return
        if self.stream is _NOT_ACQUIRED:
            future.set_result(None)
            return
        close_finished = self._close_attempt_failed or (
            self._close_task is not None and self._close_task.done()
        )
        if close_finished:
            future.set_result(None)

    def _start_close_if_safe(self) -> None:
        if self.stream is _NOT_ACQUIRED or not self._retired or self._handler_active or self._close_started:
            return

        operation = self._operation_task
        if operation is not None and not operation.done() and self._handler_active:
            return

        self._close_started = True
        stream = self.stream
        close_coroutine = _close_public_stream(stream)
        try:
            close_task = asyncio.create_task(close_coroutine)
        except BaseException:
            close_coroutine.close()
            self._close_attempt_failed = True
            self._check_full_completion()
            return
        self._close_task = close_task
        # Keep a timed-out (or caller-abandoned) close alive and consume its result.
        _track_detached_request_task(close_task)

        def close_finished(done: asyncio.Task[None]) -> None:
            _consume_task_exception(done)
            self._check_full_completion()

        close_task.add_done_callback(close_finished)

    async def observe_close(self) -> None:
        close_task = self._close_task
        if close_task is None:
            return
        try:
            done, _ = await asyncio.wait(
                {close_task},
                timeout=_STREAM_CLEANUP_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            # The close task is already tracked and must outlive this waiter.
            raise
        if close_task in done:
            _consume_task_exception(close_task)


async def _close_public_stream(stream: Any) -> None:
    """Attempt one public SDK close, preferring ``aclose`` when available."""
    close = getattr(stream, "aclose", None)
    if close is None:
        close = getattr(stream, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _run_operation(
    owner: _StreamOwner,
    create_request: Callable[[], Awaitable[Any]],
    handler: Callable[[Any, asyncio.Event | None], Awaitable[Any]],
    interrupt_flag: asyncio.Event | None,
) -> Any:
    if owner._retired:
        return None
    if interrupt_flag is not None and interrupt_flag.is_set():
        raise ReqAbortException("请求被外部信号中断")

    stream = await create_request()
    owner.attach(stream)
    if owner._retired:
        return None
    if interrupt_flag is not None and interrupt_flag.is_set():
        raise ReqAbortException("请求被外部信号中断")

    owner.begin_handler()
    token = _current_stream_owner.set(owner)
    try:
        # Preserve raw stream identity and the caller's exact interrupt object.
        return await handler(stream, interrupt_flag)
    finally:
        _current_stream_owner.reset(token)
        owner.finish_handler()


_current_stream_owner: contextvars.ContextVar[_StreamOwner | None] = contextvars.ContextVar(
    "current_managed_response_stream_owner",
    default=None,
)


def is_managed_response_stream(stream: Any) -> bool:
    """Whether ``stream`` is the raw SDK object owned in this handler context."""
    owner = _current_stream_owner.get()
    return stream is not None and owner is not None and owner.stream is stream


def _schedule_drain(task: asyncio.Task[Any] | None) -> asyncio.Task[None] | None:
    if task is None:
        return None
    if task.done():
        _consume_task_exception(task)
        return None

    # Schedule and track cleanup synchronously before the next await. The shared
    # base helper is the sole cancellation issuer for this child.
    drain_coroutine = _cancel_and_drain_request_task(task)
    try:
        drain_task = asyncio.create_task(drain_coroutine)
    except BaseException:
        drain_coroutine.close()
        # If cleanup-task creation itself fails, still issue one cancellation and
        # retain the provider task until its actual completion callback runs.
        task.cancel()
        _track_detached_request_task(task)
        return None
    _track_detached_request_task(drain_task)
    return drain_task


async def _observe_drains(tasks: set[asyncio.Task[None]]) -> None:
    if not tasks:
        return
    try:
        await asyncio.wait(
            tasks,
            timeout=_STREAM_CLEANUP_TIMEOUT_SECONDS + 0.05,
            return_when=asyncio.ALL_COMPLETED,
        )
    except asyncio.CancelledError:
        # Drain tasks are tracked before this first await and keep cleaning up.
        raise
    for task in tasks:
        _consume_task_exception(task)


async def run_provider_stream(
    create_request: Callable[[], Awaitable[Any]],
    handler: Callable[[Any, asyncio.Event | None], Awaitable[Any]],
    interrupt_flag: asyncio.Event | None,
    *,
    provider_client: Any = None,
) -> Any:
    """Acquire, process, and retire one provider stream as a single operation.

    The request factory is called only after the preset-interrupt check. The
    handler receives the original SDK object and flag; a shared owner watches both
    acquisition and handler work so an event can interrupt a blocked next-frame
    read. SDK work that resists cancellation remains tracked and is retired if it
    later yields a stream.
    """
    if interrupt_flag is not None and interrupt_flag.is_set():
        raise ReqAbortException("请求被外部信号中断")

    completion_future = asyncio.get_running_loop().create_future() if provider_client is not None else None
    owner = _StreamOwner(completion_future)
    if completion_future is not None:
        # Register before operation creation or its first await. The selection
        # scope ignores borrowed clients and unrelated nested provider calls.
        from .client_lifecycle import retain_client_until

        retain_client_until(provider_client, completion_future)

    operation_coroutine = _run_operation(owner, create_request, handler, interrupt_flag)
    try:
        operation = asyncio.create_task(operation_coroutine)
    except BaseException:
        try:
            operation_coroutine.close()
        except BaseException:
            pass
        owner.operation_setup_failed()
        raise
    owner.bind_operation(operation)
    # Register before the first await: late results must always retire their stream.
    operation.add_done_callback(owner.operation_finished)

    event_waiter: asyncio.Task[bool] | None = None
    if interrupt_flag is not None:
        event_waiter_coroutine = None
        try:
            event_waiter_coroutine = interrupt_flag.wait()
            event_waiter = asyncio.create_task(event_waiter_coroutine)
        except BaseException:
            if event_waiter_coroutine is not None:
                try:
                    event_waiter_coroutine.close()
                except BaseException:
                    pass
            owner.retire()
            drain = _schedule_drain(operation)
            if drain is not None:
                await _observe_drains({drain})
            await owner.observe_close()
            raise

    try:
        watched: set[asyncio.Task[Any]] = {operation}
        if event_waiter is not None:
            watched.add(event_waiter)
        done, _ = await asyncio.wait(
            watched,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        owner.retire()
        drains = {
            task
            for task in (
                _schedule_drain(operation),
                _schedule_drain(event_waiter),
            )
            if task is not None
        }
        await _observe_drains(drains)
        await owner.observe_close()
        raise

    event_won = bool(
        event_waiter is not None and (event_waiter in done or interrupt_flag is not None and interrupt_flag.is_set())
    )
    if event_won:
        owner.retire()
        drains = {
            task
            for task in (
                _schedule_drain(operation),
                _schedule_drain(event_waiter),
            )
            if task is not None
        }
        await _observe_drains(drains)
        await owner.observe_close()
        raise ReqAbortException("请求被外部信号中断")

    # Capture the provider/handler outcome before cleanup but deliver it afterward.
    outcome: Any = None
    failure: BaseException | None = None
    try:
        outcome = operation.result()
    except BaseException as exc:
        failure = exc

    owner.retire()
    waiter_drain = _schedule_drain(event_waiter) if event_waiter is not None else None
    if waiter_drain is not None:
        await _observe_drains({waiter_drain})
    elif event_waiter is not None:
        _consume_task_exception(event_waiter)
    await owner.observe_close()

    # An interrupt set during normal handler/close cleanup still wins over success
    # or a provider failure. A Task.cancel delivered at either cleanup await above
    # has already propagated without cancelling the tracked SDK close.
    if interrupt_flag is not None and interrupt_flag.is_set():
        raise ReqAbortException("请求被外部信号中断")
    if failure is not None:
        raise failure
    return outcome
