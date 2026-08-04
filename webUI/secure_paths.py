"""Small validation helpers for WebUI filesystem inputs."""

from __future__ import annotations

import re
from pathlib import Path

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _as_clean_text(value: str | Path, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} cannot be empty")
    if _CONTROL_CHARS_RE.search(text):
        raise ValueError(f"{label} contains control characters")
    return text


def ensure_within(root: Path, path: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve a path and require it to remain under root."""
    root_resolved = root.resolve()
    # codeql[py/path-injection]
    candidate = Path(path).resolve(strict=False)
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"Path is outside the allowed directory: {candidate}")
    # codeql[py/path-injection]
    if must_exist and not candidate.exists():
        raise FileNotFoundError(str(candidate))
    return candidate


def resolve_relative_to_root(
    root: Path,
    relative_path: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a relative path under root."""
    text = _as_clean_text(relative_path, "path")
    path = Path(text)
    if path.is_absolute():
        raise ValueError(f"Absolute paths are not allowed here: {text}")
    return ensure_within(root, root / path, must_exist=must_exist)


def resolve_named_file(
    root: Path,
    filename: str,
    *,
    suffix: str | None = None,
    must_exist: bool = False,
) -> Path:
    """Resolve a single filename under root, optionally enforcing a suffix."""
    name = _as_clean_text(filename, "filename")
    if suffix and not name.lower().endswith(suffix.lower()):
        name = f"{name}{suffix}"
    path = Path(name)
    if path.is_absolute() or path.name != name or name in {".", ".."}:
        raise ValueError(f"Invalid filename: {filename}")

    candidate = ensure_within(root, root / name, must_exist=must_exist)
    if suffix and candidate.suffix.lower() != suffix.lower():
        raise ValueError(f"Invalid filename suffix: {filename}")
    return candidate


def resolve_named_dir(root: Path, dirname: str, *, must_exist: bool = True) -> Path:
    """Resolve a direct child directory under root."""
    name = _as_clean_text(dirname, "directory name")
    path = Path(name)
    if path.is_absolute() or path.name != name or name.startswith((".", "__")):
        raise ValueError(f"Invalid directory name: {dirname}")
    candidate = ensure_within(root, root / name, must_exist=must_exist)
    # codeql[py/path-injection]
    if must_exist and not candidate.is_dir():
        raise NotADirectoryError(str(candidate))
    return candidate


def resolve_external_path(
    raw_path: str | Path,
    *,
    base_dir: Path | None = None,
    must_exist: bool = False,
    must_be_dir: bool = False,
    must_be_file: bool = False,
    allowed_suffixes: set[str] | None = None,
) -> Path:
    """Resolve an operator-supplied local path without confining it to the repo."""
    text = _as_clean_text(raw_path, "path")
    # codeql[py/path-injection]
    candidate = Path(text).expanduser()
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    # codeql[py/path-injection]
    candidate = candidate.resolve(strict=False)

    if allowed_suffixes is not None:
        normalized_suffixes = {suffix.lower() for suffix in allowed_suffixes}
        if candidate.suffix.lower() not in normalized_suffixes:
            raise ValueError(f"Unsupported file suffix: {candidate.suffix}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(str(candidate))
    if must_be_dir and not candidate.is_dir():
        raise NotADirectoryError(str(candidate))
    if must_be_file and not candidate.is_file():
        raise FileNotFoundError(str(candidate))
    return candidate
