"""Configuration loading for the standalone NachoBot Live2D adapter."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError as exc:  # pragma: no cover - Python 3.11+ is required
    raise RuntimeError("NachoBot Live2D Adapter requires Python 3.11 or newer") from exc


DEFAULT_ACTION_MAPPINGS: dict[str, str] = {
    "NOD": "Nod",
    "SHAKE_HEAD": "Shake",
    "TURN_LEFT": "TurnLeft",
    "TURN_RIGHT": "TurnRight",
    "WINK": "Wink",
    "HAPPY": "Sway",
    "TILT_HEAD": "TiltHead",
    "LOOK_AWAY": "LookAway",
}


class ConfigError(ValueError):
    """Raised when the Live2D adapter configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    token: str = ""


@dataclass(frozen=True, slots=True)
class RendererConfig:
    model_path: Path
    transparent: bool = True
    antialiasing: bool = True
    width: int = 800
    height: int = 600
    scale: float = 1.0
    track_mouse: bool = False
    poke_cooldown_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class ModelAdaptationConfig:
    enabled: bool = True
    parameter_mappings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    expression_mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    server: ServerConfig
    renderer: RendererConfig
    adaptation: ModelAdaptationConfig = field(default_factory=ModelAdaptationConfig)
    action_mappings: dict[str, str] = field(default_factory=dict)
    log_level: str = "INFO"

    def resolve_action(self, action_id: str) -> str | None:
        """Resolve a canonical action ID to a model-specific motion group."""
        return self.action_mappings.get(action_id.strip().upper())


def _as_mapping(value: Any, section_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{section_name}] must be a TOML table")
    return value


def _resolve_model_path(raw_path: Any, config_path: Path) -> Path:
    model_path_text = str(raw_path or "").strip()
    if not model_path_text:
        raise ConfigError("[renderer].model_path is required")

    model_path = Path(model_path_text).expanduser()
    if not model_path.is_absolute():
        model_path = config_path.parent / model_path

    return model_path.resolve()


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ConfigError(f"{field_name} must be a string or an array of strings")
        normalized = item.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    if not result:
        raise ConfigError(f"{field_name} cannot be empty")
    return tuple(result)


def load_config(path: str | Path) -> AdapterConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    server_raw = _as_mapping(raw.get("server"), "server")
    renderer_raw = _as_mapping(raw.get("renderer"), "renderer")
    adaptation_raw = _as_mapping(raw.get("adaptation"), "adaptation")
    parameter_mappings_raw = _as_mapping(
        adaptation_raw.get("parameters"),
        "adaptation.parameters",
    )
    expression_mappings_raw = _as_mapping(
        adaptation_raw.get("expressions"),
        "adaptation.expressions",
    )
    actions_raw = _as_mapping(raw.get("actions"), "actions")
    logging_raw = _as_mapping(raw.get("logging"), "logging")

    host = os.getenv(
        "NACHOBOT_LIVE2D_HOST",
        str(server_raw.get("host", "127.0.0.1")),
    ).strip() or "127.0.0.1"
    port = int(os.getenv("NACHOBOT_LIVE2D_PORT", str(server_raw.get("port", 8766))))
    if not 1 <= port <= 65535:
        raise ConfigError("[server].port must be between 1 and 65535")

    width = int(renderer_raw.get("width", 800))
    height = int(renderer_raw.get("height", 600))
    scale = float(renderer_raw.get("scale", 1.0))
    cooldown = float(renderer_raw.get("poke_cooldown_seconds", 10.0))

    if width <= 0 or height <= 0:
        raise ConfigError("renderer width and height must be positive")
    if scale <= 0:
        raise ConfigError("[renderer].scale must be positive")
    if cooldown < 0:
        raise ConfigError("[renderer].poke_cooldown_seconds cannot be negative")

    action_mappings = dict(DEFAULT_ACTION_MAPPINGS)
    for action_id, motion_group in actions_raw.items():
        normalized_id = str(action_id).strip().upper()
        normalized_group = str(motion_group).strip()
        if normalized_id and normalized_group:
            action_mappings[normalized_id] = normalized_group

    parameter_mappings = {
        str(canonical).strip().upper(): _string_tuple(
            value,
            f"[adaptation.parameters].{canonical}",
        )
        for canonical, value in parameter_mappings_raw.items()
        if str(canonical).strip()
    }
    expression_mappings: dict[str, str] = {}
    for emotion, expression in expression_mappings_raw.items():
        normalized_emotion = str(emotion).strip().casefold()
        if not isinstance(expression, str):
            raise ConfigError(
                f"[adaptation.expressions].{emotion} must be a string"
            )
        normalized_expression = expression.strip()
        if normalized_emotion and normalized_expression:
            expression_mappings[normalized_emotion] = normalized_expression

    log_level = str(logging_raw.get("level", "INFO")).strip().upper() or "INFO"

    return AdapterConfig(
        server=ServerConfig(
            host=host,
            port=port,
            token=str(server_raw.get("token", "")),
        ),
        renderer=RendererConfig(
            model_path=_resolve_model_path(renderer_raw.get("model_path"), config_path),
            transparent=bool(renderer_raw.get("transparent", True)),
            antialiasing=bool(renderer_raw.get("antialiasing", True)),
            width=width,
            height=height,
            scale=scale,
            track_mouse=bool(renderer_raw.get("track_mouse", False)),
            poke_cooldown_seconds=cooldown,
        ),
        adaptation=ModelAdaptationConfig(
            enabled=bool(adaptation_raw.get("enabled", True)),
            parameter_mappings=parameter_mappings,
            expression_mappings=expression_mappings,
        ),
        action_mappings=action_mappings,
        log_level=log_level,
    )
