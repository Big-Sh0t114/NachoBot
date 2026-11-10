"""Narrow configuration for the independent Live2D application."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when standalone Live2D configuration is invalid."""


DEFAULT_ACTION_MAPPINGS = {
    "NOD": "Nod",
    "SHAKE_HEAD": "Shake",
    "TURN_LEFT": "TurnLeft",
    "TURN_RIGHT": "TurnRight",
    "WINK": "Wink",
    "HAPPY": "Sway",
    "TILT_HEAD": "TiltHead",
    "LOOK_AWAY": "LookAway",
}

_LOG_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    token: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    mode: str = "live"


@dataclass(frozen=True, slots=True)
class RendererConfig:
    model_path: Path
    transparent: bool = True
    antialiasing: bool = True
    width: int = 800
    height: int = 600
    scale: float = 1.0
    track_mouse: bool = False
    hidden: bool = False
    poke_cooldown_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class AdaptationConfig:
    enabled: bool = True
    parameter_mappings: dict[str, str | tuple[str, ...]] = field(default_factory=dict)
    expression_mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StandaloneConfig:
    server: ServerConfig
    runtime: RuntimeConfig
    renderer: RendererConfig
    adaptation: AdaptationConfig = field(default_factory=AdaptationConfig)
    action_mappings: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACTION_MAPPINGS))
    log_level: str = "INFO"

    def resolve_action(self, action_id: str) -> str | None:
        return self.action_mappings.get(action_id.strip().upper())


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _boolean(raw: dict[str, Any], key: str, default: bool, section: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{section}].{key} must be true or false")
    return value


def _integer(raw: dict[str, Any], key: str, default: int, section: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{section}].{key} must be an integer")
    return value


def _mapping(raw: dict[str, Any], section: str, key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{section}.{key}] must be a TOML table")
    return value


def load_config(
    path: str | Path | None = None,
    *,
    model_override: str | Path | None = None,
    hidden_override: bool | None = None,
) -> StandaloneConfig:
    """Load only server, Live2D renderer, adaptation, and action settings.

    A missing implicit config is permitted for wheel installs when ``--model``
    is supplied. Explicit config paths fail closed when malformed or absent.
    """

    default_path = Path(__file__).resolve().parent.parent / "config.toml"
    explicit_path = path is not None
    config_path = Path(path).expanduser().resolve() if explicit_path else default_path
    if config_path.exists():
        try:
            with config_path.open("rb") as source:
                raw = tomllib.load(source)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    elif explicit_path:
        raise ConfigError(f"config file not found: {config_path}")
    else:
        raw = {}

    if not isinstance(raw, dict):
        raise ConfigError("config must contain TOML tables")
    server_raw = _table(raw, "server")
    runtime_raw = _table(raw, "runtime")
    renderer_raw = _table(raw, "renderer")
    adaptation_raw = _table(raw, "adaptation")
    logging_raw = _table(raw, "logging")

    host = server_raw.get("host", "127.0.0.1")
    token = server_raw.get("token", "")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("[server].host must be a non-empty string")
    if not isinstance(token, str):
        raise ConfigError("[server].token must be a string")
    port = _integer(server_raw, "port", 8766, "server")
    if not 1 <= port <= 65535:
        raise ConfigError("[server].port must be between 1 and 65535")

    mode = runtime_raw.get("mode", "live")
    if mode != "live":
        raise ConfigError("[runtime].mode must be 'live' for the independent Live2D app")

    configured_model = renderer_raw.get("model_path")
    model_value = model_override if model_override is not None else configured_model
    model_text = str(model_value or "").strip()
    if not model_text:
        raise ConfigError("set renderer.model_path or pass --model")
    model_path = Path(model_text).expanduser()
    if model_override is None and not model_path.is_absolute():
        model_path = config_path.parent / model_path
    model_path = model_path.resolve()

    width = _integer(renderer_raw, "width", 800, "renderer")
    height = _integer(renderer_raw, "height", 600, "renderer")
    if not 1 <= width <= 8192 or not 1 <= height <= 8192:
        raise ConfigError("[renderer].width and height must be between 1 and 8192")
    scale_value = renderer_raw.get("scale", 1.0)
    if isinstance(scale_value, bool):
        raise ConfigError("[renderer].scale must be a finite positive number")
    try:
        scale = float(scale_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError("[renderer].scale must be a finite positive number") from exc
    if not math.isfinite(scale) or not 0.01 <= scale <= 20:
        raise ConfigError("[renderer].scale must be finite and between 0.01 and 20")
    cooldown_value = renderer_raw.get("poke_cooldown_seconds", 10.0)
    if isinstance(cooldown_value, bool):
        raise ConfigError("[renderer].poke_cooldown_seconds must be a finite non-negative number")
    try:
        cooldown = float(cooldown_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(
            "[renderer].poke_cooldown_seconds must be a finite non-negative number"
        ) from exc
    if not math.isfinite(cooldown) or cooldown < 0:
        raise ConfigError("[renderer].poke_cooldown_seconds must be finite and non-negative")

    parameter_mappings: dict[str, str | tuple[str, ...]] = {}
    for key, value in _mapping(adaptation_raw, "adaptation", "parameters").items():
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ConfigError("[adaptation.parameters] keys must not be empty")
        if isinstance(value, str) and value.strip():
            parameter_mappings[normalized_key] = value.strip()
        elif isinstance(value, list) and value and all(
            isinstance(item, str) and item.strip() for item in value
        ):
            parameter_mappings[normalized_key] = tuple(item.strip() for item in value)
        else:
            raise ConfigError(f"[adaptation.parameters].{normalized_key} must be a string or string array")

    expression_mappings: dict[str, str] = {}
    for key, value in _mapping(adaptation_raw, "adaptation", "expressions").items():
        if not isinstance(value, str) or not str(key).strip() or not value.strip():
            raise ConfigError("[adaptation.expressions] keys and values must be non-empty strings")
        expression_mappings[str(key).strip()] = value.strip()

    action_mappings: dict[str, str] = dict(DEFAULT_ACTION_MAPPINGS)
    for key, value in _mapping(raw, "actions", "actions").items():
        action_id = str(key).strip().upper()
        if not action_id or not isinstance(value, str) or not value.strip():
            raise ConfigError("[actions] keys and values must be non-empty strings")
        action_mappings[action_id] = value.strip()

    log_level = logging_raw.get("level", "INFO")
    if not isinstance(log_level, str) or log_level.strip().upper() not in _LOG_LEVELS:
        raise ConfigError("[logging].level must be a supported log level")

    return StandaloneConfig(
        server=ServerConfig(host=host.strip(), port=port, token=token),
        runtime=RuntimeConfig(mode="live"),
        renderer=RendererConfig(
            model_path=model_path,
            transparent=_boolean(renderer_raw, "transparent", True, "renderer"),
            antialiasing=_boolean(renderer_raw, "antialiasing", True, "renderer"),
            width=width,
            height=height,
            scale=scale,
            track_mouse=_boolean(renderer_raw, "track_mouse", False, "renderer"),
            hidden=(hidden_override if hidden_override is not None else
                    _boolean(renderer_raw, "hidden", False, "renderer")),
            poke_cooldown_seconds=cooldown,
        ),
        adaptation=AdaptationConfig(
            enabled=_boolean(adaptation_raw, "enabled", True, "adaptation"),
            parameter_mappings=parameter_mappings,
            expression_mappings=expression_mappings,
        ),
        action_mappings=action_mappings,
        log_level=log_level.strip().upper(),
    )
