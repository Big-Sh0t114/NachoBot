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
    poke_cooldown_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class DesktopChatConfig:
    enabled: bool = False
    backend_url: str = "http://127.0.0.1:8789"
    request_timeout_seconds: float = 8.0
    reply_timeout_seconds: float = 180.0
    poll_interval_seconds: float = 0.5
    play_audio: bool = True
    tts_language: str = "auto"
    max_input_chars: int = 500
    history_path: Path | None = None
    max_history_messages: int = 100


@dataclass(frozen=True, slots=True)
class DesktopPetConfig:
    enabled: bool = False
    title: str = "NachoBot Live2D"
    always_on_top: bool = True
    hide_from_taskbar: bool = True
    click_through: bool = False
    remember_position: bool = True
    state_path: Path | None = None
    start_position: str = "bottom_right"
    margin: int = 24
    min_scale: float = 0.35
    max_scale: float = 2.5
    tray_icon: bool = True
    left_click_motion: str = "Tap"
    double_click_motion: str = "FlickUp"
    right_click_motion: str = "Flick"
    idle_motion_groups: tuple[str, ...] = ()
    idle_motion_min_seconds: float = 20.0
    idle_motion_max_seconds: float = 45.0
    chat: DesktopChatConfig = field(default_factory=DesktopChatConfig)


@dataclass(frozen=True, slots=True)
class ModelAdaptationConfig:
    enabled: bool = True
    parameter_mappings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    expression_mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    server: ServerConfig
    renderer: RendererConfig
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    desktop_pet: DesktopPetConfig = field(default_factory=DesktopPetConfig)
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


def _resolve_optional_path(raw_path: Any, config_path: Path) -> Path | None:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


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
    runtime_raw = _as_mapping(raw.get("runtime"), "runtime")
    renderer_raw = _as_mapping(raw.get("renderer"), "renderer")
    desktop_pet_raw = _as_mapping(raw.get("desktop_pet"), "desktop_pet")
    desktop_chat_raw = _as_mapping(
        desktop_pet_raw.get("chat"),
        "desktop_pet.chat",
    )
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

    legacy_desktop_enabled = bool(desktop_pet_raw.get("enabled", False))
    runtime_mode = str(
        runtime_raw.get(
            "mode",
            "desktop_pet" if legacy_desktop_enabled else "live",
        )
    ).strip().casefold()
    if runtime_mode not in {"desktop_pet", "live"}:
        raise ConfigError("[runtime].mode must be desktop_pet or live")

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
    min_scale = float(desktop_pet_raw.get("min_scale", 0.35))
    max_scale = float(desktop_pet_raw.get("max_scale", 2.5))
    idle_motion_min = float(desktop_pet_raw.get("idle_motion_min_seconds", 20.0))
    idle_motion_max = float(desktop_pet_raw.get("idle_motion_max_seconds", 45.0))
    chat_request_timeout = float(
        desktop_chat_raw.get("request_timeout_seconds", 8.0)
    )
    chat_reply_timeout = float(desktop_chat_raw.get("reply_timeout_seconds", 180.0))
    chat_poll_interval = float(desktop_chat_raw.get("poll_interval_seconds", 0.5))
    chat_max_input_chars = int(desktop_chat_raw.get("max_input_chars", 500))
    chat_max_history_messages = int(
        desktop_chat_raw.get("max_history_messages", 100)
    )
    chat_tts_language = str(desktop_chat_raw.get("tts_language", "auto")).strip().casefold()

    if width <= 0 or height <= 0:
        raise ConfigError("renderer width and height must be positive")
    if scale <= 0:
        raise ConfigError("[renderer].scale must be positive")
    if cooldown < 0:
        raise ConfigError("[renderer].poke_cooldown_seconds cannot be negative")
    if min_scale <= 0 or max_scale < min_scale:
        raise ConfigError("desktop pet scale bounds are invalid")
    if idle_motion_min < 0 or idle_motion_max < idle_motion_min:
        raise ConfigError("desktop pet idle motion interval is invalid")
    if chat_request_timeout <= 0 or chat_reply_timeout <= 0 or chat_poll_interval <= 0:
        raise ConfigError("desktop pet chat timeouts must be positive")
    if chat_max_input_chars < 1:
        raise ConfigError("[desktop_pet.chat].max_input_chars must be positive")
    if chat_max_history_messages < 1:
        raise ConfigError("[desktop_pet.chat].max_history_messages must be positive")
    if chat_tts_language not in {"auto", "zh", "ja", "en"}:
        raise ConfigError("[desktop_pet.chat].tts_language must be auto, zh, ja, or en")

    start_position = str(
        desktop_pet_raw.get("start_position", "bottom_right")
    ).strip().casefold()
    if start_position not in {"bottom_right", "bottom_left", "center"}:
        raise ConfigError(
            "[desktop_pet].start_position must be bottom_right, bottom_left, or center"
        )

    idle_motion_groups_raw = desktop_pet_raw.get("idle_motion_groups", [])
    if not isinstance(idle_motion_groups_raw, list):
        raise ConfigError("[desktop_pet].idle_motion_groups must be an array of strings")
    idle_motion_groups = tuple(
        str(item).strip() for item in idle_motion_groups_raw if str(item).strip()
    )

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
        runtime=RuntimeConfig(mode=runtime_mode),
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
        desktop_pet=DesktopPetConfig(
            enabled=runtime_mode == "desktop_pet",
            title=str(desktop_pet_raw.get("title", "NachoBot Live2D")).strip()
            or "NachoBot Live2D",
            always_on_top=bool(desktop_pet_raw.get("always_on_top", True)),
            hide_from_taskbar=bool(desktop_pet_raw.get("hide_from_taskbar", True)),
            click_through=bool(desktop_pet_raw.get("click_through", False)),
            remember_position=bool(desktop_pet_raw.get("remember_position", True)),
            state_path=_resolve_optional_path(
                desktop_pet_raw.get("state_path", "desktop_pet_state.json"),
                config_path,
            ),
            start_position=start_position,
            margin=max(0, int(desktop_pet_raw.get("margin", 24))),
            min_scale=min_scale,
            max_scale=max_scale,
            tray_icon=bool(desktop_pet_raw.get("tray_icon", True)),
            left_click_motion=str(
                desktop_pet_raw.get("left_click_motion", "Tap")
            ).strip(),
            double_click_motion=str(
                desktop_pet_raw.get("double_click_motion", "FlickUp")
            ).strip(),
            right_click_motion=str(
                desktop_pet_raw.get("right_click_motion", "Flick")
            ).strip(),
            idle_motion_groups=idle_motion_groups,
            idle_motion_min_seconds=idle_motion_min,
            idle_motion_max_seconds=idle_motion_max,
            chat=DesktopChatConfig(
                enabled=bool(desktop_chat_raw.get("enabled", False)),
                backend_url=str(
                    desktop_chat_raw.get("backend_url", "http://127.0.0.1:8789")
                ).strip().rstrip("/"),
                request_timeout_seconds=chat_request_timeout,
                reply_timeout_seconds=chat_reply_timeout,
                poll_interval_seconds=chat_poll_interval,
                play_audio=bool(desktop_chat_raw.get("play_audio", True)),
                tts_language=chat_tts_language,
                max_input_chars=chat_max_input_chars,
                history_path=_resolve_optional_path(
                    desktop_chat_raw.get(
                        "history_path", "desktop_pet_chat_history.json"
                    ),
                    config_path,
                ),
                max_history_messages=chat_max_history_messages,
            ),
        ),
        adaptation=ModelAdaptationConfig(
            enabled=bool(adaptation_raw.get("enabled", True)),
            parameter_mappings=parameter_mappings,
            expression_mappings=expression_mappings,
        ),
        action_mappings=action_mappings,
        log_level=log_level,
    )
