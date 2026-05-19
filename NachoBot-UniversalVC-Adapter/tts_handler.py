"""
TTS Handler - Text-to-Speech generation using GPT-SoVITS from NachoBot-TTS-Adapter.
"""

import sys
import logging
import os
import time
import tempfile
from pathlib import Path
from typing import Optional

# Attempt to include NachoBot-TTS-Adapter
_root_dir = Path(__file__).resolve().parents[1]
_tts_adapter_path = _root_dir / "NachoBot-TTS-Adapter"


class TTSHandler:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.tts_model = None
        self.enabled = False

        if _tts_adapter_path.exists() and str(_tts_adapter_path) not in sys.path:
            sys.path.insert(0, str(_tts_adapter_path))

        self._init_model()

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

    async def generate_speech(self, text: str) -> Optional[str]:
        """
        Generate speech audio file from text.
        Returns the path to the generated WAV file.
        """
        if not self.enabled or not self.tts_model:
            self.logger.warning("TTS is disabled or not initialized.")
            return None

        try:
            self.logger.info(f"Generating TTS for: {text}")

            start = time.time()
            # Enforce Chinese for target text, Japanese for reference audio
            audio_data = await self.tts_model.tts(
                text=text, platform="universal_vc", text_lang="zh", prompt_lang="ja"
            )
            duration = time.time() - start
            self.logger.info(f"TTS Generation took {duration:.2f}s")

            if not audio_data:
                return None

            # Save to temp file
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

            with open(path, "wb") as f:
                f.write(audio_data)

            return path

        except Exception as e:
            self.logger.error(f"Error generating speech: {e}")
            return None
