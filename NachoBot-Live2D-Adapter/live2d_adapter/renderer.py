import gc
import io
import os
import queue
import random
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import pygame

from .chat import DOCK_RESERVED_HEIGHT, DesktopPetChat
from .config import DesktopPetConfig
from .desktop_pet import (
    DesktopPetState,
    DesktopPetStateStore,
    clamp_window_position,
    initial_window_position,
)
from .model_adapter import Live2DModelAdapter
from .tray import DesktopPetTray


def _damped_step(
    position: float,
    velocity: float,
    target: float,
    delta_time: float,
    parameter_range: float,
) -> tuple[float, float]:
    """Track a physics target while limiting visible acceleration."""
    delta_time = max(1.0 / 240.0, min(1.0 / 15.0, delta_time))
    parameter_range = max(abs(parameter_range), 0.01)
    max_acceleration = parameter_range * 0.5
    max_velocity = parameter_range * 1.5
    remaining = target - position
    stopping_velocity = (2.0 * max_acceleration * abs(remaining)) ** 0.5
    desired_velocity = min(max_velocity, stopping_velocity)
    if remaining < 0.0:
        desired_velocity = -desired_velocity
    max_velocity_change = max_acceleration * delta_time
    velocity_change = max(
        -max_velocity_change,
        min(max_velocity_change, desired_velocity - velocity),
    )
    velocity += velocity_change
    position += velocity * delta_time
    return position, velocity


class Live2DRenderer:
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
        model_adapter: Live2DModelAdapter | None = None,
        desktop_pet_config: DesktopPetConfig | None = None,
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
        self.model_adapter = model_adapter or Live2DModelAdapter.from_model_path(model_path)
        self.desktop_pet = desktop_pet_config or DesktopPetConfig()
        self._state_store = DesktopPetStateStore(
            self.desktop_pet.state_path if self.desktop_pet.remember_position else None,
            logger,
        )
        self._desktop_state = (
            self._state_store.load() if self.desktop_pet.enabled else DesktopPetState()
        )
        if self._desktop_state.scale is not None:
            self.scale = max(
                self.desktop_pet.min_scale,
                min(self.desktop_pet.max_scale, self._desktop_state.scale),
            )
        self.offset_x = self._desktop_state.offset_x
        self.offset_y = self._desktop_state.offset_y
        self.always_on_top = (
            self._desktop_state.always_on_top
            if self._desktop_state.always_on_top is not None
            else self.desktop_pet.always_on_top
        )
        self.click_through = (
            self._desktop_state.click_through
            if self._desktop_state.click_through is not None
            else self.desktop_pet.click_through
        )
        self.window_visible = True
        self._tray: DesktopPetTray | None = None
        self._chat: DesktopPetChat | None = None
        if self.desktop_pet.enabled and self.desktop_pet.chat.enabled:
            self._chat = DesktopPetChat(
                self.desktop_pet.chat,
                self.desktop_pet.title,
                self._enqueue_desktop_command,
                self.logger,
            )
        self._left_drag_distance = 0
        self._last_pet_click_at = 0.0
        self._last_desktop_interaction_at = time.monotonic()
        self._next_idle_motion_at = 0.0
        self._last_chat_sync_at = 0.0
        self.running = False
        self.hwnd = None
        self.model = None
        self.live2d = None
        self._audio_channel = None
        self._current_sound = None
        self._queued_sounds = deque()
        self._audio_auto_speaking = False
        self.available_param_ids: list[str] = []
        self._parameter_indexes: dict[str, int] = {}
        self._lip_sync_param_ids: tuple[str, ...] = ()
        self._failed_parameter_writes: set[str] = set()
        self._smooth_idle_enabled = False
        self._standby_smoothing_active = False
        self._motion_in_progress = False
        self._motion_is_initial_idle = False
        self._physics_output_param_ids: tuple[str, ...] = ()
        self._physics_parameter_ranges: dict[str, float] = {}
        self._physics_filter_states: dict[str, tuple[float, float]] = {}
        self._physics_filter_updated_at: float | None = None

        # Lip sync state
        self.is_speaking = False
        self.mouth_phase = 0.0

        # Auto Gaze default: look at center/camera
        self.target_x = 0.0
        self.target_y = 0.0

        # Screen config
        self.display: pygame.Surface | None = None

        # Tweening System
        self.active_tweens = []  # List of active tweens

        # Interaction Safety
        self.last_interaction_time = 0.0

    def init_pygame(self):
        # Import live2d.v3 (DLL is now in the package directory)
        self.logger.info("[Live2D] Importing live2d.v3 module...")
        self.logger.info(f"[Live2D] sys.path[0:3] = {sys.path[0:3]}")

        # CRITICAL FIX: Clear any cached failed imports
        if "live2d" in sys.modules:
            self.logger.info("[Live2D] Clearing cached 'live2d' from sys.modules")
            del sys.modules["live2d"]
        if "live2d.v3" in sys.modules:
            self.logger.info("[Live2D] Clearing cached 'live2d.v3' from sys.modules")
            del sys.modules["live2d.v3"]
        if "live2d.v3.live2d" in sys.modules:
            self.logger.info(
                "[Live2D] Clearing cached 'live2d.v3.live2d' from sys.modules"
            )
            del sys.modules["live2d.v3.live2d"]

        try:
            import live2d.v3 as live2d

            self.live2d = live2d
            self.logger.info(f"[Live2D] ✓ Successfully imported: {live2d.__file__}")
        except Exception as e:
            self.logger.error(f"[Live2D] ✗ Import failed: {e}")
            import traceback

            self.logger.error(traceback.format_exc())

            # Debug: Try to manually check what's in the directory
            try:
                import importlib.util

                spec = importlib.util.find_spec("live2d")
                self.logger.error(f"[Live2D] DEBUG: live2d spec = {spec}")
                if spec:
                    spec3 = importlib.util.find_spec("live2d.v3")
                    self.logger.error(f"[Live2D] DEBUG: live2d.v3 spec = {spec3}")
            except Exception as debug_e:
                self.logger.error(f"[Live2D] DEBUG failed: {debug_e}")

            raise

        try:
            # CRITICAL: Enable High DPI Awareness to prevent blurriness
            try:
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(1)
                self.logger.info(
                    "[Live2D] Set Process DPI Awareness to System DPI Aware"
                )
            except Exception:
                try:
                    import ctypes

                    ctypes.windll.user32.SetProcessDPIAware()
                    self.logger.info("[Live2D] Set Process DPI Awareness (Legacy)")
                except Exception:
                    self.logger.warning("[Live2D] Failed to set DPI awareness")

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

            pygame.display.set_mode((self.width, self.height), flags)
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
                        win32con.SWP_NOMOVE,  # Allow sizing, prevent moving
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

        try:
            # Init Live2D
            self.logger.info("[Live2D] Calling live2d.init()...")
            self.live2d.init()
            self.logger.info("[Live2D] live2d.init() completed")

            # CRITICAL FIX: Initialize OpenGL extensions (GLEW)
            self.logger.info("[Live2D] Calling live2d.glInit()...")
            self.live2d.glInit()
            self.logger.info("[Live2D] live2d.glInit() completed")
        except Exception as e:
            self.logger.error(f"[Live2D] live2d.init() failed: {e}")
            import traceback

            self.logger.error(traceback.format_exc())
            raise

    def run(self):
        # Determine model paths early
        abs_path = os.path.abspath(self.model_path)
        model_dir = os.path.dirname(abs_path)
        model_filename = os.path.basename(abs_path)
        original_cwd = os.getcwd()

        if not os.path.exists(abs_path):
            self.logger.critical(f"Live2D Model not found: {abs_path}")
            return

        try:
            # CRITICAL: Change CWD *BEFORE* initializing Live2D
            # Some versions of the SDK/bindings cache the CWD upon initialization
            if os.path.exists(model_dir):
                self.logger.info(
                    f"[Live2D] Changing CWD to model dir BEFORE init: {model_dir}"
                )
                os.chdir(model_dir)
            else:
                self.logger.critical(f"Model directory not found: {model_dir}")
                return

            self.init_pygame()

            self.logger.info(f"Loading Live2D Model: {abs_path}")

            # Now we are in the model directory, we can verify files
            moc3_path = self.model_adapter.metadata.moc_path
            if moc3_path is None or not moc3_path.is_file():
                self.logger.error(
                    f"[Live2D] Critical: referenced .moc3 file not found: {moc3_path}"
                )
                raise FileNotFoundError(f"Model .moc3 file not found: {moc3_path}")
            else:
                self.logger.info(f"[Live2D] ✓ Found .moc3 file: {moc3_path}")

            self.logger.info("[Live2D] Creating LAppModel instance...")
            self.model = self.live2d.LAppModel()
            self.logger.info("[Live2D] LAppModel instance created successfully")
            self.logger.info(f"[Live2D] LAppModel attributes: {dir(self.model)}")

            self.logger.info(f"[Live2D] Loading model JSON: {model_filename}")

            try:
                # Use ./ to force relative path resolution (avoid empty directory issue)
                self.model.LoadModelJson(f"./{model_filename}")
                self.logger.info("[Live2D] ✓ Model JSON loaded successfully")
            except Exception as load_error:
                self.logger.error(f"[Live2D] ✗ LoadModelJson failed: {load_error}")
                # Try to print more info about the exception
                self.logger.error(f"[Live2D] Error details: {dir(load_error)}")
                import ctypes

                self.logger.error(f"[Live2D] Last WinError: {ctypes.get_last_error()}")
                raise

            self.logger.info("[Live2D] Resizing model...")
            self.model.Resize(self.width, self.height)

            # Diagnostic Info
            try:
                self.logger.info(
                    f"[Live2D] Canvas Size (Unit): {self.model.GetCanvasSize()}"
                )
                self.logger.info(
                    f"[Live2D] Canvas Size (Pixel): {self.model.GetCanvasSizePixel()}"
                )
                self.logger.info(
                    f"[Live2D] Pixels Per Unit: {self.model.GetPixelsPerUnit()}"
                )

                # Bind actual runtime identifiers to the non-destructive adapter.
                try:
                    try:
                        runtime_param_ids = list(self.model.GetParamIds() or ())
                    except Exception as exc:
                        runtime_param_ids = []
                        self.logger.warning(
                            f"[Live2D] Failed to enumerate parameters: {exc}"
                        )
                    try:
                        runtime_expression_ids = list(
                            self.model.GetExpressionIds() or ()
                        )
                    except Exception as exc:
                        runtime_expression_ids = []
                        self.logger.warning(
                            f"[Live2D] Failed to enumerate expressions: {exc}"
                        )
                    try:
                        runtime_motion_groups = self.model.GetMotionGroups() or ()
                    except Exception as exc:
                        runtime_motion_groups = []
                        self.logger.warning(
                            f"[Live2D] Failed to enumerate Motion Groups: {exc}"
                        )
                    if isinstance(runtime_motion_groups, dict):
                        runtime_motion_groups = list(runtime_motion_groups)
                    self.model_adapter.bind_runtime(
                        parameter_ids=runtime_param_ids,
                        expression_ids=runtime_expression_ids,
                        motion_groups=runtime_motion_groups,
                    )
                    self.available_param_ids = list(
                        self.model_adapter.available_parameter_ids
                    )
                    self._parameter_indexes = {
                        parameter_id: index
                        for index, parameter_id in enumerate(runtime_param_ids)
                    }
                    self._lip_sync_param_ids = self.model_adapter.resolve_parameter(
                        "MOUTH_OPEN"
                    )
                    self.logger.info(
                        f"[Live2D] Available Parameters ({len(self.available_param_ids)})"
                    )
                    self.logger.info(
                        f"[Live2D] Automatic adaptation: {self.model_adapter.describe()}"
                    )
                except Exception as e:
                    self.logger.warning(f"[Live2D] Failed to get model info: {e}")

                # Try to disable culling/depth if PyOpenGL is available
                try:
                    from OpenGL.GL import glDisable, GL_CULL_FACE, GL_DEPTH_TEST

                    glDisable(GL_CULL_FACE)
                    glDisable(GL_DEPTH_TEST)
                    self.logger.info("[Live2D] Disabled CULL_FACE and DEPTH_TEST")
                except ImportError:
                    self.logger.warning(
                        "[Live2D] PyOpenGL not found, cannot disable culling"
                    )
                except Exception as e:
                    self.logger.warning(f"[Live2D] OpenGL error: {e}")
            except Exception:
                pass

            # Force Scale and Offset
            scale_factor = self.scale
            self.logger.info(f"[Live2D] Scaling Model: {scale_factor}, Offset=(0, 0)")
            self.model.SetScale(scale_factor)
            self.model.SetOffset(self.offset_x, self.offset_y)

            self.logger.info("[Live2D] ✓ Model loaded and resized successfully")

            self._configure_smooth_idle()
            self._start_idle_motion()

        except Exception:
            import traceback

            self.logger.error(traceback.format_exc())
            raise
        finally:
            # CRITICAL: Always restore original working directory
            self.logger.info(f"[Live2D] Restoring CWD to {original_cwd}")
            os.chdir(original_cwd)

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
            self._schedule_next_idle_motion()
            if self._chat is not None:
                self._chat.start()
                self._chat.show()
            if self.desktop_pet.tray_icon:
                self._tray = DesktopPetTray(
                    self.desktop_pet.title,
                    self._enqueue_desktop_command,
                    self._desktop_flag,
                    self.logger,
                )
                self._tray.start()

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
                        win_x = rect[0] + dx
                        win_y = rect[1] + dy
                        win32gui.SetWindowPos(
                            self.hwnd,
                            0,
                            win_x,
                            win_y,
                            0,
                            0,
                            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER,
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
                        win32con.SWP_NOMOVE,
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
                ):
                    self.last_interaction_time = pygame.time.get_ticks() / 1000.0
                    self._last_desktop_interaction_at = time.monotonic()
                    if self.desktop_pet.enabled:
                        self._schedule_next_idle_motion()

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
                        rel_x, rel_y = self._get_model_relative_coords(x, y)
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
                        rel_x, rel_y = self._get_model_relative_coords(x, y)
                        self.logger.info(
                            f"[Live2D] Button 7 Click at Screen({x}, {y}) -> Model({rel_x:.2f}, {rel_y:.2f})"
                        )

                if event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        if self.desktop_pet.enabled and self.dragging_window:
                            self.dragging_window = False
                            if self._left_drag_distance <= 6:
                                self._handle_pet_click(1)
                            self._save_desktop_state()
                        else:
                            self.dragging_model = False
                            self.logger.info(
                                f"[Live2D] Mouse Up: Drag End. Offset: ({self.offset_x:.2f}, {self.offset_y:.2f})"
                            )
                            self._save_desktop_state()
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
                    self.scale += event.y * zoom_speed
                    minimum = self.desktop_pet.min_scale if self.desktop_pet.enabled else 0.1
                    maximum = self.desktop_pet.max_scale if self.desktop_pet.enabled else 10.0
                    self.scale = max(minimum, min(maximum, self.scale))
                    self.logger.info(f"[Live2D] Zoom: {self.scale:.2f}")
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
                        # self.logger.debug(f"Pan: {unit_dx:.4f}, {unit_dy:.4f}")

            # Interactions
            x, y = pygame.mouse.get_pos()
            should_track = self.track_mouse or self.btn_6_down or self.btn_7_down

            if self.model and not self.dragging_model and should_track:
                # CRITICAL FIX: Account for model offset and coordinate system
                # Standard Live2D Unit: Height = 2.0 Units (-1.0 to 1.0)
                # We need to shift the mouse coordinates to be relative to the model's new center.
                # Model Center X (Screen) = CenterX + OffsetX * (Height / 2)
                # Model Center Y (Screen) = CenterY - OffsetY * (Height / 2)  (Y is inverted: Up is Positive Offset)

                cx = self.width / 2.0
                cy = self.height / 2.0

                # Simplified Gaze Logic (User Request):
                # Calculate Model Center on Screen and find relative Mouse Vector
                # Using helper method for consistency
                scaled_dx, scaled_dy = self._get_model_relative_coords(x, y)

                # Look Target (Screen Coords relative to Base Center)
                final_x = cx + scaled_dx
                final_y = cy + scaled_dy

                if frame_count % 60 == 0:
                    self.logger.debug(
                        f"[GazeDebug] Mouse:({x}, {y}) "
                        f"Rel:({scaled_dx:.1f}, {scaled_dy:.1f}) "
                        f"Final:({final_x:.1f}, {final_y:.1f})"
                    )

                self.model.Drag(final_x, final_y)

            # Auto Gaze Control (if not tracking mouse explicitly)
            if self.model and not should_track and not self.dragging_model:
                pass  # Logic continues below
            elif frame_count % 300 == 0:
                self.logger.debug(
                    f"[AutoGaze Skipped] model={bool(self.model)} track_mouse={self.track_mouse} dragging={self.dragging_model}"
                )

            # Process Command Queue (BEFORE Gaze Logic to ensure active_tweens is up-to-date)
            while not self.command_queue.empty():
                try:
                    cmd_type, cmd_data = self.command_queue.get_nowait()
                    self._handle_command(cmd_type, cmd_data)
                except queue.Empty:
                    break
                except Exception as e:
                    self.logger.error(f"Command error: {e}")

            self._maybe_play_desktop_idle()
            self._sync_chat_window()

            self._advance_audio_queue()

            if (
                self.model
                and not self.track_mouse
                and not should_track
                and not self.dragging_model
            ):
                # Auto Gaze: Lerp towards target
                # CRITICAL SAFETY: Skip AutoGaze (Drag) if:
                # 1. Tweens are active (prevents SetParameterValue crash)
                # 2. User is manually dragging model (prevents fighting)
                # 3. User recently interacted (safety cooldown)
                current_time_for_gaze = pygame.time.get_ticks() / 1000.0
                is_safe_gaze = (
                    not self.active_tweens
                    and not self.dragging_model
                    and (current_time_for_gaze - self.last_interaction_time > 1.0)
                )

                if self.model and is_safe_gaze:
                    lerp_speed = 0.01  # Slower speed for smoother transition

                    # Get current drag X/Y is tricky because Live2D model doesn't expose "GetCurrentDrag".
                    # But we can just fake it by continuously dragging towards target.
                    # Ideally we store current_gaze_x/y
                    if not hasattr(self, "current_gaze_x"):
                        self.current_gaze_x = 0.0
                        self.current_gaze_y = 0.0

                    self.current_gaze_x += (
                        self.target_x - self.current_gaze_x
                    ) * lerp_speed
                    self.current_gaze_y += (
                        self.target_y - self.current_gaze_y
                    ) * lerp_speed

                    # Map unit coordinates (-1..1) to screen coordinates for Drag()
                    # Live2D Drag() expects screen coordinates (0..width, 0..height)
                    # (0,0) is top-left.
                    # Center is (width/2, height/2).

                    screen_x = (self.current_gaze_x + 1.0) * 0.5 * self.width
                    # Y is inverted in Live2D Screen mapping usually?
                    # Drag(0,0) -> Top-Left -> Model looks Top-Left.
                    # Live2D Unit Y: Up is Positive.
                    # We want Target Y=0.5 (Up) -> Screen Y < Height/2.
                    screen_y = (1.0 - self.current_gaze_y) * 0.5 * self.height

                    # Debug Auto Gaze (throttle log)
                    if frame_count % 60 == 0:
                        self.logger.debug(
                            f"[AutoGaze] Target: ({self.target_x:.2f}, {self.target_y:.2f}) "
                            f"Current: ({self.current_gaze_x:.2f}, {self.current_gaze_y:.2f}) "
                            f"Screen inputs: ({screen_x:.1f}, {screen_y:.1f})"
                        )

                    self.model.Drag(screen_x, screen_y)
                elif self.model and frame_count % 60 == 0:
                    # Debug Log why skipped
                    self.logger.debug(
                        f"[AutoGaze Skipped] tweens={len(self.active_tweens)} "
                        f"dragging={self.dragging_model} "
                        f"cooldown={current_time_for_gaze - self.last_interaction_time:.1f}s"
                    )

            # Render Frame
            # Render Frame
            # Comment out clear to see if model covers screen
            # self.live2d.clearBuffer(0.5, 0.5, 0.5, 1.0)

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

            if self.model:
                try:
                    clock.tick(60)
                    self.model.SetScale(self.scale)  # Update Scale
                    self.model.SetOffset(self.offset_x, self.offset_y)

                    # Lip Sync: Animate mouth based on speaking state
                    import math

                    if self.is_speaking:
                        # Use sine wave for natural mouth movement
                        self.mouth_phase += 0.12  # Speed of mouth movement
                        # Combine multiple sine waves for more natural look
                        mouth_value = (
                            0.4 * math.sin(self.mouth_phase * 2.5)
                            + 0.3 * math.sin(self.mouth_phase * 1.8)
                            + 0.3 * math.sin(self.mouth_phase * 3.3)
                        )
                        mouth_value = max(0.0, min(1.0, (mouth_value + 1.0) * 0.5))

                        self._set_lip_sync_value(mouth_value)
                    else:
                        # Smoothly close mouth
                        if self.mouth_phase > 0:
                            self.mouth_phase = 0.0
                            self._set_lip_sync_value(0.0)

                    # Process Tweens (BEFORE Update/Physics)
                    current_time = pygame.time.get_ticks() / 1000.0

                    # Safety: Skip tween updates if user interacted recently (within 1.0s)
                    # This prevents race conditions with Drag/Model Update
                    if current_time - self.last_interaction_time < 1.0:
                        # Unsafe to update parameters.
                        # We simply skip processing. Tweens remain in self.active_tweens.
                        pass
                    else:
                        active_tweens_next = []
                        for tween in self.active_tweens:
                            try:
                                # tween structure: {param, start_val, end_val, start_time, duration, easing}
                                elapsed = current_time - tween["start_time"]
                                t = max(0.0, min(1.0, elapsed / tween["duration"]))

                                # Easing (Simple Ease-Out Quad)
                                eased_t = 1.0 - (1.0 - t) * (1.0 - t)

                                current_val = (
                                    tween["start_val"]
                                    + (tween["end_val"] - tween["start_val"]) * eased_t
                                )

                                if self.model:
                                    try:
                                        # Validate Param ID if we have the list
                                        if (
                                            hasattr(self, "available_param_ids")
                                            and tween["param"]
                                            not in self.available_param_ids
                                        ):
                                            # Fail silently/log once
                                            pass
                                        else:
                                            self.model.SetParameterValue(
                                                tween["param"], current_val, 1.0
                                            )
                                    except Exception:
                                        # Fail silently if param doesn't exist
                                        pass

                                if t < 1.0:
                                    active_tweens_next.append(tween)
                            except Exception as e:
                                self.logger.error(f"Error in tween processing: {e}")

                        self.active_tweens = active_tweens_next

                    self._return_to_standby_if_finished()
                    self.model.Update()
                    self._smooth_physics_outputs()

                    self.model.Draw()
                    frame_count += 1
                    if frame_count % 600 == 0:  # Log every 10 seconds
                        self.logger.debug(f"[Live2D] Rendered {frame_count} frames")
                except Exception as e:
                    self.logger.error(f"Error in Update/Draw: {e}")

            pygame.display.flip()

        # Cleanup
        self._save_desktop_state()
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        if self._chat is not None:
            self._chat.stop()
        # live2d-py 0.7 owns native renderer and Cubism framework resources.
        # Release them while the OpenGL context is still alive, and destroy the
        # model before disposing the framework to avoid a Windows access
        # violation during interpreter shutdown.
        model = self.model
        self.model = None
        if model is not None:
            model.DestroyRenderer()
        del model
        gc.collect()
        self.live2d.glRelease()
        self.live2d.dispose()
        pygame.quit()
        self.logger.info("Live2D Renderer Stopped")
        if self.on_exit is not None:
            self.on_exit()

    def _enqueue_desktop_command(self, command: str, value: Any = None) -> None:
        self.command_queue.put((command, value))

    def _desktop_flag(self, name: str) -> bool:
        if name == "click_through":
            return self.click_through
        if name == "always_on_top":
            return self.always_on_top
        if name == "visible":
            return self.window_visible
        return False

    def _get_work_area(self) -> tuple[int, int, int, int]:
        import win32api
        import win32con

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
        if self._chat is not None:
            x = max(
                work_area[0] + self.desktop_pet.margin,
                min(
                    x,
                    work_area[2] - self.width - self.desktop_pet.margin,
                ),
            )
            maximum_y = (
                work_area[3]
                - self.height
                - self.desktop_pet.margin
                - DOCK_RESERVED_HEIGHT
            )
            y = max(work_area[1], min(y, maximum_y))
        insert_after = (
            win32con.HWND_TOPMOST if self.always_on_top else win32con.HWND_NOTOPMOST
        )
        flags = win32con.SWP_FRAMECHANGED | win32con.SWP_NOACTIVATE
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
        self._save_desktop_state()
        self._sync_chat_window(force=True)
        if self._tray is not None:
            self._tray.refresh()

    def _set_topmost(self, enabled: bool) -> None:
        if not self.hwnd:
            return
        import win32con
        import win32gui

        self.always_on_top = bool(enabled)
        win32gui.SetWindowPos(
            self.hwnd,
            win32con.HWND_TOPMOST if enabled else win32con.HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        self._save_desktop_state()
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
        self._sync_chat_window(force=True)
        if self._tray is not None:
            self._tray.refresh()

    def _reset_desktop_position(self) -> None:
        if not self.hwnd:
            return
        import win32con
        import win32gui

        x, y = initial_window_position(
            self.desktop_pet.start_position,
            self.width,
            self.height,
            self._get_work_area(),
            self.desktop_pet.margin,
        )
        if self._chat is not None:
            y = max(
                self._get_work_area()[1],
                y - DOCK_RESERVED_HEIGHT,
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
        self.scale = max(
            self.desktop_pet.min_scale,
            min(self.desktop_pet.max_scale, self.desktop_pet.min_scale + 0.55),
        )
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._save_desktop_state()
        self._sync_chat_window(force=True)

    def _sync_chat_window(self, *, force: bool = False) -> None:
        if self._chat is None or not self.hwnd:
            return
        now = time.monotonic()
        if not force and now - self._last_chat_sync_at < 0.08:
            return
        self._last_chat_sync_at = now
        try:
            import win32gui

            pet_rect = tuple(int(value) for value in win32gui.GetWindowRect(self.hwnd))
            self._chat.sync_anchor(
                pet_rect,
                self._get_work_area(),
                visible=self.window_visible,
                topmost=self.always_on_top,
                click_through=self.click_through,
            )
        except Exception as exc:
            self.logger.debug(f"Desktop chat position unavailable: {exc}")

    def _save_desktop_state(self) -> None:
        if not self.desktop_pet.enabled or not self.desktop_pet.remember_position:
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
                visible=True,
            )
        )

    def _schedule_next_idle_motion(self) -> None:
        minimum = self.desktop_pet.idle_motion_min_seconds
        maximum = self.desktop_pet.idle_motion_max_seconds
        self._next_idle_motion_at = time.monotonic() + random.uniform(minimum, maximum)

    def _start_random_motion(self, requested_group: str, priority: int = 3) -> bool:
        if not self.model:
            return False
        group = self.model_adapter.resolve_motion_group(requested_group)
        if group is None:
            self.logger.warning(f"[Live2D] Motion Group does not exist: {requested_group}")
            return False
        try:
            self._motion_in_progress = True
            self._motion_is_initial_idle = False
            self._standby_smoothing_active = False
            self.model.StartRandomMotion(group, priority)
            self._schedule_next_idle_motion()
            return True
        except Exception as exc:
            self._enter_standby()
            self.logger.error(f"[Live2D] Failed to start motion {group}: {exc}")
            return False

    def _maybe_play_desktop_idle(self) -> None:
        if (
            not self.desktop_pet.enabled
            or not self.window_visible
            or not self.desktop_pet.idle_motion_groups
            or self._motion_in_progress
            or self.dragging_model
            or self.dragging_window
        ):
            return
        now = time.monotonic()
        if now < self._next_idle_motion_at:
            return
        if now - self._last_desktop_interaction_at < self.desktop_pet.idle_motion_min_seconds:
            self._schedule_next_idle_motion()
            return
        group = random.choice(self.desktop_pet.idle_motion_groups)
        self.logger.info(f"[DesktopPet] idle interaction: {group}")
        if not self._start_random_motion(group, priority=1):
            self._schedule_next_idle_motion()

    def _handle_pet_click(self, button: int) -> None:
        now = time.monotonic()
        self._last_desktop_interaction_at = now
        if self.on_click is not None:
            self.on_click(button)
        if button == 1:
            is_double_click = now - self._last_pet_click_at <= 0.4
            motion = (
                self.desktop_pet.double_click_motion
                if is_double_click
                else self.desktop_pet.left_click_motion
            )
            self._last_pet_click_at = now
            if is_double_click and self._chat is not None:
                self._chat.open()
        else:
            motion = self.desktop_pet.right_click_motion
        if motion:
            self._start_random_motion(motion)
        self._schedule_next_idle_motion()

    def _get_model_relative_coords(self, x, y):
        """Helper to get coordinates relative to the model center (considering offset and scale)"""
        cx = self.width / 2.0
        cy = self.height / 2.0
        ppu = self.height / 2.0

        # Model Center (Screen Coords)
        model_center_x = cx + (self.offset_x * ppu)
        model_center_y = cy + (self.offset_y * ppu)

        # Mouse Vector relative to Model Center
        dx = x - model_center_x
        dy = y - model_center_y

        # Scale the vector (Zoom)
        rel_x = dx / self.scale
        rel_y = dy / self.scale

        return rel_x, rel_y

    def _set_lip_sync_value(self, value: float) -> None:
        if not self.model:
            return
        if not self._lip_sync_param_ids:
            self._lip_sync_param_ids = self.model_adapter.resolve_parameter("MOUTH_OPEN")
        if not self._lip_sync_param_ids:
            warning_key = "<unresolved-lip-sync>"
            if warning_key not in self._failed_parameter_writes:
                self._failed_parameter_writes.add(warning_key)
                self.logger.warning(
                    "[Live2D] No lip-sync parameter could be resolved; "
                    "configure [adaptation.parameters].MOUTH_OPEN"
                )
            return
        for parameter_id in self._lip_sync_param_ids:
            try:
                self.model.SetParameterValue(parameter_id, value, 1.0)
            except Exception as exc:
                if parameter_id not in self._failed_parameter_writes:
                    self._failed_parameter_writes.add(parameter_id)
                    self.logger.warning(
                        f"[Live2D] Failed to write parameter {parameter_id}: {exc}"
                    )

    def _configure_smooth_idle(self) -> bool:
        if not self.model:
            return False
        try:
            self.model.SetAutoBreathEnable(True)
        except Exception as exc:
            self.logger.warning(
                f"[Live2D] Failed to restore SDK auto-breath: {exc}"
            )

        self._smooth_idle_enabled = True
        self._configure_physics_smoothing()
        self.logger.info(
            "[Live2D] Idle[0] and SDK auto-breath restored with output smoothing"
        )
        return True

    def _configure_physics_smoothing(self) -> None:
        self._physics_output_param_ids = ()
        self._physics_parameter_ranges.clear()
        self._physics_filter_states.clear()
        self._physics_filter_updated_at = time.perf_counter()

        native_model = getattr(self.model, "_model", None)
        if not callable(getattr(native_model, "SetParameterValueById", None)):
            self.logger.warning(
                "[Live2D] Physics smoothing unavailable; transient parameter writes "
                "are not supported"
            )
            return

        resolved_ids = tuple(
            dict.fromkeys(
                parameter_id
                for canonical in (
                    "ANGLE_X",
                    "ANGLE_Z",
                    "BODY_ANGLE_X",
                    "BODY_ANGLE_Z",
                )
                for parameter_id in self.model_adapter.resolve_parameter(canonical)
                if parameter_id in self._parameter_indexes
            )
        )
        for parameter_id in resolved_ids:
            index = self._parameter_indexes[parameter_id]
            try:
                parameter = self.model.GetParameter(index)
                parameter_range = float(parameter.max) - float(parameter.min)
            except Exception:
                parameter_range = 1.0
            self._physics_parameter_ranges[parameter_id] = max(
                abs(parameter_range), 0.01
            )

        self._physics_output_param_ids = resolved_ids
        if resolved_ids:
            self.logger.info(
                "[Live2D] Smoothing final horizontal and tilt idle outputs"
            )

    def _smooth_physics_outputs(self) -> None:
        if (
            not self._smooth_idle_enabled
            or not self.model
            or not self._physics_output_param_ids
        ):
            return

        if not self._standby_smoothing_active:
            self._physics_filter_states.clear()
            self._physics_filter_updated_at = time.perf_counter()
            return

        now = time.perf_counter()
        previous_time = self._physics_filter_updated_at
        self._physics_filter_updated_at = now
        delta_time = 1.0 / 60.0 if previous_time is None else now - previous_time
        tweened_parameters = {
            str(tween.get("param"))
            for tween in self.active_tweens
            if isinstance(tween, dict)
        }

        for parameter_id in self._physics_output_param_ids:
            raw_value = self._get_parameter_value(parameter_id)
            if parameter_id in tweened_parameters:
                self._physics_filter_states.pop(parameter_id, None)
                continue
            state = self._physics_filter_states.get(parameter_id)
            if state is None:
                self._physics_filter_states[parameter_id] = (raw_value, 0.0)
                continue
            value, velocity = _damped_step(
                state[0],
                state[1],
                raw_value,
                delta_time,
                self._physics_parameter_ranges.get(parameter_id, 1.0),
            )
            self._physics_filter_states[parameter_id] = (value, velocity)
            try:
                self.model._model.SetParameterValueById(parameter_id, value, 1.0)
            except Exception as exc:
                if parameter_id not in self._failed_parameter_writes:
                    self._failed_parameter_writes.add(parameter_id)
                    self.logger.warning(
                        f"[Live2D] Failed to smooth physics parameter "
                        f"{parameter_id}: {exc}"
                    )

    def _get_parameter_value(self, parameter_id: str) -> float:
        if not self.model:
            return 0.0
        index = self._parameter_indexes.get(parameter_id)
        if index is None:
            return 0.0
        try:
            return float(self.model.GetParameterValue(index))
        except Exception:
            return 0.0

    def _resolve_motion_request(self, requested: Any) -> tuple[str | None, int]:
        requested_text = str(requested or "").strip()
        group = self.model_adapter.resolve_motion_group(requested_text)
        if group is not None:
            return group, 0
        base, separator, suffix = requested_text.rpartition("_")
        if separator and suffix.isdigit():
            group = self.model_adapter.resolve_motion_group(base)
            if group is not None:
                return group, int(suffix)
        return None, 0

    def _start_idle_motion(self) -> bool:
        if not self.model:
            return False
        idle_group = self.model_adapter.resolve_motion_group("Idle")
        if not idle_group:
            self.logger.info("[Live2D] No Idle motion group found; skipping")
            return False
        try:
            self.model.StartMotion(idle_group, 0, 3)
        except Exception as exc:
            self._enter_standby()
            self.logger.error(f"[Live2D] Failed to start Idle[0]: {exc}")
            return False
        self._motion_in_progress = True
        self._motion_is_initial_idle = True
        self._standby_smoothing_active = True
        self._physics_filter_states.clear()
        self._physics_filter_updated_at = time.perf_counter()
        self.logger.info(f"[Live2D] Starting one-shot Idle[0]: {idle_group}")
        return True

    def _enter_standby(self, *, preserve_filter: bool = False) -> None:
        self._motion_in_progress = False
        self._motion_is_initial_idle = False
        self._standby_smoothing_active = True
        if not preserve_filter:
            self._physics_filter_states.clear()
            self._physics_filter_updated_at = time.perf_counter()
        self.logger.info("[Live2D] Entering auto-breath standby")

    def _return_to_standby_if_finished(self) -> None:
        if not self.model or not self._motion_in_progress:
            return
        try:
            motion_finished = self.model.IsMotionFinished()
        except Exception as exc:
            self.logger.warning(f"[Live2D] Failed to inspect motion state: {exc}")
            return
        if motion_finished:
            self._enter_standby(preserve_filter=self._motion_is_initial_idle)

    def _start_motion(self, requested: Any, *, priority: int = 3) -> None:
        if not self.model:
            return
        group, index = self._resolve_motion_request(requested)
        if group is None:
            self.logger.warning(f"[Live2D] Motion Group does not exist: {requested}")
            return
        idle_group = self.model_adapter.resolve_motion_group("Idle")
        if group == idle_group:
            try:
                self.model.StopAllMotions()
            except Exception as exc:
                self.logger.error(f"[Live2D] Failed to stop motion for standby: {exc}")
            self._enter_standby()
            return
        try:
            self._motion_in_progress = True
            self._motion_is_initial_idle = False
            self._standby_smoothing_active = False
            self.model.StartMotion(group, index, priority)
        except Exception as exc:
            self._enter_standby()
            self.logger.error(f"[Live2D] Failed to start motion {group}: {exc}")

    def _handle_command(self, cmd_type: str, cmd_data: Any):
        if cmd_type == "desktop_open_chat":
            if self._chat is not None:
                self._chat.open()
            return
        if cmd_type == "desktop_toggle_visibility":
            self._toggle_visibility()
            return
        if cmd_type == "desktop_toggle_click_through":
            self._set_click_through(not self.click_through)
            return
        if cmd_type == "desktop_toggle_topmost":
            self._set_topmost(not self.always_on_top)
            return
        if cmd_type == "desktop_reset_position":
            self._reset_desktop_position()
            return
        if cmd_type == "desktop_quit":
            self.running = False
            return
        if cmd_type == "play_audio":
            self._play_audio(cmd_data)
            return
        if cmd_type == "queue_audio":
            self._queue_audio(cmd_data)
            return
        if cmd_type == "play_chat_audio":
            if self._play_audio(cmd_data):
                self.is_speaking = True
                self._audio_auto_speaking = True
            return
        if cmd_type == "stop_audio":
            self._stop_audio()
            return

        if not self.model:
            return

        if cmd_type == "desktop_motion":
            self._start_random_motion(str(cmd_data or "Tap"))
            return
        if cmd_type == "canonical_action":
            action_id = str(cmd_data or "").strip().upper()
            motion_group = self.model_adapter.resolve_action(action_id)
            if motion_group:
                self._start_motion(motion_group)
            else:
                self.logger.warning(f"[Live2D] Canonical action is unavailable: {action_id}")
            return

        # self.logger.debug(f"Live2D Command: {cmd_type} -> {cmd_data}")

        if cmd_type == "gaze":
            # Direct gaze control: [x, y]
            if isinstance(cmd_data, (list, tuple)) and len(cmd_data) >= 2:
                self.target_x = float(cmd_data[0])
                self.target_y = float(cmd_data[1])

        elif cmd_type == "param_tween":
            if isinstance(cmd_data, dict):
                requested_param = str(cmd_data.get("param") or "").strip()
                target_val = cmd_data.get("value")
                duration = cmd_data.get("duration", 1.0)
                self._queue_parameter_tweens(
                    {requested_param: target_val},
                    duration=float(duration),
                )

        elif cmd_type == "body_action":
            self.logger.info(f"[Live2D] Body Action: {cmd_data}")
            self._start_motion(cmd_data)

        elif cmd_type == "random_motion":
            if isinstance(cmd_data, dict):
                requested_group = cmd_data.get("group", "Idle")
                priority = int(cmd_data.get("priority", 3))
                group = self.model_adapter.resolve_motion_group(requested_group)
                if group is None:
                    self.logger.warning(
                        f"[Live2D] Motion Group does not exist: {requested_group}"
                    )
                    return
                idle_group = self.model_adapter.resolve_motion_group("Idle")
                if group == idle_group:
                    try:
                        self.model.StopAllMotions()
                    except Exception as exc:
                        self.logger.error(
                            f"[Live2D] Failed to stop motion for standby: {exc}"
                        )
                    self._enter_standby()
                    return
                self.logger.info(
                    f"[Live2D] Starting Random Motion: {group} (P={priority})"
                )
                try:
                    self._motion_in_progress = True
                    self._motion_is_initial_idle = False
                    self._standby_smoothing_active = False
                    self.model.StartRandomMotion(group, priority)
                except Exception as e:
                    self._enter_standby()
                    self.logger.error(f"[Live2D] Failed to start motion {group}: {e}")

        elif cmd_type == "motion":
            self._start_motion(cmd_data)

        elif cmd_type == "state":
            # Gaze Control based on state
            self.logger.info(f"State command received: {cmd_data}")
            if cmd_data == "start_viewing":
                # Look at Chat (Bottom Left)
                self.target_x = -0.5
                self.target_y = -0.2
            elif cmd_data == "start_thinking":
                # Look at Thought Bubble (Top Right)
                self.target_x = 0.3
                self.target_y = 0.5
            elif cmd_data == "start_replying":
                # Look straight ahead (or slightly down) while replying/speaking
                self.target_x = 0.0
                self.target_y = 0.0
            elif cmd_data == "finish_reply":
                # Back to Center/Camera
                self.target_x = 0.0
                self.target_y = 0.0

        elif cmd_type == "auto_gaze":
            # Direct Gaze Control (x, y)
            # Coordinates are in Live2D Unit Space (-1..1)
            try:
                self.target_x = float(cmd_data.get("x", 0.0))
                self.target_y = float(cmd_data.get("y", 0.0))
                self.logger.debug(
                    f"[Live2D] Auto Gaze set to: ({self.target_x}, {self.target_y})"
                )
            except (ValueError, TypeError):
                pass

        elif cmd_type == "emotion":
            emotion_name: str | None = None
            if isinstance(cmd_data, dict):
                try:
                    strongest = max(cmd_data, key=lambda key: float(cmd_data[key]))
                    if float(cmd_data[strongest]) >= 3:
                        emotion_name = str(strongest)
                except (TypeError, ValueError):
                    self.logger.warning("[Live2D] Ignored invalid emotion weights")
            elif isinstance(cmd_data, str):
                emotion_name = cmd_data

            if emotion_name:
                expression_id = self.model_adapter.resolve_expression(emotion_name)
                if expression_id:
                    self.logger.info(
                        f"[Live2D] Setting Emotion: {emotion_name} -> {expression_id}"
                    )
                    self.model.SetExpression(expression_id)
                elif emotion_name.casefold() in {"normal", "default", "neutral"}:
                    self.logger.info("[Live2D] Resetting expressions to the model default")
                    if hasattr(self.model, "ResetExpressions"):
                        self.model.ResetExpressions()
                    self._apply_emotion_parameter_pose(emotion_name)
                else:
                    if self._apply_emotion_parameter_pose(emotion_name):
                        self.logger.info(
                            f"[Live2D] Emotion parameter pose: {emotion_name}"
                        )
                    else:
                        self.logger.warning(
                            f"[Live2D] No expression mapping found for: {emotion_name}"
                        )

        elif cmd_type == "speaking":
            self.is_speaking = bool(cmd_data)
            self.logger.info(f"[Live2D] Speaking state: {self.is_speaking}")

    def _queue_parameter_tweens(
        self,
        values: dict[str, Any],
        *,
        duration: float = 0.35,
    ) -> bool:
        start_time = pygame.time.get_ticks() / 1000.0
        queued = False
        for requested_param, target_value in values.items():
            parameter_ids = self.model_adapter.resolve_parameter(requested_param)
            if not parameter_ids:
                self.logger.debug(
                    f"[Live2D] Emotion parameter unavailable: {requested_param}"
                )
                continue
            for parameter_id in parameter_ids:
                self.active_tweens = [
                    tween
                    for tween in self.active_tweens
                    if tween.get("param") != parameter_id
                ]
                self.active_tweens.append(
                    {
                        "param": parameter_id,
                        "start_val": self._get_parameter_value(parameter_id),
                        "end_val": float(target_value),
                        "start_time": start_time,
                        "duration": max(0.05, float(duration)),
                    }
                )
                queued = True
        return queued

    def _apply_emotion_parameter_pose(self, emotion_name: str) -> bool:
        """Provide useful emotion feedback for models without expression files."""

        normalized = emotion_name.strip().casefold()
        aliases = {
            "default": "normal",
            "neutral": "normal",
            "普通": "normal",
            "默认": "normal",
            "happy": "joy",
            "smile": "joy",
            "开心": "joy",
            "高兴": "joy",
            "sad": "sorrow",
            "悲伤": "sorrow",
            "难过": "sorrow",
            "anger": "angry",
            "mad": "angry",
            "生气": "angry",
            "愤怒": "angry",
            "surprise": "fear",
            "surprised": "fear",
            "惊讶": "fear",
            "害羞": "shy",
            "脸红": "shy",
        }
        normalized = aliases.get(normalized, normalized)
        neutral = {
            "ParamCheek": 0.0,
            "MOUTH_OPEN": 0.0,
            "MOUTH_FORM": 0.0,
            "ParamEyeLSmile": 0.0,
            "ParamEyeRSmile": 0.0,
            "BROW_L_Y": 0.0,
            "BROW_R_Y": 0.0,
            "ANGLE_Y": 0.0,
            "ANGLE_Z": 0.0,
        }
        poses: dict[str, dict[str, float]] = {
            "normal": neutral,
            "joy": {
                **neutral,
                "ParamCheek": 0.75,
                "MOUTH_FORM": 0.85,
                "ParamEyeLSmile": 0.8,
                "ParamEyeRSmile": 0.8,
            },
            "shy": {
                **neutral,
                "ParamCheek": 1.0,
                "MOUTH_FORM": 0.25,
                "ANGLE_Z": 6.0,
            },
            "angry": {
                **neutral,
                "MOUTH_FORM": -0.75,
                "BROW_L_Y": -0.65,
                "BROW_R_Y": -0.65,
            },
            "disgust": {
                **neutral,
                "MOUTH_FORM": -0.9,
                "BROW_L_Y": -0.25,
                "BROW_R_Y": 0.2,
            },
            "fear": {
                **neutral,
                "MOUTH_OPEN": 0.55,
                "BROW_L_Y": 0.65,
                "BROW_R_Y": 0.65,
            },
            "sorrow": {
                **neutral,
                "MOUTH_FORM": -0.55,
                "BROW_L_Y": 0.35,
                "BROW_R_Y": 0.35,
                "ANGLE_Y": -5.0,
            },
        }
        pose = poses.get(normalized)
        if pose is None:
            return False
        return self._queue_parameter_tweens(pose)

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
