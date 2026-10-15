"""Public, bounded TOML configuration for the supervised QQCall service.

This module intentionally uses only the Python standard library at import time
so the adapters can load it without importing the supervisor or platform APIs.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomllib

SCHEMA = 6
MAX_CONFIG_BYTES = 64 * 1024
MAX_STRING_CHARS = 4096
_UIN = re.compile(r"^[1-9][0-9]{0,19}$")
_UID = re.compile(r"^u_[A-Za-z0-9_-]{22}$")

# This is the raw schema-6 document shape. Path defaults are relative to the
# canonical plugin TOML directory and resolve to the existing project layout.
DEFAULT_CONFIG: dict[str, Any] = {
    "schema": 6,
    "plugin": {"enabled": False},
    "qqcall": {
        "adapter": "auto",
        "account_id": "",
        "identity_file": "",
        "onebot": {"url": "", "token_file": ""},
        "paths": {
            "qq_exe": "",
            "loader_root": "../../../NapCat.Shell",
            "python_exe": "../../.venv/Scripts/python.exe",
            "node_exe": "node",
        },
        "ports": {"host": 6111, "callback": 6110, "control": 6120},
        "runtime_dir": "../../../NachoBot-QQCall/.runtime/service",
        "audio": {
            "capture_device": "",
            "playback_device": "",
        },
        "universal_api": {
            "url": "http://127.0.0.1:8016",
            "token_file": "",
        },
        "policy": {"auto_accept": False},
        "peer_map": {},
        "supervision": {
            "poll_interval_sec": 2,
            "retry_initial_sec": 2,
            "retry_max_sec": 30,
            "max_failures_per_generation": 3,
        },
        "native": {"shim_variant": "recovery2", "guard_config_file": ""},
        "allow_planner_accept": False,
        "group_intent_model_task": "utils",
        "group_intent_timeout_seconds": 10,
    },
}


class ConfigError(ValueError):
    """A safe, stable configuration error with no source-value echo."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _exact_keys(value: Any, expected: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ConfigError(code)
    return value


def _string(
    value: Any,
    code: str,
    *,
    maximum: int = MAX_STRING_CHARS,
    allow_empty: bool = True,
) -> str:
    if type(value) is not str or len(value) > maximum or "\x00" in value:
        raise ConfigError(code)
    if not allow_empty and not value.strip():
        raise ConfigError(code)
    return value


def _boolean(value: Any, code: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(code)
    return value


def _integer(value: Any, code: str, low: int, high: int) -> int:
    if type(value) is not int or value < low or value > high:
        raise ConfigError(code)
    return value


def _finite_number(value: Any, code: str, low: float, high: float) -> float | int:
    try:
        finite = math.isfinite(value) if type(value) in (int, float) else False
    except OverflowError:
        finite = False
    if not finite or value < low or value > high:
        raise ConfigError(code)
    return value


def _validate_onebot_url(value: str) -> None:
    if not value:
        return
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigError("onebot_url_invalid") from None
    if (
        parsed.scheme not in ("http", "https", "ws", "wss")
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\x00" in value
    ):
        raise ConfigError("onebot_url_invalid")
    if host.lower() not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError("onebot_url_must_be_loopback")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("onebot_url_invalid")


def _validate_universal_api_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigError("universal_api_url_invalid") from None
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ConfigError("universal_api_url_invalid")
    if host.lower() not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError("universal_api_url_must_be_loopback")


def _path(base: Path, value: str, code: str, *, empty_allowed: bool = True) -> str:
    if value == "":
        if empty_allowed:
            return ""
        raise ConfigError(code)
    try:
        candidate = Path(value)
        return str((base / candidate).resolve(strict=False) if not candidate.is_absolute() else candidate.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        raise ConfigError(code) from None


def _normalize(raw: Any, config_dir: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("config_fields_invalid")
    schema = raw.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        raise ConfigError("config_schema_unsupported")
    root = _exact_keys(raw, {"schema", "plugin", "qqcall"}, "config_fields_invalid")

    plugin_raw = _exact_keys(root["plugin"], {"enabled"}, "plugin_fields_invalid")
    enabled = _boolean(plugin_raw["enabled"], "enabled_invalid")

    qqcall_value = root["qqcall"]
    if not isinstance(qqcall_value, dict):
        raise ConfigError("qqcall_fields_invalid")
    required_qqcall_keys = set(DEFAULT_CONFIG["qqcall"]) - {"audio"}
    if not required_qqcall_keys.issubset(qqcall_value) or set(qqcall_value) - (required_qqcall_keys | {"audio"}):
        raise ConfigError("qqcall_fields_invalid")
    qqcall = qqcall_value
    adapter = _string(qqcall["adapter"], "adapter_invalid", maximum=32, allow_empty=False)
    if adapter not in ("auto", "napcat", "snowluma"):
        raise ConfigError("adapter_invalid")

    account_id = _string(qqcall["account_id"], "account_id_invalid", maximum=20)
    identity_file = _string(qqcall["identity_file"], "identity_file_invalid")

    onebot = _exact_keys(qqcall["onebot"], {"url", "token_file"}, "onebot_fields_invalid")
    onebot_url = _string(onebot["url"], "onebot_url_invalid", maximum=2048)
    _validate_onebot_url(onebot_url)
    token_file = _string(onebot["token_file"], "onebot_token_file_invalid")

    universal_api_raw = _exact_keys(qqcall["universal_api"], {"url", "token_file"}, "universal_api_fields_invalid")
    universal_api_url = _string(universal_api_raw["url"], "universal_api_url_invalid", maximum=2048, allow_empty=False)
    _validate_universal_api_url(universal_api_url)
    universal_api_token_file = _string(universal_api_raw["token_file"], "universal_api_token_file_invalid")

    paths_raw = _exact_keys(qqcall["paths"], {"qq_exe", "loader_root", "python_exe", "node_exe"}, "paths_fields_invalid")
    qq_exe = _string(paths_raw["qq_exe"], "qq_exe_invalid")
    loader_root = _string(paths_raw["loader_root"], "loader_root_invalid", allow_empty=False)
    python_exe = _string(paths_raw["python_exe"], "python_exe_invalid", allow_empty=False)
    node_exe = _string(paths_raw["node_exe"], "node_exe_invalid", maximum=1024, allow_empty=False)

    if enabled:
        if _UIN.fullmatch(account_id) is None:
            raise ConfigError("enabled_account_required")
        if not onebot_url and adapter != "auto":
            raise ConfigError("enabled_identity_and_onebot_required")
        if not qq_exe:
            raise ConfigError("enabled_qq_exe_required")

    ports_raw = _exact_keys(qqcall["ports"], {"host", "callback", "control"}, "ports_fields_invalid")
    ports = {key: _integer(ports_raw[key], "port_invalid", 1, 65535) for key in ("host", "callback", "control")}
    if len(set(ports.values())) != len(ports):
        raise ConfigError("ports_must_differ")

    runtime_dir = _string(qqcall["runtime_dir"], "runtime_dir_invalid", allow_empty=False)

    audio_raw = qqcall.get("audio", {})
    if not isinstance(audio_raw, dict) or set(audio_raw) - {"capture_device", "playback_device"}:
        raise ConfigError("audio_fields_invalid")
    capture_device = _string(audio_raw.get("capture_device", ""), "capture_device_invalid", maximum=256)
    playback_device = _string(audio_raw.get("playback_device", ""), "playback_device_invalid", maximum=256)
    if (capture_device and not capture_device.strip()) or (playback_device and not playback_device.strip()):
        raise ConfigError("audio_device_override_invalid")

    policy_raw = _exact_keys(qqcall["policy"], {"auto_accept"}, "policy_fields_invalid")
    auto_accept = _boolean(policy_raw["auto_accept"], "auto_accept_invalid")

    peer_raw = qqcall["peer_map"]
    if not isinstance(peer_raw, dict) or len(peer_raw) > 64:
        raise ConfigError("peer_map_invalid")
    peer_map: dict[str, str] = {}
    seen_peer_uins: set[str] = set()
    for uid, user in peer_raw.items():
        if type(uid) is not str or _UID.fullmatch(uid) is None:
            raise ConfigError("peer_map_invalid")
        peer_uin = _string(user, "peer_map_invalid", maximum=20, allow_empty=False)
        if _UIN.fullmatch(peer_uin) is None or peer_uin in seen_peer_uins:
            raise ConfigError("peer_map_invalid")
        seen_peer_uins.add(peer_uin)
        peer_map[uid] = peer_uin

    supervision_raw = _exact_keys(
        qqcall["supervision"],
        {"poll_interval_sec", "retry_initial_sec", "retry_max_sec", "max_failures_per_generation"},
        "supervision_fields_invalid",
    )
    poll_interval = _finite_number(supervision_raw["poll_interval_sec"], "poll_interval_invalid", 0.25, 5.0)
    retry_initial = _finite_number(supervision_raw["retry_initial_sec"], "retry_initial_invalid", 0.25, 300.0)
    retry_max = _finite_number(supervision_raw["retry_max_sec"], "retry_max_invalid", 0.25, 3600.0)
    if retry_max < retry_initial:
        raise ConfigError("retry_max_invalid")
    max_failures = _integer(supervision_raw["max_failures_per_generation"], "max_failures_invalid", 1, 20)

    native_raw = _exact_keys(qqcall["native"], {"shim_variant", "guard_config_file"}, "native_fields_invalid")
    shim_variant = _string(native_raw["shim_variant"], "shim_variant_invalid", maximum=32, allow_empty=False)
    if shim_variant != "recovery2":
        raise ConfigError("shim_variant_invalid")
    guard_config_file = _string(native_raw["guard_config_file"], "guard_config_file_invalid")

    allow_planner_accept = _boolean(qqcall["allow_planner_accept"], "allow_planner_accept_invalid")
    group_intent_model_task = _string(
        qqcall["group_intent_model_task"],
        "group_intent_model_task_invalid",
        maximum=128,
        allow_empty=False,
    )
    group_intent_timeout_seconds = _integer(
        qqcall["group_intent_timeout_seconds"], "group_intent_timeout_seconds_invalid", 1, 30
    )

    normalized = {
        "schema": SCHEMA,
        "enabled": enabled,
        "adapter": adapter,
        "account_id": account_id,
        "identity_file": _path(config_dir, identity_file, "identity_file_invalid"),
        "onebot": {
            "url": onebot_url,
            "token_file": _path(config_dir, token_file, "onebot_token_file_invalid"),
        },
        "paths": {
            "qq_exe": _path(config_dir, qq_exe, "qq_exe_invalid"),
            "loader_root": _path(config_dir, loader_root, "loader_root_invalid", empty_allowed=False),
            "python_exe": _path(config_dir, python_exe, "python_exe_invalid", empty_allowed=False),
            "node_exe": node_exe if _is_bare_command(node_exe) else _path(config_dir, node_exe, "node_exe_invalid", empty_allowed=False),
        },
        "ports": ports,
        "runtime_dir": _path(config_dir, runtime_dir, "runtime_dir_invalid", empty_allowed=False),
        "audio": {
            "enabled": enabled,
            "capture_device": capture_device,
            "playback_device": playback_device,
            "capture_mode": "loopback",
            "device_scope": {"capture": "virtual", "playback": "virtual"},
        },
        "universal_api": {
            "url": universal_api_url,
            # The public TOML keeps an empty token_file as the canonical
            # default.  Resolve it only in the runtime view, alongside the
            # private service state; parsing/check/plan never create it.
            "token_file": (
                str((Path(_path(config_dir, runtime_dir, "runtime_dir_invalid", empty_allowed=False)) / "universal-api.token.private").resolve(strict=False))
                if not universal_api_token_file
                else _path(config_dir, universal_api_token_file, "universal_api_token_file_invalid")
            ),
        },
        # Internal marker for the supervisor. It is never read from public
        # TOML and lets startup distinguish its managed default from an
        # explicitly selected shared token file.
        "universal_api_token_file_defaulted": universal_api_token_file == "",
        "policy": {"auto_accept": auto_accept},
        "peer_map": peer_map,
        "supervision": {
            "poll_interval_sec": poll_interval,
            "retry_initial_sec": retry_initial,
            "retry_max_sec": retry_max,
            "max_failures_per_generation": max_failures,
        },
        "native": {
            "shim_variant": shim_variant,
            "guard_config_file": (
                _path(config_dir, guard_config_file, "guard_config_file_invalid") if guard_config_file else
                str((Path(_path(config_dir, runtime_dir, "runtime_dir_invalid", empty_allowed=False)) /
                     "audio-guard-source.private.json").resolve(strict=False))
            ),
        },
        "audio_guard_config_file_defaulted": guard_config_file == "",
        "core": {
            "allow_planner_accept": allow_planner_accept,
            "group_intent_model_task": group_intent_model_task,
            "group_intent_timeout_seconds": group_intent_timeout_seconds,
        },
    }
    return normalized


def _is_bare_command(value: str) -> bool:
    """Keep `node` / `python` as PATH commands; resolve actual relative paths."""
    return not Path(value).is_absolute() and "/" not in value and "\\" not in value and ":" not in value and value not in (".", "..")


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and normalize the sole public schema-6 TOML config.

    Relative paths resolve from the TOML file's parent. Unknown keys, duplicate
    TOML keys, invalid UTF-8, booleans in numeric slots, and oversized files fail
    with stable codes that never echo source values.
    """
    try:
        config_path = Path(path).expanduser().resolve(strict=False)
        with config_path.open("rb") as stream:
            encoded = stream.read(MAX_CONFIG_BYTES + 1)
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("config_unavailable") from None
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ConfigError("config_too_large")
    try:
        source = encoded.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("config_invalid_utf8") from None
    try:
        value = tomllib.loads(source)
    except (tomllib.TOMLDecodeError, ValueError, TypeError):
        raise ConfigError("config_invalid_toml") from None
    return _normalize(value, config_path.parent)


__all__ = ["DEFAULT_CONFIG", "MAX_CONFIG_BYTES", "SCHEMA", "ConfigError", "load_config"]
