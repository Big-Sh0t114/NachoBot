import os
from dataclasses import dataclass
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


@dataclass
class BaseConfig:
    server: ServerConfig
    enabled_plugin: EnabledPluginClass
    tts_base_config: ttsClass

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseConfig":
        return cls(
            server=ServerConfig(**data["server"]),
            enabled_plugin=EnabledPluginClass(**data["enabled_tts"]),
            tts_base_config=ttsClass(**data["tts_base_config"]),
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
