import json
import time
import traceback
import random
import re
import asyncio
from typing import Dict, Optional, Tuple, List, TYPE_CHECKING
from rich.traceback import install
from datetime import datetime
from json_repair import repair_json

from src.llm_models.utils_model import LLMRequest
from src.llm_models.exceptions import ReqAbortException
from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.common.data_models.info_data_model import ActionPlannerInfo
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.utils.prompt_variables import render_dynamic_prompt_template
from src.chat.utils.chat_message_builder import (
    build_readable_actions,
    get_actions_by_timestamp_with_chat,
    build_readable_messages_with_id,
    build_readable_messages,
    get_raw_msg_before_timestamp_with_chat,
    replace_user_references,
    get_stepped_limit,
)
from src.chat.utils.prompt_injection_guard import guard_user_content
from src.chat.utils.display_name import resolve_sender_name
from src.chat.utils.context_builder import build_tool_info, build_relation_info, build_lpmm_knowledge_info
from src.chat.utils.capability_router import CapabilityRouter
from src.memory_system.memory_retrieval import build_memory_retrieval_prompt
from src.chat.utils.utils import get_chat_type_and_target_info
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.focus.coordinator import focus_coordinator
from src.chat.focus.switch_action import SWITCH_CHAT_ACTION, normalize_switch_action_data
from src.chat.focus.switch_eligibility import can_offer_switch_chat
from src.chat.focus.switch_planner import has_active_focus_lease, render_switch_planner_context
from src.plugin_system.base.component_types import ActionInfo, ComponentType, ActionActivationType
from src.plugin_system.core.component_registry import component_registry
import os
import tomlkit
from src.plugin_system.core.tool_use import ToolExecutor
from src.plugin_system.core.mcp_tool_executor import MCPToolExecutor
from src.chat.utils.web_search import WebSearchManager
from src.chat.utils.url_fetcher import UrlContentFetcher

if TYPE_CHECKING:
    from src.common.data_models.info_data_model import TargetPersonInfo
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("replyer")

install(extra_lines=3)

_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _has_url_message(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(_URL_PATTERN.search(text))


def _is_bot_message(message: Optional["DatabaseMessages"]) -> bool:
    if not message or not getattr(message, "user_info", None):
        return False
    user_id = str(getattr(message.user_info, "user_id", ""))
    platform = getattr(message.user_info, "platform", None)
    return user_id == str(global_config.bot.qq_account) and platform == global_config.bot.platform


def _pick_latest_user_message(
    message_id_list: List[Tuple[str, "DatabaseMessages"]],
) -> Optional["DatabaseMessages"]:
    for _, message in reversed(message_id_list):
        if not _is_bot_message(message):
            return message
    return message_id_list[-1][1] if message_id_list else None


def init_prompt():
    Prompt(
        """
{name_block}
你的兴趣是：{interest}

**可用的action**
reply
动作描述：
1.你可以自然的顺着正在进行的聊天内容进行回复或自然的提出一个问题
2.如果你需要检索过去的记忆、了解某人、查资料来更好地回复，请生成一个具体的问题
{{
    "action": "reply",
    "target_message_id":"想要回复的消息id",
    "reason":"回复的原因",
    "question":"需要检索或回忆的具体问题（可选，不需要则省略）"
}}

no_reply
动作描述：
等待，保持沉默，等待对方发言
在私聊中这是最低优先级的动作，只有当对方明确表示暂时不需要回复或你已连续多轮回复需要暂停时才使用
{{
    "action": "no_reply",
}}

make_appoint
动作描述：
为用户设定一个定时提醒，在指定时间发送提醒消息
使用条件：当用户的消息中包含提醒、定时、闹钟、叫我、到时候等意图时使用
典型触发语句：「xx分钟后提醒我」「今晚十点叫我」「帮我定个闹钟」「到时候记得叫我」「xx后叫我起床」
注意：只要用户表达了"在某个时间提醒/叫/通知"的意图，就应该选择此动作而不是reply
{{
    "action": "make_appoint",
    "target_message_id":"触发action的消息id",
    "remind_time":"提醒的绝对时间，ISO8601格式如2026-02-18T22:00:00+08:00，或相对时间如+20m/+1h/+30s",
    "remind_content":"到时间后提醒用户的事项内容",
    "reason":"设定提醒的原因"
}}

cancel_appoint
动作描述：
取消用户之前设定的定时提醒
使用条件：当用户明确要求取消某个预约提醒时使用
当前待执行的预约列表：
{pending_appointments}
{{
    "action": "cancel_appoint",
    "target_message_id":"触发action的消息id",
    "remind_content":"要取消的提醒事项内容",
    "reason":"取消提醒的原因"
}}

{action_options_text}

**动作选择要求**
请你根据聊天内容,用户的最新消息和以下标准选择合适的动作:
{plan_style}
**重要**：如果用户最新消息中包含"提醒""叫我""闹钟""定时""记得叫""到时候"等词语，并且指定了时间，必须选择make_appoint动作，不要选择reply。
回复动作若未明确指定 target_message_id，请选择最新的非机器人消息。

请选择所有符合使用要求的action，动作用json格式输出，如果输出多个json，每个json都要单独用```json包裹，你可以重复使用同一个动作或不同动作:
**示例**
// 理由文本
```json
{{
    "action":"动作名",
    "target_message_id":"触发动作的消息id",
    //对应参数
}}
```
```json
{{
    "action":"动作名",
    "target_message_id":"触发动作的消息id",
    //对应参数
}}
```

--- 以下为本轮动态上下文 ---

{chat_context_description}，以下是具体的聊天内容
**聊天内容**
{chat_content_block}
{time_block}

**动作记录**
{actions_before_now_block}

请选择合适的action，并说明触发action的消息id和选择该action的原因。消息id格式:m+数字
先输出你的选择思考理由，再输出你选择的action，理由是一段平文本，不要分点，精简。
{moderation_prompt}

""",
        "brain_planner_prompt",
    )

    Prompt(
        """
{action_name}
动作描述：{action_description}
使用条件：
{action_require}
{{
    "action": "{action_name}",{action_parameters},
    "target_message_id":"触发action的消息id",
    "reason":"触发action的原因"
}}
""",
        "brain_action_prompt",
    )


class BrainPlanner:
    def __init__(self, chat_id: str, action_manager: ActionManager):
        self.chat_id = chat_id
        self.log_prefix = f"[{get_chat_manager().get_stream_name(chat_id) or chat_id}]"
        self.action_manager = action_manager

        self.separated_llm = LLMRequest(
            model_set=model_config.model_task_config.get_private_planner(), request_type="planner"
        )  # 独立规划器使用各自的模型

        self.last_obs_time_mark = 0.0

        # 初始化独立工具执行器，专用于集成规划模式
        self.tool_executor = ToolExecutor(
            chat_id=self.chat_id,
            enable_cache=True,
            cache_ttl=3,
            exclude_prefix="mcp",
            model_set=model_config.model_task_config.tool_use,
        )
        self.mcp_executor = MCPToolExecutor(
            chat_id=self.chat_id,
            model_set=model_config.model_task_config.mcp,
            include_prefix="mcp",
            prompt_template="mcp_tool_executor_prompt",
        )
        self.web_search_manager = WebSearchManager(chat_id=chat_id, enable_cache=True, cache_ttl=2)
        self.capability_router = CapabilityRouter(chat_id=chat_id)
        self.url_fetcher = UrlContentFetcher()

    @property
    def planner_llm(self) -> LLMRequest:
        if has_active_focus_lease(self.chat_id) or (
            getattr(global_config.focus, "mode", "off") == "active"
            and focus_coordinator.is_managed(self.chat_id)
        ):
            return self.separated_llm
        if global_config.bot.integrated_plan:
            from src.manager.local_store_manager import local_storage
            group = local_storage[f"private_replyer_group_{self.chat_id}"]
            if isinstance(group, int) and group in [0, 1, 2]:
                model_set = model_config.model_task_config.get_private_replyer(group)
            else:
                model_set = model_config.model_task_config.replyer
            return LLMRequest(model_set=model_set, request_type="integrated_planner")
        return self.separated_llm

    def _check_sandbox_permission(self, user_id: str) -> bool:
        """Check if user has permission to use sandbox features"""
        is_admin = str(user_id) in global_config.advanced.admins
        is_whitelisted = str(user_id) in global_config.bot.sandbox_whitelist
        return is_admin or is_whitelisted

    def _check_mcp_permission(self, user_id: str) -> bool:
        """检查当前用户是否有权限使用 MCP 工具 (集成自 PrivateGenerator)"""
        try:
            if not user_id:
                return False

            config_path = os.path.join(os.getcwd(), "plugins", "MaiBot_MCPBridgePlugin", "config.toml")
            if not os.path.exists(config_path):
                return False

            with open(config_path, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)

            plugin_config = doc.get("plugin", {})
            if not plugin_config.get("enabled", True):
                return False

            permissions = doc.get("permissions", {})
            quick_allow_users_str = permissions.get("quick_allow_users", "")
            default_mode = permissions.get("perm_default_mode", "deny_all")

            allow_users = {u.strip() for u in quick_allow_users_str.strip().split("\n") if u.strip()}

            is_allowed = user_id in allow_users
            if is_allowed:
                return True

            if default_mode == "deny_all":
                return False
            return True
        except Exception as e:
            logger.debug(f"{self.log_prefix}MCP Permission Check Failed: {e}")
            return False

    def find_message_by_id(
        self, message_id: str, message_id_list: List[Tuple[str, "DatabaseMessages"]]
    ) -> Optional["DatabaseMessages"]:
        # sourcery skip: use-next
        """
        根据message_id从message_id_list中查找对应的原始消息

        Args:
            message_id: 要查找的消息ID
            message_id_list: 消息ID列表，格式为[{'id': str, 'message': dict}, ...]

        Returns:
            找到的原始消息字典，如果未找到则返回None
        """
        for item in message_id_list:
            if item[0] == message_id:
                return item[1]
        return None

    def _parse_single_action(
        self,
        action_json: dict,
        message_id_list: List[Tuple[str, "DatabaseMessages"]],
        current_available_actions: List[Tuple[str, ActionInfo]],
    ) -> List[ActionPlannerInfo]:
        """解析单个action JSON并返回ActionPlannerInfo列表"""
        action_planner_infos = []

        try:
            action = action_json.get("action", "no_action")
            reasoning = action_json.get("reason", "未提供原因")
            requested_switch = action == SWITCH_CHAT_ACTION
            reply_text = "" if requested_switch else action_json.get("text", "")
            if requested_switch and not can_offer_switch_chat(focus_coordinator, self.chat_id):
                logger.warning(f"{self.log_prefix} 当前上下文无权使用 switch_chat，回退为 reply")
                action = "reply"
                reasoning = f"当前上下文不允许跨会话切换，改为正常回复。原始理由: {reasoning}"
                action_data = {}
            else:
                action_data = (
                    normalize_switch_action_data(action_json)
                    if requested_switch
                    else {key: value for key, value in action_json.items() if key not in ["action", "reason", "text"]}
                )
            # 非no_action动作需要target_message_id
            latest_user_message = _pick_latest_user_message(message_id_list)
            target_message = None
            fallback_to_latest = False

            if action == SWITCH_CHAT_ACTION:
                target_message = None
            elif target_message_id := action_json.get("target_message_id"):
                # 根据target_message_id查找原始消息
                target_message = self.find_message_by_id(target_message_id, message_id_list)
                if target_message is None:
                    logger.warning(f"{self.log_prefix}无法找到target_message_id '{target_message_id}' 对应的消息")
                    fallback_to_latest = True
            else:
                fallback_to_latest = True
                logger.debug(f"{self.log_prefix}动作'{action}'缺少target_message_id，使用最新消息作为target_message")

            if fallback_to_latest:
                target_message = latest_user_message or (message_id_list[-1][1] if message_id_list else None)

            if _is_bot_message(target_message) and latest_user_message:
                target_message = latest_user_message
                logger.debug(f"{self.log_prefix}target_message为机器人消息，改为最新用户消息")

            # 验证action是否可用
            available_action_names = [action_name for action_name, _ in current_available_actions]
            internal_action_names = ["no_reply", "reply", "wait_time", "make_appoint", "cancel_appoint", SWITCH_CHAT_ACTION]

            if action not in internal_action_names and action not in available_action_names:
                invalid_action = action
                logger.warning(
                    f"{self.log_prefix}LLM 返回了当前不可用或无效的动作: '{invalid_action}' (可用: {available_action_names})，将强制使用 'reply'"
                )
                reasoning = (
                    f"LLM 返回了当前不可用的动作 '{invalid_action}' (可用: {available_action_names})，已改为回复。"
                    f" 原始理由: {reasoning}"
                )
                action = "reply"

            # 创建ActionPlannerInfo对象
            # 将列表转换为字典格式
            available_actions_dict = dict(current_available_actions)
            action_planner_infos.append(
                ActionPlannerInfo(
                    action_type=action,
                    reasoning=reasoning,
                    action_data=action_data,
                    action_message=target_message,
                    available_actions=available_actions_dict,
                    reply_text=reply_text,
                )
            )

        except Exception as e:
            logger.error(f"{self.log_prefix}解析单个action时出错: {e}")
            # 将列表转换为字典格式
            available_actions_dict = dict(current_available_actions)
            action_planner_infos.append(
                ActionPlannerInfo(
                    action_type="no_reply",
                    reasoning=f"解析单个action时出错: {e}",
                    action_data={},
                    action_message=None,
                    available_actions=available_actions_dict,
                )
            )

        return action_planner_infos

    async def plan(
        self,
        available_actions: Dict[str, ActionInfo],
        loop_start_time: float = 0.0,
        interrupt_flag: Optional[asyncio.Event] = None,
    ) -> Tuple[List[ActionPlannerInfo], Optional["DatabaseMessages"]]:
        # sourcery skip: use-named-expression
        """
        规划器 (Planner): 使用LLM根据上下文决定做出什么动作。
        """
        target_message: Optional["DatabaseMessages"] = None

        # 获取必要信息
        is_group_chat, chat_target_info, current_available_actions = self.get_necessary_info()
        context_size = global_config.chat.get_max_context_size(is_group_chat=is_group_chat)

        # 获取聊天上下文
        # 如果是私聊，使用完整的120条限制；如果是群聊，使用0.6倍限制以节省token
        limit = context_size if not is_group_chat else int(context_size * 0.6)
        _stepped_limit = get_stepped_limit(self.chat_id, time.time(), limit)
        message_list_before_now = get_raw_msg_before_timestamp_with_chat(
            chat_id=self.chat_id,
            timestamp=time.time(),
            limit=_stepped_limit,
        )
        if message_list_before_now:
            latest_message = message_list_before_now[-1]
            if _has_url_message(getattr(latest_message, "processed_plain_text", "") or "") and not _is_bot_message(
                latest_message
            ):
                reasoning = "检测到包含URL的消息，直接执行网页解析回复"
                action = ActionPlannerInfo(
                    action_type="reply",
                    reasoning=reasoning,
                    action_data={"loop_start_time": loop_start_time},
                    action_message=latest_message,
                    available_actions=available_actions,
                )
                return [action], latest_message
            # 检测提醒/预约关键词，强制选择 make_appoint
            latest_text = (getattr(latest_message, "processed_plain_text", "") or "").strip()
            if not _is_bot_message(latest_message) and latest_text:
                import re as _kw_re

                _remind_kw = _kw_re.search(r"提醒|叫我|闹钟|定时|记得叫|到时候|醒我", latest_text)
                _time_kw = _kw_re.search(
                    r"\d+\s*(分钟|小时|秒钟|秒|分|时|点)|后|明天|今晚|今天|下午|上午|晚上", latest_text
                )
                if _remind_kw and _time_kw:
                    logger.info(f"{self.log_prefix} 检测到提醒关键词，强制选择 make_appoint")
                    action = ActionPlannerInfo(
                        action_type="make_appoint",
                        reasoning="检测到提醒关键词，强制执行预约提醒",
                        action_data={
                            "loop_start_time": loop_start_time,
                            "remind_time": latest_text,
                            "remind_content": latest_text,
                        },
                        action_message=latest_message,
                        available_actions=available_actions,
                    )
                    return [action], latest_message

        message_id_list: list[Tuple[str, "DatabaseMessages"]] = []
        chat_content_block, message_id_list = build_readable_messages_with_id(
            messages=message_list_before_now,
            timestamp_mode="normal_no_YMD",
            read_mark=self.last_obs_time_mark,
            truncate=True,
            show_actions=True,
        )

        message_list_before_now_short = message_list_before_now[-int(context_size * 0.3) :]
        chat_content_block_short, message_id_list_short = build_readable_messages_with_id(
            messages=message_list_before_now_short,
            timestamp_mode="normal_no_YMD",
            truncate=False,
            show_actions=False,
        )

        self.last_obs_time_mark = time.time()

        # 应用激活类型过滤
        filtered_actions = self._filter_actions_by_activation_type(available_actions, chat_content_block_short)

        logger.debug(f"{self.log_prefix}过滤后有{len(filtered_actions)}个可用动作")

        # 构建包含所有动作和回复上下文的提示词
        focus_managed = has_active_focus_lease(self.chat_id) or (
            getattr(global_config.focus, "mode", "off") == "active"
            and focus_coordinator.is_managed(self.chat_id)
        )
        if global_config.bot.integrated_plan and not focus_managed:
            prompt, message_id_list = await self.build_integrated_planner_prompt(
                is_group_chat=is_group_chat,
                chat_target_info=chat_target_info,
                current_available_actions=filtered_actions,
                chat_content_block=chat_content_block,
                message_id_list=message_id_list,
                interest=global_config.personality.interest,
            )
        else:
            prompt, message_id_list = await self.build_planner_prompt(
                is_group_chat=is_group_chat,
                chat_target_info=chat_target_info,
                current_available_actions=filtered_actions,
                message_id_list=message_id_list,
                chat_content_block=chat_content_block,
                interest=global_config.personality.interest,
            )

        # 调用LLM获取决策
        actions = await self._execute_main_planner(
            prompt=prompt,
            message_id_list=message_id_list,
            filtered_actions=filtered_actions,
            available_actions=available_actions,
            loop_start_time=loop_start_time,
            interrupt_flag=interrupt_flag,
        )

        # 获取target_message（如果有非no_action的动作）
        non_no_actions = [a for a in actions if a.action_type != "no_reply"]
        if non_no_actions:
            target_message = non_no_actions[0].action_message

        return actions, target_message

    async def build_planner_prompt(
        self,
        is_group_chat: bool,
        chat_target_info: Optional["TargetPersonInfo"],
        current_available_actions: Dict[str, ActionInfo],
        message_id_list: List[Tuple[str, "DatabaseMessages"]],
        chat_content_block: str = "",
        interest: str = "",
    ) -> tuple[str, List[Tuple[str, "DatabaseMessages"]]]:
        """构建 Planner LLM 的提示词 (获取模板并填充数据)"""
        try:
            # 获取最近执行过的动作
            actions_before_now = get_actions_by_timestamp_with_chat(
                chat_id=self.chat_id,
                timestamp_start=time.time() - 600,
                timestamp_end=time.time(),
                limit=6,
            )
            actions_before_now_block = build_readable_actions(actions=actions_before_now)
            if actions_before_now_block:
                actions_before_now_block = f"你刚刚选择并执行过的action是：\n{actions_before_now_block}"
            else:
                actions_before_now_block = ""

            # 新用户在首次消息落库、注册完成前可能尚无 TargetPersonInfo；始终提供模板所需变量。
            chat_context_description = (
                "你现在正在一个群聊中"
                if is_group_chat
                else "你正在和一位尚未认识的用户私聊"
            )

            if chat_target_info:
                # 构建聊天上下文描述
                chat_context_description = (
                    f"你正在和 {resolve_sender_name(user_info=chat_target_info, fallback='对方')} 聊天中"
                )

            # 构建动作选项块
            action_options_block = await self._build_action_options_block(current_available_actions)

            # 构建待执行预约列表
            from src.chat.heart_flow.appointment_scheduler import appointment_scheduler

            pending_appointments = appointment_scheduler.get_pending(chat_id=self.chat_id)
            if pending_appointments:
                pending_text = ""
                for appt in pending_appointments:
                    pending_text += (
                        f"- {appt['remind_content']}（时间：{appt['remind_time_iso']}，用户：{appt['user_id']}）\n"
                    )
            else:
                pending_text = "无待执行预约"

            # 其他信息
            moderation_prompt_block = "请不要输出违法违规内容，不要输出暴力，政治相关内容，如有敏感内容，请规避。"
            time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            bot_name = global_config.bot.nickname
            bot_nickname = (
                f",也可以叫你{','.join(global_config.bot.alias_names)}" if global_config.bot.alias_names else ""
            )
            name_block = f"你的名字是{bot_name}{bot_nickname}，请注意哪些是你自己的发言。"

            # 获取主规划器模板并填充
            planner_prompt_template = await global_prompt_manager.get_prompt_async("brain_planner_prompt")
            prompt = planner_prompt_template.format(
                time_block=time_block,
                chat_context_description=chat_context_description,
                chat_content_block=chat_content_block,
                actions_before_now_block=actions_before_now_block,
                action_options_text=action_options_block,
                moderation_prompt=moderation_prompt_block,
                name_block=name_block,
                interest=interest,
                plan_style=global_config.personality.private_plan_style,
                pending_appointments=pending_text,
            )
            prompt += await render_switch_planner_context(focus_coordinator, self.chat_id)

            return prompt, message_id_list
        except Exception as e:
            logger.error(f"构建 Planner 提示词时出错: {e}")
            logger.error(traceback.format_exc())
            return "构建 Planner Prompt 时出错", []

    async def build_integrated_planner_prompt(
        self,
        is_group_chat: bool,
        chat_target_info: Optional["TargetPersonInfo"],
        current_available_actions: Dict[str, ActionInfo],
        message_id_list: List[Tuple[str, "DatabaseMessages"]],
        chat_content_block: str = "",
        interest: str = "",
    ) -> tuple[str, List[Tuple[str, "DatabaseMessages"]]]:
        """构建整合了动作和回复的提示词"""
        try:
            # 获取最近执行过的动作
            actions_before_now = get_actions_by_timestamp_with_chat(
                chat_id=self.chat_id,
                timestamp_start=time.time() - 600,
                timestamp_end=time.time(),
                limit=6,
            )
            actions_before_now_block = build_readable_actions(actions=actions_before_now)
            if actions_before_now_block:
                actions_before_now_block = f"你刚刚选择并执行过的action是：\n{actions_before_now_block}"
            else:
                actions_before_now_block = ""

            sender_name = "对方"
            if chat_target_info:
                sender_name = resolve_sender_name(user_info=chat_target_info, fallback="对方")

            # 构建动作选项块
            action_options_block = await self._build_action_options_block(current_available_actions)

            # 情绪和人格
            # from src.mood.mood_manager import mood_manager

            # if global_config.mood.enable_mood:
            #     chat_mood = mood_manager.get_mood_by_chat_id(self.chat_id)
            #     mood_prompt = chat_mood.mood_state

            # 取最后一条相关的消息文本作为 target，用于触发工具和记忆
            target = ""
            if len(message_id_list) > 0:
                last_msg = message_id_list[-1][1]
                target = last_msg.processed_plain_text
                target = replace_user_references(target, global_config.bot.platform, replace_bot_name=True)
                target = re.sub(r"\\[picid:[^\\]]+\\]", "[图片]", target)
                target, _, _ = guard_user_content(target, sender_name)

            # 使用更短的上下文来检索工具和记忆，避免由于上下文过长导致的检索噪音
            short_context_size = int(global_config.chat.get_max_context_size(is_group_chat) * 0.33)
            _stepped_limit_short = get_stepped_limit(self.chat_id, time.time(), short_context_size)
            message_list_before_short = get_raw_msg_before_timestamp_with_chat(
                chat_id=self.chat_id,
                timestamp=time.time(),
                limit=_stepped_limit_short,
            )
            chat_talking_prompt_short = build_readable_messages(
                message_list_before_short,
                replace_bot_name=True,
                timestamp_mode="relative",
                read_mark=0.0,
                show_actions=True,
            )

            # --- 下面使用 gathered 异步加载上下文所需信息 ---
            user_info = chat_target_info if chat_target_info else None

            task_results = await asyncio.gather(
                build_relation_info(chat_talking_prompt_short, sender_name, user_info=user_info),
                build_memory_retrieval_prompt(
                    message=chat_talking_prompt_short,
                    sender=sender_name,
                    target=target,
                    chat_stream=get_chat_manager().get_stream(self.chat_id),
                ),
                build_tool_info(
                    chat_history=chat_talking_prompt_short,
                    sender=sender_name,
                    target=target,
                    url_fetcher=self.url_fetcher,
                    web_search_manager=self.web_search_manager,
                    capability_router=self.capability_router,
                    tool_executor=self.tool_executor,
                    mcp_executor=self.mcp_executor,
                    has_mcp_permission=self._check_mcp_permission(
                        user_info.user_id if user_info and user_info.user_id else ""
                    ),
                    enable_tool=global_config.tool.enable_tool,
                ),
                build_lpmm_knowledge_info(
                    message=chat_talking_prompt_short,
                    sender=sender_name,
                    target=target,
                    tool_executor=self.tool_executor,
                ),
            )

            relation_info_block = task_results[0]
            memory_retrieval_block = task_results[1]
            tool_info_block = task_results[2]
            knowledge_prompt_block = task_results[3]

            bot_name = global_config.bot.nickname
            bot_nickname = (
                f",也有人叫你{','.join(global_config.bot.alias_names)}" if global_config.bot.alias_names else ""
            )
            prompt_personality = f"{render_dynamic_prompt_template(global_config.personality.personality)};"
            identity = f"你的名字是{bot_name}{bot_nickname}，你{prompt_personality}"

            # 其他块
            time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            moderation_prompt_block = "请不要输出违法违规内容，不要输出暴力，政治相关内容，如有敏感内容，请规避。"

            # 习惯
            from src.chat.express.expression_selector import expression_selector

            use_expression, _, _ = global_config.expression.get_expression_config_for_chat(self.chat_id)
            expression_habits_block = ""
            if use_expression:
                selected_expressions, _ = await expression_selector.select_suitable_expressions_llm(
                    self.chat_id, chat_content_block, max_num=8
                )
                if selected_expressions:
                    style_habits = [f"当{expr['situation']}时，使用 {expr['style']}" for expr in selected_expressions]
                    expression_habits_block = "在回复时,你可以参考以下的语言习惯，不要生硬使用：\n" + "\n".join(
                        style_habits
                    )

            # 获取整合模板并填充
            planner_prompt_template = await global_prompt_manager.get_prompt_async("brain_integrated_prompt")
            prompt = planner_prompt_template.format(
                knowledge_prompt=knowledge_prompt_block,
                memory_retrieval=memory_retrieval_block,
                relation_info_block=relation_info_block,
                tool_info_block=tool_info_block,
                extra_info_block="",
                expression_habits_block=expression_habits_block,
                sender_name=sender_name,
                time_block=time_block,
                dialogue_prompt=chat_content_block,
                actions_before_now_block=actions_before_now_block,
                action_options_text=action_options_block,
                reply_target_block=f"你正在和 {sender_name} 聊天",
                identity=identity,
                reply_style=global_config.personality.private_plan_style,
                keywords_reaction_prompt="",
                moderation_prompt=moderation_prompt_block,
            )

            return prompt, message_id_list
        except Exception as e:
            logger.error(f"构建整合提示词时出错: {e}")
            logger.error(traceback.format_exc())
            return "构建 Integrated Prompt 时出错", []

    def get_necessary_info(self) -> Tuple[bool, Optional["TargetPersonInfo"], Dict[str, ActionInfo]]:
        """
        获取 Planner 需要的必要信息
        """
        is_group_chat = True
        is_group_chat, chat_target_info = get_chat_type_and_target_info(self.chat_id)
        logger.debug(f"{self.log_prefix}获取到聊天信息 - 群聊: {is_group_chat}, 目标信息: {chat_target_info}")

        # Check permissions and filter actions before they even reach activation logic
        has_sandbox_permission = False
        if chat_target_info and chat_target_info.user_id:
            has_sandbox_permission = self._check_sandbox_permission(chat_target_info.user_id)

        current_available_actions_dict = self.action_manager.get_using_actions()

        # 获取完整的动作信息
        all_registered_actions: Dict[str, ActionInfo] = component_registry.get_components_by_type(  # type: ignore
            ComponentType.ACTION
        )
        current_available_actions = {}
        for action_name in current_available_actions_dict:
            if action_name == "file_edit" and not has_sandbox_permission:
                continue
            if action_name in all_registered_actions:
                current_available_actions[action_name] = all_registered_actions[action_name]
            else:
                logger.warning(f"{self.log_prefix}使用中的动作 {action_name} 未在已注册动作中找到")

        return is_group_chat, chat_target_info, current_available_actions

    def _filter_actions_by_activation_type(
        self, available_actions: Dict[str, ActionInfo], chat_content_block: str
    ) -> Dict[str, ActionInfo]:
        """根据激活类型过滤动作"""
        filtered_actions = {}

        for action_name, action_info in available_actions.items():
            if action_info.activation_type == ActionActivationType.NEVER:
                logger.debug(f"{self.log_prefix}动作 {action_name} 设置为 NEVER 激活类型，跳过")
                continue
            elif action_info.activation_type in [ActionActivationType.LLM_JUDGE, ActionActivationType.ALWAYS]:
                filtered_actions[action_name] = action_info
            elif action_info.activation_type == ActionActivationType.RANDOM:
                if random.random() < action_info.random_activation_probability:
                    filtered_actions[action_name] = action_info
            elif action_info.activation_type == ActionActivationType.KEYWORD:
                if action_info.activation_keywords:
                    for keyword in action_info.activation_keywords:
                        if keyword in chat_content_block:
                            filtered_actions[action_name] = action_info
                            break
            else:
                logger.warning(f"{self.log_prefix}未知的激活类型: {action_info.activation_type}，跳过处理")

        return filtered_actions

    async def _build_action_options_block(self, current_available_actions: Dict[str, ActionInfo]) -> str:
        # sourcery skip: use-join
        """构建动作选项块"""
        if not current_available_actions:
            return ""

        action_options_block = ""
        for action_name, action_info in current_available_actions.items():
            # 构建参数文本
            param_text = ""
            if action_info.action_parameters:
                param_text = "\n"
                for param_name, param_description in action_info.action_parameters.items():
                    param_text += f'    "{param_name}":"{param_description}"\n'
                param_text = param_text.rstrip("\n")

            # 构建要求文本
            require_text = ""
            for require_item in action_info.action_require:
                require_text += f"- {require_item}\n"
            require_text = require_text.rstrip("\n")

            # 获取动作提示模板并填充
            using_action_prompt = await global_prompt_manager.get_prompt_async("brain_action_prompt")
            using_action_prompt = using_action_prompt.format(
                action_name=action_name,
                action_description=action_info.description,
                action_parameters=param_text,
                action_require=require_text,
            )

            action_options_block += using_action_prompt

        return action_options_block

    async def _execute_main_planner(
        self,
        prompt: str,
        message_id_list: List[Tuple[str, "DatabaseMessages"]],
        filtered_actions: Dict[str, ActionInfo],
        available_actions: Dict[str, ActionInfo],
        loop_start_time: float,
        interrupt_flag: Optional[asyncio.Event] = None,
    ) -> List[ActionPlannerInfo]:
        """执行主规划器"""
        llm_content = None
        actions: List[ActionPlannerInfo] = []

        try:
            # 调用LLM
            llm_content, (reasoning_content, _, _) = await self.planner_llm.generate_response_async(
                prompt=prompt,
                interrupt_flag=interrupt_flag,
            )

            logger.info(f"{self.log_prefix}规划器原始提示词: {prompt}")
            logger.info(f"{self.log_prefix}规划器原始响应: {llm_content}")

            if reasoning_content:
                logger.info(f"{self.log_prefix}规划器推理: {reasoning_content}")
            else:
                logger.debug(f"{self.log_prefix}规划器原始提示词: {prompt}")
                logger.debug(f"{self.log_prefix}规划器原始响应: {llm_content}")
                if reasoning_content:
                    logger.debug(f"{self.log_prefix}规划器推理: {reasoning_content}")

        except ReqAbortException:
            raise
        except Exception as req_e:
            logger.error(f"{self.log_prefix}LLM 请求执行失败: {req_e}")
            return [
                ActionPlannerInfo(
                    action_type="no_reply",
                    reasoning=f"LLM 请求失败，模型出现问题: {req_e}",
                    action_data={},
                    action_message=None,
                    available_actions=available_actions,
                )
            ]

        # 解析LLM响应
        if llm_content:
            try:
                filtered_actions_list = list(filtered_actions.items())
                json_objects = self._extract_json_from_markdown(llm_content)
                if not json_objects:
                    raw_json_objects = self._extract_json_from_raw_content(llm_content)
                    json_objects = self._filter_raw_json_action_objects(raw_json_objects, filtered_actions_list)

                if json_objects:
                    logger.debug(f"{self.log_prefix}从响应中提取到{len(json_objects)}个JSON对象")
                    for json_obj in json_objects:
                        actions.extend(self._parse_single_action(json_obj, message_id_list, filtered_actions_list))
                else:
                    # 尝试解析为直接的JSON
                    logger.warning(f"{self.log_prefix}LLM没有返回可用动作: {llm_content}")
                    actions = self._create_no_reply("LLM没有返回可用动作", available_actions)

            except Exception as json_e:
                logger.warning(f"{self.log_prefix}解析LLM响应JSON失败 {json_e}. LLM原始输出: '{llm_content}'")
                actions = self._create_no_reply(f"解析LLM响应JSON失败: {json_e}", available_actions)
                traceback.print_exc()
        else:
            actions = self._create_no_reply("规划器没有获得LLM响应", available_actions)

        # 添加循环开始时间到所有非no_action动作
        for action in actions:
            action.action_data = action.action_data or {}
            action.action_data["loop_start_time"] = loop_start_time

        switch_actions = [action for action in actions if action.action_type == SWITCH_CHAT_ACTION]
        if switch_actions:
            if len(switch_actions) > 1 or len(actions) > 1:
                logger.warning(f"{self.log_prefix} switch_chat 是终止动作，丢弃本轮其余动作")
            actions = [switch_actions[0]]
            logger.info(f"{self.log_prefix}规划器选择终止动作 switch_chat")
            return actions

        logger.info(
            f"{self.log_prefix}规划器决定执行{len(actions)}个动作: {' '.join([a.action_type for a in actions])}"
        )

        # 防止规划器抽风：当 reply 动作数量 >= 3 时，强制回退为单个 reply
        reply_actions = [a for a in actions if a.action_type == "reply"]
        if len(reply_actions) >= 3:
            logger.warning(f"{self.log_prefix}规划器异常：选择了{len(reply_actions)}个reply动作，强制回退为1个reply")
            non_reply_actions = [a for a in actions if a.action_type != "reply"]
            actions = non_reply_actions + [reply_actions[0]]

        return actions

    def _create_no_reply(self, reasoning: str, available_actions: Dict[str, ActionInfo]) -> List[ActionPlannerInfo]:
        """创建no_action"""
        return [
            ActionPlannerInfo(
                action_type="no_reply",
                reasoning=reasoning,
                action_data={},
                action_message=None,
                available_actions=available_actions,
            )
        ]

    def _extract_json_from_markdown(self, content: str) -> List[dict]:
        # sourcery skip: for-append-to-extend
        """从Markdown格式的内容中提取JSON对象"""
        json_objects = []

        # 使用正则表达式查找```json包裹的JSON内容
        json_pattern = r"```json\s*(.*?)\s*```"
        matches = re.findall(json_pattern, content, re.DOTALL)

        for match in matches:
            try:
                # 清理可能的格式问题，不再使用不安全的正则移除注释，以免误伤类似 (///ω///) 的内容
                # json_repair 库原生支持处理 JSON 中的多行和单行注释
                if json_str := match.strip():
                    json_obj = json.loads(repair_json(json_str))
                    if isinstance(json_obj, dict):
                        json_objects.append(json_obj)
                    elif isinstance(json_obj, list):
                        for item in json_obj:
                            if isinstance(item, dict):
                                json_objects.append(item)
            except Exception as e:
                logger.warning(f"解析JSON块失败: {e}, 块内容: {match[:100]}...")
                continue

        return json_objects

    def _extract_json_from_raw_content(self, content: str) -> List[dict]:
        """Parse raw JSON responses that are not wrapped in Markdown fences."""
        raw_content = content.strip()
        if not raw_content:
            return []

        if not (
            (raw_content.startswith("{") and raw_content.endswith("}"))
            or (raw_content.startswith("[") and raw_content.endswith("]"))
        ):
            return []

        try:
            json_obj = json.loads(repair_json(raw_content))
            if isinstance(json_obj, dict):
                return [json_obj]
            if isinstance(json_obj, list):
                return [item for item in json_obj if isinstance(item, dict)]
        except Exception as e:
            logger.warning(f"解析裸JSON失败: {e}, 内容: {raw_content[:100]}...")

        return []

    def _filter_raw_json_action_objects(
        self,
        json_objects: List[dict],
        current_available_actions: List[Tuple[str, ActionInfo]],
    ) -> List[dict]:
        """Keep only supported actions from raw JSON fallback responses."""
        if not json_objects:
            return []

        available_action_names = [action_name for action_name, _ in current_available_actions]
        internal_action_names = ["no_reply", "reply", "wait_time", "make_appoint", "cancel_appoint", SWITCH_CHAT_ACTION]
        supported_actions = set(internal_action_names + available_action_names)
        filtered_objects = []

        for json_obj in json_objects:
            action = json_obj.get("action")

            if action in (None, "", "no_action"):
                filtered_objects.append(
                    {
                        "action": "no_reply",
                        "reason": json_obj.get("reason", "裸JSON响应未包含可执行动作"),
                    }
                )
                continue

            if action not in supported_actions:
                logger.warning(f"{self.log_prefix}裸JSON响应包含不支持的动作: '{action}'")
                continue

            filtered_objects.append(json_obj)

        return filtered_objects


init_prompt()
