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
        "--print-mode",
        action="store_true",
        help="Print the resolved runtime mode and exit.",
    )
    parser.add_argument(
        "--print-launch-config",
        action="store_true",
        help="Print the resolved launcher settings as JSON and exit.",
    )
    return parser


def _configure_logging(level_name: str):
    logger.remove()
    logger.add(sys.stderr, level=level_name.upper())
    return logger


async def _run(config_path: Path) -> None:
    config = load_config(config_path)
    logger = _configure_logging(config.log_level)
    runtime = AvatarRuntime(config, logger)
    server = AvatarWebSocketServer(config, runtime, logger)
    await server.run()


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.print_mode or args.print_launch_config:
            config = load_config(args.config)
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
        asyncio.run(_run(args.config))
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
