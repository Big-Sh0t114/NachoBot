"""Download and locate the shared sherpa-onnx streaming ASR model."""

import logging
from pathlib import Path
import tarfile
from typing import Optional

import requests


ASR_MODEL_NAME = "sherpa-onnx-streaming-zipformer-zh-xlarge-int8-2025-06-30"
ASR_MODEL_FILES = {
    "encoder": "encoder.int8.onnx",
    "decoder": "decoder.onnx",
    "joiner": "joiner.int8.onnx",
    "tokens": "tokens.txt",
}
ASR_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{ASR_MODEL_NAME}.tar.bz2"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS_DIR = PROJECT_ROOT / "models"


class ASRModelManager:
    """Own the model files used by every adapter's streaming ASR."""

    def __init__(
        self,
        models_dir: str | Path | None = None,
        logger: Optional[logging.Logger] = None,
    ):
        path = Path(models_dir) if models_dir is not None else DEFAULT_MODELS_DIR
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.models_dir = path.resolve()
        self.model_dir = self.models_dir / ASR_MODEL_NAME
        self.logger = logger or logging.getLogger(__name__)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def ensure_model(self) -> bool:
        """Download and extract the xlarge INT8 model when it is absent."""
        missing = self.missing_files()
        if not missing:
            self.logger.info("✓ Shared streaming ASR model: %s", self.model_dir)
            return True

        archive_path = self.models_dir / f"{ASR_MODEL_NAME}.tar.bz2"
        self.logger.info(
            "Downloading shared streaming ASR model (%s)...",
            ASR_MODEL_NAME,
        )
        if not self._download(ASR_MODEL_URL, archive_path):
            return False

        try:
            with tarfile.open(archive_path, "r:bz2") as archive:
                self._safe_extract(archive)

            missing = self.missing_files()
            if missing:
                self.logger.error(
                    "ASR archive is missing required files: %s",
                    ", ".join(missing),
                )
                return False

            archive_path.unlink(missing_ok=True)
            self.logger.info("✓ Extracted shared ASR model to %s", self.model_dir)
            return True
        except Exception as exc:
            self.logger.error("Failed to extract ASR archive: %s", exc)
            return False

    def missing_files(self) -> list[str]:
        return [
            filename
            for filename in ASR_MODEL_FILES.values()
            if not (self.model_dir / filename).is_file()
        ]

    def get_model_paths(self) -> dict[str, str]:
        return {
            name: str(self.model_dir / filename)
            for name, filename in ASR_MODEL_FILES.items()
        }

    def _safe_extract(self, archive: tarfile.TarFile) -> None:
        target_root = self.models_dir.resolve()
        for member in archive.getmembers():
            member_path = (self.models_dir / member.name).resolve()
            if member_path != target_root and target_root not in member_path.parents:
                raise ValueError(f"Unsafe path in model archive: {member.name}")
        archive.extractall(path=self.models_dir)

    def _download(self, url: str, target: Path) -> bool:
        try:
            with requests.get(url, stream=True, timeout=300) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))
                downloaded = 0
                next_report = 20
                with target.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            percent = downloaded / total * 100
                            if percent >= next_report:
                                self.logger.info("  ... %.0f%%", percent)
                                next_report += 20
            self.logger.info(
                "Downloaded %s (%.1f MB)",
                target,
                downloaded / 1024 / 1024,
            )
            return True
        except Exception as exc:
            self.logger.error("ASR model download failed: %s", exc)
            target.unlink(missing_ok=True)
            return False
