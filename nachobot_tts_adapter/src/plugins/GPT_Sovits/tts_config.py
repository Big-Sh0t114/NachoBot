from dataclasses import dataclass, field
from typing import Dict, Any, List
import toml


@dataclass
class TTSPreset:
    name: str
    ref_audio_path: str
    prompt_text: str
    gpt_model: str = field(default="")
    sovits_model: str = field(default="")
    aux_ref_audio_paths: List[str] = field(default_factory=list)
    text_language: str = field(default="auto")
    prompt_language: str = field(default="zh")
    speed_factor: float = field(default=1.0)


@dataclass
class TTSModels:
    presets: Dict[str, TTSPreset]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TTSModels":
        presets = {name: TTSPreset(**preset_data) for name, preset_data in data.get("presets", {}).items()}
        return cls(
            presets=presets,
        )


@dataclass
class TTSConfig:
    host: str
    port: int
    top_k: int
    top_p: float
    temperature: float
    batch_size: int
    batch_threshold: float
    text_split_method: str
    repetition_penalty: float
    sample_steps: int
    super_sampling: bool
    models: TTSModels
    media_type: str = field(default="wav")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TTSConfig":
        models_data = data.pop("models", {})
        return cls(
            **{k: v for k, v in data.items() if k != "models"},
            models=TTSModels.from_dict(models_data),
        )


@dataclass
class PipelineConfig:
    default_preset: str
    platform_presets: Dict[str, str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineConfig":
        return cls(
            default_preset=data.get("default_preset"),
            platform_presets=data.get("platform_presets"),
        )


@dataclass
class TTSBaseConfigData:
    tts: TTSConfig
    pipeline: PipelineConfig

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TTSBaseConfig":
        tts_config = TTSConfig.from_dict(data.get("tts", {}))
        pipeline_config = PipelineConfig.from_dict(data.get("pipeline", {}))
        return cls(tts=tts_config, pipeline=pipeline_config)


class TTSBaseConfig:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config_data = load_tts_config(config_path)
        self.base_config = TTSBaseConfigData.from_dict(self.config_data)
        self.tts: TTSConfig = self.base_config.tts
        self.pipeline: PipelineConfig = self.base_config.pipeline

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
