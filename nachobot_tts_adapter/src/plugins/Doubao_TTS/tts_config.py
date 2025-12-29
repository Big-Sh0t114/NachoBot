from dataclasses import dataclass
from typing import Dict, Any
import toml


@dataclass
class DoubaoAudioConfig:
    voice_type: str
    emotion: str
    enable_emotion: bool
    emotion_scale: float
    speed_ratio: float
    explicit_language: str
    context_language: str
    loudness_ratio: float


@dataclass
class DoubaoRequestConfig:
    silence_duration: int


@dataclass
class DoubaoAppConfig:
    base_url: str
    appid: str
    token: str
    cluster: str


@dataclass
class DoubaoTTSConfig:
    app: DoubaoAppConfig
    audio: DoubaoAudioConfig
    request: DoubaoRequestConfig

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DoubaoTTSConfig":
        app_data = data.get("app", {})
        audio_data = data.get("audio", {})
        request_data = data.get("request", {})
        return cls(
            app=DoubaoAppConfig(**app_data),
            audio=DoubaoAudioConfig(**audio_data),
            request=DoubaoRequestConfig(**request_data),
        )


class DoubaoTTSBaseConfig:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config_data = load_tts_config(config_path)
        self.tts_config: DoubaoTTSConfig = DoubaoTTSConfig.from_dict(self.config_data)
        self.app: DoubaoAppConfig = self.tts_config.app
        self.audio: DoubaoAudioConfig = self.tts_config.audio
        self.request: DoubaoRequestConfig = self.tts_config.request

    def __getitem__(self, key: str) -> Any:
        return self.config_data[key]

    def __setitem__(self, key: str, value: Any):
        self.config_data[key] = value

    def __repr__(self) -> str:
        return str(self.config_data)


def load_tts_config(config_path: str) -> Dict[str, Any]:
    """加载TOML配置文件

    Args:
        config_path (str): 配置文件路径

    Returns:
        config (Dict[str, Any]): 配置文件内容
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = toml.load(f)
    return config
