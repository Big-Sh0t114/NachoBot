"""Small validation helpers for WebUI filesystem inputs."""

from __future__ import annotations

import re
import os
from pathlib import Path, PureWindowsPath

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _as_clean_text(value: str | Path, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} cannot be empty")
    if _CONTROL_CHARS_RE.search(text):
        raise ValueError(f"{label} contains control characters")
    return text


def _reject_parent_segments(text: str, label: str) -> None:
    """Reject traversal syntax before normalization can hide it."""
    if ".." in text.replace("\\", "/").split("/"):
        raise ValueError(f"Invalid {label}")


def _reject_windows_relative_syntax(text: str, label: str) -> None:
    """Reject Windows path syntax in values intended to be relative names."""
    windows_path = PureWindowsPath(text)
    if "\\" in text or ":" in text or windows_path.drive or windows_path.root:
        raise ValueError(f"Invalid {label}")


def _reject_alternate_streams(text: str) -> None:
    """Reject NTFS alternate data stream syntax in path components."""
    windows_path = PureWindowsPath(text)
    components = windows_path.parts[1:] if windows_path.drive else windows_path.parts
    if any(":" in component for component in components):
        raise ValueError("Invalid path")


def ensure_within(root: Path, path: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve a path and require it to remain under root."""
    raw_path = os.fspath(path)
    raw_text = os.fsdecode(raw_path)
    _reject_parent_segments(raw_text, "path")
    _reject_alternate_streams(raw_text)
    # Backslashes and Windows-only anchors are never valid relative syntax.
    # Native absolute Windows paths remain supported for operator-selected
    # external roots; the resolved root check below still confines children.
    if "\\" in raw_text and (os.name != "nt" or not Path(raw_text).is_absolute()):
        raise ValueError("Invalid path")
    if PureWindowsPath(raw_text).drive and not Path(raw_text).is_absolute():
        raise ValueError("Invalid path")
    root_resolved = root.resolve()
    # codeql[py/path-injection]
    candidate = Path(path).resolve(strict=False)
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError("Path is outside the allowed directory")
    # codeql[py/path-injection]
    if must_exist and not candidate.exists():
        raise FileNotFoundError("Required path does not exist")
    return candidate


def resolve_relative_to_root(
    root: Path,
    relative_path: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a relative path under root."""
    text = _as_clean_text(relative_path, "path")
    _reject_parent_segments(text, "relative path")
    _reject_windows_relative_syntax(text, "relative path")
    path = Path(text)
    if path.is_absolute():
        raise ValueError("Absolute paths are not allowed here")
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
    _reject_parent_segments(name, "filename")
    _reject_windows_relative_syntax(name, "filename")
    path = Path(name)
    if path.is_absolute() or path.name != name or name in {".", ".."}:
        raise ValueError("Invalid filename")

    candidate = ensure_within(root, root / name, must_exist=must_exist)
    if suffix and candidate.suffix.lower() != suffix.lower():
        raise ValueError("Invalid filename suffix")
    return candidate


def resolve_named_dir(root: Path, dirname: str, *, must_exist: bool = True) -> Path:
    """Resolve a direct child directory under root."""
    name = _as_clean_text(dirname, "directory name")
    _reject_parent_segments(name, "directory name")
    _reject_windows_relative_syntax(name, "directory name")
    path = Path(name)
    if path.is_absolute() or path.name != name or name.startswith((".", "__")):
        raise ValueError("Invalid directory name")
    candidate = ensure_within(root, root / name, must_exist=must_exist)
    # codeql[py/path-injection]
    if must_exist and not candidate.is_dir():
        raise NotADirectoryError("Expected a directory")
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
    windows_path = PureWindowsPath(text)
    if os.name != "nt" and (windows_path.drive or "\\" in text):
        # Windows-only paths cannot identify a local target on other platforms.
        # On Windows, preserve operator-selected drive, UNC, and relative paths.
        raise ValueError("Invalid external path")
    if os.name == "nt" and windows_path.drive and not candidate.is_absolute():
        raise ValueError("Invalid external path")
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    # codeql[py/path-injection]
    candidate = candidate.resolve(strict=False)

    if allowed_suffixes is not None:
        normalized_suffixes = {suffix.lower() for suffix in allowed_suffixes}
        if candidate.suffix.lower() not in normalized_suffixes:
            raise ValueError(f"Unsupported file suffix: {candidate.suffix}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError("Required path does not exist")
    if must_be_dir and not candidate.is_dir():
        raise NotADirectoryError("Expected a directory")
    if must_be_file and not candidate.is_file():
        raise FileNotFoundError("Required file does not exist")
    return candidate
