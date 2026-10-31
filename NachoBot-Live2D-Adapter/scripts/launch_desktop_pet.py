"""Desktop startup orchestration; model services run in independent processes."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main(argv=None) -> int:
    with tempfile.TemporaryDirectory(prefix="nachobot-desktop-session-") as directory:
        return launch(argv, Path(directory) / "active")


def launch(argv, lease) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("desktop_pet", "live"))
    parser.add_argument("--show-logs", choices=("ALL", "LIVE2D", "CORE"))
    args = parser.parse_args(argv)
    service_dir = ROOT / "NachoBot-Multimodal-Adapter"
    startup_path = service_dir / "startup.toml"
    child_env = os.environ.copy()
    child_env["NACHOBOT_DESKTOP_STARTUP_CONFIG"] = str(startup_path)
    if not args.show_logs:
        with args.config.open("rb") as stream:
            config = tomllib.load(stream)
        mode = (
            args.mode
            or os.environ.get("NACHOBOT_LIVE2D_MODE")
            or config.get("runtime", {}).get(
                "mode",
                "desktop_pet"
                if config.get("desktop_pet", {}).get("enabled")
                else "live",
            )
        )
        if mode == "desktop_pet":
            with startup_path.open("rb") as file:
                startup = tomllib.load(file)
            core = startup.get("components", {}).get("core", True)
            if type(core) is not bool:
                raise ValueError("startup.toml: components.core must be boolean")
            if not core:
                print(
                    "[Standalone] Core disabled; running the desktop pet without backend services.",
                    flush=True,
                )
            else:
                lease.touch()
                result = subprocess.run(
                    [
                        sys.executable,
                        str(service_dir / "scripts" / "service_launcher.py"),
                        "ensure",
                        "--owner-pid",
                        str(os.getpid()),
                        "--owner-lease",
                        str(lease),
                    ],
                    cwd=service_dir,
                    check=False,
                )
                if result.returncode:
                    return result.returncode
    command = [
        sys.executable,
        "-u",
        "-m",
        "live2d_adapter.launcher",
        "--config",
        str(args.config),
    ]
    if args.mode:
        command += ["--mode", args.mode]
    if args.show_logs:
        command += ["--show-logs", args.show_logs]
    return subprocess.run(command, check=False, env=child_env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
