"""Small standalone viewer used to render and smoke-test this provider."""

from __future__ import annotations

import argparse
import logging
import math
import time
import tomllib
from pathlib import Path
from typing import Any

from .model_runtime import Live2DModelRuntime, RuntimeFrame, RuntimeOptions


def _read_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        value = tomllib.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"viewer config must contain TOML tables: {path}")
    return value


def _finite(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="View a Live2D model with the standalone provider")
    parser.add_argument("--config", type=Path, help="viewer TOML config (defaults to config.toml)")
    parser.add_argument("--model", type=Path, help="model3 descriptor; overrides viewer.model_path")
    parser.add_argument("--duration", type=float, help="exit after this many seconds")
    parser.add_argument("--hidden", action="store_true", help="create a hidden OpenGL window")
    parser.add_argument("--width", type=int, help="override the configured viewport width")
    parser.add_argument("--height", type=int, help="override the configured viewport height")
    parser.add_argument("--scale", type=float, help="override the configured model scale")
    parser.add_argument("--fps", type=int, help="override the configured frame rate")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be a finite number greater than zero")

    default_config = Path(__file__).resolve().parent.parent / "config.toml"
    config_path = (args.config or default_config).expanduser().resolve()
    if args.config is not None or config_path.is_file():
        try:
            config = _read_config(config_path)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as exc:
            parser.error(f"cannot read config {config_path}: {exc}")
    else:
        # Wheels contain the provider package only. An explicit model path must
        # work without a checkout-local sample config beside site-packages.
        config = {}
    viewer = config.get("viewer", {})
    adaptation = config.get("adaptation", {})
    if not isinstance(viewer, dict) or not isinstance(adaptation, dict):
        parser.error("config [viewer] and [adaptation] values must be TOML tables")

    configured_model = viewer.get("model_path")
    if args.model is None and not str(configured_model or "").strip():
        parser.error("set viewer.model_path or pass --model")
    model_path = args.model or Path(str(configured_model))
    if args.model is None and not model_path.is_absolute():
        model_path = config_path.parent / model_path
    model_path = model_path.expanduser().resolve()

    width = max(1, args.width if args.width is not None else int(viewer.get("width", 800)))
    height = max(1, args.height if args.height is not None else int(viewer.get("height", 600)))
    scale = _finite(args.scale if args.scale is not None else viewer.get("scale", 1.0), 1.0)
    fps = max(1, args.fps if args.fps is not None else int(viewer.get("fps", 60)))
    hidden = bool(args.hidden or viewer.get("hidden", False))
    antialiasing = bool(viewer.get("antialiasing", True))

    adaptation_parameters = adaptation.get("parameters", {})
    adaptation_expressions = adaptation.get("expressions", {})
    actions = config.get("actions", {})
    if not isinstance(adaptation_parameters, dict):
        parser.error("[adaptation.parameters] must be a TOML table")
    if not isinstance(adaptation_expressions, dict) or not isinstance(actions, dict):
        parser.error("[adaptation.expressions] and [actions] must be TOML tables")

    options = RuntimeOptions(
        width=width,
        height=height,
        scale=scale,
        adaptation_enabled=bool(adaptation.get("enabled", True)),
        parameter_mappings=adaptation_parameters,
        expression_mappings=adaptation_expressions,
        action_mappings=actions,
    )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import pygame

    runtime = Live2DModelRuntime()
    try:
        pygame.init()
        if antialiasing:
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 4)
        else:
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 0)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 0)
        flags = pygame.DOUBLEBUF | pygame.OPENGL
        if hidden:
            flags |= getattr(pygame, "HIDDEN", 0)
        pygame.display.set_mode((width, height), flags)
        pygame.display.set_caption(f"Live2D Provider — {model_path.stem}")
        info = runtime.initialize(model_path, options=options)

        from OpenGL.GL import (
            GL_COLOR_BUFFER_BIT,
            GL_DEPTH_BUFFER_BIT,
            glClear,
            glClearColor,
        )

        clock = pygame.time.Clock()
        start = time.monotonic()
        previous = start
        frames = 0
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    running = False
            if not running:
                break

            now = time.monotonic()
            delta = max(0.0, now - previous)
            previous = now
            glClearColor(0.5, 0.5, 0.5, 1.0)
            glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
            runtime.update_draw(RuntimeFrame(now_seconds=now, delta=delta))
            pygame.display.flip()
            frames += 1
            clock.tick(fps)
            if args.duration is not None and now - start >= args.duration:
                running = False

        elapsed = time.monotonic() - start
        print(
            f"provider_render_ok frames={frames} elapsed={elapsed:.3f}s "
            f"model={info.model_path} parameters={len(info.parameter_ids)}"
        )
        return 0
    except Exception as exc:
        logging.getLogger("nachobot.live2d.provider").exception("Live2D provider viewer failed")
        print(f"Live2D provider viewer failed: {exc}")
        return 1
    finally:
        # initialize() rolls back partial SDK/model setup on failure. Repeating
        # dispose is safe and keeps the host context current through cleanup.
        runtime.dispose()
        pygame.quit()
