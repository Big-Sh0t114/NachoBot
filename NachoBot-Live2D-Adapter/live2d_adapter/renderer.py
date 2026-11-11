"""Independent pygame window and event host for the pure Live2D provider."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygame

from .model_adapter import Live2DModelAdapter
from .model_runtime import Live2DModelRuntime, ModelInfo, RuntimeFrame, RuntimeOptions
from .renderer_audio import RendererAudio
from .windows import (
    configure_renderer_window,
    get_cursor_position,
    move_renderer_window,
    per_monitor_dpi_context,
)

_STATE_GAZE: dict[str, tuple[float, float]] = {
    "start_viewing": (-0.5, -0.2),
    "start_thinking": (0.3, 0.5),
    "start_replying": (0.0, 0.0),
    "finish_reply": (0.0, 0.0),
}


class Live2DRenderer:
    """Own one standalone native window while composing the pure model runtime.

    The pygame display, OpenGL context, mouse events, and mixer stay on the
    calling render thread. Cubism operations are delegated to
    :class:`Live2DModelRuntime` and no desktop-pet services are imported.
    """

    def __init__(
        self,
        model_path: str | Path,
        logger: Any,
        command_queue: queue.Queue[Any],
        transparent: bool = False,
        antialiasing: bool = True,
        width: int = 800,
        height: int = 600,
        scale: float = 1.0,
        track_mouse: bool = False,
        on_click: Callable[[int], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        voice_stream_queue: queue.Queue[dict[str, Any]] | None = None,
        model_adapter: Live2DModelAdapter | None = None,
        runtime_options: RuntimeOptions | None = None,
        runtime_factory: Callable[[], Any] | None = None,
        hidden: bool = False,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self.logger = logger or logging.getLogger("nachobot.live2d.renderer")
        self.command_queue = command_queue
        self.transparent = bool(transparent)
        self.antialiasing = bool(antialiasing)
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.scale = self._positive_scale(scale)
        self.track_mouse = bool(track_mouse)
        self.hidden = bool(hidden)
        self.on_click = on_click
        self.on_ready = on_ready
        self.on_exit = on_exit
        self.model_adapter = model_adapter

        options = runtime_options or RuntimeOptions()
        if not isinstance(options, RuntimeOptions):
            raise TypeError("runtime_options must be a RuntimeOptions instance")
        self.offset_x = float(options.offset_x)
        self.offset_y = float(options.offset_y)
        self.runtime_options = replace(
            options,
            width=self.width,
            height=self.height,
            scale=self.scale,
            offset_x=self.offset_x,
            offset_y=self.offset_y,
        )
        self._runtime_factory = runtime_factory
        self.model_runtime: Any = None
        self.model_info: ModelInfo | None = None
        self.hwnd: int | None = None
        self.display: pygame.Surface | None = None
        self.running = False
        self._stop_event = threading.Event()
        self._run_started = False
        self._pygame_initialized = False
        self._shutdown_complete = False
        self.dpi_awareness_enabled = False
        self._frames_rendered = 0
        self._first_frame_window_setup_done = False
        self.dragging_model = False
        self.dragging_window = False
        self.last_mouse_pos = (0, 0)
        self.last_global_mouse_pos = (0, 0)
        self.btn_6_down = False
        self.btn_7_down = False
        self.last_interaction_time = 0.0
        self._audio = RendererAudio(pygame, self.logger, voice_stream_queue)

    @property
    def pcm_audio_ready(self) -> bool:
        return self._audio.pcm_audio_ready

    @property
    def audio_ready(self) -> bool:
        return self._audio.audio_ready

    @property
    def audio_ready_event(self) -> threading.Event:
        return self._audio.audio_ready_event

    @property
    def is_speaking(self) -> bool:
        return self._audio.is_speaking

    @property
    def frames_rendered(self) -> int:
        return self._frames_rendered

    def request_stop(self) -> None:
        """Stop the render loop without injecting an operating-system input."""

        self._stop_event.set()
        self.running = False

    def run(self) -> None:
        """Create the window, load the model, render until stopped, and clean up."""

        if self._run_started:
            raise RuntimeError("Live2DRenderer can only be run once")
        self._run_started = True
        try:
            with per_monitor_dpi_context() as dpi_enabled:
                self.dpi_awareness_enabled = bool(dpi_enabled)
                if dpi_enabled:
                    self._log("debug", "[Live2D] Per-monitor DPI context enabled")
                try:
                    self._run_renderer()
                finally:
                    # The thread DPI mode owns the complete native window lifetime.
                    self._shutdown()
        finally:
            # If setting the thread DPI context itself failed, still clean up.
            if not self._shutdown_complete:
                self._shutdown()

    def _run_renderer(self) -> None:
        if self._stop_event.is_set():
            return
        if not self.model_path.is_file():
            raise FileNotFoundError(f"Live2D model descriptor not found: {self.model_path}")

        self._initialize_window()
        if self._runtime_factory is None:
            self.model_runtime = Live2DModelRuntime(logger=self.logger)
        else:
            self.model_runtime = self._runtime_factory()
        self.model_info = self.model_runtime.initialize(
            str(self.model_path), options=self.runtime_options
        )
        self.model_runtime.resize(self.width, self.height)
        self.model_runtime.set_transform(self.scale, self.offset_x, self.offset_y)
        self._bind_adapter_metadata()
        self._disable_culling_and_depth()

        if self._stop_event.is_set():
            return
        self.running = True
        if self._stop_event.is_set():
            self.running = False
            return
        self._call_callback(self.on_ready, "ready")
        self._log(
            "info",
            f"[Live2D] Renderer started: {self.width}x{self.height}, scale={self.scale:.2f}",
        )
        clock = pygame.time.Clock()
        previous = time.monotonic()
        while self.running and not self._stop_event.is_set():
            self._process_events()
            self._move_window_if_dragging()
            self._audio.pump_voice_events()
            self._audio.pump_pcm_stream()
            self._process_commands()
            self._audio.advance_wav_queue()
            if not self.running or self._stop_event.is_set():
                break

            now = time.monotonic()
            delta = max(0.0, now - previous)
            previous = now
            self._apply_transform()
            self._draw_frame(now, delta)
            self._present_frame()
            clock.tick(60)

    def _present_frame(self) -> None:
        pygame.display.flip()
        self._frames_rendered += 1
        self._after_frame_presented()

    def _after_frame_presented(self) -> None:
        if not self._first_frame_window_setup_done:
            self._force_transparent_window_on_first_frame()

    def _force_transparent_window_on_first_frame(self) -> None:
        if self._first_frame_window_setup_done:
            return
        self._first_frame_window_setup_done = True
        if not self.transparent:
            return
        try:
            # Reassert Win32 color-key and topmost styles after startup events
            # and the first swap, without activating the window.
            if self.hwnd:
                configure_renderer_window(
                    self.hwnd,
                    self.width,
                    self.height,
                    transparent=True,
                )
        except Exception as exc:
            self._log("warning", f"[Live2D] First-frame window setup failed: {exc}")

    def _initialize_window(self) -> None:
        pygame.init()
        self._pygame_initialized = True
        try:
            pygame.display.gl_set_attribute(
                pygame.GL_MULTISAMPLEBUFFERS, 1 if self.antialiasing else 0
            )
            pygame.display.gl_set_attribute(
                pygame.GL_MULTISAMPLESAMPLES, 4 if self.antialiasing else 0
            )
        except pygame.error as exc:
            self._log("warning", f"[Live2D] Could not set MSAA attributes: {exc}")

        flags = pygame.DOUBLEBUF | pygame.OPENGL
        if self.transparent:
            flags |= pygame.NOFRAME
            flags |= pygame.RESIZABLE
        if self.hidden:
            flags |= getattr(pygame, "HIDDEN", 0)
        self.display = pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption("NachoBot Live2D Renderer")
        try:
            window_info = pygame.display.get_wm_info()
            native_hwnd = window_info.get("window")
            self.hwnd = int(native_hwnd) if native_hwnd else None
        except (pygame.error, TypeError, ValueError):
            self.hwnd = None
        if self.transparent and self.hwnd:
            try:
                configure_renderer_window(
                    self.hwnd, self.width, self.height, transparent=True
                )
            except Exception as exc:
                self._log("error", f"[Live2D] Transparent window setup failed: {exc}")
                raise
        self._audio.initialize()

    def _process_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.request_stop()
                return
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                self.request_stop()
                return
            if event.type == getattr(pygame, "WINDOWFOCUSLOST", -1):
                self.dragging_model = False
                self.dragging_window = False
                self.btn_6_down = False
                self.btn_7_down = False
                continue

            if event.type in (
                pygame.MOUSEBUTTONDOWN,
                pygame.MOUSEBUTTONUP,
                pygame.MOUSEMOTION,
                pygame.MOUSEWHEEL,
            ):
                self.last_interaction_time = time.monotonic()

            if event.type == pygame.MOUSEBUTTONDOWN:
                button = int(getattr(event, "button", 0))
                position = getattr(event, "pos", None)
                if position is None:
                    position = pygame.mouse.get_pos()
                if button == 1:
                    self.dragging_model = True
                    self.last_mouse_pos = tuple(position)
                elif button == 2:
                    # The former renderer used left-drag; middle-drag remains a
                    # useful equivalent for touch-pad users.
                    self.dragging_model = True
                    self.last_mouse_pos = tuple(position)
                elif button == 3:
                    self.dragging_window = True
                    cursor = get_cursor_position()
                    self.last_global_mouse_pos = cursor or tuple(position)
                elif button in (6, 7):
                    if button == 6:
                        self.btn_6_down = True
                    else:
                        self.btn_7_down = True
                    self._call_click(button)
            elif event.type == pygame.MOUSEBUTTONUP:
                button = int(getattr(event, "button", 0))
                if button in (1, 2):
                    self.dragging_model = False
                elif button == 3:
                    self.dragging_window = False
                elif button == 6:
                    self.btn_6_down = False
                elif button == 7:
                    self.btn_7_down = False
            elif event.type == pygame.MOUSEWHEEL:
                self.scale = max(0.1, self.scale + float(event.y) * 0.1)
            elif event.type == pygame.MOUSEMOTION and self.dragging_model:
                position = getattr(event, "pos", None)
                if position is None:
                    position = pygame.mouse.get_pos()
                dx = int(position[0]) - self.last_mouse_pos[0]
                dy = int(position[1]) - self.last_mouse_pos[1]
                self.last_mouse_pos = tuple(position)
                sensitivity = 4.0 / max(1, self.height)
                self.offset_x += dx * sensitivity
                self.offset_y -= dy * sensitivity

    def _move_window_if_dragging(self) -> None:
        if not self.dragging_window or not self.hwnd:
            return
        try:
            cursor = get_cursor_position()
            if cursor is None:
                return
            dx = cursor[0] - self.last_global_mouse_pos[0]
            dy = cursor[1] - self.last_global_mouse_pos[1]
            if dx or dy:
                info = self._window_rect()
                if info is not None:
                    move_renderer_window(
                        self.hwnd,
                        info[0] + dx,
                        info[1] + dy,
                    )
                self.last_global_mouse_pos = cursor
        except Exception as exc:
            self._log("error", f"[Live2D] Window drag failed: {exc}")

    def _window_rect(self) -> tuple[int, int] | None:
        if not self.hwnd:
            return None
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            rect = wintypes.RECT()
            get_rect = user32.GetWindowRect
            get_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            get_rect.restype = wintypes.BOOL
            if get_rect(wintypes.HWND(self.hwnd), ctypes.byref(rect)):
                return int(rect.left), int(rect.top)
        except (AttributeError, OSError):
            return None
        return None

    def _process_commands(self) -> None:
        for _ in range(256):
            try:
                item = self.command_queue.get_nowait()
            except queue.Empty:
                break
            try:
                kind, payload = item
                self._handle_command(str(kind), payload)
            except Exception as exc:
                self._log("error", f"[Live2D] Renderer command failed: {exc}")

    def _handle_command(self, kind: str, payload: Any) -> None:
        command = str(kind or "").strip().casefold()
        if command in {"shutdown", "quit", "exit"}:
            self.request_stop()
            return
        if self._audio.command(command, payload):
            return
        runtime = self.model_runtime
        if runtime is None:
            return
        if command == "state":
            target = _STATE_GAZE.get(str(payload or ""))
            if target is not None:
                runtime.command("auto_gaze", {"x": target[0], "y": target[1]})
            return
        if command == "gaze" and isinstance(payload, (tuple, list)):
            runtime.command("gaze", payload)
            return
        if command in {
            "gaze",
            "auto_gaze",
            "motion",
            "body_action",
            "random_motion",
            "canonical_action",
            "emotion",
            "param_tween",
            "tween",
            "desktop_motion",
        }:
            runtime.command(command, payload)

    def _apply_transform(self) -> None:
        runtime = self.model_runtime
        if runtime is not None:
            runtime.set_transform(self.scale, self.offset_x, self.offset_y)

    def _draw_frame(self, now: float, delta: float) -> None:
        from OpenGL.GL import (
            GL_COLOR_BUFFER_BIT,
            GL_DEPTH_BUFFER_BIT,
            glClear,
            glClearColor,
        )

        if self.transparent:
            # The Win32 color key is black, so clear RGB must stay exactly zero.
            glClearColor(0.0, 0.0, 0.0, 0.0)
        else:
            glClearColor(0.5, 0.5, 0.5, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        gaze_x: float | None = None
        gaze_y: float | None = None
        should_track = self.track_mouse or self.btn_6_down or self.btn_7_down
        if should_track and not self.dragging_model:
            mouse_x, mouse_y = pygame.mouse.get_pos()
            gaze_x, gaze_y = self._gaze_coordinates(mouse_x, mouse_y)
        interaction_recent = now - self.last_interaction_time < 1.0
        frame = RuntimeFrame(
            now_seconds=now,
            delta=delta,
            gaze_x=gaze_x,
            gaze_y=gaze_y,
            interaction_recent=interaction_recent,
            dragging=self.dragging_model,
            speaking=self.is_speaking,
        )
        self.model_runtime.update_draw(frame)

    def _gaze_coordinates(self, x: int, y: int) -> tuple[float, float]:
        half_height = max(1.0, self.height / 2.0)
        center_x = self.width / 2.0 + self.offset_x * half_height
        center_y = self.height / 2.0 + self.offset_y * half_height
        scale = self.scale
        gaze_x = ((float(x) - center_x) / scale) / max(1.0, self.width / 2.0)
        gaze_y = -((float(y) - center_y) / scale) / half_height
        return gaze_x, gaze_y

    def _disable_culling_and_depth(self) -> None:
        try:
            from OpenGL.GL import GL_CULL_FACE, GL_DEPTH_TEST, glDisable

            glDisable(GL_CULL_FACE)
            glDisable(GL_DEPTH_TEST)
        except ImportError:
            self._log("warning", "[Live2D] PyOpenGL is unavailable")
        except Exception as exc:
            self._log("warning", f"[Live2D] OpenGL setup warning: {exc}")

    def _bind_adapter_metadata(self) -> None:
        adapter = self.model_adapter
        info = self.model_info
        bind_runtime = getattr(adapter, "bind_runtime", None)
        if not callable(bind_runtime) or info is None:
            return
        try:
            bind_runtime(
                parameter_ids=getattr(info, "parameter_ids", ()),
                expression_ids=getattr(info, "expression_ids", ()),
                motion_groups=getattr(info, "motion_groups", ()),
            )
        except Exception as exc:
            self._log("warning", f"[Live2D] Could not bind static adapter metadata: {exc}")

    def _shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self.running = False
        self._stop_event.set()
        try:
            self._audio.shutdown()
        except Exception as exc:
            self._log("warning", f"[Live2D] Audio cleanup failed: {exc}")
        runtime = self.model_runtime
        self.model_runtime = None
        if runtime is not None:
            try:
                # Keep the host GL context current through SDK/model disposal.
                runtime.dispose()
            except Exception as exc:
                self._log("warning", f"[Live2D] Model runtime cleanup failed: {exc}")
        if self._pygame_initialized:
            try:
                pygame.quit()
            except Exception as exc:
                self._log("warning", f"[Live2D] pygame cleanup failed: {exc}")
            self._pygame_initialized = False
        self._call_callback(self.on_exit, "exit")

    def _call_click(self, button: int) -> None:
        self._call_callback(self.on_click, "click", button)

    def _call_callback(self, callback: Callable[..., Any] | None, name: str, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            self._log("error", f"[Live2D] {name} callback failed: {exc}")

    def _log(self, level: str, message: str) -> None:
        method = getattr(self.logger, level, None)
        if callable(method):
            method(message)

    @staticmethod
    def _positive_scale(value: Any) -> float:
        try:
            scale = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("renderer scale must be a finite positive number") from None
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("renderer scale must be a finite positive number")
        return scale
