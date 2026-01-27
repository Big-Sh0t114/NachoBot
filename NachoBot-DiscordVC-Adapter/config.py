import logging
import tomlkit
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict


@dataclass
class DiscordConfig:
    token: str
    app_id: Optional[str] = None
    proxy_enabled: bool = False
    proxy_url: Optional[str] = None


@dataclass
class NachoBotConfig:
    host: str
    port: int


@dataclass
class VoiceConfig:
    enabled: bool = True
    silence_threshold: float = 0.5  # Seconds of silence to consider end of speech
    vad_threshold: int = 500  # RMS threshold for VAD (Voice Activity Detection)
    sample_rate: int = 48000


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
class AdapterConfig:
    discord: DiscordConfig
    nachobot: NachoBotConfig
    voice: VoiceConfig
    stt: STTConfig
    prompts: PromptsConfig
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
        # Map known keys to variables
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

    discord_data = data.get("discord", {})
    nachobot_data = data.get("nachobot", {})
    voice_data = data.get("voice", {})
    prompts_data = data.get("prompts", {})
    # stt_data = data.get("stt", {}) # We'll try to load from NachoBot config first

    # Try to resolve STT from NachoBot common config
    # Assumption: Adapter is at Root/NachoBot-DiscordVC-Adapter
    # Config is at Root/NachoBot/config/model_config.toml
    root_dir = path.parent.parent
    nachobot_config_dir = root_dir / "NachoBot" / "config"
    model_config_path = nachobot_config_dir / "model_config.toml"

    stt_config = _resolve_stt_config_from_nachobot(model_config_path)

    # Resolve core prompts variables
    core_variables = _resolve_prompts_from_core(nachobot_config_dir)

    # Merge variables: Local config overrides Core config
    local_variables = prompts_data.get("variables", {})
    merged_variables = {**core_variables, **local_variables}

    # Update prompts_data with merged variables
    prompts_data["variables"] = merged_variables

    # Allow override from local config if specifically set (optional)
    local_stt_data = data.get("stt", {})
    if local_stt_data.get("enabled", False):
        # If local config explicitly enables and provides keys, use it?
        # Or maybe just use local if NachoBot's failed?
        # User said "directly call", so let's prefer NachoBot's,
        # but if local has specific overrides we can apply them.
        if local_stt_data.get("api_key"):
            stt_config.api_key = local_stt_data["api_key"]
        if local_stt_data.get("base_url"):
            stt_config.base_url = local_stt_data["base_url"]
        if local_stt_data.get("model"):
            stt_config.model = local_stt_data["model"]
        stt_config.enabled = True  # Force enable if local says so

    return AdapterConfig(
        discord=DiscordConfig(**discord_data),
        nachobot=NachoBotConfig(**nachobot_data),
        voice=VoiceConfig(**voice_data),
        stt=stt_config,
        prompts=PromptsConfig(**prompts_data),
        log_level=data.get("log_level", "INFO"),
        disable_network_search=data.get("disable_network_search", False),
    )
