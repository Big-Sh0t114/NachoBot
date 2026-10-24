"""Persistent semantic descriptions keyed by the exact artwork file bytes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path


_WRITE_LOCK = threading.Lock()
_NON_SEMANTIC_DESCRIPTIONS = {
    "",
    "none",
    "n/a",
    "unknown",
    "no description",
    "cannot tell",
    "unrecognizable",
    "unavailable",
    "无描述",
    "无文字描述",
    "无语义描述",
    "未知",
    "无法辨认",
    "无法识别",
    "看不清",
    "看不出来",
    "不清楚",
}


def hash_file(path: Path) -> str:
    """Hash a file incrementally so large artwork files are not held in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_description(value: str, *, filename: str | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    description = " ".join(value.split())
    normalized = description.casefold().strip(" .,:;，。；：\"'`“”‘’")
    if not description or normalized in _NON_SEMANTIC_DESCRIPTIONS:
        return None
    if filename and normalized == filename.casefold().strip(" .,:;，。；：\"'`“”‘’"):
        return None
    if len(description) > 600:
        return None
    return description


class ArtworkDescriptionCache:
    """Atomic JSON cache whose keys are SHA-256 digests of actual file contents."""

    def __init__(self, directory: Path | str | None = None):
        self.directory = (
            Path(directory) if directory else Path(__file__).resolve().parents[2] / "data" / "artwork_descriptions"
        )

    def get(self, content_hash: str) -> str | None:
        path = self.directory / f"{content_hash}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("sha256") != content_hash:
            return None
        return normalize_description(payload.get("description", ""))

    def save(self, content_hash: str, description: str, *, filename: str | None = None) -> bool:
        normalized = normalize_description(description, filename=filename)
        if not normalized or len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
            return False

        temporary_path: str | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"sha256": content_hash, "description": normalized},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            with _WRITE_LOCK:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.directory,
                    prefix=f".{content_hash}.",
                    suffix=".tmp",
                    delete=False,
                ) as target:
                    temporary_path = target.name
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary_path, self.directory / f"{content_hash}.json")
                temporary_path = None
            return True
        except OSError:
            return False
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
