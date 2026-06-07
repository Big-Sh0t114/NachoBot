import logging
import tomlkit
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict


@dataclass
class AudioCaptureConfig:
    """Target process audio capture settings (ProcTap)."""
    target_pid: Optional[int] = None         # Explicit PID (takes precedence)
    target_process_name: str = ""            # Process name (e.g. "VRChat.exe")
    system_capture_device: str = ""          # Input device for system-wide capture (when no target process)
    vad_threshold: int = 500                 # RMS threshold for Voice Activity Detection (legacy fallback)
    silence_threshold: float = 0.8           # Seconds of silence to mark end of speech (legacy fallback)
    min_speech_duration: float = 0.3         # Minimum duration to consider as valid speech (legacy fallback)


@dataclass
class MicrophoneConfig:
    """Microphone capture settings for owner voice input."""
    enabled: bool = False
    device_name: str = ""               # Microphone device name (empty = system default)
    owner_speaker_id: str = "owner"     # Fixed speaker ID for microphone input
    owner_speaker_name: str = "主人"    # Fixed display name for microphone input


@dataclass
class AudioOutputConfig:
    """Virtual audio cable output settings."""
    device_name: str = "CABLE Input"         # Output device name (VB-Audio Virtual Cable)
    sample_rate: int = 44100                 # Output sample rate


@dataclass
class NachoBotConfig:
    host: str = "localhost"
    port: int = 8000


@dataclass
class STTConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = ""
    model: str = "whisper-1"


@dataclass
class PromptsConfig:
    planner_prompt: str = ""
    replyer_prompt: str = ""
    variables: Dict[str, str] = field(default_factory=dict)


@dataclass
class DenoiseConfig:
    """Real-time audio denoising settings (DeepFilterNet)."""
    enabled: bool = True


@dataclass
class VADConfig:
    """Silero VAD settings (replaces simple RMS threshold)."""
    model_path: str = "models/silero_vad.onnx"
    threshold: float = 0.5
    min_silence_duration: float = 0.25   # seconds of silence to mark end of speech
    min_speech_duration: float = 0.3     # minimum duration to consider as valid speech


@dataclass
class SpeakerConfig:
    """Real-time speaker tracking settings."""
    enabled: bool = True
    embedding_model_path: str = "models/wespeaker_resnet34.onnx"
    similarity_threshold: float = 0.5   # cosine similarity threshold
    max_speakers: int = 8               # max distinct speakers to track (set to 0 for unlimited)
    db_path: str = "speaker_db.json"    # voiceprint database path


@dataclass
class LocalASRConfig:
    """Local streaming ASR settings (sherpa-onnx)."""
    mode: str = "local_streaming"        # "local_streaming" | "remote_api"
    tokens_path: str = "models/tokens.txt"
    encoder_path: str = "models/encoder-epoch-99-avg-1.onnx"
    decoder_path: str = "models/decoder-epoch-99-avg-1.onnx"
    joiner_path: str = "models/joiner-epoch-99-avg-1.onnx"
    num_threads: int = 2


@dataclass
class AdapterConfig:
    capture: AudioCaptureConfig
    output: AudioOutputConfig
    nachobot: NachoBotConfig
    stt: STTConfig
    prompts: PromptsConfig
    denoise: DenoiseConfig
    vad: VADConfig
    speaker: SpeakerConfig
    local_asr: LocalASRConfig
    microphone: MicrophoneConfig
    log_level: str = "INFO"
    disable_network_search: bool = False


def _resolve_stt_config_from_nachobot(nachobot_config_path: Path) -> STTConfig:
    """Read NachoBot's model_config.toml to resolve STT settings."""
    if not nachobot_config_path.exists():
        logging.warning(f"NachoBot model config not found at {nachobot_config_path}")
        return STTConfig(enabled=False)

    try:
        with open(nachobot_config_path, "r", encoding="utf-8") as f:
            data = tomlkit.load(f)

        # 1. Get Voice Model Name
        voice_task = data.get("model_task_config", {}).get("voice", {})
        model_list = voice_task.get("model_list", [])
        if not model_list:
            logging.warning("No voice models defined in model_config.toml")
            return STTConfig(enabled=False)

        target_model = model_list[0]

        # 2. Find Model Definition
        models = data.get("models", [])
        model_def = next(
            (
                m
                for m in models
                if m.get("model_identifier") == target_model
                or m.get("name") == target_model
            ),
            None,
        )

        if not model_def:
            logging.warning(f"Model definition for {target_model} not found")
            return STTConfig(enabled=False)

        provider_name = model_def.get("api_provider")
        real_model_name = model_def.get("name", target_model)

        # 3. Find API Provider
        providers = data.get("api_providers", [])
        provider_def = next(
            (p for p in providers if p.get("name") == provider_name), None
        )

        if not provider_def:
            logging.warning(f"Provider {provider_name} not found")
            return STTConfig(enabled=False)

        return STTConfig(
            enabled=True,
            api_key=provider_def.get("api_key", ""),
            base_url=provider_def.get("base_url", ""),
            model=real_model_name,
        )

    except Exception as e:
        logging.error(f"Failed to parse model_config.toml: {e}")
        return STTConfig(enabled=False)


def _resolve_prompts_from_core(nachobot_config_dir: Path) -> Dict[str, str]:
    """Read NachoBot's bot_config.toml to resolve personality variables."""
    bot_config_path = nachobot_config_dir / "bot_config.toml"
    if not bot_config_path.exists():
        logging.warning(f"NachoBot bot config not found at {bot_config_path}")
        return {}

    try:
        with open(bot_config_path, "r", encoding="utf-8") as f:
            data = tomlkit.load(f)
        personality_data = data.get("personality", {})

        variables = {}
        keys_to_sync = ["personality", "reply_style", "emotion_style", "interest"]
        for key in keys_to_sync:
            if val := personality_data.get(key):
                variables[key] = str(val)

        return variables
    except Exception as e:
        logging.error(f"Failed to parse bot_config.toml: {e}")
        return {}


def load_config(path: Path) -> AdapterConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    capture_data = data.get("capture", {})
    output_data = data.get("output", {})
    nachobot_data = data.get("nachobot", {})
    prompts_data = data.get("prompts", {})

    # Resolve STT from NachoBot common config
    root_dir = path.parent.parent
    nachobot_config_dir = root_dir / "NachoBot" / "config"
    model_config_path = nachobot_config_dir / "model_config.toml"

    stt_config = _resolve_stt_config_from_nachobot(model_config_path)

    # Resolve core prompts variables
    core_variables = _resolve_prompts_from_core(nachobot_config_dir)

    # Merge variables: Local config overrides Core config
    local_variables = prompts_data.get("variables", {})
    merged_variables = {**core_variables, **local_variables}
    prompts_data["variables"] = merged_variables

    # Allow local STT override
    local_stt_data = data.get("stt", {})
    if local_stt_data.get("enabled", False):
        if local_stt_data.get("api_key"):
            stt_config.api_key = local_stt_data["api_key"]
        if local_stt_data.get("base_url"):
            stt_config.base_url = local_stt_data["base_url"]
        if local_stt_data.get("model"):
            stt_config.model = local_stt_data["model"]
        stt_config.enabled = True

    # New pipeline config sections
    denoise_data = data.get("denoise", {})
    vad_data = data.get("vad", {})
    speaker_data = data.get("speaker", {})
    local_asr_data = data.get("local_asr", {})

    # Resolve model paths relative to config file directory
    config_dir = path.parent
    for cfg_dict, path_keys in [
        (vad_data, ["model_path"]),
        (speaker_data, ["embedding_model_path", "db_path"]),
        (local_asr_data, ["tokens_path", "encoder_path", "decoder_path", "joiner_path"]),
    ]:
        for key in path_keys:
            if key in cfg_dict:
                p = Path(cfg_dict[key])
                if not p.is_absolute():
                    cfg_dict[key] = str(config_dir / p)

    microphone_data = data.get("microphone", {})

    return AdapterConfig(
        capture=AudioCaptureConfig(**capture_data),
        output=AudioOutputConfig(**output_data),
        nachobot=NachoBotConfig(**nachobot_data),
        stt=stt_config,
        prompts=PromptsConfig(**prompts_data),
        denoise=DenoiseConfig(**denoise_data),
        vad=VADConfig(**vad_data),
        speaker=SpeakerConfig(**speaker_data),
        local_asr=LocalASRConfig(**local_asr_data),
        microphone=MicrophoneConfig(**microphone_data),
        log_level=data.get("log_level", "INFO"),
        disable_network_search=data.get("disable_network_search", False),
    )
