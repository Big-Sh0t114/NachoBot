import importlib
import toml
from pathlib import Path
from typing import Tuple, Any, Optional

def resolve_tts_model_class() -> Tuple[Optional[Any], Optional[str]]:
    """
    Reads the base.toml config to determine which TTS plugin to enable,
    then dynamically imports and returns its TTSModel class.
    
    Returns:
        Tuple[TTSModelClass, error_message]
        If successful, error_message is None.
        If failed, TTSModelClass is None.
    """
    try:
        # Resolve base.toml path relative to this file
        # src/utils/tts_resolver.py -> NachoBot-Multimodal-Adapter/configs/base.toml
        config_path = Path(__file__).resolve().parents[2] / "configs" / "base.toml"
        
        if not config_path.exists():
            return None, f"Config file not found: {config_path}"
            
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = toml.load(f)
            
        enabled_tts_list = config_data.get("enabled_tts", {}).get("enabled", [])
        
        if not enabled_tts_list:
            return None, "No TTS plugins enabled in base.toml [enabled_tts.enabled]"
            
        if "GPT_Sovits" in enabled_tts_list and "Vox" in enabled_tts_list:
            return None, "Both GPT_Sovits and Vox are enabled. Please select only one."
            
        # Take the first enabled plugin
        target_plugin = enabled_tts_list[0]
        
        module_name = f"nachobot_multimodal.tts.backends.{target_plugin}"
        try:
            module = importlib.import_module(module_name)
            if hasattr(module, "TTSModel"):
                return module.TTSModel, None
            else:
                # Fallback to importing from tts_model.py directly
                module_name = f"nachobot_multimodal.tts.backends.{target_plugin}.tts_model"
                module = importlib.import_module(module_name)
                return module.TTSModel, None
        except ImportError as e:
            return None, f"Failed to import TTS model {target_plugin}: {e}"
        except AttributeError as e:
            return None, f"TTSModel class not found in {target_plugin}: {e}"
            
    except Exception as e:
        return None, f"Unexpected error resolving TTS model: {e}"
