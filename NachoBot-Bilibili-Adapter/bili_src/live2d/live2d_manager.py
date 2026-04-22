import asyncio
import json
import logging
from typing import Optional, Tuple, Any


class Live2DManager:
    _ACTION_TO_MOTION_GROUP = {
        "待机/放松": "Idle",
        "点头/同意": "Nod",
        "摇头/否定": "Shake",
        "转身向左/看左边": "TurnLeft",
        "转身向右/看右边": "TurnRight",
        "眨眼/卖萌/Wink": "Wink",
        "身体晃动/开心/兴奋": "Sway",
        "歪头/疑惑/思考": "TiltHead",
        "害羞/移开视线/不好意思": "LookAway",
        "一般": "",
    }

    def __init__(self, config: Any, logger: logging.Logger, adapter_ref: Any = None):
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref
        self.controller = None

        if self.config.live_live2d_enable:
            try:
                from live2d_render.controller import Live2DController

                self.controller = Live2DController(adapter_ref, logger)
            except Exception as e:
                self.logger.error(f"Failed to initialize Live2DController: {e}")

    async def start(self):
        if self.controller:
            await self.controller.start()

    def extract_json_emotion_from_text(
        self, text: str
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """尝试解析回复中的JSON表情+动作指令，返回(解析后文本, emotion, action)。"""
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        parsed_text = ""
        emotion = None
        action = None

        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
            try:
                json_str = text[start_idx : end_idx + 1]
                data = json.loads(json_str, strict=False)

                if "reply" in data and data["reply"]:
                    parsed_text = str(data["reply"])
                emotion = data.get("emotion")
                action = data.get("action")

                return parsed_text if parsed_text else text, emotion, action
            except Exception as e:
                self.logger.debug(f"JSON parsing failed (fallback to raw): {e}")

        return text, None, None

    def execute_extracted_live2d_action(
        self, emotion: Optional[str], action: Optional[str]
    ) -> None:
        """从 extract_json_emotion_from_text 提取出的指令执行 Live2D 事件"""
        ctrl = self.controller
        if not ctrl:
            return

        if emotion and emotion in ["normal", "shy", "disgust", "angry"]:
            try:
                asyncio.create_task(ctrl.send_live2d_event("emotion", emotion))
                self.logger.info(f"Dispatched Live2D emotion event: {emotion}")
            except Exception as e:
                self.logger.error(f"Failed to dispatch Live2D emotion: {e}")

        if action:
            motion_group = self._ACTION_TO_MOTION_GROUP.get(action, "")
            if motion_group and motion_group != "Idle":
                try:
                    asyncio.create_task(
                        ctrl.send_live2d_event(
                            "random_motion",
                            {"group": motion_group, "priority": 3},
                        )
                    )
                    self.logger.info(
                        f"Dispatched Live2D action: {action} -> {motion_group}"
                    )
                except Exception as e:
                    self.logger.error(f"Failed to dispatch Live2D action: {e}")
