import asyncio
import json
import time
import traceback
import random
from typing import List, Optional, Dict, Any, Tuple, TYPE_CHECKING
from rich.traceback import install

from src.config.config import global_config
from src.common.logger import get_logger
from src.common.data_models.info_data_model import ActionPlannerInfo
from src.common.data_models.message_data_model import ReplyContentType
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.utils.prompt_builder import global_prompt_manager
from src.chat.utils.timer_calculator import Timer
from src.chat.planner_actions.planner import ActionPlanner
from src.chat.planner_actions.action_modifier import ActionModifier
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.heart_flow.hfc_utils import CycleDetail
from src.chat.heart_flow.hfc_utils import send_typing, stop_typing
from src.chat.express.expression_learner import expression_learner_manager
from src.chat.frequency_control.frequency_control import frequency_control_manager
from src.chat.keyword_cache import promise_cache_manager
from src.person_info.person_info import Person
from src.plugin_system.base.component_types import EventType, ActionInfo
from src.chat.injection.injection_manager import injection_manager
from src.plugin_system.core import events_manager
from src.plugin_system.apis import generator_api, send_api, message_api, database_api
from src.mais4u.mai_think import mai_thinking_manager
from src.mais4u.s4u_config import s4u_config
from src.chat.utils.chat_message_builder import (
    build_readable_messages_with_id,
    get_raw_msg_before_timestamp_with_chat,
    get_stepped_limit,
)
from src.memory_system.chat_history_summarizer import ChatHistorySummarizer
from src.chat.heart_flow.relation_scanner import RelationScanner

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages
    from src.common.data_models.message_data_model import ReplySetModel


ERROR_LOOP_INFO = {
    "loop_plan_info": {
        "action_result": {
            "action_type": "error",
            "action_data": {},
            "reasoning": "循环处理失败",
        },
    },
    "loop_action_info": {
        "action_taken": False,
        "reply_text": "",
        "command": "",
        "taken_time": time.time(),
    },
}


install(extra_lines=3)

# 注释：原来的动作修改超时常量已移除，因为改为顺序执行

logger = get_logger("hfc")  # Logger Name Changed


class HeartFChatting:
    """
    管理一个连续的Focus Chat循环
    用于在特定聊天流中生成回复。
    其生命周期现在由其关联的 SubHeartflow 的 FOCUSED 状态控制。
    """

    def __init__(self, chat_id: str):
        """
        HeartFChatting 初始化函数

        参数:
            chat_id: 聊天流唯一标识符(如stream_id)
            on_stop_focus_chat: 当收到stop_focus_chat命令时调用的回调函数
            performance_version: 性能记录版本号，用于区分不同启动版本
        """
        # 基础属性
        self.stream_id: str = chat_id  # 聊天流ID
        self.chat_stream: ChatStream = get_chat_manager().get_stream(self.stream_id)  # type: ignore
        if not self.chat_stream:
            raise ValueError(f"无法找到聊天流: {self.stream_id}")
        self.log_prefix = f"[{get_chat_manager().get_stream_name(self.stream_id) or self.stream_id}]"

        self.expression_learner = expression_learner_manager.get_expression_learner(self.stream_id)

        self.action_manager = ActionManager()
        self.action_planner = ActionPlanner(chat_id=self.stream_id, action_manager=self.action_manager)
        self.action_modifier = ActionModifier(action_manager=self.action_manager, chat_id=self.stream_id)

        # 循环控制内部状态
        self.running: bool = False
        self._loop_task: Optional[asyncio.Task] = None  # 主循环任务

        # 添加循环信息管理相关的属性
        self.history_loop: List[CycleDetail] = []
        self._cycle_counter = 0
        self._current_cycle_detail: CycleDetail = None  # type: ignore

        self.last_read_time = time.time() - 2

        self.talk_threshold = global_config.chat.get_talk_value_for_chat(self.stream_id)
        # 跟踪连续 no_reply 次数，用于动态调整阈值
        self.consecutive_no_reply_count = 0

        # 聊天内容概括器与关系扫描器
        self.chat_history_summarizer = ChatHistorySummarizer(chat_id=self.stream_id)
        self.relation_scanner = RelationScanner(chat_id=self.stream_id)

        self.no_reply_until_call = False

        # 用户屏蔽列表: {user_id: 过期时间戳}
        self.blocked_users: Dict[str, float] = {}

    def _is_user_blocked(self, user_id: str) -> bool:
        """检查用户是否在屏蔽列表中且未过期"""
        if user_id not in self.blocked_users:
            return False
        if time.time() > self.blocked_users[user_id]:
            del self.blocked_users[user_id]
            logger.info(f"{self.log_prefix} 用户 {user_id} 的屏蔽已到期，已自动解除")
            return False
        return True

    def _filter_blocked_users(self, messages: List["DatabaseMessages"], caller: str = "") -> List["DatabaseMessages"]:
        """从消息列表中过滤掉被屏蔽用户的消息"""
        if not self.blocked_users:
            return messages
        # 清理过期条目
        now = time.time()
        expired = [uid for uid, exp in self.blocked_users.items() if now > exp]
        for uid in expired:
            del self.blocked_users[uid]
            logger.info(f"{self.log_prefix} 用户 {uid} 的屏蔽已到期，已自动解除")
        if not self.blocked_users:
            return messages
        logger.debug(
            f"{self.log_prefix} [block_filter][{caller}] 屏蔽列表: {self.blocked_users}, 待过滤消息数: {len(messages)}"
        )
        original_count = len(messages)
        filtered = [msg for msg in messages if not self._is_user_blocked(str(msg.user_info.user_id))]
        if original_count != len(filtered):
            logger.debug(f"{self.log_prefix} [block_filter][{caller}] 已过滤 {original_count - len(filtered)} 条")
        return filtered

    def _resolve_user_id_by_nickname(self, nickname: str) -> Optional[str]:
        """通过昵称从最近消息中查找用户的真实QQ号"""
        try:
            recent_messages = get_raw_msg_before_timestamp_with_chat(
                chat_id=self.stream_id,
                timestamp=time.time(),
                limit=100,
            )
            for msg in recent_messages:
                user_nickname = msg.user_info.user_nickname or ""
                user_cardname = msg.user_info.user_cardname or ""
                user_id = str(msg.user_info.user_id)
                if nickname == user_nickname or nickname == user_cardname:
                    return user_id
        except Exception as e:
            logger.error(f"{self.log_prefix} 通过昵称解析QQ号失败: {e}")
        return None

    async def start(self):
        """检查是否需要启动主循环，如果未激活则启动。"""

        # 如果循环已经激活，直接返回
        if self.running:
            logger.debug(f"{self.log_prefix} HeartFChatting 已激活，无需重复启动")
            return

        try:
            # 标记为活动状态，防止重复启动
            self.running = True

            self._loop_task = asyncio.create_task(self._main_chat_loop())
            self._loop_task.add_done_callback(self._handle_loop_completion)

            # 启动聊天内容概括器的后台定期检查循环
            if getattr(self.chat_stream, "platform", "") not in ("bilibili", "bilibili.live"):
                await self.chat_history_summarizer.start()

            # 暂时停用群聊关系扫描
            # await self.relation_scanner.start()
            if not getattr(self.chat_stream, "group_info", None):
                await self.relation_scanner.start()

            logger.info(f"{self.log_prefix} HeartFChatting 启动完成")

        except Exception as e:
            # 启动失败时重置状态
            self.running = False
            self._loop_task = None
            logger.error(f"{self.log_prefix} HeartFChatting 启动失败: {e}")
            raise

    def _handle_loop_completion(self, task: asyncio.Task):
        """当 _hfc_loop 任务完成时执行的回调。"""
        try:
            if exception := task.exception():
                logger.error(f"{self.log_prefix} HeartFChatting: 脱离了聊天(异常): {exception}")
                logger.error(traceback.format_exc())  # Log full traceback for exceptions
            else:
                logger.info(f"{self.log_prefix} HeartFChatting: 脱离了聊天 (外部停止)")
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} HeartFChatting: 结束了聊天")

    def start_cycle(self) -> Tuple[Dict[str, float], str]:
        self._cycle_counter += 1
        self._current_cycle_detail = CycleDetail(self._cycle_counter)
        self._current_cycle_detail.thinking_id = f"tid{str(round(time.time(), 2))}"
        cycle_timers = {}
        return cycle_timers, self._current_cycle_detail.thinking_id

    def end_cycle(self, loop_info, cycle_timers):
        self._current_cycle_detail.set_loop_info(loop_info)
        self.history_loop.append(self._current_cycle_detail)
        self._current_cycle_detail.timers = cycle_timers
        self._current_cycle_detail.end_time = time.time()

    def print_cycle_info(self, cycle_timers):
        # 记录循环信息和计时器结果
        timer_strings = []
        for name, elapsed in cycle_timers.items():
            formatted_time = f"{elapsed * 1000:.2f}毫秒" if elapsed < 1 else f"{elapsed:.2f}秒"
            timer_strings.append(f"{name}: {formatted_time}")

        logger.info(
            f"{self.log_prefix} 第{self._current_cycle_detail.cycle_id}次思考,"
            f"耗时: {self._current_cycle_detail.end_time - self._current_cycle_detail.start_time:.1f}秒"  # type: ignore
            + (f"\n详情: {'; '.join(timer_strings)}" if timer_strings else "")
        )

    async def _loopbody(self):  # sourcery skip: hoist-if-from-if
        recent_messages_list = message_api.get_messages_by_time_in_chat(
            chat_id=self.stream_id,
            start_time=self.last_read_time,
            end_time=time.time(),
            limit=20,
            limit_mode="latest",
            filter_mai=True,
            filter_command=True,
        )

        if len(recent_messages_list) >= 1:
            # 先推进读取时间戳，防止被屏蔽用户的消息不断累积占用循环
            self.last_read_time = time.time()

            # 再过滤被屏蔽用户的消息
            recent_messages_list = self._filter_blocked_users(recent_messages_list, caller="loopbody")

            # 过滤后无消息则跳过本轮思考
            if len(recent_messages_list) == 0:
                await asyncio.sleep(0.2)
                return True

            # !处理no_reply_until_call逻辑
            if self.no_reply_until_call:
                for message in recent_messages_list:
                    if (
                        message.is_mentioned
                        or message.is_at
                        or len(recent_messages_list) >= 8
                        or time.time() - self.last_read_time > 600
                    ):
                        self.no_reply_until_call = False
                        break
                # 没有提到，继续保持沉默
                if self.no_reply_until_call:
                    # logger.info(f"{self.log_prefix} 没有提到，继续保持沉默")
                    await asyncio.sleep(1)
                    return True

            # !此处使at或者提及必定回复
            mentioned_message = None
            for message in recent_messages_list:
                if (message.is_mentioned or message.is_at) and global_config.chat.mentioned_bot_reply:
                    mentioned_message = message

            # *控制频率用
            if mentioned_message:
                await self._observe(recent_messages_list=recent_messages_list, force_reply_message=mentioned_message)
            elif (
                random.random()
                < self.talk_threshold
                * frequency_control_manager.get_or_create_frequency_control(self.stream_id).get_talk_frequency_adjust()
            ):
                await self._observe(recent_messages_list=recent_messages_list)
            else:
                # 没有提到，继续保持沉默，等待5秒防止频繁触发
                await asyncio.sleep(5)
                return True
        else:
            await asyncio.sleep(0.2)
            return True
        return True

    async def _send_and_store_reply(
        self,
        response_set: "ReplySetModel",
        action_message: "DatabaseMessages",
        cycle_timers: Dict[str, float],
        thinking_id,
        actions,
        selected_expressions: Optional[List[int]] = None,
    ) -> Tuple[Dict[str, Any], str, Dict[str, float]]:
        with Timer("回复发送", cycle_timers):
            reply_text = await self._send_response(
                reply_set=response_set,
                message_data=action_message,
                selected_expressions=selected_expressions,
            )

        from src.manager.local_store_manager import local_storage
        if "deploy_success" not in local_storage:
            local_storage["deploy_success"] = True
            logger.info(f"{self.log_prefix} 核心成功完成一次完整的收发与回复，标记部署成功。")

        # 获取 platform，如果不存在则从 chat_stream 获取，如果还是 None 则使用默认值
        platform = action_message.chat_info.platform
        if platform is None:
            platform = getattr(self.chat_stream, "platform", "unknown")

        person = Person(platform=platform, user_id=action_message.user_info.user_id)
        person_name = person.person_name
        action_prompt_display = f"你对{person_name}进行了回复：{reply_text}"

        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=False,
            action_prompt_display=action_prompt_display,
            action_done=True,
            thinking_id=thinking_id,
            action_data={"reply_text": reply_text},
            action_name="reply",
        )

        # 构建循环信息
        loop_info: Dict[str, Any] = {
            "loop_plan_info": {
                "action_result": actions,
            },
            "loop_action_info": {
                "action_taken": True,
                "reply_text": reply_text,
                "command": "",
                "taken_time": time.time(),
            },
        }

        return loop_info, reply_text, cycle_timers

    async def _observe(
        self,  # interest_value: float = 0.0,
        recent_messages_list: Optional[List["DatabaseMessages"]] = None,
        force_reply_message: Optional["DatabaseMessages"] = None,
    ) -> bool:  # sourcery skip: merge-else-if-into-elif, remove-redundant-if
        if recent_messages_list is None:
            recent_messages_list = []
        reply_text = ""  # 初始化reply_text变量，避免UnboundLocalError

        if s4u_config.enable_s4u:
            await send_typing()

        # 刷新上下文以确保获取最新的模板信息
        get_chat_manager().get_stream(self.stream_id)
        current_template = self.chat_stream.context.get_template_name()
        logger.debug(f"{self.log_prefix} [HFC] Current template name: {current_template}")

        async with global_prompt_manager.async_message_scope(current_template):
            # Debug check
            debug_prompt = await global_prompt_manager.get_prompt_async("brain_planner_prompt")
            logger.debug(f"{self.log_prefix} [HFC] Resolved brain_planner_prompt preview: {str(debug_prompt)[:50]}...")

            # 使用后台任务触发学习，避免阻塞当前主流程（尤其避免LLM拥堵时阻塞发起思考）
            asyncio.create_task(self.expression_learner.trigger_learning_for_chat())

            cycle_timers, thinking_id = self.start_cycle()
            logger.info(f"{self.log_prefix} 开始第{self._cycle_counter}次思考")

            # 优先检测是不是通知戳戳，直接旁路 (跳过Bilibili平台，因为不支持且不适用)
            if len(recent_messages_list) > 0 and getattr(self.chat_stream, "platform", "") not in (
                "bilibili",
                "bilibili.live",
            ):
                latest_msg = recent_messages_list[-1]
                if getattr(latest_msg, "is_notify", False) and random.random() < 0.5:
                    logger.info(f"{self.log_prefix} 检测到戳戳动作，50%概率触发 active_poke")
                    action_data = {"loop_start_time": self.last_read_time}
                    if user_info := getattr(latest_msg, "user_info", None):
                        if user_id := getattr(user_info, "user_id", None):
                            action_data["poke_keywords"] = str(user_id)

                    poke_action = ActionPlannerInfo(
                        action_type="active_poke",
                        reasoning="检测到戳戳动作，依据50%概率触发回复戳戳",
                        action_data=action_data,
                        action_message=latest_msg,
                        available_actions={},
                    )
                    no_reply_action = ActionPlannerInfo(
                        action_type="no_reply",
                        reasoning="已触发回复戳戳，不需要语言回复",
                        action_data={},
                        action_message=latest_msg,
                        available_actions={},
                    )
                    action_to_use_info = [poke_action, no_reply_action]

                    # 假装执行了后面的逻辑，直接跳转到后面动作执行部分
                    # 需要模拟剩下的几个变量
                    available_actions = {
                        "active_poke": ActionInfo(
                            name="active_poke",
                            component_type=None,  # type: ignore
                            description="",
                            action_require=[],
                            parallel_action=True,
                        )
                    }
                    is_group_chat = bool(self.chat_stream.group_info)
                    force_reply_message = None
                    pass  # 这只是为了不打乱下面的缩进，实际上我们会把整个大块包进 else 里或者使用 goto，最好是用 if-else 包装后续的生成
                else:
                    poke_action = None
            else:
                poke_action = None

            if not poke_action:
                # 第一步：动作检查
                available_actions: Dict[str, ActionInfo] = {}
                try:
                    await self.action_modifier.modify_actions()
                    available_actions = self.action_manager.get_using_actions()
                except Exception as e:
                    logger.error(f"{self.log_prefix} 动作修改失败: {e}")

                # 执行planner
                is_group_chat, chat_target_info, _ = self.action_planner.get_necessary_info()
                context_size = global_config.chat.get_max_context_size(is_group_chat=is_group_chat)

                _hf_size = int(context_size * 0.6)
                _stepped_limit_hf = get_stepped_limit(self.stream_id, time.time(), _hf_size)
                message_list_before_now = get_raw_msg_before_timestamp_with_chat(
                    chat_id=self.stream_id,
                    timestamp=time.time(),
                    limit=_stepped_limit_hf,
                )
                # 过滤被屏蔽用户的消息
                message_list_before_now = self._filter_blocked_users(message_list_before_now, caller="observe")
                promise_snippets = []
                if not self.chat_stream.group_info:  # 群聊不启用誓言缓存
                    promise_snippets = promise_cache_manager.collect_snippets_for_messages(
                        self.stream_id, message_list_before_now
                    )
                chat_content_block, message_id_list = build_readable_messages_with_id(
                    messages=message_list_before_now,
                    timestamp_mode="normal_no_YMD",
                    read_mark=self.action_planner.last_obs_time_mark,
                    truncate=True,
                    show_actions=True,
                )
                if promise_snippets:
                    promise_block = "\n".join(["[约定缓存]"] + promise_snippets)
                    chat_content_block = f"{promise_block}\n----\n{chat_content_block}"

                prompt_info = await self.action_planner.build_planner_prompt(
                    is_group_chat=is_group_chat,
                    chat_target_info=chat_target_info,
                    current_available_actions=available_actions,
                    chat_content_block=chat_content_block,
                    message_id_list=message_id_list,
                    interest=global_config.personality.interest,
                )
                continue_flag, modified_message = await events_manager.handle_nacho_events(
                    EventType.ON_PLAN, None, prompt_info[0], None, self.chat_stream.stream_id
                )
                if not continue_flag:
                    return False
                if modified_message and modified_message._modify_flags.modify_llm_prompt:
                    prompt_info = (modified_message.llm_prompt, prompt_info[1])

                # Bypass planner for Bilibili Live (Group Chat) AND Discord Voice Channel
                # Both need low latency and simple reply/no-reply logic
                if (
                    is_group_chat and self.chat_stream.platform == "bilibili"
                ) or self.chat_stream.platform in {"discord_vc", "universal_vc"}:
                    logger.info(f"{self.log_prefix} [HFC] Bypassing Planner for {self.chat_stream.platform}")
                    # Skip bot's own messages to prevent self-reply loops
                    bot_id = str(global_config.bot.qq_account)
                    target_msg = None
                    if message_list_before_now:
                        for msg in reversed(message_list_before_now):
                            if str(msg.user_info.user_id) != bot_id:
                                target_msg = msg
                                break
                    if target_msg is None:
                        logger.info(f"{self.log_prefix} [HFC] No non-bot message found, skipping reply")
                        return True

                    # Build pending danmaku summary when multiple messages accumulated
                    bypass_extra_info = ""
                    if recent_messages_list and len(recent_messages_list) > 1:
                        danmaku_lines = []
                        for msg in recent_messages_list:
                            if str(msg.user_info.user_id) == bot_id:
                                continue
                            nick = getattr(msg.user_info, "user_nickname", None) or str(msg.user_info.user_id)
                            text = getattr(msg, "processed_plain_text", "") or ""
                            if text:
                                danmaku_lines.append(f"{nick}: {text}")
                        if len(danmaku_lines) > 1:
                            summary = "\n".join(danmaku_lines)
                            bypass_extra_info = (
                                f"[弹幕堆积提示] 以下是最近堆积的 {len(danmaku_lines)} 条弹幕，"
                                "请在回复时综合考虑这些弹幕的内容，尽量涵盖多条弹幕而不只是最新的一条：\n"
                                f"{summary}\n"
                                "[弹幕堆积提示结束]"
                            )
                            logger.info(
                                f"{self.log_prefix} [HFC] Bypass: {len(danmaku_lines)} pending danmaku detected"
                            )

                    action_to_use_info = [
                        ActionPlannerInfo(
                            action_type="reply",
                            reasoning="Bilibili Live Bypass: Direct Reply",
                            action_data={"bypass_extra_info": bypass_extra_info} if bypass_extra_info else {},
                            action_message=target_msg,
                            available_actions=available_actions,
                        )
                    ]
                else:
                    with Timer("规划器", cycle_timers):
                        # 获取当前有效的屏蔽用户ID集合传递给规划器
                        _active_blocked = (
                            {uid for uid, exp in self.blocked_users.items() if time.time() <= exp}
                            if self.blocked_users
                            else None
                        )
                        action_to_use_info, _ = await self.action_planner.plan(
                            loop_start_time=self.last_read_time,
                            available_actions=available_actions,
                            blocked_user_ids=_active_blocked,
                        )

            has_reply = False
            _reply_equivalent = {"reply", "make_appoint", "cancel_appoint"}
            for action in action_to_use_info:
                if action.action_type in _reply_equivalent:
                    has_reply = True
                    break
                action_info = available_actions.get(action.action_type)
                if action_info and "reply" in (action_info.associated_types or []):
                    has_reply = True
                    break

            if not has_reply and force_reply_message:
                action_to_use_info.append(
                    ActionPlannerInfo(
                        action_type="reply",
                        reasoning="有人提到了你，进行回复",
                        action_data={},
                        action_message=force_reply_message,
                        available_actions=available_actions,
                    )
                )

            # 3. 按并行标记执行动作，避免非并行动作互相抢占
            serial_actions = []
            parallel_actions = []
            for action in action_to_use_info:
                if action.action_type == "reply":
                    serial_actions.append(action)
                    continue
                action_info = available_actions.get(action.action_type)
                if action_info and action_info.parallel_action:
                    parallel_actions.append(action)
                else:
                    serial_actions.append(action)

            if serial_actions:
                reply_actions = [action for action in serial_actions if action.action_type == "reply"]
                other_serial_actions = [action for action in serial_actions if action.action_type != "reply"]
                serial_actions = reply_actions + other_serial_actions

            results = []
            reply_text_for_tts = ""
            for action in serial_actions:
                if reply_text_for_tts and action.action_type == "tts_action":
                    action_data = action.action_data or {}
                    if not action_data.get("voice_text"):
                        action_data["voice_text"] = reply_text_for_tts
                        action.action_data = action_data
                result = await self._execute_action(
                    action, action_to_use_info, thinking_id, available_actions, cycle_timers
                )
                results.append(result)
                if action.action_type == "reply" and isinstance(result, dict) and result.get("success"):
                    reply_text_for_tts = (result.get("reply_text") or "").strip()

            if reply_text_for_tts:
                for action in parallel_actions:
                    if action.action_type == "tts_action":
                        action_data = action.action_data or {}
                        if not action_data.get("voice_text"):
                            action_data["voice_text"] = reply_text_for_tts
                            action.action_data = action_data

            if parallel_actions:
                action_tasks = [
                    asyncio.create_task(
                        self._execute_action(action, action_to_use_info, thinking_id, available_actions, cycle_timers)
                    )
                    for action in parallel_actions
                ]
                results.extend(await asyncio.gather(*action_tasks, return_exceptions=True))

            # 处理执行结果
            reply_loop_info = None
            reply_text_from_reply = ""
            action_success = False
            action_reply_text = ""

            for result in results:
                if isinstance(result, BaseException):
                    logger.error(f"{self.log_prefix} 动作执行异常: {result}")
                    continue

                if result["action_type"] != "reply":
                    action_success = result["success"]
                    action_reply_text = result["reply_text"]
                elif result["action_type"] == "reply":
                    if result["success"]:
                        reply_loop_info = result["loop_info"]
                        reply_text_from_reply = result["reply_text"]
                    else:
                        logger.warning(f"{self.log_prefix} 回复动作执行失败")

            # 构建最终的循环信息
            if reply_loop_info:
                # 如果有回复信息，使用回复的loop_info作为基础
                loop_info = reply_loop_info
                # 更新动作执行信息
                loop_info["loop_action_info"].update(
                    {
                        "action_taken": action_success,
                        "taken_time": time.time(),
                    }
                )
                reply_text = reply_text_from_reply
            else:
                # 没有回复信息，构建纯动作的loop_info
                loop_info = {
                    "loop_plan_info": {
                        "action_result": action_to_use_info,
                    },
                    "loop_action_info": {
                        "action_taken": action_success,
                        "reply_text": action_reply_text,
                        "taken_time": time.time(),
                    },
                }
                reply_text = action_reply_text

            self.end_cycle(loop_info, cycle_timers)
            self.print_cycle_info(cycle_timers)

            """S4U内容，暂时保留"""
            if s4u_config.enable_s4u:
                await stop_typing()
                await mai_thinking_manager.get_mai_think(self.stream_id).do_think_after_response(reply_text)
            """S4U内容，暂时保留"""

            return True

    async def _main_chat_loop(self):
        """主循环，持续进行计划并可能回复消息，直到被外部取消。"""
        try:
            while self.running:
                # 主循环
                success = await self._loopbody()
                await asyncio.sleep(0.1)
                if not success:
                    break
        except asyncio.CancelledError:
            # 设置了关闭标志位后被取消是正常流程
            logger.info(f"{self.log_prefix} 已关闭聊天")
        except Exception:
            logger.error(f"{self.log_prefix} 聊天意外错误，将于3s后尝试重新启动")
            print(traceback.format_exc())
            await asyncio.sleep(3)
            self._loop_task = asyncio.create_task(self._main_chat_loop())
        logger.error(f"{self.log_prefix} 结束了当前聊天循环")

    async def _handle_action(
        self,
        action: str,
        reasoning: str,
        action_data: dict,
        cycle_timers: Dict[str, float],
        thinking_id: str,
        action_message: Optional["DatabaseMessages"] = None,
    ) -> tuple[bool, str, str]:
        """
        处理规划动作，使用动作工厂创建相应的动作处理器

        参数:
            action: 动作类型
            reasoning: 决策理由
            action_data: 动作数据，包含不同动作需要的参数
            cycle_timers: 计时器字典
            thinking_id: 思考ID

        返回:
            tuple[bool, str, str]: (是否执行了动作, 思考消息ID, 命令)
        """
        try:
            # 使用工厂创建动作处理器实例
            try:
                action_handler = self.action_manager.create_action(
                    action_name=action,
                    action_data=action_data,
                    reasoning=reasoning,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    chat_stream=self.chat_stream,
                    log_prefix=self.log_prefix,
                    action_message=action_message,
                )
            except Exception as e:
                logger.error(f"{self.log_prefix} 创建动作处理器时出错: {e}")
                traceback.print_exc()
                return False, "", ""

            if not action_handler:
                logger.warning(f"{self.log_prefix} 未能创建动作处理器: {action}")
                return False, "", ""

            # 处理动作并获取结果
            result = await action_handler.execute()
            success, action_text = result
            command = ""

            return success, action_text, command

        except Exception as e:
            logger.error(f"{self.log_prefix} 处理{action}时出错: {e}")
            traceback.print_exc()
            return False, "", ""

    async def _send_response(
        self,
        reply_set: "ReplySetModel",
        message_data: "DatabaseMessages",
        selected_expressions: Optional[List[int]] = None,
    ) -> str:
        new_message_count = message_api.count_new_messages(
            chat_id=self.chat_stream.stream_id, start_time=self.last_read_time, end_time=time.time()
        )

        need_reply = new_message_count >= random.randint(2, 4)

        if need_reply:
            logger.info(f"{self.log_prefix} 从思考到回复，共有{new_message_count}条新消息，使用引用回复")

        if send_api._should_suppress_reply_set(reply_set):
            texts = []
            for rc in reply_set.reply_data:
                if rc.content_type == ReplyContentType.TEXT:
                    texts.append(str(rc.content))
                elif rc.content_type == ReplyContentType.HYBRID:
                    if isinstance(rc.content, list):
                        for sub in rc.content:
                            if sub.content_type == ReplyContentType.TEXT:
                                texts.append(str(sub.content))
            pre_filtered = " ".join(texts)
            logger.warning(f"{self.log_prefix} 过滤前信息: {pre_filtered}")
            logger.error(f"{self.log_prefix} 检测到可疑回复模板，已替换为 Filtered")
            await send_api.text_to_stream(
                text="Filtered",
                stream_id=self.chat_stream.stream_id,
                reply_message=message_data,
                set_reply=need_reply,
                typing=False,
                selected_expressions=selected_expressions,
            )
            return "Filtered"

        # Smart Aggregation for Bilibili TTS in HeartFC_Chat
        # If platform is Bilibili AND message contains TTS tags (<ZH> or <JP>),
        # aggregate all text into one message to prevent tag splitting.
        is_bilibili = self.chat_stream.platform in ["bilibili", "bilibili.live"]
        has_tts_tags = False

        # DEBUG LOGGING (HeartFC)
        logger.debug(
            f"{self.log_prefix} [HFC-SmartAggregation]Platform='{self.chat_stream.platform}' is_bilibili={is_bilibili}"
        )

        if is_bilibili:
            for i, reply_content in enumerate(reply_set.reply_data):
                if reply_content.content_type == ReplyContentType.TEXT:
                    content = str(reply_content.content)
                    # Relaxed check: case insensitive
                    if "<zh>" in content.lower() or "<jp>" in content.lower():
                        has_tts_tags = True
                        logger.debug(
                            f"{self.log_prefix} [HFC-SmartAggregation] Found TTS tag in chunk {i}: {content[:20]}..."
                        )
                        break

        if is_bilibili and has_tts_tags:
            logger.debug(f"{self.log_prefix} [HFC] 检测到 Bilibili TTS 标签，启用智能聚合发送模式")
            full_text = ""
            for reply_content in reply_set.reply_data:
                if reply_content.content_type == ReplyContentType.TEXT:
                    full_text += str(reply_content.content)

            if full_text:
                await send_api.text_to_stream(
                    text=full_text,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=need_reply,
                    typing=False,
                    selected_expressions=selected_expressions,
                )
                return full_text
            return ""

        reply_text = ""
        first_replied = False
        for reply_content in reply_set.reply_data:
            if reply_content.content_type != ReplyContentType.TEXT:
                continue
            data: str = reply_content.content  # type: ignore
            if not first_replied:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=need_reply,
                    typing=False,
                    selected_expressions=selected_expressions,
                )
                first_replied = True
            else:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=False,
                    typing=True,
                    selected_expressions=selected_expressions,
                )
            reply_text += data

        return reply_text

    async def _execute_action(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        """执行单个动作的通用函数"""
        try:
            with Timer(f"动作{action_planner_info.action_type}", cycle_timers):
                if action_planner_info.action_type == "no_reply":
                    # 直接处理no_action逻辑，不再通过动作系统
                    reason = action_planner_info.reasoning or "选择不回复"
                    # logger.info(f"{self.log_prefix} 选择不回复，原因: {reason}")

                    # 存储no_action信息到数据库
                    await database_api.store_action_info(
                        chat_stream=self.chat_stream,
                        action_build_into_prompt=False,
                        action_prompt_display=reason,
                        action_done=True,
                        thinking_id=thinking_id,
                        action_data={"reason": reason},
                        action_name="no_action",
                    )
                    return {"action_type": "no_action", "success": True, "reply_text": "", "command": ""}

                elif action_planner_info.action_type == "wait_time":
                    action_planner_info.action_data = action_planner_info.action_data or {}
                    logger.info(f"{self.log_prefix} 等待{action_planner_info.action_data['time']}秒后回复")
                    await asyncio.sleep(action_planner_info.action_data["time"])
                    return {"action_type": "wait_time", "success": True, "reply_text": "", "command": ""}

                elif action_planner_info.action_type == "no_reply_until_call":
                    logger.info(f"{self.log_prefix} 保持沉默，直到有人直接叫的名字")
                    self.no_reply_until_call = True
                    return {"action_type": "no_reply_until_call", "success": True, "reply_text": "", "command": ""}

                elif action_planner_info.action_type == "block_user":
                    return await self._handle_block_user(
                        action_planner_info,
                        chosen_action_plan_infos,
                        thinking_id,
                        available_actions,
                        cycle_timers,
                    )

                elif action_planner_info.action_type == "set_group_title":
                    return await self._handle_set_group_title(
                        action_planner_info,
                        chosen_action_plan_infos,
                        thinking_id,
                        available_actions,
                        cycle_timers,
                    )

                elif action_planner_info.action_type == "make_appoint":
                    return await self._handle_make_appoint(
                        action_planner_info,
                        chosen_action_plan_infos,
                        thinking_id,
                        available_actions,
                        cycle_timers,
                    )

                elif action_planner_info.action_type == "cancel_appoint":
                    return await self._handle_cancel_appoint(
                        action_planner_info,
                        chosen_action_plan_infos,
                        thinking_id,
                        available_actions,
                        cycle_timers,
                    )

                elif action_planner_info.action_type == "reply":
                    try:
                        message_text_for_injection = ""
                        if action_planner_info.action_message:
                            message_text_for_injection = (
                                getattr(action_planner_info.action_message, "processed_plain_text", "") or ""
                            )
                        injection_text = injection_manager.build_injection_text(
                            chat_id=self.chat_stream.stream_id, message_text=message_text_for_injection
                        )

                        # Inject pending danmaku summary from Bilibili/DiscordVC bypass
                        bypass_extra = (action_planner_info.action_data or {}).get("bypass_extra_info", "")
                        if bypass_extra:
                            injection_text = f"{bypass_extra}\n{injection_text}" if injection_text else bypass_extra

                        # Logic to allow tools for Bilibili Comments while keeping disabled for Live Danmu
                        # even if reasoning says "Bilibili Live Bypass: Direct Reply"
                        current_enable_tool = global_config.tool.enable_tool
                        current_enable_typo = (
                            global_config.chinese_typo.enable if hasattr(global_config, "chinese_typo") else True
                        )
                        if action_planner_info.reasoning == "Bilibili Live Bypass: Direct Reply":
                            current_enable_tool = False
                            current_enable_typo = False
                            # Check if it is a comment section (re-enable tools and typo)
                            if (
                                getattr(self.chat_stream, "group_info", None)
                                and self.chat_stream.group_info.group_id
                                and str(self.chat_stream.group_info.group_id).startswith("comment:")
                            ):
                                current_enable_tool = global_config.tool.enable_tool
                                current_enable_typo = (
                                    global_config.chinese_typo.enable
                                    if hasattr(global_config, "chinese_typo")
                                    else True
                                )

                        success, llm_response = await generator_api.generate_reply(
                            chat_stream=self.chat_stream,
                            reply_message=action_planner_info.action_message,
                            available_actions=available_actions,
                            chosen_actions=chosen_action_plan_infos,
                            reply_reason=action_planner_info.reasoning or "",
                            enable_tool=current_enable_tool,
                            enable_chinese_typo=current_enable_typo,
                            request_type="replyer",
                            from_plugin=False,
                            extra_info=injection_text,
                        )

                        if not success or not llm_response or not llm_response.reply_set:
                            if action_planner_info.action_message:
                                logger.info(
                                    f"对 {action_planner_info.action_message.processed_plain_text} 的回复生成失败"
                                )
                            else:
                                logger.info("回复生成失败")
                            return {"action_type": "reply", "success": False, "reply_text": "", "loop_info": None}

                        # === Bilibili Live 两阶段联网搜索 ===
                        # 检查是否禁用了工具
                        disable_tools = False
                        if (
                            action_planner_info.action_message
                            and hasattr(action_planner_info.action_message, "additional_config")
                            and isinstance(action_planner_info.action_message.additional_config, dict)
                        ):
                            disable_tools = action_planner_info.action_message.additional_config.get(
                                "disable_tools", False
                            )

                        # 检测 Pass 1 回复中是否包含 web_search JSON 请求
                        is_bilibili_live_danmu = (
                            not disable_tools
                            and action_planner_info.reasoning == "Bilibili Live Bypass: Direct Reply"
                            and getattr(self.chat_stream, "group_info", None)
                            and not str(getattr(self.chat_stream.group_info, "group_id", "")).startswith("comment:")
                        )
                        if is_bilibili_live_danmu and llm_response.content:
                            search_result = await self._handle_bilibili_live_search_reply(
                                llm_response=llm_response,
                                action_planner_info=action_planner_info,
                                chosen_action_plan_infos=chosen_action_plan_infos,
                                thinking_id=thinking_id,
                                available_actions=available_actions,
                                cycle_timers=cycle_timers,
                            )
                            if search_result is not None:
                                return search_result
                        # === 两阶段联网搜索结束 ===

                    except asyncio.CancelledError:
                        logger.debug(f"{self.log_prefix} 并行执行：回复生成任务已被取消")
                        return {"action_type": "reply", "success": False, "reply_text": "", "loop_info": None}
                    response_set = llm_response.reply_set
                    selected_expressions = llm_response.selected_expressions
                    loop_info, reply_text, _ = await self._send_and_store_reply(
                        response_set=response_set,
                        action_message=action_planner_info.action_message,  # type: ignore
                        cycle_timers=cycle_timers,
                        thinking_id=thinking_id,
                        actions=chosen_action_plan_infos,
                        selected_expressions=selected_expressions,
                    )
                    return {
                        "action_type": "reply",
                        "success": True,
                        "reply_text": reply_text,
                        "loop_info": loop_info,
                    }

                # 其他动作
                else:
                    # 执行普通动作
                    with Timer("动作执行", cycle_timers):
                        success, reply_text, command = await self._handle_action(
                            action_planner_info.action_type,
                            action_planner_info.reasoning or "",
                            action_planner_info.action_data or {},
                            cycle_timers,
                            thinking_id,
                            action_planner_info.action_message,
                        )
                    if (
                        not success
                        and action_planner_info.action_type == "send_artwork"
                        and "未检测到明确的看画请求" in (reply_text or "")
                        and not any(action.action_type == "reply" for action in chosen_action_plan_infos)
                        and action_planner_info.action_message
                    ):
                        logger.info(f"{self.log_prefix} 画作请求未明确，改为文本回复")
                        fallback_action = ActionPlannerInfo(
                            action_type="reply",
                            reasoning="画作请求未明确，转为文本回复",
                            action_data={},
                            action_message=action_planner_info.action_message,
                            available_actions=available_actions,
                        )
                        return await self._execute_action(
                            fallback_action,
                            chosen_action_plan_infos,
                            thinking_id,
                            available_actions,
                            cycle_timers,
                        )
                    return {
                        "action_type": action_planner_info.action_type,
                        "success": success,
                        "reply_text": reply_text,
                        "command": command,
                    }

        except Exception as e:
            logger.error(f"{self.log_prefix} 执行动作时出错: {e}")
            logger.error(f"{self.log_prefix} 错误信息: {traceback.format_exc()}")
            return {
                "action_type": action_planner_info.action_type,
                "success": False,
                "reply_text": "",
                "loop_info": None,
                "error": str(e),
            }

    async def _handle_make_appoint(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        """处理 make_appoint 动作：设定定时提醒"""
        from datetime import datetime, timedelta, timezone

        from src.chat.heart_flow.appointment_scheduler import appointment_scheduler

        action_data = action_planner_info.action_data or {}
        remind_time_raw = action_data.get("remind_time", "")
        remind_content = action_data.get("remind_content", "提醒")

        # 获取 user_id
        user_id = ""
        if action_planner_info.action_message and hasattr(action_planner_info.action_message, "user_info"):
            user_id = str(getattr(action_planner_info.action_message.user_info, "user_id", ""))

        # --- 解析时间 ---
        from src.chat.heart_flow.appointment_scheduler import parse_remind_time

        tz_local = timezone(timedelta(hours=8))
        now = datetime.now(tz_local)
        remind_datetime = parse_remind_time(remind_time_raw)

        # 校验
        if not remind_datetime:
            extra_info = (
                f'[预约提醒失败] 用户请求设定提醒但时间格式无法解析："{remind_time_raw}"。'
                f"请告知用户时间格式无法识别，请重新指定。"
            )
        elif remind_datetime <= now:
            extra_info = (
                f"[预约提醒失败] 用户请求设定提醒，但指定的时间 {remind_datetime.strftime('%Y-%m-%d %H:%M')} 已经过去了。"
                f"请告知用户指定的时间已过，请重新指定一个未来的时间。"
            )
        elif (remind_datetime - now).total_seconds() > 7 * 24 * 3600:
            extra_info = "[预约提醒失败] 用户请求设定提醒，但指定的时间超过7天后。请告知用户提醒时间不能超过7天。"
        else:
            extra_info = None  # 时间有效

        if extra_info:
            # 时间无效，生成错误回复
            try:
                success, llm_response = await generator_api.generate_reply(
                    chat_stream=self.chat_stream,
                    reply_message=action_planner_info.action_message,
                    available_actions=available_actions,
                    chosen_actions=chosen_action_plan_infos,
                    reply_reason=action_planner_info.reasoning or "",
                    request_type="replyer",
                    from_plugin=False,
                    extra_info=extra_info,
                )
                if success and llm_response and llm_response.reply_set:
                    _, reply_text, _ = await self._send_and_store_reply(
                        response_set=llm_response.reply_set,
                        action_message=action_planner_info.action_message,
                        cycle_timers=cycle_timers,
                        thinking_id=thinking_id,
                        actions=chosen_action_plan_infos,
                        selected_expressions=llm_response.selected_expressions,
                    )
                    return {"action_type": "make_appoint", "success": False, "reply_text": reply_text}
            except Exception as e:
                logger.error(f"{self.log_prefix} make_appoint 错误回复生成失败: {e}")
            return {"action_type": "make_appoint", "success": False, "reply_text": ""}

        # --- 时间有效，生成确认回复 ---
        formatted_time = remind_datetime.strftime("%Y-%m-%d %H:%M")
        confirm_extra_info = (
            f'[预约提醒] 用户要求你在 {formatted_time} 提醒他"{remind_content}"。'
            f"请回复确认你已了解这个预约提醒请求，并在回复中自然地提及你会在指定时间提醒。"
        )

        try:
            success, llm_response = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                reply_message=action_planner_info.action_message,
                available_actions=available_actions,
                chosen_actions=chosen_action_plan_infos,
                reply_reason=action_planner_info.reasoning or "",
                request_type="replyer",
                from_plugin=False,
                extra_info=confirm_extra_info,
            )
            if success and llm_response and llm_response.reply_set:
                _, confirm_text, _ = await self._send_and_store_reply(
                    response_set=llm_response.reply_set,
                    action_message=action_planner_info.action_message,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    actions=chosen_action_plan_infos,
                    selected_expressions=llm_response.selected_expressions,
                )
            else:
                logger.warning(f"{self.log_prefix} make_appoint 确认回复生成失败")
                confirm_text = ""
        except Exception as e:
            logger.error(f"{self.log_prefix} make_appoint 确认回复异常: {e}")
            confirm_text = ""

        # --- 预生成提醒消息 ---
        reminder_extra_info = (
            f'[定时提醒触发] 现在是 {formatted_time}，之前用户要求你在这个时间提醒他"{remind_content}"。'
            f"请现在发送提醒消息，直接提醒用户该做的事情。语气亲切自然。"
        )

        remind_text = f"提醒你：{remind_content}"  # 默认兜底文本
        try:
            success, llm_response = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                reply_message=action_planner_info.action_message,
                available_actions=available_actions,
                chosen_actions=chosen_action_plan_infos,
                reply_reason="定时提醒触发",
                request_type="replyer",
                from_plugin=False,
                extra_info=reminder_extra_info,
            )
            if success and llm_response and llm_response.reply_set:
                # 提取纯文本
                parts = []
                for item in llm_response.reply_set.reply_data:
                    if item.content_type == ReplyContentType.TEXT:
                        parts.append(item.content)
                if parts:
                    remind_text = " ".join(parts)
        except Exception as e:
            logger.error(f"{self.log_prefix} make_appoint 提醒文本预生成失败: {e}")

        # --- 注册到调度器 ---
        appt_id = await appointment_scheduler.schedule(
            chat_id=self.chat_stream.stream_id,
            user_id=user_id,
            remind_datetime=remind_datetime,
            remind_content=remind_content,
            remind_text=remind_text,
        )

        logger.info(f"{self.log_prefix} 预约已创建: id={appt_id}, time={formatted_time}, content={remind_content}")

        # 存储 action info
        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=True,
            action_prompt_display=f'你设定了一个提醒：在 {formatted_time} 提醒用户"{remind_content}"',
            action_done=True,
            thinking_id=thinking_id,
            action_data={"remind_time": formatted_time, "remind_content": remind_content, "appt_id": appt_id},
            action_name="make_appoint",
        )

        return {"action_type": "make_appoint", "success": True, "reply_text": confirm_text}

    async def _handle_cancel_appoint(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        """处理 cancel_appoint 动作：取消定时提醒"""
        from src.chat.heart_flow.appointment_scheduler import appointment_scheduler

        action_data = action_planner_info.action_data or {}
        remind_content = action_data.get("remind_content", "")

        # 获取 user_id
        user_id = ""
        if action_planner_info.action_message and hasattr(action_planner_info.action_message, "user_info"):
            user_id = str(getattr(action_planner_info.action_message.user_info, "user_id", ""))

        chat_id = self.chat_stream.stream_id

        # 模糊匹配预约
        matches = appointment_scheduler.cancel_by_content(chat_id, user_id, remind_content)

        if len(matches) == 0:
            extra_info = (
                f'[取消预约] 用户要求取消"{remind_content}"的提醒，'
                f"但没有找到匹配的待执行预约。请告知用户当前没有这个提醒。"
            )
        elif len(matches) == 1:
            # 直接取消
            appt = matches[0]
            appointment_scheduler.cancel_by_id(appt["id"])
            extra_info = (
                f'[取消预约成功] 已取消"{appt["remind_content"]}"的提醒'
                f"（原定时间：{appt['remind_time_iso']}）。请确认告知用户已取消。"
            )
        else:
            # 多个匹配，列出让用户选择
            appt_list = ""
            for appt in matches:
                appt_list += f"- {appt['remind_content']}（时间：{appt['remind_time_iso']}）\n"
            extra_info = (
                f'[取消预约] 用户要求取消"{remind_content}"的提醒，'
                f"但找到了多个匹配的预约：\n{appt_list}"
                f"请询问用户具体要取消哪一个。"
            )

        # 生成回复
        try:
            success, llm_response = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                reply_message=action_planner_info.action_message,
                available_actions=available_actions,
                chosen_actions=chosen_action_plan_infos,
                reply_reason=action_planner_info.reasoning or "",
                request_type="replyer",
                from_plugin=False,
                extra_info=extra_info,
            )
            if success and llm_response and llm_response.reply_set:
                _, reply_text, _ = await self._send_and_store_reply(
                    response_set=llm_response.reply_set,
                    action_message=action_planner_info.action_message,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    actions=chosen_action_plan_infos,
                    selected_expressions=llm_response.selected_expressions,
                )
            else:
                reply_text = ""
        except Exception as e:
            logger.error(f"{self.log_prefix} cancel_appoint 回复生成异常: {e}")
            reply_text = ""

        # 存储 action info
        cancel_success = len(matches) == 1
        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=True,
            action_prompt_display=f'你处理了取消提醒请求："{remind_content}"，{"已取消" if cancel_success else "需要用户确认"}',
            action_done=True,
            thinking_id=thinking_id,
            action_data={"remind_content": remind_content, "matches_count": len(matches)},
            action_name="cancel_appoint",
        )

        return {"action_type": "cancel_appoint", "success": cancel_success, "reply_text": reply_text}

    async def _handle_block_user(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        """处理 block_user 动作：屏蔽指定用户15分钟"""
        action_data = action_planner_info.action_data or {}
        # 兼容旧版本的 target_qq 参数
        target_name = str(action_data.get("target_name", action_data.get("target_qq", "")))

        if not target_name:
            logger.warning(f"{self.log_prefix} block_user 缺少 target_name 参数")
            return {"action_type": "block_user", "success": False, "reply_text": ""}

        # 如果LLM返回的是纯数字（QQ号），直接使用；否则通过昵称解析QQ号
        if target_name.isdigit():
            target_user_id = target_name
            logger.info(f"{self.log_prefix} block_user 直接使用QQ号 {target_user_id}")
        else:
            resolved_id = self._resolve_user_id_by_nickname(target_name)
            if resolved_id:
                logger.info(f"{self.log_prefix} block_user 将用户 '{target_name}' 解析为 QQ号 {resolved_id}")
                target_user_id = resolved_id
            else:
                logger.warning(f"{self.log_prefix} block_user 无法将 '{target_name}' 解析为有效的QQ号")
                return {"action_type": "block_user", "success": False, "reply_text": ""}

        # 仅群聊可用
        if not self.chat_stream.group_info:
            logger.warning(f"{self.log_prefix} block_user 仅适用于群聊")
            return {"action_type": "block_user", "success": False, "reply_text": ""}

        # 不能屏蔽自己
        bot_id = str(global_config.bot.qq_account)
        if target_user_id == bot_id:
            logger.warning(f"{self.log_prefix} 不能屏蔽自己")
            return {"action_type": "block_user", "success": False, "reply_text": ""}

        # 添加到屏蔽列表，15分钟
        block_duration = 15 * 60  # 15分钟
        self.blocked_users[target_user_id] = time.time() + block_duration
        logger.info(f"{self.log_prefix} 已屏蔽用户 {target_user_id}，持续15分钟")

        # 生成确认回复
        confirm_extra_info = (
            f"[用户屏蔽] 你已屏蔽该用户({target_name})的消息，持续15分钟。"
            f"在此期间你将看不到该用户的任何消息。你需要告知该用户你刚刚屏蔽了他的消息。"
        )

        reply_text = ""
        try:
            success, llm_response = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                reply_message=action_planner_info.action_message,
                available_actions=available_actions,
                chosen_actions=chosen_action_plan_infos,
                reply_reason=action_planner_info.reasoning or "",
                request_type="replyer",
                from_plugin=False,
                extra_info=confirm_extra_info,
            )
            if success and llm_response and llm_response.reply_set:
                _, reply_text, _ = await self._send_and_store_reply(
                    response_set=llm_response.reply_set,
                    action_message=action_planner_info.action_message,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    actions=chosen_action_plan_infos,
                    selected_expressions=llm_response.selected_expressions,
                )
            else:
                reply_text = ""
        except Exception as e:
            logger.error(f"{self.log_prefix} block_user 回复生成异常: {e}")
            reply_text = ""

        # 存储 action info
        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=True,
            action_prompt_display=f"你屏蔽了用户(QQ:{target_user_id})的消息，持续15分钟",
            action_done=True,
            thinking_id=thinking_id,
            action_data={"target_qq": target_user_id, "duration": block_duration},
            action_name="block_user",
        )

        return {"action_type": "block_user", "success": True, "reply_text": reply_text}

    async def _handle_set_group_title(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        """处理 set_group_title 动作：设置群成员专属头衔"""
        action_data = action_planner_info.action_data or {}
        target_name = str(action_data.get("target_name", ""))
        title = str(action_data.get("title", ""))

        if not target_name:
            logger.warning(f"{self.log_prefix} set_group_title 缺少 target_name 参数")
            return {"action_type": "set_group_title", "success": False, "reply_text": ""}

        # 头衔长度限制：6个字符
        if len(title) > 6:
            title = title[:6]
            logger.info(f"{self.log_prefix} set_group_title 头衔超过6字符，已截断为: {title}")

        # 仅群聊可用
        if not self.chat_stream.group_info:
            logger.warning(f"{self.log_prefix} set_group_title 仅适用于群聊")
            return {"action_type": "set_group_title", "success": False, "reply_text": ""}

        # 二次校验：当前群是否在启用列表中
        current_group_id = str(self.chat_stream.group_info.group_id)
        enabled_groups = global_config.chat.title_enabled_groups
        if current_group_id not in enabled_groups:
            logger.warning(f"{self.log_prefix} set_group_title 当前群 {current_group_id} 未启用头衔功能")
            return {"action_type": "set_group_title", "success": False, "reply_text": ""}

        # 解析用户QQ号（与 block_user 相同逻辑）
        if target_name.isdigit():
            target_user_id = target_name
            logger.info(f"{self.log_prefix} set_group_title 直接使用QQ号 {target_user_id}")
        else:
            resolved_id = self._resolve_user_id_by_nickname(target_name)
            if resolved_id:
                logger.info(f"{self.log_prefix} set_group_title 将用户 '{target_name}' 解析为 QQ号 {resolved_id}")
                target_user_id = resolved_id
            else:
                logger.warning(f"{self.log_prefix} set_group_title 无法将 '{target_name}' 解析为有效的QQ号")
                return {"action_type": "set_group_title", "success": False, "reply_text": ""}

        # 发送命令到适配器
        try:
            await send_api.command_to_stream(
                command={
                    "name": "SET_GROUP_TITLE",
                    "args": {
                        "group_id": current_group_id,
                        "qq_id": target_user_id,
                        "title": title,
                    },
                },
                stream_id=self.chat_stream.stream_id,
                storage_message=False,
            )
            logger.info(
                f"{self.log_prefix} set_group_title 已发送命令: "
                f"群={current_group_id}, 用户={target_user_id}, 头衔='{title}'"
            )
        except Exception as e:
            logger.error(f"{self.log_prefix} set_group_title 发送命令失败: {e}")
            return {"action_type": "set_group_title", "success": False, "reply_text": ""}

        # 生成确认回复
        action_desc = f"清除了{target_name}的头衔" if not title else f"将{target_name}的头衔设置为「{title}」"
        confirm_extra_info = (
            f"[头衔设置] 你刚刚{action_desc}。"
            f"请自然地确认这个操作，告知用户头衔已设置成功。"
        )

        reply_text = ""
        try:
            success, llm_response = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                reply_message=action_planner_info.action_message,
                available_actions=available_actions,
                chosen_actions=chosen_action_plan_infos,
                reply_reason=action_planner_info.reasoning or "",
                request_type="replyer",
                from_plugin=False,
                extra_info=confirm_extra_info,
            )
            if success and llm_response and llm_response.reply_set:
                _, reply_text, _ = await self._send_and_store_reply(
                    response_set=llm_response.reply_set,
                    action_message=action_planner_info.action_message,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    actions=chosen_action_plan_infos,
                    selected_expressions=llm_response.selected_expressions,
                )
            else:
                reply_text = ""
        except Exception as e:
            logger.error(f"{self.log_prefix} set_group_title 回复生成异常: {e}")
            reply_text = ""

        # 存储 action info
        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=True,
            action_prompt_display=f"你{action_desc}",
            action_done=True,
            thinking_id=thinking_id,
            action_data={"target_qq": target_user_id, "title": title},
            action_name="set_group_title",
        )

        return {"action_type": "set_group_title", "success": True, "reply_text": reply_text}

    # =========================================================================
    # Bilibili Live 两阶段联网搜索
    # =========================================================================

    _BILIBILI_SEARCH_FALLBACK_REPLY = (
        '{"reply": "<JP>ちょっと待ってにゃ、猫猫が調べるにゃ</JP><ZH>稍等喵，猫猫查查看~</ZH>", "emotion": "normal"}'
    )

    async def _handle_bilibili_live_search_reply(
        self,
        llm_response,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ) -> Optional[Dict[str, Any]]:
        """处理 Bilibili 直播间两阶段联网搜索。

        检测 Pass 1 LLM 输出中是否包含 web_search JSON 请求。
        如果包含：发送初始回复 → 执行搜索 → 生成 Pass 2 回复。
        如果不包含：返回 None，让调用方继续正常发送流程。

        Returns:
            Optional[Dict]: 如果触发了搜索流程，返回 action result dict；
                            否则返回 None 表示没有触发搜索。
        """
        content = llm_response.content
        if not content:
            return None

        # --- 解析 LLM JSON 输出中的 web_search 字段 ---
        search_query = self._extract_search_query_from_response(content)
        if not search_query:
            return None

        logger.info(f"{self.log_prefix} [两阶段搜索] 检测到搜索请求: {search_query}")

        # --- Pass 1: 发送"正在查询"的初始回复 ---
        initial_reply_text = self._extract_initial_reply_from_response(llm_response)
        if not initial_reply_text:
            initial_reply_text = self._BILIBILI_SEARCH_FALLBACK_REPLY

        logger.info(f"{self.log_prefix} [两阶段搜索] 发送初始回复: {initial_reply_text[:50]}...")

        # 发送初始回复给直播间
        await send_api.text_to_stream(
            text=initial_reply_text,
            stream_id=self.chat_stream.stream_id,
            typing=False,
            storage_message=True,
        )

        # --- 执行联网搜索 (30秒超时) ---
        search_results = ""
        try:
            from src.chat.utils.web_search import WebSearchManager

            search_manager = WebSearchManager(
                chat_id=self.chat_stream.stream_id,
                enable_cache=True,
                cache_ttl=2,
            )
            search_results = await asyncio.wait_for(
                search_manager.execute_search_direct(
                    query=search_query,
                    chat_history="",
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"{self.log_prefix} [两阶段搜索] 搜索超时 (30s)，跳过 Pass 2")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply_text,
                "loop_info": None,
            }
        except Exception as e:
            logger.error(f"{self.log_prefix} [两阶段搜索] 搜索执行失败: {e}")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply_text,
                "loop_info": None,
            }

        if not search_results:
            logger.info(f"{self.log_prefix} [两阶段搜索] 搜索无结果，跳过 Pass 2")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply_text,
                "loop_info": None,
            }

        logger.info(f"{self.log_prefix} [两阶段搜索] 搜索完成，开始 Pass 2 回复生成")

        # --- Pass 2: 带搜索结果生成最终回复 ---
        try:
            pass2_result = await self._generate_search_followup_reply(
                search_results=search_results,
                original_question=getattr(action_planner_info.action_message, "processed_plain_text", "") or "",
                initial_reply=initial_reply_text,
                action_planner_info=action_planner_info,
                chosen_action_plan_infos=chosen_action_plan_infos,
                thinking_id=thinking_id,
                available_actions=available_actions,
                cycle_timers=cycle_timers,
            )
            return pass2_result
        except Exception as e:
            logger.error(f"{self.log_prefix} [两阶段搜索] Pass 2 回复生成失败: {e}")
            # Pass 1 已经发送了，返回成功
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply_text,
                "loop_info": None,
            }

    def _extract_search_query_from_response(self, content: str) -> Optional[str]:
        """从 LLM 回复内容中提取 web_search 请求。

        支持以下 JSON 格式（隔离解析，不影响其他 JSON 字段）：
          {"web_search": true, "search_query": "关键词", ...}

        Returns:
            Optional[str]: 搜索关键词，如果未请求搜索则返回 None
        """
        try:
            # 尝试从整个回复中找到 JSON 块
            text = content.strip()
            # 支持 ```json ... ``` 包裹
            import re

            json_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
            if json_match:
                text = json_match.group(1)
            else:
                # 直接尝试找 JSON 对象
                start_idx = text.find("{")
                end_idx = text.rfind("}")
                if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                    text = text[start_idx : end_idx + 1]
                else:
                    return None

            data = json.loads(text, strict=False)
            if not isinstance(data, dict):
                return None

            # 检查 web_search 字段
            web_search = data.get("web_search")
            if web_search is True or (isinstance(web_search, str) and web_search.lower() in ("true", "yes", "1")):
                query = data.get("search_query", "")
                if isinstance(query, str) and query.strip():
                    return query.strip()

        except (json.JSONDecodeError, Exception):
            pass

        return None

    def _extract_initial_reply_from_response(self, llm_response) -> Optional[str]:
        """从 LLM 响应中提取初始回复文本（搜索前发给观众的文字）。

        复用 generator 已经健壮解析好的 reply_set，
        从中提取所有的文本内容拼接。

        Returns:
            Optional[str]: 初始回复文本，如果无法提取则返回 None
        """
        if not llm_response or not getattr(llm_response, "reply_set", None):
            return None

        try:
            from src.common.data_models.message_data_model import ReplyContentType

            texts = []
            for item in llm_response.reply_set.reply_data:
                if item.content_type == ReplyContentType.TEXT or item.content_type == "text":
                    text_str = str(item.content).strip()
                    if text_str:
                        texts.append(text_str)

            if texts:
                return "\n".join(texts)
        except Exception as e:
            logger.debug(f"[两阶段搜索] 从 reply_set 提取初始回复失败: {e}")

        return None

    async def _generate_search_followup_reply(
        self,
        search_results: str,
        original_question: str,
        initial_reply: str,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ) -> Dict[str, Any]:
        """生成 Pass 2 搜索结果回复。

        使用 replyer_search_followup_prompt 模板，将搜索结果注入后生成最终回复。
        """
        from datetime import datetime
        from src.chat.utils.chat_message_builder import (
            build_readable_messages,
            get_raw_msg_before_timestamp_with_chat,
            get_stepped_limit as _get_stepped_limit,
        )

        chat_stream = self.chat_stream
        chat_id = chat_stream.stream_id
        context_size = global_config.chat.get_max_context_size(is_group_chat=True)

        # 获取背景对话
        background_dialogue_prompt = ""
        try:
            _bg_stepped_limit = _get_stepped_limit(chat_stream.stream_id, time.time(), context_size)
            messages_before = get_raw_msg_before_timestamp_with_chat(
                chat_stream.stream_id,
                time.time(),
                limit=_bg_stepped_limit,
            )
            if messages_before:
                background_dialogue_prompt = build_readable_messages(
                    messages_before,
                    replace_bot_name=True,
                )
        except Exception as e:
            logger.debug(f"[两阶段搜索] 获取背景对话失败: {e}")

        # 构建 identity
        identity = global_config.personality.personality

        # 构建表达习惯块（简化版）
        expression_habits_block = ""

        # 关键词反应
        keywords_reaction_prompt = ""

        # 回复风格
        reply_style = global_config.personality.reply_style or ""

        # moderation
        moderation_prompt = ""

        time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        try:
            prompt = await global_prompt_manager.format_prompt(
                "replyer_search_followup_prompt",
                identity=identity,
                expression_habits_block=expression_habits_block,
                time_block=time_block,
                background_dialogue_prompt=background_dialogue_prompt,
                original_question=original_question,
                initial_reply=initial_reply,
                search_results=search_results,
                keywords_reaction_prompt=keywords_reaction_prompt,
                reply_style=reply_style,
                moderation_prompt=moderation_prompt,
            )
        except Exception as e:
            logger.error(f"[两阶段搜索] 构建 Pass 2 prompt 失败: {e}")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply,
                "loop_info": None,
            }

        # 检测 TTS 模式：如果当前是 Bilibili TTS 直播间，追加 TTS 语言标签指令
        try:
            if chat_stream.context and chat_stream.context.message:
                msg = chat_stream.context.message
                if msg.message_info and msg.message_info.template_info:
                    template_name = msg.message_info.template_info.template_name
                    if template_name and isinstance(template_name, str) and template_name.endswith("_tts"):
                        tts_instruction = (
                            "\n\n非常重要：请必须同时输出中文回复和对应的日文翻译（用于语音播放），格式严格如下：\n"
                            "<JP>日本語翻訳</JP><ZH>中文原本意思</ZH>\n"
                            "例如：\n"
                            "<JP>こんにちは、ご飯を食べましたか？</JP><ZH>你好呀，吃过饭了吗？</ZH>\n"
                        )
                        prompt += tts_instruction
                        logger.info("[两阶段搜索] Pass 2 已追加 TTS 语言标签指令")
        except Exception as e:
            logger.debug(f"[两阶段搜索] TTS 检测失败: {e}")

        # 调用 LLM 生成 Pass 2 回复
        replyer = generator_api.get_replyer(chat_stream=chat_stream, request_type="replyer")
        if not replyer:
            logger.error("[两阶段搜索] 无法获取回复器")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply,
                "loop_info": None,
            }

        try:
            response_content, _, _, _ = await replyer.llm_generate_content(prompt)
        except Exception as e:
            logger.error(f"[两阶段搜索] Pass 2 LLM 调用失败: {e}")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply,
                "loop_info": None,
            }

        if not response_content or not response_content.strip():
            logger.warning("[两阶段搜索] Pass 2 LLM 返回空内容")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply,
                "loop_info": None,
            }

        logger.info(f"{self.log_prefix} [两阶段搜索] Pass 2 回复: {response_content[:80]}...")

        # 处理 Pass 2 内容 — 不分割、不加错字（直播场景简洁优先）
        from src.plugin_system.apis.generator_api import process_human_text

        reply_set = process_human_text(response_content, enable_splitter=True, enable_chinese_typo=False)
        if not reply_set:
            logger.warning("[两阶段搜索] Pass 2 文本处理失败")
            return {
                "action_type": "reply",
                "success": True,
                "reply_text": initial_reply,
                "loop_info": None,
            }

        # 发送 Pass 2 回复
        loop_info, reply_text, _ = await self._send_and_store_reply(
            response_set=reply_set,
            action_message=action_planner_info.action_message,  # type: ignore
            cycle_timers=cycle_timers,
            thinking_id=thinking_id,
            actions=chosen_action_plan_infos,
        )

        return {
            "action_type": "reply",
            "success": True,
            "reply_text": reply_text,
            "loop_info": loop_info,
        }
