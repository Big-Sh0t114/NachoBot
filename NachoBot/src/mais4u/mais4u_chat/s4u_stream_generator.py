import asyncio
import random
from typing import AsyncGenerator, Tuple

from src.llm_models.utils_model import LLMRequest
from src.llm_models.payload_content.message import MessageBuilder
from src.config.config import model_config
from src.chat.message_receive.message import MessageRecvS4U
from src.mais4u.mais4u_chat.s4u_prompt import prompt_builder
from src.common.logger import get_logger
from src.mais4u.s4u_config import s4u_config_main
import re


logger = get_logger("s4u_stream_generator")


class S4UStreamGenerator:
    @property
    def llm_request(self) -> LLMRequest:
        return LLMRequest(model_set=model_config.model_task_config.replyer, request_type="s4u_replyer")

    def __init__(self):
        self.current_model_name = "unknown model"
        self.partial_response = ""

        # 正则表达式用于按句子切分，同时处理各种标点和边缘情况
        # 匹配常见的句子结束符，但会忽略引号内和数字中的标点
        self.sentence_split_pattern = re.compile(
            r'([^\s\w"\'([{]*["\'([{].*?["\'}\])][^\s\w"\'([{]*|'  # 匹配被引号/括号包裹的内容
            r'[^.。!?？！\n\r]+(?:[.。!?？！\n\r](?![\'"])|$))',  # 匹配直到句子结束符
            re.UNICODE | re.DOTALL,
        )

        self.chat_stream = None

    async def build_last_internal_message(self, message: MessageRecvS4U, previous_reply_context: str = ""):
        # person_id = PersonInfoManager.get_person_id(
        #     message.chat_stream.user_info.platform, message.chat_stream.user_info.user_id
        # )
        # person_info_manager = get_person_info_manager()
        # person_name = await person_info_manager.get_value(person_id, "person_name")

        # if message.chat_stream.user_info.user_nickname:
        #     if person_name:
        #         sender_name = f"[{message.chat_stream.user_info.user_nickname}]（{person_name}）"
        #     else:
        #         sender_name = f"[{message.chat_stream.user_info.user_nickname}]"
        # else:
        #     sender_name = f"用户({message.chat_stream.user_info.user_id})"

        # 构建prompt
        if previous_reply_context:
            message_txt = f"""
            你正在回复用户的消息，但中途被打断了。这是已有的对话上下文:
            [你已经对上一条消息说的话]: {previous_reply_context}
            ---
            [这是用户发来的新消息, 你需要结合上下文，对此进行回复]:
            {message.processed_plain_text}
            """
            return True, message_txt
        else:
            message_txt = message.processed_plain_text
            return False, message_txt

    async def generate_response(
        self, message: MessageRecvS4U, previous_reply_context: str = ""
    ) -> AsyncGenerator[str, None]:
        """根据当前模型类型选择对应的生成函数"""
        # 从global_config中获取模型概率值并选择模型
        self.partial_response = ""
        message_txt = message.processed_plain_text
        if not message.is_internal:
            interupted, message_txt_added = await self.build_last_internal_message(message, previous_reply_context)
            if interupted:
                message_txt = message_txt_added

        message.chat_stream = self.chat_stream
        prompt = await prompt_builder.build_prompt_normal(
            message=message,
            message_txt=message_txt,
        )

        logger.info(
            f"{self.current_model_name}思考:{message_txt[:30] + '...' if len(message_txt) > 30 else message_txt}"
        )  # noqa: E501

        # 使用LLMRequest进行流式生成
        async for chunk in self._generate_response_with_llm_request(prompt):
            yield chunk

    async def _generate_response_with_llm_request(self, prompt: str) -> AsyncGenerator[str, None]:
        """使用LLMRequest进行流式响应生成"""

        # 构建消息
        message_builder = MessageBuilder()
        message_builder.add_text_content(prompt)
        messages = [message_builder.build()]

        # 选择模型
        model_info, api_provider, client = self.llm_request._select_model()
        self.current_model_name = model_info.name

        # 定义message_factory用于正确调用_execute_request
        def message_factory(client):
            return messages

        # 使用generate_response_async，直接传入prompt字符串
        try:
            response, (reasoning, model_name, tool_calls) = await self.llm_request.generate_response_async(
                prompt=prompt,
            )

            content = response or ""
            async for chunk in self._process_content_streaming(content):
                yield chunk
        except Exception as e:
            logger.error(f"LLM请求执行失败: {e}")
            raise

    async def _process_buffer_streaming(self, buffer: str) -> AsyncGenerator[str, None]:
        """实时处理缓冲区内容，输出完整句子"""
        # 使用正则表达式匹配完整句子
        for match in self.sentence_split_pattern.finditer(buffer):
            sentence = match.group(0).strip()
            if sentence and match.end(0) <= len(buffer):
                # 检查句子是否完整（以标点符号结尾）
                if sentence.endswith(("。", "！", "？", ".", "!", "?")):
                    if sentence not in [",", "，", ".", "。", "!", "！", "?", "？"]:
                        self.partial_response += sentence
                        yield sentence

    async def _process_content_streaming(self, content: str) -> AsyncGenerator[str, None]:
        """处理内容进行流式输出（用于非流式模型的模拟流式输出）"""
        buffer = content
        punctuation_buffer = ""

        # 使用正则表达式匹配句子
        last_match_end = 0
        for match in self.sentence_split_pattern.finditer(buffer):
            sentence = match.group(0).strip()
            if sentence:
                # 检查是否只是一个标点符号
                if sentence in [",", "，", ".", "。", "!", "！", "?", "？"]:
                    punctuation_buffer += sentence
                else:
                    # 发送之前累积的标点和当前句子
                    to_yield = punctuation_buffer + sentence
                    if to_yield.endswith((",", "，")):
                        to_yield = to_yield.rstrip(",，")

                    self.partial_response += to_yield
                    yield to_yield
                    punctuation_buffer = ""  # 清空标点符号缓冲区

                last_match_end = match.end(0)

        # 发送缓冲区中剩余的任何内容
        remaining = buffer[last_match_end:].strip()
        to_yield = (punctuation_buffer + remaining).strip()
        if to_yield:
            if to_yield.endswith(("，", ",")):
                to_yield = to_yield.rstrip("，,")
            if to_yield:
                self.partial_response += to_yield
                yield to_yield

    async def _generate_response_with_model(
        self,
        prompt: str,
        client,
        model_name: str,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """保留原有方法签名以保持兼容性，但重定向到新的实现"""
        async for chunk in self._generate_response_with_llm_request(prompt):
            yield chunk

    # ============ Streamer Mode Methods ============

    def get_length_instruction(self, valid_danmu_count: int) -> Tuple[str, int]:
        """
        根据有效弹幕数量返回长度指令和目标段数

        Args:
            valid_danmu_count: 有效弹幕数量

        Returns:
            (length_instruction, segment_count)
        """
        config = s4u_config_main.streamer_mode

        if valid_danmu_count > config.high_activity_threshold:
            # 高活跃度：短回复
            instruction = f"回复简短，{config.short_reply_length}字左右。"
            return instruction, 1
        elif valid_danmu_count >= config.medium_activity_threshold:
            # 中等活跃度：中等回复
            instruction = f"可以稍微多说一点，{config.medium_reply_length}字左右。"
            return instruction, 2
        else:
            # 低活跃度：长回复（多段）
            instruction = f"""
请分3-5段说，模拟真人主播风格：
- 第1段：直接回应弹幕
- 后续段：延伸、转折、或跳到相关话题
- 像聊天一样，段之间可以话题跳跃
- 允许突然想起什么、吐槽、自问自答
每段用 "---" 分隔，总计{config.long_reply_length}字左右。
"""
            return instruction, 4

    async def generate_response_with_dynamic_length(
        self, message: MessageRecvS4U, valid_danmu_count: int, previous_reply_context: str = ""
    ) -> AsyncGenerator[str, None]:
        """
        主播模式下带动态长度和段间等待的响应生成

        Args:
            message: 消息对象
            valid_danmu_count: 当前有效弹幕数量
            previous_reply_context: 之前的回复上下文

        Yields:
            每段内容（带段间等待）
        """
        config = s4u_config_main.streamer_mode
        self.partial_response = ""

        # 获取动态长度指令
        length_instruction, expected_segments = self.get_length_instruction(valid_danmu_count)

        # 构建消息文本
        message_txt = message.processed_plain_text
        if not message.is_internal:
            interrupted, message_txt_added = await self.build_last_internal_message(message, previous_reply_context)
            if interrupted:
                message_txt = message_txt_added

        message.chat_stream = self.chat_stream

        # 构建带动态长度指令的 prompt
        prompt = await prompt_builder.build_prompt_normal(
            message=message,
            message_txt=message_txt,
            extra_instruction=length_instruction,  # 注入动态长度指令
        )

        logger.info(f"[StreamerMode] 生成回复 (有效弹幕={valid_danmu_count}, 预期段数={expected_segments})")

        # 收集完整响应
        full_response = ""
        async for chunk in self._generate_response_with_llm_request(prompt):
            full_response += chunk

        # 解析多段（按 "---" 分隔）
        if "---" in full_response:
            segments = [s.strip() for s in full_response.split("---") if s.strip()]
        else:
            # 如果没有分隔符，作为单段处理
            segments = [full_response.strip()] if full_response.strip() else []

        logger.info(f"[StreamerMode] 解析到 {len(segments)} 段内容")

        # 逐段输出，带段间等待
        for i, segment in enumerate(segments):
            self.partial_response += segment
            yield segment

            # 除了最后一段，每段后面加随机等待
            if i < len(segments) - 1:
                wait_time = random.uniform(config.segment_wait_min, config.segment_wait_max)
                logger.info(f"[StreamerMode] 段 {i + 1} 完成，等待 {wait_time:.1f} 秒...")
                await asyncio.sleep(wait_time)
