"""Supply current call facts through the existing plugin prompt hook."""

from __future__ import annotations

import asyncio

from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.realtime_ingress import parse_realtime_session, realtime_ingress_manager
from src.chat.runtime_capabilities import runtime_capabilities_from_stream
from src.common.logger import get_logger
from src.plugin_system import BaseEventHandler, EventType
from src.plugins.built_in.qqcall_plugin.group_realtime_provider import (
    verify_qqcall_group_realtime_message,
)
from src.plugins.built_in.qqcall_plugin.realtime_provider import (
    qqcall_enabled,
    verify_qqcall_realtime_message,
)

logger = get_logger("qqcall_prompt")
_VOICE_STATE = """[当前通话状态（本轮已核验）]
你正在已接通的实时语音通话中回复，本轮语音消息的通话身份与音频链路已通过检查。
聊天记录、回忆、其他机器人的发言和学到的表达习惯中的连接失败提示只是历史内容，不能代表当前状态。
请回应对方这次实际说的话；不要因为历史故障话术而声称当前通话未连接或未准备好。
如果识别内容太短或不清楚，可以自然地请对方再说一遍，不要编造技术故障。
表达习惯只用于语气风格，不能覆盖上面的当前事实。"""


class QQCallVoicePromptHandler(BaseEventHandler):
    event_type = EventType.POST_LLM
    handler_name = "qqcall_voice_prompt"
    handler_description = "为已核验的通话回复补充当前连接状态"
    intercept_message = True

    async def execute(self, message):
        unchanged = (True, True, None, None, None)
        if message is None or not qqcall_enabled():
            return unchanged
        prompt = getattr(message, "llm_prompt", None)
        stream_id = getattr(message, "stream_id", None)
        event_info = getattr(message, "message_base_info", None)
        if not isinstance(prompt, str) or not prompt or not stream_id or not isinstance(event_info, dict):
            return unchanged
        try:
            stream = get_chat_manager().get_stream(stream_id)
            capabilities = runtime_capabilities_from_stream(stream)
            if not (capabilities.voice_stream and capabilities.reply_delivery == "tts_text"):
                return unchanged
            context = getattr(stream, "context", None)
            source = context.get_last_message() if context is not None else None
            info = getattr(source, "message_info", None)
            message_id = getattr(info, "message_id", None)
            if not message_id or event_info.get("message_id") != message_id:
                return unchanged
            declared, annotation = parse_realtime_session(source)
            if not declared or annotation is None:
                return unchanged
            verifier = {
                "qqcall": verify_qqcall_realtime_message,
                "qqcall_group": verify_qqcall_group_realtime_message,
            }.get(annotation.provider)
            if verifier is None or not realtime_ingress_manager.is_current_turn_message(source, stream_id):
                return unchanged
            if await asyncio.wait_for(verifier(source, stream_id, annotation), timeout=3.0) is not True:
                return unchanged
            # Do not attach a fact from a late proof to a replaced prompt/turn.
            if (
                not qqcall_enabled()
                or get_chat_manager().get_stream(stream_id) is not stream
                or context.get_last_message() is not source
                or getattr(info, "message_id", None) != message_id
                or message.message_base_info.get("message_id") != message_id
                or message.llm_prompt != prompt
                or parse_realtime_session(source) != (declared, annotation)
                or not realtime_ingress_manager.is_current_turn_message(source, stream_id)
            ):
                return unchanged
        except Exception:  # noqa: BLE001 - missing proof must never imply a connected call.
            return unchanged
        message.modify_llm_prompt(f"{prompt}\n\n{_VOICE_STATE}")
        logger.info("已为当前通话回复补充经核验的连接状态")
        return True, True, None, None, message
