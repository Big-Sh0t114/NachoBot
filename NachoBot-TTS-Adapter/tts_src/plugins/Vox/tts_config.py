from dataclasses import dataclass, field
from typing import Dict, Any, List
import toml


@dataclass
class VoxPreset:
    name: str
    ref_audio_path: str = field(default="")
    control_instruction: str = field(default="")
    prompt_text: str = field(default="")
    cfg_value: float = field(default=2.0)
    inference_timesteps: int = field(default=10)
    denoise: bool = field(default=True)
    normalize: bool = field(default=False)
    seed: int = field(default=-1)


@dataclass
class VoxConfig:
    host: str
    port: int
    model_dir: str
    lora_weights_path: str = field(default="")
    cfg_value: float = field(default=2.0)
    inference_timesteps: int = field(default=10)
    denoise: bool = field(default=True)
    normalize: bool = field(default=False)
    seed: int = field(default=-1)
    split_method: str = field(default="cut3")
    max_split_length: int = field(default=80)
    segment_gap_ms: int = field(default=100)
    presets: Dict[str, VoxPreset] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VoxConfig":
        models_data = data.pop("models", {})
        presets_data = models_data.get("presets", {})
        presets = {
            name: VoxPreset(**preset_data)
            for name, preset_data in presets_data.items()
        }
        return cls(**data, presets=presets)


@dataclass
class PipelineConfig:
    default_preset: str
    platform_presets: Dict[str, str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineConfig":
        return cls(
            default_preset=data.get("default_preset", "default"),
            platform_presets=data.get("platform_presets", {}),
        )


@dataclass
class EmotionConfig:
    """情感分类系统配置"""
    enabled: bool = True
    classifier_model: str = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"
    classifier_device: str = "cpu"
    use_fp16: bool = True
    confidence_threshold: float = 0.4
    default_emotion: str = "\u5e73\u5e38"
    available_tags: List[str] = field(default_factory=lambda: ["\u5e73\u5e38"])
    tag_preset_map: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EmotionConfig":
        return cls(
            enabled=data.get("enabled", True),
            classifier_model=data.get("classifier_model", cls.classifier_model),
            classifier_device=data.get("classifier_device", "cpu"),
            use_fp16=data.get("use_fp16", True),
            confidence_threshold=data.get("confidence_threshold", 0.4),
            default_emotion=data.get("default_emotion", "\u5e73\u5e38"),
            available_tags=data.get("available_tags", ["\u5e73\u5e38"]),
            tag_preset_map=data.get("tag_preset_map", {}),
        )


@dataclass
class VoxBaseConfigData:
    vox: VoxConfig
    pipeline: PipelineConfig
    emotion: EmotionConfig

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VoxBaseConfigData":
        vox_config = VoxConfig.from_dict(data.get("tts", {}))
        pipeline_config = PipelineConfig.from_dict(data.get("pipeline", {}))
        emotion_config = EmotionConfig.from_dict(data.get("emotion", {}))
        return cls(vox=vox_config, pipeline=pipeline_config, emotion=emotion_config)


class VoxBaseConfig:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config_data = load_vox_config(config_path)
        self.base_config = VoxBaseConfigData.from_dict(self.config_data)
        self.vox: VoxConfig = self.base_config.vox
        self.pipeline: PipelineConfig = self.base_config.pipeline
        self.emotion: EmotionConfig = self.base_config.emotion

    def __getitem__(self, key: str) -> Any:
        return self.config_data[key]

    def __setitem__(self, key: str, value: Any):
        self.config_data[key] = value

    def __repr__(self) -> str:
        return str(self.config_data)


def load_vox_config(config_path: str) -> Dict[str, Any]:
    """加载TOML配置文件

    Args:
        config_path (str): 配置文件路径

    Returns:
        config (Dict[str, Any]): 配置文件内容
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = toml.load(f)
    return config
