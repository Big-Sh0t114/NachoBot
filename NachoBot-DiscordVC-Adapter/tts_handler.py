import sys
import logging
import asyncio
import os
import time
import tempfile
from pathlib import Path
from typing import Optional

# Attempt to include nachobot_tts_adapter
_root_dir = Path(__file__).resolve().parents[1]
_tts_adapter_path = _root_dir / "NachoBot-TTS-Adapter"


class TTSHandler:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.tts_model = None
        self.enabled = False

        if _tts_adapter_path.exists() and str(_tts_adapter_path) not in sys.path:
            sys.path.insert(0, str(_tts_adapter_path))

        self._init_model()  # Eager init

    def _init_model(self):
        """Initialize the TTS model based on availability."""
        try:
            from tts_src.utils.tts_resolver import resolve_tts_model_class
            
            TTSModelClass, err = resolve_tts_model_class()
            if TTSModelClass:
                self.tts_model = TTSModelClass()
                self.enabled = True
                self.logger.info(f"TTS Model initialized successfully: {TTSModelClass.__module__}")
            else:
                self.logger.warning(f"Could not resolve TTS Model: {err}")

        except Exception as e:
            self.logger.error(f"Failed to initialize TTS: {e}")

    async def generate_speech(self, text: str, preset_name: Optional[str] = None, split_method: Optional[str] = None) -> Optional[str]:
        """
        Generate speech audio file from text.
        Returns the path to the generated file.
        """
        if not self.enabled or not self.tts_model:
            self.logger.warning("TTS is disabled or not initialized.")
            return None

        try:
            # The TTSModel.tts() usually returns bytes or saves a file.
            # In debugger it returned bytes: audio_data = await tts_class.tts(...)

            # We need to save it to a temp file for Discord to play

            self.logger.info(f"Generating TTS for: {text}")

            # Assume tts() is async and takes text
            start = time.time()
            # Enforce Chinese for target text, but Japanese for reference audio (as user clarified)
            audio_data = await self.tts_model.tts(
                text=text, platform="discord", text_lang="zh", prompt_lang="ja", preset_name=preset_name, split_method=split_method
            )
            duration = time.time() - start
            self.logger.info(f"TTS Generation took {duration:.2f}s")

            if not audio_data:
                return None

            # Save to temp file
            # Discord uses ffmpeg, wav is fine.
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

            with open(path, "wb") as f:
                f.write(audio_data)

            return path

        except Exception as e:
            self.logger.error(f"Error generating speech: {e}")
            return None
