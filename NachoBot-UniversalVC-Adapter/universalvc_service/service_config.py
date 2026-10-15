"""Service-owned bind, authentication, model, and resource settings.

Session capture/output/denoise/speaker choices are intentionally not read here;
each session must declare them in its own API request.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ServiceConfigError(ValueError):
    """A sanitized service startup or configuration error."""


_ADAPTER_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServiceSettings:
    bind_host: str
    port: int
    token: str
    core_host: str
    core_port: int
    core_token: str
    vad_model_path: Path
    speaker_model_path: Path
    speaker_db_root: Path
    max_sessions: int = 8
    max_sources_per_session: int = 4
    max_total_sources: int = 32
    source_queue_size: int = 32
    session_idle_timeout_seconds: float = 30.0
    heartbeat_interval_seconds: float = 10.0
    max_utterance_seconds: float = 60.0


def _read_config(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ServiceConfigError("UniversalVC service config file was not found") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ServiceConfigError("UniversalVC service config file is invalid") from exc
    if not isinstance(value, dict):
        raise ServiceConfigError("UniversalVC service config must be a TOML table")
    return value


def _table(root: dict, name: str) -> dict:
    value = root.get(name, {})
    if not isinstance(value, dict):
        raise ServiceConfigError(f"UniversalVC [{name}] config must be a table")
    return value


def _path(value: object, default: Path, base: Path) -> Path:
    if value is None:
        return default.resolve()
    if not isinstance(value, str) or not value.strip():
        raise ServiceConfigError("UniversalVC model path must be a non-empty string")
    result = Path(value).expanduser()
    if not result.is_absolute():
        result = base / result
    return result.resolve()


def load_service_settings(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ServiceSettings:
    """Load only service process configuration, never adapter ``config.toml``."""

    env = os.environ if environ is None else environ
    path = Path(config_path).expanduser().resolve() if config_path is not None else None
    data = _read_config(path)
    server = _table(data, "server")
    core = _table(data, "core")
    models = _table(data, "models")
    resources = _table(data, "resources")
    base = path.parent if path is not None else _ADAPTER_ROOT

    host = env.get("NACHOBOT_UNIVERSALVC_HOST", server.get("host", "127.0.0.1"))
    if not isinstance(host, str) or not host:
        raise ServiceConfigError("UniversalVC bind host must be a loopback IP literal")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ServiceConfigError("UniversalVC bind host must be a loopback IP literal") from exc
    if not address.is_loopback:
        raise ServiceConfigError("UniversalVC service may bind only to a loopback IP literal")

    raw_port = env.get("NACHOBOT_UNIVERSALVC_PORT", server.get("port", 8016))
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ServiceConfigError("UniversalVC service port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ServiceConfigError("UniversalVC service port is outside the valid range")

    token = env.get("NACHOBOT_UNIVERSALVC_TOKEN", server.get("token", ""))
    if (
        not isinstance(token, str)
        or not token.strip()
        or len(token) > 512
        or token.strip().upper() in {"REPLACE_WITH_A_RANDOM_SECRET", "CHANGE_ME"}
    ):
        raise ServiceConfigError(
            "Set NACHOBOT_UNIVERSALVC_TOKEN or replace server.token before starting the service"
        )

    core_host = env.get("NACHOBOT_CORE_HOST", core.get("host", "127.0.0.1"))
    if not isinstance(core_host, str) or not core_host.strip():
        raise ServiceConfigError("UniversalVC Core host is invalid")
    raw_core_port = env.get("NACHOBOT_CORE_PORT", core.get("port", 8000))
    try:
        core_port = int(raw_core_port)
    except (TypeError, ValueError) as exc:
        raise ServiceConfigError("UniversalVC Core port must be an integer") from exc
    if not 1 <= core_port <= 65535:
        raise ServiceConfigError("UniversalVC Core port is outside the valid range")
    core_token = env.get("NACHOBOT_CORE_TOKEN", core.get("token", ""))
    if not isinstance(core_token, str):
        raise ServiceConfigError("UniversalVC Core token is invalid")

    vad_model_path = _path(
        models.get("vad_model_path"),
        _ADAPTER_ROOT / "models" / "silero_vad.onnx",
        base,
    )
    speaker_model_path = _path(
        models.get("speaker_model_path"),
        _ADAPTER_ROOT / "models" / "wespeaker_resnet34.onnx",
        base,
    )
    speaker_db_root = _path(
        models.get("speaker_db_root"),
        _ADAPTER_ROOT / "service_data" / "speakers",
        base,
    )

    def bounded_integer(name: str, default: int, low: int, high: int) -> int:
        value = resources.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ServiceConfigError(f"UniversalVC resource setting {name} is invalid")
        return value

    max_sessions = bounded_integer("max_sessions", 8, 1, 8)
    max_sources = bounded_integer("max_sources_per_session", 4, 1, 4)
    max_total_sources = bounded_integer("max_total_sources", 32, 1, 32)
    queue_size = bounded_integer("source_queue_size", 32, 1, 32)

    return ServiceSettings(
        bind_host=address.compressed,
        port=port,
        token=token.strip(),
        core_host=core_host.strip(),
        core_port=core_port,
        core_token=core_token.strip(),
        vad_model_path=vad_model_path,
        speaker_model_path=speaker_model_path,
        speaker_db_root=speaker_db_root,
        max_sessions=max_sessions,
        max_sources_per_session=max_sources,
        max_total_sources=max_total_sources,
        source_queue_size=queue_size,
    )
