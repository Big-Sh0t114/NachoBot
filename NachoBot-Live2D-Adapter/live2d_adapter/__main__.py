"""Command-line entry point for the standalone Live2D adapter."""

from __future__ import annotations

import argparse
import asyncio
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
