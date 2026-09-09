"""Live2D response parsing and remote-adapter coordination."""

from __future__ import annotations

import asyncio
import json
from loguru import logger
from typing import Any, Optional, Tuple


class Live2DManager:
    """Translate Bilibili reply metadata into platform-neutral avatar commands."""

    _ACTION_TO_CANONICAL_ID = {
        "待机/放松": "IDLE",
        "点头/同意": "NOD",
        "摇头/否定": "SHAKE_HEAD",
        "转身向左/看左边": "TURN_LEFT",
        "转身向右/看右边": "TURN_RIGHT",
        "眨眼/卖萌/Wink": "WINK",
        "身体晃动/开心/兴奋": "HAPPY",
        "歪头/疑惑/思考": "TILT_HEAD",
        "害羞/移开视线/不好意思": "LOOK_AWAY",
        "一般": "GENERAL",
    }

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

        if emotion in {"normal", "shy", "disgust", "angry"}:
            self._schedule(
                controller.send_live2d_event("emotion", emotion),
                f"emotion:{emotion}",
            )

        if not action:
            return

        action_id = self._ACTION_TO_CANONICAL_ID.get(action)
        if not action_id or action_id in {"IDLE", "GENERAL"}:
            return

        self._schedule(
            controller.send_canonical_action(action_id),
            f"action:{action}->{action_id}",
        )

    def _schedule(self, coroutine: Any, description: str) -> None:
        try:
            asyncio.create_task(coroutine)
            self.logger.info("Dispatched Live2D {}", description)
        except Exception as exc:
            if hasattr(coroutine, "close"):
                coroutine.close()
            self.logger.error("Failed to dispatch Live2D {}: {}", description, exc)
