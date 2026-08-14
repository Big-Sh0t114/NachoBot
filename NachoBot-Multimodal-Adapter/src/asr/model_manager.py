"""Download and locate the shared sherpa-onnx streaming ASR model."""

import logging
import os
from pathlib import Path
import shutil
import tarfile
from typing import Optional
from urllib.parse import quote

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
ASR_HF_REPO = f"csukuangfj/{ASR_MODEL_NAME}"

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

        self.logger.info(
            "Downloading shared streaming ASR model (%s)...",
            ASR_MODEL_NAME,
        )

        # Prefer the Hugging Face mirror path. The same sherpa-onnx model is
        # published as individual files there, so mainland-China deployments
        # do not have to reach GitHub Releases for the initial 700+ MB download.
        if self._download_from_huggingface():
            self.logger.info("✓ Downloaded shared ASR model to %s", self.model_dir)
            return True

        # GitHub Releases remains a compatibility fallback for deployments
        # where Hugging Face/mirrors are unavailable.
        self.logger.warning("Hugging Face ASR download failed; trying GitHub Releases")
        archive_path = self.models_dir / f"{ASR_MODEL_NAME}.tar.bz2"
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

    @staticmethod
    def _hf_endpoints() -> list[str]:
        endpoints: list[str] = []
        for env_name in ("NACHOBOT_HF_ENDPOINT", "HF_ENDPOINT"):
            endpoint = os.environ.get(env_name, "").strip().rstrip("/")
            if endpoint:
                endpoints.append(endpoint)
        endpoints.extend(("https://hf-mirror.com", "https://huggingface.co"))
        return list(dict.fromkeys(endpoints))

    def _download_from_huggingface(self) -> bool:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        missing = self.missing_files()
        if not missing:
            return True

        for endpoint in self._hf_endpoints():
            self.logger.info("Trying ASR model download via %s", endpoint)
            endpoint_ok = True
            for filename in missing:
                destination = self.model_dir / filename
                if destination.is_file():
                    continue
                encoded_filename = quote(filename, safe="/")
                url = f"{endpoint}/{ASR_HF_REPO}/resolve/main/{encoded_filename}?download=true"
                if not self._download(url, destination):
                    endpoint_ok = False
                    break

            if endpoint_ok and not self.missing_files():
                return True

            # Keep already completed files for resume/fallback. _download()
            # removes only the failed target, so the next endpoint does not
            # need to redownload successful 700+ MB files.
            missing = self.missing_files()

        return False

    def _safe_extract(self, archive: tarfile.TarFile) -> None:
        target_root = self.models_dir.resolve()
        for member in archive.getmembers():
            member_path = (self.models_dir / member.name).resolve()
            if member_path != target_root and target_root not in member_path.parents:
                raise ValueError(f"Unsafe path in model archive: {member.name}")
            if member.isdir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"Unsupported archive member: {member.name}")

            member_path.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Unable to read archive member: {member.name}")
            with source, member_path.open("wb") as output:
                shutil.copyfileobj(source, output)

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
