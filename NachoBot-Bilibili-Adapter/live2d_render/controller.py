import threading
import queue
import logging
import asyncio
import time
from typing import Any


# Interface for the adapter to interact with
class Live2DController:
    def __init__(self, adapter, logger: logging.Logger):
        self.adapter = adapter
        self.logger = logger
        self.mood_manager = None
        self.action_manager = None
        self.watching_manager = None
        self.random_motion_manager = None

        # State tracking
        self.current_mode = "idle"  # idle, busy, listening, etc.

        # Will be initialized by the managers
        self.is_running = False

        # Renderer components
        self.renderer = None
        self.render_thread = None
        self.command_queue = queue.Queue()
        self._last_poke_time = 0.0

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    def initialize_managers(self):
        from .watching_manager import WatchingManager
        from .random_motion_manager import RandomMotionManager

        if self.adapter.config.live_live2d_mood_enable:
            from .mood_manager import MoodManager

            self.mood_manager = MoodManager(self)
        else:
            self.logger.info("Live2D mood detection disabled by config")

        if self.adapter.config.live_live2d_action_enable:
            from .action_manager import ActionManager

            self.action_manager = ActionManager(self)
        else:
            self.logger.info("Live2D action detection disabled by config")

        self.watching_manager = WatchingManager(self)
        self.random_motion_manager = RandomMotionManager(self)

    def _on_renderer_click(self, button: int):
        """Callback from renderer thread when model is clicked."""
        if button not in (6, 7):
            return

        now = time.time()
        if now - self._last_poke_time < 10.0:
            self.logger.info(
                f"Poke cooldown active. Time remaining: {10.0 - (now - self._last_poke_time):.1f}s"
            )
            return

        self._last_poke_time = now

        # Helper to run async task from thread
        if self.loop and self.adapter:
            config = self.adapter.config
            room_id = config.live_host_room_id

            # 修复：从配置文件动态读取 Master 信息，避免硬编码，并提供默认后备值
            user_id = getattr(config, "live_master_user_id", "1")
            user_name = getattr(config, "live_master_user_name", "主人")

            if not room_id:
                self.logger.warning("Cannot poke: Host Room ID not configured.")
                return

            import asyncio

            asyncio.run_coroutine_threadsafe(
                self.adapter.handle_incoming_poke(room_id, str(user_id), user_name),
                self.loop,
            )

    async def start(self):
        if self.is_running:
            return

        self.initialize_managers()

        if self.mood_manager:
            await self.mood_manager.start()
        if self.action_manager:
            await self.action_manager.start()

        if self.random_motion_manager:
            await self.random_motion_manager.start()

        # Start Renderer if model path is configured
        model_path = self.adapter.config.live_live2d_model_path
        if model_path:
            try:
                # Ensure user site-packages is in path for live2d-py
                # CRITICAL: Always force it to position 0 for highest priority
                import sys
                import site

                # 修复：使用 site 模块动态获取当前用户的 site-packages 路径
                user_site = site.getusersitepackages()

                # Remove if exists, then insert at position 0
                if user_site in sys.path:
                    sys.path.remove(user_site)
                sys.path.insert(0, user_site)
                self.logger.debug(
                    f"Forced user site-packages to position 0: {user_site}"
                )
                self.logger.debug(f"sys.path[0:3] = {sys.path[0:3]}")

                self.logger.debug("Attempting to import Live2DRenderer...")
                from .renderer import Live2DRenderer

                self.logger.debug("Live2DRenderer imported successfully")

                self.renderer = Live2DRenderer(
                    model_path,
                    self.logger,
                    self.command_queue,
                    transparent=self.adapter.config.live_live2d_transparent,
                    antialiasing=self.adapter.config.live_live2d_antialiasing,
                    width=self.adapter.config.live_live2d_width,
                    height=self.adapter.config.live_live2d_height,
                    scale=self.adapter.config.live_live2d_scale,
                    track_mouse=self.adapter.config.live_live2d_track_mouse,
                    on_click=self._on_renderer_click,
                )
                self.render_thread = threading.Thread(
                    target=self.renderer.run, daemon=True
                )
                self.render_thread.start()
            except Exception as e:
                import traceback

                self.logger.error(f"Failed to start Live2D Renderer: {e}")
                self.logger.error(f"Traceback: {traceback.format_exc()}")
        else:
            self.logger.warning(
                "Live2D model path not configured, renderer execution skipped."
            )

        self.is_running = True
        self.logger.info("Live2D Controller started")

    async def on_message_received(self, message):
        """Hook for message received event"""
        if self.watching_manager:
            await self.watching_manager.on_message_received()

        if self.mood_manager:
            await self.mood_manager.update_mood_by_message(message)

        if self.action_manager:
            await self.action_manager.update_action_by_message(message)

    async def on_reply_start(self):
        if self.watching_manager:
            await self.watching_manager.on_reply_start()

    async def on_start_replying(self):
        if self.watching_manager:
            await self.watching_manager.on_start_replying()
        if self.action_manager:
            await self.action_manager.on_start_replying()

    async def on_reply_finished(self):
        if self.watching_manager:
            await self.watching_manager.on_reply_finished()
        if self.action_manager:
            await self.action_manager.on_reply_finished()

    def set_speaking(self, speaking: bool):
        """Set the speaking state for lip sync animation."""
        if self.renderer:
            try:
                self.command_queue.put_nowait(("speaking", speaking))
            except Exception as e:
                self.logger.error(f"Failed to push speaking command: {e}")

    async def send_live2d_event(self, event_type: str, content: Any):
        """
        Central method to send Live2D events.
        Can be routed to WebSocket or local renderer.
        """
        # Route to local renderer
        if self.renderer:
            try:
                if event_type == "param_tween":
                    # Ensure thread-safety by using the queue
                    self.command_queue.put_nowait(("param_tween", content))
                elif event_type == "random_motion":
                    # Trigger a native Live2D motion group
                    self.command_queue.put_nowait(("random_motion", content))
                elif event_type == "state":
                    # Update Gaze State
                    self.command_queue.put_nowait(("state", content))
                elif event_type == "auto_gaze":
                    # Direct Gaze Control
                    self.command_queue.put_nowait(("auto_gaze", content))
                elif event_type == "body_action":
                    # Body Action (Motion)
                    self.command_queue.put_nowait(("body_action", content))
                elif event_type == "emotion":
                    # Emotion Expression (Live2D)
                    self.command_queue.put_nowait(("emotion", content))
                else:
                    self.logger.warning(f"Unknown Live2D event type: {event_type}")
            except Exception as e:
                self.logger.error(f"Failed to push render command: {e}")

        # Update mode based on state commands
        if event_type == "state":
            if content == "finish_reply":
                self.current_mode = "idle"
            elif content in ["start_thinking", "start_replying", "start_viewing"]:
                self.current_mode = "busy"
