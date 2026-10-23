"""Small, privacy-conscious logging helpers shared by the host and worker adapter."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any


_logger = logging.getLogger("mai_recog_self")
_BASE64_LIKE = re.compile(r"[A-Za-z0-9+/=_-]{64,}")
_MAX_IMAGE_INDEX = 2**31 - 1


def _text(value: Any, limit: int = 256) -> str | None:
    if value is None:
        return None
    try:
        rendered = str(value)[:4096]
    except Exception:
        return "<unavailable>"
    rendered = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in rendered)
    rendered = _BASE64_LIKE.sub("[redacted]", rendered)
    rendered = " ".join(rendered.split())
    return rendered[:limit] or None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not finite:
        return None
    return value


def _event(name: str, level: int, values: dict[str, Any]) -> None:
    try:
        payload = {"event": name, **values}
        _logger.log(level, "%s", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        # Observability must never change recognition or message handling.
        return


def exception_summary(error: Any) -> str | None:
    return _text(error, 384)


def image_result(
    *,
    message_id: Any,
    image_index: int,
    status: Any,
    reason: Any = None,
    predicted_class: Any = None,
    character: Any = None,
    score: Any = None,
    margin: Any = None,
    actual_version: Any = None,
    desired_version: Any = None,
    is_self: Any = None,
    annotation: str,
    error: Any = None,
    warming: bool | None = None,
) -> None:
    status_text = _text(status, 48)
    level = logging.WARNING if status_text in {"model_error", "timeout", "worker_error"} else logging.INFO
    values = {
        "message_id": _text(message_id, 128),
        "image_index": min(max(0, image_index), _MAX_IMAGE_INDEX)
        if isinstance(image_index, int) and not isinstance(image_index, bool)
        else 0,
        "status": status_text,
        "reason": _text(reason, 96),
        "predicted_class": _text(predicted_class, 128),
        "character": _text(character, 128),
        "score": _number(score),
        "margin": _number(margin),
        "actual_version": _text(actual_version, 128),
        "desired_version": _text(desired_version, 128),
        "is_self": is_self if isinstance(is_self, bool) else None,
        "annotation": annotation if annotation in {"added", "rejected"} else "rejected",
        "error": _text(error, 384),
        "warming": warming if isinstance(warming, bool) else None,
    }
    _event("image_recognition", level, {key: value for key, value in values.items() if value is not None})


def preprocessing_stopped(*, message_id: Any, reason: Any, image_count: int) -> None:
    _event(
        "preprocessing_stopped",
        logging.INFO,
        {
            "message_id": _text(message_id, 128),
            "reason": _text(reason, 96),
            "image_count": max(0, image_count) if isinstance(image_count, int) and not isinstance(image_count, bool) else 0,
        },
    )


def preprocessing_error(*, message_id: Any, error: Any) -> None:
    _event(
        "preprocessing_failed",
        logging.WARNING,
        {"message_id": _text(message_id, 128), "error": exception_summary(error)},
    )


def tool_result(
    *,
    tool_name: Any,
    message_id: Any = None,
    image_index: int | None = None,
    status: Any,
    reason: Any = None,
    predicted_class: Any = None,
    character: Any = None,
    score: Any = None,
    margin: Any = None,
    actual_version: Any = None,
    desired_version: Any = None,
    is_self: Any = None,
    error: Any = None,
    warming: bool | None = None,
) -> None:
    status_text = _text(status, 48)
    level = logging.WARNING if status_text in {"model_error", "timeout", "worker_error"} else logging.INFO
    values = {
        "tool_name": _text(tool_name, 96),
        "message_id": _text(message_id, 128),
        "image_index": min(max(0, image_index), _MAX_IMAGE_INDEX)
        if isinstance(image_index, int) and not isinstance(image_index, bool)
        else None,
        "status": status_text,
        "reason": _text(reason, 96),
        "predicted_class": _text(predicted_class, 128),
        "character": _text(character, 128),
        "score": _number(score),
        "margin": _number(margin),
        "actual_version": _text(actual_version, 128),
        "desired_version": _text(desired_version, 128),
        "is_self": is_self if isinstance(is_self, bool) else None,
        "error": _text(error, 384),
        "warming": warming if isinstance(warming, bool) else None,
    }
    _event("tool_image_recognition", level, {key: value for key, value in values.items() if value is not None})


def runtime_event(
    event: str,
    *,
    level: int = logging.INFO,
    worker: Any = None,
    desired_version: Any = None,
    actual_version: Any = None,
    status: Any = None,
    error: Any = None,
) -> None:
    values = {
        "worker": _text(worker, 384),
        "desired_version": _text(desired_version, 128),
        "actual_version": _text(actual_version, 128),
        "status": _text(status, 48),
        "error": _text(error, 384),
    }
    _event(event, level, {key: value for key, value in values.items() if value is not None})


def worker_error_summary(stderr_tail: Any) -> str | None:
    """Extract the terminal exception from a bounded, process-local stderr tail."""
    try:
        if not isinstance(stderr_tail, (bytes, bytearray, str)):
            return None
        if isinstance(stderr_tail, str):
            tail = stderr_tail[-8192:]
        else:
            tail = bytes(stderr_tail[-8192:]).decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()[-80:]):
            candidate = line.strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):", candidate):
                return _text(candidate, 384)
        return None
    except Exception:
        return None
