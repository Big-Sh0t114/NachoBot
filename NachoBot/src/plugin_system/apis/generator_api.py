"""
回复器API模块

提供回复器相关功能，采用标准Python包设计模式
使用方式：
    from src.plugin_system.apis import generator_api
    replyer = generator_api.get_replyer(chat_stream)
    success, reply_set, _ = await generator_api.generate_reply(chat_stream, action_data, reasoning)
"""

import asyncio
import traceback
from typing import Tuple, Any, Dict, List, Optional, TYPE_CHECKING
from rich.traceback import install
from src.common.logger import get_logger
from src.common.data_models.message_data_model import ReplySetModel
from src.chat.replyer.group_generator import DefaultReplyer
from src.chat.replyer.private_generator import PrivateReplyer
from src.llm_models.exceptions import ReqAbortException
from src.chat.message_receive.chat_stream import ChatStream
from src.chat.utils.utils import process_llm_response
from src.chat.replyer.replyer_manager import replyer_manager
from src.chat.focus.reply_context import (
    ReplyContextRef,
    ReplyContextRequest,
    assemble_reply_context,
    release_reply_context,
)
from src.plugin_system.base.component_types import ActionInfo

if TYPE_CHECKING:
    from src.common.data_models.info_data_model import ActionPlannerInfo
    from src.common.data_models.database_data_model import DatabaseMessages
    from src.common.data_models.llm_data_model import LLMGenerationDataModel

install(extra_lines=3)

logger = get_logger("generator_api")


# =============================================================================
# 回复器获取API函数
# =============================================================================


def get_replyer(
    chat_stream: Optional[ChatStream] = None,
    chat_id: Optional[str] = None,
    request_type: str = "replyer",
) -> Optional[DefaultReplyer | PrivateReplyer]:
    """获取回复器对象

    优先使用chat_stream，如果没有则使用chat_id直接查找。
    使用 ReplyerManager 来管理实例，避免重复创建。

    Args:
        chat_stream: 聊天流对象（优先）
        chat_id: 聊天ID（实际上就是stream_id）
        request_type: 请求类型

    Returns:
        Optional[DefaultReplyer]: 回复器对象，如果获取失败则返回None

    Raises:
        ValueError: chat_stream 和 chat_id 均为空
    """
    if not chat_id and not chat_stream:
        raise ValueError("chat_stream 和 chat_id 不可均为空")
    try:
        logger.debug(f"[GeneratorAPI] 正在获取回复器，chat_id: {chat_id}, chat_stream: {'有' if chat_stream else '无'}")
        return replyer_manager.get_replyer(
            chat_stream=chat_stream,
            chat_id=chat_id,
            request_type=request_type,
        )
    except Exception as e:
        logger.error(f"[GeneratorAPI] 获取回复器时发生意外错误: {e}", exc_info=True)
        traceback.print_exc()
        return None


# =============================================================================
# 回复生成API函数
# =============================================================================


async def generate_reply(
    chat_stream: Optional[ChatStream] = None,
    chat_id: Optional[str] = None,
    action_data: Optional[Dict[str, Any]] = None,
    reply_message: Optional["DatabaseMessages"] = None,
    extra_info: str = "",
    person_profile_block: str = "",
    reply_reason: str = "",
    available_actions: Optional[Dict[str, ActionInfo]] = None,
    chosen_actions: Optional[List["ActionPlannerInfo"]] = None,
    enable_tool: bool = False,
    enable_splitter: bool = True,
    enable_chinese_typo: bool = True,
    request_type: str = "generator_api",
    from_plugin: bool = True,
    interrupt_flag: Optional[asyncio.Event] = None,
    reply_context: Optional[ReplyContextRequest] = None,
) -> Tuple[bool, Optional["LLMGenerationDataModel"]]:
    """生成回复

    Args:
        chat_stream: 聊天流对象（优先）
        chat_id: 聊天ID（备用）
        action_data: 动作数据（向下兼容，包含reply_to和extra_info）
        reply_message: 回复的消息对象
        extra_info: 额外信息，用于补充上下文
        person_profile_block: Rendered profile block for an explicit Replyer template slot
        reply_reason: 回复原因
        available_actions: 可用动作
        chosen_actions: 已选动作
        enable_tool: 是否启用工具调用
        enable_splitter: 是否启用消息分割器
        enable_chinese_typo: 是否启用错字生成器
        return_prompt: 是否返回提示词
        model_set_with_weight: 模型配置列表，每个元素为 (TaskConfig, weight) 元组
        request_type: 请求类型（可选，记录LLM使用）
        from_plugin: 是否来自插件
        reply_context: Focus 短期跨会话上下文请求；由服务端 provider 校验并预留
    Returns:
        Tuple[bool, List[Tuple[str, Any]], Optional[str]]: (是否成功, 回复集合, 提示词)
    """
    acquired_refs: tuple[ReplyContextRef, ...] = ()
    try:
        if chat_stream is not None and chat_id is not None and chat_stream.stream_id != chat_id:
            raise ValueError("chat_stream 与 chat_id 指向不同会话")
        canonical_chat_id = chat_stream.stream_id if chat_stream is not None else chat_id
        if not canonical_chat_id:
            raise ValueError("无法确定回复目标会话")
        if reply_message is not None:
            reply_message_chat_id = getattr(reply_message, "chat_id", None)
            if reply_message_chat_id and reply_message_chat_id != canonical_chat_id:
                raise ValueError("reply_message 不属于当前回复目标会话")

        # 获取回复器
        logger.debug("[GeneratorAPI] 开始生成回复")
        replyer = get_replyer(chat_stream, chat_id, request_type=request_type)
        if not replyer:
            logger.error("[GeneratorAPI] 无法获取回复器")
            return False, None
        if replyer.chat_stream.stream_id != canonical_chat_id:
            raise ValueError("Replyer 实例与回复目标会话不一致")

        prompt_context = await assemble_reply_context(reply_context, target_chat_id=canonical_chat_id)
        acquired_refs = prompt_context.context_refs

        if not extra_info and action_data:
            extra_info = action_data.get("extra_info", "")

        if not reply_reason and action_data:
            reply_reason = action_data.get("reason", "")

        # 调用回复器生成回复
        success, llm_response = await replyer.generate_reply_with_context(
            extra_info=extra_info,
            person_profile_block=person_profile_block,
            available_actions=available_actions,
            chosen_actions=chosen_actions,
            enable_tool=enable_tool,
            reply_message=reply_message,
            reply_reason=reply_reason,
            from_plugin=from_plugin,
            stream_id=canonical_chat_id,
            interrupt_flag=interrupt_flag,
            prompt_context=prompt_context,
        )
        if llm_response is not None:
            llm_response.context_refs = list(acquired_refs)
        if not success:
            await release_reply_context(acquired_refs, "generation_failed")
            acquired_refs = ()
            logger.warning("[GeneratorAPI] 回复生成失败")
            return False, None
        reply_set: Optional[ReplySetModel] = None
        if content := llm_response.content:
            text_for_json_check = content.strip()
            start_idx = text_for_json_check.find("{")
            end_idx = text_for_json_check.rfind("}")
            is_json_envelope = False
            if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                try:
                    import json

                    _ = json.loads(text_for_json_check[start_idx : end_idx + 1], strict=False)
                    is_json_envelope = True
                except Exception:
                    pass
            if is_json_envelope:
                # An adapter-owned JSON envelope is a wire message.  Do not run
                # human-text cleanup that could mutate its keys or values.
                reply_set = ReplySetModel()
                reply_set.add_text_content(content)
            else:
                reply_set = process_human_text(content, enable_splitter, enable_chinese_typo)
        llm_response.reply_set = reply_set
        if not content or not content.strip() or not reply_set or len(reply_set) == 0:
            await release_reply_context(acquired_refs, "empty_generation")
            llm_response.context_refs = []
            acquired_refs = ()
        logger.debug(f"[GeneratorAPI] 回复生成成功，生成了 {len(reply_set) if reply_set else 0} 个回复项")

        return success, llm_response

    except asyncio.CancelledError:
        await asyncio.shield(release_reply_context(acquired_refs, "generation_cancelled"))
        raise

    except ValueError as ve:
        await release_reply_context(acquired_refs, "invalid_request")
        raise ve

    except UserWarning as uw:
        await release_reply_context(acquired_refs, "generation_cancelled")
        logger.warning(f"[GeneratorAPI] 中断了生成: {uw}")
        return False, None

    except ReqAbortException:
        await release_reply_context(acquired_refs, "generation_cancelled")
        logger.debug("[GeneratorAPI] 回复生成被外部信号中断")
        return False, None

    except Exception as e:
        await release_reply_context(acquired_refs, "generation_error")
        logger.error(f"[GeneratorAPI] 生成回复时出错: {e}")
        logger.error(traceback.format_exc())
        return False, None


async def rewrite_reply(
    chat_stream: Optional[ChatStream] = None,
    reply_data: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
    enable_splitter: bool = True,
    enable_chinese_typo: bool = True,
    raw_reply: str = "",
    reason: str = "",
    reply_to: str = "",
    request_type: str = "generator_api",
) -> Tuple[bool, Optional["LLMGenerationDataModel"]]:
    """重写回复

    Args:
        chat_stream: 聊天流对象（优先）
        reply_data: 回复数据字典（向下兼容备用，当其他参数缺失时从此获取）
        chat_id: 聊天ID（备用）
        enable_splitter: 是否启用消息分割器
        enable_chinese_typo: 是否启用错字生成器
        model_set_with_weight: 模型配置列表，每个元素为 (TaskConfig, weight) 元组
        raw_reply: 原始回复内容
        reason: 回复原因
        reply_to: 回复对象
        return_prompt: 是否返回提示词

    Returns:
        Tuple[bool, List[Tuple[str, Any]]]: (是否成功, 回复集合)
    """
    try:
        # 获取回复器
        replyer = get_replyer(chat_stream, chat_id, request_type=request_type)
        if not replyer:
            logger.error("[GeneratorAPI] 无法获取回复器")
            return False, None

        logger.info("[GeneratorAPI] 开始重写回复")

        # 如果参数缺失，从reply_data中获取
        if reply_data:
            raw_reply = raw_reply or reply_data.get("raw_reply", "")
            reason = reason or reply_data.get("reason", "")
            reply_to = reply_to or reply_data.get("reply_to", "")

        # 调用回复器重写回复
        success, llm_response = await replyer.rewrite_reply_with_context(
            raw_reply=raw_reply,
            reason=reason,
            reply_to=reply_to,
        )
        reply_set: Optional[ReplySetModel] = None
        if success and llm_response and (content := llm_response.content):
            text_for_json_check = content.strip()
            start_idx = text_for_json_check.find("{")
            end_idx = text_for_json_check.rfind("}")
            is_json_envelope = False
            if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                try:
                    import json

                    _ = json.loads(text_for_json_check[start_idx : end_idx + 1], strict=False)
                    is_json_envelope = True
                except Exception:
                    pass
            if is_json_envelope:
                reply_set = ReplySetModel()
                reply_set.add_text_content(content)
            else:
                reply_set = process_human_text(content, enable_splitter, enable_chinese_typo)
        llm_response.reply_set = reply_set
        if success:
            logger.info(f"[GeneratorAPI] 重写回复成功，生成了 {len(reply_set) if reply_set else 0} 个回复项")
        else:
            logger.warning("[GeneratorAPI] 重写回复失败")

        return success, llm_response

    except ValueError as ve:
        raise ve

    except Exception as e:
        logger.error(f"[GeneratorAPI] 重写回复时出错: {e}")
        return False, None


def process_human_text(content: str, enable_splitter: bool, enable_chinese_typo: bool) -> Optional[ReplySetModel]:
    """将文本处理为更拟人化的文本

    Args:
        content: 文本内容
        enable_splitter: 是否启用消息分割器
        enable_chinese_typo: 是否启用错字生成器
    """
    if not isinstance(content, str):
        raise ValueError("content 必须是字符串类型")
    try:
        reply_set = ReplySetModel()
        processed_response = process_llm_response(content, enable_splitter, enable_chinese_typo)

        for text in processed_response:
            reply_set.add_text_content(text)

        return reply_set

    except Exception as e:
        logger.error(f"[GeneratorAPI] 处理人形文本时出错: {e}")
        return None


async def generate_response_custom(
    chat_stream: Optional[ChatStream] = None,
    chat_id: Optional[str] = None,
    request_type: str = "generator_api",
    prompt: str = "",
) -> Optional[str]:
    replyer = get_replyer(chat_stream, chat_id, request_type=request_type)
    if not replyer:
        logger.error("[GeneratorAPI] 无法获取回复器")
        return None

    try:
        logger.debug("[GeneratorAPI] 开始生成自定义回复")
        response, _, _, _ = await replyer.llm_generate_content(prompt)
        if response:
            logger.debug("[GeneratorAPI] 自定义回复生成成功")
            return response
        else:
            logger.warning("[GeneratorAPI] 自定义回复生成失败")
            return None
    except Exception as e:
        logger.error(f"[GeneratorAPI] 生成自定义回复时出错: {e}")
        return None
