"""Bounded in-process storage for reusable tool and search results.

Entries are scoped to an owner handle, expire by monotonic age, and are limited
by both owner and process-wide entry and byte budgets. Values are copied on
write and read; only exact built-in JSON-like containers and scalar types are
accepted, so caching never serializes arbitrary objects.
"""

from __future__ import annotations

import hashlib
import math
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from typing import Any, Callable


DEFAULT_RESULT_CACHE_MAX_AGE_SECONDS = 300.0
DEFAULT_RESULT_CACHE_OWNER_MAX_ENTRIES = 64
DEFAULT_RESULT_CACHE_OWNER_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_RESULT_CACHE_MAX_ENTRIES = 2048
DEFAULT_RESULT_CACHE_MAX_BYTES = 32 * 1024 * 1024

_MAX_VALUE_DEPTH = 32
_MAX_VALUE_NODES = 10_000
_ENTRY_OVERHEAD_BYTES = 512


class _UncacheableValue(ValueError):
    """Raised when a result cannot be copied safely within the size budget."""


class _PayloadCopier:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.size_bytes = 0
        self.nodes = 0
        self._active_containers: set[int] = set()

    def _add_size(self, size: int) -> None:
        self.size_bytes += size
        if self.size_bytes > self.max_bytes:
            raise _UncacheableValue("result exceeds the cache entry byte limit")

    def _string_size(self, value: str) -> int:
        if len(value) > self.max_bytes:
            raise _UncacheableValue("string exceeds the cache entry byte limit")
        size = sys.getsizeof(value)
        self._add_size(size)
        return size

    def copy(self, value: Any, depth: int = 0) -> Any:
        self.nodes += 1
        if self.nodes > _MAX_VALUE_NODES:
            raise _UncacheableValue("result exceeds the cache node limit")
        if depth > _MAX_VALUE_DEPTH:
            raise _UncacheableValue("result exceeds the cache nesting limit")

        value_type = type(value)
        if value is None:
            self._add_size(sys.getsizeof(value))
            return None
        if value_type is bool:
            self._add_size(sys.getsizeof(value))
            return value
        if value_type is int:
            self._add_size(sys.getsizeof(value))
            return value
        if value_type is float:
            if not math.isfinite(value):
                raise _UncacheableValue("non-finite floats are not cacheable")
            self._add_size(sys.getsizeof(value))
            return value
        if value_type is str:
            self._string_size(value)
            return value
        if value_type is bytes:
            self._add_size(sys.getsizeof(value))
            return value

        if value_type not in (dict, list, tuple):
            raise _UncacheableValue(f"unsupported result type: {value_type.__name__}")

        identity = id(value)
        if identity in self._active_containers:
            raise _UncacheableValue("cyclic results are not cacheable")
        self._active_containers.add(identity)
        try:
            if value_type is dict:
                original_size = sys.getsizeof(value)
                self._add_size(original_size)
                copied_dict: dict[str, Any] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise _UncacheableValue("result dictionaries require string keys")
                    self._string_size(key)
                    copied_dict[key] = self.copy(item, depth + 1)
                copied_size = sys.getsizeof(copied_dict)
                if copied_size > original_size:
                    self._add_size(copied_size - original_size)
                return copied_dict

            original_size = sys.getsizeof(value)
            self._add_size(original_size)
            copied_items = [self.copy(item, depth + 1) for item in value]
            if value_type is tuple:
                copied_tuple = tuple(copied_items)
                copied_size = sys.getsizeof(copied_tuple)
                if copied_size > original_size:
                    self._add_size(copied_size - original_size)
                return copied_tuple
            copied_size = sys.getsizeof(copied_items)
            if copied_size > original_size:
                self._add_size(copied_size - original_size)
            return copied_items
        finally:
            self._active_containers.remove(identity)


class _CacheEntry:
    __slots__ = ("owner_id", "value", "remaining_uses", "created_at", "expires_at", "timestamp", "size_bytes")

    def __init__(
        self,
        owner_id: str,
        value: Any,
        remaining_uses: int,
        created_at: float,
        expires_at: float,
        timestamp: float,
        size_bytes: int,
    ):
        self.owner_id = owner_id
        self.value = value
        self.remaining_uses = remaining_uses
        self.created_at = created_at
        self.expires_at = expires_at
        self.timestamp = timestamp
        self.size_bytes = size_bytes


class ResultCacheStore:
    """Thread-safe LRU store shared by result-cache owners."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        owner_max_entries: int = DEFAULT_RESULT_CACHE_OWNER_MAX_ENTRIES,
        owner_max_bytes: int = DEFAULT_RESULT_CACHE_OWNER_MAX_BYTES,
        max_entries: int = DEFAULT_RESULT_CACHE_MAX_ENTRIES,
        max_bytes: int = DEFAULT_RESULT_CACHE_MAX_BYTES,
    ):
        if min(owner_max_entries, owner_max_bytes, max_entries, max_bytes) <= 0:
            raise ValueError("result-cache limits must be positive")

        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._owner_max_entries = owner_max_entries
        self._owner_max_bytes = owner_max_bytes
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()
        self._owner_entries: dict[str, OrderedDict[str, None]] = {}
        self._owner_bytes: dict[str, int] = {}
        self._total_bytes = 0
        self._next_owner_id = 0
        self._lock = threading.RLock()

    def create_owner(
        self,
        namespace: str,
        *,
        max_age_seconds: float = DEFAULT_RESULT_CACHE_MAX_AGE_SECONDS,
    ) -> "ResultCacheOwner":
        """Create a unique owner handle without retaining the caller or its key."""
        digest_builder = hashlib.sha256()
        for offset in range(0, len(namespace), 4096):
            digest_builder.update(
                namespace[offset : offset + 4096].encode("utf-8", errors="surrogatepass")
            )
        digest = digest_builder.hexdigest()
        with self._lock:
            self._next_owner_id += 1
            owner_id = f"{digest}:{self._next_owner_id}"
        return ResultCacheOwner(self, owner_id, max_age_seconds=max_age_seconds)

    def _remove_locked(self, owner_id: str, key: str) -> bool:
        cache_key = (owner_id, key)
        entry = self._entries.pop(cache_key, None)
        if entry is None:
            return False

        owner_entries = self._owner_entries.get(owner_id)
        if owner_entries is not None:
            owner_entries.pop(key, None)
            if not owner_entries:
                self._owner_entries.pop(owner_id, None)

        current_owner_bytes = self._owner_bytes.get(owner_id, 0) - entry.size_bytes
        if current_owner_bytes > 0:
            self._owner_bytes[owner_id] = current_owner_bytes
        else:
            self._owner_bytes.pop(owner_id, None)
        self._total_bytes -= entry.size_bytes
        return True

    def _prune_locked(self, now: float) -> int:
        expired = [
            (owner_id, key)
            for (owner_id, key), entry in self._entries.items()
            if entry.remaining_uses <= 0 or entry.expires_at <= now
        ]
        for owner_id, key in expired:
            self._remove_locked(owner_id, key)
        return len(expired)

    def _make_room_locked(self, owner_id: str, key: str, size_bytes: int) -> bool:
        if size_bytes > self._owner_max_bytes or size_bytes > self._max_bytes:
            return False

        owner_entries = self._owner_entries.get(owner_id)
        while owner_entries and (
            len(owner_entries) >= self._owner_max_entries
            or self._owner_bytes.get(owner_id, 0) + size_bytes > self._owner_max_bytes
        ):
            oldest_owner_key = next(iter(owner_entries))
            self._remove_locked(owner_id, oldest_owner_key)
            owner_entries = self._owner_entries.get(owner_id)

        while self._entries and (
            len(self._entries) >= self._max_entries or self._total_bytes + size_bytes > self._max_bytes
        ):
            oldest_cache_key = next(iter(self._entries))
            self._remove_locked(*oldest_cache_key)

        return True

    def _set(
        self,
        owner_id: str,
        key: str,
        value: Any,
        *,
        uses: int,
        max_age_seconds: float,
    ) -> bool:
        if type(key) is not str:
            return False

        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._remove_locked(owner_id, key)

        if type(uses) is not int or uses <= 0 or max_age_seconds <= 0:
            return False

        try:
            copier = _PayloadCopier(self._owner_max_bytes)
            safe_value = copier.copy(value)
            if len(key) > self._owner_max_bytes:
                return False
            key_size = sys.getsizeof(key)
            namespace_size = sys.getsizeof(owner_id)
            size_bytes = copier.size_bytes + key_size + namespace_size + _ENTRY_OVERHEAD_BYTES
        except (OverflowError, UnicodeError, _UncacheableValue):
            return False
        if size_bytes > self._owner_max_bytes or size_bytes > self._max_bytes:
            return False

        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            self._remove_locked(owner_id, key)
            if not self._make_room_locked(owner_id, key, size_bytes):
                return False

            self._owner_entries.setdefault(owner_id, OrderedDict())
            entry = _CacheEntry(
                owner_id=owner_id,
                value=safe_value,
                remaining_uses=uses,
                created_at=now,
                expires_at=now + max_age_seconds,
                timestamp=self._wall_clock(),
                size_bytes=size_bytes,
            )
            cache_key = (owner_id, key)
            self._entries[cache_key] = entry
            self._owner_entries[owner_id][key] = None
            self._owner_bytes[owner_id] = self._owner_bytes.get(owner_id, 0) + size_bytes
            self._total_bytes += size_bytes
            return True

    def _get(self, owner_id: str, key: str) -> tuple[bool, Any]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            cache_key = (owner_id, key)
            entry = self._entries.get(cache_key)
            if entry is None:
                return False, None

            try:
                value = _PayloadCopier(self._owner_max_bytes).copy(entry.value)
            except _UncacheableValue:
                self._remove_locked(owner_id, key)
                return False, None

            entry.remaining_uses -= 1
            self._entries.move_to_end(cache_key)
            self._owner_entries[owner_id].move_to_end(key)
            return True, value

    def _peek_entry(self, owner_id: str, key: str, field_name: str) -> dict[str, Any] | None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get((owner_id, key))
            if entry is None:
                return None
            try:
                value = _PayloadCopier(self._owner_max_bytes).copy(entry.value)
            except _UncacheableValue:
                self._remove_locked(owner_id, key)
                return None
            return {
                field_name: value,
                "ttl": entry.remaining_uses,
                "timestamp": entry.timestamp,
            }

    def _owner_keys(self, owner_id: str) -> list[str]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return list(self._owner_entries.get(owner_id, ()))

    def _owner_has_key(self, owner_id: str, key: str) -> bool:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return (owner_id, key) in self._entries

    def _owner_count(self, owner_id: str) -> int:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return len(self._owner_entries.get(owner_id, ()))

    def _owner_ttls(self, owner_id: str) -> list[int]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return [
                self._entries[(owner_id, key)].remaining_uses
                for key in self._owner_entries.get(owner_id, ())
                if (owner_id, key) in self._entries
            ]

    def _delete(self, owner_id: str, key: str) -> bool:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return self._remove_locked(owner_id, key)

    def _clear_owner(self, owner_id: str) -> int:
        with self._lock:
            keys = list(self._owner_entries.get(owner_id, ()))
            for key in keys:
                self._remove_locked(owner_id, key)
            return len(keys)

    def _prune_owner(self, owner_id: str) -> int:
        now = self._clock()
        with self._lock:
            before = len(self._owner_entries.get(owner_id, ()))
            self._prune_locked(now)
            after = len(self._owner_entries.get(owner_id, ()))
            return max(before - after, 0)

    @property
    def stats(self) -> dict[str, int]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return {
                "entry_count": len(self._entries),
                "total_bytes": self._total_bytes,
                "owner_count": len(self._owner_entries),
            }


class ResultCacheOwner:
    """A cache namespace whose values never cross into another owner."""

    def __init__(
        self,
        store: ResultCacheStore,
        owner_id: str,
        *,
        max_age_seconds: float,
    ):
        self._store = store
        self._owner_id = owner_id
        age = float(max_age_seconds)
        self.max_age_seconds = max(0.0, age) if math.isfinite(age) else 0.0

    def get(self, key: str) -> tuple[bool, Any]:
        return self._store._get(self._owner_id, key)

    def set(self, key: str, value: Any, *, uses: int) -> bool:
        return self._store._set(
            self._owner_id,
            key,
            value,
            uses=uses,
            max_age_seconds=self.max_age_seconds,
        )

    def peek_entry(self, key: str, field_name: str) -> dict[str, Any] | None:
        return self._store._peek_entry(self._owner_id, key, field_name)

    def keys(self) -> list[str]:
        return self._store._owner_keys(self._owner_id)

    def has_key(self, key: str) -> bool:
        return self._store._owner_has_key(self._owner_id, key)

    def __len__(self) -> int:
        return self._store._owner_count(self._owner_id)

    def ttl_values(self) -> list[int]:
        return self._store._owner_ttls(self._owner_id)

    def delete(self, key: str) -> bool:
        return self._store._delete(self._owner_id, key)

    def clear(self) -> int:
        return self._store._clear_owner(self._owner_id)

    def prune(self) -> int:
        return self._store._prune_owner(self._owner_id)


class ResultCacheMappingView(Mapping[str, dict[str, Any]]):
    """Read-only dict-shaped view over one owner's compatibility metadata."""

    def __init__(self, owner: ResultCacheOwner, field_name: str):
        self._owner = owner
        self._field_name = field_name

    def __getitem__(self, key: str) -> dict[str, Any]:
        entry = self._owner.peek_entry(key, self._field_name)
        if entry is None:
            raise KeyError(key)
        return entry

    def __iter__(self) -> Iterator[str]:
        return iter(self._owner.keys())

    def __len__(self) -> int:
        return len(self._owner)

    def __contains__(self, key: object) -> bool:
        return type(key) is str and self._owner.has_key(key)

    def items(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for key in self._owner.keys():
            try:
                yield key, self[key]
            except KeyError:
                continue

    def values(self) -> Iterator[dict[str, Any]]:
        for _key, value in self.items():
            yield value

    def clear(self) -> None:
        self._owner.clear()


GLOBAL_RESULT_CACHE = ResultCacheStore()


def create_result_cache_owner(namespace: str) -> ResultCacheOwner:
    """Create an owner in the process-wide bounded result cache."""
    return GLOBAL_RESULT_CACHE.create_owner(namespace)
