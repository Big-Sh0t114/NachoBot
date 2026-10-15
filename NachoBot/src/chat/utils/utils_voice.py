from src.config.config import global_config

from src.common.logger import get_logger
from rich.traceback import install

install(extra_lines=3)

logger = get_logger("chat_voice")


def normalize_asr_receipt_platform(platform: object) -> str | None:
    """Normalize one platform name against Core's ASR receipt allowlist."""

    from src.multimodal.contracts import normalize_asr_stream_platform

    return normalize_asr_stream_platform(platform)


def normalize_asr_receipt_scope(scope: object) -> str | None:
    """Normalize an optional bounded opaque scope used to claim a receipt."""

    from src.multimodal.contracts import normalize_asr_stream_scope

    return normalize_asr_stream_scope(scope)


async def get_voice_text(
    voice_base64: str,
    *,
    precomputed_asr_result_id: str | None = None,
    precomputed_asr_context: str | None = "universal_vc",
    precomputed_asr_scope: str = "",
) -> str:
    """获取音频文件转录文本"""
    if not global_config.voice.enable_asr:
        logger.warning("语音识别未启用，无法处理语音消息")
        return "[语音]"
    try:
        # Conversational ASR is a Core-owned facade operation.  Platform
        # adapters only deliver the voice field and never choose a model.
        from src.multimodal import get_multimodal_router

        receipt_context = normalize_asr_receipt_platform(precomputed_asr_context)
        receipt_scope = normalize_asr_receipt_scope(precomputed_asr_scope)
        receipt_id = (
            precomputed_asr_result_id
            if receipt_context is not None and receipt_scope is not None
            else None
        )
        transcribe_kwargs = {
            "precomputed_asr_result_id": receipt_id,
            "precomputed_asr_context": receipt_context or "",
        }
        if receipt_scope:
            transcribe_kwargs["precomputed_asr_scope"] = receipt_scope
        result = await get_multimodal_router().transcribe(voice_base64, **transcribe_kwargs)
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
