from src.config.config import global_config

from src.common.logger import get_logger
from rich.traceback import install

install(extra_lines=3)

logger = get_logger("chat_voice")


async def get_voice_text(voice_base64: str) -> str:
    """获取音频文件转录文本"""
    if not global_config.voice.enable_asr:
        logger.warning("语音识别未启用，无法处理语音消息")
        return "[语音]"
    try:
        # Conversational ASR is a Core-owned facade operation.  Platform
        # adapters only deliver the voice field and never choose a model.
        from src.multimodal import get_multimodal_router

        result = await get_multimodal_router().transcribe(voice_base64)
        text = result.text.strip()
        if not text:
            logger.warning("未能生成语音文本")
            return "[语音(文本生成失败)]"

        logger.debug(f"描述是{text}")
        if result.degraded:
            # A voice-only failure must become one visible text outcome, not a
            # fabricated transcript and not a second Planner turn.
            return text
        return f"[语音：{text}]"
    except Exception as e:
        logger.error(f"语音转文字失败: {str(e)}")
        return "[语音识别失败，请稍后重试]"
