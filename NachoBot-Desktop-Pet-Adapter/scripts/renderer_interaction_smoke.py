"""Bounded, no-service smoke for the actual Live2D renderer integration.

Run from the adapter with:
    .venv/Scripts/python.exe scripts/renderer_interaction_smoke.py

The child loads the configured model into the real pygame/OpenGL renderer for
five seconds, keeps its window hidden, disables tray/chat/autonomous mutations,
and verifies that the render-loop desktop scene sampler returns a snapshot.
The parent enforces a hard timeout in case native model loading or shutdown
hangs. It refuses to run beside a normally launched desktop pet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pet_processes() -> tuple[str, ...]:
    """Return normal adapter processes that would conflict with this smoke."""

    query = (
        "$ErrorActionPreference='Stop'; "
        "$items = Get-CimInstance Win32_Process | Where-Object { "
        "((($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and "
        "($_.CommandLine -match 'desktop_pet_adapter\\.launcher' -or "
        "$_.CommandLine -match '(?:^|\\s)-m\\s+desktop_pet_adapter(?:\\s|$)' -or "
        "$_.CommandLine -match 'scripts[\\\\/]launch_desktop_pet\\.py')) -or "
        "$_.Name -eq 'desktop-pet-adapter.exe' -or "
        "$_.Name -eq 'nachobot-live2d-adapter.exe') }; "
        "$items | ForEach-Object { '{0}`t{1}' -f $_.ProcessId,$_.CommandLine }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "could not check for an existing pet process: "
            + (result.stderr.strip() or f"PowerShell exit {result.returncode}")
        )
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _config_text(model_path: Path) -> str:
    model_path_text = json.dumps(str(model_path))
    return f"""[server]
host = "127.0.0.1"
port = 18766

[runtime]
mode = "desktop_pet"

[renderer]
model_path = {model_path_text}
transparent = true
antialiasing = false
width = 520
height = 760
scale = 0.9
track_mouse = false

[components]
core = false
tts = false
perception = false

[desktop_pet]
show_core_console = false
remember_position = false
state_path = ""
tray_icon = false
margin = 24

[desktop_pet.interactions]
enabled = false
window_approach = false
icon_approach = false
follow_foreground_window = false
move_windows = false
move_icons = false
gui_inspection = false
gui_actions = false
scene_interval_seconds = 0.5
action_timeout_seconds = 2.0

[desktop_pet.chat]
enabled = false
play_audio = false
history_path = ""
window_state_path = ""

[desktop_pet.perception]
vision = false
speech = false

[adaptation]
enabled = true

[logging]
level = "ERROR"
"""


def _run_renderer_child() -> int:
    if sys.platform != "win32":
        print("SKIP: renderer integration smoke requires Windows")
        return 0
    from desktop_pet_adapter.backend import BackendContext, create_backend
    from desktop_pet_adapter.config import load_config

    default_config = load_config(ROOT / "config.toml")
    model_path = default_config.renderer.model_path
    if model_path is None or not model_path.is_file():
        print(f"FAIL: model file not found: {model_path}", file=sys.stderr)
        return 2

    from loguru import logger

    warnings: list[str] = []
    logger.remove()
    logger.add(
        lambda message: warnings.append(str(message).strip()),
        level="WARNING",
        format="{level}: {message}",
    )
    command_queue = __import__("queue").Queue()
    renderer = None
    frame_count = 0
    snapshot_seen = threading.Event()
    quit_watcher: threading.Thread | None = None
    with tempfile.TemporaryDirectory(prefix="nachobot-renderer-smoke-") as temp_dir:
        temp_path = Path(temp_dir)
        config_path = temp_path / "smoke.toml"
        startup_path = temp_path / "startup.toml"
        config_path.write_text(_config_text(model_path), encoding="utf-8")
        startup_path.write_text(
            "[components]\ncore = false\ntts = false\nperception = false\n",
            encoding="utf-8",
        )
        os.environ["NACHOBOT_DESKTOP_STARTUP_CONFIG"] = str(startup_path)
        config = load_config(config_path, runtime_mode_override="desktop_pet")
        if config.renderer.model_path is None or not config.renderer.model_path.is_file():
            print(f"FAIL: model file not found: {config.renderer.model_path}", file=sys.stderr)
            return 2

        def on_ready() -> None:
            nonlocal quit_watcher

            def watch_scene_then_quit() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    session = renderer._desktop_interactions if renderer else None
                    if session is not None and session.scene is not None:
                        snapshot_seen.set()
                    time.sleep(0.05)
                command_queue.put(("desktop_quit", None))

            quit_watcher = threading.Thread(
                target=watch_scene_then_quit,
                name="renderer-smoke-deadline",
                daemon=True,
            )
            quit_watcher.start()

        backend = create_backend(config.renderer.backend, config, logger)
        backend.prepare()
        renderer = backend.create_renderer(
            BackendContext(
                config=config,
                logger=logger,
                command_queue=command_queue,
                voice_stream_queue=__import__("queue").Queue(maxsize=64),
                on_click=lambda _button: None,
                on_ready=on_ready,
                on_exit=lambda: None,
            )
        )
        renderer.window_visible = False
        renderer._start_desktop_chat_host = lambda: None
        original_draw = renderer._update_and_draw_model

        def count_frames(frame) -> None:
            nonlocal frame_count
            frame_count += 1
            original_draw(frame)

        renderer._update_and_draw_model = count_frames
        try:
            renderer.run()
        except Exception as exc:
            print(f"FAIL: renderer raised {type(exc).__name__}: {exc}", file=sys.stderr)
            for warning in warnings:
                print(warning, file=sys.stderr)
            return 1
        finally:
            if quit_watcher is not None:
                quit_watcher.join(timeout=1.0)

        session = renderer._desktop_interactions
        scene = session.scene if session is not None else None
        if frame_count < 20:
            print(f"FAIL: renderer produced only {frame_count} frames", file=sys.stderr)
            return 1
        if not snapshot_seen.is_set() or scene is None:
            print("FAIL: render-loop desktop snapshot was not observed", file=sys.stderr)
            return 1
        if renderer.running or session is None or not session._stopped:
            print("FAIL: renderer/session did not shut down cleanly", file=sys.stderr)
            return 1
        if warnings:
            print("FAIL: renderer emitted warnings:", file=sys.stderr)
            for warning in warnings:
                print(warning, file=sys.stderr)
            return 1
        print(
            "PASS: "
            f"frames={frame_count} "
            f"snapshot_windows={len(scene.windows)} "
            f"desktop_icons={len(scene.icons)} "
            "mutations=disabled hidden_window=true services=disabled clean_exit=true warnings=0"
        )
        return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--renderer-child":
        return _run_renderer_child()
    if sys.platform != "win32":
        print("SKIP: renderer integration smoke requires Windows")
        return 0
    try:
        existing = _pet_processes()
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if existing:
        print("SKIP: a normal adapter pet process is already running:")
        for process in existing:
            print(process)
        return 0
    command = [sys.executable, __file__, "--renderer-child"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        print("FAIL: renderer integration smoke exceeded its 25 second limit", file=sys.stderr)
        return 124
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    print(f"renderer_smoke_exit={result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
