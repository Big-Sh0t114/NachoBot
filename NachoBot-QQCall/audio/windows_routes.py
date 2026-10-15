"""Read-only Windows Core Audio endpoint-session route observation.

This module observes active WASAPI sessions for an explicitly supplied Host
process tree. It does not change endpoint policy, open audio streams, or read
sample data. The native COM implementation is deliberately private and runs on
one bounded worker thread per observation.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, Self

_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_PROCESSES = 128
_MAX_LOOPBACK_READERS = 16
_MAX_ENDPOINTS = 256
_MAX_SESSIONS_PER_ENDPOINT = 4_096
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_REPORT_BYTES = 64 * 1024
_MAX_DEVICE_NAME_CHARS = 256
_FILETIME_MAX = (1 << 64) - 1
_WORKER_TIMEOUT_SECONDS = 1.25
_FILETIME_RE = re.compile(r"^[0-9]{1,20}$")
_PROCESS_CREATION_EPOCH_DELTA = 116_444_736_000_000_000
_PROCESS_CREATION_TICKS_PER_SECOND = 10_000_000
_SESSION_STATE_NAMES = {0: "inactive", 1: "active", 2: "expired"}
# AUDCLNT_SUCCESS(0x00d) == MAKE_SCODE(SEVERITY_SUCCESS, 0x889, 0x00d).
_AUDCLNT_S_NO_SINGLE_PROCESS = 0x0889000D
# IAudioSessionControl2 inherits the 12 IUnknown + IAudioSessionControl
# entries before declaring its own methods.
_SESSION_CONTROL2_GET_SESSION_INSTANCE_IDENTIFIER_SLOT = 13
_SESSION_CONTROL2_GET_PROCESS_ID_SLOT = 14

_WORKER_LOCK = threading.Lock()

ErrorCode = Literal[
    "request_invalid",
    "platform_unsupported",
    "observer_busy",
    "observer_timeout",
    "com_initialization_failed",
    "observer_unavailable",
    "target_process_unavailable",
    "target_process_stale",
    "endpoint_not_found",
    "endpoint_ambiguous",
    "endpoint_metadata_unavailable",
    "session_enumeration_failed",
    "session_attribution_unavailable",
    "session_state_unavailable",
    "session_identity_unavailable",
    "shared_session_unverified",
    "endpoint_session_conflict",
    "routes_not_distinct",
    "host_input_not_active",
    "host_output_not_active",
]


class _ObserverFailure(RuntimeError):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class EndpointInventoryError(RuntimeError):
    """Sanitized failure from bounded active-endpoint metadata inventory."""

    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code) else "observer_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _ProcessTarget:
    role: Literal["host", "child"]
    pid: int
    creation_time_100ns: int


@dataclass(frozen=True, slots=True)
class _LoopbackReaderTarget:
    pid: int
    creation_time_100ns: int


@dataclass(frozen=True, slots=True)
class _Expectation:
    key: Literal["host_input", "host_output"]
    direction: Literal["capture", "render"]
    endpoint_name: str


@dataclass(frozen=True, slots=True)
class _Request:
    host_generation: int
    host_pid: int
    processes: tuple[_ProcessTarget, ...]
    expectations: tuple[_Expectation, ...]
    loopback_readers: tuple[_LoopbackReaderTarget, ...] = ()
    discover_children: bool = False


@dataclass(frozen=True, slots=True)
class _RawSession:
    pid: int | None
    attribution: Literal["single", "shared", "unavailable"]
    state: str | None
    instance_id: str | None
    error: ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class _RawEndpoint:
    direction: Literal["capture", "render"]
    friendly_name: str
    endpoint_id: str | None
    sessions: tuple[_RawSession, ...]
    error: ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class _RawSnapshot:
    endpoints: tuple[_RawEndpoint, ...]
    process_times_before: dict[int, int]
    process_times_after: dict[int, int]
    exited_pids: frozenset[int] = frozenset()
    discovered_processes: tuple[_ProcessTarget, ...] = ()


class _SnapshotReader(Protocol):
    def read(self, request: _Request) -> _RawSnapshot: ...


def observe_windows_routes(
    request_data: Any,
    *,
    reader: _SnapshotReader | None = None,
    timeout_seconds: float = _WORKER_TIMEOUT_SECONDS,
    include_route_diagnostics: bool = False,
) -> dict[str, Any]:
    """Observe the two explicit Host routes and return only redacted metadata.

    ``creationTime100ns`` is a decimal string because FILETIME values exceed
    JavaScript's exact integer range. Tests may inject a snapshot reader; normal
    callers use the Windows COM reader.
    """
    observed_at_ms = time.time_ns() // 1_000_000
    try:
        request = _validate_request(request_data)
    except _ObserverFailure as exc:
        return _empty_report(observed_at_ms, request_data, [exc.code])

    if reader is None and os.name != "nt":
        return _empty_report(observed_at_ms, request, ["platform_unsupported"])
    if not _valid_timeout(timeout_seconds):
        return _empty_report(observed_at_ms, request, ["request_invalid"])
    if not _WORKER_LOCK.acquire(blocking=False):
        return _empty_report(observed_at_ms, request, ["observer_busy"])

    selected_reader = reader if reader is not None else _WindowsCoreAudioReader()
    result_box: list[_RawSnapshot | ErrorCode] = []

    def read_on_worker() -> None:
        try:
            result_box.append(selected_reader.read(request))
        except _ObserverFailure as exc:
            result_box.append(exc.code)
        except Exception:  # noqa: BLE001 - sanitize all worker failures at the privacy boundary.
            # Native HRESULTs, exception text, and device paths never enter the report.
            result_box.append("observer_unavailable")
        finally:
            _WORKER_LOCK.release()

    try:
        worker = threading.Thread(target=read_on_worker, name="qqcall-route-observer", daemon=True)
        worker.start()
    except (RuntimeError, MemoryError):
        _WORKER_LOCK.release()
        return _empty_report(observed_at_ms, request, ["observer_unavailable"])

    worker.join(timeout_seconds)
    if worker.is_alive():
        # The worker retains the lock and all COM references until its own finally
        # blocks release them; callers cannot stack additional stuck observers.
        return _empty_report(observed_at_ms, request, ["observer_timeout"])
    if not result_box:
        return _empty_report(observed_at_ms, request, ["observer_unavailable"])
    raw_snapshot = result_box[0]
    if isinstance(raw_snapshot, str):
        return _empty_report(observed_at_ms, request, [raw_snapshot])
    if not isinstance(raw_snapshot, _RawSnapshot):
        return _empty_report(observed_at_ms, request, ["observer_unavailable"])
    return _build_report(
        request,
        raw_snapshot,
        observed_at_ms,
        include_route_diagnostics=include_route_diagnostics is True,
    )


def enumerate_active_endpoints(
    *,
    reader: Callable[[], Any] | None = None,
    timeout_seconds: float = _WORKER_TIMEOUT_SECONDS,
) -> list[dict[str, str]]:
    """Return active endpoint IDs, friendly names, and flows without opening audio.

    The optional reader is an offline test seam. The production reader uses only
    IMMDeviceEnumerator metadata calls, runs on a bounded MTA worker, and never
    asks Windows for a default endpoint or activates an audio client.
    """
    if not _valid_timeout(timeout_seconds):
        raise EndpointInventoryError("request_invalid")
    if reader is None and os.name != "nt":
        raise EndpointInventoryError("platform_unsupported")
    if not _WORKER_LOCK.acquire(blocking=False):
        raise EndpointInventoryError("observer_busy")

    selected_reader = reader if reader is not None else _WindowsEndpointInventoryReader().read
    result_box: list[Any] = []

    def read_on_worker() -> None:
        try:
            result_box.append(selected_reader())
        except _ObserverFailure as exc:
            result_box.append(exc.code)
        except Exception:  # noqa: BLE001 - sanitize all COM/test-reader failures.
            result_box.append("observer_unavailable")
        finally:
            _WORKER_LOCK.release()

    try:
        worker = threading.Thread(target=read_on_worker, name="qqcall-endpoint-inventory", daemon=True)
        worker.start()
    except (RuntimeError, MemoryError):
        _WORKER_LOCK.release()
        raise EndpointInventoryError("observer_unavailable") from None
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise EndpointInventoryError("observer_timeout")
    if not result_box:
        raise EndpointInventoryError("observer_unavailable")
    result = result_box[0]
    if isinstance(result, str):
        raise EndpointInventoryError(result)
    if not isinstance(result, (list, tuple)) or not 1 <= len(result) <= _MAX_ENDPOINTS * 2:
        raise EndpointInventoryError("endpoint_metadata_unavailable")

    endpoints: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for endpoint in result:
        if not isinstance(endpoint, dict) or set(endpoint) != {"id", "name", "flow"}:
            raise EndpointInventoryError("endpoint_metadata_unavailable")
        endpoint_id = endpoint["id"]
        name = endpoint["name"]
        flow = endpoint["flow"]
        if (
            type(endpoint_id) is not str or not endpoint_id or len(endpoint_id) > 1024
            or any(ord(character) < 0x20 for character in endpoint_id)
            or type(name) is not str or not name or len(name) > _MAX_DEVICE_NAME_CHARS
            or any(ord(character) < 0x20 for character in name)
            or type(flow) is not str or flow not in ("capture", "render")
        ):
            raise EndpointInventoryError("endpoint_metadata_unavailable")
        folded_id = endpoint_id.casefold()
        if folded_id in seen_ids:
            raise EndpointInventoryError("endpoint_metadata_ambiguous")
        seen_ids.add(folded_id)
        endpoints.append({"id": endpoint_id, "name": name, "flow": flow})
    return endpoints


def read_request_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a bounded JSON request and reject duplicate object keys."""
    try:
        with Path(path).open("rb") as request_file:
            raw_bytes = request_file.read(_MAX_REQUEST_BYTES + 1)
        if len(raw_bytes) > _MAX_REQUEST_BYTES:
            raise _ObserverFailure("request_invalid")
        raw = raw_bytes.decode("utf-8")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except _ObserverFailure:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise _ObserverFailure("request_invalid") from None
    if not isinstance(value, dict):
        raise _ObserverFailure("request_invalid")
    return value


def write_report_file(path: str | os.PathLike[str], report: dict[str, Any]) -> None:
    """Atomically save a metadata-only report to the caller-selected path."""
    destination = Path(path)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _ObserverFailure("observer_unavailable") from None


def serve_stdio(input_stream: Any = None, output_stream: Any = None) -> int:
    """Serial metadata observations over inherited pipes; EOF ends the worker.

    A timed-out native reader remains quarantined until process exit. No next
    request may stack a second COM reader on top of it.
    """
    source = sys.stdin.buffer if input_stream is None else input_stream
    destination = sys.stdout if output_stream is None else output_stream
    while True:
        line = source.readline(_MAX_REQUEST_BYTES + 1)
        if not line:
            return 0
        if len(line) > _MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            return 2
        try:
            envelope = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
            if (not isinstance(envelope, dict) or set(envelope) != {"id", "request"}
                    or not isinstance(envelope["id"], str)
                    or re.fullmatch(r"[0-9a-f]{32}", envelope["id"]) is None):
                return 2
            report = observe_windows_routes(envelope["request"], include_route_diagnostics=True)
            encoded = json.dumps({"id": envelope["id"], "report": report}, separators=(",", ":"))
            if len(encoded.encode("utf-8")) + 1 > _MAX_REPORT_BYTES:
                return 2
            destination.write(encoded + "\n")
            destination.flush()
            if "observer_timeout" in report.get("errors", []):
                return 2
        except (OSError, UnicodeError, ValueError, TypeError, _ObserverFailure):
            return 2


def main(argv: list[str] | None = None) -> int:
    entered_at_ms = time.time_ns() // 1_000_000
    entered_at = time.monotonic()
    parser = argparse.ArgumentParser(description="Observe explicit Windows QQ Host audio sessions (metadata only).")
    parser.add_argument("--request-file", help="Private schema-1 JSON request file")
    parser.add_argument("--report", help="Destination for redacted JSON metadata")
    parser.add_argument("--stdio", action="store_true", help="Serve serialized observations over inherited pipes")
    parser.add_argument(
        "--include-route-diagnostics",
        action="store_true",
        help="Include bounded private per-route counters from the same snapshot",
    )
    arguments = parser.parse_args(argv)
    if arguments.stdio:
        if arguments.request_file or arguments.report or arguments.include_route_diagnostics:
            parser.error("--stdio cannot be combined with file options")
        return serve_stdio()
    if not arguments.request_file or not arguments.report:
        parser.error("--request-file and --report are required without --stdio")
    request_age_ms = -1
    observation_started = time.monotonic()
    try:
        request = read_request_file(arguments.request_file)
        if arguments.include_route_diagnostics:
            try:
                request_age_ms = max(-1, min(60_000, entered_at_ms -
                    Path(arguments.request_file).stat().st_mtime_ns // 1_000_000))
            except OSError:
                pass
        observation_started = time.monotonic()
        report = observe_windows_routes(
            request,
            include_route_diagnostics=arguments.include_route_diagnostics,
        )
    except _ObserverFailure as exc:
        report = _empty_report(time.time_ns() // 1_000_000, {}, [exc.code])
    observation_elapsed_ms = round((time.monotonic() - observation_started) * 1000)
    report_started = time.monotonic()
    try:
        write_report_file(arguments.report, report)
    except _ObserverFailure:
        print('{"status":"report_write_failed","verified":false}', file=sys.stdout)
        return 2
    if arguments.include_route_diagnostics:
        # One bounded, metadata-only diagnostic beside the already private
        # reports. It is never read as a proof and cannot change authorization.
        # Keep the strict report/stdout wire schema unchanged for older Hosts.
        timing = {
            "schema": 1,
            "helperEnteredAtMs": entered_at_ms,
            "requestAgeAtEntryMs": request_age_ms,
            "observationElapsedMs": observation_elapsed_ms,
            "reportWriteElapsedMs": round((time.monotonic() - report_started) * 1000),
            "helperElapsedMs": round((time.monotonic() - entered_at) * 1000),
        }
        try:
            write_report_file(Path(arguments.report).with_name("audio-route-timing.private.json"), timing)
        except _ObserverFailure:
            pass  # Diagnostics must not change the independently verified result.
    summary_status = "verified" if report["verified"] else "unverified"
    print(json.dumps({"status": summary_status, "verified": report["verified"]}, separators=(",", ":")))
    return 0


def _validate_request(value: Any) -> _Request:
    base_fields = {"schema", "hostGeneration", "hostPid", "processes", "expectations"}
    if not isinstance(value, dict) or not base_fields <= set(value) or set(value) - base_fields - {"loopbackReaders", "discoverChildren"}:
        raise _ObserverFailure("request_invalid")
    if "discoverChildren" in value and value["discoverChildren"] is not True:
        raise _ObserverFailure("request_invalid")
    if value.get("schema") != 1 or type(value.get("schema")) is not int:
        raise _ObserverFailure("request_invalid")
    host_generation = value.get("hostGeneration")
    host_pid = value.get("hostPid")
    if not _safe_positive_integer(host_generation) or not _valid_pid(host_pid):
        raise _ObserverFailure("request_invalid")
    process_values = value.get("processes")
    expectations_values = value.get("expectations")
    if not isinstance(process_values, list) or not 1 <= len(process_values) <= _MAX_PROCESSES:
        raise _ObserverFailure("request_invalid")
    if not isinstance(expectations_values, list) or len(expectations_values) != 2:
        raise _ObserverFailure("request_invalid")

    processes: list[_ProcessTarget] = []
    seen_pids: set[int] = set()
    host_targets: list[_ProcessTarget] = []
    for item in process_values:
        if not isinstance(item, dict) or set(item) != {"role", "pid", "creationTime100ns"}:
            raise _ObserverFailure("request_invalid")
        role, pid = item.get("role"), item.get("pid")
        creation_value = item.get("creationTime100ns")
        if role not in ("host", "child") or not _valid_pid(pid) or pid in seen_pids:
            raise _ObserverFailure("request_invalid")
        creation_time = _parse_filetime(creation_value)
        target = _ProcessTarget(role, pid, creation_time)
        processes.append(target)
        seen_pids.add(pid)
        if role == "host":
            host_targets.append(target)
    if len(host_targets) != 1 or host_targets[0].pid != host_pid:
        raise _ObserverFailure("request_invalid")

    reader_values = value.get("loopbackReaders", [])
    if (
        not isinstance(reader_values, list)
        or ("loopbackReaders" in value and not 1 <= len(reader_values) <= _MAX_LOOPBACK_READERS)
        or len(process_values) + len(reader_values) > _MAX_PROCESSES
    ):
        raise _ObserverFailure("request_invalid")
    loopback_readers: list[_LoopbackReaderTarget] = []
    for item in reader_values:
        if not isinstance(item, dict) or set(item) != {"pid", "creationTime100ns"}:
            raise _ObserverFailure("request_invalid")
        pid = item.get("pid")
        if not _valid_pid(pid) or pid in seen_pids:
            raise _ObserverFailure("request_invalid")
        loopback_readers.append(_LoopbackReaderTarget(pid, _parse_filetime(item.get("creationTime100ns"))))
        seen_pids.add(pid)

    expectations: list[_Expectation] = []
    seen_keys: set[str] = set()
    for item in expectations_values:
        if not isinstance(item, dict) or set(item) != {"key", "direction", "endpointName"}:
            raise _ObserverFailure("request_invalid")
        key, direction, name = item.get("key"), item.get("direction"), item.get("endpointName")
        if key not in ("host_input", "host_output") or key in seen_keys:
            raise _ObserverFailure("request_invalid")
        required_direction = "capture" if key == "host_input" else "render"
        if direction != required_direction or not _valid_endpoint_name(name):
            raise _ObserverFailure("request_invalid")
        expectations.append(_Expectation(key, direction, name))
        seen_keys.add(key)
    if seen_keys != {"host_input", "host_output"}:
        raise _ObserverFailure("request_invalid")
    return _Request(
        host_generation, host_pid, tuple(processes), tuple(expectations), tuple(loopback_readers), value.get("discoverChildren", False),
    )


def _new_route_diagnostics(
    request: _Request,
    observed_at_ms: int,
    *,
    process_snapshot: Literal["stable", "stale", "invalid_target_set", "unobserved"],
    stale_process_count: int = 0,
) -> dict[str, Any]:
    def new_route(key: str) -> dict[str, Any]:
        return {
            "key": key,
            "endpointMatches": 0,
            "endpointStatus": "not_observed",
            "endpointIdPresent": False,
            "endpointError": None,
            "sessionCount": 0,
            "stateCounts": {"active": 0, "inactive": 0, "expired": 0, "unknown": 0},
            "attributionCounts": {"single": 0, "shared": 0, "unavailable": 0},
            "targetCounts": {"host": 0, "child": 0, "pinnedLoopback": 0, "foreign": 0, "unclassified": 0},
            "identityQueried": 0,
            "identityAvailable": 0,
            "identityMissing": 0,
            "identityNotQueried": 0,
            "qualifyingSessions": 0,
            "active": False,
        }

    return {
        "schema": 1,
        "observedAtMs": observed_at_ms,
        "processSnapshot": process_snapshot,
        "staleProcessCount": stale_process_count,
        "routes": [new_route(item.key) for item in sorted(request.expectations, key=lambda item: item.key)],
    }


def _attach_route_diagnostics(report: dict[str, Any], diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    if diagnostics is None:
        return report
    try:
        encoded_diagnostics = json.dumps(diagnostics, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(encoded_diagnostics) > _MAX_REPORT_BYTES:
            return report
        report["routeDiagnostics"] = diagnostics
        encoded_report = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded_report) > _MAX_REPORT_BYTES:
            report.pop("routeDiagnostics", None)
    except Exception:  # noqa: BLE001 - optional diagnostics must never affect the proof.
        report.pop("routeDiagnostics", None)
    return report


def _build_report(
    request: _Request,
    snapshot: _RawSnapshot,
    observed_at_ms: int,
    *,
    include_route_diagnostics: bool = False,
) -> dict[str, Any]:
    discovered = snapshot.discovered_processes if request.discover_children else ()
    original_pids = {target.pid for target in (*request.processes, *request.loopback_readers)}
    root_time = next(target.creation_time_100ns for target in request.processes if target.role == "host")
    invalid_target_set = len(original_pids) + len(discovered) > _MAX_PROCESSES or any(
        target.role != "child" or target.pid in original_pids or target.creation_time_100ns < root_time
        for target in discovered
    ) or len({target.pid for target in discovered}) != len(discovered)
    if invalid_target_set:
        report = _empty_report(observed_at_ms, request, ["target_process_stale"])
        if include_route_diagnostics:
            try:
                diagnostics = _new_route_diagnostics(
                    request, observed_at_ms, process_snapshot="invalid_target_set",
                )
            except Exception:  # noqa: BLE001 - diagnostic construction is optional.
                diagnostics = None
            return _attach_route_diagnostics(report, diagnostics)
        return report
    process_targets = {target.pid: target for target in (*request.processes, *discovered)}
    reader_targets = {target.pid: target for target in request.loopback_readers}
    identity_pids = {target.pid for target in (*request.processes, *discovered)}
    all_targets = (*request.processes, *discovered, *request.loopback_readers)
    stale_process_count = sum(
        1
        for target in all_targets
        if (
            snapshot.process_times_before.get(target.pid) != target.creation_time_100ns
            or snapshot.process_times_after.get(target.pid) != target.creation_time_100ns
            or target.pid in snapshot.exited_pids
        )
    )
    if stale_process_count:
        report = _empty_report(observed_at_ms, request, ["target_process_stale"])
        if include_route_diagnostics:
            try:
                diagnostics = _new_route_diagnostics(
                    request,
                    observed_at_ms,
                    process_snapshot="stale",
                    stale_process_count=stale_process_count,
                )
            except Exception:  # noqa: BLE001 - diagnostic construction is optional.
                diagnostics = None
            return _attach_route_diagnostics(report, diagnostics)
        return report

    route_diagnostics: dict[str, Any] | None = None
    diagnostics_by_key: dict[str, dict[str, Any]] = {}
    if include_route_diagnostics:
        try:
            route_diagnostics = _new_route_diagnostics(
                request, observed_at_ms, process_snapshot="stable",
            )
            diagnostics_by_key = {item["key"]: item for item in route_diagnostics["routes"]}
        except Exception:  # noqa: BLE001 - optional diagnostics must never affect routing.
            route_diagnostics = None
            diagnostics_by_key = {}

    route_records: list[dict[str, Any]] = []
    route_raw_endpoints: dict[str, _RawEndpoint] = {}
    errors: set[ErrorCode] = set()
    observed_at_utc = _utc_from_epoch_ms(observed_at_ms)

    for expectation in sorted(request.expectations, key=lambda item: item.key):
        matches = [
            endpoint for endpoint in snapshot.endpoints
            if endpoint.direction == expectation.direction and endpoint.friendly_name == expectation.endpoint_name
        ]
        route = {
            "key": expectation.key,
            "direction": expectation.direction,
            "endpointName": expectation.endpoint_name,
            "endpointIdSha256": None,
            "active": False,
            "sessions": [],
        }
        route_records.append(route)
        diagnostic = diagnostics_by_key.get(expectation.key)
        if diagnostic is not None:
            try:
                diagnostic["endpointMatches"] = min(len(matches), _MAX_ENDPOINTS)
                if not matches:
                    diagnostic["endpointStatus"] = "not_found"
                elif len(matches) != 1:
                    diagnostic["endpointStatus"] = "ambiguous"
                else:
                    endpoint_match = matches[0]
                    diagnostic["endpointIdPresent"] = endpoint_match.endpoint_id is not None
                    endpoint_error = endpoint_match.error
                    diagnostic["endpointError"] = (
                        endpoint_error if isinstance(endpoint_error, str) and endpoint_error in ErrorCode.__args__ else None
                    )
                    diagnostic["endpointStatus"] = (
                        "unique"
                        if endpoint_error is None and endpoint_match.endpoint_id is not None
                        else "metadata_error"
                    )
            except Exception:  # noqa: BLE001 - drop broken diagnostics only.
                route_diagnostics = None
                diagnostics_by_key = {}
                diagnostic = None
        if not matches:
            errors.add("endpoint_not_found")
            continue
        if len(matches) != 1:
            errors.add("endpoint_ambiguous")
            continue
        endpoint = matches[0]
        route_raw_endpoints[expectation.key] = endpoint
        if endpoint.error is not None:
            errors.add(endpoint.error)
        if endpoint.endpoint_id is None:
            errors.add("endpoint_metadata_unavailable")
        else:
            route["endpointIdSha256"] = _hash_identifier("mmdevice-id", endpoint.endpoint_id)

        verified_sessions = 0
        for session in endpoint.sessions:
            if diagnostic is not None:
                try:
                    diagnostic["sessionCount"] += 1
                    state_key = session.state if session.state in {"active", "inactive", "expired"} else "unknown"
                    diagnostic["stateCounts"][state_key] += 1
                    attribution_key = session.attribution if session.attribution in {"single", "shared", "unavailable"} else "unavailable"
                    diagnostic["attributionCounts"][attribution_key] += 1
                    session_pid = session.pid if type(session.pid) is int and session.pid > 0 else None
                    session_target = process_targets.get(session_pid) if session_pid is not None else None
                    if session.attribution == "single" and session_pid is not None:
                        if session_target is not None:
                            diagnostic["targetCounts"][session_target.role] += 1
                        elif session_pid in reader_targets:
                            diagnostic["targetCounts"]["pinnedLoopback"] += 1
                        else:
                            diagnostic["targetCounts"]["foreign"] += 1
                    else:
                        diagnostic["targetCounts"]["unclassified"] += 1
                    identity_was_queried = (
                        session.state == "active"
                        and session.attribution == "single"
                        and session_pid in identity_pids
                    )
                    if identity_was_queried:
                        diagnostic["identityQueried"] += 1
                        if session.instance_id is None:
                            diagnostic["identityMissing"] += 1
                        else:
                            diagnostic["identityAvailable"] += 1
                    else:
                        diagnostic["identityNotQueried"] += 1
                except Exception:  # noqa: BLE001 - diagnostics cannot affect the existing decision path.
                    route_diagnostics = None
                    diagnostics_by_key = {}
                    diagnostic = None
            if session.state in ("inactive", "expired"):
                continue
            if session.state != "active":
                if session.error is not None:
                    errors.add(session.error)
                errors.add("session_state_unavailable")
                continue
            if session.attribution == "shared":
                if session.error is not None:
                    errors.add(session.error)
                errors.add("shared_session_unverified")
                continue
            if session.attribution != "single" or session.pid is None:
                if session.error is not None:
                    errors.add(session.error)
                errors.add("session_attribution_unavailable")
                continue
            target = process_targets.get(session.pid)
            if target is None:
                if session.pid in reader_targets:
                    if expectation.key != "host_output":
                        errors.add("endpoint_session_conflict")
                    # A pinned loopback reader is exempt only on the expected
                    # render endpoint and never counts as a Host route session.
                    continue
                if expectation.key == "host_output":
                    errors.add("endpoint_session_conflict")
                continue
            if session.error is not None:
                errors.add(session.error)
                continue
            if session.instance_id is None:
                errors.add("session_identity_unavailable")
                continue
            session_hash = _hash_identifier("audio-session-instance", session.instance_id)
            record = {
                "role": target.role,
                "pid": target.pid,
                "processGeneration": str(target.creation_time_100ns),
                "processStartTimeUtc": _utc_from_filetime(target.creation_time_100ns),
                "sessionGenerationSha256": session_hash,
                "firstObservedUtc": observed_at_utc,
                "state": session.state,
                "processAttribution": session.attribution,
            }
            route["sessions"].append(record)
            verified_sessions += 1
            if diagnostic is not None:
                try:
                    diagnostic["qualifyingSessions"] += 1
                except Exception:  # noqa: BLE001 - drop broken diagnostics only.
                    route_diagnostics = None
                    diagnostics_by_key = {}
                    diagnostic = None
        route["active"] = verified_sessions > 0 and endpoint.endpoint_id is not None and endpoint.error is None
        if diagnostic is not None:
            try:
                diagnostic["active"] = route["active"]
            except Exception:  # noqa: BLE001 - drop broken diagnostics only.
                route_diagnostics = None
                diagnostics_by_key = {}
                diagnostic = None
        if not route["active"]:
            errors.add("host_input_not_active" if expectation.key == "host_input" else "host_output_not_active")

    capture_endpoint = route_raw_endpoints.get("host_input")
    render_endpoint = route_raw_endpoints.get("host_output")
    routes_distinct = (
        capture_endpoint is not None
        and render_endpoint is not None
        and capture_endpoint.endpoint_id is not None
        and render_endpoint.endpoint_id is not None
        and capture_endpoint.endpoint_id != render_endpoint.endpoint_id
    )
    if capture_endpoint is not None and render_endpoint is not None and not routes_distinct:
        errors.add("routes_not_distinct")

    report = {
        "schema": 1,
        "source": "windows_active_sessions",
        "observedAtMs": observed_at_ms,
        "verified": (
            all(route["active"] and len(route["sessions"]) == 1 for route in route_records)
            and routes_distinct
            and not errors
        ),
        "hostGeneration": request.host_generation,
        "hostPid": request.host_pid,
        "routes": route_records,
        "errors": sorted(errors),
    }
    if request.discover_children:
        report["discoveredProcesses"] = [{"role": target.role, "pid": target.pid,
            "creationTime100ns": str(target.creation_time_100ns)} for target in discovered]
    return _attach_route_diagnostics(report, route_diagnostics)


def _empty_report(observed_at_ms: int, request_data: Any, errors: list[str]) -> dict[str, Any]:
    host_generation: int | None = None
    host_pid: int | None = None
    if isinstance(request_data, _Request):
        host_generation = request_data.host_generation
        host_pid = request_data.host_pid
    elif isinstance(request_data, dict):
        candidate_generation = request_data.get("hostGeneration")
        candidate_pid = request_data.get("hostPid")
        host_generation = candidate_generation if _safe_positive_integer(candidate_generation) else None
        host_pid = candidate_pid if _valid_pid(candidate_pid) else None
    routes = []
    if isinstance(request_data, _Request):
        routes = [
            {
                "key": expectation.key,
                "direction": expectation.direction,
                "endpointName": expectation.endpoint_name,
                "endpointIdSha256": None,
                "active": False,
                "sessions": [],
            }
            for expectation in sorted(request_data.expectations, key=lambda item: item.key)
        ]
    return {
        "schema": 1,
        "source": "windows_active_sessions",
        "observedAtMs": observed_at_ms,
        "verified": False,
        "hostGeneration": host_generation,
        "hostPid": host_pid,
        "routes": routes,
        "errors": sorted(set(errors)),
    }


def _safe_positive_integer(value: Any) -> bool:
    return type(value) is int and 0 < value <= _MAX_SAFE_INTEGER


def _valid_pid(value: Any) -> bool:
    return type(value) is int and 0 < value <= 0xFFFFFFFF


def _parse_filetime(value: Any) -> int:
    if not isinstance(value, str) or _FILETIME_RE.fullmatch(value) is None:
        raise _ObserverFailure("request_invalid")
    try:
        parsed = int(value, 10)
    except ValueError:
        raise _ObserverFailure("request_invalid") from None
    if parsed <= 0 or parsed > _FILETIME_MAX or str(parsed) != value:
        raise _ObserverFailure("request_invalid")
    return parsed


def _valid_endpoint_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_DEVICE_NAME_CHARS
        and value == value.strip()
        and not any(ord(character) < 32 for character in value)
    )


def _valid_timeout(value: Any) -> bool:
    return type(value) in (float, int) and 0.01 <= float(value) <= 15.0


def _hash_identifier(kind: str, value: str) -> str:
    return hashlib.sha256(f"qqcall-{kind}-v1\0".encode("ascii") + value.encode("utf-16-le")).hexdigest()


def _session_attribution_from_hresult(hr: int) -> Literal["single", "shared", "unavailable"]:
    if hr == 0:
        return "single"
    if hr == _AUDCLNT_S_NO_SINGLE_PROCESS:
        return "shared"
    return "unavailable"


def _session_state_from_hresult(hr: int, state_value: int) -> str | None:
    # GetState documents S_OK on success; unknown positive HRESULTs are not proof.
    if hr != 0:
        return None
    return _SESSION_STATE_NAMES.get(state_value)


def _utc_from_filetime(value: int) -> str:
    microseconds = (value - _PROCESS_CREATION_EPOCH_DELTA) // 10
    try:
        return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, ValueError):
        return ""


def _utc_from_epoch_ms(value: int) -> str:
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class _WindowsCoreAudioReader:
    """ctypes Core Audio reader. Every COM object remains on this caller thread."""

    def read(self, request: _Request) -> _RawSnapshot:
        if os.name != "nt":
            raise _ObserverFailure("platform_unsupported")
        ole32, kernel32 = _load_windows_apis()
        coinit_hr = int(ole32.CoInitializeEx(None, 0))  # COINIT_MULTITHREADED
        if coinit_hr < 0:
            raise _ObserverFailure("com_initialization_failed")
        try:
            return self._read_on_mta(request, ole32, kernel32)
        finally:
            ole32.CoUninitialize()

    def _read_on_mta(self, request: _Request, ole32: Any, kernel32: Any) -> _RawSnapshot:
        with ExitStack() as stack:
            all_targets = (*request.processes, *request.loopback_readers)
            process_handles = {
                target.pid: _open_process_handle(kernel32, target.pid, stack)
                for target in all_targets
            }
            before, exited_before = _read_process_times(kernel32, process_handles)
            _assert_expected_process_times(request, before, exited_before)
            discovered: list[_ProcessTarget] = []
            parents_before = _read_process_parents(kernel32) if request.discover_children else {}
            if request.discover_children:
                root_image = _read_process_image(kernel32, process_handles[request.host_pid])
                if os.path.basename(root_image).casefold() != "qq.exe":
                    raise _ObserverFailure("target_process_stale")
                candidates = _descendant_pids(parents_before, request.host_pid)
                if len(candidates) + len(all_targets) > _MAX_PROCESSES:
                    raise _ObserverFailure("request_invalid")
                for pid in sorted(candidates):
                    if pid in process_handles:
                        continue
                    handle = _open_process_handle(kernel32, pid, stack)
                    process_handles[pid] = handle
                before, exited_before = _read_process_times(kernel32, process_handles)
                _assert_expected_process_times(request, before, exited_before)
                # Windows retains a creator PID after its process exits. If
                # that PID is reused, an older surviving process is not a
                # child of this new incarnation. Prune that entire branch by
                # exact creation time before checking images or audio scope.
                descendants = _descendant_pids(parents_before, request.host_pid, before)
                if exited_before.intersection({request.host_pid, *descendants}):
                    raise _ObserverFailure("target_process_stale")
                for pid in sorted(descendants):
                    if _read_process_image(kernel32, process_handles[pid]).casefold() != root_image.casefold():
                        raise _ObserverFailure("target_process_stale")
                    parent = parents_before[pid]
                    if parent not in before or before[pid] < before[parent]:
                        raise _ObserverFailure("target_process_stale")
                    if not any(target.pid == pid for target in request.processes):
                        discovered.append(_ProcessTarget("child", pid, before[pid]))
            enumerator = stack.enter_context(_ComReference(_create_mmdevice_enumerator(ole32)))
            raw_endpoints: list[_RawEndpoint] = []
            target_pids = {target.pid for target in (*all_targets, *discovered)}
            session_identity_pids = {target.pid for target in (*request.processes, *discovered)}
            for expectation in request.expectations:
                raw_endpoints.extend(
                    _read_named_endpoint_sessions(
                        ole32, enumerator.pointer, expectation, target_pids, session_identity_pids,
                    )
                )
            after, exited_after = _read_process_times(kernel32, process_handles)
            _assert_expected_process_times(request, after, exited_after)
            if request.discover_children:
                parents_after = _read_process_parents(kernel32)
                if _descendant_pids(parents_after, request.host_pid, after) != descendants or any(
                       parents_after.get(pid) != parents_before.get(pid) or before.get(pid) != after.get(pid)
                       or pid in exited_after for pid in descendants):
                    raise _ObserverFailure("target_process_stale")
            return _RawSnapshot(
                endpoints=tuple(raw_endpoints),
                process_times_before=before,
                process_times_after=after,
                exited_pids=frozenset(exited_before | exited_after),
                discovered_processes=tuple(discovered),
            )


class _WindowsEndpointInventoryReader:
    """Metadata-only active endpoint enumeration on a dedicated COM MTA."""

    def read(self) -> list[dict[str, str]]:
        if os.name != "nt":
            raise _ObserverFailure("platform_unsupported")
        ole32, _kernel32 = _load_windows_apis()
        coinit_hr = int(ole32.CoInitializeEx(None, 0))  # COINIT_MULTITHREADED
        if coinit_hr < 0:
            raise _ObserverFailure("com_initialization_failed")
        try:
            with ExitStack() as stack:
                enumerator = stack.enter_context(_ComReference(_create_mmdevice_enumerator(ole32)))
                return _read_active_endpoint_inventory(ole32, enumerator.pointer)
        finally:
            ole32.CoUninitialize()


class _ComReference:
    def __init__(self, pointer: ctypes.c_void_p):
        if not pointer:
            raise _ObserverFailure("observer_unavailable")
        self.pointer = pointer

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        pointer = self.pointer
        self.pointer = ctypes.c_void_p()
        if pointer:
            _com_method(pointer, 2, ctypes.c_ulong, ())(pointer)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> _GUID:
        import uuid

        parsed = uuid.UUID(value)
        return cls(parsed.time_low, parsed.time_mid, parsed.time_hi_version, (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]))


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _CALPWSTR(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("values", ctypes.c_void_p)]


class _PROPVARIANT_VALUE(ctypes.Union):
    # ctypes field declarations are mutable class metadata by design.
    _fields_ = [("pointer", ctypes.c_void_p), ("unsigned64", ctypes.c_uint64), ("wide_string_array", _CALPWSTR)]  # noqa: RUF012


class _PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("reserved1", ctypes.c_uint16),
        ("reserved2", ctypes.c_uint16),
        ("reserved3", ctypes.c_uint16),
        ("value", _PROPVARIANT_VALUE),
    ]


def _load_windows_apis() -> tuple[Any, Any]:
    try:
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):
        raise _ObserverFailure("observer_unavailable") from None
    hresult = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = hresult
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = hresult
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PROPVARIANT)]
    ole32.PropVariantClear.restype = hresult
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return ole32, kernel32


def _com_method(
    interface: ctypes.c_void_p,
    index: int,
    restype: Any,
    argtypes: tuple[Any, ...],
) -> Any:
    convention = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[index]
    if not address:
        raise _ObserverFailure("observer_unavailable")
    return convention(restype, ctypes.c_void_p, *argtypes)(address)


def _check_hresult(value: int) -> int:
    if value != 0:
        raise _ObserverFailure("observer_unavailable")
    return value


def _com_out_interface(
    interface: ctypes.c_void_p,
    index: int,
    argtypes: tuple[Any, ...] = (),
    args: tuple[Any, ...] = (),
) -> ctypes.c_void_p:
    output = ctypes.c_void_p()
    method = _com_method(interface, index, ctypes.c_long, (*argtypes, ctypes.POINTER(ctypes.c_void_p)))
    _check_hresult(method(interface, *args, ctypes.byref(output)))
    if not output:
        raise _ObserverFailure("observer_unavailable")
    return output


def _create_mmdevice_enumerator(ole32: Any) -> ctypes.c_void_p:
    class_id = _GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
    interface_id = _GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
    output = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(class_id), None, 0x05, ctypes.byref(interface_id), ctypes.byref(output),
    )
    _check_hresult(int(hr))
    if not output:
        raise _ObserverFailure("observer_unavailable")
    return output


def _open_process_handle(kernel32: Any, pid: int, stack: ExitStack) -> ctypes.c_void_p:
    # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE; no memory-read access.
    handle = kernel32.OpenProcess(0x00100000 | 0x00001000, 0, pid)
    if not handle:
        raise _ObserverFailure("target_process_unavailable")
    stack.callback(kernel32.CloseHandle, handle)
    return ctypes.c_void_p(handle)


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def _descendant_pids(
    parents: dict[int, int], root: int, creation_times: dict[int, int] | None = None,
) -> set[int]:
    found: set[int] = set()
    frontier = {root}
    for _ in range(_MAX_PROCESSES):
        children = {pid for pid, parent in parents.items() if parent in frontier and pid != root and pid not in found}
        if creation_times is not None:
            if any(pid not in creation_times or parents[pid] not in creation_times for pid in children):
                raise _ObserverFailure("target_process_stale")
            children = {pid for pid in children if creation_times[pid] >= creation_times[parents[pid]]}
        if not children:
            return found
        found.update(children)
        if len(found) > _MAX_PROCESSES:
            raise _ObserverFailure("request_invalid")
        frontier = children
    raise _ObserverFailure("request_invalid")


def _read_process_image(kernel32: Any, handle: Any) -> str:
    function = kernel32.QueryFullProcessImageNameW
    function.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    function.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.c_uint32(len(buffer))
    if not function(handle, 0, buffer, ctypes.byref(size)) or not size.value:
        raise _ObserverFailure("target_process_stale")
    return buffer.value


def _read_process_parents(kernel32: Any) -> dict[int, int]:
    class Entry(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("usage", ctypes.c_uint32), ("pid", ctypes.c_uint32),
            ("heap", ctypes.c_size_t), ("module", ctypes.c_uint32), ("threads", ctypes.c_uint32),
            ("parent", ctypes.c_uint32), ("priority", ctypes.c_long), ("flags", ctypes.c_uint32),
            ("image", ctypes.c_wchar * 260)]
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel32, name)
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
        function.restype = ctypes.c_int
    snapshot = kernel32.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise _ObserverFailure("target_process_unavailable")
    try:
        entry = Entry(); entry.size = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise _ObserverFailure("target_process_unavailable")
        rows = {}
        while True:
            if len(rows) >= 16384:
                raise _ObserverFailure("request_invalid")
            rows[entry.pid] = entry.parent
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:
                    raise _ObserverFailure("target_process_unavailable")
                return rows
    finally:
        kernel32.CloseHandle(snapshot)


def _read_process_times(
    kernel32: Any,
    handles: dict[int, ctypes.c_void_p],
) -> tuple[dict[int, int], set[int]]:
    times: dict[int, int] = {}
    exited: set[int] = set()
    for pid, handle in handles.items():
        creation = _FILETIME()
        exit_time = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        ):
            raise _ObserverFailure("target_process_stale")
        times[pid] = (creation.high << 32) | creation.low
        wait_result = int(kernel32.WaitForSingleObject(handle, 0))
        if wait_result == 0:
            exited.add(pid)
        elif wait_result != 0x00000102:  # WAIT_TIMEOUT: process handle is still live.
            raise _ObserverFailure("target_process_stale")
    return times, exited


def _assert_expected_process_times(
    request: _Request,
    observed: dict[int, int],
    exited: set[int],
) -> None:
    for target in request.processes:
        if observed.get(target.pid) != target.creation_time_100ns or target.pid in exited:
            raise _ObserverFailure("target_process_stale")


def _read_named_endpoint_sessions(
    ole32: Any,
    enumerator: ctypes.c_void_p,
    expectation: _Expectation,
    target_pids: set[int],
    session_identity_pids: set[int] | None = None,
) -> list[_RawEndpoint]:
    flow = 0 if expectation.direction == "render" else 1  # eRender / eCapture
    collection = _com_out_interface(
        enumerator, 3,
        (ctypes.c_int, ctypes.c_uint32),
        (flow, 0x00000001),  # DEVICE_STATE_ACTIVE only.
    )
    matches: list[_RawEndpoint] = []
    with _ComReference(collection) as owned_collection:
        count = ctypes.c_uint32()
        get_count = _com_method(owned_collection.pointer, 3, ctypes.c_long, (ctypes.POINTER(ctypes.c_uint32),))
        _check_hresult(int(get_count(owned_collection.pointer, ctypes.byref(count))))
        if count.value > _MAX_ENDPOINTS:
            raise _ObserverFailure("observer_unavailable")
        for index in range(count.value):
            device_pointer = _com_out_interface(
                owned_collection.pointer, 4,
                (ctypes.c_uint32,), (index,),
            )
            with _ComReference(device_pointer) as device:
                state = ctypes.c_uint32()
                get_state = _com_method(device.pointer, 6, ctypes.c_long, (ctypes.POINTER(ctypes.c_uint32),))
                _check_hresult(int(get_state(device.pointer, ctypes.byref(state))))
                if not state.value & 0x00000001:
                    continue
                friendly_name = _read_friendly_name(ole32, device.pointer)
                if friendly_name != expectation.endpoint_name:
                    continue
                try:
                    endpoint_id = _read_endpoint_id(ole32, device.pointer)
                except _ObserverFailure:
                    matches.append(_RawEndpoint(
                        expectation.direction, friendly_name, None, (), "endpoint_metadata_unavailable",
                    ))
                    continue
                try:
                    sessions = tuple(_read_endpoint_sessions(
                        ole32, device.pointer, target_pids, session_identity_pids,
                    ))
                    matches.append(_RawEndpoint(expectation.direction, friendly_name, endpoint_id, sessions))
                except _ObserverFailure:
                    matches.append(_RawEndpoint(
                        expectation.direction, friendly_name, endpoint_id, (), "session_enumeration_failed",
                    ))
    return matches


def _read_active_endpoint_inventory(
    ole32: Any,
    enumerator: ctypes.c_void_p,
) -> list[dict[str, str]]:
    endpoints: list[dict[str, str]] = []
    for flow, flow_name in ((0, "render"), (1, "capture")):  # eRender / eCapture
        collection = _com_out_interface(
            enumerator, 3,
            (ctypes.c_int, ctypes.c_uint32),
            (flow, 0x00000001),  # DEVICE_STATE_ACTIVE only.
        )
        with _ComReference(collection) as owned_collection:
            count = ctypes.c_uint32()
            get_count = _com_method(
                owned_collection.pointer, 3, ctypes.c_long,
                (ctypes.POINTER(ctypes.c_uint32),),
            )
            _check_hresult(int(get_count(owned_collection.pointer, ctypes.byref(count))))
            if count.value > _MAX_ENDPOINTS:
                raise _ObserverFailure("observer_unavailable")
            for index in range(count.value):
                device_pointer = _com_out_interface(
                    owned_collection.pointer, 4,
                    (ctypes.c_uint32,), (index,),
                )
                with _ComReference(device_pointer) as device:
                    state = ctypes.c_uint32()
                    get_state = _com_method(
                        device.pointer, 6, ctypes.c_long,
                        (ctypes.POINTER(ctypes.c_uint32),),
                    )
                    _check_hresult(int(get_state(device.pointer, ctypes.byref(state))))
                    if not state.value & 0x00000001:
                        continue
                    name = _read_friendly_name(ole32, device.pointer)
                    endpoint_id = _read_endpoint_id(ole32, device.pointer)
                    endpoints.append({"id": endpoint_id, "name": name, "flow": flow_name})
    return endpoints


def _read_friendly_name(ole32: Any, device: ctypes.c_void_p) -> str:
    property_store = _com_out_interface(
        device, 4, (ctypes.c_uint32,), (0,),  # STGM_READ
    )
    with _ComReference(property_store) as store:
        property_key = _PROPERTYKEY(
            _GUID.parse("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14,
        )
        value = _PROPVARIANT()
        get_value = _com_method(
            store.pointer, 5,
            ctypes.c_long,
            (ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT)),
        )
        try:
            _check_hresult(int(get_value(store.pointer, ctypes.byref(property_key), ctypes.byref(value))))
            if value.vt != 31 or not value.pointer:  # VT_LPWSTR
                raise _ObserverFailure("endpoint_metadata_unavailable")
            return ctypes.wstring_at(value.pointer)
        finally:
            ole32.PropVariantClear(ctypes.byref(value))


def _read_endpoint_id(ole32: Any, device: ctypes.c_void_p) -> str:
    output = ctypes.c_void_p()
    get_id = _com_method(device, 5, ctypes.c_long, (ctypes.POINTER(ctypes.c_void_p),))
    result = int(get_id(device, ctypes.byref(output)))
    try:
        _check_hresult(result)
        if not output:
            raise _ObserverFailure("endpoint_metadata_unavailable")
        return ctypes.wstring_at(output)
    finally:
        if output:
            ole32.CoTaskMemFree(output)


def _read_endpoint_sessions(
    ole32: Any,
    device: ctypes.c_void_p,
    target_pids: set[int],
    session_identity_pids: set[int] | None = None,
) -> list[_RawSession]:
    manager_id = _GUID.parse("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
    manager_pointer = _com_out_interface(
        device, 3,
        (ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p),
        (ctypes.byref(manager_id), 0x05, None),  # CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER.
    )
    sessions: list[_RawSession] = []
    with _ComReference(manager_pointer) as manager:
        session_list_pointer = _com_out_interface(manager.pointer, 5)
        with _ComReference(session_list_pointer) as session_list:
            count = ctypes.c_int()
            get_count = _com_method(session_list.pointer, 3, ctypes.c_long, (ctypes.POINTER(ctypes.c_int),))
            _check_hresult(int(get_count(session_list.pointer, ctypes.byref(count))))
            if count.value < 0 or count.value > _MAX_SESSIONS_PER_ENDPOINT:
                raise _ObserverFailure("observer_unavailable")
            for index in range(count.value):
                control_pointer = _com_out_interface(
                    session_list.pointer, 4,
                    (ctypes.c_int,), (index,),
                )
                with _ComReference(control_pointer) as control:
                    sessions.append(_read_session(ole32, control.pointer, target_pids, session_identity_pids))
    return sessions


def _read_session(
    ole32: Any,
    control: ctypes.c_void_p,
    target_pids: set[int],
    session_identity_pids: set[int] | None = None,
) -> _RawSession:
    control2_id = _GUID.parse("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
    try:
        control2_pointer = _com_out_interface(
            control, 0, (ctypes.POINTER(_GUID),), (ctypes.byref(control2_id),),
        )
    except _ObserverFailure:
        return _RawSession(None, "unavailable", None, None, "session_attribution_unavailable")

    with _ComReference(control2_pointer) as control2:
        pid_value = ctypes.c_uint32()
        get_pid = _com_method(
            control2.pointer,
            _SESSION_CONTROL2_GET_PROCESS_ID_SLOT,
            ctypes.c_long,
            (ctypes.POINTER(ctypes.c_uint32),),
        )
        pid_result = int(get_pid(control2.pointer, ctypes.byref(pid_value)))
        attribution = _session_attribution_from_hresult(pid_result)
        pid: int | None = int(pid_value.value) if attribution != "unavailable" else None

        state_value = ctypes.c_int()
        get_state = _com_method(control2.pointer, 3, ctypes.c_long, (ctypes.POINTER(ctypes.c_int),))
        state_result = int(get_state(control2.pointer, ctypes.byref(state_value)))
        state = _session_state_from_hresult(state_result, state_value.value)
        error: ErrorCode | None = None
        if state is None:
            error = "session_state_unavailable"
        elif state == "active" and attribution == "unavailable":
            error = "session_attribution_unavailable"

        instance_id: str | None = None
        identity_pids = target_pids if session_identity_pids is None else session_identity_pids
        if state == "active" and attribution == "single" and pid in identity_pids:
            instance_id_pointer = ctypes.c_void_p()
            get_instance_id = _com_method(
                control2.pointer,
                _SESSION_CONTROL2_GET_SESSION_INSTANCE_IDENTIFIER_SLOT,
                ctypes.c_long,
                (ctypes.POINTER(ctypes.c_void_p),),
            )
            instance_result = int(get_instance_id(control2.pointer, ctypes.byref(instance_id_pointer)))
            if instance_result == 0 and instance_id_pointer:
                try:
                    instance_id = ctypes.wstring_at(instance_id_pointer)
                finally:
                    # GetSessionInstanceIdentifier allocates a CoTaskMem string.
                    ole32.CoTaskMemFree(instance_id_pointer)
            else:
                if instance_id_pointer:
                    ole32.CoTaskMemFree(instance_id_pointer)
                error = "session_identity_unavailable"
        return _RawSession(pid, attribution, state, instance_id, error)


if __name__ == "__main__":
    raise SystemExit(main())
