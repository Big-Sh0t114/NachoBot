"""Configuration loading for the standalone NachoBot Live2D adapter."""

from __future__ import annotations

import os
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    """Raised when the desktop-pet adapter configuration is invalid."""


MAX_CONFIG_REDIRECTS = 8


def resolve_config_path(path: str | Path) -> Path:
    """Resolve compatibility config redirects and return the canonical TOML path.

    Redirects are used by the retired Live2D project to hand its old config
    path to the canonical desktop-pet project. Relative redirect targets are
    resolved from the file that declares them. The bounded traversal and
    resolved-path cycle check make malformed redirect chains fail clearly.
    """

    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    config_path = config_path.resolve()
    visited: set[str] = set()

    for redirect_count in range(MAX_CONFIG_REDIRECTS + 1):
        identity = os.path.normcase(os.path.normpath(str(config_path)))
        if identity in visited:
            raise ConfigError(f"configuration redirect cycle detected at {config_path}")
        visited.add(identity)
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")

        try:
            with config_path.open("rb") as config_file:
                raw = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read config file {config_path}: {exc}") from exc

        compatibility = raw.get("compatibility")
        if compatibility is None:
            return config_path
        if not isinstance(compatibility, dict):
            raise ConfigError("[compatibility] must be a TOML table")
        redirect = compatibility.get("config")
        if not isinstance(redirect, str) or not redirect.strip():
            raise ConfigError("[compatibility].config must be a non-empty path")
        if redirect_count == MAX_CONFIG_REDIRECTS:
            raise ConfigError(
                f"configuration redirect chain exceeds {MAX_CONFIG_REDIRECTS} files"
            )
        target = Path(redirect).expanduser()
        if not target.is_absolute():
            target = config_path.parent / target
        config_path = target.resolve()

    raise ConfigError(f"configuration redirect chain exceeds {MAX_CONFIG_REDIRECTS} files")


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
    model_path: Path | None
    transparent: bool = True
    antialiasing: bool = True
    width: int = 800
    height: int = 600
    scale: float = 1.0
    track_mouse: bool = False
    poke_cooldown_seconds: float = 10.0
    backend: str = "live2d"


@dataclass(frozen=True, slots=True)
class SpriteConfig:
    """Settings for the packaged frame-atlas sprite renderer."""

    atlas_path: Path | None = None
    size_px: int = 176
    movement_speed_px_per_second: float = 44.0
    idle_pause_min_seconds: float = 1.5
    idle_pause_max_seconds: float = 4.5


@dataclass(frozen=True, slots=True)
class PerceptionConfig:
    vision: bool = False
    speech: bool = False
    capture_interval_seconds: float = 30.0
    capture_active_window: bool = True
    excluded_exes: tuple[str, ...] = ("obs64.exe", "obs32.exe")
    microphone_device: int | str | None = None
    push_to_talk_key: str = ""
    push_to_talk_mode: str = "hold"


@dataclass(frozen=True, slots=True)
class DesktopChatConfig:
    enabled: bool = False
    transport: str = "http"
    core_url: str = "ws://127.0.0.1:8000/ws"
    core_reply_model_group: str = ""
    proactive: bool = False
    proactive_idle_seconds: float = 300.0
    tts_provider: str = "multimodal"
    tts_url: str = "http://127.0.0.1:9880"
    backend_url: str = "http://127.0.0.1:8789"
    perception: PerceptionConfig = field(default_factory=PerceptionConfig)
    # The chat window is an independent companion surface by default.  It can
    # be made into a docked accessory for users who prefer the old behavior.
    follow_pet: bool = False
    remember_position: bool = True
    window_state_path: Path | None = None
    request_timeout_seconds: float = 8.0
    # A failed Core request is expired by the local host after 45 seconds;
    # keep a small buffer for status polling and network jitter.
    reply_timeout_seconds: float = 60.0
    poll_interval_seconds: float = 0.5
    play_audio: bool = True
    tts_language: str = "zh"
    max_input_chars: int = 500
    history_path: Path | None = None
    max_history_messages: int = 100


@dataclass(frozen=True, slots=True)
class DesktopGuiTarget:
    """An exact UI Automation control identity the user has allowed."""

    process_name: str
    window_title: str
    control_name: str
    control_type: str
    action: str
    automation_id: str = ""


@dataclass(frozen=True, slots=True)
class DesktopInteractionConfig:
    enabled: bool = False
    idle_interval_seconds: float = 90.0
    scene_interval_seconds: float = 1.5
    action_timeout_seconds: float = 3.0
    target_cooldown_seconds: float = 900.0
    window_approach: bool = True
    icon_approach: bool = True
    follow_foreground_window: bool = True
    move_windows: bool = True
    move_icons: bool = True
    gui_inspection: bool = True
    gui_actions: bool = True
    max_window_shift_px: int = 12
    max_icon_shift_px: int = 12
    foot_anchor_x: float = 0.5
    foot_anchor_y: float = 0.9
    gui_targets_path: Path | None = None
    gui_targets: tuple[DesktopGuiTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class DesktopPetConfig:
    enabled: bool = False
    show_desktop_console: bool = False
    show_core_console: bool = False
    title: str = "桃濑日和 · NachoBot 桌宠"
    character_name: str = "日和"
    chat_header: str = "桃濑日和 · NachoBot"
    always_on_top: bool = True
    hide_from_taskbar: bool = True
    click_through: bool = False
    remember_position: bool = True
    state_path: Path | None = None
    start_position: str = "bottom_right"
    margin: int = 24
    min_scale: float = 0.45
    max_scale: float = 1.8
    # Live2D is rendered into a fixed-size transparent viewport.  Keeping the
    # model fitted to that viewport prevents zooming from clipping the body.
    fit_to_window: bool = True
    tray_icon: bool = True
    left_click_motion: str = "Tap"
    double_click_motion: str = "FlickUp"
    right_click_motion: str = "Flick"
    idle_motion_groups: tuple[str, ...] = ("Flick", "FlickDown", "FlickUp", "Tap", "Tap@Body", "Flick@Body")
    idle_motion_min_seconds: float = 18.0
    idle_motion_max_seconds: float = 40.0
    interactions: DesktopInteractionConfig = field(default_factory=DesktopInteractionConfig)
    chat: DesktopChatConfig = field(default_factory=DesktopChatConfig)
    presentation_mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelAdaptationConfig:
    enabled: bool = True
    parameter_mappings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    expression_mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComponentConfig:
    core: bool = True
    tts: bool = False
    perception: bool = False


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    server: ServerConfig
    renderer: RendererConfig
    sprite: SpriteConfig = field(default_factory=SpriteConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    desktop_pet: DesktopPetConfig = field(default_factory=DesktopPetConfig)
    components: ComponentConfig = field(default_factory=ComponentConfig)
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


def model_display_name(model_path: Path | None) -> str:
    """Read the selected model's own metadata, then its directory name."""
    if model_path is None:
        return "Desktop Pet"
    stem = model_path.name.removesuffix(".model3.json").removesuffix(".model.json")
    for path in (model_path.with_name(stem + ".vtube.json"), model_path):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            name = data.get("Name") if isinstance(data, dict) else None
            if isinstance(name, str) and name.strip():
                return name.strip()[:100]
        except (OSError, ValueError):
            continue
    directory = model_path.parent.name
    return (stem if directory.casefold() in {"runtime", "model", "models"} else directory) or stem or "Live2D"


def _resolve_model_path(raw_path: Any, config_path: Path) -> Path | None:
    model_path_text = str(raw_path or "").strip()
    if not model_path_text:
        return None

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


def _parse_gui_targets(value: Any) -> tuple[DesktopGuiTarget, ...]:
    if not isinstance(value, list):
        raise ConfigError("[desktop_pet.interactions].gui_targets must be an array of tables")
    targets: list[DesktopGuiTarget] = []
    allowed_actions = {"invoke", "toggle", "expand"}
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ConfigError(
                f"[desktop_pet.interactions].gui_targets[{index}] must be a table"
            )
        fields = {}
        for key in ("process_name", "window_title", "control_name", "control_type"):
            raw_text = item.get(key)
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise ConfigError(
                    f"[desktop_pet.interactions].gui_targets[{index}].{key} is required"
                )
            text_value = raw_text.strip()
            if len(text_value) > 256:
                raise ConfigError(
                    f"[desktop_pet.interactions].gui_targets[{index}].{key} is too long"
                )
            fields[key] = text_value
        action = item.get("action", "invoke")
        if not isinstance(action, str) or action.casefold() not in allowed_actions:
            raise ConfigError(
                f"[desktop_pet.interactions].gui_targets[{index}].action must be invoke, toggle, or expand"
            )
        automation_id = item.get("automation_id", "")
        if not isinstance(automation_id, str) or len(automation_id) > 256:
            raise ConfigError(
                f"[desktop_pet.interactions].gui_targets[{index}].automation_id must be a short string"
            )
        targets.append(
            DesktopGuiTarget(
                **fields,
                action=action.casefold(),
                automation_id=automation_id.strip(),
            )
        )
    return tuple(dict.fromkeys(targets))


def load_config(
    path: str | Path,
    *,
    runtime_mode_override: str | None = None,
    renderer_backend_override: str | None = None,
) -> AdapterConfig:
    config_path = resolve_config_path(path)

    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    server_raw = _as_mapping(raw.get("server"), "server")
    runtime_raw = _as_mapping(raw.get("runtime"), "runtime")
    renderer_raw = _as_mapping(raw.get("renderer"), "renderer")
    sprite_raw = _as_mapping(raw.get("sprite"), "sprite")
    components_raw = _as_mapping(raw.get("components"), "components")
    startup_override = os.getenv("NACHOBOT_DESKTOP_STARTUP_CONFIG", "")
    startup_path = (Path(startup_override).expanduser().resolve() if startup_override else
                    config_path.parent.parent / "NachoBot-Multimodal-Adapter" / "startup.toml")
    if startup_path.is_file() or startup_override:
        try:
            with startup_path.open("rb") as file:
                startup = tomllib.load(file)
            components_raw = _as_mapping(startup.get("components"), "components")
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"invalid shared startup config: {startup_path}: {exc}") from exc
    for name in ("core", "tts", "perception"):
        if name in components_raw and type(components_raw[name]) is not bool:
            raise ConfigError(f"[components].{name} must be a boolean")
    core_enabled = components_raw.get("core", True)
    desktop_pet_raw = _as_mapping(raw.get("desktop_pet"), "desktop_pet")
    presentations_raw = _as_mapping(
        desktop_pet_raw.get("presentations"), "desktop_pet.presentations"
    )
    allowed_presentation_intents = {
        "approach_window",
        "approach_icon",
        "recover",
    }
    presentation_mappings: dict[str, str] = {}
    for intent, group in presentations_raw.items():
        normalized_intent = str(intent).strip().casefold()
        if normalized_intent not in allowed_presentation_intents:
            raise ConfigError(
                f"[desktop_pet.presentations].{intent} is not a supported movement intent"
            )
        if not isinstance(group, str) or not group.strip():
            raise ConfigError(
                f"[desktop_pet.presentations].{intent} must be a non-empty motion group"
            )
        presentation_mappings[normalized_intent] = group.strip()
    interactions_raw = _as_mapping(
        desktop_pet_raw.get("interactions"), "desktop_pet.interactions"
    )
    interaction_bool_defaults = {
        "enabled": False,
        "window_approach": True,
        "icon_approach": True,
        "follow_foreground_window": True,
        "move_windows": True,
        "move_icons": True,
        "gui_inspection": True,
        "gui_actions": True,
    }
    interaction_bools: dict[str, bool] = {}
    for key, default in interaction_bool_defaults.items():
        value = interactions_raw.get(key, default)
        if type(value) is not bool:
            raise ConfigError(f"[desktop_pet.interactions].{key} must be a boolean")
        interaction_bools[key] = value
    idle_interaction_seconds = interactions_raw.get("idle_interval_seconds", 90)
    scene_interval_seconds = interactions_raw.get("scene_interval_seconds", 1.5)
    action_timeout_seconds = interactions_raw.get("action_timeout_seconds", 3.0)
    target_cooldown_seconds = interactions_raw.get("target_cooldown_seconds", 900)
    numeric_bounds = {
        "idle_interval_seconds": (idle_interaction_seconds, 15, 86400),
        "scene_interval_seconds": (scene_interval_seconds, 0.5, 30),
        "action_timeout_seconds": (action_timeout_seconds, 0.25, 15),
        "target_cooldown_seconds": (target_cooldown_seconds, 30, 86400),
    }
    for key, (value, minimum, maximum) in numeric_bounds.items():
        if (
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            or not minimum <= value <= maximum
        ):
            raise ConfigError(
                f"[desktop_pet.interactions].{key} must be between {minimum} and {maximum}"
            )
    max_window_shift = interactions_raw.get("max_window_shift_px", 12)
    max_icon_shift = interactions_raw.get("max_icon_shift_px", 12)
    if type(max_window_shift) is not int or not 1 <= max_window_shift <= 32:
        raise ConfigError(
            "[desktop_pet.interactions].max_window_shift_px must be between 1 and 32"
        )
    if type(max_icon_shift) is not int or not 1 <= max_icon_shift <= 16:
        raise ConfigError(
            "[desktop_pet.interactions].max_icon_shift_px must be between 1 and 16"
        )
    foot_anchor_x = interactions_raw.get("foot_anchor_x", 0.5)
    foot_anchor_y = interactions_raw.get("foot_anchor_y", 0.9)
    for key, value in (("foot_anchor_x", foot_anchor_x), ("foot_anchor_y", foot_anchor_y)):
        if (
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            or not 0 <= value <= 1
        ):
            raise ConfigError(
                f"[desktop_pet.interactions].{key} must be between 0 and 1"
            )
    gui_targets = _parse_gui_targets(interactions_raw.get("gui_targets", []))
    gui_targets_path = _resolve_optional_path(
        interactions_raw.get("gui_targets_path", "state/desktop_gui_targets.json"),
        config_path,
    )
    perception_raw = _as_mapping(desktop_pet_raw.get("perception"), "desktop_pet.perception")
    for key in ("vision", "speech", "capture_active_window"):
        if key in perception_raw and type(perception_raw[key]) is not bool:
            raise ConfigError(f"[desktop_pet.perception].{key} must be a boolean")
    interval = perception_raw.get("capture_interval_seconds", 30)
    if type(interval) not in (int, float) or not 1 <= interval <= 3600:
        raise ConfigError("[desktop_pet.perception].capture_interval_seconds must be between 1 and 3600")
    excluded = perception_raw.get("excluded_exes", ["obs64.exe", "obs32.exe"])
    if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded):
        raise ConfigError("[desktop_pet.perception].excluded_exes must be a string list")
    microphone = perception_raw.get("microphone_device")
    if microphone is not None and type(microphone) not in (int, str):
        raise ConfigError("[desktop_pet.perception].microphone_device must be a device index or name")
    from .push_to_talk import parse_ptt_key

    ptt_key = perception_raw.get("push_to_talk_key", "")
    ptt_mode = perception_raw.get("push_to_talk_mode", "hold")
    if ptt_mode not in ("hold", "toggle"):
        raise ConfigError("[desktop_pet.perception].push_to_talk_mode must be hold or toggle")
    try:
        parse_ptt_key(ptt_key)
    except ValueError as exc:
        raise ConfigError(f"[desktop_pet.perception].{exc}") from exc
    for console_setting in ("show_desktop_console", "show_core_console"):
        if not isinstance(desktop_pet_raw.get(console_setting, False), bool):
            raise ConfigError(f"[desktop_pet].{console_setting} must be a boolean")
    desktop_chat_raw = _as_mapping(
        desktop_pet_raw.get("chat"),
        "desktop_pet.chat",
    )
    proactive = desktop_chat_raw.get("proactive", False)
    proactive_idle = desktop_chat_raw.get("proactive_idle_seconds", 300)
    if type(proactive) is not bool:
        raise ConfigError("[desktop_pet.chat].proactive must be a boolean")
    if type(proactive_idle) not in (int, float) or not 30 <= proactive_idle <= 86400:
        raise ConfigError("[desktop_pet.chat].proactive_idle_seconds must be between 30 and 86400")
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
    configured_runtime_mode = runtime_raw.get(
        "mode",
        "desktop_pet" if legacy_desktop_enabled else "live",
    )
    runtime_mode = str(
        runtime_mode_override
        or os.getenv("NACHOBOT_LIVE2D_MODE", "")
        or configured_runtime_mode
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

    renderer_backend = str(
        renderer_backend_override
        or os.getenv("NACHOBOT_RENDERER_BACKEND", "")
        or renderer_raw.get("backend", "live2d")
    ).strip().casefold()
    if not renderer_backend:
        raise ConfigError("[renderer].backend cannot be empty")
    sprite_size = int(sprite_raw.get("size_px", 176))
    sprite_speed = float(sprite_raw.get("movement_speed_px_per_second", 44.0))
    sprite_idle_min = float(sprite_raw.get("idle_pause_min_seconds", 1.5))
    sprite_idle_max = float(sprite_raw.get("idle_pause_max_seconds", 4.5))
    sprite_atlas_path = _resolve_optional_path(sprite_raw.get("atlas_path"), config_path)
    width = sprite_size if renderer_backend == "sprite" else int(renderer_raw.get("width", 800))
    height = sprite_size if renderer_backend == "sprite" else int(renderer_raw.get("height", 600))
    scale = float(renderer_raw.get("scale", 1.0))
    cooldown = float(renderer_raw.get("poke_cooldown_seconds", 10.0))
    min_scale = float(desktop_pet_raw.get("min_scale", 0.45))
    max_scale = float(desktop_pet_raw.get("max_scale", 1.8))
    idle_motion_min = float(desktop_pet_raw.get("idle_motion_min_seconds", 18.0))
    idle_motion_max = float(desktop_pet_raw.get("idle_motion_max_seconds", 40.0))
    chat_request_timeout = float(
        desktop_chat_raw.get("request_timeout_seconds", 8.0)
    )
    chat_reply_timeout = float(desktop_chat_raw.get("reply_timeout_seconds", 60.0))
    chat_poll_interval = float(desktop_chat_raw.get("poll_interval_seconds", 0.5))
    chat_max_input_chars = int(desktop_chat_raw.get("max_input_chars", 500))
    chat_max_history_messages = int(
        desktop_chat_raw.get("max_history_messages", 100)
    )
    chat_tts_language = str(desktop_chat_raw.get("tts_language", "zh")).strip().casefold()
    chat_transport = str(desktop_chat_raw.get("transport", "http")).strip().casefold()
    chat_core_url = str(desktop_chat_raw.get("core_url", "ws://127.0.0.1:8000/ws")).strip()
    chat_tts_provider = str(desktop_chat_raw.get("tts_provider", "multimodal")).strip().casefold()
    chat_tts_url = str(desktop_chat_raw.get("tts_url", "http://127.0.0.1:9880")).strip().rstrip("/")
    if chat_transport not in {"core", "http"}:
        raise ConfigError("[desktop_pet.chat].transport must be core or http")
    if core_enabled and chat_transport != "core" and (perception_raw.get("vision") or perception_raw.get("speech")):
        raise ConfigError("Desktop perception requires [desktop_pet.chat].transport = core")
    if chat_tts_provider != "multimodal":
        raise ConfigError("Online TTS has been removed; use the local Multimodal Adapter")
    for value, schemes, name in ((chat_core_url, {"ws", "wss"}, "core_url"),
                                 (chat_tts_url, {"http", "https"}, "tts_url")):
        parts = urlsplit(value)
        if parts.scheme not in schemes or not parts.netloc:
            raise ConfigError(f"[desktop_pet.chat].{name} has an invalid URL")


    if width <= 0 or height <= 0:
        raise ConfigError("renderer width and height must be positive")
    if not all(math.isfinite(value) for value in (sprite_speed, sprite_idle_min, sprite_idle_max)):
        raise ConfigError("[sprite] movement speed and idle pauses must be finite numbers")
    if sprite_size < 96 or sprite_size > 512:
        raise ConfigError("[sprite].size_px must be between 96 and 512")
    if sprite_speed <= 0 or sprite_speed > 500:
        raise ConfigError("[sprite].movement_speed_px_per_second must be between 0 and 500")
    if sprite_idle_min < 0 or sprite_idle_max < sprite_idle_min or sprite_idle_max > 120:
        raise ConfigError("[sprite] idle pause interval is invalid")
    if scale <= 0:
        raise ConfigError("[renderer].scale must be positive")
    if cooldown < 0:
        raise ConfigError("[renderer].poke_cooldown_seconds cannot be negative")
    if min_scale <= 0 or max_scale < min_scale:
        raise ConfigError("desktop pet scale bounds are invalid")
    fit_to_window = bool(desktop_pet_raw.get("fit_to_window", True))
    if fit_to_window and min_scale > 1.0:
        raise ConfigError(
            "desktop pet min_scale cannot exceed 1.0 while fit_to_window is enabled"
        )
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

    idle_motion_groups_raw = desktop_pet_raw.get("idle_motion_groups", list(DesktopPetConfig().idle_motion_groups))
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
    model_path = _resolve_model_path(renderer_raw.get("model_path"), config_path)
    configured_character_name = str(
        desktop_pet_raw.get("character_name", "")
    ).strip()
    character_fallback = (
        "NachoBot" if renderer_backend == "sprite" else model_display_name(model_path)
    )
    character_name = configured_character_name or character_fallback

    return AdapterConfig(
        server=ServerConfig(
            host=host,
            port=port,
            token=str(server_raw.get("token", "")),
        ),
        runtime=RuntimeConfig(mode=runtime_mode),
        renderer=RendererConfig(
            model_path=model_path,
            backend=renderer_backend,
            transparent=bool(renderer_raw.get("transparent", True)),
            antialiasing=bool(renderer_raw.get("antialiasing", True)),
            width=width,
            height=height,
            scale=scale,
            track_mouse=bool(renderer_raw.get("track_mouse", False)),
            poke_cooldown_seconds=cooldown,
        ),
        sprite=SpriteConfig(
            atlas_path=sprite_atlas_path,
            size_px=sprite_size,
            movement_speed_px_per_second=sprite_speed,
            idle_pause_min_seconds=sprite_idle_min,
            idle_pause_max_seconds=sprite_idle_max,
        ),
        components=ComponentConfig(core=core_enabled,
                                   tts=core_enabled and components_raw.get("tts", False),
                                   perception=core_enabled and components_raw.get("perception", False)),
        desktop_pet=DesktopPetConfig(
            enabled=runtime_mode == "desktop_pet",
            show_desktop_console=desktop_pet_raw.get("show_desktop_console", False),
            show_core_console=core_enabled and desktop_pet_raw.get("show_core_console", False),
            title=str(desktop_pet_raw.get("title", "")).strip() or f"{character_name} · NachoBot 桌宠",
            character_name=character_name,
            chat_header=str(desktop_pet_raw.get("chat_header", "")).strip() or f"{character_name} · NachoBot",
            always_on_top=bool(desktop_pet_raw.get("always_on_top", True)),
            hide_from_taskbar=bool(desktop_pet_raw.get("hide_from_taskbar", True)),
            click_through=bool(desktop_pet_raw.get("click_through", False)),
            remember_position=bool(desktop_pet_raw.get("remember_position", True)),
            state_path=_resolve_optional_path(
                desktop_pet_raw.get(
                    "state_path",
                    "state/desktop_pet_sprite_state.json"
                    if renderer_backend == "sprite"
                    else "state/desktop_pet_state.json",
                ),
                config_path,
            ),
            start_position=start_position,
            margin=max(0, int(desktop_pet_raw.get("margin", 24))),
            min_scale=min_scale,
            max_scale=max_scale,
            fit_to_window=fit_to_window,
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
            presentation_mappings=presentation_mappings,
            idle_motion_min_seconds=idle_motion_min,
            idle_motion_max_seconds=idle_motion_max,
            interactions=DesktopInteractionConfig(
                **interaction_bools,
                idle_interval_seconds=float(idle_interaction_seconds),
                scene_interval_seconds=float(scene_interval_seconds),
                action_timeout_seconds=float(action_timeout_seconds),
                target_cooldown_seconds=float(target_cooldown_seconds),
                max_window_shift_px=max_window_shift,
                max_icon_shift_px=max_icon_shift,
                foot_anchor_x=float(foot_anchor_x),
                foot_anchor_y=float(foot_anchor_y),
                gui_targets_path=gui_targets_path,
                gui_targets=gui_targets,
            ),
            chat=DesktopChatConfig(
                enabled=core_enabled and bool(desktop_chat_raw.get("enabled", False)),
                transport=chat_transport,
                core_url=chat_core_url,
                core_reply_model_group=str(desktop_chat_raw.get("core_reply_model_group", "")).strip(),
                proactive=core_enabled and proactive and chat_transport == "core",
                proactive_idle_seconds=float(proactive_idle),
                perception=PerceptionConfig(
                    vision=core_enabled and perception_raw.get("vision", False),
                    speech=core_enabled and perception_raw.get("speech", False),
                    capture_interval_seconds=float(interval),
                    capture_active_window=perception_raw.get("capture_active_window", True),
                    excluded_exes=tuple(item.strip().casefold() for item in excluded if item.strip()), microphone_device=microphone,
                    push_to_talk_key=ptt_key.strip(),
                    push_to_talk_mode=ptt_mode,
                ),
                tts_provider=chat_tts_provider,
                tts_url=chat_tts_url,
                backend_url=str(
                    desktop_chat_raw.get("backend_url", "http://127.0.0.1:8789")
                ).strip().rstrip("/"),
                follow_pet=bool(desktop_chat_raw.get("follow_pet", False)),
                remember_position=bool(
                    desktop_chat_raw.get("remember_position", True)
                ),
                window_state_path=_resolve_optional_path(
                    desktop_chat_raw.get(
                        "window_state_path", "state/desktop_pet_chat_state.json"
                    ),
                    config_path,
                ),
                request_timeout_seconds=chat_request_timeout,
                reply_timeout_seconds=chat_reply_timeout,
                poll_interval_seconds=chat_poll_interval,
                play_audio=core_enabled and bool(desktop_chat_raw.get("play_audio", True)),
                tts_language=chat_tts_language,
                max_input_chars=chat_max_input_chars,
                history_path=_resolve_optional_path(
                    desktop_chat_raw.get(
                        "history_path", "state/desktop_pet_chat_history.json"
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
