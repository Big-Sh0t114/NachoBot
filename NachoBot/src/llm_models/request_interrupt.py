"""Shared request-interrupt boundaries for the LLM scheduler."""

import asyncio

from .exceptions import ReqAbortException


def raise_if_request_interrupted(flag: asyncio.Event | None) -> None:
    """Raise the scheduler's normal abort exception when a request is obsolete."""
    if flag is not None and flag.is_set():
        raise ReqAbortException("请求被外部信号中断")


async def wait_for_retry_interval(delay: float, flag: asyncio.Event | None) -> None:
    """Wait for a retry delay, waking promptly when the request is interrupted."""
    if flag is None:
        await asyncio.sleep(delay)
        return

    raise_if_request_interrupted(flag)
    if delay <= 0:
        # Keep asyncio.sleep's yield behavior for zero and negative delays.
        await asyncio.sleep(delay)
        raise_if_request_interrupted(flag)
        return

    try:
        async with asyncio.timeout(delay):
            await flag.wait()
    except TimeoutError:
        # The event and timer can become ready on the same loop turn. The
        # explicit check below gives a set event precedence over retrying.
        pass
    raise_if_request_interrupted(flag)
