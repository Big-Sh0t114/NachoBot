"""Metadata helpers for identifying platform-replayed inbound messages.

Adapters capture the platform timestamp and receive time before any networked
media conversion.  Core uses the transport connection epoch and its own start
boundary to decide whether a row belongs to offline recovery.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

OFFLINE_RECOVERY_KEY = "offline_recovery"
OFFLINE_RECOVERY_VERSION = 1
TIMESTAMP_BOUNDARY_TOLERANCE_SECONDS = 1.0

_core_startup_boundary = time.time()


def normalize_original_timestamp(value: Any, received_at: float) -> tuple[float, str]:
    """Normalize a OneBot timestamp to seconds, preserving fractional values.

    OneBot ``time`` is seconds.  Some forks emit Unix milliseconds; values at
    or above 1e11 are treated as milliseconds and divided by 1000.  Missing,
    boolean, nonnumeric, nonpositive, NaN, and infinite values use the captured
    receive time so they cannot create a false old-message signal.
    """

    fallback = _valid_positive_finite(received_at)
    if fallback is None:
        fallback = time.time()

    if isinstance(value, bool) or value is None:
        return fallback, "receive_time_fallback"
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return fallback, "receive_time_fallback"
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback, "receive_time_fallback"
    if not math.isfinite(timestamp) or timestamp <= 0:
        return fallback, "receive_time_fallback"

    if timestamp >= 100_000_000_000:
        timestamp /= 1000.0
        if not math.isfinite(timestamp) or timestamp <= 0:
            return fallback, "receive_time_fallback"
        return timestamp, "platform_milliseconds"
    return timestamp, "platform_seconds"


def build_recovery_metadata(
    raw: Mapping[str, Any],
    connection_epoch_started_at: float,
    received_at: float,
    progress_id: str,
) -> dict[str, Any]:
    """Build the additive ``additional_config.offline_recovery`` envelope."""

    original_timestamp, timestamp_source = normalize_original_timestamp(raw.get("time"), received_at)
    epoch = _valid_positive_finite(connection_epoch_started_at)
    if epoch is None:
        # Invalid connection metadata must fail closed for replay detection.
        epoch = _valid_positive_finite(received_at) or time.time()
    captured_receive = _valid_positive_finite(received_at) or time.time()
    return {
        "version": OFFLINE_RECOVERY_VERSION,
        "connection_epoch_started_at": epoch,
        "received_at": captured_receive,
        "original_timestamp": original_timestamp,
        "timestamp_source": timestamp_source,
        "progress_id": str(progress_id),
    }


def is_offline_replay(
    metadata: Any,
    *,
    core_startup_at: float | None = None,
    tolerance_seconds: float = TIMESTAMP_BOUNDARY_TOLERANCE_SECONDS,
) -> bool:
    """Return whether a captured platform timestamp predates either boundary."""

    if not isinstance(metadata, Mapping) or metadata.get("version") != OFFLINE_RECOVERY_VERSION:
        return False
    original = _valid_positive_finite(metadata.get("original_timestamp"))
    epoch = _valid_positive_finite(metadata.get("connection_epoch_started_at"))
    core_start = _valid_positive_finite(core_startup_at if core_startup_at is not None else _core_startup_boundary)
    tolerance = float(tolerance_seconds)
    if original is None or epoch is None or core_start is None or not math.isfinite(tolerance) or tolerance < 0:
        return False
    # Adapter-initiated history pulls carry an explicit run boundary because
    # transport outages can happen while both Adapter and Core processes stay
    # alive.  Preserve the true original timestamp and connection epoch while
    # recognizing a verified historical candidate independently of those
    # process-start heuristics.
    history_run = metadata.get("history_run")
    if isinstance(history_run, Mapping):
        run_id = history_run.get("run_id")
        cutoff = _valid_positive_finite(history_run.get("cutoff_at"))
        if isinstance(run_id, str) and run_id.strip() and cutoff is not None and original <= cutoff:
            return True
    return original < epoch - tolerance or original < core_start - tolerance


def set_core_startup_boundary(timestamp: float | None = None) -> float:
    """Capture Core's process-start boundary before listeners become active."""

    global _core_startup_boundary
    value = _valid_positive_finite(timestamp if timestamp is not None else time.time())
    if value is None:
        raise ValueError("Core startup boundary must be a finite positive timestamp")
    _core_startup_boundary = value
    return value


def get_core_startup_boundary() -> float:
    return _core_startup_boundary


def _valid_positive_finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None
