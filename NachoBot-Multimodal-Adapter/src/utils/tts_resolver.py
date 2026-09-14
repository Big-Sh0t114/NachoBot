"""Resolve the active TTS backend and identify its consumed configuration.

The adapters keep TTS clients alive while their transports keep running.  A
startup class therefore cannot be treated as the current backend forever.  The
snapshot resolver reads the base file and the selected backend file together,
hashes their contents, and carries the exact paths into the shared runtime so
known backends consume the same file that was fingerprinted.
"""

from dataclasses import dataclass
import hashlib
import importlib
from pathlib import Path
from typing import Any, Optional, Tuple

import toml


_BACKEND_CONFIGS = {
    "GPT_Sovits": "gpt-sovits.toml",
    "Vox": "vox.toml",
}


@dataclass(frozen=True)
class TTSResolution:
    """One immutable resolution read used for selection and fingerprinting."""

    model_class: Optional[Any]
    error: Optional[str]
    fingerprint: Optional[str]
    plugin: Optional[str]
    base_config_path: Optional[Path]
    backend_config_path: Optional[Path]


def _default_configs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "configs"


def _config_location(config_dir: Optional[Path] = None) -> Tuple[Path, Path]:
    """Return ``(configs_dir, base_path)`` while honoring a supplied file."""

    if config_dir is None:
        configs_dir = _default_configs_dir()
        return configs_dir, configs_dir / "base.toml"

    path = Path(config_dir)
    if path.suffix.lower() == ".toml":
        return path.parent, path
    return path, path / "base.toml"


def _content(path: Path) -> Tuple[bytes, Optional[str]]:
    try:
        return path.read_bytes(), None
    except OSError as exc:
        return b"", f"Config file not found: {path} ({exc})"


def _fingerprint(*parts: Tuple[str, bytes]) -> str:
    digest = hashlib.sha256()
    for label, value in parts:
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value)
        digest.update(b"\0")
    return digest.hexdigest()


def _import_tts_model(target_plugin: str) -> Any:
    """Import a plugin class using the historical package fallback."""

    module_name = f"nachobot_multimodal.tts.backends.{target_plugin}"
    module = importlib.import_module(module_name)
    if hasattr(module, "TTSModel"):
        return module.TTSModel

    module_name = f"nachobot_multimodal.tts.backends.{target_plugin}.tts_model"
    module = importlib.import_module(module_name)
    return module.TTSModel


def _import_result(
    target_plugin: str,
    fingerprint: Optional[str],
    base_path: Optional[Path],
    backend_path: Optional[Path],
) -> TTSResolution:
    try:
        model_class = _import_tts_model(target_plugin)
        return TTSResolution(
            model_class,
            None,
            fingerprint,
            target_plugin,
            base_path,
            backend_path,
        )
    except ImportError as exc:
        error = f"Failed to import TTS model {target_plugin}: {exc}"
    except AttributeError as exc:
        error = f"TTSModel class not found in {target_plugin}: {exc}"
    except Exception as exc:
        error = f"Unexpected error resolving TTS model {target_plugin}: {exc}"
    return TTSResolution(None, error, fingerprint, target_plugin, base_path, backend_path)


def resolve_tts_model_snapshot(
    config_dir: Optional[Path] = None,
    *,
    fixed_backend: Optional[str] = None,
) -> TTSResolution:
    """Read and resolve the active backend in one content-aware operation.

    ``config_dir`` may be a directory or an explicitly named ``base.toml``
    path. ``fixed_backend`` is used by the legacy GPT API server: it reads and
    hashes only that backend file and deliberately ignores global backend
    selection and server metadata.
    """

    configs_dir, base_path = _config_location(config_dir)

    if fixed_backend:
        backend_filename = _BACKEND_CONFIGS.get(fixed_backend)
        if backend_filename is None:
            return TTSResolution(
                None,
                f"Unknown fixed TTS plugin: {fixed_backend}",
                None,
                fixed_backend,
                None,
                None,
            )
        backend_path = configs_dir / backend_filename
        backend_bytes, backend_error = _content(backend_path)
        fingerprint = _fingerprint(
            ("fixed-backend", fixed_backend.encode("utf-8")),
            ("fixed-config-path", str(backend_path.resolve()).encode("utf-8")),
            ("fixed-config", backend_bytes),
        )
        if backend_error:
            return TTSResolution(None, backend_error, fingerprint, fixed_backend, None, backend_path)
        try:
            toml.loads(backend_bytes.decode("utf-8"))
        except Exception as exc:
            return TTSResolution(
                None,
                f"Failed to parse selected TTS config {backend_path}: {exc}",
                fingerprint,
                fixed_backend,
                None,
                backend_path,
            )
        return _import_result(fixed_backend, fingerprint, None, backend_path)

    base_bytes, base_error = _content(base_path)
    if base_error:
        return TTSResolution(None, base_error, None, None, base_path, None)
    try:
        config_data = toml.loads(base_bytes.decode("utf-8"))
    except Exception as exc:
        return TTSResolution(None, f"Failed to parse config file {base_path}: {exc}", None, None, base_path, None)

    if not isinstance(config_data, dict):
        return TTSResolution(
            None,
            f"Invalid TOML root in {base_path}: expected a table",
            None,
            None,
            base_path,
            None,
        )

    enabled_section = config_data.get("enabled_tts", {})
    if not isinstance(enabled_section, dict):
        return TTSResolution(
            None,
            "Invalid [enabled_tts] in base.toml: expected a table",
            None,
            None,
            base_path,
            None,
        )
    enabled = enabled_section.get("enabled", [])
    if isinstance(enabled, (str, bytes)) or not isinstance(enabled, list):
        return TTSResolution(
            None,
            "Invalid base.toml [enabled_tts.enabled]: expected a list",
            None,
            None,
            base_path,
            None,
        )
    if not enabled:
        return TTSResolution(
            None,
            "No TTS plugins enabled in base.toml [enabled_tts.enabled]",
            None,
            None,
            base_path,
            None,
        )
    if any(not isinstance(item, str) or not item.strip() for item in enabled):
        return TTSResolution(
            None,
            "Invalid base.toml [enabled_tts.enabled]: entries must be non-empty strings",
            None,
            None,
            base_path,
            None,
        )
    if "GPT_Sovits" in enabled and "Vox" in enabled:
        return TTSResolution(
            None,
            "Both GPT_Sovits and Vox are enabled. Please select only one.",
            None,
            None,
            base_path,
            None,
        )

    target_plugin = enabled[0]
    backend_filename = _BACKEND_CONFIGS.get(target_plugin)
    if backend_filename is None:
        # Preserve the legacy resolver's extensible dynamic plugin contract.
        fingerprint = _fingerprint(
            ("base", base_bytes),
            ("selected-backend", target_plugin.encode("utf-8")),
        )
        return _import_result(target_plugin, fingerprint, base_path, None)

    backend_path = configs_dir / backend_filename
    backend_bytes, backend_error = _content(backend_path)
    fingerprint = _fingerprint(
        ("base", base_bytes),
        ("selected-backend", target_plugin.encode("utf-8")),
        ("backend", backend_bytes),
    )
    if backend_error:
        return TTSResolution(None, backend_error, fingerprint, target_plugin, base_path, backend_path)
    try:
        toml.loads(backend_bytes.decode("utf-8"))
    except Exception as exc:
        return TTSResolution(
            None,
            f"Failed to parse selected TTS config {backend_path}: {exc}",
            fingerprint,
            target_plugin,
            base_path,
            backend_path,
        )
    return _import_result(target_plugin, fingerprint, base_path, backend_path)


def resolve_tts_model_class_with_fingerprint(
    config_dir: Optional[Path] = None,
) -> Tuple[Optional[Any], Optional[str], Optional[str]]:
    """Compatibility helper returning ``(class, error, fingerprint)``."""

    snapshot = resolve_tts_model_snapshot(config_dir)
    return snapshot.model_class, snapshot.error, snapshot.fingerprint


def resolve_tts_model_class() -> Tuple[Optional[Any], Optional[str]]:
    """Legacy two-value startup resolver; dynamic plugin imports are retained."""

    model_class, error, _fingerprint_value = resolve_tts_model_class_with_fingerprint()
    return model_class, error
