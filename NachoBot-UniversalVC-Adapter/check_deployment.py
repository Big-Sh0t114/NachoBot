"""Read-only deployment probe; run with UniversalVC's own Python environment."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys


def check_deployment(root: Path) -> dict:
    issues = []
    if sys.version_info < (3, 11) or sys.maxsize <= 2**32:
        issues.append("environment_invalid")
    try:
        from universalvc_service.runtime_compat import preload_onnxruntime
        preload_onnxruntime()
    except Exception:
        issues.append("dependencies_unavailable")
    # Import native dependencies too: installed package metadata does not prove
    # that the matching DLLs can load in this environment.
    for name in ("aiohttp", "numpy", "scipy", "sounddevice", "onnxruntime",
                 "sherpa_onnx", "universalvc_service.api"):
        try:
            importlib.import_module(name)
        except Exception:
            issues.append("dependencies_unavailable")
    try:
        from universalvc_service.service_config import load_service_settings
        config = root / "service.toml"
        # Authentication is established by the supervisor, including on a new
        # deployment. This probe never creates or prints credentials.
        env = dict(os.environ, NACHOBOT_UNIVERSALVC_TOKEN="deployment-probe-only")
        settings = load_service_settings(config if config.exists() or config.is_symlink() else None, environ=env)
    except Exception:
        issues.append("service_config_invalid")
        settings = None
    if settings is not None:
        for kind, path in (("vad", settings.vad_model_path),
                           ("speaker", settings.speaker_model_path)):
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    issues.append(f"{kind}_model_missing")
                    continue
                if "dependencies_unavailable" in issues:
                    continue
                # Validate the actual configured models with the same runtime
                # as sessions. Custom model paths are supported; no downloads.
                import sherpa_onnx
                if kind == "vad":
                    options = sherpa_onnx.VadModelConfig()
                    options.silero_vad.model = str(path)
                    options.sample_rate = 16000
                    model = sherpa_onnx.VoiceActivityDetector(options, buffer_size_in_seconds=1)
                else:
                    options = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path))
                    model = sherpa_onnx.SpeakerEmbeddingExtractor(options)
                del model
            except Exception:
                issues.append(f"{kind}_model_invalid")
    # Denoising is optional and is checked by sessions only when requested.
    issues = list(dict.fromkeys(issues))
    return {"ok": not issues, "issues": issues}


if __name__ == "__main__":
    result = check_deployment(Path(__file__).resolve().parent)
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 2)
