"""Start the managed Docker TTS engine selected by the live base.toml."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))

from nachobot_multimodal.utils.model_devices import apply_device_environment  # noqa: E402
from scripts.tts_runtime_manager import resolve_engine as resolve_managed_engine  # noqa: E402
from scripts.tts_public_runtime import resolve_public_python  # noqa: E402


def resolve_engine(config_path: Path, override: str = "") -> str:
    return resolve_managed_engine(config_path, override)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="NachoBot container TTS selector")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--backend-port", type=int, default=None)
    parser.add_argument("--config", type=Path, default=ADAPTER_ROOT / "configs" / "base.toml")
    args = parser.parse_args(argv)

    engine = resolve_engine(args.config, os.environ.get("NACHOBOT_TTS_ENGINE", ""))
    apply_device_environment(
        "tts",
        config_dir=args.config.parent,
        base_path=args.config,
        runtime_profile=os.environ.get("NACHOBOT_TTS_RUNTIME_PROFILE", "gpu"),
        engine=engine,
    )
    manager = ADAPTER_ROOT / "scripts" / "tts_runtime_manager.py"
    public_python = resolve_public_python(engine, args.config, platform=os.name)
    command = [
        public_python,
        str(manager),
        "serve",
        "--engine",
        engine,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--config",
        str(args.config),
    ]
    if args.backend_port is not None:
        command.extend(["--backend-port", str(args.backend_port)])
    if os.name == "nt":
        child = subprocess.run(command, check=False)
        raise SystemExit(child.returncode)
    os.execv(public_python, command)


if __name__ == "__main__":
    main()
