# NachoBot Independent Live2D

This project has two surfaces: an independent Live2D application and a pure provider API for other hosts. The application owns its native window, OpenGL context, model rendering, mouse tracking, local audio playback, and versioned WebSocket command server. It does not start NachoBot Core, TTS, chat, or the DesktopPet application.

## Run the application

On Windows, run `launch_live2d.bat`, or use the project environment directly:

```bat
cd NachoBot-Live2D-Adapter
uv sync --locked
uv run --no-sync python -m live2d_adapter
```

The default renderer opens the Live2D window and listens at `ws://127.0.0.1:8766`. `config.toml` contains the renderer, server, adaptation, canonical action mapping, and logging settings. The product defaults are 1400×1200, scale 1.0, transparent, antialiased, mouse tracking disabled, and a 10-second poke cooldown. The model path and action groups match the standalone adapter configuration from before the desktop-pet merge.

Useful bounded launch options are `--model PATH`, `--config PATH`, `--duration SECONDS`, `--hidden`, and `--no-server`. `--hidden` still starts the full Live2D renderer and OpenGL context. `--no-server` runs only that renderer window.

The gray provider viewer is retained for explicit debugging only. Normal launch always uses the full Live2D application; `--preview` is the only way to select this viewer:

```bat
uv run --no-sync python -m live2d_adapter --preview --model resources/NachoBot/Nachobot.model3.json --duration 3 --hidden
```

## WebSocket protocol

The local server implements `avatar.command` and `avatar.interaction` protocol version 1.1. It emits `ready`, `click`, `poke`, `pong`, `reply_prepared`, `control_applied`, and `error` interactions. Commands include `ping`, `state`, `speaking`, `emotion`, `action`, `motion`, `random_motion`, `gaze`, `param_tween`, `play_audio`, `queue_audio`, `voice_stream`, `stop_audio`, `prepare_reply`, `apply_control`, and `shutdown`.

Set `[server].token` in `config.toml` to require a token query parameter when clients connect. The default server listens only on loopback.

## Provider API

Importing `live2d_adapter` exposes the model metadata and engine composition API without creating a window or starting the application. A host creates its own current OpenGL context, calls `Live2DModelRuntime.initialize(model_path, options=...)`, then calls `update_draw(RuntimeFrame(...))` on that render thread. `dispose()` must run before the host destroys the context. The returned `ModelInfo` and `parameter_snapshot()` contain copied values; the native model stays private to the runtime.

- `RuntimeOptions` contains viewport, transform, and value-only model mapping settings.
- `RuntimeFrame` contains monotonic frame time, delta, optional gaze coordinates in Live2D unit space (`-1..1`), interaction/drag flags, and speaking state.
- `command(kind, payload)` accepts model operations such as gaze, motions, expressions, tweens, and canonical actions.
- `set_parameter`, `reset_parameters`, and `parameter_snapshot` support copied parameter values without sharing native handles.
