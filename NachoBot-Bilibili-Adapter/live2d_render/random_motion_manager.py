import asyncio
import random
from typing import Optional


class RandomMotionManager:
    def __init__(self, controller):
        self.controller = controller
        self.logger = controller.logger
        self._task: Optional[asyncio.Task] = None
        self.is_running = False

        # Configurable parameters (could be moved to config.toml later)
        self.enabled = True
        self.interval_min = 2.0
        self.interval_max = 8.0
        self.gaze_range_x = (-0.5, 0.5)
        self.gaze_range_y = (-0.3, 0.3)

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._random_motion_loop())
        self.logger.info("RandomMotionManager started")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.logger.info("RandomMotionManager stopped")

    async def _random_motion_loop(self):
        while self.is_running:
            try:
                # Wait for a random interval
                wait_time = random.uniform(self.interval_min, self.interval_max)
                await asyncio.sleep(wait_time)

                # Only perform random motion if the controller is in "idle" mode
                if (
                    hasattr(self.controller, "current_mode")
                    and self.controller.current_mode == "idle"
                ):
                    # Randomly select a motion type
                    # Weights: Gaze (60%), Tap (20%), Flick (10%), Body (10%)
                    roll = random.random()

                    if roll < 0.60:
                        # Gaze
                        # Subtle gaze shift (avoid extreme angles)
                        x = random.uniform(-0.4, 0.4)
                        y = random.uniform(-0.25, 0.25)
                        await self.controller.send_live2d_event(
                            "auto_gaze", {"x": x, "y": y}
                        )
                        self.logger.debug(
                            f"[RandomMotion] Gaze set to ({x:.2f}, {y:.2f})"
                        )

                    elif roll < 0.80:
                        # Pout (嘟嘴, no mouth opening)
                        await self.controller.send_live2d_event(
                            "random_motion",
                            {"group": "Pout", "priority": 3},
                        )

                    elif roll < 0.90:
                        # Native Motion: Flick
                        await self.controller.send_live2d_event(
                            "random_motion",
                            {"group": "Flick", "priority": 3},
                        )

                    elif roll < 0.95:
                        # SwayBig (hiyori_m02) - Lowered frequency
                        await self.controller.send_live2d_event(
                            "random_motion",
                            {"group": "SwayBig", "priority": 3},
                        )

                    else:
                        # Native Motion: Body Action (Tap@Body)
                        await self.controller.send_live2d_event(
                            "random_motion",
                            {"group": "Tap@Body", "priority": 3},
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in random motion loop: {e}")
                await asyncio.sleep(5.0)
