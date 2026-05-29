"""
Model Manager — Automatic download and verification of required models.

Manages: silero_vad.onnx, Zipformer ASR, WeSpeaker speaker embedding.
DeepFilterNet models are bundled with the pip package and not managed here.
"""

import logging
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

import requests

# ── Model Definitions ────────────────────────────────────────────────

_MODELS = {
    "silero_vad": {
        "filename": "silero_vad.onnx",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        "description": "Silero VAD model",
    },
    "zipformer_encoder": {
        "filename": "encoder-epoch-99-avg-1.onnx",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23.tar.bz2",
        "archive": True,
        "archive_dir": "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23",
        "description": "Zipformer Chinese streaming ASR (archive)",
    },
    "wespeaker": {
        "filename": "wespeaker_resnet34.onnx",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_zh_cnceleb_resnet34.onnx",
        "description": "WeSpeaker ResNet34 speaker embedding model",
    },
}


class ModelManager:
    """Download and verify required model files."""

    def __init__(self, models_dir: str = "models",
                 logger: Optional[logging.Logger] = None):
        self.models_dir = Path(models_dir)
        self.logger = logger or logging.getLogger(__name__)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def ensure_all(self) -> bool:
        """Ensure all required models are present. Returns True if all OK."""
        ok = True
        # 1. Silero VAD
        if not self._ensure_file("silero_vad"):
            ok = False
        # 2. Zipformer ASR (archive)
        if not self._ensure_archive("zipformer_encoder"):
            ok = False
        # 3. WeSpeaker
        if not self._ensure_file("wespeaker"):
            ok = False
        return ok

    def _ensure_file(self, model_key: str) -> bool:
        info = _MODELS[model_key]
        target = self.models_dir / info["filename"]
        if target.exists():
            self.logger.info(f"✓ {info['description']}: {target}")
            return True
        self.logger.info(f"Downloading {info['description']}...")
        return self._download(info["url"], target)

    def _ensure_archive(self, model_key: str) -> bool:
        """Download and extract an archive containing multiple model files."""
        info = _MODELS[model_key]
        archive_dir = self.models_dir / info.get("archive_dir", "")
        # Check if key files exist (encoder, decoder, joiner, tokens)
        expected = ["encoder-epoch-99-avg-1.onnx", "decoder-epoch-99-avg-1.onnx",
                     "joiner-epoch-99-avg-1.onnx", "tokens.txt"]
        all_exist = all((self.models_dir / f).exists() for f in expected)
        if all_exist:
            self.logger.info(f"✓ {info['description']}: all files present")
            return True

        # Also check if they exist inside the archive subdirectory
        if archive_dir.exists():
            all_in_subdir = all((archive_dir / f).exists() for f in expected)
            if all_in_subdir:
                # Move files to models root for flat access
                for f in expected:
                    src = archive_dir / f
                    dst = self.models_dir / f
                    if src.exists() and not dst.exists():
                        src.rename(dst)
                self.logger.info(f"✓ {info['description']}: extracted from subdirectory")
                return True

        # Download archive
        archive_path = self.models_dir / "asr_archive.tar.bz2"
        self.logger.info(f"Downloading {info['description']}...")
        if not self._download(info["url"], archive_path):
            return False

        # Extract
        try:
            with tarfile.open(archive_path, "r:bz2") as tar:
                tar.extractall(path=self.models_dir)
            archive_path.unlink(missing_ok=True)

            # Move files from subdirectory to models root
            if archive_dir.exists():
                for f in archive_dir.iterdir():
                    dst = self.models_dir / f.name
                    if not dst.exists():
                        f.rename(dst)
            self.logger.info(f"✓ Extracted ASR model files")
            return True
        except Exception as e:
            self.logger.error(f"Failed to extract archive: {e}")
            return False

    def _download(self, url: str, target: Path) -> bool:
        try:
            resp = requests.get(url, stream=True, timeout=300)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(target, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded / total * 100
                        if int(pct) % 20 == 0:
                            self.logger.info(f"  ... {pct:.0f}%")
            self.logger.info(f"Downloaded: {target} ({downloaded / 1024 / 1024:.1f} MB)")
            return True
        except Exception as e:
            self.logger.error(f"Download failed: {url} → {e}")
            target.unlink(missing_ok=True)
            return False

    def get_model_paths(self) -> dict:
        """Return a dict of resolved model file paths."""
        return {
            "silero_vad": str(self.models_dir / "silero_vad.onnx"),
            "asr_encoder": str(self.models_dir / "encoder-epoch-99-avg-1.onnx"),
            "asr_decoder": str(self.models_dir / "decoder-epoch-99-avg-1.onnx"),
            "asr_joiner": str(self.models_dir / "joiner-epoch-99-avg-1.onnx"),
            "asr_tokens": str(self.models_dir / "tokens.txt"),
            "wespeaker": str(self.models_dir / "wespeaker_resnet34.onnx"),
        }
