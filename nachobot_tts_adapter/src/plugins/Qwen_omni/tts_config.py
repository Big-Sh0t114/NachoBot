from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class TTSConfigData:
    """大模型TTS配置"""

    api_key: str
    model_name: str
    voice_character: str
    media_format: str
    base_url: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OmniTTSConfig":
        return cls(
            api_key=data.get("api_key", ""),
            model_name=data.get("model_name", "qwen-omni-turbo"),
            voice_character=data.get("voice", "Chelsie"),
            media_format=data.get("media_format", "wav"),
            base_url=data.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )


class OmniTTSConfig:
    def __init__(self, config_path: str):
        """初始化配置

        Args:
            config_path (str): 配置文件路径
        """
        self.config_path = config_path
        self.config_data = load_tts_config(config_path)
        self.base_config = TTSConfigData.from_dict(self.config_data)
        self.api_key: str = self.base_config.api_key
        self.base_url: str = self.base_config.base_url
        self.model_name: str = self.base_config.model_name
        self.voice_character: str = self.base_config.voice_character
        self.media_format: str = self.base_config.media_format
        self.headers: Dict[str, str] = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

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
        Dict[str, Any]: 配置数据
    """
    import toml

    with open(config_path, "r", encoding="utf-8") as f:
        return toml.load(f)
