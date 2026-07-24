"""Configuration loading for the standalone NachoBot Live2D adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

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
class AdapterConfig:
    server: ServerConfig
    renderer: RendererConfig
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
    actions_raw = _as_mapping(raw.get("actions"), "actions")
    logging_raw = _as_mapping(raw.get("logging"), "logging")

    host = str(server_raw.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    port = int(server_raw.get("port", 8766))
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
        action_mappings=action_mappings,
        log_level=log_level,
    )
