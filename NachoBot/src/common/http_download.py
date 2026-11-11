"""Small bounded reader for an already-open HTTPX response stream."""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


_READ_CHUNK_BYTES = 64 * 1024


class BoundedResponseTooLargeError(ValueError):
    """Raised when a decoded response body exceeds its byte budget."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        super().__init__(f"HTTP response exceeds the {max_bytes}-byte limit")


def _content_length_hint(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    content_encoding = str(headers.get("content-encoding", "")).strip().lower()
    if content_encoding and content_encoding != "identity":
        # Content-Length measures encoded bytes; the enforced limit is decoded.
        return None

    raw_length = str(headers.get("content-length", "")).strip()
    if not raw_length or not raw_length.isascii() or not raw_length.isdecimal():
        return None
    try:
        return int(raw_length)
    except (ValueError, OverflowError):
        return None


async def read_bounded_response(response: Any, *, max_bytes: int) -> bytes:
    """Read a complete HTTPX response body without buffering beyond its limit.

    The caller owns the response's streaming context. HTTPX's ``aiter_bytes``
    yields decoded bytes, so encoded response sizes never substitute for the
    enforced application byte budget.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    hinted_length = _content_length_hint(response)
    if hinted_length is not None and hinted_length > max_bytes:
        raise BoundedResponseTooLargeError(max_bytes)

    output = bytearray()
    chunk_size = min(_READ_CHUNK_BYTES, max_bytes)
    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
        remaining = max_bytes - len(output)
        if len(chunk) > remaining:
            raise BoundedResponseTooLargeError(max_bytes)
        output.extend(chunk)
    return bytes(output)


@asynccontextmanager
async def stream_with_deadline(
    client: Any,
    method: str,
    url: str,
    *,
    total_timeout: float,
    **stream_kwargs: Any,
) -> AsyncIterator[Any]:
    """Open and own an HTTP response stream within one cumulative deadline.

    The timeout covers stream creation, context entry, response use, and normal
    response cleanup. It is independent of any transport timeout passed in
    ``stream_kwargs``. The caller retains ownership of the client.
    """

    if type(total_timeout) not in (int, float):
        raise ValueError("total_timeout must be a finite positive int or float")
    try:
        timeout_seconds = float(total_timeout)
    except OverflowError as exc:
        raise ValueError("total_timeout must be a finite positive int or float") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("total_timeout must be a finite positive int or float")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    async with asyncio.timeout_at(deadline) as budget:
        async with client.stream(method, url, **stream_kwargs) as response:
            if loop.time() >= deadline:
                raise TimeoutError("HTTP response exceeded its total download deadline")
            yield response

    # A cooperative response context can suppress cancellation during entry,
    # body iteration, or cleanup. A synchronous overrun also cannot deliver
    # the timeout callback until control returns to the event loop.
    if budget.expired() or loop.time() >= deadline:
        raise TimeoutError("HTTP response exceeded its total download deadline")
