"""Command-line entry point for the independent Live2D product."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .model_adapter import ModelAdaptationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the independent NachoBot Live2D app")
    parser.add_argument("--config", type=Path, help="standalone app TOML config")
    parser.add_argument("--model", type=Path, help="model3 descriptor; overrides renderer.model_path")
    parser.add_argument("--duration", type=float, help="exit after this many seconds")
    parser.add_argument("--hidden", action="store_true", help="create the real renderer window hidden")
    parser.add_argument("--no-server", action="store_true", help="run the renderer without WebSocket")
    parser.add_argument("--mode", dest="mode_option", help="legacy alias; only 'live' is supported")
    parser.add_argument("mode", nargs="?", help="legacy positional alias: live")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="run the provider debug viewer instead of the standalone application",
    )
    return parser


def _configure_logging(level: str) -> None:
    """Use the level selected by the live standalone config."""

    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level=level)


def main(
    argv: list[str] | None = None,
    *,
    app_factory=None,
) -> int:
    """Launch the native application; ``--preview`` explicitly selects debug viewer."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--preview" in arguments:
        arguments.remove("--preview")
        from .viewer import main as preview_main

        return preview_main(arguments)

    parser = _parser()
    args = parser.parse_args(arguments)
    for mode in (args.mode, args.mode_option):
        if mode is not None and mode != "live":
            parser.error("only standalone 'live' mode is supported")
    if args.mode is not None and args.mode_option is not None:
        parser.error("specify at most one legacy mode alias")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be a finite number greater than zero")
    try:
        config = load_config(
            args.config,
            model_override=args.model,
            hidden_override=True if args.hidden else None,
        )
    except ConfigError as exc:
        parser.error(str(exc))

    _configure_logging(config.log_level)
    try:
        if app_factory is None:
            from .app import StandaloneApplication

            app_factory = StandaloneApplication
        application = app_factory(
            config,
            duration=args.duration,
            no_server=args.no_server,
        )
        return asyncio.run(application.run())
    except KeyboardInterrupt:
        return 0
    except (ConfigError, ModelAdaptationError) as exc:
        print(f"Live2D adapter configuration error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Live2D adapter startup error: {exc}", file=sys.stderr)
        return 3
    except Exception:
        from loguru import logger

        logger.exception("Live2D adapter terminated unexpectedly")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
