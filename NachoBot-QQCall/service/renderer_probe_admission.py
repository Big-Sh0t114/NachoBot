"""Read-only admission gate for the development renderer capability probe.

The command accepts one bounded JSON object on stdin. Production always reads
the canonical QQCall config and SnowLuma manifest through ``service.manager``;
the dependency seam is private and exists only so unit tests can avoid live
configuration, authentication, and process queries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import ntpath
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import service.manager as manager


MAX_STDIN_BYTES = 16 * 1024
MAX_PID = 0x7FFFFFFF
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_FILETIME = (1 << 64) - 1
MAX_WINDOWS_PATH_CHARS = 32_768
MIN_MANIFEST_AGE_MS = -5_000
MAX_MANIFEST_AGE_MS = 10_000

_REQUEST_KEYS = frozenset({
    "schema", "targetPid", "targetFiletime", "appPath", "brokerGeneration", "fingerprintKey",
})
_FILETIME_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_HEX_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_UIN_RE = re.compile(r"^[1-9][0-9]{0,19}$")

_ERROR_CODES = frozenset({
    "input_invalid",
    "config_unavailable",
    "config_invalid",
    "config_changed",
    "admission_disabled",
    "adapter_mismatch",
    "manifest_unavailable",
    "manifest_invalid",
    "manifest_stale",
    "account_mismatch",
    "binding_unavailable",
    "binding_changed",
    "target_mismatch",
    "internal_error",
})


class _AdmissionRejected(Exception):
    def __init__(self, code: str):
        if code not in _ERROR_CODES:
            code = "internal_error"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Dependencies:
    config_path: Path
    load_config: Callable[[Path], dict[str, Any]]
    read_manifest: Callable[[dict[str, Any]], dict[str, Any] | None]
    manifest_fingerprint: Callable[[dict[str, Any]], tuple[Any, ...]]
    system_factory: Callable[[], Any]
    now_ms: Callable[[], int]


def _production_dependencies() -> _Dependencies:
    return _Dependencies(
        config_path=manager.DEFAULT_CONFIG_PATH,
        load_config=manager.load_config,
        read_manifest=manager._read_manifest,
        manifest_fingerprint=manager._manifest_fingerprint,
        system_factory=manager.WindowsSystem,
        now_ms=lambda: time.time_ns() // 1_000_000,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _parse_request_bytes(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > MAX_STDIN_BYTES:
        raise _AdmissionRejected("input_invalid")
    try:
        source = raw.decode("utf-8", errors="strict")
        value = json.loads(
            source,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        raise _AdmissionRejected("input_invalid") from None
    return _validate_request(value)


def _canonical_windows_path(value: str) -> str:
    """Return a case- and separator-normalized fully-qualified Windows path."""
    if type(value) is not str or not value or len(value) > MAX_WINDOWS_PATH_CHARS or "\x00" in value:
        raise _AdmissionRejected("input_invalid")
    drive, _tail = ntpath.splitdrive(value)
    if not drive or not ntpath.isabs(value):
        raise _AdmissionRejected("input_invalid")
    normalized = ntpath.normcase(ntpath.normpath(value))
    if not normalized:
        raise _AdmissionRejected("input_invalid")
    return normalized


def _validate_request(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _REQUEST_KEYS:
        raise _AdmissionRejected("input_invalid")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise _AdmissionRejected("input_invalid")
    pid = value["targetPid"]
    if type(pid) is not int or not 1 <= pid <= MAX_PID:
        raise _AdmissionRejected("input_invalid")
    filetime = value["targetFiletime"]
    if type(filetime) is not str or _FILETIME_RE.fullmatch(filetime) is None:
        raise _AdmissionRejected("input_invalid")
    if int(filetime) > MAX_FILETIME:
        raise _AdmissionRejected("input_invalid")
    app_path = value["appPath"]
    app_path_key = _canonical_windows_path(app_path)
    broker_generation = value["brokerGeneration"]
    if type(broker_generation) is not int or not 1 <= broker_generation <= MAX_SAFE_INTEGER:
        raise _AdmissionRejected("input_invalid")
    key = value["fingerprintKey"]
    if type(key) is not str or _HEX_KEY_RE.fullmatch(key) is None:
        raise _AdmissionRejected("input_invalid")
    return {
        "schema": 1,
        "targetPid": pid,
        "targetFiletime": filetime,
        "appPath": app_path,
        "appPathKey": app_path_key,
        "brokerGeneration": broker_generation,
        "fingerprintKeyBytes": bytes.fromhex(key),
    }


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise _AdmissionRejected("config_invalid") from None


def _config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(config)).hexdigest()


def _read_config(dependencies: _Dependencies) -> dict[str, Any]:
    try:
        config = dependencies.load_config(dependencies.config_path)
    except Exception:
        raise _AdmissionRejected("config_unavailable") from None
    if type(config) is not dict:
        raise _AdmissionRejected("config_invalid")
    if type(config.get("enabled")) is not bool or type(config.get("adapter")) is not str:
        raise _AdmissionRejected("config_invalid")
    if type(config.get("account_id")) is not str or not config["account_id"]:
        raise _AdmissionRejected("config_invalid")
    try:
        _config_digest(config)
    except _AdmissionRejected:
        raise
    return config


def _read_current_manifest(
    dependencies: _Dependencies,
    config: dict[str, Any],
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    try:
        manifest = dependencies.read_manifest(config)
    except Exception:
        raise _AdmissionRejected("manifest_unavailable") from None
    if manifest is None:
        raise _AdmissionRejected("manifest_unavailable")
    expected_keys = {
        "schema", "adapter", "accountId", "connected", "adapterPid",
        "adapterCreationTime100ns", "connectionGeneration", "updatedAtMs",
    }
    if type(manifest) is not dict or set(manifest) != expected_keys:
        raise _AdmissionRejected("manifest_invalid")
    if (
        type(manifest["schema"]) is not int
        or manifest["schema"] != 1
        or type(manifest["adapter"]) is not str
        or type(manifest["accountId"]) is not str
        or type(manifest["connected"]) is not bool
        or type(manifest["adapterPid"]) is not int
        or type(manifest["adapterCreationTime100ns"]) is not str
        or type(manifest["connectionGeneration"]) not in (int, str)
        or type(manifest["updatedAtMs"]) is not int
    ):
        raise _AdmissionRejected("manifest_invalid")
    if (
        not 1 <= manifest["adapterPid"] <= MAX_PID
        or _FILETIME_RE.fullmatch(manifest["adapterCreationTime100ns"]) is None
        or int(manifest["adapterCreationTime100ns"]) > MAX_FILETIME
        or _UIN_RE.fullmatch(manifest["accountId"]) is None
        or not manager._connection_generation(manifest["connectionGeneration"], connected=True)
        or not 0 <= manifest["updatedAtMs"] <= MAX_SAFE_INTEGER
    ):
        raise _AdmissionRejected("manifest_invalid")
    try:
        fingerprint = dependencies.manifest_fingerprint(manifest)
        stable_fingerprint = tuple(fingerprint)
        # Ensure the stable identity can be represented canonically before it
        # is included in the admission HMAC.
        _canonical_json(list(stable_fingerprint))
    except Exception:
        raise _AdmissionRejected("manifest_invalid") from None
    return manifest, stable_fingerprint


def _check_config_and_manifest(
    config: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    if config["enabled"] is not True:
        raise _AdmissionRejected("admission_disabled")
    if config["adapter"] != "snowluma" or manifest["adapter"] != "snowluma":
        raise _AdmissionRejected("adapter_mismatch")
    if manifest["connected"] is not True:
        raise _AdmissionRejected("manifest_unavailable")
    if config["account_id"] != manifest["accountId"]:
        raise _AdmissionRejected("account_mismatch")


def _manifest_age_ms(manifest: dict[str, Any], now_ms: Callable[[], int]) -> int:
    try:
        current = now_ms()
    except Exception:
        raise _AdmissionRejected("internal_error") from None
    if type(current) is not int or not 0 <= current <= MAX_SAFE_INTEGER:
        raise _AdmissionRejected("internal_error")
    age = current - manifest["updatedAtMs"]
    if not MIN_MANIFEST_AGE_MS <= age <= MAX_MANIFEST_AGE_MS:
        raise _AdmissionRejected("manifest_stale")
    return age


def _binding_fields(binding: Any) -> dict[str, Any]:
    try:
        adapter_pid = binding.adapter_pid
        adapter_filetime = binding.adapter_creation_time_100ns
        kernel_pid = binding.kernel_pid
        kernel_filetime = binding.kernel_creation_time_100ns
        app_path = binding.app_path
    except Exception:
        raise _AdmissionRejected("binding_unavailable") from None
    if (
        type(adapter_pid) is not int or not 1 <= adapter_pid <= MAX_PID
        or type(adapter_filetime) is not int or not 1 <= adapter_filetime <= MAX_FILETIME
        or type(kernel_pid) is not int or not 1 <= kernel_pid <= MAX_PID
        or type(kernel_filetime) is not int or not 1 <= kernel_filetime <= MAX_FILETIME
    ):
        raise _AdmissionRejected("binding_unavailable")
    try:
        app_path_key = _canonical_windows_path(app_path)
    except _AdmissionRejected:
        raise _AdmissionRejected("binding_unavailable") from None
    return {
        "adapterPid": adapter_pid,
        "adapterCreationTime100ns": str(adapter_filetime),
        "kernelPid": kernel_pid,
        "kernelCreationTime100ns": str(kernel_filetime),
        "appPath": app_path_key,
    }


def _match_target_pins(request: dict[str, Any], binding: dict[str, Any]) -> None:
    # WindowsSystem's authenticated kernel process is the canonical SnowLuma
    # QQ process. A personal QQ process cannot be admitted by choosing its PID.
    if (
        request["targetPid"] != binding["kernelPid"]
        or request["targetFiletime"] != binding["kernelCreationTime100ns"]
        or request["appPathKey"] != binding["appPath"]
    ):
        raise _AdmissionRejected("target_mismatch")


def _admission_fingerprint(
    request: dict[str, Any],
    config_digest: str,
    manifest_fingerprint: tuple[Any, ...],
    binding: dict[str, Any],
) -> str:
    # Keep SnowLuma's adapter generation and the caller's broker generation in
    # separate, named fields; they belong to independent generation domains.
    payload = {
        "schema": 1,
        "canonicalConfigDigest": config_digest,
        "snowLumaManifestStableFingerprint": list(manifest_fingerprint),
        "targetBinding": binding,
        "controllerPins": {
            "targetPid": request["targetPid"],
            "targetFiletime": request["targetFiletime"],
            "appPath": request["appPathKey"],
            "brokerGeneration": request["brokerGeneration"],
        },
    }
    return hmac.new(
        request["fingerprintKeyBytes"],
        _canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()


def _evaluate_request(request: dict[str, Any], dependencies: _Dependencies) -> dict[str, Any]:
    config = _read_config(dependencies)
    try:
        config_digest = _config_digest(config)
    except _AdmissionRejected:
        raise
    manifest, manifest_fingerprint = _read_current_manifest(dependencies, config)
    _check_config_and_manifest(config, manifest)
    _manifest_age_ms(manifest, dependencies.now_ms)

    try:
        system = dependencies.system_factory()
        binding_object = system.bind_adapter(config, manifest)
    except Exception:
        raise _AdmissionRejected("binding_unavailable") from None
    binding = _binding_fields(binding_object)
    _match_target_pins(request, binding)

    # Re-read both fixed inputs after binding. updatedAtMs is intentionally
    # excluded from the stable manifest fingerprint, but the new timestamp
    # must itself still be current.
    fresh_config = _read_config(dependencies)
    fresh_config_digest = _config_digest(fresh_config)
    fresh_manifest, fresh_fingerprint = _read_current_manifest(dependencies, fresh_config)
    _check_config_and_manifest(fresh_config, fresh_manifest)
    _manifest_age_ms(fresh_manifest, dependencies.now_ms)
    if fresh_config_digest != config_digest:
        raise _AdmissionRejected("config_changed")
    if fresh_fingerprint != manifest_fingerprint:
        raise _AdmissionRejected("binding_changed")

    try:
        if not system.revalidate_binding(fresh_config, fresh_manifest, binding_object):
            raise _AdmissionRejected("binding_changed")
    except _AdmissionRejected:
        raise
    except Exception:
        raise _AdmissionRejected("binding_changed") from None

    final_config = _read_config(dependencies)
    final_config_digest = _config_digest(final_config)
    final_manifest, final_fingerprint = _read_current_manifest(dependencies, final_config)
    _check_config_and_manifest(final_config, final_manifest)
    manifest_age = _manifest_age_ms(final_manifest, dependencies.now_ms)
    if final_config_digest != config_digest:
        raise _AdmissionRejected("config_changed")
    if final_fingerprint != manifest_fingerprint:
        raise _AdmissionRejected("binding_changed")

    return {
        "schema": 1,
        "ok": True,
        "code": None,
        "manifestAgeMs": manifest_age,
        "admissionFingerprint": _admission_fingerprint(
            request,
            config_digest,
            manifest_fingerprint,
            binding,
        ),
    }


def _failure(code: str) -> dict[str, Any]:
    return {"schema": 1, "ok": False, "code": code if code in _ERROR_CODES else "internal_error"}


def _read_request(stream: BinaryIO) -> dict[str, Any]:
    try:
        raw = stream.read(MAX_STDIN_BYTES + 1)
    except Exception:
        raise _AdmissionRejected("input_invalid") from None
    return _parse_request_bytes(raw)


def _write_result(stream: Any, result: dict[str, Any]) -> None:
    stream.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: Any = None,
    dependencies: _Dependencies | None = None,
) -> int:
    """Run the fixed-path CLI; the optional dependencies parameter is test-only."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    if arguments:
        _write_result(output, _failure("input_invalid"))
        return 2
    input_stream = sys.stdin.buffer if stdin is None else stdin
    try:
        request = _read_request(input_stream)
        result = _evaluate_request(
            request,
            _production_dependencies() if dependencies is None else dependencies,
        )
    except _AdmissionRejected as exc:
        result = _failure(exc.code)
    except Exception:
        result = _failure("internal_error")
    _write_result(output, result)
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI caller
    raise SystemExit(main())
