"""Download and verify UniversalVC-owned support models.

Streaming ASR is owned by NachoBot-Multimodal-Adapter. UniversalVC keeps only
the VAD and speaker-embedding models that are specific to its audio pipeline.
"""

import logging
from pathlib import Path
from typing import Optional

import requests


_MODELS = {
    "silero_vad": {
        "filename": "silero_vad.onnx",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        "description": "Silero VAD model",
    },
    "wespeaker": {
        "filename": "wespeaker_resnet34.onnx",
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/wespeaker_zh_cnceleb_resnet34.onnx"
        ),
        "description": "WeSpeaker ResNet34 speaker embedding model",
    },
}


class ModelManager:
    """Download and verify the support models owned by UniversalVC."""

    def __init__(
        self,
        models_dir: str = "models",
        logger: Optional[logging.Logger] = None,
    ):
        self.models_dir = Path(models_dir)
        self.logger = logger or logging.getLogger(__name__)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def ensure_all(self) -> bool:
        """Ensure the VAD and speaker models are present."""
        results = [self._ensure_file(model_key) for model_key in _MODELS]
        return all(results)

    def _ensure_file(self, model_key: str) -> bool:
        info = _MODELS[model_key]
        target = self.models_dir / info["filename"]
        if target.is_file():
            self.logger.info(f"✓ {info['description']}: {target}")
            return True

        self.logger.info(f"Downloading {info['description']}...")
        return self._download(info["url"], target)

    def _download(self, url: str, target: Path) -> bool:
        try:
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            downloaded = 0
            next_report = 20

            with target.open("wb") as output:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    output.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        percent = downloaded / total * 100
                        if percent >= next_report:
                            self.logger.info(f"  ... {percent:.0f}%")
                            next_report += 20

            self.logger.info(
                f"Downloaded: {target} ({downloaded / 1024 / 1024:.1f} MB)"
            )
            return True
        except Exception as exc:
            self.logger.error(f"Download failed: {url} → {exc}")
            target.unlink(missing_ok=True)
            return False

    def get_model_paths(self) -> dict:
        """Return resolved paths for UniversalVC-owned models."""
        return {
            "silero_vad": str(self.models_dir / "silero_vad.onnx"),
            "wespeaker": str(self.models_dir / "wespeaker_resnet34.onnx"),
        }
