"""Cooperative cancellation boundaries for host-owned async work.

The guard records only the cancellation count at the root boundary. It does
not retain a task, cancel work, or remember cancellations that an enclosing
``asyncio.timeout`` has already recovered from.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import weakref
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar


_P = ParamSpec("_P")
_R = TypeVar("_R")


class _CancellationGuard:
    __slots__ = ("owner_ref", "baseline", "active")

    def __init__(self, owner: asyncio.Task[Any], baseline: int) -> None:
        self.owner_ref: weakref.ReferenceType[asyncio.Task[Any]] | None = weakref.ref(owner)
        self.baseline = baseline
        self.active = True


_ACTIVE_GUARD: contextvars.ContextVar[_CancellationGuard | None] = contextvars.ContextVar(
    "nachobot_tool_cancellation_guard", default=None
)


def _current_guard() -> tuple[asyncio.Task[Any], _CancellationGuard]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    guard = _ACTIVE_GUARD.get()
    if (
        task is None
        or guard is None
        or not guard.active
        or guard.owner_ref is None
        or guard.owner_ref() is not task
    ):
        raise RuntimeError("tool cancellation checks require an active scope owned by the current task")
    return task, guard


@contextmanager
def cancellation_scope() -> Generator[None, None, None]:
    """Create a root guard or borrow the active guard for this same task.

    Context copied into a different task is intentionally ignored. That task
    starts with its own cancellation baseline if it enters a scope.
    """

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        raise RuntimeError("tool cancellation scopes require an active asyncio task")

    inherited = _ACTIVE_GUARD.get()
    if (
        inherited is not None
        and inherited.active
        and inherited.owner_ref is not None
        and inherited.owner_ref() is task
    ):
        yield
        return

    guard = _CancellationGuard(task, task.cancelling())
    token = _ACTIVE_GUARD.set(guard)
    try:
        yield
    finally:
        guard.active = False
        guard.owner_ref = None
        _ACTIVE_GUARD.reset(token)


async def cancellation_checkpoint() -> None:
    """Yield fairly, then surface cancellation added since the root scope."""

    task, guard = _current_guard()
    await asyncio.sleep(0)
    if task.cancelling() > guard.baseline:
        raise asyncio.CancelledError


async def check_cancellation() -> None:
    """Check cheaply on success paths, yielding only when cancellation is new."""

    task, guard = _current_guard()
    if task.cancelling() > guard.baseline:
        await asyncio.sleep(0)
        raise asyncio.CancelledError


def cancellation_boundary(
    function: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    """Wrap an async host boundary with a task-owned cancellation guard."""

    @functools.wraps(function)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with cancellation_scope():
            await cancellation_checkpoint()
            try:
                result = await function(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                await check_cancellation()
                raise
            await check_cancellation()
            return result

    return wrapped
