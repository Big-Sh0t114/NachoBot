"""
信使 (Messenger) 插件 - 核心逻辑

Action 模式：加入 planner 动作池，由 LLM 在规划阶段根据完整的聊天上下文
判断是否需要执行转告。planner 选择时直接填入 target_name 和 content 参数。

管理员 #convey 指令：独立的 Command 组件，不受 Action 改造影响。
"""

import asyncio
import re
import hashlib
import time
from typing import Optional, Tuple, Dict

from src.common.logger import get_logger
from src.common.database.database_model import PersonInfo as PersonInfoModel
from src.person_info.person_info import (
    person_info_manager,
    calculate_string_similarity,
    Person,
)
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.advanced.advanced_manager import advanced_manager
from src.plugin_system.apis import send_api
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.base_command import BaseCommand

logger = get_logger("messenger")

# 管理员直达指令: #convey_<QQ号> <内容>
CONVEY_PATTERN = re.compile(r"^#convey_(\d+)\s+(.+)", re.DOTALL)


class MessengerRelayAction(BaseAction):
    """转告消息动作 - 帮忙向目标用户转述消息

    当 planner LLM 从对话上下文中识别到用户请求转告/传话时，选择此动作。
    """

    # 激活设置 - 始终出现在 planner 动作池中
    activation_type = ActionActivationType.ALWAYS
    parallel_action = False

    # 动作基本信息
    action_name = "messenger_relay"
    action_description = "帮忙向指定用户转告/传话/询问，将消息传达到对方的私聊。当用户让你去问某人、告诉某人、转告某人时，必须选择此动作"

    # 动作参数 - planner LLM 在选择此动作时填写
    action_parameters = {
        "target_name": "要转告的目标人名称",
        "content": "要转述的具体内容",
    }

    # 使用条件 - 指导 planner LLM 何时选择此动作
    action_require = [
        "当用户让你帮忙联系、询问、转告、传话、带话给某个人时，必须选择此动作",
        "触发示例：'问问XX在干嘛'、'告诉XX...'、'跟XX说...'、'帮我问XX...'、'去问问XX...'、'转告XX...'",
        "只要用户指定了一个人名并要求你去联系/询问/传达，就应该选择此动作而不是reply",
        "仅在QQ平台生效",
        "注意：不要把用户自己在聊天中提到和某人说话的描述误认为转告请求",
    ]

    # 记录每个 target_stream_id 最近的转告来源用户名（用于解析代词）
    _relay_source_cache: Dict[str, str] = {}

    async def execute(self) -> Tuple[bool, str]:
        """执行转告动作"""
        target_name = (self.action_data.get("target_name") or "").strip()
        content = (self.action_data.get("content") or "").strip()

        if not target_name or not content:
            logger.info("[信使] planner 未提供有效的目标名称或内容，跳过")
            return False, "缺少转告目标或内容"

        # 仅对 QQ 平台生效
        if self.platform != "qq":
            logger.debug(f"[信使] 非 QQ 平台 ({self.platform})，跳过")
            return False, "转告功能仅在QQ平台可用"

        logger.info(f"[信使] 执行转告: 目标={target_name}, 内容={content[:30]}...")

        # 获取发送者信息
        source_name = self.user_nickname or "未知用户"
        if self.platform and self.user_id:
            try:
                source_person = Person(platform=self.platform, user_id=self.user_id)
                source_name = source_person.person_name or source_name
            except Exception:
                logger.debug(f"[信使] 无法获取发送者 Person 信息，使用昵称: {source_name}")

        # Step 1: 双向模糊匹配目标用户
        similarity_threshold = self.get_config("components.similarity_threshold", 0.4)
        matched_person_id, matched_name = self._find_target_person(target_name, float(similarity_threshold))

        if not matched_person_id:
            logger.info(f"[信使] 未找到匹配的目标用户: {target_name}")
            await self.send_text(f"找不到叫「{target_name}」的人呢...(´-ω-`)")
            return True, f"未找到目标用户: {target_name}"

        logger.info(f"[信使] 匹配到目标用户: {matched_name} (person_id: {matched_person_id})")

        # Step 2: 查找目标用户的私聊 stream_id
        target_stream_id = self._find_private_stream_id(matched_person_id)
        if not target_stream_id:
            logger.info(f"[信使] 未找到目标用户的私聊记录: {matched_name}")
            await self.send_text(f"找到了{matched_name}，但是没有和ta私聊过呢，没办法转告...(´；ω；`)")
            return True, f"无私聊记录: {matched_name}"

        # 获取 bot 对目标用户的称呼
        target_person = Person(person_id=matched_person_id)
        bot_target_name = target_person.person_name or matched_name

        # Step 3: 构造通知文本并注入消息触发 LLM 思考
        notice_text = f"[转告] {source_name}让你帮忙转告{bot_target_name}：{content}"
        await self._inject_trigger_message(target_stream_id, notice_text, self.platform or "qq")

        # 记录转告来源（供目标用户回复时解析代词）
        MessengerRelayAction._relay_source_cache[target_stream_id] = source_name

        # 回复原始用户
        await self.send_text(f"已经跟{matched_name}说过啦~(≧▽≦)/")

        # 记录动作信息
        await self.store_action_info(
            action_build_into_prompt=True,
            action_prompt_display=f"你帮{source_name}转告了消息给{matched_name}",
            action_done=True,
        )

        logger.info(f"[信使] 转告完成: {source_name} -> {matched_name}: {content[:30]}...")
        return True, f"已转告给{matched_name}"

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

        # 在独立任务中执行，不阻塞当前动作
        asyncio.create_task(_do_inject())
        logger.info(f"[信使] 已调度注入任务到 stream: {target_stream_id}")


class ConveyCommand(BaseCommand):
    """管理员 #convey 指令 - 以 bot 自己的语气发送消息到目标私聊

    格式: #convey_<QQ号> <内容>
    """

    command_name: str = "convey"
    command_description: str = "管理员向目标用户私聊发送消息"
    command_pattern: str = r"(?P<convey>^#convey_\d+\s+.+)"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        """处理 #convey 指令"""
        text = self.matched_groups.get("convey", "") or (self.message.processed_plain_text or "")
        convey_match = CONVEY_PATTERN.match(text)
        if not convey_match:
            return False, "指令格式错误", True

        target_qq = convey_match.group(1)
        convey_content = convey_match.group(2).strip()

        # 权限检查
        source_user_id = ""
        if self.message and self.message.message_info:
            user_info = getattr(self.message.message_info, "user_info", None)
            if user_info:
                source_user_id = str(getattr(user_info, "user_id", ""))

        if not advanced_manager.is_allowed(source_user_id):
            logger.debug(f"[信使] #convey 权限不足: {source_user_id}")
            return True, None, True

        # 获取来源 stream_id
        stream_id = ""
        if self.message and self.message.chat_stream:
            stream_id = self.message.chat_stream.stream_id

        # 获取来源平台
        source_platform = ""
        if self.message and self.message.message_info:
            source_platform = getattr(self.message.message_info, "platform", "") or ""

        # 直接通过 QQ号 计算私聊 stream_id
        key = f"qq_{target_qq}_private"
        target_stream_id = hashlib.md5(key.encode()).hexdigest()

        # 验证 stream 存在
        chat_manager = get_chat_manager()
        target_stream = chat_manager.get_stream(target_stream_id)
        if not target_stream:
            logger.info(f"[信使] #convey 未找到目标私聊: QQ={target_qq}")
            if stream_id:
                await send_api.text_to_stream(
                    f"没有和 QQ:{target_qq} 私聊过，无法发送...(´-ω-`)",
                    stream_id,
                )
            return True, f"未找到目标私聊: QQ={target_qq}", True

        # 注入消息 —— 以 bot 自己的思考形式，不带转告标识
        thought_text = (
            f"你现在想主动跟对方说一句话或者问他们一个问题。"
            f"你想说/问的内容是：'{convey_content}'。"
            f"请用你自己的语气自然地将这句话发送给对方。"
        )
        await self._inject_convey_message(target_stream_id, thought_text, source_platform)

        # 回复管理员
        target_name = target_stream.user_info.user_nickname if target_stream.user_info else target_qq
        if stream_id:
            await send_api.text_to_stream(
                f"已向{target_name}发送消息~",
                stream_id,
            )

        logger.info(f"[信使] #convey 完成: QQ={target_qq}, 内容: {convey_content[:30]}...")
        return True, f"已向{target_name}发送消息", True

    async def _inject_convey_message(self, target_stream_id: str, thought_text: str, platform: str):
        """以 bot 自己的思考形式注入消息到目标私聊

        extra_info 不包含转告标识，LLM 会认为这是自己的想法而自然表达。
        """

        async def _do_convey():
            try:
                await asyncio.sleep(0.3)

                from src.chat.message_receive.message import MessageRecv

                chat_manager = get_chat_manager()
                target_stream = chat_manager.get_stream(target_stream_id)
                if not target_stream:
                    logger.error(f"[信使] convey 注入失败: 找不到目标 stream: {target_stream_id}")
                    return

                # 确保 context 存在
                if not target_stream.context:
                    msg_time = time.time()
                    dummy_data = {
                        "message_info": {
                            "platform": platform or target_stream.platform,
                            "message_id": f"convey_ctx_{int(msg_time * 1000)}",
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

                from src.plugin_system.apis import generator_api

                success, llm_response = await generator_api.generate_reply(
                    chat_stream=target_stream,
                    extra_info=thought_text,
                    reply_reason="主动发起对话",
                    request_type="messenger.convey_reply",
                )

                if success and llm_response and llm_response.reply_set:
                    for reply_item in llm_response.reply_set.reply_data:
                        if reply_item.content and reply_item.content_type.value == "text":
                            await send_api.text_to_stream(
                                text=reply_item.content,
                                stream_id=target_stream_id,
                            )
                    logger.info(f"[信使] convey 回复已发送到 stream: {target_stream_id}")
                else:
                    logger.warning(f"[信使] convey 回复生成失败或为空，stream: {target_stream_id}")

            except Exception as e:
                logger.error(f"[信使] convey 注入失败: {e}")
                import traceback

                traceback.print_exc()

        asyncio.create_task(_do_convey())
        logger.info(f"[信使] 已调度 convey 任务到 stream: {target_stream_id}")
