"""
信使 (Messenger) 插件 - 核心逻辑

当用户发送 "帮我转告XX <内容>" 时：
1. 提取目标名称和转述内容
2. 在 PersonInfo 中双向模糊匹配目标用户
3. 查找目标用户的私聊 stream_id
4. 向目标私聊发送转告通知
5. 注入消息触发 LLM 思考
"""

import asyncio
import re
import hashlib
import time
from typing import Optional, Tuple, Dict

from src.common.logger import get_logger
from src.common.database.database_model import PersonInfo as PersonInfoModel
from src.config.config import model_config
from src.llm_models.utils_model import LLMRequest
from src.person_info.person_info import (
    person_info_manager,
    calculate_string_similarity,
    Person,
)
from src.chat.message_receive.chat_stream import get_chat_manager
from src.plugin_system.apis import send_api, message_api
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import EventType, NachoMessages

logger = get_logger("messenger")

# 转告意图正则预筛选（宽松匹配，由 LLM 精确判断）
RELAY_PATTERN = re.compile(
    r"(?:帮我|帮忙)?(?:转告|告诉|问|传话|带话|带个话|传个话)"
    r"|(?:跟|和|给|替我跟|替我给|替我和).{1,10}(?:说|问|讲|传话|转告|带话)"
    r"|(?:告诉|转告|问).{1,10}一下"
)


class MessengerEventHandler(BaseEventHandler):
    """信使事件处理器 - 监听 ON_MESSAGE 事件，检测并处理转告请求"""

    event_type = EventType.ON_MESSAGE
    handler_name = "messenger_handler"
    handler_description = "检测转告请求并发送消息到目标用户的私聊"
    weight = 10  # 较高权重，优先处理
    intercept_message = False  # 不拦截消息，允许后续处理继续

    def __init__(self):
        super().__init__()
        self._llm = LLMRequest(
            model_set=model_config.model_task_config.utils_small,
            request_type="messenger.extract",
        )
        # 记录每个 target_stream_id 最近的转告来源用户名（用于解析代词）
        self._relay_source_cache: Dict[str, str] = {}

    async def execute(
        self, message: NachoMessages | None
    ) -> Tuple[bool, bool, Optional[str], None, Optional[NachoMessages]]:
        """
        处理 ON_MESSAGE 事件

        Returns:
            (success, continue_processing, response_text, custom_result, modified_message)
        """
        if message is None:
            return True, True, None, None, None

        text = message.plain_text or ""
        if not text:
            return True, True, None, None, None

        # 去除引用回复前缀，只检查用户自己的消息内容
        # QQ 回复格式: [回复<XX:ID>：引用内容]，说：实际消息
        actual_text = re.sub(r"\[回复.*?\](?:，说：\s*)?", "", text).strip()
        if not actual_text:
            return True, True, None, None, None

        # 正则预筛选 - 快速跳过无关消息
        if not RELAY_PATTERN.search(actual_text):
            return True, True, None, None, None

        logger.info(f"[信使] 检测到疑似转告请求: {text[:50]}...")

        # 仅对 QQ 平台生效，其他平台当成普通消息处理
        source_info = message.message_base_info or {}
        source_platform = source_info.get("platform", "")
        if source_platform != "qq":
            logger.debug(f"[信使] 非 QQ 平台消息 ({source_platform})，跳过")
            return True, True, None, None, None

        # 获取来源信息
        stream_id = message.stream_id
        if not stream_id:
            return True, True, None, None, None

        # 获取发送者信息（source_info/source_platform 已在上方平台检查时提取）
        source_user_id = source_info.get("user_id", "")
        source_nickname = source_info.get("user_nickname", "未知用户")

        # 使用 Person 获取发送者的 person_name（防御性处理）
        source_name = source_nickname
        if source_platform and source_user_id:
            try:
                source_person = Person(platform=source_platform, user_id=source_user_id)
                source_name = source_person.person_name or source_nickname
            except Exception:
                logger.debug(f"[信使] 无法获取发送者 Person 信息，使用昵称: {source_nickname}")

        # Step 1: LLM 提取目标名称和转述内容（传入 stream_id 获取近期对话上下文）
        target_name, content = await self._extract_relay_info(text, stream_id)
        if not target_name or not content:
            logger.info("[信使] LLM 未能提取出有效的目标名称或内容，跳过")
            return True, True, None, None, None

        logger.info(f"[信使] 提取结果 - 目标: {target_name}, 内容: {content}")

        # Step 2: 双向模糊匹配目标用户
        similarity_threshold = self.get_config("components.similarity_threshold", 0.4)
        matched_person_id, matched_name = self._find_target_person(target_name, float(similarity_threshold))

        if not matched_person_id:
            logger.info(f"[信使] 未找到匹配的目标用户: {target_name}")
            await send_api.text_to_stream(
                f"找不到叫「{target_name}」的人呢...(´-ω-`)",
                stream_id,
            )
            return True, True, None, None, None

        logger.info(f"[信使] 匹配到目标用户: {matched_name} (person_id: {matched_person_id})")

        # Step 3: 查找目标用户的私聊 stream_id
        target_stream_id = self._find_private_stream_id(matched_person_id)
        if not target_stream_id:
            logger.info(f"[信使] 未找到目标用户的私聊记录: {matched_name}")
            await send_api.text_to_stream(
                f"找到了{matched_name}，但是没有和ta私聊过呢，没办法转告...(´；ω；`)",
                stream_id,
            )
            return True, True, None, None, None

        # 获取 bot 对目标用户的称呼（target_name）
        target_person = Person(person_id=matched_person_id)
        bot_target_name = target_person.person_name or matched_name

        # Step 4: 构造通知文本并注入消息触发 BrainChatting 思考
        # 不直接发送 raw 通知，而是注入为 LLM 上下文，由 LLM 生成最终转告消息
        notice_text = f"[转告] {source_name}让你帮忙转告{bot_target_name}：{content}"
        await self._inject_trigger_message(target_stream_id, notice_text, source_platform)

        # 记录转告来源（供目标用户回复时解析代词）
        self._relay_source_cache[target_stream_id] = source_name

        # 回复原始用户
        await send_api.text_to_stream(
            f"已经跟{matched_name}说过啦~(≧▽≦)/",
            stream_id,
        )

        logger.info(f"[信使] 转告完成: {source_name} -> {matched_name}: {content[:30]}...")
        return True, True, None, None, None

    async def _extract_relay_info(self, text: str, stream_id: str = "") -> Tuple[str, str]:
        """使用 LLM 从用户消息中提取目标名称和转述内容

        传入 stream_id 时会获取最近的聊天记录作为上下文，
        帮助 LLM 解析代词（如 "他"、"她"）指代的具体人物。

        Returns:
            (target_name, content) 提取失败时返回 ("", "")
        """
        # 获取近期对话上下文
        context_block = ""
        # 添加最近转告来源信息（帮助解析代词）
        relay_source = self._relay_source_cache.get(stream_id, "")
        if relay_source:
            context_block += (
                f"提示：刚才{relay_source}通过你转告了消息给当前用户，如果用户说'他/她'可能指的是{relay_source}。\n\n"
            )

        if stream_id:
            try:
                recent_msgs = await message_api.get_messages_by_time_in_chat(
                    chat_id=stream_id,
                    time_range=300,  # 最近5分钟
                    limit=10,
                )
                if recent_msgs:
                    context_lines = []
                    for msg in recent_msgs[-10:]:
                        nick = msg.user_info.user_nickname if msg.user_info else "未知"
                        content_text = msg.processed_plain_text or msg.display_message or ""
                        if content_text:
                            context_lines.append(f"{nick}: {content_text}")
                    if context_lines:
                        context_block = (
                            "最近的聊天记录（供参考，用于解析代词指代）：\n" + "\n".join(context_lines) + "\n\n"
                        )
            except Exception as e:
                logger.debug(f"[信使] 获取上下文消息失败: {e}")

        prompt = (
            "你是一个信息提取助手。用户会发送一条包含转告/传话/询问请求的消息，"
            "请从中提取出：\n"
            "1. target_name: 要转告/询问的目标人名称（如果是代词如'他/她'，请根据聊天记录推断具体人名）\n"
            "2. content: 要转述或询问的具体内容\n"
            "注意：'转告/告诉/问/说/传话'都算转告请求。例如'去问问XX在干什么'中，target_name是XX，content是'在干什么'。\n"
            "如果消息明显不是转告/传话/询问请求，则 target_name 和 content 都返回空字符串。\n\n"
            f"{context_block}"
            f"用户消息：{text}\n\n"
            "请用JSON格式输出，不要输出其他内容：\n"
            "```json\n"
            "{\n"
            '    "target_name": "目标人名称",\n'
            '    "content": "要转述的内容"\n'
            "}\n"
            "```"
        )

        try:
            response, _ = await self._llm.generate_response_async(prompt)
            result = person_info_manager._extract_json_from_text(response)

            target_name = result.get("target_name", "")
            content = result.get("content", "")

            if target_name and content:
                return target_name.strip(), content.strip()

        except Exception as e:
            logger.error(f"[信使] LLM 提取转告信息失败: {e}")

        return "", ""

    def _find_target_person(self, name: str, threshold: float = 0.4) -> Tuple[Optional[str], str]:
        """双向模糊匹配目标用户

        搜索策略：
        1. 完全匹配 → 1.0
        2. 输入是候选子串（如 "甘油" in "甘油三酯"）→ 0.8
        3. 候选是输入子串 → 0.7
        4. 编辑距离相似度

        Returns:
            (person_id, matched_name) 匹配失败时返回 (None, "")
        """
        best_score = 0.0
        best_person_id = None
        best_name = ""

        # 只匹配 QQ 平台的用户
        qq_person_ids = set()
        try:
            for record in PersonInfoModel.select(PersonInfoModel.person_id).where(PersonInfoModel.platform == "qq"):
                qq_person_ids.add(record.person_id)
        except Exception as e:
            logger.error(f"[信使] 查询 QQ 平台用户失败: {e}")
            return None, ""

        # 合并 person_name_list 和 person_nickname_list 进行搜索（仅 QQ 用户）
        candidates: Dict[str, list] = {}  # person_id -> [(candidate_name, source)]
        for pid, pname in person_info_manager.person_name_list.items():
            if pid in qq_person_ids:
                candidates.setdefault(pid, []).append((pname, "person_name"))
        for pid, nick in person_info_manager.person_nickname_list.items():
            if pid in qq_person_ids:
                candidates.setdefault(pid, []).append((nick, "nickname"))

        for person_id, name_list in candidates.items():
            for candidate_name, _ in name_list:
                if not candidate_name:
                    continue

                score = self._calculate_match_score(name, candidate_name)

                if score > best_score:
                    best_score = score
                    best_person_id = person_id
                    # 优先使用 person_name 作为显示名称
                    best_name = person_info_manager.person_name_list.get(person_id, candidate_name)

        if best_score >= threshold and best_person_id:
            logger.debug(f"[信使] 最佳匹配: {best_name} (score={best_score:.2f}, id={best_person_id})")
            return best_person_id, best_name

        logger.debug(f"[信使] 未达到匹配阈值: best_score={best_score:.2f} < {threshold}")
        return None, ""

    @staticmethod
    def _calculate_match_score(input_name: str, candidate_name: str) -> float:
        """计算输入名称与候选名称的匹配分数"""
        # 完全匹配
        if input_name == candidate_name:
            return 1.0

        # 输入是候选的子串（如 "甘油" in "甘油三酯"）
        if input_name in candidate_name:
            # 子串比例越高分数越高
            ratio = len(input_name) / len(candidate_name)
            return 0.7 + 0.2 * ratio  # 0.7 ~ 0.9

        # 候选是输入的子串
        if candidate_name in input_name:
            ratio = len(candidate_name) / len(input_name)
            return 0.6 + 0.1 * ratio  # 0.6 ~ 0.7

        # 编辑距离相似度
        return calculate_string_similarity(input_name, candidate_name)

    def _find_private_stream_id(self, person_id: str) -> Optional[str]:
        """根据 person_id 查找目标用户的私聊 stream_id

        从 PersonInfo DB 获取 platform 和 user_id,
        计算 md5(platform_userId_private) 得到 stream_id,
        验证该 stream 存在于 ChatManager
        """
        try:
            record = PersonInfoModel.get_or_none(PersonInfoModel.person_id == person_id)
            if not record:
                logger.warning(f"[信使] PersonInfo 中未找到 person_id: {person_id}")
                return None

            platform = record.platform
            user_id = record.user_id

            if not platform or not user_id:
                logger.warning(f"[信使] PersonInfo 中缺少 platform 或 user_id: {person_id}")
                return None

            # 计算私聊 stream_id: md5(platform_userId_private)
            key = f"{platform}_{user_id}_private"
            stream_id = hashlib.md5(key.encode()).hexdigest()

            # 验证该 stream 是否存在于 ChatManager
            chat_manager = get_chat_manager()
            stream = chat_manager.get_stream(stream_id)
            if not stream:
                logger.debug(f"[信使] ChatManager 中未找到私聊流: {stream_id} (key={key})")
                return None

            return stream_id

        except Exception as e:
            logger.error(f"[信使] 查找私聊 stream_id 失败: {e}")
            return None

    async def _inject_trigger_message(self, target_stream_id: str, notice_text: str, platform: str):
        """直接用 generator_api 生成转告回复并发送到目标私聊

        不存储假的用户消息（否则 LLM 会误以为是目标用户在说话），
        而是将转告内容通过 extra_info 传给回复器，让 bot 自然地把消息转达给对方。
        """

        async def _do_inject():
            try:
                await asyncio.sleep(0.3)

                from src.chat.message_receive.message import MessageRecv

                # 获取目标 stream
                chat_manager = get_chat_manager()
                target_stream = chat_manager.get_stream(target_stream_id)
                if not target_stream:
                    logger.error(f"[信使] 注入消息失败: 找不到目标 stream: {target_stream_id}")
                    return

                # 确保 chat_stream 有 context（generator_api 需要）
                if not target_stream.context:
                    msg_time = time.time()
                    dummy_data = {
                        "message_info": {
                            "platform": platform or target_stream.platform,
                            "message_id": f"messenger_ctx_{int(msg_time * 1000)}",
                            "time": msg_time,
                            "group_info": None,
                            "user_info": {
                                "platform": platform or target_stream.platform,
                                "user_id": target_stream.user_info.user_id,
                                "user_nickname": target_stream.user_info.user_nickname,
                                "user_cardname": "",
                            },
                            "additional_config": {},
                            "format_info": {"content_format": "", "accept_format": ""},
                            "template_info": {"template_items": {}},
                        },
                        "raw_message": "",
                        "processed_plain_text": "",
                    }
                    ctx_msg = MessageRecv(dummy_data)
                    target_stream.set_context(ctx_msg)

                # 直接生成回复，通过 extra_info 传递转告内容
                from src.plugin_system.apis import generator_api

                success, llm_response = await generator_api.generate_reply(
                    chat_stream=target_stream,
                    extra_info=f"你现在需要帮忙转告一条消息给对方。转告内容如下：{notice_text}\n请你自然地将这条转告消息传达给对方。",
                    reply_reason="帮忙转告消息",
                    request_type="messenger.relay_reply",
                )

                if success and llm_response and llm_response.reply_set:
                    for reply_item in llm_response.reply_set.reply_data:
                        if reply_item.content and reply_item.content_type.value == "text":
                            await send_api.text_to_stream(
                                text=reply_item.content,
                                stream_id=target_stream_id,
                            )
                    logger.info(f"[信使] 已生成并发送转告回复到 stream: {target_stream_id}")
                else:
                    logger.warning(f"[信使] 回复生成失败或为空，stream: {target_stream_id}")

            except Exception as e:
                logger.error(f"[信使] 注入触发消息失败: {e}")
                import traceback

                traceback.print_exc()

        # 在独立任务中执行，不阻塞当前事件处理器
        asyncio.create_task(_do_inject())
        logger.info(f"[信使] 已调度注入任务到 stream: {target_stream_id}")
