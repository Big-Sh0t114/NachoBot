"""Bounded request reuse that retains the existing model-selection state."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
import hashlib
import json
import threading
from typing import Any, Callable


def _configuration_signature(model_set: Any, config: Any) -> str | None:
    try:
        models = [config.get_model_info(name) for name in model_set.model_list]
        providers = [config.get_provider(model.api_provider) for model in models]
        payload = {"task": asdict(model_set), "models": [asdict(model) for model in models],
                   "providers": [asdict(provider) for provider in providers]}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (AttributeError, TypeError, ValueError, KeyError):
        # Unsupported/custom configuration retains the original construction path.
        return None


class LLMRequestCache:
    def __init__(self, max_entries: int = 64) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, int], tuple[Any, Any, Any, str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, *, model_set: Any, request_type: str, config: Any, factory: Callable[..., Any]) -> Any:
        with self._lock:
            signature = _configuration_signature(model_set, config)
            key = (request_type, id(model_set))
            entry = self._entries.get(key)
            if (signature is not None and entry is not None and entry[0] is model_set and entry[1] is config
                    and entry[2] is factory and entry[3] == signature):
                self._entries.move_to_end(key)
                return entry[4]
            request = factory(model_set=model_set, request_type=request_type)
            if signature is None:
                self._entries.pop(key, None)
                return request
            self._entries[key] = (model_set, config, factory, signature, request)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return request


def cached_replyer_request(owner: Any, **kwargs: Any) -> Any:
    cache = getattr(owner, "_express_request_cache", None)
    if cache is None:
        cache = LLMRequestCache(max_entries=8)
        owner._express_request_cache = cache
    return cache.get(**kwargs)
