"""Bounded Sprite preview and native Win32 alpha-window smoke checks."""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from desktop_pet_adapter.backend import BackendContext
from desktop_pet_adapter.config import load_config
from desktop_pet_adapter.sprite_backend import SpriteBackend
from desktop_pet_adapter.sprite_renderer import SpriteRenderer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_DIR / "config.toml")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preview", action="store_true", help="render an off-screen PNG")
    modes.add_argument(
        "--native-window",
        action="store_true",
        help="briefly exercise the real Win32 layered-alpha window",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--duration", type=float, default=3.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.preview:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    config = load_config(
        args.config,
        runtime_mode_override="desktop_pet",
        renderer_backend_override="sprite",
    )
    # The smoke never creates a controller, service process, chat host, or tray.
    config = replace(
        config,
        components=replace(config.components, core=False, tts=False, perception=False),
        desktop_pet=replace(
            config.desktop_pet,
            enabled=True,
            tray_icon=False,
            chat=replace(config.desktop_pet.chat, enabled=False),
            interactions=replace(config.desktop_pet.interactions, enabled=False),
        ),
    )
    logger = _SmokeLogger()
    backend = SpriteBackend(config, logger)
    backend.prepare()
    renderer = SpriteRenderer(
        BackendContext(
            config=config,
            logger=logger,
            command_queue=queue.Queue(),
            voice_stream_queue=queue.Queue(maxsize=64),
            on_click=lambda _button: None,
            on_ready=lambda: None,
            on_exit=lambda: None,
        ),
        backend.manifest,
        enable_desktop_controller=False,
    )
    output = args.output or PROJECT_DIR / "logs" / (
        "sprite-preview.png" if args.preview else "sprite-native-smoke.png"
    )
    if args.preview:
        result = renderer.run_preview_smoke(output)
        import pygame

        passed = (
            len(result["frames_seen"]) > 1
            and result["movement_pixels"] > 0
            and not renderer.running
            and not pygame.get_init()
        )
    else:
        if os.name != "nt":
            raise RuntimeError("--native-window is available only on Windows")
        result = renderer.run_native_smoke(output, duration_seconds=args.duration)
        passed = (
            result.get("layered_style") is True
            and result.get("update_layered_window_calls", 0) > 0
            and len(result.get("frames_seen", [])) > 1
            and result.get("movement_pixels", 0) > 0
            and result.get("visibility_cycle_verified") is True
            and result.get("window_destroyed") is True
            and result.get("pygame_cleaned") is True
            and not renderer.running
        )
    result["passed"] = passed
    result["services_started"] = False
    result["chat_started"] = False
    result["tray_started"] = False
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


class _SmokeLogger:
    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def debug(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
