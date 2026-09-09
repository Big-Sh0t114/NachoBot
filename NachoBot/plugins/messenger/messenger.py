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
import tomllib
from pathlib import Path
from typing import Optional, Tuple, Dict

from ncnk_message import UserInfo
from src.common.logger import get_logger
from src.common.database.database_model import PersonInfo as PersonInfoModel
from src.person_info.person_info import (
    person_info_manager,
    calculate_string_similarity,
    Person,
)
from src.chat.message_receive.chat_stream import get_chat_manager
from src.plugin_system.apis import send_api, message_api
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.base_command import BaseCommand

logger = get_logger("messenger")

# 管理员直达指令: #convey_<QQ号> <内容>
CONVEY_PATTERN = re.compile(r"^#convey_(\d+)\s+(.+)", re.DOTALL)
TARGET_QQ_PATTERN = re.compile(r"^(?:qq(?:号|号码)?\s*[:：]?\s*)?(\d{5,12})$", re.IGNORECASE)


def _configured_owner_qq() -> Optional[str]:
    """读取主人认证插件中的主人QQ号；读取失败时安全返回空值。"""
    raw_owner_qq = None
    try:
        from src.plugin_system.core.component_registry import component_registry

        plugin_config = component_registry.get_plugin_config("owner_auth_plugin") or {}
        owner_config = plugin_config.get("owner_auth", {})
        raw_owner_qq = owner_config.get("owner_qq") if isinstance(owner_config, dict) else None
    except Exception as e:
        logger.debug(f"[信使] 从插件注册表读取主人QQ失败，将读取配置文件: {e}")

    # 插件注册表尚未完成初始化时，直接读取同一份主人认证配置，避免权限判断误拒绝主人。
    if not raw_owner_qq:
        try:
            config_path = Path(__file__).resolve().parent.parent / "owner_auth_plugin" / "config.toml"
            with config_path.open("rb") as config_file:
                owner_config = tomllib.load(config_file).get("owner_auth", {})
            raw_owner_qq = owner_config.get("owner_qq") if isinstance(owner_config, dict) else None
        except Exception as e:
            logger.warning(f"[信使] 读取主人QQ配置失败，拒绝主动私聊: {e}")

    normalized = re.sub(r"\D", "", str(raw_owner_qq or ""))
    return normalized or None


def _is_owner_user(user_id: object, platform: object) -> bool:
    """主动私聊的权限判断：只允许 QQ 平台且QQ号等于主人配置。"""
    normalized_platform = str(platform or "").strip().lower().split("-", 1)[0]
    normalized_user_id = str(user_id or "").strip()
    owner_qq = _configured_owner_qq()
    return normalized_platform == "qq" and bool(owner_qq) and normalized_user_id == owner_qq


def _extract_target_qq(target_name: str) -> Optional[str]:
    """支持直接填写QQ号、QQ:123456或QQ号 123456。"""
    match = TARGET_QQ_PATTERN.fullmatch(target_name.strip())
    return match.group(1) if match else None


class MessengerRelayAction(BaseAction):
    """转告消息动作 - 帮忙向目标用户转述消息

    当 planner LLM 从对话上下文中识别到用户请求转告/传话时，选择此动作。
    """

    # 激活设置 - 始终出现在 planner 动作池中
    activation_type = ActionActivationType.ALWAYS
    parallel_action = False

    # 动作基本信息
    action_name = "messenger_relay"
    action_description = (
        "仅主人可用：主动向指定QQ或已认识的用户发起私聊。当主人要求私聊、询问、告诉、转告某人时，必须选择此动作；其他用户不得使用"
    )

    # 动作参数 - planner LLM 在选择此动作时填写
    action_parameters = {
        "target_name": "要转告的目标人名称",
        "content": "要转述的具体内容",
    }

    # 声明此动作自带回复语义，阻止 heart_flow 自动强加 reply 动作
    associated_types = ["reply"]

    # 使用条件 - 指导 planner LLM 何时选择此动作
    action_require = [
        "仅当发言者是已验证的主人怀望（QQ: 3143873450）时，才允许选择此动作",
        "主人让你主动私聊、帮忙联系、询问、转告、传话、带话给某个人时，必须选择此动作",
        "触发示例：'私聊XX说你好'、'问问XX在干嘛'、'告诉XX...'、'跟XX说...'、'帮我问XX...'、'转告XX...'",
        "目标可以是已认识的用户名称，也可以是QQ号；只要主人明确要求，就不要先征求主人二次确认",
        "仅在QQ平台生效",
        "非主人提出相同请求时，不得执行此动作，只能说明主动私聊功能仅主人可用",
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

        # 在任何目标解析和发送前校验，防止非主人通过 planner 间接触发外部联系。
        if not _is_owner_user(self.user_id, self.platform):
            logger.warning(f"[信使] 拒绝非主人主动私聊: user_id={self.user_id}, platform={self.platform}")
            await self.send_text("主动私聊功能仅主人可用哦~", storage_message=False)
            return True, "主动私聊功能仅主人可用"

        logger.info(f"[信使] 执行转告: 目标={target_name}, 内容={content[:30]}...")

        # 获取发送者信息
        source_name = self.user_nickname or "未知用户"
        if self.platform and self.user_id:
            try:
                source_person = Person(platform=self.platform, user_id=self.user_id)
                source_name = source_person.person_name or source_name
            except Exception:
                logger.debug(f"[信使] 无法获取发送者 Person 信息，使用昵称: {source_name}")

        # Step 1: 优先支持直接QQ号；名称模式继续匹配已认识的QQ用户。
        target_qq = _extract_target_qq(target_name)
        matched_person_id: Optional[str] = None
        matched_name = target_name
        target_record = None

        if target_qq:
            target_record = PersonInfoModel.get_or_none(
                (PersonInfoModel.platform == "qq") & (PersonInfoModel.user_id == target_qq)
            )
            if target_record:
                matched_person_id = target_record.person_id
                matched_name = target_record.person_name or target_record.nickname or target_record.user_nickname or target_qq
        else:
            similarity_threshold = self.get_config("components.similarity_threshold", 0.4)
            matched_person_id, matched_name = self._find_target_person(target_name, float(similarity_threshold))

            if matched_person_id:
                target_record = PersonInfoModel.get_or_none(PersonInfoModel.person_id == matched_person_id)
                if target_record and target_record.user_id:
                    target_qq = str(target_record.user_id)

        if not target_qq:
            logger.info(f"[信使] 未找到可发送的目标QQ: {target_name}")
            await self.send_text(
                f"找不到叫「{target_name}」的已知用户呢；如果对方还没和我聊过，请直接提供对方QQ号~",
                storage_message=False,
            )
            return True, f"未找到目标用户或QQ号: {target_name}"

        logger.info(f"[信使] 匹配到目标用户: {matched_name} (QQ: {target_qq}, person_id: {matched_person_id})")

        # Step 1.5: 检查目标用户是否在免打扰列表中
        mute_list = self.get_config("components.mute_user_list", [])
        mute_qqs = {str(user_id) for user_id in mute_list} if isinstance(mute_list, (list, tuple, set)) else set()
        if str(target_qq) in mute_qqs:
            logger.info(f"[信使] 目标用户在免打扰列表中，取消转告: {matched_name}")
            await self.send_text("此用户关闭了转告功能哦~", storage_message=False)
            return True, f"目标用户已关闭转告: {matched_name}"

        # Step 2: 查找目标用户的私聊 stream_id
        target_stream_id = self._find_private_stream_id(matched_person_id) if matched_person_id else None
        if not target_stream_id:
            # 主人可以主动联系尚未建立私聊记录的用户，先注册目标私聊流再发送。
            target_stream_id = await self._ensure_private_stream(target_qq, matched_name)
        if not target_stream_id:
            logger.error(f"[信使] 无法创建目标用户的私聊流: {matched_name} (QQ: {target_qq})")
            await self.send_text("目标私聊通道创建失败，当前无法完成主动私聊。", storage_message=False)
            return True, f"无法创建私聊流: {matched_name}"

        # 获取 bot 对目标用户的称呼
        if matched_person_id:
            target_person = Person(person_id=matched_person_id)
            bot_target_name = target_person.person_name or matched_name
        else:
            bot_target_name = matched_name or target_qq

        # Step 3: 构造通知文本并注入消息触发 LLM 思考
        await self._inject_trigger_message(
            target_stream_id, source_name, bot_target_name, content, self.platform or "qq"
        )

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

    async def _wait_for_confirmation_reply(self, confirm_start: float, timeout: int) -> Optional[str]:
        """等待传话发起人的确认消息，忽略群内其他成员的回复。"""
        source_user_id = str(self.user_id or "").strip()
        if not source_user_id:
            logger.error("[信使] 无法确定传话发起人，拒绝接受确认回复")
            return None

        wait_start = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - wait_start
            if elapsed > timeout:
                logger.info(f"[信使] 等待确认超时 ({timeout}s)")
                return None

            # 只查询发起此动作的用户，群内其他成员和机器人消息均不能确认转告。
            user_messages = message_api.get_messages_by_time_in_chat_for_users(
                self.chat_id,
                confirm_start,
                time.time(),
                [source_user_id],
                limit=1,
                limit_mode="latest",
            )
            if user_messages:
                reply_text = (user_messages[0].processed_plain_text or "").strip()
                logger.info(f"[信使] 收到传话发起人确认回复: {reply_text}")
                return reply_text

            await asyncio.sleep(1.0)

    @staticmethod
    def _extract_confirmation_intent(text: str) -> Optional[str]:
        """解析用户确认回复的意图

        使用前缀匹配，容忍用户输入多余字符（如"是d"、"好的"、"确认吧"）。
        否定词优先检测，避免"不是"被"是"误匹配。
        """
        REFUSE_PATTERN = r"^(不是|不对|不行|不要|不用|错了|取消|算了|别|停|否|no\b|n\b)"
        CONFIRM_PATTERN = r"^(是|对|确认|好|嗯|ok\b|yes\b|y\b|true\b|确定|没错|可以|行)"

        text = text.strip().lower()
        if not text:
            return None
        # 否定优先：避免 "不是" 被 "是" 前缀误匹配
        if re.match(REFUSE_PATTERN, text, re.IGNORECASE):
            return "refuse"
        if re.match(CONFIRM_PATTERN, text, re.IGNORECASE):
            return "confirm"
        return None

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

    async def _ensure_private_stream(self, target_qq: str, target_name: str) -> Optional[str]:
        """为主人主动私聊创建目标流，允许目标用户此前从未和机器人聊过。"""
        try:
            chat_manager = get_chat_manager()
            stream_id = chat_manager.get_stream_id("qq", str(target_qq), is_group=False)
            if chat_manager.get_stream(stream_id):
                return stream_id

            target_user = UserInfo(
                platform="qq",
                user_id=str(target_qq),
                user_nickname=target_name or str(target_qq),
                user_cardname="",
            )
            await chat_manager.get_or_create_stream("qq", target_user, None)
            if chat_manager.get_stream(stream_id):
                logger.info(f"[信使] 已创建主动私聊流: {target_name} (QQ: {target_qq})")
                return stream_id
        except Exception as e:
            logger.error(f"[信使] 创建主动私聊流失败: target={target_qq}, error={e}")
        return None

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

    async def _inject_trigger_message(
        self, target_stream_id: str, source_name: str, bot_target_name: str, content: str, platform: str
    ):
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

                notice_text = f"[转告] {source_name}让你帮忙转告{bot_target_name}：{content}"

                success, llm_response = await generator_api.generate_reply(
                    chat_stream=target_stream,
                    extra_info=f"你现在需要帮忙转告一条消息给对方。转告内容如下：{notice_text}\n请你自然地将这条转告消息传达给对方。",
                    reply_reason="帮忙转告消息",
                    request_type="messenger.relay_reply",
                )

                if success and llm_response and llm_response.reply_set:
                    await send_api.custom_reply_set_to_stream(
                        reply_set=llm_response.reply_set,
                        stream_id=target_stream_id,
                        typing=True,
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

        # 获取来源平台与用户ID
        source_user_id = ""
        source_platform = ""
        if self.message and self.message.message_info:
            source_platform = getattr(self.message.message_info, "platform", "") or ""
            user_info = getattr(self.message.message_info, "user_info", None)
            if user_info:
                source_user_id = str(getattr(user_info, "user_id", ""))

        if not _is_owner_user(source_user_id, source_platform):
            logger.warning(f"[信使] #convey 拒绝非主人主动私聊: {source_user_id}")
            await self.send_text("主动私聊功能仅主人可用哦~", storage_message=False)
            return True, "主动私聊功能仅主人可用", True

        # 获取来源 stream_id
        stream_id = ""
        if self.message and self.message.chat_stream:
            stream_id = self.message.chat_stream.stream_id

        # 直接通过 QQ号 计算私聊 stream_id
        key = f"qq_{target_qq}_private"
        target_stream_id = hashlib.md5(key.encode()).hexdigest()

        # 获取或创建目标私聊流，允许主人主动联系此前未聊过的QQ。
        chat_manager = get_chat_manager()
        target_stream = chat_manager.get_stream(target_stream_id)
        if not target_stream:
            try:
                target_user = UserInfo(
                    platform="qq",
                    user_id=str(target_qq),
                    user_nickname=str(target_qq),
                    user_cardname="",
                )
                await chat_manager.get_or_create_stream("qq", target_user, None)
                target_stream = chat_manager.get_stream(target_stream_id)
            except Exception as e:
                logger.error(f"[信使] #convey 创建目标私聊流失败: QQ={target_qq}, error={e}")

        if not target_stream:
            logger.info(f"[信使] #convey 无法创建目标私聊: QQ={target_qq}")
            if stream_id:
                await send_api.text_to_stream(
                    f"目标 QQ:{target_qq} 的私聊通道创建失败，当前无法完成。",
                    stream_id,
                    storage_message=False,
                )
            return True, f"无法创建目标私聊: QQ={target_qq}", True

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
                    await send_api.custom_reply_set_to_stream(
                        reply_set=llm_response.reply_set,
                        stream_id=target_stream_id,
                        typing=True,
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
