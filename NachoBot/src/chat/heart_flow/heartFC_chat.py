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
from src.chat.runtime_capabilities import runtime_capabilities_from_stream
from src.chat.utils.prompt_builder import global_prompt_manager
from src.chat.utils.prompt_variables import render_dynamic_prompt_template
from src.chat.utils.timer_calculator import Timer
from src.chat.planner_actions.planner import ActionPlanner
from src.chat.planner_actions.action_modifier import ActionModifier
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.heart_flow.hfc_utils import CycleDetail
from src.chat.express.expression_learner import expression_learner_manager
from src.chat.frequency_control.frequency_control import frequency_control_manager
from src.chat.keyword_cache import promise_cache_manager
from src.person_info.person_info import Person
from src.plugin_system.base.component_types import EventType, ActionInfo
from src.chat.injection.injection_manager import injection_manager
from src.plugin_system.core import events_manager
from src.plugin_system.apis import generator_api, send_api, message_api, database_api
from src.chat.utils.chat_message_builder import (
    build_readable_messages_with_id,
    get_raw_msg_before_timestamp_with_chat,
    get_stepped_limit,
)
from src.memory_system.chat_history_summarizer import ChatHistorySummarizer
from src.chat.heart_flow.relation_scanner import RelationScanner
from src.llm_models.exceptions import ReqAbortException
from src.chat.focus.coordinator import FocusCoordinator, bind_lease, focus_coordinator, current_context_lease
from src.chat.focus.bypass_gate import (
    FocusBypassDecisionGate,
    FocusBypassDecisionKind,
    FocusBypassGateError,
)
from src.chat.focus.models import (
    ChatKind,
    EffectKind,
    FocusStoppedError,
    FocusMember,
    FocusTurn,
    StaleFocusLeaseError,
    TurnOutcome,
    TurnStatus,
    WakeReason,
    focus_session_priority,
)
from src.chat.focus.reply_context import acquire_reply_context_request, release_reply_context
from src.chat.focus.reply_delivery import settle_reply_context_delivery
from src.chat.focus.message_repository import load_message_batch
from src.chat.focus.switch_action import (
    SWITCH_CHAT_ACTION,
    execute_switch_chat,
    format_recent_source_messages,
)
from src.chat.focus.switch_planner import suppress_focus_planner_context

if TYPE_CHECKING:
    from src.chat.focus.reply_context import ReplyContextRef
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

        # 中期记忆管理器
        from src.memory_system.mid_term_memory import get_mid_term_memory_manager

        self.mid_term_memory = get_mid_term_memory_manager(chat_id=self.stream_id)

        self.no_reply_until_call = False

        # 用户屏蔽列表: {user_id: 过期时间戳}
        self.blocked_users: Dict[str, float] = {}

        # Planner 打断机制
        self._planner_interrupt_flag: Optional[asyncio.Event] = None
        self._planner_interrupt_requested: bool = False
        self._planner_interrupt_consecutive_count: int = 0
        self._last_message_received_at: float = time.time()
        self._message_debounce_required: bool = False
        self._message_debounce_seconds: float = 1.0
        self._focus_bypass_gate: FocusBypassDecisionGate | None = None
        self._focus_delivered_event_revisions: Dict[str, int] | None = None

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
            if runtime_capabilities_from_stream(self.chat_stream).history_summarization:
                await self.chat_history_summarizer.start()

            # 注册到中期记忆后台构建器
            try:
                from src.memory_system.mid_term_memory_builder import get_mid_term_memory_builder

                _mtm_builder = get_mid_term_memory_builder()
                _mtm_builder.register_chat(self.stream_id)
                await _mtm_builder.start()  # 幂等，多次调用安全
            except Exception as e:
                logger.debug(f"{self.log_prefix} 中期记忆后台构建器注册跳过: {e}")

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

    async def stop(self):
        """幂等停止当前聊天循环及其按聊天创建的后台任务。"""
        self.running = False
        task = self._loop_task
        self._loop_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self.chat_history_summarizer.stop()
        except Exception as e:
            logger.debug(f"{self.log_prefix} 停止聊天概括器时跳过: {e}")
        try:
            await self.relation_scanner.stop()
        except Exception as e:
            logger.debug(f"{self.log_prefix} 停止关系扫描器时跳过: {e}")
        try:
            from src.memory_system.mid_term_memory_builder import get_mid_term_memory_builder

            get_mid_term_memory_builder().unregister_chat(self.stream_id)
        except Exception as e:
            logger.debug(f"{self.log_prefix} 注销中期记忆构建器时跳过: {e}")

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

    def signal_new_message(self, skip_interrupt: bool = False):
        """由消息处理器推送调用，通知有新消息到达。如果 Planner 正在执行则触发打断。"""
        self._last_message_received_at = time.time()
        if skip_interrupt or not getattr(global_config.chat, "planner_interrupt_enabled", True):
            return
        if self._planner_interrupt_flag is not None and not self._planner_interrupt_requested:
            planner_interrupt_max = getattr(global_config.chat, "planner_interrupt_max_consecutive_count", 3)
            if self._planner_interrupt_consecutive_count < planner_interrupt_max:
                self._planner_interrupt_requested = True
                self._planner_interrupt_consecutive_count += 1
                self._planner_interrupt_flag.set()
                logger.info(
                    f"{self.log_prefix} 新消息推送触发 Planner 打断 "
                    f"({self._planner_interrupt_consecutive_count}/{planner_interrupt_max})"
                )

    async def _loopbody(self, focus_turn: FocusTurn | None = None):  # sourcery skip: hoist-if-from-if
        if focus_turn is not None:
            batch = load_message_batch(
                self.stream_id,
                focus_turn.read_after_row_id,
                focus_turn.read_through_row_id,
                limit=getattr(global_config.focus, "max_unread_messages", 20),
            )
            recent_messages_list = list(batch.messages)
            self._focus_consumed_through_row_id = batch.consumed_through_row_id
        else:
            recent_messages_list = message_api.get_messages_by_time_in_chat(
                chat_id=self.stream_id,
                start_time=self.last_read_time,
                end_time=time.time(),
                limit=20,
                limit_mode="latest",
                filter_mai=True,
                filter_command=True,
            )
        focus_requires_observe = bool(
            focus_turn is not None
            and (
                focus_turn.events
                or focus_turn.handoff_ids
                or focus_turn.wake_reason & (WakeReason.FOCUS_EVENT | WakeReason.SWITCH_TARGET)
            )
        )

        if len(recent_messages_list) >= 1:
            # 先推进读取时间戳，防止被屏蔽用户的消息不断累积占用循环
            self.last_read_time = time.time()
            self._last_message_received_at = time.time()

            # 再过滤被屏蔽用户的消息
            recent_messages_list = self._filter_blocked_users(recent_messages_list, caller="loopbody")

            # 过滤后无消息则跳过本轮思考
            if len(recent_messages_list) == 0:
                if focus_requires_observe:
                    return await self._observe(recent_messages_list=[], focus_turn=focus_turn)
                if focus_turn is None:
                    await asyncio.sleep(0.2)
                return True

            # Planner 打断：如果 Planner 正在执行且收到新消息，触发打断（@提及除外，总开关关闭时跳过）
            if (
                getattr(global_config.chat, "planner_interrupt_enabled", True)
                and self._planner_interrupt_flag is not None
                and not self._planner_interrupt_requested
            ):
                has_mentioned = any(
                    getattr(msg, "is_mentioned", False) or getattr(msg, "is_at", False) for msg in recent_messages_list
                )
                if not has_mentioned:
                    planner_interrupt_max = getattr(global_config.chat, "planner_interrupt_max_consecutive_count", 3)
                    if self._planner_interrupt_consecutive_count < planner_interrupt_max:
                        self._planner_interrupt_requested = True
                        self._planner_interrupt_consecutive_count += 1
                        self._message_debounce_required = True
                        self._planner_interrupt_flag.set()
                        logger.info(
                            f"{self.log_prefix} 收到新消息，发起规划器打断; "
                            f"消息数={len(recent_messages_list)} "
                            f"连续打断次数={self._planner_interrupt_consecutive_count}/{planner_interrupt_max}"
                        )

            # !处理no_reply_until_call逻辑
            if self.no_reply_until_call and not focus_requires_observe:
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
            if focus_requires_observe:
                await self._observe(recent_messages_list=recent_messages_list, focus_turn=focus_turn)
            elif mentioned_message:
                await self._observe(
                    recent_messages_list=recent_messages_list,
                    force_reply_message=mentioned_message,
                    focus_turn=focus_turn,
                )
            elif (
                random.random()
                < self.talk_threshold
                * frequency_control_manager.get_or_create_frequency_control(self.stream_id).get_talk_frequency_adjust()
            ):
                await self._observe(recent_messages_list=recent_messages_list, focus_turn=focus_turn)
            else:
                # 没有提到，继续保持沉默，等待5秒防止频繁触发
                await asyncio.sleep(5)
                return True
        else:
            if focus_requires_observe:
                return await self._observe(recent_messages_list=[], focus_turn=focus_turn)
            if focus_turn is None:
                await asyncio.sleep(0.2)
            return True
        return True

    def _get_focus_bypass_gate(self) -> FocusBypassDecisionGate:
        gate = self._focus_bypass_gate
        if gate is None:
            gate = FocusBypassDecisionGate(
                self.action_planner.planner_llm,
                timeout_seconds=global_config.focus.bypass_gate_timeout_seconds,
                max_tokens=global_config.focus.bypass_gate_max_tokens,
            )
            self._focus_bypass_gate = gate
        return gate

    @staticmethod
    def _focus_member_bypasses_planner(member: FocusMember) -> bool:
        return member.planner_bypass

    @staticmethod
    def _focus_switch_target_reply_action(
        focus_turn: FocusTurn | None,
        recent_messages: List["DatabaseMessages"],
        available_actions: Dict[str, ActionInfo],
    ) -> ActionPlannerInfo | None:
        """Turn a committed Focus switch into a direct Replyer handoff."""

        if (
            focus_turn is None
            or not (focus_turn.wake_reason & WakeReason.SWITCH_TARGET)
            or not recent_messages
        ):
            return None
        target_message = recent_messages[-1]
        return ActionPlannerInfo(
            action_type="reply",
            reasoning="Focus switch target: directly handle the event selected by the routing Gate",
            action_data={"focus_switch_target": True},
            action_message=target_message,
            available_actions=available_actions,
        )

    @staticmethod
    def _is_focus_event_only_turn(
        focus_turn: FocusTurn,
        recent_messages_list: List["DatabaseMessages"],
    ) -> bool:
        # WakeReason records why a runtime was nudged; it is not authoritative
        # evidence that unread local rows still exist.  In particular, a safe
        # return can leave SWITCH_TARGET on the same turn as a new Focus event.
        # The cursor range is the source of truth for local message work.
        return bool(
            focus_turn.events
            and not recent_messages_list
            and not focus_turn.handoff_ids
            and focus_turn.read_through_row_id <= focus_turn.read_after_row_id
        )

    @classmethod
    def _fence_focus_event_only_actions(
        cls,
        focus_turn: FocusTurn | None,
        recent_messages_list: List["DatabaseMessages"],
        actions: List[ActionPlannerInfo],
    ) -> List[ActionPlannerInfo]:
        if focus_turn is None or not cls._is_focus_event_only_turn(focus_turn, recent_messages_list):
            return actions
        if any(action.action_type == SWITCH_CHAT_ACTION for action in actions):
            return actions
        return []

    @staticmethod
    def _focus_handoff_recent_results(
        recent_messages: List["DatabaseMessages"] | None,
    ) -> List[str]:
        return list(format_recent_source_messages(recent_messages or (), limit=10))

    @staticmethod
    def _focus_forced_priority_action(
        coordinator: FocusCoordinator,
        source_chat_id: str,
        focus_turn: FocusTurn,
        available_actions: Dict[str, ActionInfo],
        recent_messages: List["DatabaseMessages"] | None = None,
    ) -> ActionPlannerInfo | None:
        """Immediately preempt the active session for a higher-priority event."""

        definition = coordinator.definition_for_chat(source_chat_id)
        if definition is None:
            return None
        source = coordinator.policy.member(definition, source_chat_id)
        if source is None:
            return None
        source_priority = focus_session_priority(source)

        def event_priority(event) -> int:
            target = coordinator.policy.member(definition, event.target_chat_id)
            return int(focus_session_priority(target)) if target is not None else 0

        ranked_events = sorted(
            focus_turn.events,
            key=lambda event: (
                -event_priority(event),
                not (event.is_at or event.is_mentioned),
                event.first_unread.message_time,
                event.event_id,
            ),
        )
        recent_results = HeartFChatting._focus_handoff_recent_results(recent_messages)
        for event in ranked_events:
            target = coordinator.policy.member(definition, event.target_chat_id)
            if target is None:
                continue
            target_priority = focus_session_priority(target)
            if target_priority <= source_priority:
                continue

            safe_return = coordinator.policy.can_return_without_handoff(
                definition,
                source_chat_id,
                event.target_chat_id,
            )
            decision = coordinator.policy.decide_switch(
                definition,
                source_chat_id,
                event.target_chat_id,
                has_handoff=not safe_return,
            )
            if not decision.allowed:
                continue

            target_kind = (
                "Planner bypass 会话"
                if HeartFChatting._focus_member_bypasses_planner(target)
                else "私聊"
            )
            handoff = {}
            if not safe_return:
                handoff = {
                    "task_summary": (
                        f"按 Focus 会话优先级从「{source.display_name or source.chat_id}」切换到"
                        f"「{target.display_name or target.chat_id}」处理新的{target_kind}事件；"
                        "以下源会话近期内容用于衔接。"
                    ),
                    "known_facts": [],
                    "pending_items": [f"处理目标{target_kind}中的最新未读消息"],
                    "recent_results": recent_results,
                }
            return ActionPlannerInfo(
                action_type=SWITCH_CHAT_ACTION,
                reasoning=(
                    "Focus priority preemption: "
                    "planner-bypass > private > normal-group"
                ),
                action_data={"event_id": event.event_id, "handoff": handoff},
                action_message=None,
                available_actions=available_actions,
            )
        return None

    # Compatibility for integrations/tests that used the old private-only helper.
    _focus_forced_private_action = _focus_forced_priority_action

    @staticmethod
    def _focus_gate_failure_fallback(
        coordinator: FocusCoordinator,
        source_chat_id: str,
        focus_turn: FocusTurn,
        available_actions: Dict[str, ActionInfo],
    ) -> ActionPlannerInfo | None:
        """Choose a server-validated fallback after the routing LLM is unavailable."""

        definition = coordinator.definition_for_chat(source_chat_id)
        if definition is None:
            return None
        source = coordinator.policy.member(definition, source_chat_id)
        if source is None:
            return None
        source_bypasses_planner = HeartFChatting._focus_member_bypasses_planner(source)
        for event in focus_turn.events:
            target = coordinator.policy.member(definition, event.target_chat_id)
            if target is None:
                continue

            explicit_signal = bool(event.is_mentioned or event.is_at)
            bypass_boundary = (
                source_bypasses_planner
                or HeartFChatting._focus_member_bypasses_planner(target)
            )
            if not (explicit_signal or bypass_boundary):
                continue

            safe_return = coordinator.policy.can_return_without_handoff(
                definition,
                source_chat_id,
                event.target_chat_id,
            )
            if source.kind is ChatKind.PRIVATE and not safe_return:
                continue
            if target.kind is ChatKind.PRIVATE and not global_config.focus.allow_group_to_private:
                continue
            policy_decision = coordinator.policy.decide_switch(
                definition,
                source_chat_id,
                event.target_chat_id,
                has_handoff=not safe_return,
            )
            if not policy_decision.allowed:
                continue

            fallback_kind = "mentioned/at" if explicit_signal else "bypass-session boundary"
            handoff = {}
            if not safe_return:
                handoff = {
                    "task_summary": (
                        f"Focus Gate 不可用，按 {fallback_kind} 降级规则从"
                        f"「{source.display_name or source.chat_id}」切换到"
                        f"「{target.display_name or target.chat_id}」。"
                    ),
                    "pending_items": ["处理目标会话中的最新 Focus 事件"],
                }
            return ActionPlannerInfo(
                action_type=SWITCH_CHAT_ACTION,
                reasoning=f"Focus Gate unavailable; deterministic {fallback_kind} fallback",
                action_data={"event_id": event.event_id, "handoff": handoff},
                action_message=None,
                available_actions=available_actions,
            )
        return None

    async def _focus_bypass_action(
        self,
        *,
        focus_turn: FocusTurn,
        current_chat_context: str,
        available_actions: Dict[str, ActionInfo],
        event_only: bool = False,
    ) -> ActionPlannerInfo | None:
        observed_revisions = {event.event_id: event.revision for event in focus_turn.events}
        definition = focus_coordinator.definition_for_chat(self.stream_id)
        source = focus_coordinator.policy.member(definition, self.stream_id) if definition is not None else None
        allow_handoff = source is None or source.kind is not ChatKind.PRIVATE
        max_attempts = global_config.focus.bypass_gate_max_attempts
        decision = None
        for attempt in range(1, max_attempts + 1):
            try:
                decision = await self._get_focus_bypass_gate().decide(
                    events=focus_turn.events,
                    current_chat_context=current_chat_context,
                    event_only=event_only,
                    allow_handoff=allow_handoff,
                )
                break
            except FocusBypassGateError as exc:
                if attempt < max_attempts:
                    retry_seconds = global_config.focus.bypass_gate_retry_seconds
                    logger.warning(
                        f"{self.log_prefix} Focus bypass gate failed on attempt "
                        f"{attempt}/{max_attempts} ({exc}); retrying in {retry_seconds}s"
                    )
                    await asyncio.sleep(retry_seconds)
                    continue
                fallback_action = self._focus_gate_failure_fallback(
                    focus_coordinator,
                    self.stream_id,
                    focus_turn,
                    available_actions,
                )
                self._focus_delivered_event_revisions = {} if fallback_action is not None else observed_revisions
                fallback_name = "switch" if fallback_action is not None else "stay"
                logger.warning(
                    f"{self.log_prefix} Focus bypass gate failed after {max_attempts} attempts "
                    f"({exc}); deterministic fallback={fallback_name}"
                )
                return fallback_action

        assert decision is not None
        logger.info(
            f"{self.log_prefix} [Focus Gate] decision={decision.kind.value} event_id={decision.event_id or '-'}"
        )
        if decision.kind is FocusBypassDecisionKind.STAY:
            self._focus_delivered_event_revisions = dict(decision.observed_event_revisions)
            return None
        # A switch event is acknowledged atomically by switch_chat.  Keeping
        # this mapping empty ensures a failed switch is not mistaken for stay.
        self._focus_delivered_event_revisions = {}
        return ActionPlannerInfo(
            action_type=SWITCH_CHAT_ACTION,
            reasoning=decision.reasoning or "Focus bypass gate selected a pending event",
            action_data=dict(decision.action_data),
            action_message=None,
            available_actions=available_actions,
        )

    def _focus_reply_context(self, cycle_id: str):
        """为当前 Focus turn 预留一次 handoff；Normal 模式返回 None。"""
        lease = current_context_lease()
        if lease is None or lease.chat_id != self.stream_id:
            return None
        return acquire_reply_context_request(
            lease,
            str(cycle_id),
            max_prompt_tokens=global_config.focus.handoff_prompt_tokens,
        )

    async def _send_and_store_reply(
        self,
        response_set: "ReplySetModel",
        action_message: "DatabaseMessages",
        cycle_timers: Dict[str, float],
        thinking_id,
        actions,
        selected_expressions: Optional[List[int]] = None,
        context_refs: Optional[List["ReplyContextRef"]] = None,
    ) -> Tuple[Dict[str, Any], str, Dict[str, float]]:
        with Timer("回复发送", cycle_timers):
            reply_text = await self._send_response(
                reply_set=response_set,
                message_data=action_message,
                selected_expressions=selected_expressions,
                context_refs=context_refs,
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
        focus_turn: FocusTurn | None = None,
    ) -> bool:  # sourcery skip: merge-else-if-into-elif, remove-redundant-if
        if recent_messages_list is None:
            recent_messages_list = []

        # 刷新上下文以确保获取最新的模板信息
        get_chat_manager().get_stream(self.stream_id)
        context = getattr(self.chat_stream, "context", None)
        current_template = context.get_template_name() if context is not None else None
        logger.debug(f"{self.log_prefix} [HFC] Current template name: {current_template}")

        async with global_prompt_manager.async_message_scope(current_template):
            # Debug check
            debug_prompt = await global_prompt_manager.get_prompt_async("brain_planner_prompt")
            logger.debug(f"{self.log_prefix} [HFC] Resolved brain_planner_prompt preview: {str(debug_prompt)[:50]}...")

            # 使用后台任务触发学习，避免阻塞当前主流程（尤其避免LLM拥堵时阻塞发起思考）
            asyncio.create_task(self.expression_learner.trigger_learning_for_chat())

            cycle_timers, thinking_id = self.start_cycle()
            logger.info(f"{self.log_prefix} 开始第{self._cycle_counter}次思考")

            # 仅在适配器声明支持通知动作时处理。
            capabilities = runtime_capabilities_from_stream(self.chat_stream)
            if len(recent_messages_list) > 0 and capabilities.notice_actions:
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

                capabilities = runtime_capabilities_from_stream(self.chat_stream)
                bypass_session = capabilities.planner_bypass
                event_only_focus_turn = bool(
                    focus_turn is not None and self._is_focus_event_only_turn(focus_turn, recent_messages_list)
                )
                gate_required = bool(focus_turn is not None and focus_turn.events)
                bypass_planner = bool(bypass_session)
                switch_target_reply_action = self._focus_switch_target_reply_action(
                    focus_turn,
                    recent_messages_list,
                    available_actions,
                )


                logger.debug(
                    f"{self.log_prefix} bypass_planner={bypass_planner}, messages={len(message_list_before_now)}"
                )

                forced_priority_action = (
                    self._focus_forced_priority_action(
                        focus_coordinator,
                        self.stream_id,
                        focus_turn,
                        available_actions,
                        message_list_before_now,
                    )
                    if focus_turn is not None
                    else None
                )
                if forced_priority_action is not None:
                    logger.info(f"{self.log_prefix} [Focus] higher-priority event forces active-session switch")
                    switch_result = await execute_switch_chat(
                        focus_coordinator,
                        lease=focus_turn.lease,
                        action_data=forced_priority_action.action_data,
                        reasoning=forced_priority_action.reasoning,
                    )
                    if switch_result.success:
                        return True
                    logger.warning(
                        f"{self.log_prefix} [Focus] forced priority switch failed: {switch_result.reason}"
                    )
                    return False

                # --- 中期记忆摘要生成已移至独立后台循环，此处无需处理 ---

                # --- A_Memorix: 启发式长期记忆注入 ---
                if not bypass_planner:
                    try:
                        from src.memory_system.heuristic_memory_injector import inject_memory_context

                        chat_content_block = await inject_memory_context(
                            chat_id=self.stream_id,
                            chat_content_block=chat_content_block,
                            messages=message_list_before_now,
                        )
                    except Exception as e:
                        logger.debug(f"{self.log_prefix} 长期记忆注入跳过: {e}")

                    # --- 人物画像注入 ---
                    try:
                        from src.memory_system.person_profile_injector import inject_person_profiles

                        chat_content_block = await inject_person_profiles(
                            chat_id=self.stream_id,
                            chat_content_block=chat_content_block,
                            messages=message_list_before_now,
                            is_group_chat=is_group_chat,
                        )
                    except Exception as e:
                        logger.debug(f"{self.log_prefix} 人物画像注入跳过: {e}")

                # Focus routing is owned by the dedicated Gate.  The full
                # Planner only decides ordinary actions after Gate has stayed;
                # it must not reinterpret or acknowledge Focus events itself.
                gate_action = None
                focus_gate_stayed = False
                if gate_required and focus_turn is not None:
                    if global_config.focus.bypass_gate_enabled:
                        gate_action = await self._focus_bypass_action(
                            focus_turn=focus_turn,
                            current_chat_context=chat_content_block,
                            available_actions=available_actions,
                            event_only=event_only_focus_turn,
                        )
                    else:
                        self._focus_delivered_event_revisions = {
                            event.event_id: event.revision for event in focus_turn.events
                        }
                        logger.info(f"{self.log_prefix} [Focus Gate] disabled; deterministic stay")
                    focus_gate_stayed = gate_action is None
                    if focus_gate_stayed and event_only_focus_turn:
                        logger.info(f"{self.log_prefix} [Focus Gate] event-only stay; skipping all actions")
                        return True
                    if gate_action is not None:
                        logger.info(f"{self.log_prefix} [Focus Gate] terminal switch without full Planner")
                        switch_result = await execute_switch_chat(
                            focus_coordinator,
                            lease=focus_turn.lease,
                            action_data=gate_action.action_data,
                            reasoning=gate_action.reasoning,
                        )
                        if switch_result.success:
                            return True
                        logger.warning(
                            f"{self.log_prefix} [Focus Gate] switch failed: {switch_result.reason}"
                        )
                        return False

                async def build_current_planner_prompt():
                    return await self.action_planner.build_planner_prompt(
                        is_group_chat=is_group_chat,
                        chat_target_info=chat_target_info,
                        current_available_actions=available_actions,
                        chat_content_block=chat_content_block,
                        message_id_list=message_id_list,
                        interest=global_config.personality.interest,
                    )

                if focus_gate_stayed:
                    with suppress_focus_planner_context():
                        prompt_info = await build_current_planner_prompt()
                else:
                    prompt_info = await build_current_planner_prompt()
                continue_flag, modified_message = await events_manager.handle_nacho_events(
                    EventType.ON_PLAN, None, prompt_info[0], None, self.chat_stream.stream_id
                )
                if not continue_flag:
                    return False
                if modified_message and modified_message._modify_flags.modify_llm_prompt:
                    prompt_info = (modified_message.llm_prompt, prompt_info[1])

                if bypass_planner:
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

                    # Summarize accumulated messages for any direct-reply session.
                    bypass_extra_info = ""
                    if recent_messages_list and len(recent_messages_list) > 1:
                        pending_lines = []
                        for msg in recent_messages_list:
                            if str(msg.user_info.user_id) == bot_id:
                                continue
                            nick = getattr(msg.user_info, "user_nickname", None) or str(msg.user_info.user_id)
                            text = getattr(msg, "processed_plain_text", "") or ""
                            if text:
                                pending_lines.append(f"{nick}: {text}")
                        if len(pending_lines) > 1:
                            summary = "\n".join(pending_lines)
                            bypass_extra_info = (
                                f"[消息堆积提示] 以下是最近堆积的 {len(pending_lines)} 条消息，"
                                "请在回复时综合考虑这些消息的内容，尽量涵盖多条消息而不只是最新的一条：\n"
                                f"{summary}\n"
                                "[消息堆积提示结束]"
                            )
                            logger.info(
                                f"{self.log_prefix} [HFC] Bypass: {len(pending_lines)} pending messages detected"
                            )

                    action_to_use_info = [
                        ActionPlannerInfo(
                            action_type="reply",
                            reasoning="Adapter capability: direct reply",
                            action_data={"bypass_extra_info": bypass_extra_info} if bypass_extra_info else {},
                            action_message=target_msg,
                            available_actions=available_actions,
                        )
                    ]
                elif switch_target_reply_action is not None:
                    logger.info(f"{self.log_prefix} [Focus] switch target goes directly to Replyer")
                    action_to_use_info = [switch_target_reply_action]
                else:
                    with Timer("规划器", cycle_timers):
                        # 获取当前有效的屏蔽用户ID集合传递给规划器
                        _active_blocked = (
                            {uid for uid, exp in self.blocked_users.items() if time.time() <= exp}
                            if self.blocked_users
                            else None
                        )
                        # 创建打断信号（仅用于 reply 动作，Planner 不受打断）
                        interrupt_flag = asyncio.Event()
                        self._planner_interrupt_flag = interrupt_flag
                        self._planner_interrupt_requested = False
                        if focus_gate_stayed:
                            with suppress_focus_planner_context():
                                action_to_use_info, _ = await self.action_planner.plan(
                                    loop_start_time=self.last_read_time,
                                    available_actions=available_actions,
                                    blocked_user_ids=_active_blocked,
                                )
                        else:
                            action_to_use_info, _ = await self.action_planner.plan(
                                loop_start_time=self.last_read_time,
                                available_actions=available_actions,
                                blocked_user_ids=_active_blocked,
                            )

            fenced_actions = self._fence_focus_event_only_actions(
                focus_turn,
                recent_messages_list,
                action_to_use_info,
            )
            if action_to_use_info and not fenced_actions:
                logger.warning(f"{self.log_prefix} event-only Focus turn 丢弃非 switch_chat 动作")
            action_to_use_info = fenced_actions

            switch_actions = [action for action in action_to_use_info if action.action_type == SWITCH_CHAT_ACTION]
            terminal_switch_selected = bool(switch_actions)
            if terminal_switch_selected:
                if len(switch_actions) > 1 or len(action_to_use_info) > 1:
                    logger.warning(f"{self.log_prefix} switch_chat 是终止动作，丢弃本轮其余动作")
                action_to_use_info = [switch_actions[0]]

            has_reply = False
            _reply_equivalent = {"reply", "make_appoint", "cancel_appoint", "ban_user", "block_user", "tts_action"}
            for action in action_to_use_info:
                if action.action_type in _reply_equivalent:
                    has_reply = True
                    break
                action_info = available_actions.get(action.action_type)
                if action_info and "reply" in (action_info.associated_types or []):
                    has_reply = True
                    break

            if not terminal_switch_selected and not has_reply and force_reply_message:
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

            terminal_result = next(
                (result for result in results if isinstance(result, dict) and result.get("terminal")),
                None,
            )

            # 处理执行结果
            reply_loop_info = None
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
                    else:
                        logger.debug(f"{self.log_prefix} 回复动作未执行（可能被中断或生成失败）")

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

            self.end_cycle(loop_info, cycle_timers)
            self.print_cycle_info(cycle_timers)

            if terminal_result is not None:
                self._planner_interrupt_flag = None
                if not self._planner_interrupt_requested:
                    self._planner_interrupt_consecutive_count = 0
                return not bool(terminal_result.get("retry"))

            self._planner_interrupt_flag = None
            if not self._planner_interrupt_requested:
                self._planner_interrupt_consecutive_count = 0

            return True

    async def _run_focus_turn(self) -> bool:
        turn = await focus_coordinator.wait_for_turn(self.stream_id)
        self._focus_consumed_through_row_id = turn.read_after_row_id
        self._focus_delivered_event_revisions = None
        outcome = None
        self._focus_turn_interrupted = False
        try:
            with bind_lease(turn.lease):
                success = await self._loopbody(turn)
            if not await focus_coordinator.is_current(turn.lease):
                status = TurnStatus.SWITCHED
            elif self._focus_turn_interrupted or self._planner_interrupt_requested:
                status = TurnStatus.CANCELLED
            elif success:
                status = TurnStatus.COMPLETED
            else:
                status = TurnStatus.FAILED
            outcome = TurnOutcome(
                status=status,
                consumed_through_row_id=self._focus_consumed_through_row_id,
                delivered_event_revisions=self._focus_delivered_event_revisions,
            )
        except asyncio.CancelledError:
            outcome = TurnOutcome(
                status=TurnStatus.CANCELLED,
                consumed_through_row_id=self._focus_consumed_through_row_id,
            )
            raise
        except StaleFocusLeaseError as exc:
            logger.info(f"{self.log_prefix} Focus turn became stale: {exc}")
            outcome = TurnOutcome(
                status=TurnStatus.STALE,
                consumed_through_row_id=self._focus_consumed_through_row_id,
            )
        except ReqAbortException:
            self._focus_turn_interrupted = True
            outcome = TurnOutcome(
                status=TurnStatus.CANCELLED,
                consumed_through_row_id=self._focus_consumed_through_row_id,
            )
        except Exception:
            outcome = TurnOutcome(
                status=TurnStatus.FAILED,
                consumed_through_row_id=self._focus_consumed_through_row_id,
            )
            raise
        finally:
            if outcome is not None:
                try:
                    await asyncio.shield(focus_coordinator.finish_turn(turn, outcome))
                except Exception as exc:
                    logger.error(f"{self.log_prefix} Failed to finish Focus turn: {exc}")
        return True

    async def _main_chat_loop(self):
        if not focus_coordinator.is_managed(self.stream_id):
            await self._normal_chat_loop()
            return

        try:
            while self.running:
                try:
                    await self._run_focus_turn()
                    await asyncio.sleep(0.1)
                except FocusStoppedError:
                    break
                except asyncio.CancelledError:
                    raise
                except FocusBypassGateError as exc:
                    retry_seconds = global_config.focus.bypass_gate_retry_seconds
                    logger.warning(f"{self.log_prefix} Focus bypass gate failed ({exc}); retrying in {retry_seconds}s")
                    await asyncio.sleep(retry_seconds)
                except Exception:
                    logger.error(f"{self.log_prefix} Focus turn failed; retrying in 3s")
                    print(traceback.format_exc())
                    await asyncio.sleep(3)
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} Focus chat loop stopped")
        logger.info(f"{self.log_prefix} Focus chat loop ended")

    async def _normal_chat_loop(self):
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

            # Focus 管理的动作必须持有当前 turn/epoch 租约，防止切换后的迟到副作用。
            async with focus_coordinator.effect_permit(
                None,
                EffectKind.ACTION,
                target_chat_id=self.stream_id,
            ):
                result = await action_handler.execute()
            success, action_text = result
            command = ""

            return success, action_text, command

        except StaleFocusLeaseError as e:
            logger.info(f"{self.log_prefix} Focus 租约失效，取消动作 {action}: {e}")
            return False, "", ""
        except Exception as e:
            logger.error(f"{self.log_prefix} 处理{action}时出错: {e}")
            traceback.print_exc()
            return False, "", ""

    async def _send_response(
        self,
        reply_set: "ReplySetModel",
        message_data: "DatabaseMessages",
        selected_expressions: Optional[List[int]] = None,
        context_refs: Optional[List["ReplyContextRef"]] = None,
    ) -> str:
        receipts: List[send_api.SendReceipt] = []
        refs = tuple(context_refs or ())
        try:
            # One permit covers the complete logical reply so a Focus switch
            # cannot commit between split message segments.
            async with focus_coordinator.effect_permit(
                None,
                EffectKind.SEND,
                target_chat_id=self.stream_id,
            ):
                reply_text, receipts = await self._send_response_permitted(
                    reply_set=reply_set,
                    message_data=message_data,
                    selected_expressions=selected_expressions,
                    receipts=receipts,
                )
        except StaleFocusLeaseError as exc:
            await self._settle_interrupted_reply_context(refs, receipts, "stale_focus_lease")
            logger.info(f"{self.log_prefix} Focus lease became stale; dropping reply send: {exc}")
            return ""
        except asyncio.CancelledError:
            await self._settle_interrupted_reply_context(refs, receipts, "send_cancelled")
            raise
        except Exception:
            await self._settle_interrupted_reply_context(refs, receipts, "send_error")
            raise

        try:
            await settle_reply_context_delivery(refs, receipts)
        except Exception as exc:
            # Delivery already happened. Releasing here could consume the same
            # handoff twice after a later retry, so retain it for recovery.
            logger.error(f"{self.log_prefix} Focus delivery settlement failed: {exc}")
        return reply_text

    async def _settle_interrupted_reply_context(
        self,
        context_refs: Tuple["ReplyContextRef", ...],
        receipts: List[send_api.SendReceipt],
        reason: str,
    ) -> None:
        if not context_refs:
            return
        try:
            if any(receipt.delivered for receipt in receipts):
                await asyncio.shield(settle_reply_context_delivery(context_refs, receipts))
            else:
                await asyncio.shield(release_reply_context(context_refs, reason))
        except Exception as exc:
            logger.warning(
                f"{self.log_prefix} Focus reply context release/settlement failed: reason={reason}, error={exc}"
            )

    async def _send_response_permitted(
        self,
        reply_set: "ReplySetModel",
        message_data: "DatabaseMessages",
        selected_expressions: Optional[List[int]],
        receipts: List[send_api.SendReceipt],
    ) -> Tuple[str, List[send_api.SendReceipt]]:
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
            receipt = await send_api.text_to_stream_receipt(
                text="Filtered",
                stream_id=self.chat_stream.stream_id,
                reply_message=message_data,
                set_reply=need_reply,
                typing=False,
                selected_expressions=selected_expressions,
            )
            receipts.append(receipt)
            return ("Filtered" if receipt.delivered else ""), receipts

        # Some adapters require tagged text to remain in one transport message.
        aggregate_tagged_text = (
            runtime_capabilities_from_stream(self.chat_stream).reply_delivery
            == "aggregate_tagged_text"
        )
        has_tts_tags = False

        logger.debug(
            f"{self.log_prefix} [HFC-SmartAggregation] aggregate_tagged_text={aggregate_tagged_text}"
        )

        if aggregate_tagged_text:
            for i, reply_content in enumerate(reply_set.reply_data):
                if reply_content.content_type == ReplyContentType.TEXT:
                    content = str(reply_content.content)
                    if "<zh>" in content.lower() or "<jp>" in content.lower():
                        has_tts_tags = True
                        logger.debug(
                            f"{self.log_prefix} [HFC-SmartAggregation] Found TTS tag in chunk {i}: {content[:20]}..."
                        )
                        break

        if aggregate_tagged_text and has_tts_tags:
            logger.debug(f"{self.log_prefix} [HFC] 检测到 TTS 标签，启用适配器声明的聚合发送模式")
            full_text = ""
            for reply_content in reply_set.reply_data:
                if reply_content.content_type == ReplyContentType.TEXT:
                    full_text += str(reply_content.content)

            if full_text:
                receipt = await send_api.text_to_stream_receipt(
                    text=full_text,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=need_reply,
                    typing=False,
                    selected_expressions=selected_expressions,
                )
                receipts.append(receipt)
                return (full_text if receipt.delivered else ""), receipts
            return "", receipts

        reply_text = ""
        first_replied = False
        for reply_content in reply_set.reply_data:
            if reply_content.content_type != ReplyContentType.TEXT:
                continue
            data: str = reply_content.content  # type: ignore
            if not first_replied:
                receipt = await send_api.text_to_stream_receipt(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=need_reply,
                    typing=False,
                    selected_expressions=selected_expressions,
                )
            else:
                receipt = await send_api.text_to_stream_receipt(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=False,
                    typing=True,
                    selected_expressions=selected_expressions,
                )
            receipts.append(receipt)
            if receipt.delivered:
                first_replied = True
                reply_text += data

        return reply_text, receipts

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
                if action_planner_info.action_type == SWITCH_CHAT_ACTION:
                    lease = current_context_lease()
                    if lease is None or lease.chat_id != self.stream_id:
                        return {
                            "action_type": SWITCH_CHAT_ACTION,
                            "success": False,
                            "reply_text": "",
                            "terminal": True,
                            "retry": False,
                            "reason": "switch_chat requires the current Focus turn lease",
                        }
                    switch_result = await execute_switch_chat(
                        focus_coordinator,
                        lease=lease,
                        action_data=action_planner_info.action_data or {},
                        reasoning=action_planner_info.reasoning or "",
                    )
                    retryable = switch_result.reason.startswith(
                        (
                            "cannot resolve Focus events",
                            "target runtime preparation failed",
                            "handoff persistence failed",
                            "switch compare-and-set failed",
                            "Focus event revision changed",
                        )
                    )
                    log = logger.info if switch_result.success else logger.warning
                    log(f"{self.log_prefix} Focus switch_chat: {switch_result.reason}")
                    return {
                        "action_type": SWITCH_CHAT_ACTION,
                        "success": switch_result.success,
                        "reply_text": "",
                        "terminal": True,
                        "retry": retryable,
                        "reason": switch_result.reason,
                        "target_chat_id": switch_result.target_chat_id,
                        "handoff_id": switch_result.handoff_id,
                    }

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

                elif action_planner_info.action_type == "ban_user":
                    return await self._handle_ban_user(
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

                        capabilities = runtime_capabilities_from_stream(self.chat_stream)
                        person_profile_block = await self._build_low_latency_person_profile_block(
                            action_planner_info.action_message,
                        )

                        # Inject pending-message summary from direct-reply sessions.
                        bypass_extra = (action_planner_info.action_data or {}).get("bypass_extra_info", "")
                        if bypass_extra:
                            injection_text = f"{bypass_extra}\n{injection_text}" if injection_text else bypass_extra

                        current_enable_tool = (
                            global_config.tool.enable_tool
                            and capabilities.tool_mode != "disabled"
                        )
                        configured_typo = (
                            global_config.chinese_typo.enable if hasattr(global_config, "chinese_typo") else True
                        )
                        current_enable_typo = configured_typo and capabilities.typo_enabled

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
                            person_profile_block=person_profile_block,
                            interrupt_flag=self._planner_interrupt_flag,
                            reply_context=self._focus_reply_context(thinking_id),
                        )

                        if not success or not llm_response or not llm_response.reply_set:
                            if action_planner_info.action_message:
                                logger.info(
                                    f"对 {action_planner_info.action_message.processed_plain_text} 的回复生成失败"
                                )
                            else:
                                logger.info("回复生成失败")
                            return {"action_type": "reply", "success": False, "reply_text": "", "loop_info": None}

                        if capabilities.web_search_mode == "two_phase" and llm_response.content:
                            search_result = await self._handle_two_phase_search_reply(
                                llm_response=llm_response,
                                action_planner_info=action_planner_info,
                                chosen_action_plan_infos=chosen_action_plan_infos,
                                context_refs=llm_response.context_refs,
                                thinking_id=thinking_id,
                                available_actions=available_actions,
                                cycle_timers=cycle_timers,
                            )
                            if search_result is not None:
                                return search_result

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
                        context_refs=llm_response.context_refs,
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

        # --- LLM 结构化解析 + 双卡口（防误触发）---
        from src.chat.heart_flow.appointment_parser import parse_appointment_request

        message_text = ""
        if action_planner_info.action_message is not None:
            message_text = (getattr(action_planner_info.action_message, "processed_plain_text", "") or "").strip()
        # planner 强制分支会把整条消息塞进 remind_time/remind_content，作为兜底文本
        if not message_text:
            message_text = str(remind_time_raw or remind_content or "").strip()

        parse_result = await parse_appointment_request(
            message_text=message_text,
            remind_time_raw=remind_time_raw,
            remind_content_hint=remind_content,
        )

        tz_local = timezone(timedelta(hours=8))
        now = datetime.now(tz_local)

        # 卡口 1：类型归一化纠偏——并非真正的提醒请求，回退为普通回复，不建预约
        if not parse_result.is_reminder:
            logger.info(f"{self.log_prefix} make_appoint 经 LLM 判定非提醒请求，转为普通回复")
            try:
                success, llm_response = await generator_api.generate_reply(
                    chat_stream=self.chat_stream,
                    reply_message=action_planner_info.action_message,
                    available_actions=available_actions,
                    chosen_actions=chosen_action_plan_infos,
                    reply_reason=action_planner_info.reasoning or "",
                    request_type="replyer",
                    from_plugin=False,
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
                    return {"action_type": "reply", "success": True, "reply_text": reply_text}
            except Exception as e:
                logger.error(f"{self.log_prefix} make_appoint 回退回复生成失败: {e}")
            return {"action_type": "reply", "success": False, "reply_text": ""}

        # 采用 LLM 解析出的提醒内容（已去除时间和指令词）
        if parse_result.remind_content:
            remind_content = parse_result.remind_content

        # 卡口 2：confidence / ambiguities 双卡口——信息不够明确则追问，不静默建预约
        if parse_result.needs_clarification:
            reason = "；".join(parse_result.ambiguities) if parse_result.ambiguities else "提醒信息不够明确"
            logger.info(f"{self.log_prefix} make_appoint 需追问：{reason}")
            clarify_extra_info = (
                f"[预约提醒待确认] 用户似乎想让你设定提醒，但信息不够明确：{reason}。"
                f"请自然地向用户追问缺少的信息（例如具体是哪天、几点），不要擅自创建提醒。"
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
                    extra_info=clarify_extra_info,
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
                logger.error(f"{self.log_prefix} make_appoint 追问回复生成失败: {e}")
            return {"action_type": "make_appoint", "success": False, "reply_text": ""}

        remind_datetime = parse_result.remind_datetime

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

        # 如果LLM返回的是纯数字（QQ号），直接使用；否则通过 Person 系统解析QQ号
        if target_name.isdigit():
            target_user_id = target_name
            logger.info(f"{self.log_prefix} block_user 直接使用QQ号 {target_user_id}")
        else:
            try:
                person = Person(person_name=target_name)
                if person.is_known:
                    target_user_id = person.get_user_id_for_platform(global_config.bot.platform) or str(person.user_id)
                    logger.info(
                        f"{self.log_prefix} block_user 通过Person解析用户 '{target_name}' -> QQ号 {target_user_id}"
                    )
                else:
                    # 回退到消息记录匹配
                    resolved_id = self._resolve_user_id_by_nickname(target_name)
                    if resolved_id:
                        target_user_id = resolved_id
                        logger.info(
                            f"{self.log_prefix} block_user 通过消息记录解析用户 '{target_name}' -> QQ号 {target_user_id}"
                        )
                    else:
                        logger.warning(f"{self.log_prefix} block_user 无法将 '{target_name}' 解析为有效的QQ号")
                        return {"action_type": "block_user", "success": False, "reply_text": ""}
            except Exception as e:
                logger.warning(f"{self.log_prefix} block_user 解析用户 '{target_name}' 失败: {e}")
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

    async def _handle_ban_user(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        """处理 ban_user 动作：禁言指定用户（由 Replyer 决策是否执行及时长）"""
        action_data = action_planner_info.action_data or {}
        target_name = str(action_data.get("target_name", ""))
        planner_reason = action_planner_info.reasoning or action_data.get("reason", "未提供原因")

        # 1. 参数校验
        if not target_name:
            logger.warning(f"{self.log_prefix} ban_user 缺少 target_name 参数")
            return {"action_type": "ban_user", "success": False, "reply_text": ""}

        # 2. 仅群聊可用
        if not self.chat_stream.group_info:
            logger.warning(f"{self.log_prefix} ban_user 仅适用于群聊")
            return {"action_type": "ban_user", "success": False, "reply_text": ""}

        # 3. 解析用户ID（复用 block_user 的解析逻辑）
        if target_name.isdigit():
            target_user_id = target_name
            logger.info(f"{self.log_prefix} ban_user 直接使用QQ号 {target_user_id}")
        else:
            try:
                person = Person(person_name=target_name)
                if person.is_known:
                    target_user_id = person.get_user_id_for_platform(global_config.bot.platform) or str(person.user_id)
                    logger.info(
                        f"{self.log_prefix} ban_user 通过Person解析用户 '{target_name}' -> QQ号 {target_user_id}"
                    )
                else:
                    resolved_id = self._resolve_user_id_by_nickname(target_name)
                    if resolved_id:
                        target_user_id = resolved_id
                        logger.info(
                            f"{self.log_prefix} ban_user 通过消息记录解析用户 '{target_name}' -> QQ号 {target_user_id}"
                        )
                    else:
                        logger.warning(f"{self.log_prefix} ban_user 无法将 '{target_name}' 解析为有效的QQ号")
                        return {"action_type": "ban_user", "success": False, "reply_text": ""}
            except Exception as e:
                logger.warning(f"{self.log_prefix} ban_user 解析用户 '{target_name}' 失败: {e}")
                return {"action_type": "ban_user", "success": False, "reply_text": ""}

        # 4. 不能禁言自己
        bot_id = str(global_config.bot.qq_account)
        if target_user_id == bot_id:
            logger.warning(f"{self.log_prefix} ban_user 不能禁言自己")
            return {"action_type": "ban_user", "success": False, "reply_text": ""}

        # 5. 白名单检查
        ban_whitelist = global_config.bot.ban_whitelist
        if target_user_id in ban_whitelist:
            logger.warning(f"{self.log_prefix} ban_user 用户 {target_user_id} 在禁言白名单中，跳过")
            return {"action_type": "ban_user", "success": False, "reply_text": ""}

        # 6. 构建禁言规则 extra_info，交给 Replyer 决策
        # 优先使用配置中的规则，为空则使用内置默认规则
        custom_rules = global_config.bot.ban_rules.strip() if global_config.bot.ban_rules else ""
        if custom_rules:
            rules_text = custom_rules
        else:
            rules_text = (
                "1. 你需要判断是否真的应该禁言该用户。请基于聊天上下文和Planner的理由做出独立判断。\n"
                "2. 如果你认为不应该禁言（比如理由不充分、用户行为不严重、存在误判），你应该拒绝禁言。\n"
                "3. 如果用户的发言令你感到不适，你可以禁言。\n"
                "4. 如果你决定禁言，需要确定禁言时长（1分钟~24小时之间），根据违规严重程度决定：\n"
                "   - 轻度违规（如轻度刷屏或是和用户开玩笑）：1~10分钟\n"
                "   - 中度违规（如持续骚扰、攻击性语言）：10~60分钟\n"
                "   - 严重违规（如涉政讨论、极端不当内容）：1~24小时\n"
                "5. 禁言时长不可超过24小时（1440分钟）。"
            )

        ban_rules_extra_info = (
            f"[禁言决策请求]\n"
            f"Planner 认为应该禁言用户「{target_name}」(ID: {target_user_id})。\n"
            f"Planner 给出的理由是：{planner_reason}\n\n"
            f"**禁言规则**：\n{rules_text}\n\n"
            f"**输出格式**：在你的回复中，必须包含以下JSON来表达你的决策（用```json包裹）：\n"
            f"如果决定禁言：\n"
            f'```json\n{{"ban_decision": true, "duration_minutes": <禁言分钟数>, "reply_to_user": "<你要对群里说的话>"}}\n```\n'
            f"如果拒绝禁言：\n"
            f'```json\n{{"ban_decision": false, "reply_to_user": "<你要对群里说的话>"}}\n```\n'
            f"注意：reply_to_user 是你想在群聊中发送的话（可以是告诫、警告，或者解释为什么不禁言等）。"
        )

        # 7. 调用 Replyer 生成决策
        try:
            success, llm_response = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                reply_message=action_planner_info.action_message,
                available_actions=available_actions,
                chosen_actions=chosen_action_plan_infos,
                reply_reason=planner_reason,
                request_type="replyer",
                from_plugin=False,
                extra_info=ban_rules_extra_info,
            )
        except Exception as e:
            logger.error(f"{self.log_prefix} ban_user Replyer 决策生成异常: {e}")
            return {"action_type": "ban_user", "success": False, "reply_text": ""}

        if not success or not llm_response or not llm_response.content:
            logger.warning(f"{self.log_prefix} ban_user Replyer 决策生成失败")
            return {"action_type": "ban_user", "success": False, "reply_text": ""}

        # 8. 解析 Replyer 的 JSON 决策
        ban_decision = self._parse_ban_decision(llm_response.content)

        reply_text = ""
        ban_executed = False
        duration_minutes = 0

        if ban_decision and ban_decision.get("ban_decision"):
            # Replyer 决定禁言
            duration_minutes = min(int(ban_decision.get("duration_minutes", 10)), 1440)
            duration_minutes = max(duration_minutes, 1)  # 至少1分钟
            duration_seconds = duration_minutes * 60

            # 发送 GROUP_BAN 命令
            current_group_id = str(self.chat_stream.group_info.group_id)
            try:
                await send_api.command_to_stream(
                    command={
                        "name": "GROUP_BAN",
                        "args": {
                            "group_id": current_group_id,
                            "qq_id": target_user_id,
                            "duration": duration_seconds,
                        },
                    },
                    stream_id=self.chat_stream.stream_id,
                    storage_message=False,
                )
                ban_executed = True
                logger.info(
                    f"{self.log_prefix} ban_user 已发送禁言命令: "
                    f"群={current_group_id}, 用户={target_user_id}, 时长={duration_minutes}分钟"
                )
            except Exception as e:
                logger.error(f"{self.log_prefix} ban_user 发送禁言命令失败: {e}")
        else:
            logger.info(f"{self.log_prefix} ban_user Replyer 拒绝禁言用户 {target_name}")

        # 9. 发送回复（使用 Replyer 决策中的回复文本）
        user_reply_text = ban_decision.get("reply_to_user", "") if ban_decision else ""

        if user_reply_text:
            from src.plugin_system.apis.generator_api import process_human_text

            reply_set = process_human_text(user_reply_text, True, True)
            if reply_set:
                _, reply_text, _ = await self._send_and_store_reply(
                    response_set=reply_set,
                    action_message=action_planner_info.action_message,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    actions=chosen_action_plan_infos,
                    selected_expressions=llm_response.selected_expressions,
                )
        elif llm_response.reply_set:
            _, reply_text, _ = await self._send_and_store_reply(
                response_set=llm_response.reply_set,
                action_message=action_planner_info.action_message,
                cycle_timers=cycle_timers,
                thinking_id=thinking_id,
                actions=chosen_action_plan_infos,
                selected_expressions=llm_response.selected_expressions,
            )

        # 10. 存储 action info
        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=True,
            action_prompt_display=(
                f"你{'禁言了' if ban_executed else '拒绝禁言'}用户({target_name})"
                + (f"，时长{duration_minutes}分钟" if ban_executed else "")
            ),
            action_done=True,
            thinking_id=thinking_id,
            action_data={
                "target_name": target_name,
                "target_user_id": target_user_id,
                "ban_executed": ban_executed,
                "duration_minutes": duration_minutes,
                "planner_reason": planner_reason,
            },
            action_name="ban_user",
        )

        return {"action_type": "ban_user", "success": ban_executed, "reply_text": reply_text}

    def _parse_ban_decision(self, content: str) -> Optional[dict]:
        """从 Replyer 回复中解析禁言决策 JSON"""
        import re
        from json_repair import repair_json

        try:
            # 优先匹配 ```json ... ``` 代码块
            json_pattern = r"```json\s*(.*?)\s*```"
            matches = re.findall(json_pattern, content, re.DOTALL)
            for match in matches:
                json_str = match.strip()
                if json_str:
                    obj = json.loads(repair_json(json_str))
                    if isinstance(obj, dict) and "ban_decision" in obj:
                        return obj

            # 回退：尝试在全文中直接搜索 JSON
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1 and start < end:
                obj = json.loads(repair_json(content[start : end + 1]))
                if isinstance(obj, dict) and "ban_decision" in obj:
                    return obj
        except Exception as e:
            logger.warning(f"{self.log_prefix} ban_user 解析 Replyer 决策 JSON 失败: {e}")
        return None

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

        # 解析用户QQ号：优先通过 Person 系统，回退到消息记录匹配
        if target_name.isdigit():
            target_user_id = target_name
            logger.info(f"{self.log_prefix} set_group_title 直接使用QQ号 {target_user_id}")
        else:
            try:
                person = Person(person_name=target_name)
                if person.is_known:
                    target_user_id = person.get_user_id_for_platform(global_config.bot.platform) or str(person.user_id)
                    logger.info(
                        f"{self.log_prefix} set_group_title 通过Person解析用户 '{target_name}' -> QQ号 {target_user_id}"
                    )
                else:
                    # 回退到消息记录匹配
                    resolved_id = self._resolve_user_id_by_nickname(target_name)
                    if resolved_id:
                        target_user_id = resolved_id
                        logger.info(
                            f"{self.log_prefix} set_group_title 通过消息记录解析用户 '{target_name}' -> QQ号 {target_user_id}"
                        )
                    else:
                        logger.warning(f"{self.log_prefix} set_group_title 无法将 '{target_name}' 解析为有效的QQ号")
                        return {"action_type": "set_group_title", "success": False, "reply_text": ""}
            except Exception as e:
                logger.warning(f"{self.log_prefix} set_group_title 解析用户 '{target_name}' 失败: {e}")
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
        confirm_extra_info = f"[头衔设置] 你刚刚{action_desc}。请自然地确认这个操作，告知用户头衔已设置成功。"

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
    # Adapter-declared low-latency Person Profile injection
    # =========================================================================

    async def _build_low_latency_person_profile_block(
        self,
        action_message,
    ) -> str:
        """Build a bounded-latency profile block when requested by the adapter."""
        capabilities = runtime_capabilities_from_stream(self.chat_stream)
        if capabilities.person_profile_mode != "low_latency" or action_message is None:
            return ""
        timeout = capabilities.person_profile_timeout_seconds

        try:
            from src.memory_system.person_profile_injector import inject_person_profiles

            messages = [action_message] if action_message else []
            is_group_chat = bool(self.chat_stream.group_info)

            result = await asyncio.wait_for(
                inject_person_profiles(
                    chat_id=self.stream_id,
                    chat_content_block="",
                    messages=messages,
                    is_group_chat=is_group_chat,
                ),
                timeout=timeout,
            )

            return result.strip() if isinstance(result, str) and result.strip() else ""
        except asyncio.TimeoutError:
            logger.debug(f"{self.log_prefix} Person Profile 预取超时")
            return ""
        except Exception as e:
            logger.debug(f"{self.log_prefix} Person Profile 预取跳过: {e}")
            return ""

    # =========================================================================
    # Adapter-declared two-phase web search
    # =========================================================================

    _TWO_PHASE_SEARCH_FALLBACK_REPLY = (
        '{"reply": "<JP>ちょっと待ってにゃ、猫猫が調べるにゃ</JP><ZH>稍等喵，猫猫查查看~</ZH>", "emotion": "normal"}'
    )

    async def _handle_two_phase_search_reply(
        self,
        llm_response,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        context_refs: Optional[List["ReplyContextRef"]],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ) -> Optional[Dict[str, Any]]:
        """处理适配器声明的两阶段联网搜索。
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

        # From this point the two-pass path owns the reservation. When there is
        # no search request the caller still owns it and settles it through the
        # normal reply path.
        refs = tuple(context_refs or ())
        logger.info(f"{self.log_prefix} [两阶段搜索] 检测到搜索请求: {search_query}")

        # --- Pass 1: 发送"正在查询"的初始回复 ---
        try:
            initial_reply_text = self._extract_initial_reply_from_response(llm_response)
            if not initial_reply_text:
                initial_reply_text = self._TWO_PHASE_SEARCH_FALLBACK_REPLY

            logger.info(f"{self.log_prefix} [两阶段搜索] 发送初始回复: {initial_reply_text[:50]}...")
            receipt = await send_api.text_to_stream_receipt(
                text=initial_reply_text,
                stream_id=self.chat_stream.stream_id,
                typing=False,
                storage_message=True,
            )
        except asyncio.CancelledError:
            await self._settle_interrupted_reply_context(refs, [], "two_phase_pass1_cancelled")
            raise
        except Exception as e:
            await self._settle_interrupted_reply_context(refs, [], "two_phase_pass1_send_error")
            logger.error(f"{self.log_prefix} [两阶段搜索] Pass 1 发送失败: {e}")
            return {
                "action_type": "reply",
                "success": False,
                "reply_text": "",
                "loop_info": None,
            }

        # Pass 1 is the reply produced from the reserved handoff context. Settle
        # it now from the real adapter receipt; Pass 2 must never ACK it again.
        await self._settle_interrupted_reply_context(refs, [receipt], "two_phase_pass1_not_delivered")
        if not receipt.delivered:
            logger.warning(
                f"{self.log_prefix} [两阶段搜索] Pass 1 未投递，停止搜索: "
                f"status={receipt.status}, detail={receipt.detail}"
            )
            return {
                "action_type": "reply",
                "success": False,
                "reply_text": "",
                "loop_info": None,
            }

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
        identity = render_dynamic_prompt_template(global_config.personality.personality)

        # 构建表达习惯块（简化版）
        expression_habits_block = ""

        # 关键词反应
        keywords_reaction_prompt = ""

        # 回复风格
        reply_style = render_dynamic_prompt_template(global_config.personality.reply_style or "")

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

        # 按适配器声明的 TTS 语言追加输出格式指令。
        try:
            if chat_stream.context and chat_stream.context.message:
                msg = chat_stream.context.message
                if msg.message_info:
                    tts_language = runtime_capabilities_from_stream(chat_stream).tts_language
                    if tts_language:
                        if tts_language == "ja":
                            # 双语模式：要求输出 <JP>/<ZH> 标签
                            tts_instruction = (
                                "\n\n非常重要：请必须同时输出中文回复和对应的日文翻译（用于语音播放），格式严格如下：\n"
                                "<JP>日本語翻訳</JP><ZH>中文原本意思</ZH>\n"
                                "例如：\n"
                                "<JP>こんにちは、ご飯を食べましたか？</JP><ZH>你好呀，吃过饭了吗？</ZH>\n"
                            )
                            prompt += tts_instruction
                            logger.info("[两阶段搜索] Pass 2 已追加 TTS 双语标签指令 (ja)")
                        elif tts_language == "zh":
                            # 纯中文模式：不需要双语标签，直接输出中文
                            tts_instruction = (
                                "\n\n请直接用中文回复，不需要输出日文翻译，不需要使用 <JP> <ZH> 等标签。\n"
                            )
                            prompt += tts_instruction
                            logger.info("[两阶段搜索] Pass 2 已追加 TTS 纯中文指令 (zh)")
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

        # 处理 Pass 2 内容 — 不分割、不加错字（低延迟场景简洁优先）
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
        # context_refs intentionally stay on Pass 1: the generated follow-up is
        # a second delivery, not a second consumption of the Focus handoff.

        return {
            "action_type": "reply",
            "success": True,
            "reply_text": reply_text,
            "loop_info": loop_info,
        }
