import os
from dataclasses import dataclass, field
import math
from typing import Dict, Any, List
import toml
from pathlib import Path

logging_level = "INFO"  # Default logging level, can be overridden by config


@dataclass
class ServerConfig:
    host: str
    port: int


@dataclass
class EnabledPluginClass:
    enabled: List[str]


@dataclass
class ttsClass:
    stream_mode: bool
    post_process: bool


@dataclass(frozen=True)
class AmbientConfig:
    """Public TTS ambience options, independent from telephone post-processing."""

    enabled: bool = False
    preset: str = "room"
    level_db: float = -38.0
    events_per_minute: float = 3.0
    source_file: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> "AmbientConfig":
        if not isinstance(value, dict):
            raise ValueError("[tts_ambient] must be a table")

        enabled = value.get("enabled", cls.enabled)
        if not isinstance(enabled, bool):
            raise ValueError("[tts_ambient].enabled must be a boolean")

        preset = value.get("preset", cls.preset)
        if not isinstance(preset, str) or preset not in {"room", "desk"}:
            raise ValueError("[tts_ambient].preset must be 'room' or 'desk'")

        level_db = _finite_number(value.get("level_db", cls.level_db), "level_db")
        if not -90.0 <= level_db <= 0.0:
            raise ValueError("[tts_ambient].level_db must be between -90 and 0 dBFS")

        events_per_minute = _finite_number(
            value.get("events_per_minute", cls.events_per_minute),
            "events_per_minute",
        )
        if not 0.0 <= events_per_minute <= 30.0:
            raise ValueError("[tts_ambient].events_per_minute must be between 0 and 30")

        source_file = value.get("source_file", cls.source_file)
        if not isinstance(source_file, str) or len(source_file) > 1024 or "\x00" in source_file:
            raise ValueError("[tts_ambient].source_file must be a path string up to 1024 characters")

        return cls(
            enabled=enabled,
            preset=preset,
            level_db=level_db,
            events_per_minute=events_per_minute,
            source_file=source_file,
        )


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"[tts_ambient].{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"[tts_ambient].{name} must be a finite number")
    return result


@dataclass
class BaseConfig:
    server: ServerConfig
    enabled_plugin: EnabledPluginClass
    tts_base_config: ttsClass
    tts_ambient: AmbientConfig = field(default_factory=AmbientConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseConfig":
        return cls(
            server=ServerConfig(**data["server"]),
            enabled_plugin=EnabledPluginClass(**data["enabled_tts"]),
            tts_base_config=ttsClass(**data["tts_base_config"]),
            tts_ambient=AmbientConfig.from_dict(data.get("tts_ambient", {})),
        )


class Config:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config_data = load_config(config_path)
        server = self.config_data.setdefault(
            "server",
            {"host": "127.0.0.1", "port": 9880},
        )
        server["host"] = os.getenv("NACHOBOT_MULTIMODAL_HOST", str(server.get("host", "127.0.0.1")))
        server["port"] = int(
            os.getenv("NACHOBOT_MULTIMODAL_PORT", str(server.get("port", 9880)))
        )
        self.config_data.setdefault("enabled_tts", {"enabled": []})
        self.config_data.setdefault(
            "tts_base_config",
            {"stream_mode": False, "post_process": False},
        )
        self.base_config = BaseConfig.from_dict(self.config_data)

    def __getitem__(self, key: str) -> Any:
        return self.config_data[key]

    def __setitem__(self, key: str, value: Any):
        self.config_data[key] = value

    def __repr__(self) -> str:
        return str(self.config_data)

    @property
    def server(self) -> ServerConfig:
        return self.base_config.server

    @property
    def enabled_plugin(self) -> EnabledPluginClass:
        return self.base_config.enabled_plugin

    @property
    def tts_base_config(self) -> ttsClass:
        return self.base_config.tts_base_config

    @property
    def tts_ambient(self) -> AmbientConfig:
        return self.base_config.tts_ambient


def load_config(config_path: str) -> Dict[str, Any]:
    """加载TOML配置文件

    Args:
        config_path: 配置文件路径

    Returns:
        配置字典
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = toml.load(f)
    global logging_level
    # 设置全局日志级别
    logging_level = config.get("debug", {}).get("logging_level", "INFO").upper()
    return config


def get_default_config() -> Config:
    """获取默认配置"""
    config_path = Path(__file__).parent.parent / "configs" / "base.toml"
    return Config(str(config_path))
