"""Download and verify UniversalVC-owned support models.

Speech perception is owned by Core. UniversalVC keeps only the VAD and
speaker-embedding models that are specific to its audio pipeline.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests


_MODELS = {
    "silero_vad": {
        "filename": "silero_vad.onnx",
        "hf_repo": "codeandwhisky/sva-genesis",
        "hf_filename": "silero_vad.onnx",
        "sha256": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        "description": "Silero VAD model",
    },
    "wespeaker": {
        "filename": "wespeaker_resnet34.onnx",
        "hf_repo": "csukuangfj/speaker-embedding-models",
        "hf_filename": "wespeaker_zh_cnceleb_resnet34.onnx",
        "sha256": "f86cd6c509f331f0e20b07bd48d1b2eb7de54202643401c4e84695ac861a0e5a",
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

        urls = self._download_urls(info)
        for index, url in enumerate(urls):
            if index == 0:
                self.logger.info(f"Downloading {info['description']}...")
            else:
                self.logger.warning(
                    f"Download failed; trying fallback {index + 1}/{len(urls)}"
                )
            if self._download(url, target, info["sha256"]):
                return True
        return False

    @staticmethod
    def _hf_endpoints() -> list[str]:
        endpoints: list[str] = []
        for env_name in ("NACHOBOT_HF_ENDPOINT", "HF_ENDPOINT"):
            endpoint = os.environ.get(env_name, "").strip().rstrip("/")
            if endpoint:
                endpoints.append(endpoint)
        endpoints.extend(("https://hf-mirror.com", "https://huggingface.co"))
        return list(dict.fromkeys(endpoints))

    @classmethod
    def _download_urls(cls, info: dict[str, str]) -> list[str]:
        urls = [
            f"{endpoint}/{info['hf_repo']}/resolve/main/"
            f"{quote(info['hf_filename'], safe='/')}?download=true"
            for endpoint in cls._hf_endpoints()
        ]
        urls.append(info["url"])
        return urls

    def _download(
        self,
        url: str,
        target: Path,
        expected_sha256: str | None = None,
    ) -> bool:
        partial = target.with_name(f"{target.name}.part")
        try:
            partial.unlink(missing_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(url, stream=True, timeout=300) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                expected_size = int(content_length) if content_length else None
                downloaded = 0
                next_report = 20
                digest = hashlib.sha256() if expected_sha256 else None

                with partial.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        output.write(chunk)
                        if digest is not None:
                            digest.update(chunk)
                        downloaded += len(chunk)
                        if expected_size and expected_size > 0:
                            percent = downloaded / expected_size * 100
                            if percent >= next_report:
                                self.logger.info(f"  ... {percent:.0f}%")
                                next_report += 20

            if downloaded == 0:
                raise ValueError("downloaded file is empty")
            if expected_size is not None and downloaded != expected_size:
                raise ValueError(
                    f"incomplete download: received {downloaded} bytes, "
                    f"expected {expected_size}"
                )
            if digest is not None and digest.hexdigest().lower() != expected_sha256.lower():
                raise ValueError("SHA-256 checksum mismatch")

            partial.replace(target)

            self.logger.info(
                f"Downloaded: {target} ({downloaded / 1024 / 1024:.1f} MB)"
            )
            return True
        except Exception as exc:
            self.logger.error(f"Download failed: {url} → {exc}")
            partial.unlink(missing_ok=True)
            return False

    def get_model_paths(self) -> dict:
        """Return resolved paths for UniversalVC-owned models."""
        return {
            "silero_vad": str(self.models_dir / "silero_vad.onnx"),
            "wespeaker": str(self.models_dir / "wespeaker_resnet34.onnx"),
        }
