import io
import math
import os
import queue
import threading
from array import array
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pygame

from .chat import DesktopPetChat, _suppress_thread_window_activation
from .config import DesktopPetConfig
from .desktop_pet import (
    DesktopPetState,
    DesktopPetStateStore,
    clamp_window_position,
    initial_window_position,
)
from desktop_pet_adapter.desktop_controller import (
    DesktopPetController,
    DesktopPetControllerHost,
    DesktopPetFrame,
)
from desktop_pet_adapter.desktop_movement import MovementGeometry
from desktop_pet_adapter.windows_dpi import per_monitor_dpi_context
from .parameter_panel import ParameterPanel


PET_CLICK_DRAG_THRESHOLD = 14
PET_DOUBLE_CLICK_SECONDS = 0.45


MAX_PCM_PENDING_BYTES = 512 * 1024
MAX_PCM_STREAM_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PCM_CHUNK_BYTES = 64 * 1024


class _PCM16Resampler:
    """Incremental PCM16 sample-rate and mono/stereo converter."""

    def __init__(
        self,
        source_rate: int,
        source_channels: int,
        output_rate: int,
        output_channels: int,
    ) -> None:
        self.source_rate = source_rate
        self.source_channels = source_channels
        self.output_rate = output_rate
        self.output_channels = output_channels
        self.step = source_rate / float(output_rate)
        self.position = 0.0
        self.pending: list[tuple[int, ...]] = []
        self.input_frames = 0
        self.output_frames = 0

    def convert(self, pcm: bytes) -> bytes:
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        frames = [
            self._map_channels(
                tuple(samples[offset : offset + self.source_channels])
            )
            for offset in range(0, len(samples), self.source_channels)
        ]
        self.input_frames += len(frames)
        combined = self.pending + frames
        output_frames: list[tuple[int, ...]] = []
        while self.position < len(combined) - 1:
            index = int(self.position)
            fraction = self.position - index
            left = combined[index]
            right = combined[index + 1]
            output_frames.append(
                tuple(
                    max(-32768, min(32767, round(a + (b - a) * fraction)))
                    for a, b in zip(left, right, strict=True)
                )
            )
            self.position += self.step
        self.output_frames += len(output_frames)
        consumed = min(int(self.position), max(0, len(combined) - 1))
        self.pending = combined[consumed:]
        self.position -= consumed
        return self._encode(output_frames)

    def estimate_output_bytes(self, input_bytes: int) -> int:
        input_frames = input_bytes // (self.source_channels * 2)
        combined_frames = len(self.pending) + input_frames
        positions_before_last_frame = combined_frames - 1 - self.position
        output_frames = (
            max(0, math.ceil(positions_before_last_frame / self.step))
            if positions_before_last_frame > 0
            else 0
        )
        return output_frames * self.output_channels * 2

    def finish(self) -> bytes:
        target_frames = round(self.input_frames * self.output_rate / self.source_rate)
        remaining = max(0, target_frames - self.output_frames)
        output_frames: list[tuple[int, ...]] = []
        for _ in range(remaining):
            if not self.pending:
                break
            index = min(int(self.position), len(self.pending) - 1)
            fraction = self.position - int(self.position)
            left = self.pending[index]
            right = self.pending[min(index + 1, len(self.pending) - 1)]
            output_frames.append(
                tuple(
                    max(-32768, min(32767, round(a + (b - a) * fraction)))
                    for a, b in zip(left, right, strict=True)
                )
            )
            self.position += self.step
        self._reset()
        return self._encode(output_frames)

    def _map_channels(self, frame: tuple[int, ...]) -> tuple[int, ...]:
        if self.source_channels == self.output_channels:
            return frame
        if self.source_channels == 1:
            return (frame[0], frame[0])
        return ((frame[0] + frame[1]) // 2,)

    def _reset(self) -> None:
        self.position = 0.0
        self.pending = []
        self.input_frames = 0
        self.output_frames = 0

    def _encode(self, frames: list[tuple[int, ...]]) -> bytes:
        samples = array("h", (sample for frame in frames for sample in frame))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()


class Live2DHost:
    def __init__(
        self,
        model_path: str,
        logger,
        command_queue: queue.Queue,
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
        desktop_pet_config: DesktopPetConfig | None = None,
        runtime_factory: Callable[[], Any] | None = None,
        runtime_options: Any = None,
        runtime_frame_factory: Callable[..., Any] | None = None,
    ):
        self.model_path = model_path
        self.logger = logger
        self.command_queue = command_queue
        self.transparent = transparent
        self.antialiasing = antialiasing
        self.width = width
        self.height = height
        self.scale = scale
        self.track_mouse = track_mouse
        self.on_click = on_click
        self.on_ready = on_ready
        self.on_exit = on_exit
        if runtime_factory is None:
            raise ValueError("Live2DHost requires a provider runtime factory")
        if runtime_frame_factory is None:
            raise ValueError("Live2DHost requires a provider frame factory")
        self._runtime_factory = runtime_factory
        self._runtime_options = runtime_options
        self._runtime_frame_factory = runtime_frame_factory
        self.model_runtime: Any = None
        self.model_info: Any = None
        self.desktop_pet = desktop_pet_config or DesktopPetConfig()
        self._state_store = DesktopPetStateStore(
            self.desktop_pet.state_path if self.desktop_pet.remember_position else None,
            logger,
        )
        self._desktop_state = (
            self._state_store.load() if self.desktop_pet.enabled else DesktopPetState()
        )
        self._scale_min, self._scale_max = self._get_scale_bounds()
        self.scale = self._clamp_scale(self.scale)
        if self._desktop_state.scale is not None:
            self.scale = self._clamp_scale(self._desktop_state.scale)
        self.offset_x = self._desktop_state.offset_x
        self.offset_y = self._desktop_state.offset_y
        self._constrain_model_offset()
        self.always_on_top = True if self.desktop_pet.enabled else (
            self._desktop_state.always_on_top
            if self._desktop_state.always_on_top is not None
            else self.desktop_pet.always_on_top
        )
        self.click_through = (
            self._desktop_state.click_through
            if self._desktop_state.click_through is not None
            else self.desktop_pet.click_through
        )
        # Restore the last visibility choice as well as position/scale.  The
        # tray remains available to bring the pet back when it was hidden.
        self.window_visible = self._desktop_state.visible
        self._last_state_save_at = 0.0
        self.hwnd = None
        self._desktop_controller: DesktopPetController | None = None
        self._legacy_interactions: Any = None
        self._legacy_chat: Any = None
        self._legacy_tray: Any = None
        self._left_drag_distance = 0
        self._last_parameter_panel_sync_at = 0.0
        if self.desktop_pet.enabled:
            self._desktop_controller = DesktopPetController(
                self.desktop_pet,
                self.logger,
                DesktopPetControllerHost(
                    enqueue_command=self._enqueue_desktop_command,
                    sample_frame=self._desktop_frame,
                    move_window=self._move_pet_window,
                    present_movement=self._present_movement_intent,
                    start_motion=self._start_random_motion,
                    read_window_flag=self._desktop_window_flag,
                    sync_chat_anchor=self._sync_chat_anchor,
                    open_parameters=self._open_desktop_parameters,
                    set_parameter=self._set_desktop_parameter,
                    reset_parameters=self._reset_desktop_parameters,
                    toggle_visibility=self._toggle_visibility,
                    toggle_click_through=self._toggle_click_through,
                    toggle_topmost=self._toggle_topmost,
                    reset_position=self._reset_desktop_position,
                    quit=self._quit_renderer,
                    report_click=self._report_desktop_click,
                    parameter_panel_factory=self._create_parameter_panel,
                ),
                own_hwnd=self.hwnd,
                own_pid=os.getpid(),
            )
        self.running = False
        self._passive_window = None
        self._audio_channel = None
        self._current_sound = None
        self.voice_stream_queue = voice_stream_queue or queue.Queue(maxsize=64)
        self._pcm_audio_channel = None
        self._pcm_current_sound = None
        self._pcm_queued_sound = None
        self._pcm_chunks: deque[bytes] = deque()
        self._pcm_queued_bytes = 0
        self._pcm_stream_id: str | None = None
        self._pcm_parent_message_id: str | None = None
        self._pcm_sample_rate = 0
        self._pcm_channels = 0
        self._pcm_expected_seq = 0
        self._pcm_total_bytes = 0
        self._pcm_sample_width = 0
        self._pcm_end_pending = False
        self._pcm_resampler: _PCM16Resampler | None = None
        self._pcm_output_rate = 0
        self._pcm_output_channels = 0
        self.pcm_audio_ready = False
        self.audio_ready_event = threading.Event()
        self._queued_sounds = deque()
        self._audio_auto_speaking = False
        self.is_speaking = False

        # Auto Gaze default: look at center/camera
        self.target_x = 0.0
        self.target_y = 0.0

        # Screen config
        self.display: pygame.Surface | None = None

        # Interaction Safety
        self.last_interaction_time = 0.0

    def init_pygame(self):
        try:
            self.logger.info("[Live2D] Initializing PyGame...")
            pygame.init()
            self.logger.info("[Live2D] Creating OpenGL window...")

            # Request OpenGL 3.3 Core Profile
            # COMMENTED OUT: Testing if Core Profile breaks Live2D (since test_live2d.py works without this)
            # pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
            # pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
            # pygame.display.gl_set_attribute(
            #    pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_CORE
            # )

            if self.antialiasing:
                self.logger.info("[Live2D] Anti-Aliasing Enabled (MSAA=4)")
                pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
                pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 4)
            else:
                self.logger.info("[Live2D] Anti-Aliasing Disabled (MSAA=0)")
                pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 0)
                pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 0)

            flags = pygame.DOUBLEBUF | pygame.OPENGL
            if self.transparent:
                self.logger.info("[Live2D] Transparent mode enabled (NOFRAME)")
                flags |= (
                    pygame.NOFRAME | pygame.RESIZABLE
                )  # Add RESIZABLE to try to fix clamping

            if self.desktop_pet.enabled:
                # Create hidden under the thread-local activation guard. SDL otherwise
                # activates its new window before the NOACTIVATE style can be applied.
                with _suppress_thread_window_activation():
                    pygame.display.set_mode((self.width, self.height), flags | pygame.HIDDEN)
                self.hwnd = pygame.display.get_wm_info().get("window")
                from .window_behavior import PassiveWindow
                self._passive_window = PassiveWindow(self.hwnd)
            else:
                pygame.display.set_mode((self.width, self.height), flags)
            try:
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                self._refresh_pcm_audio_readiness()
            except pygame.error as exc:
                self.logger.warning("[Live2D] PCM audio device unavailable: {}", exc)
            finally:
                self.audio_ready_event.set()
            pygame.display.set_caption(
                self.desktop_pet.title if self.desktop_pet.enabled else "NachoBot Live2D Renderer"
            )
            self.hwnd = pygame.display.get_wm_info().get("window")

            if self.transparent:
                try:
                    import win32gui
                    import win32con
                    import win32api

                    self.logger.info(
                        f"[Live2D] Setting Layered Window for HWND: {self.hwnd}"
                    )

                    # Set WS_EX_LAYERED
                    ex_style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
                    win32gui.SetWindowLong(
                        self.hwnd,
                        win32con.GWL_EXSTYLE,
                        ex_style | win32con.WS_EX_LAYERED,
                    )

                    # Set Color Key (Black: 0, 0, 0)
                    key_color = win32api.RGB(0, 0, 0)
                    win32gui.SetLayeredWindowAttributes(
                        self.hwnd, key_color, 0, win32con.LWA_COLORKEY
                    )

                    insert_after = (
                        win32con.HWND_TOPMOST
                        if not self.desktop_pet.enabled or self.always_on_top
                        else win32con.HWND_NOTOPMOST
                    )
                    win32gui.SetWindowPos(
                        self.hwnd,
                        insert_after,
                        0,
                        0,
                        self.width,
                        self.height,
                        win32con.SWP_NOMOVE | win32con.SWP_NOACTIVATE,
                    )
                    self.logger.info(
                        f"[Live2D] Forced Window Size: {self.width}x{self.height}"
                    )
                except Exception as win_err:
                    self.logger.error(
                        f"[Live2D] Failed to set transparent window: {win_err}"
                    )

            if self.desktop_pet.enabled and self.hwnd:
                self._configure_desktop_window()

            self.logger.info("[Live2D] PyGame initialized successfully")
        except Exception as e:
            self.logger.error(f"[Live2D] PyGame init failed: {e}")
            raise

        # The provider initializes the Live2D SDK after this thread owns the
        # current pygame OpenGL context.

    @property
    def _desktop_interactions(self):
        if self._desktop_controller is not None:
            return self._desktop_controller.interactions
        return self._legacy_interactions

    @_desktop_interactions.setter
    def _desktop_interactions(self, value):
        self._legacy_interactions = value
        if self._desktop_controller is not None:
            self._desktop_controller.interactions = value

    @property
    def _chat(self):
        if self._desktop_controller is not None:
            return self._desktop_controller.chat
        return self._legacy_chat

    @_chat.setter
    def _chat(self, value):
        self._legacy_chat = value
        if self._desktop_controller is not None:
            self._desktop_controller.chat = value

    @property
    def _tray(self):
        if self._desktop_controller is not None:
            return self._desktop_controller.tray
        return self._legacy_tray

    @_tray.setter
    def _tray(self, value):
        self._legacy_tray = value
        if self._desktop_controller is not None:
            self._desktop_controller.tray = value

    def _start_desktop_chat_host(self) -> None:
        """Start the hidden hover UI host, including model-controls-only mode."""

        if self._desktop_controller is not None:
            self._desktop_controller.start_chat_host()

    def run(self) -> None:
        """Run the host and release every owned resource on all exit paths."""

        with per_monitor_dpi_context():
            try:
                self._run_renderer()
            finally:
                self._shutdown()

    def _shutdown(self) -> None:
        self.running = False
        try:
            self._save_desktop_state(force=True)
        except Exception as exc:
            self.logger.warning("Could not save desktop pet state during shutdown: {}", exc)
        if self._desktop_controller is not None:
            try:
                self._desktop_controller.shutdown()
            except Exception as exc:
                self.logger.warning("Could not stop desktop pet controller: {}", exc)
        self._dispose_model_runtime()
        if self._passive_window is not None:
            try:
                self._passive_window.close()
            except Exception as exc:
                self.logger.warning("Could not close passive window: {}", exc)
            self._passive_window = None
        try:
            pygame.quit()
        except Exception as exc:
            self.logger.warning("Could not quit pygame cleanly: {}", exc)
        self.logger.info("Live2D desktop host stopped")
        if self.on_exit is not None:
            try:
                self.on_exit()
            except Exception as exc:
                self.logger.warning("Live2D host exit callback failed: {}", exc)

    def _run_renderer(self):
        model_path = Path(self.model_path).expanduser().resolve()
        if not model_path.is_file():
            self.logger.critical("Live2D model not found: {}", model_path)
            return

        try:
            self.model_path = str(model_path)
            self.init_pygame()
            self.model_runtime = self._runtime_factory()
            self.model_info = self.model_runtime.initialize(
                str(model_path),
                options=self._runtime_options,
            )
            self.model_runtime.resize(self.width, self.height)
            self.model_runtime.set_transform(self.scale, self.offset_x, self.offset_y)
            for warning in getattr(self.model_info, "warnings", ()):
                self.logger.warning("[Live2D] model metadata warning: {}", warning)

            try:
                from OpenGL.GL import GL_CULL_FACE, GL_DEPTH_TEST, glDisable

                glDisable(GL_CULL_FACE)
                glDisable(GL_DEPTH_TEST)
            except ImportError:
                self.logger.warning("[Live2D] PyOpenGL is unavailable")
            except Exception as exc:
                self.logger.warning("[Live2D] OpenGL setup warning: {}", exc)
        except Exception:
            import traceback

            self.logger.error(traceback.format_exc())
            raise

        self.running = True
        self.logger.info("Live2D Renderer Started")
        if self.on_ready is not None:
            self.on_ready()

        clock = pygame.time.Clock()
        frame_count = 0

        self.dragging_model = False
        self.last_mouse_pos = (0, 0)

        # Bot Control Targets
        self.target_x = 0.0
        self.target_y = 0.0

        self.dragging_window = False
        self.last_global_mouse_pos = (0, 0)
        self.btn_6_down = False
        self.btn_7_down = False

        if self.desktop_pet.enabled:
            self._start_desktop_chat_host()
            if self._desktop_controller is not None:
                self._desktop_controller.start()

        while self.running:
            # Handle Window Dragging (Manual)
            if self.dragging_window:
                try:
                    import win32api
                    import win32gui
                    import win32con

                    cur_x, cur_y = win32api.GetCursorPos()
                    dx = cur_x - self.last_global_mouse_pos[0]
                    dy = cur_y - self.last_global_mouse_pos[1]

                    if dx != 0 or dy != 0:
                        rect = win32gui.GetWindowRect(self.hwnd)
                        work_area = self._get_work_area(drag_point=(cur_x, cur_y))
                        margin = self.desktop_pet.margin if self.desktop_pet.enabled else 0
                        win_x, win_y = clamp_window_position(
                            rect[0] + dx,
                            rect[1] + dy,
                            self.width,
                            self.height,
                            work_area,
                            margin,
                        )
                        win32gui.SetWindowPos(
                            self.hwnd,
                            0,
                            win_x,
                            win_y,
                            0,
                            0,
                            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
                        )
                        self.last_global_mouse_pos = (cur_x, cur_y)
                        self._left_drag_distance += abs(dx) + abs(dy)
                except Exception as e:
                    self.logger.error(f"Window Drag Error: {e}")

            if frame_count == 0 and self.transparent and self.hwnd:
                # Force Size AGAIN after loop starts
                try:
                    import win32gui
                    import win32con

                    win32gui.SetWindowPos(
                        self.hwnd,
                        (
                            win32con.HWND_TOPMOST
                            if not self.desktop_pet.enabled or self.always_on_top
                            else win32con.HWND_NOTOPMOST
                        ),
                        0,
                        0,
                        self.width,
                        self.height,
                        win32con.SWP_NOMOVE | win32con.SWP_NOACTIVATE,
                    )
                    rect = win32gui.GetWindowRect(self.hwnd)
                    self.logger.info(f"[Live2D] Frame 0 Force Size. Rect: {rect}")
                    surf_size = pygame.display.get_surface().get_size()
                    self.logger.info(f"[Live2D] Surface Size: {surf_size}")
                except Exception as e:
                    self.logger.error(f"[Live2D] Force Size Error: {e}")

            # Process PyGame Events
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                    break

                # Update interaction time for any mouse event
                if event.type in (
                    pygame.MOUSEBUTTONDOWN,
                    pygame.MOUSEBUTTONUP,
                    pygame.MOUSEMOTION,
                    pygame.MOUSEWHEEL,
                    pygame.KEYDOWN,
                    pygame.KEYUP,
                ):
                    self.last_interaction_time = time.monotonic()
                    if self._desktop_controller is not None:
                        self._desktop_controller.note_user_input(
                            pet_dragging=self.dragging_window or self.dragging_model
                        )

                if event.type == pygame.MOUSEBUTTONDOWN:
                    self.logger.info(
                        f"[Live2D] Mouse Down: Button {event.button} at {event.pos}"
                    )
                    # Desktop-pet mode uses left drag for the window. Hold Shift
                    # while dragging to reposition the model inside its window.
                    if event.button == 1:
                        if self.desktop_pet.enabled and not (
                            pygame.key.get_mods() & pygame.KMOD_SHIFT
                        ):
                            self.dragging_window = True
                            self._left_drag_distance = 0
                            if self._desktop_controller is not None:
                                self._desktop_controller.note_user_input(pet_dragging=True)
                            try:
                                import win32api

                                self.last_global_mouse_pos = win32api.GetCursorPos()
                            except Exception:
                                self.dragging_window = False
                        else:
                            self.dragging_model = True
                            self.last_mouse_pos = pygame.mouse.get_pos()

                    # Right Click to Move Window (Button 3)
                    elif event.button == 3:
                        if not self.desktop_pet.enabled:
                            self.logger.info("[Live2D] Right Click: Start Window Drag")
                            self.dragging_window = True
                            try:
                                import win32api

                                self.last_global_mouse_pos = win32api.GetCursorPos()
                            except Exception:
                                self.dragging_window = False

                    elif event.button == 6:
                        # Side Button 1 (Back)
                        self.btn_6_down = True
                        self.logger.info("[Live2D] Button 6 Down: Enable Gaze Tracking")
                        if self.on_click:
                            self.on_click(6)

                        # Debug: Log Model relative coords
                        x, y = pygame.mouse.get_pos()
                        rel_x, rel_y = self._pointer_gaze((x, y))
                        self.logger.info(
                            f"[Live2D] Button 6 Click at Screen({x}, {y}) -> Model({rel_x:.2f}, {rel_y:.2f})"
                        )

                    elif event.button == 7:
                        # Side Button 2 (Forward)
                        self.btn_7_down = True
                        self.logger.info("[Live2D] Button 7 Down: Enable Gaze Tracking")
                        if self.on_click:
                            self.on_click(7)

                        # Debug: Log Model relative coords
                        x, y = pygame.mouse.get_pos()
                        rel_x, rel_y = self._pointer_gaze((x, y))
                        self.logger.info(
                            f"[Live2D] Button 7 Click at Screen({x}, {y}) -> Model({rel_x:.2f}, {rel_y:.2f})"
                        )

                if event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        if self.desktop_pet.enabled and self.dragging_window:
                            self.dragging_window = False
                            if self._left_drag_distance <= PET_CLICK_DRAG_THRESHOLD:
                                self._handle_pet_click(1)
                            else:
                                # A real drag must never be interpreted as the
                                # first half of a later double-click.
                                if self._desktop_controller is not None:
                                    self._desktop_controller.cancel_click_sequence()
                            self._left_drag_distance = 0
                            self._save_desktop_state(force=True)
                        else:
                            self.dragging_model = False
                            self.logger.info(
                                f"[Live2D] Mouse Up: Drag End. Offset: ({self.offset_x:.2f}, {self.offset_y:.2f})"
                            )
                            self._save_desktop_state(force=True)
                    elif event.button == 3:
                        if self.desktop_pet.enabled:
                            self._handle_pet_click(3)
                        else:
                            self.dragging_window = False
                            self.logger.info("[Live2D] Right Click: End Window Drag")
                    elif event.button == 6:
                        self.btn_6_down = False
                        self.logger.info("[Live2D] Button 6 Up: Disable Gaze Tracking")
                    elif event.button == 7:
                        self.btn_7_down = False
                        self.logger.info("[Live2D] Button 7 Up: Disable Gaze Tracking")

                if event.type == pygame.MOUSEWHEEL:
                    zoom_speed = 0.1
                    self.scale = self._clamp_scale(self.scale + event.y * zoom_speed)
                    self._constrain_model_offset()
                    self.logger.info(f"[Live2D] Zoom: {self.scale:.2f}")
                    # Wheel events can arrive in bursts; throttle disk writes
                    # while preserving the final value on shutdown.
                    self._save_desktop_state()

                if event.type == pygame.MOUSEMOTION:
                    if self.dragging_model:
                        x, y = pygame.mouse.get_pos()
                        dx = x - self.last_mouse_pos[0]
                        dy = y - self.last_mouse_pos[1]
                        self.last_mouse_pos = (x, y)

                        # Increase Sensitivity
                        sensitivity = 4.0
                        unit_dx = dx * (sensitivity / self.height)
                        unit_dy = dy * (sensitivity / self.height)

                        self.offset_x += unit_dx
                        self.offset_y -= unit_dy  # Invert Y for OpenGL
                        self._constrain_model_offset()
                        # self.logger.debug(f"Pan: {unit_dx:.4f}, {unit_dy:.4f}")

            # Process SDL releases first; finish an outside release without requiring
            # foreground mouse capture (or treating it as a click on the model).
            self._poll_pet_drag_release()

            while not self.command_queue.empty():
                try:
                    cmd_type, cmd_data = self.command_queue.get_nowait()
                    self._handle_command(cmd_type, cmd_data)
                except queue.Empty:
                    break
                except Exception as e:
                    self.logger.error(f"Command error: {e}")

            if self._desktop_controller is not None:
                self._desktop_controller.tick(
                    max(1 / 240, min(1 / 15, clock.get_time() / 1000.0 or 1 / 60))
                )

            while not self.voice_stream_queue.empty():
                try:
                    self._handle_voice_stream_event(self.voice_stream_queue.get_nowait())
                except queue.Empty:
                    break
            self._pump_pcm_stream()
            self._advance_audio_queue()

            # Render Frame
            # Render Frame
            # Comment out clear to see if model covers screen

            # Force OpenGL State for Live2D
            try:
                from OpenGL.GL import (
                    glDisable,
                    glEnable,
                    glBlendFunc,
                    GL_DEPTH_TEST,
                    GL_CULL_FACE,
                    GL_BLEND,
                    GL_SRC_ALPHA,
                    GL_ONE_MINUS_SRC_ALPHA,
                    glClear,
                    GL_COLOR_BUFFER_BIT,
                    GL_DEPTH_BUFFER_BIT,
                    glClearColor,
                    GL_TEXTURE_2D,
                    GL_ALPHA_TEST,
                )

                # Clear manually
                # Clear manually
                if self.transparent:
                    # Clear to Black for Color Key
                    glClearColor(0.0, 0.0, 0.0, 0.0)
                    # Disable Alpha Test to fix Eyes
                    glDisable(GL_ALPHA_TEST)
                else:
                    # Clear to GRAY
                    glClearColor(0.5, 0.5, 0.5, 1.0)
                    glDisable(GL_ALPHA_TEST)

                glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

                glDisable(GL_DEPTH_TEST)
                glDisable(GL_CULL_FACE)
                glEnable(GL_BLEND)
                glEnable(GL_TEXTURE_2D)
                glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            except:
                pass

            if self.model_runtime is not None:
                try:
                    elapsed = clock.tick(60) / 1000.0
                    delta = max(1 / 240, min(1 / 15, elapsed or 1 / 60))
                    now_seconds = time.monotonic()
                    should_track = self.track_mouse or self.btn_6_down or self.btn_7_down
                    gaze_x: float | None = None
                    gaze_y: float | None = None
                    if should_track and not self.dragging_model:
                        gaze_x, gaze_y = self._pointer_gaze(pygame.mouse.get_pos())
                    frame = self._runtime_frame(
                        now_seconds=now_seconds,
                        delta=delta,
                        gaze_x=gaze_x,
                        gaze_y=gaze_y,
                    )
                    self._render_provider_frame(frame)
                    frame_count += 1
                    if frame_count % 600 == 0:
                        self.logger.debug("[Live2D] Rendered {} frames", frame_count)
                except Exception as exc:
                    self.logger.error("Error in provider update/draw: {}", exc)

            pygame.display.flip()

    def _dispose_model_runtime(self) -> None:
        """Dispose provider resources on the render thread before pygame quits."""

        runtime = self.model_runtime
        self.model_runtime = None
        self.model_info = None
        if runtime is not None:
            try:
                runtime.dispose()
            except Exception as exc:
                self.logger.warning("[Live2D] provider disposal failed: {}", exc)

    def _enqueue_desktop_command(self, command: str, value: Any = None) -> None:
        self.command_queue.put((command, value))

    def _desktop_frame(self) -> DesktopPetFrame | None:
        if not self.hwnd:
            return None
        try:
            import win32gui

            rect = tuple(int(value) for value in win32gui.GetWindowRect(self.hwnd))
            return DesktopPetFrame(
                position=(float(rect[0]), float(rect[1])),
                geometry=MovementGeometry(
                    viewport=(self.width, self.height),
                    foot_anchor=(
                        self.desktop_pet.interactions.foot_anchor_x,
                        self.desktop_pet.interactions.foot_anchor_y,
                    ),
                ),
                work_area=self._get_work_area(),
                visible=self.window_visible and bool(win32gui.IsWindowVisible(self.hwnd)),
                pet_dragging=self.dragging_window or self.dragging_model,
                motion_busy=False,
            )
        except Exception as exc:
            self.logger.debug("Desktop pet frame unavailable: {}", exc)
            return None

    def _move_pet_window(self, x: int, y: int) -> None:
        if not self.hwnd:
            return
        import win32con
        import win32gui

        win32gui.SetWindowPos(
            self.hwnd,
            0,
            x,
            y,
            0,
            0,
            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )

    def _present_movement_intent(self, intent: str) -> bool:
        group = self.desktop_pet.presentation_mappings.get(str(intent).strip())
        return bool(group and self._start_random_motion(group, priority=1))

    def _desktop_window_flag(self, name: str) -> bool:
        if name == "click_through":
            return self.click_through
        if name == "always_on_top":
            return self.always_on_top
        if name == "visible":
            return self.window_visible
        return False

    def _sync_chat_anchor(self, chat: DesktopPetChat, force: bool) -> None:
        self._sync_chat_window_backend(chat)

    def _sync_chat_window_backend(self, chat: DesktopPetChat) -> None:
        if not self.hwnd:
            return
        try:
            import win32gui

            pet_rect = tuple(int(value) for value in win32gui.GetWindowRect(self.hwnd))
            left, top, right, bottom = pet_rect
            width, height = right - left, bottom - top
            center_x = (left + right) / 2 + self.offset_x * height / 2
            center_y = (top + bottom) / 2 + self.offset_y * height / 2
            pet_rect = (
                round(center_x - width * self.scale / 2),
                round(center_y - height * self.scale / 2),
                round(center_x + width * self.scale / 2),
                round(center_y + height * self.scale / 2),
            )
            chat.sync_anchor(
                pet_rect,
                self._get_work_area(),
                visible=self.window_visible,
                topmost=self.always_on_top,
                click_through=self.click_through,
                hovered=self._cursor_over_pet_window(),
                model_scale=self.scale,
            )
            sync_output = getattr(chat, "set_output_speaking", None)
            if callable(sync_output):
                sync_output(bool(self.is_speaking))
        except Exception as exc:
            self.logger.debug("Desktop chat position unavailable: {}", exc)

    def _open_desktop_parameters(self) -> None:
        if self._chat is not None:
            self._chat.open_parameters()

    def _create_parameter_panel(self, *args: Any, **kwargs: Any) -> Any:
        return ParameterPanel(*args, **kwargs)

    def _set_desktop_parameter(self, value: dict[str, Any]) -> None:
        runtime = self.model_runtime
        if runtime is not None:
            runtime.set_parameter(str(value.get("id", "")), value.get("value"))

    def _reset_desktop_parameters(self, group: str | None) -> None:
        runtime = self.model_runtime
        if runtime is not None:
            runtime.reset_parameters(group)

    def _toggle_click_through(self) -> None:
        self._set_click_through(not self.click_through)

    def _toggle_topmost(self) -> None:
        self._set_topmost(not self.always_on_top)

    def _quit_renderer(self) -> None:
        self.running = False

    def _report_desktop_click(self, button: int) -> None:
        if self.on_click is not None:
            self.on_click(button)

    def _get_scale_bounds(self) -> tuple[float, float]:
        if not self.desktop_pet.enabled:
            return 0.1, 10.0
        minimum = float(self.desktop_pet.min_scale)
        maximum = float(self.desktop_pet.max_scale)
        if self.desktop_pet.fit_to_window:
            # SetScale() zooms inside the fixed pygame viewport.  A value above
            # 1.0 enlarges the model beyond that viewport, so the transparent
            # window would show only a clipped fragment.  Users can opt out
            # explicitly when they intentionally want an overflowing canvas.
            maximum = min(maximum, 1.0)
        return minimum, max(minimum, maximum)

    def _clamp_scale(self, requested: float) -> float:
        bounded = max(self._scale_min, min(self._scale_max, float(requested)))
        if abs(bounded - float(requested)) > 1e-6:
            self.logger.info(
                "[DesktopPet] zoom limited to {:.2f} to keep the full character visible",
                bounded,
            )
        return bounded

    def _constrain_model_offset(self) -> None:
        if not self.desktop_pet.enabled or not self.desktop_pet.fit_to_window:
            return
        # At scale 1.0 the model fills the viewport.  At smaller scales allow
        # only the spare normalized margin for Shift-drag repositioning.
        allowance = max(0.0, min(0.45, (1.0 - self.scale) * 0.5))
        self.offset_x = max(-allowance, min(allowance, self.offset_x))
        self.offset_y = max(-allowance, min(allowance, self.offset_y))

    def _desktop_flag(self, name: str) -> bool:
        if self._desktop_interactions is not None:
            interaction_state = self._desktop_interactions.get_state(name)
            if name in {
                "interaction_enabled",
                "window_approach",
                "icon_approach",
                "follow_foreground_window",
                "move_windows",
                "move_icons",
                "gui_inspection",
                "gui_actions",
            } or name.startswith("gui_target:"):
                return interaction_state
        if name == "click_through":
            return self.click_through
        if name == "always_on_top":
            return self.always_on_top
        if name == "visible":
            return self.window_visible
        return False

    def _poll_pet_drag_release(self) -> None:
        if not self.desktop_pet.enabled or not (self.dragging_window or self.dragging_model):
            return
        import win32api
        if not win32api.GetAsyncKeyState(0x01) & 0x8000:
            self.dragging_window = self.dragging_model = False
            self._left_drag_distance = 0
            if self._desktop_controller is not None:
                self._desktop_controller.cancel_click_sequence()
            self._save_desktop_state(force=True)

    def _get_work_area(
        self,
        *,
        drag_point: tuple[int, int] | None = None,
    ) -> tuple[int, int, int, int]:
        import win32api
        import win32con

        if drag_point is None and self.dragging_window:
            drag_point = tuple(int(value) for value in win32api.GetCursorPos())
        if drag_point is not None:
            monitor = win32api.MonitorFromPoint(
                tuple(int(value) for value in drag_point),
                win32con.MONITOR_DEFAULTTONEAREST,
            )
        else:
            monitor = win32api.MonitorFromWindow(
                self.hwnd,
                win32con.MONITOR_DEFAULTTONEAREST,
            )
        info = win32api.GetMonitorInfo(monitor)
        return tuple(int(value) for value in info["Work"])

    def _configure_desktop_window(self) -> None:
        import win32con
        import win32gui

        win32gui.SetWindowText(self.hwnd, self.desktop_pet.title)
        ex_style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        ex_style |= win32con.WS_EX_LAYERED
        if self.desktop_pet.hide_from_taskbar:
            ex_style |= win32con.WS_EX_TOOLWINDOW
            ex_style &= ~win32con.WS_EX_APPWINDOW
        if self.click_through:
            ex_style |= win32con.WS_EX_TRANSPARENT
        else:
            ex_style &= ~win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, ex_style)

        work_area = self._get_work_area()
        if self._desktop_state.x is not None and self._desktop_state.y is not None:
            x, y = self._desktop_state.x, self._desktop_state.y
        else:
            x, y = initial_window_position(
                self.desktop_pet.start_position,
                self.width,
                self.height,
                work_area,
                self.desktop_pet.margin,
            )
        x, y = clamp_window_position(
            x,
            y,
            self.width,
            self.height,
            work_area,
            self.desktop_pet.margin,
        )
        insert_after = (
            win32con.HWND_TOPMOST if self.always_on_top else win32con.HWND_NOTOPMOST
        )
        flags = win32con.SWP_FRAMECHANGED | win32con.SWP_NOACTIVATE
        win32gui.ShowWindow(
            self.hwnd,
            win32con.SW_SHOWNOACTIVATE if self.window_visible else win32con.SW_HIDE,
        )
        # SDL's show handler may restore its own z-order. Apply topmost afterwards.
        pygame.event.pump()
        win32gui.SetWindowPos(
            self.hwnd,
            insert_after,
            x,
            y,
            self.width,
            self.height,
            flags,
        )
        self.logger.info(
            f"[DesktopPet] window ready at ({x}, {y}), "
            f"topmost={self.always_on_top}, click_through={self.click_through}"
        )

    def _set_click_through(self, enabled: bool) -> None:
        if not self.hwnd:
            return
        import win32con
        import win32gui

        self.click_through = bool(enabled)
        ex_style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        if self.click_through:
            ex_style |= win32con.WS_EX_TRANSPARENT
        else:
            ex_style &= ~win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, ex_style)
        win32gui.SetWindowPos(
            self.hwnd,
            0,
            0,
            0,
            0,
            0,
            win32con.SWP_FRAMECHANGED
            | win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE,
        )
        self._save_desktop_state(force=True)
        self._sync_chat_window(force=True)
        if self._tray is not None:
            self._tray.refresh()

    def _set_topmost(self, enabled: bool) -> None:
        if not self.hwnd:
            return
        import win32con
        import win32gui

        enabled = True if self.desktop_pet.enabled else bool(enabled)
        self.always_on_top = enabled
        win32gui.SetWindowPos(
            self.hwnd,
            win32con.HWND_TOPMOST if enabled else win32con.HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        self._save_desktop_state(force=True)
        self._sync_chat_window(force=True)
        if self._tray is not None:
            self._tray.refresh()

    def _toggle_visibility(self) -> None:
        if not self.hwnd:
            return
        import win32con
        import win32gui

        self.window_visible = not self.window_visible
        win32gui.ShowWindow(
            self.hwnd,
            win32con.SW_SHOWNA if self.window_visible else win32con.SW_HIDE,
        )
        if self.window_visible:
            self._set_topmost(self.always_on_top)
        else:
            self._save_desktop_state(force=True)
        self._sync_chat_window(force=True)
        if self._tray is not None:
            self._tray.refresh()

    def _reset_desktop_position(self) -> None:
        if not self.hwnd:
            return
        import win32con
        import win32gui

        work_area = self._get_work_area()
        x, y = initial_window_position(
            self.desktop_pet.start_position,
            self.width,
            self.height,
            work_area,
            self.desktop_pet.margin,
        )
        x, y = clamp_window_position(
            x,
            y,
            self.width,
            self.height,
            work_area,
            self.desktop_pet.margin,
        )
        win32gui.SetWindowPos(
            self.hwnd,
            win32con.HWND_TOPMOST if self.always_on_top else win32con.HWND_NOTOPMOST,
            x,
            y,
            self.width,
            self.height,
            win32con.SWP_NOACTIVATE,
        )
        self.scale = self._clamp_scale(self.desktop_pet.min_scale + 0.55)
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._constrain_model_offset()
        self._save_desktop_state(force=True)
        self._sync_chat_window(force=True)

    def _sync_chat_window(self, *, force: bool = False) -> None:
        if self._desktop_controller is not None:
            self._desktop_controller.sync_chat(force=force)

    def _cursor_over_pet_window(self) -> bool:
        """Use native hit testing so transparent pixels and other windows do not trigger chat."""

        if not self.hwnd or not self.window_visible or self.click_through:
            return False
        try:
            import win32api
            import win32con
            import win32gui

            point = tuple(win32api.GetCursorPos())
            hit = int(win32gui.WindowFromPoint(point))
            root_hwnd = int(
                win32gui.GetAncestor(self.hwnd, win32con.GA_ROOT) or self.hwnd
            )
            hit_root = int(win32gui.GetAncestor(hit, win32con.GA_ROOT) or hit)
            return hit_root == root_hwnd or bool(win32gui.IsChild(root_hwnd, hit))
        except Exception as exc:
            self.logger.debug(f"Desktop chat hover hit-test unavailable: {exc}")
            return False

    def _update_desktop_interactions(self, delta_time: float) -> None:
        if self._desktop_controller is not None:
            self._desktop_controller.tick(delta_time)

    def _sync_parameter_panel(self, *, force: bool = False) -> None:
        """Send provider snapshots to the chat UI as copied plain values."""

        runtime = self.model_runtime
        if self._chat is None or runtime is None:
            return
        now = time.monotonic()
        if not force and now - self._last_parameter_panel_sync_at < 0.2:
            return
        self._last_parameter_panel_sync_at = now
        try:
            self._chat.sync_parameter_specs(runtime.parameter_snapshot())
        except Exception as exc:
            self.logger.debug("Desktop parameter panel sync unavailable: {}", exc)

    def _runtime_frame(
        self,
        *,
        now_seconds: float,
        delta: float,
        gaze_x: float | None,
        gaze_y: float | None,
    ) -> Any:
        """Build the provider's copied value-only frame input."""

        return self._runtime_frame_factory(
            now_seconds=now_seconds,
            delta=delta,
            gaze_x=gaze_x,
            gaze_y=gaze_y,
            interaction_recent=time.monotonic() - self.last_interaction_time < 1.0,
            dragging=bool(self.dragging_window or self.dragging_model),
            speaking=bool(self.is_speaking),
        )

    def _update_and_draw_model(self, frame: Any) -> None:
        """Delegate model update, provider-owned parameter work, and drawing."""

        runtime = self.model_runtime
        if runtime is not None:
            runtime.update_draw(frame)
            self._sync_parameter_panel()

    def _render_provider_frame(self, frame: Any) -> None:
        """Apply host transform and preserve the provider's update/draw boundary."""

        runtime = self.model_runtime
        if runtime is None:
            return
        self._constrain_model_offset()
        runtime.set_transform(self.scale, self.offset_x, self.offset_y)
        self._update_and_draw_model(frame)

    def _save_desktop_state(self, *, force: bool = False) -> None:
        if not self.desktop_pet.enabled or not self.desktop_pet.remember_position:
            return
        now = time.monotonic()
        if not force and now - self._last_state_save_at < 0.5:
            return
        x: int | None = None
        y: int | None = None
        if self.hwnd:
            try:
                import win32gui

                left, top, _right, _bottom = win32gui.GetWindowRect(self.hwnd)
                x, y = int(left), int(top)
            except Exception as exc:
                self.logger.debug(f"Desktop pet window position unavailable: {exc}")
        self._state_store.save(
            DesktopPetState(
                x=x,
                y=y,
                scale=self.scale,
                offset_x=self.offset_x,
                offset_y=self.offset_y,
                always_on_top=self.always_on_top,
                click_through=self.click_through,
                visible=self.window_visible,
            )
        )
        self._last_state_save_at = now

    def _schedule_next_idle_motion(self) -> None:
        if self._desktop_controller is not None:
            self._desktop_controller.schedule_next_idle_motion()

    def _start_random_motion(self, requested_group: str, priority: int = 3) -> bool:
        runtime = self.model_runtime
        if runtime is None:
            return False
        started = runtime.command(
            "desktop_motion",
            {"group": str(requested_group), "priority": int(priority)},
        )
        if started:
            self._schedule_next_idle_motion()
        return bool(started)

    def _maybe_play_desktop_idle(self) -> None:
        if self._desktop_controller is not None:
            self._desktop_controller.maybe_play_idle_motion(self._desktop_frame())

    def _handle_pet_click(self, button: int) -> None:
        if self._desktop_controller is not None:
            self._desktop_controller.handle_pet_click(button)

    def _pointer_gaze(self, position: tuple[int, int]) -> tuple[float, float]:
        """Translate the host cursor into normalized model gaze coordinates."""

        x, y = position
        half_width = max(1.0, self.width / 2.0)
        half_height = max(1.0, self.height / 2.0)
        center_x = self.width / 2.0 + self.offset_x * half_height
        center_y = self.height / 2.0 + self.offset_y * half_height
        scale = max(self.scale, 0.01)
        gaze_x = ((x - center_x) / scale) / half_width
        gaze_y = -((y - center_y) / scale) / half_height
        return gaze_x, gaze_y











    def _handle_command(self, cmd_type: str, cmd_data: Any) -> None:
        if (
            self._desktop_controller is not None
            and self._desktop_controller.handle_command(cmd_type, cmd_data)
        ):
            return
        if cmd_type == "voice_stream":
            self._handle_voice_stream_event(cmd_data)
            return
        if cmd_type in {"play_audio", "play_chat_audio"}:
            if self._play_audio(cmd_data):
                self.is_speaking = True
                self._audio_auto_speaking = True
            return
        if cmd_type == "queue_audio":
            self._queue_audio(cmd_data)
            return
        if cmd_type == "stop_audio":
            self._stop_audio()
            return
        if cmd_type == "speaking":
            self.is_speaking = bool(cmd_data)
            return

        runtime = self.model_runtime
        if cmd_type == "state":
            state_targets = {
                "start_viewing": (-0.5, -0.2),
                "start_thinking": (0.3, 0.5),
                "start_replying": (0.0, 0.0),
                "finish_reply": (0.0, 0.0),
            }
            target = state_targets.get(str(cmd_data))
            if target is not None:
                self.target_x, self.target_y = target
                if runtime is not None:
                    runtime.command("auto_gaze", {"x": target[0], "y": target[1]})
            return
        if cmd_type in {"gaze", "auto_gaze"}:
            if isinstance(cmd_data, (tuple, list)) and len(cmd_data) >= 2:
                target = cmd_data[0], cmd_data[1]
            elif isinstance(cmd_data, dict):
                target = cmd_data.get("x", 0.0), cmd_data.get("y", 0.0)
            else:
                return
            try:
                self.target_x, self.target_y = float(target[0]), float(target[1])
            except (TypeError, ValueError, OverflowError):
                return
            if runtime is not None:
                runtime.command(
                    "auto_gaze",
                    {"x": self.target_x, "y": self.target_y},
                )
            return
        if runtime is None:
            return

        kind = {
            "desktop_motion": "desktop_motion",
            "canonical_action": "canonical_action",
            "body_action": "motion",
            "motion": "motion",
            "random_motion": "random_motion",
            "param_tween": "param_tween",
            "emotion": "emotion",
        }.get(cmd_type)
        if kind is not None and not runtime.command(kind, cmd_data):
            self.logger.debug("Provider rejected {} command", cmd_type)



    def _play_audio(self, audio_data: Any) -> bool:
        """Play WAV data inside the renderer process via PyGame's audio device."""
        if not isinstance(audio_data, bytes) or not audio_data:
            self.logger.warning("[Live2D] Ignored empty or invalid audio command")
            return False

        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            self._stop_audio()
            sound = pygame.mixer.Sound(file=io.BytesIO(audio_data))
            channel = sound.play()
            if channel is None:
                self.logger.warning("[Live2D] No PyGame mixer channel is available for TTS")
                return False
            self._current_sound = sound
            self._audio_channel = channel
            self.logger.info(
                "[Live2D] Started TTS audio playback: {} bytes, {:.2f}s",
                len(audio_data),
                sound.get_length(),
            )
            return True
        except pygame.error as exc:
            self.logger.error(f"[Live2D] Failed to play TTS audio: {exc}")
            return False

    def _queue_audio(self, item: Any) -> bool:
        """Append one short WAV block without interrupting the current block."""
        if not isinstance(item, dict):
            self.logger.warning("[Live2D] Ignored invalid queued audio command")
            return False
        audio_data = item.get("audio")
        if not isinstance(audio_data, bytes) or not audio_data:
            self.logger.warning("[Live2D] Ignored empty queued audio block")
            return False

        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            if item.get("reset"):
                self._stop_audio()
                self.logger.info("[Live2D] Starting streamed TTS playback")
            sound = pygame.mixer.Sound(file=io.BytesIO(audio_data))
            if self._audio_channel is not None and self._audio_channel.get_busy():
                self._queued_sounds.append(sound)
            else:
                channel = sound.play()
                if channel is None:
                    self.logger.warning("[Live2D] No PyGame mixer channel is available for streamed TTS")
                    return False
                self._current_sound = sound
                self._audio_channel = channel
            self.is_speaking = True
            self._audio_auto_speaking = True
            return True
        except pygame.error as exc:
            self.logger.error(f"[Live2D] Failed to queue streamed TTS audio: {exc}")
            return False

    def _advance_audio_queue(self) -> None:
        if not self._audio_auto_speaking:
            return
        if self._audio_channel is not None and self._audio_channel.get_busy():
            return
        if self._queued_sounds:
            sound = self._queued_sounds.popleft()
            channel = sound.play()
            if channel is not None:
                self._current_sound = sound
                self._audio_channel = channel
                return
            self.logger.warning("[Live2D] Could not continue streamed TTS playback")
        self._stop_audio()
        self.logger.info("[Live2D] Streamed TTS playback finished")
        self.target_x = 0.0
        self.target_y = 0.0

    def _stop_audio(self) -> None:
        self._abort_pcm_stream()
        channel = self._audio_channel
        if channel is not None:
            try:
                channel.stop()
            except pygame.error:
                pass
        self._audio_channel = None
        self._current_sound = None
        self._queued_sounds.clear()
        if self._audio_auto_speaking:
            self.is_speaking = False
        self._audio_auto_speaking = False


    def _abort_pcm_stream(self) -> None:
        channel = self._pcm_audio_channel
        if channel is not None:
            try:
                channel.stop()
            except Exception:
                pass
        self._clear_pcm_stream()


    def _clear_pcm_stream(self) -> None:
        self._pcm_audio_channel = None
        self._pcm_current_sound = None
        self._pcm_queued_sound = None
        self._pcm_chunks.clear()
        self._pcm_queued_bytes = 0
        self._pcm_stream_id = None
        self._pcm_parent_message_id = None
        self._pcm_sample_rate = 0
        self._pcm_channels = 0
        self._pcm_sample_width = 0
        self._pcm_expected_seq = 0
        self._pcm_total_bytes = 0
        self._pcm_end_pending = False
        self._pcm_resampler = None
        self.is_speaking = False


    def _handle_voice_stream_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            raise ValueError("Live2D PCM event must be an object")
        operation = event.get("event")
        if operation == "start":
            stream_id = event.get("stream_id")
            parent_id = event.get("parent_message_id")
            sample_rate = event.get("sample_rate")
            channels = event.get("channels")
            sample_width = event.get("sample_width")
            if (
                not isinstance(stream_id, str)
                or not stream_id.strip()
                or not isinstance(parent_id, str)
                or not parent_id.strip()
                or isinstance(sample_rate, bool)
                or not isinstance(sample_rate, int)
                or not 8000 <= sample_rate <= 96000
                or isinstance(channels, bool)
                or not isinstance(channels, int)
                or channels not in (1, 2)
                or sample_width != 2
                or event.get("codec") != "pcm_s16le"
            ):
                raise ValueError("Live2D PCM start metadata is invalid")
            if not self.pcm_audio_ready:
                raise RuntimeError("Live2D PCM output was not ready")

            self._stop_audio()
            try:
                self._pcm_audio_channel = pygame.mixer.Channel(0)
            except Exception:
                self.pcm_audio_ready = False
                self._abort_pcm_stream()
                raise RuntimeError(
                    "Live2D PCM playback channel is unavailable"
                ) from None
            self._pcm_stream_id = stream_id.strip()
            self._pcm_parent_message_id = parent_id.strip()
            self._pcm_sample_rate = sample_rate
            self._pcm_channels = channels
            self._pcm_sample_width = sample_width
            self._pcm_resampler = _PCM16Resampler(
                sample_rate,
                channels,
                self._pcm_output_rate,
                self._pcm_output_channels,
            )
            self._pcm_expected_seq = 0
            self._pcm_total_bytes = 0
            self._pcm_end_pending = False
            self.is_speaking = True
            return

        if not self._matches_pcm_stream(event):
            self.logger.warning("[Live2D] Ignored PCM event for an unknown stream")
            return
        if operation == "chunk":
            seq = event.get("seq")
            pcm = event.get("pcm")
            if (
                isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq != self._pcm_expected_seq
                or not isinstance(pcm, bytes)
                or not pcm
                or len(pcm) > MAX_PCM_CHUNK_BYTES
                or len(pcm) % (self._pcm_channels * self._pcm_sample_width)
            ):
                raise ValueError("Live2D PCM chunk is invalid or out of order")
            if (
                self._pcm_total_bytes + len(pcm) > MAX_PCM_STREAM_TOTAL_BYTES
            ):
                raise BufferError("Live2D PCM stream exceeded its total limit")
            assert self._pcm_resampler is not None
            if (
                self._pcm_queued_bytes
                + self._pcm_resampler.estimate_output_bytes(len(pcm))
                > MAX_PCM_PENDING_BYTES
            ):
                raise BufferError("Live2D PCM playback queue is full")
            output_pcm = self._pcm_resampler.convert(pcm)
            if self._pcm_queued_bytes + len(output_pcm) > MAX_PCM_PENDING_BYTES:
                raise BufferError("Live2D PCM playback queue is full")
            if output_pcm:
                self._pcm_chunks.append(output_pcm)
                self._pcm_queued_bytes += len(output_pcm)
            self._pcm_total_bytes += len(pcm)
            self._pcm_expected_seq += 1
            return

        if operation == "end":
            if self._pcm_resampler is not None:
                tail = self._pcm_resampler.finish()
                self._pcm_resampler = None
                if self._pcm_queued_bytes + len(tail) > MAX_PCM_PENDING_BYTES:
                    raise BufferError("Live2D PCM playback queue is full")
                if tail:
                    self._pcm_chunks.append(tail)
                    self._pcm_queued_bytes += len(tail)
            self._pcm_end_pending = True
            return
        if operation == "abort":
            self._abort_pcm_stream()
            return
        raise ValueError("Live2D PCM event is unsupported")


    def _matches_pcm_stream(self, event: dict[str, Any]) -> bool:
        return bool(
            self._pcm_stream_id is not None
            and event.get("stream_id") == self._pcm_stream_id
            and event.get("parent_message_id") == self._pcm_parent_message_id
            and event.get("sample_rate") == self._pcm_sample_rate
            and event.get("channels") == self._pcm_channels
            and event.get("sample_width") == self._pcm_sample_width
            and event.get("codec") == "pcm_s16le"
        )


    def _pump_pcm_stream(self) -> None:
        channel = self._pcm_audio_channel
        if channel is None or self._pcm_stream_id is None:
            return

        try:
            if channel.get_busy():
                current = channel.get_sound()
                if current is self._pcm_queued_sound:
                    self._pcm_current_sound = self._pcm_queued_sound
                    self._pcm_queued_sound = None
                if self._pcm_queued_sound is None and self._pcm_chunks:
                    pcm = self._pcm_chunks.popleft()
                    self._pcm_queued_bytes -= len(pcm)
                    queued_sound = pygame.mixer.Sound(buffer=pcm)
                    channel.queue(queued_sound)
                    self._pcm_queued_sound = queued_sound
                return

            # An idle channel means its current and any queued sound drained.
            self._pcm_current_sound = None
            self._pcm_queued_sound = None
            if self._pcm_chunks:
                pcm = self._pcm_chunks.popleft()
                self._pcm_queued_bytes -= len(pcm)
                sound = pygame.mixer.Sound(buffer=pcm)
                channel.play(sound)
                self._pcm_current_sound = sound
                return
            if self._pcm_end_pending:
                self._clear_pcm_stream()
        except Exception as exc:
            self.logger.error("[Live2D] PCM playback failed: {}", type(exc).__name__)
            self._abort_pcm_stream()


    def _refresh_pcm_audio_readiness(self) -> bool:
        mixer_format = pygame.mixer.get_init()
        if not mixer_format:
            self.pcm_audio_ready = False
            self._pcm_output_rate = 0
            self._pcm_output_channels = 0
            return False
        sample_rate, sample_format, channels = mixer_format
        self._pcm_output_rate = int(sample_rate)
        self._pcm_output_channels = int(channels)
        self.pcm_audio_ready = bool(
            self._pcm_output_rate >= 8000
            and sample_format == -16
            and self._pcm_output_channels in (1, 2)
        )
        return self.pcm_audio_ready
