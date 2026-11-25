"""Per-conversation cutoffs for short-term Planner and Replyer context.

The cutoff is persisted in the existing local store, while message archives and
long-term memory remain untouched. It is intentionally applied only by
short-term prompt readers.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.manager.local_store_manager import local_storage


_STORAGE_KEY = "short_term_context_cutoffs"
_LOCK = threading.RLock()
_CUTOFF_UNSET = object()


class ShortTermContextPersistenceError(RuntimeError):
    """Raised when a context reset could not be saved durably."""


def clear_short_term_context(stream_id: str) -> float:
    """Persist a new per-stream short-term context cutoff and return its timestamp.

    A persistence failure leaves both the active cutoff and local-store value at
    their previous state. The local store writes atomically, so its prior file is
    retained on a failed save.
    """

    key = _stream_key(stream_id)
    with _LOCK:
        previous_store_value = local_storage[_STORAGE_KEY]
        has_previous_store_value = _STORAGE_KEY in local_storage
        previous_cutoffs = previous_store_value if isinstance(previous_store_value, Mapping) else {}
        previous_cutoff = _valid_timestamp(previous_cutoffs.get(key))
        cutoff = time.time()
        if previous_cutoff is not None and cutoff <= previous_cutoff:
            cutoff = math.nextafter(previous_cutoff, math.inf)

        updated_store_value = dict(previous_cutoffs)
        updated_store_value[key] = cutoff
        try:
            local_storage[_STORAGE_KEY] = updated_store_value
        except Exception as exc:
            # LocalStoreManager updates its in-memory dict before the atomic file
            # save. Restore that in-memory value too, so failed commands remain
            # retryable and do not appear successful in this process.
            if has_previous_store_value:
                local_storage.store[_STORAGE_KEY] = previous_store_value
            else:
                local_storage.store.pop(_STORAGE_KEY, None)
            raise ShortTermContextPersistenceError(
                f"Could not persist short-term context cutoff for stream {key!r}"
            ) from exc

        return cutoff


def get_short_term_context_cutoff(stream_id: str) -> float | None:
    """Return the persisted cutoff for a stream, if one has been set."""

    key = _stream_key(stream_id)
    with _LOCK:
        stored_value = local_storage[_STORAGE_KEY]
        if not isinstance(stored_value, Mapping):
            return None
        return _valid_timestamp(stored_value.get(key))


def filter_short_term_context_rows(
    stream_id: str,
    rows: Iterable[Any],
    *,
    cutoff: float | None | object = _CUTOFF_UNSET,
) -> list[Any]:
    """Keep only timestamped rows newer than this stream's cutoff.

    The same helper filters message snapshots and explicit Planner action history.
    """
    if cutoff is _CUTOFF_UNSET:
        cutoff = get_short_term_context_cutoff(stream_id)
    else:
        cutoff = _valid_timestamp(cutoff)
    materialized = list(rows)
    if cutoff is None:
        return materialized
    # Drop timestamp-less rows after a reset: keeping one could reintroduce
    # pre-reset content from an incomplete snapshot.
    filtered = []
    for row in materialized:
        timestamp = _timestamp(row)
        if timestamp is not None and timestamp > cutoff:
            filtered.append(row)
    return filtered


def _stream_key(stream_id: str) -> str:
    if not isinstance(stream_id, str) or not stream_id:
        raise ValueError("stream_id must be a non-empty string")
    return stream_id


def _timestamp(value: Any) -> float | None:
    timestamp_value = value.get("time") if isinstance(value, Mapping) else getattr(value, "time", None)
    return _valid_timestamp(timestamp_value)


def _valid_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


__all__ = [
    "ShortTermContextPersistenceError",
    "clear_short_term_context",
    "filter_short_term_context_rows",
    "get_short_term_context_cutoff",
]
