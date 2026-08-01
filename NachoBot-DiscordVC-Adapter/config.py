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
class PromptsConfig:
    planner_prompt: str = ""
    replyer_prompt: str = ""
    variables: Dict[str, str] = field(default_factory=dict)


@dataclass
class AdapterConfig:
    discord: DiscordConfig
    nachobot: NachoBotConfig
    voice: VoiceConfig
    prompts: PromptsConfig
    log_level: str = "INFO"
    disable_network_search: bool = False


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

    # Assumption: Adapter is at Root/NachoBot-DiscordVC-Adapter.
    root_dir = path.parent.parent
    nachobot_config_dir = root_dir / "NachoBot" / "config"

    # Resolve core prompts variables
    core_variables = _resolve_prompts_from_core(nachobot_config_dir)

    # Merge variables: Local config overrides Core config
    local_variables = prompts_data.get("variables", {})
    merged_variables = {**core_variables, **local_variables}

    # Update prompts_data with merged variables
    prompts_data["variables"] = merged_variables

    return AdapterConfig(
        discord=DiscordConfig(**discord_data),
        nachobot=NachoBotConfig(**nachobot_data),
        voice=VoiceConfig(**voice_data),
        prompts=PromptsConfig(**prompts_data),
        log_level=data.get("log_level", "INFO"),
        disable_network_search=data.get("disable_network_search", False),
    )
