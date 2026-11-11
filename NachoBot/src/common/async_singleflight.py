"""Bounded, per-owner coalescing for concurrent asynchronous work."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any, Generic, TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")
_MAX_KEY_LENGTH = 256


class SingleFlightCapacityError(RuntimeError):
    """The helper already tracks its full per-loop operation budget."""

    def __init__(self) -> None:
        super().__init__("single-flight capacity reached")


class SingleFlightReentrancyError(RuntimeError):
    """A factory tried to re-enter the same helper."""

    def __init__(self) -> None:
        super().__init__("single-flight factory re-entered its helper")


class _FactoryMarker:
    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


class _Flight(Generic[T]):
    __slots__ = ("key", "task", "waiters", "cancel_requested", "error")

    def __init__(self, key: str) -> None:
        self.key = key
        self.task: asyncio.Task[T] | None = None
        self.waiters = 0
        self.cancel_requested = False
        self.error: BaseException | None = None


class _LoopState:
    __slots__ = ("joinable", "operations")

    def __init__(self) -> None:
        self.joinable: dict[str, _Flight[Any]] = {}
        self.operations: set[_Flight[Any]] = set()


def fingerprint_json(*values: Any) -> str:
    """Return a fixed-size SHA-256 key for trusted JSON scalar inputs.

    Exact built-in scalar types and flat lists/tuples of those scalars are
    accepted. Arbitrary objects are rejected instead of being stringified or
    serialized through user-defined hooks.
    """

    def validate(value: Any) -> None:
        if type(value) in (str, int, bool, type(None)):
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("fingerprint floats must be finite")
            return
        if type(value) in (tuple, list):
            if len(value) > 64:
                raise ValueError("fingerprint sequence is too long")
            if any(type(item) in (tuple, list, dict) for item in value):
                raise TypeError("fingerprint sequences must contain only scalars")
            for item in value:
                validate(item)
            return
        raise TypeError("fingerprint values must be built-in JSON scalars")

    for value in values:
        validate(value)
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class AsyncSingleFlight:
    """Coalesce identical in-flight work without retaining completed values.

    An instance belongs to its owner. Its operation state is separate for each
    running event loop, so tasks are never shared between loops.
    """

    def __init__(self, max_inflight: int = 32) -> None:
        if type(max_inflight) is not int or max_inflight < 1:
            raise ValueError("max_inflight must be a positive integer")
        self._max_inflight = max_inflight
        self._states: WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = WeakKeyDictionary()
        self._registry_lock = threading.Lock()
        self._active_factory: ContextVar[_FactoryMarker | None] = ContextVar(
            f"async_singleflight_factory_{id(self)}", default=None
        )

    def _state_for(self, loop: asyncio.AbstractEventLoop) -> _LoopState:
        # The lock protects only the weak loop registry. No async work or waits
        # happen while it is held.
        with self._registry_lock:
            state = self._states.get(loop)
            if state is None:
                state = _LoopState()
                self._states[loop] = state
            return state

    @classmethod
    def _prune_done(cls, state: _LoopState) -> None:
        for flight in tuple(state.operations):
            task = flight.task
            if task is not None and task.done():
                cls._retire_joinable(state, flight)
                state.operations.discard(flight)

    @staticmethod
    def _retire_joinable(state: _LoopState, flight: _Flight[Any]) -> None:
        if state.joinable.get(flight.key) is flight:
            del state.joinable[flight.key]

    @classmethod
    def _finish_task(cls, state: _LoopState, flight: _Flight[Any], task: asyncio.Task[Any]) -> None:
        cls._retire_joinable(state, flight)
        state.operations.discard(flight)
        try:
            task.exception()
        except BaseException:
            # Retrieving a cancelled task raises CancelledError; this callback
            # consumes it and any late factory exception without logging it.
            pass

    async def run(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        marker = self._active_factory.get()
        if marker is not None and marker.active:
            raise SingleFlightReentrancyError()
        if type(key) is not str:
            raise TypeError("single-flight key must be a string")
        if len(key) > _MAX_KEY_LENGTH:
            raise ValueError("single-flight key is too long")
        if not callable(factory):
            raise TypeError("single-flight factory must be callable")

        # A Task.cancel issued immediately before run() may still be pending
        # when this coroutine starts executing. Reach a cancellation point
        # before publishing any work, then take the historical-count baseline.
        await asyncio.sleep(0)
        caller = asyncio.current_task()
        cancellation_baseline = caller.cancelling() if caller is not None else 0

        loop = asyncio.get_running_loop()
        state = self._state_for(loop)
        self._prune_done(state)
        flight = state.joinable.get(key)

        if flight is None:
            if len(state.operations) >= self._max_inflight:
                raise SingleFlightCapacityError()
            flight = _Flight[T](key)
            flight.waiters = 1
            state.joinable[key] = flight
            state.operations.add(flight)
            runner = self._run_factory(flight, factory)
            try:
                task = loop.create_task(runner)
            except BaseException:
                runner.close()
                self._retire_joinable(state, flight)
                state.operations.discard(flight)
                raise
            flight.task = task
            task.add_done_callback(lambda completed: self._finish_task(state, flight, completed))
        else:
            flight.waiters += 1

        task = flight.task
        if task is None:
            # Only possible if task creation failed, which rolls back before
            # this point. Keep a defensive invariant error free of the key.
            raise RuntimeError("single-flight task was not created")

        waiter_active = True

        def leave_waiter() -> None:
            nonlocal waiter_active
            if not waiter_active:
                return
            waiter_active = False
            flight.waiters -= 1
            if flight.waiters == 0 and not task.done() and not flight.cancel_requested:
                # Remove the old identity before cancelling it so a fresh call
                # may publish a new flight under the same key immediately.
                self._retire_joinable(state, flight)
                flight.cancel_requested = True
                task.cancel()

        try:
            result = await asyncio.shield(task)
            # Eager task execution can cancel this caller before shield is
            # awaited. Yield once before publishing the completed value so
            # that pending caller cancellation keeps its normal precedence.
            await asyncio.sleep(0)
            return result
        except asyncio.CancelledError:
            leave_waiter()
            # The inner task can finish cancelled before a cancellation that
            # was requested for this caller is delivered (notably with eager
            # task factories). Give that caller cancellation its normal chance
            # to arrive before deciding which cancellation to propagate.
            await asyncio.sleep(0)
            # shield preserves this caller's cancellation. If the inner factory
            # itself raised CancelledError, restore that original exception
            # when this caller has not received a new cancellation request.
            caller_was_cancelled = caller is not None and caller.cancelling() > cancellation_baseline
            if not caller_was_cancelled and isinstance(flight.error, asyncio.CancelledError):
                raise flight.error from None
            raise
        except BaseException:
            leave_waiter()
            # Apply the same pending-cancellation checkpoint before publishing
            # an underlying factory error.
            await asyncio.sleep(0)
            raise
        finally:
            leave_waiter()

    async def _run_factory(self, flight: _Flight[T], factory: Callable[[], Awaitable[T]]) -> T:
        marker = _FactoryMarker()
        token: Token[_FactoryMarker | None] = self._active_factory.set(marker)
        try:
            try:
                return await factory()
            except BaseException as exc:
                flight.error = exc
                raise
        finally:
            marker.active = False
            self._active_factory.reset(token)
