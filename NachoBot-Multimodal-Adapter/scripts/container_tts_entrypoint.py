"""Start the managed Docker TTS engine selected by the live base.toml."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import toml


ADAPTER_ROOT = Path(__file__).resolve().parents[1]


def resolve_engine(config_path: Path, override: str = "") -> str:
    normalized_override = str(override or "").strip().lower()
    aliases = {
        "gpt-sovits": "gpt-sovits",
        "gpt_sovits": "gpt-sovits",
        "voxcpm": "voxcpm",
        "vox": "voxcpm",
    }
    if normalized_override:
        if normalized_override not in aliases:
            raise ValueError("NACHOBOT_TTS_ENGINE must be gpt-sovits or voxcpm")
        return aliases[normalized_override]

    config = toml.load(str(config_path))
    enabled = config.get("enabled_tts", {}).get("enabled", [])
    if not isinstance(enabled, list) or len(enabled) != 1:
        raise ValueError("base.toml must enable exactly one TTS backend")
    selected = str(enabled[0]).strip().lower()
    if selected not in aliases:
        raise ValueError(f"unsupported TTS backend in base.toml: {enabled[0]}")
    return aliases[selected]


def main() -> None:
    parser = argparse.ArgumentParser(description="NachoBot container TTS selector")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--backend-port", type=int, default=None)
    parser.add_argument("--config", type=Path, default=ADAPTER_ROOT / "configs" / "base.toml")
    args = parser.parse_args()

    engine = resolve_engine(args.config, os.environ.get("NACHOBOT_TTS_ENGINE", ""))
    manager = ADAPTER_ROOT / "scripts" / "tts_runtime_manager.py"
    command = [
        sys.executable,
        str(manager),
        "serve",
        "--engine",
        engine,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.backend_port is not None:
        command.extend(["--backend-port", str(args.backend_port)])
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
