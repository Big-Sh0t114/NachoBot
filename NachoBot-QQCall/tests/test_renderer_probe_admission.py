from __future__ import annotations

import copy
import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import service.manager as manager
from service.renderer_probe_admission import (
    MAX_STDIN_BYTES,
    _Dependencies,
    main,
)


NOW_MS = 1_800_000_000_000
APP_PATH = r"C:\QQ\versions\9.9.26-44343\resources\app"
REQUEST = {
    "schema": 1,
    "targetPid": 500,
    "targetFiletime": "5000",
    "appPath": APP_PATH,
    "brokerGeneration": 71,
    "fingerprintKey": "a" * 64,
}


def _config(*, account_id: str = "12345", enabled: bool = True) -> dict:
    return {
        "enabled": enabled,
        "adapter": "snowluma",
        "account_id": account_id,
        "runtime_dir": r"C:\QQCall\.runtime\service",
        "paths": {"qq_exe": r"C:\QQ\QQ.exe"},
    }


def _manifest(
    *,
    account_id: str = "12345",
    updated_at: int = NOW_MS - 1_000,
    generation: int | str = 17,
    connected: bool = True,
) -> dict:
    return {
        "schema": 1,
        "adapter": "snowluma",
        "accountId": account_id,
        "connected": connected,
        "adapterPid": 400,
        "adapterCreationTime100ns": "4000",
        "connectionGeneration": generation,
        "updatedAtMs": updated_at,
    }


def _binding(*, kernel_pid: int = 500, kernel_filetime: int = 5000, app_path: str = APP_PATH):
    return SimpleNamespace(
        adapter_pid=400,
        adapter_creation_time_100ns=4000,
        kernel_pid=kernel_pid,
        kernel_creation_time_100ns=kernel_filetime,
        app_path=app_path,
    )


class _FakeSystem:
    def __init__(self, binding=None, *, bind_error: Exception | None = None, current: bool = True):
        self.binding = binding or _binding()
        self.bind_error = bind_error
        self.current = current
        self.bind_calls = 0
        self.revalidate_calls = 0

    def bind_adapter(self, config, manifest):
        self.bind_calls += 1
        if self.bind_error is not None:
            raise self.bind_error
        return self.binding

    def revalidate_binding(self, config, manifest, binding):
        self.revalidate_calls += 1
        return self.current and binding == self.binding


def _dependencies(
    *,
    manifests: list[dict | None] | None = None,
    configs: list[dict] | None = None,
    system: _FakeSystem | None = None,
    now_ms: int = NOW_MS,
) -> _Dependencies:
    manifest_values = [copy.deepcopy(item) for item in (manifests or [_manifest()] * 3)]
    config_values = [copy.deepcopy(item) for item in (configs or [_config()] * 3)]
    reads = {"manifest": 0, "config": 0}

    def load_config(path: Path) -> dict:
        if path != Path(r"C:\canonical\config.toml"):
            raise AssertionError("unexpected config path")
        index = min(reads["config"], len(config_values) - 1)
        reads["config"] += 1
        return copy.deepcopy(config_values[index])

    def read_manifest(config: dict) -> dict | None:
        index = min(reads["manifest"], len(manifest_values) - 1)
        reads["manifest"] += 1
        value = manifest_values[index]
        return copy.deepcopy(value)

    return _Dependencies(
        config_path=Path(r"C:\canonical\config.toml"),
        load_config=load_config,
        read_manifest=read_manifest,
        manifest_fingerprint=manager._manifest_fingerprint,
        system_factory=lambda: system or _FakeSystem(),
        now_ms=lambda: now_ms,
    )


def _run(raw: bytes, dependencies: _Dependencies | None = None, *, argv=()) -> tuple[int, str]:
    output = io.StringIO()
    code = main(argv, stdin=io.BytesIO(raw), stdout=output, dependencies=dependencies)
    return code, output.getvalue()


def _request_bytes(request: dict | None = None) -> bytes:
    return json.dumps(REQUEST if request is None else request, separators=(",", ":")).encode("utf-8")


class RendererProbeAdmissionTests(unittest.TestCase):
    def test_success_has_exact_public_shape_and_uses_read_only_binding(self) -> None:
        system = _FakeSystem()
        code, output = _run(_request_bytes(), _dependencies(system=system))
        self.assertEqual(code, 0)
        value = json.loads(output)
        self.assertEqual(
            set(value),
            {"schema", "ok", "code", "manifestAgeMs", "admissionFingerprint"},
        )
        self.assertEqual(value["schema"], 1)
        self.assertIs(value["ok"], True)
        self.assertIsNone(value["code"])
        self.assertEqual(value["manifestAgeMs"], 1_000)
        self.assertRegex(value["admissionFingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(system.bind_calls, 1)
        self.assertEqual(system.revalidate_calls, 1)

    def test_rejects_duplicate_json_keys_and_unknown_or_missing_fields(self) -> None:
        duplicate = b'{"schema":1,"schema":1}'
        self.assert_failure(duplicate, "input_invalid")
        for request in (
            {key: value for key, value in REQUEST.items() if key != "appPath"},
            {**REQUEST, "unexpected": "value"},
        ):
            self.assert_failure(_request_bytes(request), "input_invalid")

    def test_rejects_oversize_stdin_without_parsing_it(self) -> None:
        self.assertGreater(MAX_STDIN_BYTES, 0)
        self.assert_failure(b" " * (MAX_STDIN_BYTES + 1), "input_invalid")

    def test_rejects_noncanonical_request_types_and_values(self) -> None:
        malformed = (
            {**REQUEST, "schema": True},
            {**REQUEST, "targetPid": True},
            {**REQUEST, "targetPid": 0},
            {**REQUEST, "targetPid": 0x80000000},
            {**REQUEST, "targetFiletime": "05000"},
            {**REQUEST, "targetFiletime": str((1 << 64))},
            {**REQUEST, "appPath": "relative\\app"},
            {**REQUEST, "appPath": r"\rooted-without-drive"},
            {**REQUEST, "appPath": APP_PATH + "\x00hidden"},
            {**REQUEST, "brokerGeneration": True},
            {**REQUEST, "brokerGeneration": 0},
            {**REQUEST, "fingerprintKey": "A" * 64},
            {**REQUEST, "fingerprintKey": "a" * 62},
        )
        for request in malformed:
            with self.subTest(request=request):
                self.assert_failure(_request_bytes(request), "input_invalid")

    def test_rejects_cli_arguments(self) -> None:
        code, output = _run(_request_bytes(), argv=("--config", r"C:\other\config.toml"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output), {"schema": 1, "ok": False, "code": "input_invalid"})

    def test_requires_enabled_snowluma_and_exact_account_match(self) -> None:
        self.assert_failure(
            _request_bytes(),
            "admission_disabled",
            _dependencies(configs=[_config(enabled=False)] * 3),
        )
        self.assert_failure(
            _request_bytes(),
            "adapter_mismatch",
            _dependencies(configs=[{**_config(), "adapter": "napcat"}] * 3),
        )
        self.assert_failure(
            _request_bytes(),
            "account_mismatch",
            _dependencies(manifests=[_manifest(account_id="99999")] * 3),
        )

    def test_requires_connected_fresh_manifest_and_rejects_changed_identity(self) -> None:
        self.assert_failure(
            _request_bytes(),
            "manifest_unavailable",
            _dependencies(manifests=[_manifest(connected=False)] * 3),
        )
        self.assert_failure(
            _request_bytes(),
            "manifest_stale",
            _dependencies(manifests=[_manifest(updated_at=NOW_MS - 10_001)] * 3),
        )
        self.assert_failure(
            _request_bytes(),
            "manifest_stale",
            _dependencies(manifests=[_manifest(), _manifest(), _manifest(updated_at=NOW_MS - 10_001)]),
        )
        self.assert_failure(
            _request_bytes(),
            "binding_changed",
            _dependencies(manifests=[_manifest(), _manifest(generation=18), _manifest(generation=18)]),
        )

    def test_generation_uses_manager_validation_for_integer_and_string_forms(self) -> None:
        for invalid in (True, False, 1.0, 0, -1, 9_007_199_254_740_992):
            with self.subTest(generation=invalid):
                self.assert_failure(
                    _request_bytes(),
                    "manifest_invalid",
                    _dependencies(manifests=[_manifest(generation=invalid)] * 3),
                )

        int_generation = _manifest(generation=17)
        string_generation = _manifest(generation="snow-17")
        _, integer_output = _run(_request_bytes(), _dependencies(manifests=[int_generation] * 3))
        _, string_output = _run(_request_bytes(), _dependencies(manifests=[string_generation] * 3))
        self.assertIs(json.loads(integer_output)["ok"], True)
        self.assertIs(json.loads(string_output)["ok"], True)
        self.assertNotEqual(
            json.loads(integer_output)["admissionFingerprint"],
            json.loads(string_output)["admissionFingerprint"],
        )

    def test_rejects_config_change_after_binding(self) -> None:
        initial = _config()
        changed = {**_config(), "runtime_dir": r"C:\other\runtime"}
        self.assert_failure(
            _request_bytes(),
            "config_changed",
            _dependencies(configs=[initial, changed, changed]),
        )

    def test_rejects_personal_pid_filetime_and_app_path_pins(self) -> None:
        for request in (
            {**REQUEST, "targetPid": 501},
            {**REQUEST, "targetFiletime": "5001"},
            {**REQUEST, "appPath": r"C:\QQ\OtherProfile\resources\app"},
        ):
            with self.subTest(request=request):
                self.assert_failure(_request_bytes(request), "target_mismatch")

    def test_app_path_pin_uses_windows_case_and_separator_canonicalization(self) -> None:
        request = {**REQUEST, "appPath": r"c:/qq/versions/9.9.26-44343/resources/app/"}
        code, output = _run(_request_bytes(request), _dependencies())
        self.assertEqual(code, 0)
        self.assertIs(json.loads(output)["ok"], True)

    def test_binding_and_private_profile_failures_remain_safe(self) -> None:
        for private_error in (
            PermissionError("private Snow token C:\\secrets\\token.json"),
            RuntimeError("profile C:\\Users\\Someone\\private and account 12345"),
        ):
            with self.subTest(error=type(private_error).__name__):
                system = _FakeSystem(bind_error=private_error)
                output_stream = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = main(
                        (),
                        stdin=io.BytesIO(_request_bytes()),
                        stdout=output_stream,
                        dependencies=_dependencies(system=system),
                    )
                output = output_stream.getvalue()
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output), {"schema": 1, "ok": False, "code": "binding_unavailable"})
                self.assertNotIn("token", output.lower())
                self.assertNotIn("someone", output.lower())
                self.assertEqual(stderr.getvalue(), "")

    def test_current_binding_is_revalidated_and_private_exceptions_never_escape(self) -> None:
        system = _FakeSystem(current=False)
        output = io.StringIO()
        stderr = io.StringIO()
        code = main((), stdin=io.BytesIO(_request_bytes()), stdout=output, dependencies=_dependencies(system=system))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"schema": 1, "ok": False, "code": "binding_changed"})
        self.assertEqual(stderr.getvalue(), "")

        dependencies = _dependencies()
        bad = _Dependencies(
            config_path=dependencies.config_path,
            load_config=lambda path: (_ for _ in ()).throw(RuntimeError("private config token=secret")),
            read_manifest=dependencies.read_manifest,
            manifest_fingerprint=dependencies.manifest_fingerprint,
            system_factory=dependencies.system_factory,
            now_ms=dependencies.now_ms,
        )
        _, output_value = _run(_request_bytes(), bad)
        self.assertEqual(json.loads(output_value), {"schema": 1, "ok": False, "code": "config_unavailable"})
        self.assertNotIn("secret", output_value)

    def test_updated_at_is_freshness_only_and_hmac_key_changes_fingerprint(self) -> None:
        first_reads = [_manifest(updated_at=NOW_MS - 900), _manifest(updated_at=NOW_MS - 600), _manifest(updated_at=NOW_MS - 400)]
        second_reads = [_manifest(updated_at=NOW_MS - 800), _manifest(updated_at=NOW_MS - 500), _manifest(updated_at=NOW_MS - 200)]
        _, output_a = _run(_request_bytes(), _dependencies(manifests=first_reads))
        _, output_b = _run(_request_bytes(), _dependencies(manifests=second_reads))
        fingerprint_a = json.loads(output_a)["admissionFingerprint"]
        fingerprint_b = json.loads(output_b)["admissionFingerprint"]
        self.assertEqual(fingerprint_a, fingerprint_b)
        self.assertEqual(json.loads(output_a)["manifestAgeMs"], 400)
        self.assertEqual(json.loads(output_b)["manifestAgeMs"], 200)

        alternate_key_request = {**REQUEST, "fingerprintKey": "b" * 64}
        _, output_c = _run(_request_bytes(alternate_key_request), _dependencies(manifests=first_reads))
        self.assertNotEqual(fingerprint_a, json.loads(output_c)["admissionFingerprint"])

    def test_adapter_and_broker_generations_are_separate_fingerprint_inputs(self) -> None:
        _, base_output = _run(_request_bytes(), _dependencies(manifests=[_manifest(generation=17)] * 3))
        base = json.loads(base_output)["admissionFingerprint"]

        broker_request = {**REQUEST, "brokerGeneration": REQUEST["brokerGeneration"] + 1}
        _, broker_output = _run(_request_bytes(broker_request), _dependencies(manifests=[_manifest(generation=17)] * 3))
        broker_changed = json.loads(broker_output)["admissionFingerprint"]

        _, adapter_output = _run(_request_bytes(), _dependencies(manifests=[_manifest(generation=18)] * 3))
        adapter_changed = json.loads(adapter_output)["admissionFingerprint"]

        self.assertNotEqual(base, broker_changed)
        self.assertNotEqual(base, adapter_changed)
        self.assertNotEqual(broker_changed, adapter_changed)

    def assert_failure(self, raw: bytes, expected_code: str, dependencies: _Dependencies | None = None) -> None:
        code, output = _run(raw, dependencies or _dependencies())
        self.assertEqual(code, 2)
        value = json.loads(output)
        self.assertEqual(value, {"schema": 1, "ok": False, "code": expected_code})


if __name__ == "__main__":
    unittest.main()
