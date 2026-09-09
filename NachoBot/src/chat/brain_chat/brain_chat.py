import asyncio
from collections import deque
import time
import traceback
import random
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Tuple, TYPE_CHECKING
from rich.traceback import install

from src.config.config import global_config
from src.common.logger import get_logger
from src.common.data_models.info_data_model import ActionPlannerInfo
from src.common.data_models.message_data_model import ReplyContentType
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.utils.prompt_builder import global_prompt_manager
from src.chat.utils.timer_calculator import Timer
from src.chat.brain_chat.brain_planner import BrainPlanner
from src.chat.planner_actions.action_modifier import ActionModifier
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.heart_flow.hfc_utils import CycleDetail

from src.chat.express.expression_learner import expression_learner_manager
from src.chat.advanced.advanced_manager import advanced_manager
from src.chat.keyword_cache import promise_cache_manager
from src.chat.injection.injection_manager import injection_manager
from src.person_info.person_info import Person
from src.plugin_system.base.component_types import EventType, ActionInfo
from src.plugin_system.core import events_manager
from src.plugin_system.apis import generator_api, send_api, message_api, database_api
from src.llm_models.exceptions import ReqAbortException
from src.chat.focus.bypass_gate import (
    FocusBypassDecisionGate,
    FocusBypassDecisionKind,
    FocusBypassGateError,
)
from src.chat.focus.coordinator import bind_lease, focus_coordinator, current_context_lease
from src.chat.focus.models import (
    EffectKind,
    FocusStoppedError,
    FocusTurn,
    StaleFocusLeaseError,
    TurnOutcome,
    TurnStatus,
    WakeReason,
)
from src.chat.focus.reply_context import acquire_reply_context_request, release_reply_context
from src.chat.focus.reply_delivery import settle_reply_context_delivery
from src.chat.focus.message_repository import load_message_batch
from src.chat.focus.switch_action import (
    SWITCH_CHAT_ACTION,
    SwitchDisposition,
    classify_switch_failure_reason,
    classify_switch_result,
    execute_switch_chat,
)
from src.chat.utils.chat_message_builder import (
    build_readable_messages_with_id,
    get_raw_msg_before_timestamp_with_chat,
)
from src.chat.heart_flow.relation_scanner import RelationScanner
from src.chat.memory_system.memory_activator import MemoryActivator

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

logger = get_logger("bc")  # Logger Name Changed


class BrainChatting:
    """
    管理一个连续的私聊Brain Chat循环
    用于在特定聊天流中生成回复。
    """

    def __init__(self, chat_id: str):
        """
        BrainChatting 初始化函数

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
        self.action_planner = BrainPlanner(chat_id=self.stream_id, action_manager=self.action_manager)
        self.action_modifier = ActionModifier(action_manager=self.action_manager, chat_id=self.stream_id)

        # 循环控制内部状态
        self.running: bool = False
        self._loop_task: Optional[asyncio.Task] = None  # 主循环任务
        self._in_flight_operations: int = 0

        # 添加循环信息管理相关的属性
        self.history_loop: deque[CycleDetail] = deque(maxlen=200)
        self._cycle_counter = 0
        self._current_cycle_detail: CycleDetail = None  # type: ignore

        self.last_read_time = time.time() - 2

        self.more_plan = False

        # Planner 打断机制
        self._planner_interrupt_flag: Optional[asyncio.Event] = None
        self._planner_interrupt_requested: bool = False
        self._planner_interrupt_consecutive_count: int = 0
        self._last_message_received_at: float = time.time()
        self._focus_bypass_gate: FocusBypassDecisionGate | None = None

        # 关系扫描器与记忆激活器
        self.relation_scanner = RelationScanner(chat_id=self.stream_id)
        self.memory_activator = MemoryActivator()

    async def start(self):
        """检查是否需要启动主循环，如果未激活则启动。"""

        # 如果循环已经激活，直接返回
        if self.running:
            logger.debug(f"{self.log_prefix} BrainChatting 已激活，无需重复启动")
            return

        try:
            # 标记为活动状态，防止重复启动
            self.running = True

            self._loop_task = asyncio.create_task(self._main_chat_loop())
            self._loop_task.add_done_callback(self._handle_loop_completion)

            # 暂时停用群聊关系扫描
            # await self.relation_scanner.start()
            if not getattr(self.chat_stream, "group_info", None):
                await self.relation_scanner.start()

            logger.info(f"{self.log_prefix} BrainChatting 启动完成")

        except Exception as e:
            await self.stop()
            logger.error(f"{self.log_prefix} BrainChatting 启动失败: {e}")
            raise

    async def stop(self):
        """幂等停止私聊运行时。"""
        self.running = False
        task = self._loop_task
        self._loop_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self.relation_scanner.stop()
        except Exception as e:
            logger.debug(f"{self.log_prefix} 停止关系扫描器时跳过: {e}")

    def _handle_loop_completion(self, task: asyncio.Task):
        """当 _hfc_loop 任务完成时执行的回调。"""
        try:
            if exception := task.exception():
                logger.error(f"{self.log_prefix} BrainChatting: 脱离了聊天(异常): {exception}")
                logger.error(traceback.format_exc())  # Log full traceback for exceptions
            else:
                logger.info(f"{self.log_prefix} BrainChatting: 脱离了聊天 (外部停止)")
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} BrainChatting: 结束了聊天")

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

    @staticmethod
    def _is_focus_event_only_turn(
        focus_turn: FocusTurn,
        recent_messages_list: List["DatabaseMessages"],
    ) -> bool:
        """Whether this turn has routing events but no local chat work."""

        return bool(
            focus_turn.events
            and not recent_messages_list
            and not focus_turn.handoff_ids
            and focus_turn.read_through_row_id <= focus_turn.read_after_row_id
        )

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

    def _build_focus_gate_context(self) -> str:
        """Build a small read-only context for the routing-only Focus Gate."""

        is_group_chat, _, _ = self.action_planner.get_necessary_info()
        context_size = global_config.chat.get_max_context_size(is_group_chat=is_group_chat)
        messages = get_raw_msg_before_timestamp_with_chat(
            chat_id=self.stream_id,
            timestamp=time.time(),
            limit=int(context_size * 0.6),
        )
        context, _ = build_readable_messages_with_id(
            messages=messages,
            timestamp_mode="normal_no_YMD",
            read_mark=self.action_planner.last_obs_time_mark,
            truncate=True,
            show_actions=True,
        )
        return context

    async def _route_focus_event_only_turn(self, focus_turn: FocusTurn) -> bool:
        """Resolve an event-only wake without exposing historical actions to Planner."""

        if not global_config.focus.bypass_gate_enabled:
            logger.info(f"{self.log_prefix} [Focus Gate] disabled; event-only deterministic stay")
            return True

        try:
            current_chat_context = self._build_focus_gate_context()
        except Exception as exc:
            logger.debug(f"{self.log_prefix} [Focus Gate] current context unavailable: {exc}")
            current_chat_context = ""

        decision = None
        max_attempts = global_config.focus.bypass_gate_max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                decision = await self._get_focus_bypass_gate().decide(
                    events=focus_turn.events,
                    current_chat_context=current_chat_context,
                    event_only=True,
                    allow_handoff=False,
                )
                break
            except FocusBypassGateError as exc:
                if attempt < max_attempts:
                    retry_seconds = global_config.focus.bypass_gate_retry_seconds
                    logger.warning(
                        f"{self.log_prefix} Focus Gate failed on attempt "
                        f"{attempt}/{max_attempts} ({exc}); retrying in {retry_seconds}s"
                    )
                    await asyncio.sleep(retry_seconds)
                    continue

                fallback_event = next(
                    (event for event in focus_turn.events if event.is_mentioned or event.is_at),
                    None,
                )
                if fallback_event is None:
                    logger.warning(
                        f"{self.log_prefix} Focus Gate failed after {max_attempts} attempts "
                        f"({exc}); event-only deterministic stay"
                    )
                    return True

                logger.warning(
                    f"{self.log_prefix} Focus Gate failed after {max_attempts} attempts "
                    f"({exc}); deterministic mentioned/at switch"
                )
                action_data = {"event_id": fallback_event.event_id}
                reasoning = "Focus Gate unavailable; deterministic mentioned/at fallback"
                break

        if decision is not None:
            logger.info(
                f"{self.log_prefix} [Focus Gate] decision={decision.kind.value} "
                f"event_id={decision.event_id or '-'}"
            )
            if decision.kind is FocusBypassDecisionKind.STAY:
                logger.info(f"{self.log_prefix} [Focus Gate] event-only stay; skipping Brain Planner")
                return True
            action_data = dict(decision.action_data)
            reasoning = decision.reasoning or "Focus Gate selected a pending event"

        switch_result = await execute_switch_chat(
            focus_coordinator,
            lease=focus_turn.lease,
            action_data=action_data,
            reasoning=reasoning,
        )
        disposition = classify_switch_result(switch_result)
        if switch_result.success:
            logger.info(f"{self.log_prefix} [Focus Gate] terminal switch without Brain Planner")
            return True
        logger.warning(
            f"{self.log_prefix} [Focus Gate] switch failed; "
            f"{'retrying' if disposition is SwitchDisposition.RETRY else 'dropping event'}: "
            f"{switch_result.reason}"
        )
        return disposition is not SwitchDisposition.RETRY

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

        if focus_turn is not None and self._is_focus_event_only_turn(focus_turn, recent_messages_list):
            return await self._route_focus_event_only_turn(focus_turn)

        if len(recent_messages_list) >= 1:
            self.last_read_time = time.time()
            self._last_message_received_at = time.time()

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
                        self._planner_interrupt_flag.set()
                        logger.info(
                            f"{self.log_prefix} 收到新消息，发起规划器打断; "
                            f"消息数={len(recent_messages_list)} "
                            f"连续打断次数={self._planner_interrupt_consecutive_count}/{planner_interrupt_max}"
                        )

            await self._observe(
                recent_messages_list=recent_messages_list,
                focus_turn=focus_turn,
            )

        else:
            # Normal模式：消息数量不足，等待
            if focus_requires_observe:
                return await self._observe(
                    recent_messages_list=[],
                    focus_turn=focus_turn,
                )
            if focus_turn is not None:
                return True
            await asyncio.sleep(0.2)
            return True
        return True

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

    @classmethod
    def _fence_focus_event_only_actions(
        cls,
        focus_turn: FocusTurn | None,
        recent_messages_list: List["DatabaseMessages"],
        actions: List[ActionPlannerInfo],
    ) -> List[ActionPlannerInfo]:
        """Prevent an event-only Focus turn from acting on historical chat messages.

        A FOCUS_EVENT wakes the active chat so Planner can decide whether to switch
        to the chat named by the event. When the active chat itself supplied no
        new messages, side-effecting actions would necessarily target historical
        context. Only the terminal switch action is valid.
        """
        if focus_turn is None or not cls._is_focus_event_only_turn(focus_turn, recent_messages_list):
            return actions

        return [action for action in actions if action.action_type == SWITCH_CHAT_ACTION]

    async def _observe(
        self,  # interest_value: float = 0.0,
        recent_messages_list: Optional[List["DatabaseMessages"]] = None,
        focus_turn: FocusTurn | None = None,
    ) -> bool:  # sourcery skip: merge-else-if-into-elif, remove-redundant-if
        if recent_messages_list is None:
            recent_messages_list = []
        # 刷新上下文以确保获取最新的模板信息
        get_chat_manager().get_stream(self.stream_id)
        context = getattr(self.chat_stream, "context", None)
        current_template = context.get_template_name() if context is not None else None
        logger.debug(f"{self.log_prefix} Current template name: {current_template}")
        async with global_prompt_manager.async_message_scope(current_template):
            # Debug check
            debug_prompt = await global_prompt_manager.get_prompt_async("brain_planner_prompt")
            logger.debug(f"{self.log_prefix} Resolved brain_planner_prompt preview: {str(debug_prompt)[:50]}...")
            await self.expression_learner.trigger_learning_for_chat()

            cycle_timers, thinking_id = self.start_cycle()
            logger.info(f"{self.log_prefix} 开始第{self._cycle_counter}次思考")

            # 第一步：动作检查
            available_actions: Dict[str, ActionInfo] = {}
            try:
                await self.action_modifier.modify_actions()
                available_actions = self.action_manager.get_using_actions()
                # 如果最近已有TTS，临时移除tts_action，避免被选中
                try:
                    from src.plugins.built_in.tts_plugin.plugin import TTSAction

                    if "tts_action" in available_actions:
                        if await TTSAction.has_recent_tts_in_chat(self.stream_id, limit=5):
                            available_actions.pop("tts_action", None)
                            logger.info(f"{self.log_prefix} 近期已有TTS，planner本轮禁用tts_action")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"{self.log_prefix} 动作修改失败: {e}")

            # 执行planner
            is_group_chat, chat_target_info, _ = self.action_planner.get_necessary_info()
            context_size = global_config.chat.get_max_context_size(is_group_chat=is_group_chat)

            message_list_before_now = get_raw_msg_before_timestamp_with_chat(
                chat_id=self.stream_id,
                timestamp=time.time(),
                limit=int(context_size * 0.6),
            )
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

            # High-level mode check
            if advanced_manager.is_on(self.chat_stream):
                logger.info(f"{self.log_prefix} 检测到高级模式开启，跳过Planner直接回复")

                # Try to find the latest user message to reply to
                target_message = None
                # Use message_id_list from build_readable_messages_with_id which contains (id, msg) tuples
                if message_id_list:
                    # Iterate backwards to find last non-bot message
                    for _, msg in reversed(message_id_list):
                        if msg and msg.user_info and str(msg.user_info.user_id) != str(global_config.bot.qq_account):
                            target_message = msg
                            break
                    # If no user message found, fallback to the very last message
                    if target_message is None and message_id_list:
                        target_message = message_id_list[-1][1]

                action_to_use_info = [
                    ActionPlannerInfo(
                        action_type="reply",
                        reasoning="Advanced Mode: Direct Reply",
                        action_data={"loop_start_time": self.last_read_time},
                        action_message=target_message,
                        available_actions=available_actions,
                    )
                ]
            else:
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

                with Timer("规划器", cycle_timers):
                    # 创建 Planner 打断信号
                    interrupt_flag = asyncio.Event()
                    self._planner_interrupt_flag = interrupt_flag
                    self._planner_interrupt_requested = False
                    try:
                        action_to_use_info, _ = await self.action_planner.plan(
                            loop_start_time=self.last_read_time,
                            available_actions=available_actions,
                            interrupt_flag=interrupt_flag,
                        )
                    except ReqAbortException:
                        self._planner_interrupt_flag = None
                        self._focus_turn_interrupted = True
                        if not self._planner_interrupt_requested:
                            self._planner_interrupt_consecutive_count = 0
                        logger.info(f"{self.log_prefix} Planner 被新消息打断，中止本轮思考，等待新消息重新触发")
                        return True

            action_to_use_info = self._fence_focus_event_only_actions(
                focus_turn,
                recent_messages_list,
                action_to_use_info,
            )

            switch_actions = [action for action in action_to_use_info if action.action_type == SWITCH_CHAT_ACTION]
            terminal_switch_selected = bool(switch_actions)
            if terminal_switch_selected:
                if len(switch_actions) > 1 or len(action_to_use_info) > 1:
                    logger.warning(f"{self.log_prefix} switch_chat 是终止动作，丢弃本轮其余动作")
                action_to_use_info = [switch_actions[0]]

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
                reply_actions = [action for action in serial_actions if action.action_type in ["reply", "file_edit"]]
                other_serial_actions = [
                    action for action in serial_actions if action.action_type not in ["reply", "file_edit"]
                ]
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
                if action.action_type in ["reply", "file_edit"] and isinstance(result, dict) and result.get("success"):
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

                if result["action_type"] not in ["reply", "file_edit"]:
                    action_success = result["success"]
                    action_reply_text = result["reply_text"]
                elif result["action_type"] in ["reply", "file_edit"]:
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

            self._planner_interrupt_flag = None
            if not self._planner_interrupt_requested:
                self._planner_interrupt_consecutive_count = 0

            if terminal_result is not None and terminal_result.get("retry"):
                return False
            return True

    async def _run_focus_turn(self) -> bool:
        turn = await focus_coordinator.wait_for_turn(self.stream_id)
        self._focus_consumed_through_row_id = turn.read_after_row_id
        outcome = None
        self._focus_turn_interrupted = False
        try:
            with bind_lease(turn.lease):
                success = await self._run_tracked_loopbody(turn)
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
                except Exception:
                    logger.error(f"{self.log_prefix} Focus turn failed; retrying in 3s")
                    print(traceback.format_exc())
                    await asyncio.sleep(3)
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} Focus chat loop stopped")
        logger.info(f"{self.log_prefix} Focus chat loop ended")

    async def _run_tracked_loopbody(self, focus_turn: FocusTurn | None = None) -> bool:
        """记录 Planner/动作执行在途状态，防止运行时被 TTL/LRU 回收。"""
        self._in_flight_operations += 1
        try:
            return await self._loopbody(focus_turn)
        finally:
            self._in_flight_operations = max(0, self._in_flight_operations - 1)

    async def _normal_chat_loop(self):
        """主循环，持续进行计划并可能回复消息，直到被外部取消。"""
        try:
            while self.running:
                # 主循环
                success = await self._run_tracked_loopbody()
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
                        reason = "switch_chat requires the current Focus turn lease"
                        disposition = classify_switch_failure_reason(reason)
                        logger.warning(
                            f"{self.log_prefix} Focus switch_chat failed; "
                            f"{'retrying' if disposition is SwitchDisposition.RETRY else 'dropping event'}: {reason}"
                        )
                        return {
                            "action_type": SWITCH_CHAT_ACTION,
                            "success": False,
                            "reply_text": "",
                            "terminal": True,
                            "retry": disposition is SwitchDisposition.RETRY,
                            "reason": reason,
                        }
                    switch_result = await execute_switch_chat(
                        focus_coordinator,
                        lease=lease,
                        action_data=action_planner_info.action_data or {},
                        reasoning=action_planner_info.reasoning or "",
                    )
                    disposition = classify_switch_result(switch_result)
                    if switch_result.success:
                        logger.info(f"{self.log_prefix} Focus switch_chat succeeded: {switch_result.reason}")
                    else:
                        logger.warning(
                            f"{self.log_prefix} Focus switch_chat failed; "
                            f"{'retrying' if disposition is SwitchDisposition.RETRY else 'dropping event'}: "
                            f"{switch_result.reason}"
                        )
                    return {
                        "action_type": SWITCH_CHAT_ACTION,
                        "success": switch_result.success,
                        "reply_text": "",
                        "terminal": True,
                        "retry": disposition is SwitchDisposition.RETRY,
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

                elif action_planner_info.action_type in ["reply", "file_edit"]:
                    request_type = "file_edit" if action_planner_info.action_type == "file_edit" else "replyer"
                    try:
                        message_text_for_injection = ""
                        if action_planner_info.action_message:
                            message_text_for_injection = (
                                getattr(action_planner_info.action_message, "processed_plain_text", "") or ""
                            )
                        injection_text = injection_manager.build_injection_text(
                            chat_id=self.chat_stream.stream_id, message_text=message_text_for_injection
                        )

                        # 高级模式路由：禁用工具/联网并移除TTS动作
                        advanced_on = advanced_manager.is_on(self.chat_stream)
                        enable_tool_flag = global_config.tool.enable_tool

                        # Check for disable_tools in message config (e.g. from Discord VC)
                        if action_planner_info.action_message:
                            add_conf = getattr(action_planner_info.action_message, "additional_config", None)

                            # If not found directly, try message_info.additional_config (safely)
                            # DatabaseMessages does not have message_info, MessageRecv does.
                            if add_conf is None:
                                msg_info = getattr(action_planner_info.action_message, "message_info", None)
                                if msg_info:
                                    add_conf = getattr(msg_info, "additional_config", {})

                            if isinstance(add_conf, str) and add_conf:
                                try:
                                    add_conf = json.loads(add_conf)
                                except Exception:
                                    add_conf = {}

                            if add_conf and isinstance(add_conf, dict) and add_conf.get("disable_tools"):
                                enable_tool_flag = False
                                logger.info(f"{self.log_prefix} 检测到消息配置 disable_tools=True，禁用工具")
                        filtered_available_actions = available_actions
                        filtered_chosen_actions = chosen_action_plan_infos or []

                        if advanced_on:
                            if global_config.advanced.block_tools_when_on:
                                enable_tool_flag = False
                            if global_config.advanced.block_tts_when_on:
                                filtered_available_actions = {
                                    name: info for name, info in available_actions.items() if name != "tts_action"
                                }
                                filtered_chosen_actions = [
                                    action
                                    for action in (chosen_action_plan_infos or [])
                                    if getattr(action, "action_type", "") != "tts_action"
                                ]

                        # 整合模式：如果 planner 已经生成了回复，直接使用
                        focus_replyer_required = getattr(
                            global_config.focus, "mode", "off"
                        ) == "active" and focus_coordinator.is_managed(self.stream_id)
                        if action_planner_info.reply_text and not focus_replyer_required:
                            logger.info(f"{self.log_prefix} 使用集成生成的回复内容")
                            # 将文本转换为 ReplySetModel
                            from src.plugin_system.apis.generator_api import process_human_text

                            response_set = process_human_text(
                                action_planner_info.reply_text, enable_splitter=True, enable_chinese_typo=True
                            )
                            if response_set:
                                loop_info, reply_text, _ = await self._send_and_store_reply(
                                    response_set=response_set,
                                    action_message=action_planner_info.action_message,  # type: ignore
                                    cycle_timers=cycle_timers,
                                    thinking_id=thinking_id,
                                    actions=chosen_action_plan_infos,
                                    selected_expressions=None,  # 集成模式暂不单独选表情，或由 planner 决定
                                )
                                return {
                                    "action_type": action_planner_info.action_type,
                                    "success": True,
                                    "reply_text": reply_text,
                                    "loop_info": loop_info,
                                }

                        success, llm_response = await generator_api.generate_reply(
                            chat_stream=self.chat_stream,
                            reply_message=action_planner_info.action_message,
                            available_actions=filtered_available_actions,
                            chosen_actions=filtered_chosen_actions,
                            reply_reason=action_planner_info.reasoning or "",
                            enable_tool=enable_tool_flag or (request_type == "file_edit"),
                            request_type=request_type,
                            from_plugin=False,
                            extra_info=injection_text,
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
                            return {
                                "action_type": action_planner_info.action_type,
                                "success": False,
                                "reply_text": "",
                                "loop_info": None,
                            }

                    except asyncio.CancelledError:
                        logger.debug(f"{self.log_prefix} 并行执行：回复生成任务已被取消")
                        return {
                            "action_type": action_planner_info.action_type,
                            "success": False,
                            "reply_text": "",
                            "loop_info": None,
                        }
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
                        "action_type": action_planner_info.action_type,
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
                        and not any(action.action_type in ["reply", "file_edit"] for action in chosen_action_plan_infos)
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

    # ── make_appoint / cancel_appoint handlers ──────────────────────────

    async def _handle_make_appoint(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        action_data = action_planner_info.action_data or {}
        remind_time_raw: str = action_data.get("remind_time", "")
        remind_content: str = action_data.get("remind_content", "")

        user_id = ""
        if action_planner_info.action_message and hasattr(action_planner_info.action_message, "user_info"):
            user_id = str(getattr(action_planner_info.action_message.user_info, "user_id", ""))

        # --- 解析时间 ---
        from src.chat.heart_flow.appointment_scheduler import parse_remind_time

        tz_local = timezone(timedelta(hours=8))
        now = datetime.now(tz_local)
        remind_datetime = parse_remind_time(remind_time_raw)

        # --- 校验 ---
        extra_info = None
        if remind_datetime is None:
            extra_info = (
                f'[预约提醒失败] 用户请求设定提醒但时间格式无法解析："{remind_time_raw}"。'
                f"请告知用户时间格式无法识别，请重新指定。"
            )
        elif remind_datetime <= now:
            extra_info = (
                f"[预约提醒失败] 用户请求设定提醒，但指定的时间 {remind_datetime.strftime('%Y-%m-%d %H:%M')} 已经过去了。"
                "请告知用户指定的时间已过，请重新指定一个未来的时间。"
            )
        elif (remind_datetime - now).total_seconds() > 7 * 24 * 3600:
            extra_info = "[预约提醒失败] 用户请求设定提醒，但指定的时间超过7天后。请告知用户提醒时间不能超过7天。"

        if extra_info:
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
                    interrupt_flag=self._planner_interrupt_flag,
                )
                if success and llm_response and llm_response.reply_set:
                    await self._send_and_store_reply(
                        response_set=llm_response.reply_set,
                        action_message=action_planner_info.action_message,
                        cycle_timers=cycle_timers,
                        thinking_id=thinking_id,
                        actions=chosen_action_plan_infos,
                        selected_expressions=llm_response.selected_expressions,
                    )
            except Exception as e:
                logger.error(f"{self.log_prefix} make_appoint 错误回复异常: {e}")
            return {"action_type": "make_appoint", "success": False, "reply_text": "", "command": ""}

        # --- 时间有效 ---
        formatted_time = remind_datetime.strftime("%Y-%m-%d %H:%M")

        # 生成确认回复
        confirm_extra_info = (
            f'[预约提醒已设定] 用户要求你在 {formatted_time} 提醒他"{remind_content}"。'
            f"请确认已记下这个提醒，用自然语气告诉用户你会准时提醒。"
        )
        confirm_text = ""
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
                interrupt_flag=self._planner_interrupt_flag,
            )
            if success and llm_response and llm_response.reply_set:
                await self._send_and_store_reply(
                    response_set=llm_response.reply_set,
                    action_message=action_planner_info.action_message,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    actions=chosen_action_plan_infos,
                    selected_expressions=llm_response.selected_expressions,
                )
                parts = []
                for item in llm_response.reply_set.reply_data:
                    if item.content_type == ReplyContentType.TEXT:
                        parts.append(item.content)
                confirm_text = " ".join(parts) if parts else ""
            else:
                logger.warning(f"{self.log_prefix} make_appoint 确认回复生成失败")
        except Exception as e:
            logger.error(f"{self.log_prefix} make_appoint 确认回复异常: {e}")

        # --- 预生成提醒消息 ---
        reminder_extra_info = (
            f'[定时提醒触发] 现在是 {formatted_time}，之前用户要求你在这个时间提醒他"{remind_content}"。'
            f"请现在发送提醒消息，直接提醒用户该做的事情。语气亲切自然。"
        )
        remind_text = f"提醒你：{remind_content}"
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
                interrupt_flag=self._planner_interrupt_flag,
            )
            if success and llm_response and llm_response.reply_set:
                parts = []
                for item in llm_response.reply_set.reply_data:
                    if item.content_type == ReplyContentType.TEXT:
                        parts.append(item.content)
                if parts:
                    remind_text = " ".join(parts)
        except Exception as e:
            logger.warning(f"{self.log_prefix} make_appoint 提醒文本预生成失败: {e}")

        # --- 注册预约 ---
        from src.chat.heart_flow.appointment_scheduler import appointment_scheduler

        appt_id = await appointment_scheduler.schedule(
            chat_id=self.stream_id,
            user_id=user_id,
            remind_datetime=remind_datetime,
            remind_content=remind_content,
            remind_text=remind_text,
        )

        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=True,
            action_prompt_display=f"你为用户设定了提醒：{remind_content}，时间：{formatted_time}，预约ID：{appt_id}",
            action_done=True,
            thinking_id=thinking_id,
            action_data={"appt_id": appt_id, "remind_time": formatted_time, "remind_content": remind_content},
            action_name="make_appoint",
        )

        return {
            "action_type": "make_appoint",
            "success": True,
            "reply_text": confirm_text,
            "command": "",
        }

    async def _handle_cancel_appoint(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        action_data = action_planner_info.action_data or {}
        remind_content: str = action_data.get("remind_content", "")

        user_id = ""
        if action_planner_info.action_message and hasattr(action_planner_info.action_message, "user_info"):
            user_id = str(getattr(action_planner_info.action_message.user_info, "user_id", ""))

        from src.chat.heart_flow.appointment_scheduler import appointment_scheduler

        matches = appointment_scheduler.cancel_by_content(
            chat_id=self.stream_id,
            user_id=user_id,
            remind_content=remind_content,
        )

        if not matches:
            extra_info = (
                f'[取消预约失败] 用户要求取消提醒"{remind_content}"，但没有找到匹配的待执行预约。'
                f"请告知用户当前没有与此内容匹配的提醒。"
            )
        elif len(matches) == 1:
            cancelled = matches[0]
            appointment_scheduler.cancel_by_id(cancelled["id"])
            extra_info = (
                f'[预约已取消] 用户的提醒"{cancelled["remind_content"]}"（时间：{cancelled["remind_time_iso"]}）'
                f"已成功取消。请用自然语气确认取消。"
            )
        else:
            listing = "\n".join(f"- {m['remind_content']}（时间：{m['remind_time_iso']}）" for m in matches)
            extra_info = (
                f'[取消预约] 用户要求取消"{remind_content}"，但找到多个匹配的待执行预约：\n{listing}\n'
                f"请列出这些预约并请用户指明要取消哪一个。本次不执行取消操作。"
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
                extra_info=extra_info,
                interrupt_flag=self._planner_interrupt_flag,
            )
            if success and llm_response and llm_response.reply_set:
                await self._send_and_store_reply(
                    response_set=llm_response.reply_set,
                    action_message=action_planner_info.action_message,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    actions=chosen_action_plan_infos,
                    selected_expressions=llm_response.selected_expressions,
                )
        except Exception as e:
            logger.error(f"{self.log_prefix} cancel_appoint 回复异常: {e}")

        action_success = len(matches) == 1
        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=True,
            action_prompt_display=(
                f"你取消了提醒：{matches[0]['remind_content']}" if action_success else f"取消提醒失败：{remind_content}"
            ),
            action_done=action_success,
            thinking_id=thinking_id,
            action_data={"remind_content": remind_content, "matches_count": len(matches)},
            action_name="cancel_appoint",
        )

        return {
            "action_type": "cancel_appoint",
            "success": action_success,
            "reply_text": "",
            "command": "",
        }
