import os
import sys
import logging
import queue
import pygame
from typing import Optional, Any, Callable
import site

# Ensure user site-packages is at position 0 for highest priority
user_site = site.getusersitepackages()
print(f"[renderer.py MODULE] Adding user_site to sys.path[0]: {user_site}")
if user_site in sys.path:
    sys.path.remove(user_site)
sys.path.insert(0, user_site)
print(f"[renderer.py MODULE] sys.path[0:3] = {sys.path[0:3]}")


class Live2DRenderer:
    def __init__(
        self,
        model_path: str,
        logger: logging.Logger,
        command_queue: queue.Queue,
        transparent: bool = False,
        antialiasing: bool = True,
        width: int = 800,
        height: int = 600,
        scale: float = 1.0,
        track_mouse: bool = False,
        on_click: Optional[Callable[[int], None]] = None,
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
        self.running = False
        self.hwnd = None
        self.model = None
        self.live2d = None

        # Lip sync state
        self.is_speaking = False
        self.mouth_phase = 0.0

        # Auto Gaze default: look at center/camera
        self.target_x = 0.0
        self.target_y = 0.0

        # Screen config
        self.display: Optional[pygame.Surface] = None

        # Tweening System
        self.active_tweens = []  # List of active tweens

        # Interaction Safety
        self.last_interaction_time = 0.0

    def init_pygame(self):
        # Import live2d.v3 (DLL is now in the package directory)
        self.logger.info("[Live2D] Importing live2d.v3 module...")
        self.logger.info(f"[Live2D] sys.path[0:3] = {sys.path[0:3]}")
        self.logger.info(f"[Live2D] user_site = '{user_site}'")
        self.logger.info(f"[Live2D] user_site in sys.path: {user_site in sys.path}")

        # CRITICAL FIX: Add DLL directory to PATH for Windows DLL loading
        # The Live2D C++ bindings need to find Live2DCubismCore.dll at runtime
        live2d_dll_dir = os.path.join(user_site, "live2d", "v3")
        if os.path.exists(live2d_dll_dir):
            current_path = os.environ.get("PATH", "")
            if live2d_dll_dir not in current_path:
                os.environ["PATH"] = live2d_dll_dir + os.pathsep + current_path
                self.logger.info(
                    f"[Live2D] Added DLL directory to PATH: {live2d_dll_dir}"
                )

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
            pygame.display.set_caption("NachoBot Live2D Renderer")

            if self.transparent:
                try:
                    import win32gui
                    import win32con
                    import win32api

                    self.hwnd = pygame.display.get_wm_info()["window"]
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

                    # Set TopMost AND Force Size
                    win32gui.SetWindowPos(
                        self.hwnd,
                        win32con.HWND_TOPMOST,
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
            moc3_file = model_filename.replace(".model3.json", ".moc3")
            if not os.path.exists(moc3_file):
                self.logger.error(
                    f"[Live2D] Critical: .moc3 file not found in CWD ({os.getcwd()}): {moc3_file}"
                )
                raise FileNotFoundError(f"Model .moc3 file not found: {moc3_file}")
            else:
                self.logger.info(f"[Live2D] ✓ Found .moc3 file: {moc3_file}")

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

                # Check Drawables and Parameters
                try:
                    # Log all parameter IDs for debugging if available
                    self.available_param_ids = []
                    if hasattr(self.model, "GetParamIds"):
                        try:
                            self.available_param_ids = self.model.GetParamIds()
                            self.logger.info(
                                f"[Live2D] Available Parameters ({len(self.available_param_ids)})"
                            )
                        except Exception:
                            pass

                    # Fallback list if empty
                    if not self.available_param_ids:
                        self.available_param_ids = [
                            "ParamAngleX",
                            "ParamAngleY",
                            "ParamAngleZ",
                            "ParamBodyAngleX",
                            "ParamBodyAngleY",
                            "ParamBodyAngleZ",
                            "ParamEyeLOpen",
                            "ParamEyeROpen",
                            "ParamMouthOpenY",
                        ]

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
            self.model.SetOffset(0, 0)

            self.logger.info("[Live2D] ✓ Model loaded and resized successfully")

            # Try to start Idle motion (Safely)
            try:
                self.logger.info("[Live2D] Starting Idle motion...")
                self.model.StartMotion("Idle", 0, 3)
            except Exception as e:
                self.logger.error(f"[Live2D] Failed to start motion: {e}")

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

        clock = pygame.time.Clock()
        frame_count = 0

        self.offset_x = 0.0
        self.offset_y = 0.0
        self.dragging_model = False
        self.last_mouse_pos = (0, 0)

        # Bot Control Targets
        self.target_x = 0.0
        self.target_y = 0.0

        self.dragging_window = False
        self.last_global_mouse_pos = (0, 0)
        self.btn_6_down = False
        self.btn_7_down = False

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
                except Exception as e:
                    self.logger.error(f"Window Drag Error: {e}")

            if frame_count == 0 and self.transparent and self.hwnd:
                # Force Size AGAIN after loop starts
                try:
                    import win32gui
                    import win32con

                    win32gui.SetWindowPos(
                        self.hwnd,
                        win32con.HWND_TOPMOST,
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

                if event.type == pygame.MOUSEBUTTONDOWN:
                    self.logger.info(
                        f"[Live2D] Mouse Down: Button {event.button} at {event.pos}"
                    )
                    # Left Click to Pan Model (Button 1)
                    if event.button == 1:
                        self.dragging_model = True
                        self.last_mouse_pos = pygame.mouse.get_pos()

                    # Right Click to Move Window (Button 3)
                    elif event.button == 3:
                        self.logger.info("[Live2D] Right Click: Start Window Drag")
                        self.dragging_window = True
                        try:
                            import win32api

                            self.last_global_mouse_pos = win32api.GetCursorPos()
                        except:
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
                        self.dragging_model = False
                        self.logger.info(
                            f"[Live2D] Mouse Up: Drag End. Offset: ({self.offset_x:.2f}, {self.offset_y:.2f})"
                        )
                    elif event.button == 3:
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
                    if self.scale < 0.1:
                        self.scale = 0.1
                    self.logger.info(f"[Live2D] Zoom: {self.scale:.2f}")

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

                        # Only set if parameter exists (avoid crash)
                        # We can try/except or just assume standard params exist.
                        # For MouthOpenY it's standard.
                        self.model.SetParameterValue(
                            "ParamMouthOpenY", mouth_value, 1.0
                        )
                    else:
                        # Smoothly close mouth
                        if self.mouth_phase > 0:
                            self.mouth_phase = 0.0
                            self.model.SetParameterValue("ParamMouthOpenY", 0.0, 1.0)

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

                    self.model.Update()

                    self.model.Draw()
                    frame_count += 1
                    if frame_count % 600 == 0:  # Log every 10 seconds
                        self.logger.debug(f"[Live2D] Rendered {frame_count} frames")
                except Exception as e:
                    self.logger.error(f"Error in Update/Draw: {e}")

            pygame.display.flip()

        # Cleanup
        self.live2d.dispose()
        pygame.quit()
        self.logger.info("Live2D Renderer Stopped")

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

    def _handle_command(self, cmd_type: str, cmd_data: Any):
        if not self.model:
            return

        # self.logger.debug(f"Live2D Command: {cmd_type} -> {cmd_data}")

        if cmd_type == "gaze":
            # Direct gaze control: [x, y]
            if isinstance(cmd_data, (list, tuple)) and len(cmd_data) >= 2:
                self.target_x = float(cmd_data[0])
                self.target_y = float(cmd_data[1])

        elif cmd_type == "param_tween":
            # cmd_data: {"param": str, "value": float, "duration": float}
            if isinstance(cmd_data, dict):
                param_id = cmd_data.get("param")
                target_val = cmd_data.get("value")
                duration = cmd_data.get("duration", 1.0)

                # Get current value (approximate, since we don't track all params perfectly,
                # but Live2D might reset on update. Best effort: assume 0 or track last set)
                # Ideally we GetParameterValue but python bindings might not expose it easily/reliably.
                # Use 0.0 as start if unknown, or maybe we should store current values.
                # For safety, let's assume we start from 0 for now or whatever the idle motion left it at.
                # Actually, reading back is better.
                try:
                    start_val = self.model.GetParameterValue(param_id)
                except:
                    start_val = 0.0

                import pygame

                start_time = pygame.time.get_ticks() / 1000.0

                self.active_tweens.append(
                    {
                        "param": param_id,
                        "start_val": start_val,
                        "end_val": target_val,
                        "start_time": start_time,
                        "duration": duration,
                    }
                )

        if cmd_type == "body_action":
            parts = cmd_data.split("_")
            group = parts[0]
            no = 0
            if len(parts) > 1:
                try:
                    no = int(parts[1])
                except ValueError:
                    pass
            self.model.StartMotion(group, no, 3)

        elif cmd_type == "random_motion":
            # Native Motion System (Safe)
            # data = {"group": "Tap", "priority": 3}
            group = cmd_data.get("group", "Idle")
            priority = cmd_data.get("priority", 3)
            self.logger.info(f"[Live2D] Starting Random Motion: {group} (P={priority})")
            if self.model:
                try:
                    self.model.StartRandomMotion(group, priority)
                except Exception as e:
                    self.logger.error(f"[Live2D] Failed to start motion {group}: {e}")

        elif cmd_type == "motion":
            self.model.StartMotion(cmd_data, 0, 3)

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

        elif cmd_type == "body_action":
            # Body Action Command (Motion Group)
            # e.g., "Tap", "Flick", "Idle"
            group = str(cmd_data)
            self.logger.info(f"[Live2D] Body Action: {group}")
            if self.model:
                try:
                    # Priority 3 (Force play)
                    self.model.StartMotion(group, 0, 3)
                except Exception as e:
                    self.logger.error(
                        f"[Live2D] Failed to start body action {group}: {e}"
                    )

        elif cmd_type == "emotion":
            expr_map = {
                "joy": "f01",  # Standard Smile
                "anger": "angry",  # Custom angry
                "sorrow": "f04",  # Standard Sorrow
                "fear": "f02",  # Standard Surprise/Fear
                "shy": "shy",
                "disgust": "disgust",
                "angry": "angry",
                "normal": "normal",
            }
            # Handle emotion dict: {"joy": 5, "anger": 1, ...}
            if isinstance(cmd_data, dict):
                # Find strongest emotion
                strongest = max(cmd_data, key=cmd_data.get)
                value = cmd_data[strongest]

                # Only set expression if intensity is high enough
                if value >= 3:
                    # Map to standard Live2D expression names (adjust as needed for specific model)
                    if strongest in expr_map:
                        expr_name = expr_map[strongest]
                        self.logger.info(
                            f"[Live2D] Setting Emotion: {strongest} -> {expr_name}"
                        )
                        self.model.SetExpression(expr_name)
                    else:
                        self.model.SetExpression("")  # Default to empty/none
            elif isinstance(cmd_data, str):
                expr_name = expr_map.get(cmd_data, cmd_data)
                self.logger.info(
                    f"[Live2D] Setting Emotion (String): {cmd_data} -> {expr_name}"
                )
                self.model.SetExpression(expr_name)

        elif cmd_type == "speaking":
            self.is_speaking = bool(cmd_data)
            self.logger.info(f"[Live2D] Speaking state: {self.is_speaking}")
