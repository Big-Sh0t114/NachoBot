"""Small, process-local cache for expensive media conversions.

Only encoded bytes are stored so callers that decode an image always receive a
fresh mutable Pillow object. Keys include the content digest and every caller
supplied transformation option.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable


# Cache restoration and age based cleanup use the same lock so a cleanup pass
# cannot unlink a file while a cache hit is restoring it.
MEDIA_FILESYSTEM_LOCK = threading.RLock()


@dataclass
class _PendingConversion:
    owner_thread: int
    condition: threading.Condition
    done: bool = False
    result: bytes | None = None
    error: BaseException | None = None


class MediaConversionCache:
    """Thread-safe LRU cache with bounded storage and per-key singleflight."""

    def __init__(self, max_entries: int = 512, max_bytes: int = 32 * 1024 * 1024, max_concurrent: int = 2):
        if max_entries < 1 or max_bytes < 1 or max_concurrent < 1:
            raise ValueError("media cache limits must be positive")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_concurrent = max_concurrent
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, bytes] = OrderedDict()
        self._pending: dict[str, _PendingConversion] = {}
        self._stored_bytes = 0
        self._conversion_slots = threading.BoundedSemaphore(max_concurrent)
        self._thread_state = threading.local()

    @staticmethod
    def make_key(content: bytes | bytearray | memoryview, operation: str, options: object) -> str:
        """Build a stable key from content SHA-256 and serialized options."""
        option_json = json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        digest = hashlib.sha256(content).hexdigest()
        payload = f"{operation}\0{option_json}\0{digest}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def get_or_compute(
        self,
        content: bytes | bytearray | memoryview,
        operation: str,
        options: object,
        converter: Callable[[], bytes | bytearray | memoryview],
    ) -> bytes:
        """Return encoded output, computing once per key under a two-worker cap.

        A nested cache hit is allowed; a nested miss fails explicitly to avoid
        lock, slot, or cross-key singleflight deadlocks. Failed conversions are
        shared with current waiters, then removed so a later call can retry.
        """
        key = self.make_key(content, operation, options)
        thread_id = threading.get_ident()
        active_keys: list[str] = getattr(self._thread_state, "active_keys", [])
        if key in active_keys:
            raise RuntimeError("reentrant media conversion requested its own in-flight key")

        while True:
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return cached

                pending = self._pending.get(key)
                if pending is not None:
                    if active_keys:
                        raise RuntimeError("nested media conversion cannot wait on another in-flight key")
                    if pending.owner_thread == thread_id:
                        raise RuntimeError("reentrant media conversion requested its own in-flight key")
                    while not pending.done:
                        pending.condition.wait()
                    if pending.error is not None:
                        raise pending.error
                    if pending.result is None:  # defensive invariant guard
                        raise RuntimeError("media conversion completed without a result")
                    return pending.result

                if active_keys:
                    raise RuntimeError("nested media conversion cache misses are not supported")

            # Acquire a slot before registering an owner so pending state remains
            # bounded by the configured conversion concurrency.
            self._conversion_slots.acquire()
            owner = False
            pending_to_wait: _PendingConversion | None = None
            try:
                with self._lock:
                    cached = self._entries.get(key)
                    if cached is not None:
                        self._entries.move_to_end(key)
                        return cached
                    pending_to_wait = self._pending.get(key)
                    if pending_to_wait is None:
                        pending_to_wait = _PendingConversion(
                            owner_thread=thread_id,
                            condition=threading.Condition(self._lock),
                        )
                        self._pending[key] = pending_to_wait
                        owner = True
                if not owner:
                    # Drop the slot before waiting or retrying; duplicate keys
                    # must not occupy scarce conversion capacity.
                    continue

                self._thread_state.active_keys = [*active_keys, key]
                result = bytes(converter())
                with self._lock:
                    pending_to_wait.result = result
                    if len(result) <= self.max_bytes:
                        while self._entries and (
                            len(self._entries) >= self.max_entries
                            or self._stored_bytes + len(result) > self.max_bytes
                        ):
                            _old_key, old_value = self._entries.popitem(last=False)
                            self._stored_bytes -= len(old_value)
                        self._entries[key] = result
                        self._stored_bytes += len(result)
                    pending_to_wait.done = True
                    self._pending.pop(key, None)
                    pending_to_wait.condition.notify_all()
                return result
            except BaseException as error:
                if owner and pending_to_wait is not None:
                    with self._lock:
                        pending_to_wait.error = error
                        pending_to_wait.done = True
                        self._pending.pop(key, None)
                        pending_to_wait.condition.notify_all()
                raise
            finally:
                if owner:
                    self._thread_state.active_keys = active_keys
                self._conversion_slots.release()

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def stored_bytes(self) -> int:
        with self._lock:
            return self._stored_bytes

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def clear(self) -> None:
        """Clear stored results. In-flight conversions are allowed to finish."""
        with self._lock:
            self._entries.clear()
            self._stored_bytes = 0


media_conversion_cache = MediaConversionCache()
