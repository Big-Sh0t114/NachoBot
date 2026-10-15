from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from windows_routes import (
    _AUDCLNT_S_NO_SINGLE_PROCESS,
    _CALPWSTR,
    _FILETIME,
    _GUID,
    _PROPERTYKEY,
    _PROPVARIANT,
    _SESSION_CONTROL2_GET_PROCESS_ID_SLOT,
    _SESSION_CONTROL2_GET_SESSION_INSTANCE_IDENTIFIER_SLOT,
    _SESSION_STATE_NAMES,
    _WORKER_LOCK,
    EndpointInventoryError,
    _build_report,
    _descendant_pids,
    enumerate_active_endpoints,
    _ProcessTarget,
    _WindowsCoreAudioReader,
    _ObserverFailure,
    _RawEndpoint,
    _RawSession,
    _RawSnapshot,
    _read_session,
    _read_active_endpoint_inventory,
    _Request,
    _session_attribution_from_hresult,
    _session_state_from_hresult,
    _validate_request,
    _utc_from_filetime,
    observe_windows_routes,
)

FILETIME_HOST = 134_000_000_000_000_000
FILETIME_CHILD = 134_000_000_100_000_000


def fake_session_control2(
    *, pid: int = 4101, pid_hresult: int = 0, state: int = 1, state_hresult: int = 0,
) -> tuple[ctypes.c_void_p, object, object, list[int]]:
    convention = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    interface_pointer: dict[str, ctypes.c_void_p] = {}
    instance_buffer = ctypes.create_unicode_buffer("session-instance")
    freed: list[int] = []

    def query_interface(_this, _iid, output):
        output[0] = interface_pointer["value"]
        return 0

    def add_ref(_this):
        return 1

    def release(_this):
        return 0

    def get_state(_this, output):
        output[0] = state
        return state_hresult

    def get_instance(_this, output):
        output[0] = ctypes.cast(instance_buffer, ctypes.c_void_p).value
        return 0

    def get_pid(_this, output):
        output[0] = pid
        return pid_hresult

    callbacks = [
        convention(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(query_interface),
        convention(ctypes.c_ulong, ctypes.c_void_p)(add_ref),
        convention(ctypes.c_ulong, ctypes.c_void_p)(release),
        convention(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))(get_state),
        convention(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(get_instance),
        convention(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))(get_pid),
    ]
    vtable = (ctypes.c_void_p * 17)()
    for slot, callback in zip((0, 1, 2, 3, 13, 14), callbacks, strict=True):
        vtable[slot] = ctypes.cast(callback, ctypes.c_void_p).value

    class Interface(ctypes.Structure):
        _fields_ = [("vtable", ctypes.POINTER(ctypes.c_void_p))]

    instance = Interface(ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p)))
    pointer = ctypes.cast(ctypes.pointer(instance), ctypes.c_void_p)
    interface_pointer["value"] = pointer
    ole32 = SimpleNamespace(CoTaskMemFree=lambda value: freed.append(int(value.value)))
    keeper = (instance, vtable, instance_buffer, callbacks)
    return pointer, keeper, ole32, freed


def request_data() -> dict[str, object]:
    return {
        "schema": 1,
        "hostGeneration": 23,
        "hostPid": 4100,
        "processes": [
            {"role": "host", "pid": 4100, "creationTime100ns": str(FILETIME_HOST)},
            {"role": "child", "pid": 4101, "creationTime100ns": str(FILETIME_CHILD)},
        ],
        "expectations": [
            {"key": "host_input", "direction": "capture", "endpointName": "Line 1 Virtual Cable"},
            {"key": "host_output", "direction": "render", "endpointName": "Voicemeeter In 5"},
        ],
    }


def expected_request() -> _Request:
    return _validate_request(request_data())


def active_session(pid: int, instance_id: str = "private-session-instance") -> _RawSession:
    return _RawSession(pid, "single", "active", instance_id)


def complete_snapshot(
    *,
    capture_sessions: tuple[_RawSession, ...] | None = None,
    render_sessions: tuple[_RawSession, ...] | None = None,
    capture_endpoint_id: str = "endpoint-capture-private-id",
    render_endpoint_id: str = "endpoint-render-private-id",
    capture_name: str = "Line 1 Virtual Cable",
    render_name: str = "Voicemeeter In 5",
    before_host: int = FILETIME_HOST,
    after_host: int = FILETIME_HOST,
    before_child: int = FILETIME_CHILD,
    after_child: int = FILETIME_CHILD,
    extra_process_times_before: dict[int, int] | None = None,
    extra_process_times_after: dict[int, int] | None = None,
    exited: frozenset[int] = frozenset(),
) -> _RawSnapshot:
    process_times_before = {4100: before_host, 4101: before_child}
    process_times_after = {4100: after_host, 4101: after_child}
    process_times_before.update(extra_process_times_before or {})
    process_times_after.update(extra_process_times_after or {})
    return _RawSnapshot(
        endpoints=(
            _RawEndpoint(
                "capture", capture_name, capture_endpoint_id,
                (active_session(4101),) if capture_sessions is None else capture_sessions,
            ),
            _RawEndpoint(
                "render", render_name, render_endpoint_id,
                (active_session(4101),) if render_sessions is None else render_sessions,
            ),
        ),
        process_times_before=process_times_before,
        process_times_after=process_times_after,
        exited_pids=exited,
    )


class FixedReader:
    def __init__(self, snapshot: _RawSnapshot):
        self.snapshot = snapshot
        self.calls = 0

    def read(self, _request: _Request) -> _RawSnapshot:
        self.calls += 1
        return self.snapshot


class RequestValidationTests(unittest.TestCase):
    def test_valid_request_keeps_filetime_exact_as_decimal_string(self) -> None:
        parsed = _validate_request(request_data())
        self.assertEqual(parsed.host_generation, 23)
        self.assertEqual(parsed.host_pid, 4100)
        self.assertEqual(parsed.processes[0].creation_time_100ns, FILETIME_HOST)
        self.assertEqual(parsed.expectations[0].direction, "capture")
        self.assertEqual(parsed.loopback_readers, ())

    def test_optional_loopback_reader_is_a_separate_exact_process_pin(self) -> None:
        value = request_data()
        value["loopbackReaders"] = [{"pid": 4102, "creationTime100ns": str(FILETIME_CHILD + 1)}]
        parsed = _validate_request(value)
        self.assertEqual(len(parsed.loopback_readers), 1)
        self.assertEqual(parsed.loopback_readers[0].pid, 4102)
        self.assertEqual(parsed.loopback_readers[0].creation_time_100ns, FILETIME_CHILD + 1)

    def test_loopback_reader_pins_reject_duplicates_overlap_malformed_and_unbounded_lists(self) -> None:
        invalid_values = (
            [{"pid": 4102, "creationTime100ns": str(FILETIME_CHILD + 1)}] * 2,
            [{"pid": 4100, "creationTime100ns": str(FILETIME_HOST)}],
            [{"pid": 4101, "creationTime100ns": str(FILETIME_CHILD)}],
            [{"pid": True, "creationTime100ns": str(FILETIME_CHILD)}],
            [{"pid": 4102, "creationTime100ns": FILETIME_CHILD + 1}],
            [],
            [{"pid": 4200 + index, "creationTime100ns": str(FILETIME_CHILD + index + 1)} for index in range(17)],
        )
        for readers in invalid_values:
            value = request_data()
            value["loopbackReaders"] = readers
            with self.subTest(readers=readers), self.assertRaisesRegex(RuntimeError, "request_invalid"):
                _validate_request(value)

    def test_filetime_conversion_uses_the_unix_epoch_once_for_known_2026_time(self) -> None:
        expected = datetime(2026, 10, 5, 12, 34, 56, 789123, tzinfo=UTC)
        delta = expected - datetime(1970, 1, 1, tzinfo=UTC)
        unix_microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
        filetime = 116_444_736_000_000_000 + unix_microseconds * 10
        self.assertEqual(_utc_from_filetime(filetime), "2026-10-05T12:34:56.789123Z")

    def test_rejects_duplicate_process_id_even_with_different_role(self) -> None:
        value = request_data()
        value["processes"].append({"role": "child", "pid": 4100, "creationTime100ns": str(FILETIME_HOST)})
        with self.assertRaisesRegex(RuntimeError, "request_invalid"):
            _validate_request(value)

    def test_rejects_invalid_generation_pid_filetime_and_missing_direction(self) -> None:
        for field, replacement in (
            ("hostGeneration", True),
            ("hostPid", True),
        ):
            value = request_data()
            value[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "request_invalid"):
                _validate_request(value)

        value = request_data()
        value["processes"][0]["creationTime100ns"] = FILETIME_HOST
        with self.assertRaisesRegex(RuntimeError, "request_invalid"):
            _validate_request(value)

        value = request_data()
        del value["expectations"][0]["direction"]
        with self.assertRaisesRegex(RuntimeError, "request_invalid"):
            _validate_request(value)

    def test_endpoint_names_may_repeat_across_directions_but_route_keys_are_fixed(self) -> None:
        value = request_data()
        value["expectations"] = [
            {"key": "host_input", "direction": "capture", "endpointName": "Same friendly name"},
            {"key": "host_output", "direction": "render", "endpointName": "Same friendly name"},
        ]
        parsed = _validate_request(value)
        self.assertEqual(parsed.expectations[0].endpoint_name, parsed.expectations[1].endpoint_name)

        value["expectations"][1]["direction"] = "capture"
        with self.assertRaisesRegex(RuntimeError, "request_invalid"):
            _validate_request(value)


class WindowsAbiTests(unittest.TestCase):
    def test_session_control2_methods_follow_inherited_interface_slots(self) -> None:
        self.assertEqual(_SESSION_CONTROL2_GET_SESSION_INSTANCE_IDENTIFIER_SLOT, 13)
        self.assertEqual(_SESSION_CONTROL2_GET_PROCESS_ID_SLOT, 14)

    def test_ctypes_structures_match_windows_core_audio_layout(self) -> None:
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(_GUID), 16)
        self.assertEqual(ctypes.sizeof(_PROPERTYKEY), 20)
        self.assertEqual(ctypes.sizeof(_FILETIME), 8)
        self.assertEqual(ctypes.sizeof(_CALPWSTR), 8 + pointer_size)
        self.assertEqual(ctypes.sizeof(_PROPVARIANT), 8 + max(8, ctypes.sizeof(_CALPWSTR)))

    def test_guid_layout_preserves_known_mmdevice_class_id(self) -> None:
        guid = _GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
        self.assertEqual(guid.Data1, 0xBCDE0395)
        self.assertEqual(guid.Data2, 0xE52F)
        self.assertEqual(guid.Data3, 0x467C)
        self.assertEqual(bytes(guid.Data4), bytes.fromhex("8E3DC4579291692E"))

    def test_core_audio_state_values_and_hresult_are_fail_closed(self) -> None:
        # AudioSessionStateInactive=0, Active=1, Expired=2 per the SDK enum.
        self.assertEqual(_SESSION_STATE_NAMES, {0: "inactive", 1: "active", 2: "expired"})
        self.assertEqual(_session_state_from_hresult(0, 0), "inactive")
        self.assertEqual(_session_state_from_hresult(0, 1), "active")
        self.assertIsNone(_session_state_from_hresult(0x00000001, 1))
        self.assertIsNone(_session_state_from_hresult(0, 7))

    def test_shared_process_hresult_uses_facility_encoded_success_code(self) -> None:
        self.assertEqual(_AUDCLNT_S_NO_SINGLE_PROCESS, 0x0889000D)
        self.assertEqual(_session_attribution_from_hresult(0), "single")
        self.assertEqual(_session_attribution_from_hresult(0x0889000D), "shared")
        self.assertEqual(_session_attribution_from_hresult(0x0000000D), "unavailable")
        self.assertEqual(_session_attribution_from_hresult(0x0889000E), "unavailable")

    def test_actual_audio_session_enum_values_drive_route_active_state(self) -> None:
        inactive = _RawSession(4101, "single", _session_state_from_hresult(0, 0), "inactive-id")
        inactive_report = _build_report(
            expected_request(), complete_snapshot(capture_sessions=(inactive,)), 1_800_000_000_000,
        )
        self.assertFalse(inactive_report["routes"][0]["active"])

        active = _RawSession(4101, "single", _session_state_from_hresult(0, 1), "active-id")
        active_report = _build_report(
            expected_request(), complete_snapshot(capture_sessions=(active,)), 1_800_000_000_000,
        )
        self.assertTrue(active_report["routes"][0]["active"])

    def test_read_session_calls_sdk_slots_and_ignores_inactive_shared_session(self) -> None:
        pointer, keeper, ole32, freed = fake_session_control2(
            pid=0, pid_hresult=_AUDCLNT_S_NO_SINGLE_PROCESS, state=0,
        )
        session = _read_session(ole32, pointer, {4100, 4101})
        self.assertEqual(session.attribution, "shared")
        self.assertEqual(session.state, "inactive")
        self.assertIsNone(session.instance_id)
        self.assertIsNone(session.error)
        self.assertEqual(freed, [])
        self.assertIsNotNone(keeper)

        pointer, keeper, ole32, freed = fake_session_control2(state=1)
        active = _read_session(ole32, pointer, {4100, 4101})
        self.assertEqual(active.attribution, "single")
        self.assertEqual(active.state, "active")
        self.assertEqual(active.instance_id, "session-instance")
        self.assertEqual(len(freed), 1)
        self.assertIsNotNone(keeper)

    def test_read_session_shared_active_and_unknown_attribution_fail_closed(self) -> None:
        pointer, keeper, ole32, _freed = fake_session_control2(
            pid=4101, pid_hresult=_AUDCLNT_S_NO_SINGLE_PROCESS, state=1,
        )
        shared = _read_session(ole32, pointer, {4100, 4101})
        self.assertEqual(shared.attribution, "shared")
        self.assertEqual(shared.state, "active")
        self.assertIsNone(shared.instance_id)
        self.assertIsNone(shared.error)
        self.assertIsNotNone(keeper)

        pointer, keeper, ole32, _freed = fake_session_control2(pid=4101, pid_hresult=1, state=1)
        unknown = _read_session(ole32, pointer, {4100, 4101})
        self.assertEqual(unknown.attribution, "unavailable")
        self.assertEqual(unknown.state, "active")
        self.assertEqual(unknown.error, "session_attribution_unavailable")
        self.assertIsNotNone(keeper)

        pointer, keeper, ole32, _freed = fake_session_control2(
            pid=9999, state=1, state_hresult=1,
        )
        unknown_state = _read_session(ole32, pointer, {4100, 4101})
        self.assertIsNone(unknown_state.state)
        self.assertEqual(unknown_state.error, "session_state_unavailable")
        self.assertIsNotNone(keeper)

    def test_loopback_reader_session_does_not_request_or_publish_session_identifier(self) -> None:
        pointer, keeper, ole32, freed = fake_session_control2(pid=4102, state=1)
        reader_session = _read_session(ole32, pointer, {4100, 4101, 4102}, {4100, 4101})
        self.assertEqual(reader_session.pid, 4102)
        self.assertEqual(reader_session.state, "active")
        self.assertIsNone(reader_session.instance_id)
        self.assertIsNone(reader_session.error)
        self.assertEqual(freed, [])
        self.assertIsNotNone(keeper)


class SnapshotMatchingTests(unittest.TestCase):
    def test_route_diagnostics_are_opt_in_and_do_not_change_the_decision_report(self) -> None:
        request = expected_request()
        snapshot = complete_snapshot(
            capture_sessions=(_RawSession(4101, "single", "inactive", "private-inactive-session"),),
        )
        baseline = _build_report(request, snapshot, 1_800_000_000_000)
        default = _build_report(request, snapshot, 1_800_000_000_000)
        self.assertEqual(default, baseline)
        self.assertNotIn("routeDiagnostics", default)

        observed = _build_report(
            request,
            snapshot,
            1_800_000_000_000,
            include_route_diagnostics=True,
        )
        diagnostics = observed.pop("routeDiagnostics")
        self.assertEqual(observed, baseline)
        capture = diagnostics["routes"][0]
        self.assertEqual(capture["endpointStatus"], "unique")
        self.assertEqual(capture["sessionCount"], 1)
        self.assertEqual(capture["stateCounts"], {"active": 0, "inactive": 1, "expired": 0, "unknown": 0})
        self.assertEqual(capture["attributionCounts"], {"single": 1, "shared": 0, "unavailable": 0})
        self.assertEqual(capture["targetCounts"], {"host": 0, "child": 1, "pinnedLoopback": 0, "foreign": 0, "unclassified": 0})
        self.assertEqual(capture["identityQueried"], 0)
        self.assertEqual(capture["identityNotQueried"], 1)
        self.assertEqual(capture["qualifyingSessions"], 0)
        self.assertFalse(capture["active"])
        encoded = json.dumps(diagnostics)
        for private_value in (
            "private-inactive-session", "endpoint-capture-private-id", "4100", "4101",
        ):
            self.assertNotIn(private_value, encoded)

    def test_route_diagnostics_distinguish_foreign_shared_loopback_and_missing_identity(self) -> None:
        value = request_data()
        value["loopbackReaders"] = [{"pid": 4102, "creationTime100ns": str(FILETIME_CHILD + 1)}]
        request = _validate_request(value)
        sessions = (
            _RawSession(4100, "single", "inactive", "inactive-host-session"),
            _RawSession(4102, "single", "active", None, "session_identity_unavailable"),
            _RawSession(9999, "single", "active", None),
            _RawSession(None, "unavailable", "active", None, "session_attribution_unavailable"),
            _RawSession(4101, "single", "active", None, "session_identity_unavailable"),
            _RawSession(4100, "shared", "expired", "expired-shared-session"),
        )
        report = _build_report(
            request,
            complete_snapshot(
                capture_sessions=sessions,
                extra_process_times_before={4102: FILETIME_CHILD + 1},
                extra_process_times_after={4102: FILETIME_CHILD + 1},
            ),
            1_800_000_000_000,
            include_route_diagnostics=True,
        )
        capture = report["routeDiagnostics"]["routes"][0]
        self.assertEqual(capture["sessionCount"], 6)
        self.assertEqual(capture["stateCounts"], {"active": 4, "inactive": 1, "expired": 1, "unknown": 0})
        self.assertEqual(capture["attributionCounts"], {"single": 4, "shared": 1, "unavailable": 1})
        self.assertEqual(capture["targetCounts"], {"host": 1, "child": 1, "pinnedLoopback": 1, "foreign": 1, "unclassified": 2})
        self.assertEqual(capture["identityQueried"], 1)
        self.assertEqual(capture["identityAvailable"], 0)
        self.assertEqual(capture["identityMissing"], 1)
        self.assertEqual(capture["identityNotQueried"], 5)
        self.assertEqual(capture["qualifyingSessions"], 0)
        self.assertFalse(capture["active"])

        render_snapshot = complete_snapshot(
            render_sessions=(active_session(4101), active_session(4102, "private-reader-session")),
            extra_process_times_before={4102: FILETIME_CHILD + 1},
            extra_process_times_after={4102: FILETIME_CHILD + 1},
        )
        render_report = _build_report(request, render_snapshot, 1_800_000_000_000, include_route_diagnostics=True)
        render = render_report["routeDiagnostics"]["routes"][1]
        self.assertTrue(render_report["verified"])
        self.assertEqual(render["targetCounts"]["pinnedLoopback"], 1)
        self.assertEqual(render["identityNotQueried"], 1)
        self.assertEqual(render["qualifyingSessions"], 1)

    def test_route_diagnostics_count_multiple_qualifiers_and_stale_processes(self) -> None:
        multiple = _build_report(
            expected_request(),
            complete_snapshot(capture_sessions=(active_session(4100), active_session(4101))),
            1_800_000_000_000,
            include_route_diagnostics=True,
        )
        self.assertFalse(multiple["verified"])
        self.assertTrue(multiple["routes"][0]["active"])
        self.assertEqual(multiple["routeDiagnostics"]["routes"][0]["qualifyingSessions"], 2)

        stale = _build_report(
            expected_request(),
            complete_snapshot(after_host=FILETIME_HOST + 1),
            1_800_000_000_000,
            include_route_diagnostics=True,
        )
        self.assertEqual(stale["errors"], ["target_process_stale"])
        self.assertEqual(stale["routeDiagnostics"]["processSnapshot"], "stale")
        self.assertEqual(stale["routeDiagnostics"]["staleProcessCount"], 1)
        self.assertTrue(all(route["endpointStatus"] == "not_observed" for route in stale["routeDiagnostics"]["routes"]))

    def test_route_diagnostics_are_omitted_on_construction_serialization_or_size_failure(self) -> None:
        request = expected_request()
        snapshot = complete_snapshot()
        baseline = _build_report(request, snapshot, 1_800_000_000_000)
        with patch("windows_routes._new_route_diagnostics", side_effect=RuntimeError("private sentinel")):
            constructed = _build_report(request, snapshot, 1_800_000_000_000, include_route_diagnostics=True)
        self.assertEqual(constructed, baseline)

        with patch("windows_routes.json.dumps", side_effect=ValueError("private sentinel")):
            serialized = _build_report(request, snapshot, 1_800_000_000_000, include_route_diagnostics=True)
        self.assertEqual(serialized, baseline)

        many_sessions = tuple(active_session(4101, f"private-session-{index}") for index in range(300))
        large_snapshot = complete_snapshot(capture_sessions=many_sessions)
        large_base = _build_report(request, large_snapshot, 1_800_000_000_000)
        large_optional = _build_report(
            request,
            large_snapshot,
            1_800_000_000_000,
            include_route_diagnostics=True,
        )
        self.assertEqual(large_optional, large_base)
        self.assertNotIn("routeDiagnostics", large_optional)

    def test_route_diagnostics_endpoint_funnel_and_invalid_target_set(self) -> None:
        request = expected_request()
        base = complete_snapshot()
        ambiguous = replace(
            base,
            endpoints=(
                _RawEndpoint("capture", "Line 1 Virtual Cable", "capture-one", (active_session(4101),)),
                _RawEndpoint("capture", "Line 1 Virtual Cable", "capture-two", (active_session(4100),)),
                base.endpoints[1],
            ),
        )
        report = _build_report(request, ambiguous, 1_800_000_000_000, include_route_diagnostics=True)
        capture = report["routeDiagnostics"]["routes"][0]
        self.assertEqual(capture["endpointMatches"], 2)
        self.assertEqual(capture["endpointStatus"], "ambiguous")
        self.assertEqual(capture["sessionCount"], 0)

        invalid = replace(base, discovered_processes=(_ProcessTarget("host", 4100, FILETIME_HOST),))
        invalid_report = _build_report(
            _Request(
                expected_request().host_generation,
                expected_request().host_pid,
                expected_request().processes,
                expected_request().expectations,
                discover_children=True,
            ),
            invalid,
            1_800_000_000_000,
            include_route_diagnostics=True,
        )
        self.assertEqual(invalid_report["errors"], ["target_process_stale"])
        self.assertEqual(invalid_report["routeDiagnostics"]["processSnapshot"], "invalid_target_set")

    def test_verified_requires_active_target_session_on_both_exact_routes(self) -> None:
        report = _build_report(expected_request(), complete_snapshot(), 1_800_000_000_000)
        self.assertTrue(report["verified"])
        self.assertEqual(report["source"], "windows_active_sessions")
        self.assertEqual(report["hostGeneration"], 23)
        self.assertEqual([route["key"] for route in report["routes"]], ["host_input", "host_output"])
        self.assertTrue(all(route["active"] for route in report["routes"]))

        missing_input = complete_snapshot(capture_sessions=(_RawSession(4101, "single", "inactive", "capture-id"),))
        report = _build_report(expected_request(), missing_input, 1_800_000_000_000)
        self.assertFalse(report["verified"])
        self.assertFalse(report["routes"][0]["active"])
        self.assertIn("host_input_not_active", report["errors"])

        no_output = complete_snapshot(render_sessions=())
        report = _build_report(expected_request(), no_output, 1_800_000_000_000)
        self.assertFalse(report["verified"])
        self.assertFalse(report["routes"][1]["active"])
        self.assertIn("host_output_not_active", report["errors"])

    def test_process_creation_time_is_checked_before_and_after_snapshot(self) -> None:
        stale_before = complete_snapshot(before_child=FILETIME_CHILD + 1)
        report = _build_report(expected_request(), stale_before, 1_800_000_000_000)
        self.assertFalse(report["verified"])
        self.assertEqual(report["errors"], ["target_process_stale"])

        stale_after = complete_snapshot(after_host=FILETIME_HOST + 1)
        report = _build_report(expected_request(), stale_after, 1_800_000_000_000)
        self.assertEqual(report["errors"], ["target_process_stale"])

        exited = complete_snapshot(exited=frozenset({4101}))
        report = _build_report(expected_request(), exited, 1_800_000_000_000)
        self.assertEqual(report["errors"], ["target_process_stale"])

    def test_shared_audio_session_pid_is_never_counted_as_route_proof(self) -> None:
        shared = _RawSession(4101, "shared", "active", "shared-session-id")
        report = _build_report(
            expected_request(),
            complete_snapshot(capture_sessions=(shared,)),
            1_800_000_000_000,
        )
        self.assertFalse(report["verified"])
        self.assertFalse(report["routes"][0]["active"])
        self.assertEqual(report["routes"][0]["sessions"], [])
        self.assertIn("shared_session_unverified", report["errors"])

    def test_inactive_pid_zero_shared_background_session_is_ignored(self) -> None:
        inactive_shared = _RawSession(0, "shared", "inactive", None)
        report = _build_report(
            expected_request(),
            complete_snapshot(
                capture_sessions=(inactive_shared,),
                render_sessions=(inactive_shared,),
            ),
            1_800_000_000_000,
        )
        self.assertFalse(report["verified"])
        self.assertEqual(report["routes"][0]["sessions"], [])
        self.assertEqual(report["routes"][1]["sessions"], [])
        self.assertNotIn("shared_session_unverified", report["errors"])
        self.assertIn("host_input_not_active", report["errors"])
        self.assertIn("host_output_not_active", report["errors"])

    def test_multiple_target_sessions_do_not_produce_unique_route_verification(self) -> None:
        snapshot = complete_snapshot(
            capture_sessions=(active_session(4100, "capture-host"), active_session(4101, "capture-child")),
        )
        report = _build_report(expected_request(), snapshot, 1_800_000_000_000)
        self.assertTrue(report["routes"][0]["active"])
        self.assertEqual(len(report["routes"][0]["sessions"]), 2)
        self.assertFalse(report["verified"])

    def test_unattributed_session_surfaces_error_instead_of_disappearing(self) -> None:
        unattributed = _RawSession(None, "unavailable", "active", None, "session_attribution_unavailable")
        report = _build_report(
            expected_request(),
            complete_snapshot(capture_sessions=(unattributed,)),
            1_800_000_000_000,
        )
        self.assertFalse(report["verified"])
        self.assertIn("session_attribution_unavailable", report["errors"])
        self.assertFalse(report["routes"][0]["active"])

    def test_unrelated_shared_session_still_blocks_unambiguous_verification(self) -> None:
        unrelated_shared = _RawSession(9999, "shared", "active", "shared-session-id")
        report = _build_report(
            expected_request(),
            complete_snapshot(render_sessions=(active_session(4101), unrelated_shared)),
            1_800_000_000_000,
        )
        self.assertTrue(report["routes"][1]["active"])
        self.assertFalse(report["verified"])
        self.assertIn("shared_session_unverified", report["errors"])

    def test_active_foreign_render_conflicts_but_foreign_capture_does_not(self) -> None:
        foreign = _RawSession(9999, "single", "active", None)
        render_conflict = _build_report(
            expected_request(),
            complete_snapshot(render_sessions=(active_session(4101), foreign)),
            1_800_000_000_000,
        )
        self.assertTrue(render_conflict["routes"][1]["active"])
        self.assertEqual(len(render_conflict["routes"][1]["sessions"]), 1)
        self.assertFalse(render_conflict["verified"])
        self.assertIn("endpoint_session_conflict", render_conflict["errors"])

        capture_foreign = _build_report(
            expected_request(),
            complete_snapshot(capture_sessions=(foreign,)),
            1_800_000_000_000,
        )
        self.assertFalse(capture_foreign["routes"][0]["active"])
        self.assertNotIn("endpoint_session_conflict", capture_foreign["errors"])

    def test_pinned_loopback_reader_is_ignored_only_on_host_output_and_never_counts_as_host(self) -> None:
        value = request_data()
        reader_pid = 4102
        reader_filetime = FILETIME_CHILD + 1
        value["loopbackReaders"] = [{"pid": reader_pid, "creationTime100ns": str(reader_filetime)}]
        request = _validate_request(value)
        snapshot = complete_snapshot(
            render_sessions=(active_session(4101, "host-output"), active_session(reader_pid, "reader")),
            extra_process_times_before={reader_pid: reader_filetime},
            extra_process_times_after={reader_pid: reader_filetime},
        )
        report = _build_report(request, snapshot, 1_800_000_000_000)
        self.assertTrue(report["verified"])
        host_output = report["routes"][1]
        self.assertEqual(len(host_output["sessions"]), 1)
        self.assertEqual(host_output["sessions"][0]["pid"], 4101)
        self.assertNotIn(reader_pid, [session["pid"] for route in report["routes"] for session in route["sessions"]])
        self.assertNotIn(str(reader_pid), json.dumps(report))

        wrong_flow = _build_report(
            request,
            complete_snapshot(
                capture_sessions=(active_session(4101, "host-input"), active_session(reader_pid, "reader")),
                render_sessions=(active_session(4101, "host-output"),),
                extra_process_times_before={reader_pid: reader_filetime},
                extra_process_times_after={reader_pid: reader_filetime},
            ),
            1_800_000_000_000,
        )
        self.assertFalse(wrong_flow["verified"])
        self.assertIn("endpoint_session_conflict", wrong_flow["errors"])
        self.assertEqual([session["pid"] for session in wrong_flow["routes"][0]["sessions"]], [4101])

    def test_loopback_reader_pin_must_be_live_exact_and_single_process(self) -> None:
        value = request_data()
        reader_pid = 4102
        reader_filetime = FILETIME_CHILD + 1
        value["loopbackReaders"] = [{"pid": reader_pid, "creationTime100ns": str(reader_filetime)}]
        request = _validate_request(value)
        reader = active_session(reader_pid, "reader")
        valid_snapshot = complete_snapshot(
            render_sessions=(active_session(4101, "host-output"), reader),
            extra_process_times_before={reader_pid: reader_filetime},
            extra_process_times_after={reader_pid: reader_filetime},
        )
        stale_snapshots = (
            complete_snapshot(
                render_sessions=(active_session(4101), reader),
                extra_process_times_before={reader_pid: reader_filetime - 1},
                extra_process_times_after={reader_pid: reader_filetime},
            ),
            complete_snapshot(
                render_sessions=(active_session(4101), reader),
                extra_process_times_before={reader_pid: reader_filetime},
                extra_process_times_after={reader_pid: reader_filetime - 1},
            ),
            complete_snapshot(render_sessions=(active_session(4101), reader)),
            complete_snapshot(
                render_sessions=(active_session(4101), reader),
                extra_process_times_before={reader_pid: reader_filetime},
                extra_process_times_after={reader_pid: reader_filetime},
                exited=frozenset({reader_pid}),
            ),
        )
        self.assertTrue(_build_report(request, valid_snapshot, 1_800_000_000_000)["verified"])
        for snapshot in stale_snapshots:
            with self.subTest(snapshot=snapshot):
                report = _build_report(request, snapshot, 1_800_000_000_000)
                self.assertFalse(report["verified"])
                self.assertEqual(report["errors"], ["target_process_stale"])

        shared_reader = _RawSession(reader_pid, "shared", "active", None)
        shared = _build_report(
            request,
            complete_snapshot(
                render_sessions=(active_session(4101), shared_reader),
                extra_process_times_before={reader_pid: reader_filetime},
                extra_process_times_after={reader_pid: reader_filetime},
            ),
            1_800_000_000_000,
        )
        self.assertFalse(shared["verified"])
        self.assertIn("shared_session_unverified", shared["errors"])

    def test_same_friendly_name_in_opposite_flows_is_allowed_if_endpoint_ids_differ(self) -> None:
        snapshot = complete_snapshot(
            capture_name="Duplex endpoint",
            render_name="Duplex endpoint",
            capture_endpoint_id="capture-flow-id",
            render_endpoint_id="render-flow-id",
        )
        request = _validate_request({
            **request_data(),
            "expectations": [
                {"key": "host_input", "direction": "capture", "endpointName": "Duplex endpoint"},
                {"key": "host_output", "direction": "render", "endpointName": "Duplex endpoint"},
            ],
        })
        report = _build_report(request, snapshot, 1_800_000_000_000)
        self.assertTrue(report["verified"])

    def test_same_endpoint_identity_across_routes_is_not_verified(self) -> None:
        snapshot = complete_snapshot(
            capture_endpoint_id="same-endpoint-id",
            render_endpoint_id="same-endpoint-id",
        )
        report = _build_report(expected_request(), snapshot, 1_800_000_000_000)
        self.assertFalse(report["verified"])
        self.assertIn("routes_not_distinct", report["errors"])

    def test_duplicate_name_within_a_direction_is_ambiguous(self) -> None:
        base = complete_snapshot()
        snapshot = _RawSnapshot(
            endpoints=(
                base.endpoints[0],
                _RawEndpoint("capture", "Line 1 Virtual Cable", "second-id", (active_session(4101),)),
                base.endpoints[1],
            ),
            process_times_before=base.process_times_before,
            process_times_after=base.process_times_after,
        )
        report = _build_report(expected_request(), snapshot, 1_800_000_000_000)
        self.assertFalse(report["verified"])
        self.assertFalse(report["routes"][0]["active"])
        self.assertIn("endpoint_ambiguous", report["errors"])

    def test_report_contains_hashes_not_raw_endpoint_or_session_ids(self) -> None:
        report = _build_report(expected_request(), complete_snapshot(), 1_800_000_000_000)
        encoded = json.dumps(report)
        self.assertNotIn("endpoint-capture-private-id", encoded)
        self.assertNotIn("private-session-instance", encoded)
        self.assertNotIn("session-instance", encoded)
        self.assertRegex(report["routes"][0]["endpointIdSha256"], r"^[0-9a-f]{64}$")
        session = report["routes"][0]["sessions"][0]
        self.assertEqual(session["pid"], 4101)
        self.assertEqual(session["processGeneration"], str(FILETIME_CHILD))
        self.assertIn("processStartTimeUtc", session)
        self.assertIn("firstObservedUtc", session)
        self.assertNotEqual(session["processStartTimeUtc"], session["firstObservedUtc"])


class WorkerTests(unittest.TestCase):
    def test_timeout_returns_unverified_and_does_not_stack_workers(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        snapshot = complete_snapshot()

        class BlockingReader:
            def read(self, _request: _Request) -> _RawSnapshot:
                entered.set()
                release.wait()
                return snapshot

        first = observe_windows_routes(request_data(), reader=BlockingReader(), timeout_seconds=0.03)
        self.assertTrue(entered.is_set())
        self.assertFalse(first["verified"])
        self.assertEqual(first["errors"], ["observer_timeout"])

        second = observe_windows_routes(request_data(), reader=FixedReader(snapshot), timeout_seconds=0.03)
        self.assertFalse(second["verified"])
        self.assertEqual(second["errors"], ["observer_busy"])

        release.set()
        deadline = time.monotonic() + 0.5
        while _WORKER_LOCK.locked() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(_WORKER_LOCK.locked())
        third = observe_windows_routes(request_data(), reader=FixedReader(snapshot), timeout_seconds=0.3)
        self.assertTrue(third["verified"])


class EndpointInventoryTests(unittest.TestCase):
    def test_injected_metadata_reader_returns_bounded_active_endpoint_records(self) -> None:
        observed_threads: list[str] = []
        records = [
            {"id": "capture-id", "name": "Virtual Input", "flow": "capture"},
            {"id": "render-id", "name": "Virtual Output", "flow": "render"},
        ]

        def reader() -> list[dict[str, str]]:
            observed_threads.append(threading.current_thread().name)
            return records

        self.assertEqual(enumerate_active_endpoints(reader=reader, timeout_seconds=0.3), records)
        self.assertEqual(observed_threads, ["qqcall-endpoint-inventory"])

    def test_inventory_rejects_ambiguous_ids_and_invalid_metadata(self) -> None:
        for records in (
            [
                {"id": "same", "name": "Virtual Input", "flow": "capture"},
                {"id": "SAME", "name": "Virtual Output", "flow": "render"},
            ],
            [{"id": "id", "name": "", "flow": "capture"}],
            [{"id": "id", "name": "Virtual Input", "flow": "loopback"}],
        ):
            with self.subTest(records=records), self.assertRaises(EndpointInventoryError):
                enumerate_active_endpoints(reader=lambda records=records: records, timeout_seconds=0.3)

    def test_timeout_keeps_worker_lock_until_late_metadata_reader_finishes(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        records = [{"id": "id", "name": "Virtual Audio Cable", "flow": "render"}]

        def blocked_reader():
            entered.set()
            release.wait()
            return records

        with self.assertRaises(EndpointInventoryError) as timed_out:
            enumerate_active_endpoints(reader=blocked_reader, timeout_seconds=0.03)
        self.assertEqual(timed_out.exception.code, "observer_timeout")
        self.assertTrue(entered.is_set())
        with self.assertRaises(EndpointInventoryError) as busy:
            enumerate_active_endpoints(reader=lambda: records, timeout_seconds=0.03)
        self.assertEqual(busy.exception.code, "observer_busy")

        release.set()
        deadline = time.monotonic() + 0.5
        while _WORKER_LOCK.locked() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(_WORKER_LOCK.locked())
        self.assertEqual(enumerate_active_endpoints(reader=lambda: records, timeout_seconds=0.3), records)

    def test_reader_exceptions_are_sanitized_and_release_worker_lock(self) -> None:
        def failed_reader():
            raise RuntimeError("private endpoint path")

        with self.assertRaises(EndpointInventoryError) as caught:
            enumerate_active_endpoints(reader=failed_reader, timeout_seconds=0.3)
        self.assertEqual(caught.exception.code, "observer_unavailable")
        self.assertNotIn("private endpoint path", str(caught.exception))
        self.assertFalse(_WORKER_LOCK.locked())

    def test_native_inventory_calls_only_enumeration_metadata_primitives(self) -> None:
        class FakeReference:
            def __init__(self, pointer):
                self.pointer = pointer

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        com_calls: list[tuple[int, int]] = []

        def fake_interface(pointer, slot, _argtypes, values):
            com_calls.append((int(pointer.value), slot))
            if slot == 3:
                return ctypes.c_void_p(100 + int(values[0]))
            return ctypes.c_void_p(1000 + int(pointer.value) * 10 + int(values[0]))

        def fake_method(pointer, slot, _restype, _argtypes):
            com_calls.append((int(pointer.value), slot))

            def invoke(_this, output):
                if slot == 3:
                    ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = 1
                elif slot == 6:
                    ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = 1
                return 0

            return invoke

        with patch("windows_routes._ComReference", FakeReference), \
             patch("windows_routes._com_out_interface", side_effect=fake_interface), \
             patch("windows_routes._com_method", side_effect=fake_method), \
             patch("windows_routes._read_friendly_name", side_effect=["Virtual Capture", "Virtual Render"]), \
             patch("windows_routes._read_endpoint_id", side_effect=["capture-id", "render-id"]):
            endpoints = _read_active_endpoint_inventory(object(), ctypes.c_void_p(1))

        self.assertEqual([item["flow"] for item in endpoints], ["render", "capture"])
        self.assertEqual({item["id"] for item in endpoints}, {"capture-id", "render-id"})
        self.assertTrue(all(slot in (3, 4, 6) for _pointer, slot in com_calls))


class ChildDiscoveryTests(unittest.TestCase):
    def test_reused_parent_pid_prunes_older_survivor_and_its_entire_branch(self):
        parents = {4100: 100, 4101: 4100, 5000: 4101, 5001: 5000}
        times = {4100: 100, 4101: 110, 5000: 90, 5001: 95}
        self.assertEqual(_descendant_pids(parents, 4100, times), {4101})
        # New activity in the excluded old branch cannot add an audio owner.
        self.assertEqual(_descendant_pids({**parents, 5002: 5000}, 4100, times), {4101})
        with self.assertRaises(_ObserverFailure):
            _descendant_pids({**parents, 5002: 4101}, 4100, times)

    def test_reader_excludes_old_non_qq_pid_branch_but_rejects_real_non_qq_child(self):
        request = _validate_request({**request_data(), "discoverChildren": True})
        parents = {4100: 100, 4101: 4100, 5000: 4101, 5001: 5000}
        class FakeReference:
            pointer = ctypes.c_void_p(1)
            def __init__(self, _pointer): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        for old_branch in (True, False):
            times = {4100: FILETIME_HOST, 4101: FILETIME_CHILD,
                     5000: FILETIME_HOST - 100 if old_branch else FILETIME_CHILD + 100,
                     5001: FILETIME_HOST - 50 if old_branch else FILETIME_CHILD + 200}
            images = []
            def image(_kernel, handle):
                images.append(handle.value)
                return r"C:\QQ\QQ.exe" if handle.value in (4100, 4101) else r"C:\Windows\cmd.exe"
            with self.subTest(old_branch=old_branch), \
                 patch("windows_routes._read_process_parents", return_value=parents), \
                 patch("windows_routes._open_process_handle", side_effect=lambda _k, pid, _s: ctypes.c_void_p(pid)), \
                 patch("windows_routes._read_process_times", return_value=(times, set())), \
                 patch("windows_routes._read_process_image", side_effect=image), \
                 patch("windows_routes._create_mmdevice_enumerator", return_value=ctypes.c_void_p(1)), \
                 patch("windows_routes._ComReference", FakeReference), \
                 patch("windows_routes._read_named_endpoint_sessions", return_value=[]):
                if old_branch:
                    snapshot = _WindowsCoreAudioReader()._read_on_mta(request, object(), object())
                    self.assertEqual(snapshot.discovered_processes, ())
                    self.assertEqual(images, [4100, 4101])
                else:
                    with self.assertRaises(_ObserverFailure) as caught:
                        _WindowsCoreAudioReader()._read_on_mta(request, object(), object())
                    self.assertEqual(caught.exception.code, "target_process_stale")

    def test_mta_reader_rejects_new_or_removed_descendants_during_observation(self):
        request = _validate_request({**request_data(), "discoverChildren": True})
        stable = {4100: 100, 4101: 4100, 5000: 100}
        times = {4100: FILETIME_HOST, 4101: FILETIME_CHILD}
        class FakeReference:
            pointer = ctypes.c_void_p(1)
            def __init__(self, _pointer): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        for after in (stable, {**stable, 4200: 4100}, {4100: 100, 5000: 100}):
            with self.subTest(after=after), \
                patch("windows_routes._read_process_parents", side_effect=[stable, after]), \
                patch("windows_routes._open_process_handle", return_value=ctypes.c_void_p(1)), \
                patch("windows_routes._read_process_times", return_value=(times, set())), \
                patch("windows_routes._read_process_image", return_value="C:\\QQ\\QQ.exe"), \
                patch("windows_routes._create_mmdevice_enumerator", return_value=ctypes.c_void_p(1)), \
                patch("windows_routes._ComReference", FakeReference), \
                patch("windows_routes._read_named_endpoint_sessions", return_value=[]):
                if after == stable:
                    snapshot = _WindowsCoreAudioReader()._read_on_mta(request, object(), object())
                    self.assertEqual(snapshot.process_times_after, times)
                else:
                    with self.assertRaises(_ObserverFailure) as failure:
                        _WindowsCoreAudioReader()._read_on_mta(request, object(), object())
                    self.assertEqual(failure.exception.code, "target_process_stale")

    def test_tree_is_rooted_and_excludes_unrelated_branches(self):
        self.assertEqual(_descendant_pids({4100: 100, 4101: 4100, 4102: 4101, 5000: 100}, 4100), {4101, 4102})

    def test_new_child_is_verified_only_with_stable_exact_time(self):
        request = _validate_request({**request_data(), "discoverChildren": True})
        child = _ProcessTarget("child", 4200, FILETIME_CHILD + 100)
        snapshot = replace(complete_snapshot(capture_sessions=(active_session(4200),), render_sessions=(active_session(4200),),
            extra_process_times_before={4200: child.creation_time_100ns},
            extra_process_times_after={4200: child.creation_time_100ns}), discovered_processes=(child,))
        report = _build_report(request, snapshot, 1780000000000)
        self.assertTrue(report["verified"])
        self.assertEqual(report["discoveredProcesses"][0]["pid"], 4200)
        self.assertFalse(_build_report(request, replace(snapshot, process_times_after={4100: FILETIME_HOST, 4101: FILETIME_CHILD, 4200: child.creation_time_100ns + 1}), 1780000000000)["verified"])
        self.assertFalse(_build_report(request, replace(snapshot, discovered_processes=(child, child)), 1780000000000)["verified"])
        self.assertFalse(_build_report(expected_request(), snapshot, 1780000000000)["verified"])

if __name__ == "__main__":
    unittest.main()
