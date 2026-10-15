"""Small Uvicorn logging customizations shared by the local runtimes.

Uvicorn emits its access records at ``INFO`` regardless of whether a request
is a frequent readiness probe.  The filter below keeps ordinary access logs
unchanged while moving successful local health/capability probes out of the
normal console stream.  It is deliberately independent of Uvicorn internals
apart from the documented five-element access-record argument tuple.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any


QUIET_PROBE_PATHS = frozenset({"/health", "/api/health", "/v1/capabilities"})
_FILTER_MARKER = "_nachobot_quiet_access_filter"


def _access_request(record: logging.LogRecord) -> tuple[str, int] | None:
    """Return the path and status from a Uvicorn access record, if present."""

    args: Any = record.args
    if not isinstance(args, Sequence) or isinstance(args, (str, bytes)) or len(args) != 5:
        return None

    try:
        full_path = str(args[2])
        status_code = int(args[4])
    except (TypeError, ValueError):
        return None
    # Uvicorn's ``full_path`` includes the query string.  The health routes
    # themselves are stable; query parameters must not turn them into noisy
    # ordinary requests.
    path = full_path.split("?", 1)[0]
    return path, status_code


class QuietProbeAccessFilter(logging.Filter):
    """Demote successful health/capability access records to DEBUG.

    The filter is installed on ``uvicorn.access`` after Uvicorn has configured
    its handlers.  Re-emitting through ``uvicorn.error`` lets DEBUG-enabled
    diagnostics retain the equivalent access message without feeding the
    record back through the access logger.
    """

    _nachobot_quiet_access_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        request = _access_request(record)
        if request is None:
            return True

        path, status_code = request
        if path not in QUIET_PROBE_PATHS or status_code >= 400:
            return True

        error_logger = logging.getLogger("uvicorn.error")
        if error_logger is not logging.getLogger("uvicorn.access") and error_logger.isEnabledFor(logging.DEBUG):
            # Keep the original Uvicorn format string and its five arguments.
            # This produces the same access message for default/error or
            # custom handlers while avoiding recursive access filtering.
            args = record.args
            if isinstance(args, tuple):
                error_logger.log(logging.DEBUG, record.msg, *args)
            else:  # Defensive fallback for a sequence-like custom record.
                error_logger.log(logging.DEBUG, record.msg, *tuple(args))
        return False


def install_quiet_access_logging() -> QuietProbeAccessFilter:
    """Install the shared probe filter once on the configured access logger.

    ``uvicorn.Config`` configures the logging tree in its constructor, so
    callers must invoke this helper immediately after constructing ``Config``
    and before starting ``Server``.  The marker makes repeated installation
    harmless, including when multiple runtime setup paths share a process.
    """

    access_logger = logging.getLogger("uvicorn.access")
    for existing in access_logger.filters:
        if getattr(existing, _FILTER_MARKER, False):
            return existing  # type: ignore[return-value]

    access_filter = QuietProbeAccessFilter()
    access_logger.addFilter(access_filter)
    return access_filter


# Keep a descriptive alias for callers that prefer the noun form while
# retaining one implementation and one installation marker.
install_quiet_access_log_filter = install_quiet_access_logging


__all__ = [
    "QUIET_PROBE_PATHS",
    "QuietProbeAccessFilter",
    "install_quiet_access_logging",
    "install_quiet_access_log_filter",
]
