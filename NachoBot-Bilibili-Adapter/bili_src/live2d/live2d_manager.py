"""Live2D response parsing and remote-adapter coordination."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional, Tuple


_ACTION_ADAPTER_DIR = Path(__file__).resolve().parents[3] / "NachoBot-Live2D-Adapter" / "live2d_adapter"
if str(_ACTION_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ACTION_ADAPTER_DIR))

from action_adapter import ActionAdapter  # noqa: E402


class Live2DManager:
    """Translate Bilibili reply metadata into platform-neutral avatar commands."""

    def __init__(
        self,
        config: Any,
        logger,
        adapter_ref: Any = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref
        self.controller = None
        self.action_adapter = ActionAdapter(logger)

        if self.config.live_live2d_enable:
            try:
                from bili_src.live2d.remote_controller import RemoteLive2DController

                self.controller = RemoteLive2DController(adapter_ref, logger)
            except Exception as exc:
                self.logger.error(
                    "Failed to initialize remote Live2D controller: {}",
                    exc,
                )

    async def start(self) -> None:
        if self.controller:
            await self.controller.start()

    async def stop(self) -> None:
        if self.controller:
            await self.controller.stop()

    def extract_json_emotion_from_text(
        self,
        text: str,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Parse reply JSON and return ``(reply, emotion, action)``."""
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        parsed_text = ""
        emotion = None
        action = None

        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
            try:
                json_str = text[start_idx : end_idx + 1]
                data = json.loads(json_str, strict=False)
                if not isinstance(data, dict):
                    return text, None, None

                if data.get("reply"):
                    parsed_text = str(data["reply"])
                emotion_value = data.get("emotion")
                action_value = data.get("action")
                emotion = str(emotion_value) if emotion_value is not None else None
                action = str(action_value) if action_value is not None else None
                return parsed_text if parsed_text else text, emotion, action
            except Exception as exc:
                self.logger.debug("JSON parsing failed; using raw reply: {}", exc)

        return text, None, None

    def execute_extracted_live2d_action(
        self,
        emotion: Optional[str],
        action: Optional[str],
    ) -> None:
        """Dispatch parsed emotion and action metadata without blocking TTS."""
        controller = self.controller
        if not controller:
            return

        decision = self.action_adapter.decide(
            emotion=emotion,
            requested_action=action,
        )
        if decision.emotion and (emotion is not None or action is not None):
            self._schedule(
                controller.send_live2d_event("emotion", decision.emotion),
                f"emotion:{decision.emotion}",
            )

        if not decision.action_id:
            return

        if decision.action_id in {"IDLE", "GENERAL"}:
            return

        self._schedule(
            controller.send_canonical_action(decision.action_id),
            f"action:{action}->{decision.action_id}",
        )

    def _schedule(self, coroutine: Any, description: str) -> None:
        try:
            asyncio.create_task(coroutine)
            self.logger.info("Dispatched Live2D {}", description)
        except Exception as exc:
            if hasattr(coroutine, "close"):
                coroutine.close()
            self.logger.error("Failed to dispatch Live2D {}: {}", description, exc)
