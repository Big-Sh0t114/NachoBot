from typing import AsyncGenerator
from src.llm_models.utils_model import LLMRequest, RequestType
from src.llm_models.payload_content.message import MessageBuilder
from src.config.config import model_config
from src.chat.message_receive.message import MessageRecvS4U
from src.mais4u.mais4u_chat.s4u_prompt import prompt_builder
from src.common.logger import get_logger
import re


logger = get_logger("s4u_stream_generator")


class S4UStreamGenerator:
    def __init__(self):
        # 使用LLMRequest替代AsyncOpenAIClient
        self.llm_request = LLMRequest(model_set=model_config.model_task_config.replyer, request_type="s4u_replyer")

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
        #         sender_name = f"[{message.chat_stream.user_info.user_nickname}]（你叫ta{person_name}）"
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

        # 如果模型支持强制流式模式，使用真正的流式处理
        if model_info.force_stream_mode:
            # 简化流式处理：直接使用LLMRequest的流式功能
            try:
                # 直接调用LLMRequest的流式处理
                response = await self.llm_request._execute_request(
                    api_provider=api_provider,
                    client=client,
                    request_type=RequestType.RESPONSE,
                    model_info=model_info,
                    message_list=messages,
                )

                # 处理响应内容
                content = response.content or ""
                if content:
                    # 将内容按句子分割并输出
                    async for chunk in self._process_content_streaming(content):
                        yield chunk

            except Exception as e:
                logger.error(f"流式请求执行失败: {e}")
                # 如果流式请求失败，回退到普通模式
                response = await self.llm_request._execute_request(
                    api_provider=api_provider,
                    client=client,
                    request_type=RequestType.RESPONSE,
                    model_info=model_info,
                    message_list=messages,
                )
                content = response.content or ""
                async for chunk in self._process_content_streaming(content):
                    yield chunk

        else:
            # 如果不支持流式，使用普通方式然后模拟流式输出
            response = await self.llm_request._execute_request(
                api_provider=api_provider,
                client=client,
                request_type=RequestType.RESPONSE,
                model_info=model_info,
                message_list=messages,
            )

            content = response.content or ""
            async for chunk in self._process_content_streaming(content):
                yield chunk

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
