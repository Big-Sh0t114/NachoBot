"""Platform-neutral emotion and question to avatar-action mapping.

This module intentionally has no Live2D SDK, Bilibili, or NachoBot imports.  It
only turns reply metadata and the user's text into canonical avatar controls;
the Live2D adapter remains responsible for resolving those controls to the
current model's motion groups.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any


CANONICAL_EMOTIONS = frozenset({"normal", "shy", "disgust", "angry", "joy", "fear", "sorrow"})


@dataclass(frozen=True, slots=True)
class ActionDecision:
    """The canonical controls selected for one reply."""

    emotion: str | None
    action_id: str | None
    reason: str


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return "".join(character for character in text if not character.isspace())


class ActionAdapter:
    """Select avatar actions without knowing which platform produced a reply."""

    _EMOTION_ALIASES = {
        "normal": "normal",
        "default": "normal",
        "neutral": "normal",
        "普通": "normal",
        "默认": "normal",
        "happy": "joy",
        "joy": "joy",
        "smile": "joy",
        "开心": "joy",
        "高兴": "joy",
        "兴奋": "joy",
        "shy": "shy",
        "blush": "shy",
        "embarrassed": "shy",
        "害羞": "shy",
        "脸红": "shy",
        "disgust": "disgust",
        "dislike": "disgust",
        "厌恶": "disgust",
        "嫌弃": "disgust",
        "angry": "angry",
        "anger": "angry",
        "mad": "angry",
        "生气": "angry",
        "愤怒": "angry",
        "fear": "fear",
        "surprise": "fear",
        "surprised": "fear",
        "惊讶": "fear",
        "sorrow": "sorrow",
        "sad": "sorrow",
        "悲伤": "sorrow",
        "难过": "sorrow",
    }

    _ACTION_ALIASES = {
        "idle": "IDLE",
        "IDLE": "IDLE",
        "standby": "IDLE",
        "待机": "IDLE",
        "待机放松": "IDLE",
        "待机/放松": "IDLE",
        "nod": "NOD",
        "NOD": "NOD",
        "agree": "NOD",
        "点头": "NOD",
        "同意": "NOD",
        "点头/同意": "NOD",
        "shake": "SHAKE_HEAD",
        "shakehead": "SHAKE_HEAD",
        "shake_head": "SHAKE_HEAD",
        "SHAKE_HEAD": "SHAKE_HEAD",
        "disagree": "SHAKE_HEAD",
        "摇头": "SHAKE_HEAD",
        "否定": "SHAKE_HEAD",
        "摇头/否定": "SHAKE_HEAD",
        "turnleft": "TURN_LEFT",
        "turn_left": "TURN_LEFT",
        "TURN_LEFT": "TURN_LEFT",
        "lookleft": "TURN_LEFT",
        "向左": "TURN_LEFT",
        "看左边": "TURN_LEFT",
        "转身向左/看左边": "TURN_LEFT",
        "turnright": "TURN_RIGHT",
        "turn_right": "TURN_RIGHT",
        "TURN_RIGHT": "TURN_RIGHT",
        "lookright": "TURN_RIGHT",
        "向右": "TURN_RIGHT",
        "看右边": "TURN_RIGHT",
        "转身向右/看右边": "TURN_RIGHT",
        "wink": "WINK",
        "WINK": "WINK",
        "wave": "WINK",
        "眨眼": "WINK",
        "卖萌": "WINK",
        "眨眼/卖萌/Wink": "WINK",
        "开心": "HAPPY",
        "兴奋": "HAPPY",
        "身体晃动": "HAPPY",
        "happy": "HAPPY",
        "HAPPY": "HAPPY",
        "dance": "HAPPY",
        "身体晃动/开心/兴奋": "HAPPY",
        "tilthead": "TILT_HEAD",
        "tilt_head": "TILT_HEAD",
        "TILT_HEAD": "TILT_HEAD",
        "think": "TILT_HEAD",
        "歪头": "TILT_HEAD",
        "疑惑": "TILT_HEAD",
        "思考": "TILT_HEAD",
        "歪头/疑惑/思考": "TILT_HEAD",
        "lookaway": "LOOK_AWAY",
        "look_away": "LOOK_AWAY",
        "LOOK_AWAY": "LOOK_AWAY",
        "shylook": "LOOK_AWAY",
        "移开视线": "LOOK_AWAY",
        "害羞": "LOOK_AWAY",
        "害羞/移开视线/不好意思": "LOOK_AWAY",
        "一般": "GENERAL",
        "general": "GENERAL",
        "GENERAL": "GENERAL",
    }

    _EMOTION_ACTIONS = {
        "joy": "HAPPY",
        "shy": "LOOK_AWAY",
        "angry": "SHAKE_HEAD",
        "disgust": "SHAKE_HEAD",
        "fear": "WINK",
        "sorrow": "NOD",
    }

    _QUESTION_MARKERS = ("?", "？", "吗", "嗎", "么", "麼", "什么", "什麼", "为什么", "為什麼", "怎么", "怎麼", "如何", "哪儿", "哪裡", "哪里", "幾", "几")
    _NEGATIVE_MARKERS = ("不", "不是", "别", "不要", "不能", "拒绝", "錯", "错", "讨厌", "討厭", "不喜欢", "不喜歡")
    _PRAISE_MARKERS = ("谢谢", "謝謝", "喜欢你", "喜歡你", "好棒", "厉害", "厲害", "可爱", "可愛", "开心", "高兴", "高興", "爱你", "愛你")
    _GREETING_MARKERS = ("你好", "您好", "嗨", "哈喽", "哈囉", "hello", "早上好", "晚上好", "晚安", "欢迎", "歡迎")
    _FAREWELL_MARKERS = ("再见", "再見", "拜拜", "bye", "晚安")

    def __init__(self, logger: Any = None) -> None:
        self.logger = logger

    @classmethod
    def normalize_emotion(cls, value: Any) -> str | None:
        normalized = _normalize(value)
        if not normalized:
            return None
        return cls._EMOTION_ALIASES.get(normalized)

    @classmethod
    def normalize_action(cls, value: Any) -> str | None:
        normalized = _normalize(value)
        if not normalized:
            return None
        return cls._ACTION_ALIASES.get(normalized)

    def decide(
        self,
        *,
        question: str = "",
        reply: str = "",
        emotion: Any = None,
        requested_action: Any = None,
    ) -> ActionDecision:
        """Choose stable canonical controls from metadata and conversation text.

        Explicit model metadata wins.  ``一般`` and unknown actions are treated
        as no explicit action so a clear question or emotion still gets a
        meaningful gesture.
        """

        normalized_emotion = self.normalize_emotion(emotion)
        action_id = self.normalize_action(requested_action)
        if action_id in {"IDLE", "GENERAL"}:
            action_id = None

        context = f"{question}\n{reply}"
        if normalized_emotion is None:
            normalized_emotion = self._infer_emotion(context)
        if action_id is not None:
            return ActionDecision(normalized_emotion, action_id, "explicit_action")

        action_id, reason = self._infer_action(question, context, normalized_emotion)
        return ActionDecision(normalized_emotion, action_id, reason)

    @classmethod
    def _infer_emotion(cls, text: str) -> str:
        normalized = _normalize(text)
        if any(marker in normalized for marker in ("害羞", "脸红", "不好意思")):
            return "shy"
        if any(marker in normalized for marker in ("生气", "愤怒", "讨厌", "討厭")):
            return "angry"
        if any(marker in normalized for marker in ("难过", "難過", "悲伤", "悲傷", "伤心", "傷心")):
            return "sorrow"
        if any(marker in normalized for marker in cls._PRAISE_MARKERS):
            return "joy"
        if any(marker in normalized for marker in ("惊讶", "驚訝", "吓", "嚇", "真的吗", "真的嗎")):
            return "fear"
        return "normal"

    @classmethod
    def _infer_action(
        cls,
        question: str,
        context: str,
        emotion: str | None,
    ) -> tuple[str | None, str]:
        normalized_question = _normalize(question)
        normalized_context = _normalize(context)
        if any(marker in normalized_question for marker in cls._QUESTION_MARKERS):
            return "TILT_HEAD", "question"
        if emotion in cls._EMOTION_ACTIONS:
            return cls._EMOTION_ACTIONS[emotion], f"emotion:{emotion}"
        if any(marker in normalized_context for marker in cls._NEGATIVE_MARKERS):
            return "SHAKE_HEAD", "negative_context"
        if any(marker in normalized_context for marker in cls._FAREWELL_MARKERS):
            return "LOOK_AWAY", "farewell"
        if any(marker in normalized_context for marker in cls._PRAISE_MARKERS):
            return "HAPPY", "praise"
        if any(marker in normalized_context for marker in cls._GREETING_MARKERS):
            return "WINK", "greeting"
        return None, "neutral"
