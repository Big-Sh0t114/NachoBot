"""Command-line entry point for the standalone Live2D adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from loguru import logger

from .config import ConfigError, load_config
from .model_adapter import ModelAdaptationError
from .runtime import AvatarRuntime
from .server import AvatarWebSocketServer


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nachobot-live2d-adapter",
        description="Run the standalone NachoBot Live2D rendering adapter.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="TOML configuration path (default: ./config.toml)",
    )
    parser.add_argument(
        "--mode",
        choices=("desktop_pet", "live"),
        help="Override [runtime].mode without editing config.toml.",
    )
    parser.add_argument(
        "--print-mode",
        action="store_true",
        help="Print the resolved runtime mode and exit.",
    )
    parser.add_argument(
        "--print-launch-config",
        action="store_true",
        help="Print the resolved launcher settings as JSON and exit.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Also write structured runtime logs to this UTF-8 file.",
    )
    return parser


def _configure_logging(level_name: str, log_file: Path | None = None):
    logger.remove()
    logger.add(sys.stderr, level=level_name.upper())
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_file),
            level=level_name.upper(),
            encoding="utf-8",
            enqueue=True,
            rotation="10 MB",
            retention=3,
            backtrace=True,
            diagnose=False,
        )
    return logger


async def _run(
    config_path: Path,
    mode: str | None = None,
    log_file: Path | None = None,
) -> None:
    config = load_config(config_path, runtime_mode_override=mode)
    logger = _configure_logging(config.log_level, log_file)
    runtime = AvatarRuntime(config, logger)
    server = AvatarWebSocketServer(config, runtime, logger)
    await server.run()


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.print_mode or args.print_launch_config:
            config = load_config(args.config, runtime_mode_override=args.mode)
            if args.print_mode:
                print(config.runtime.mode)
            else:
                print(
                    json.dumps(
                        {
                            "mode": config.runtime.mode,
                            "chat_enabled": config.desktop_pet.chat.enabled,
                            "chat_backend_url": config.desktop_pet.chat.backend_url,
                            "chat_play_audio": config.desktop_pet.chat.play_audio,
                        }
                    )
                )
            return 0
        asyncio.run(_run(args.config, args.mode, args.log_file))
    except (ConfigError, ModelAdaptationError) as exc:
        print(f"Live2D adapter configuration error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Live2D adapter startup error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.exception(
            "Live2D adapter terminated unexpectedly"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
