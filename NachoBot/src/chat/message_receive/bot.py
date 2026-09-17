import traceback
import os
import re
import time
import asyncio
import json

from typing import Dict, Any, Optional
from ncnk_message import (
    Seg,
    UserInfo,
    SystemEventState,
    classify_system_event_route,
    classify_system_event,
    get_system_event,
    system_event_fallback_text,
)

from src.common.logger import get_logger
from src.config.config import global_config
from src.mood.mood_manager import mood_manager  # 导入情绪管理器
from src.chat.message_receive.chat_stream import get_chat_manager, ChatStream
from src.chat.message_receive.message import MessageRecv
from src.chat.message_receive.storage import MessageStorage
from src.chat.replyer.sandbox_callback import consume_sandbox_callback_reply
from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.advanced.advanced_manager import advanced_manager
from src.plugin_system.core import component_registry, events_manager, global_announcement_manager
from src.plugin_system.base import BaseCommand, EventType
from src.plugin_system.apis import database_api, send_api
from src.live.platform_event_tracker import track_platform_event
from src.chat.keyword_cache import promise_cache_manager
from src.person_info.bind_manager import bind_manager  # 导入多平台绑定管理器

# 定义日志配置

# 获取项目根目录（假设本文件在src/chat/message_receive/下，根目录为上上上级目录）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))

# 配置主程序日志格式
logger = get_logger("chat")


def _routing_user_info_for_system_event(message: MessageRecv, event: dict[str, Any]) -> UserInfo | None:
    """Validate an explicit private route and build a routing-only identity.

    Structured events never put this identity back into
    ``message.message_info.user_info``.  Group events remain keyed solely by
    their group metadata and therefore do not require a route.
    """

    if message.message_info.group_info is not None:
        return None

    route_result = classify_system_event_route(message)
    if not route_result.is_valid or route_result.route is None:
        raise ValueError("private system_event requires a valid system_event_route")

    route = route_result.route
    platform = str(message.message_info.platform or "")
    if route.get("platform") != platform:
        raise ValueError("system_event_route platform does not match message platform")

    peer = route.get("peer")
    peer_id = str(peer.get("user_id", "")).strip() if isinstance(peer, dict) else ""
    if not peer_id:
        raise ValueError("system_event_route peer user_id is required")

    if event.get("type") == "qq.poke":
        actor = event.get("actor")
        actor_id = str(actor.get("user_id", "")).strip() if isinstance(actor, dict) else ""
        if not actor_id or actor_id != peer_id:
            raise ValueError("qq.poke route peer user_id must match actor user_id")

    nickname = (
        (peer.get("nickname") or peer.get("name"))
        if isinstance(peer, dict)
        else None
    )
    cardname = peer.get("cardname") if isinstance(peer, dict) else None
    return UserInfo(
        platform=platform,
        user_id=peer_id,
        user_nickname=nickname or peer_id,
        user_cardname=cardname,
    )


def _check_ban_words(text: str, chat: ChatStream, userinfo: Optional[UserInfo]) -> bool:
    """检查消息是否包含过滤词。system_event 允许没有 sender。"""
    for word in global_config.message_receive.ban_words:
        if word in text:
            chat_name = chat.group_info.group_name if chat.group_info else "私聊"
            if userinfo is None:
                logger.info(f"[{chat_name}][系统事件] {text}")
            else:
                logger.info(f"[{chat_name}]{userinfo.user_nickname}:{text}")
            logger.info(f"[过滤词识别]消息中含有{word}，filtered")
            return True
    return False


def _check_ban_regex(text: str, chat: ChatStream, userinfo: Optional[UserInfo]) -> bool:
    """检查消息是否匹配过滤正则表达式。system_event 允许没有 sender。"""
    if text is None or not text:
        return False

    for pattern in global_config.message_receive.ban_msgs_regex:
        if re.search(pattern, text):
            chat_name = chat.group_info.group_name if chat.group_info else "私聊"
            if userinfo is None:
                logger.info(f"[{chat_name}][系统事件] {text}")
            else:
                logger.info(f"[{chat_name}]{userinfo.user_nickname}:{text}")
            logger.info(f"[正则表达式过滤]消息匹配到{pattern}，filtered")
            return True
    return False


class ChatBot:
    def __init__(self):
        self.bot = None  # bot 实例引用
        self._started = False
        self.mood_manager = mood_manager  # 获取情绪管理器单例
        self.heartflow_message_receiver = HeartFCMessageReceiver()  # 新增

    async def _ensure_started(self):
        """确保所有任务已启动"""
        if not self._started:
            logger.debug("确保ChatBot所有任务已启动")

            self._started = True

    async def _process_commands_with_new_system(self, message: MessageRecv):
        # sourcery skip: use-named-expression
        """使用新插件系统处理命令"""
        try:
            text = message.processed_plain_text or ""
            raw_text = message.raw_message if isinstance(message.raw_message, str) else None
            message.force_command = False

            force_suffix_pattern = r"\s+-force\s*$"
            force_requested = False
            command_text = text
            raw_command_text = raw_text
            if text and re.search(force_suffix_pattern, text, re.IGNORECASE):
                force_requested = True
                command_text = re.sub(force_suffix_pattern, "", text, flags=re.IGNORECASE)
                if raw_text is not None:
                    raw_command_text = re.sub(force_suffix_pattern, "", raw_text, flags=re.IGNORECASE)

            user_info = getattr(message.message_info, "user_info", None)
            user_id = getattr(user_info, "user_id", None) if user_info else None
            force_allowed = force_requested and advanced_manager.is_admin(user_id)

            def _apply_command_text():
                message.processed_plain_text = command_text
                if raw_command_text is not None:
                    message.raw_message = raw_command_text

            def _apply_force_command():
                message.force_command = True
                _apply_command_text()

            if command_text:
                stripped = command_text.strip().lower()
                if stripped in ["#adv_on", "#adv_off"]:
                    message.is_command = True
                    if force_requested:
                        _apply_command_text()
                    if not advanced_manager.is_allowed(user_id):
                        await send_api.text_to_stream(
                            "现在的关系还不能使用此指令哦~(´-ω-`)", message.chat_stream.stream_id
                        )
                        return True, "not allowed", False
                    is_group = bool(message.chat_stream and message.chat_stream.group_info)
                    if is_group and not force_allowed:
                        await send_api.text_to_stream("笨蛋，这里是群里喵~(´-ω-`)", message.chat_stream.stream_id)
                        return True, "group not allowed", False

                    enabled = stripped == "#adv_on"
                    if force_allowed:
                        _apply_force_command()
                    if is_group:
                        group_id = message.chat_stream.group_info.group_id
                        advanced_manager.set_group_state(message.chat_stream.platform, str(group_id), enabled)
                        reply_text = (
                            "高级模式已开启，请尽情使唤NachoBot哦~"
                            if enabled
                            else "高级模式已关闭，tts及工具调用等功能已恢复"
                        )
                    else:
                        advanced_manager.set_state(str(user_id), enabled, stream_id=message.chat_stream.stream_id)
                        reply_text = (
                            "高级模式已开启，请尽情使唤NachoBot哦~"
                            if enabled
                            else "高级模式已关闭，tts及工具调用等功能已恢复"
                        )
                    await send_api.text_to_stream(reply_text, message.chat_stream.stream_id)
                    return True, reply_text, False

                if stripped == "#adv_check":
                    message.is_command = True
                    if force_requested:
                        _apply_command_text()
                    if not advanced_manager.is_admin(user_id):
                        await send_api.text_to_stream(
                            "这是只有给主人才能看的东西哦~(´-ω-`)", message.chat_stream.stream_id
                        )
                        return True, "not admin", False
                    if message.chat_stream and message.chat_stream.group_info and not force_allowed:
                        await send_api.text_to_stream("注意隐私哦，主人~(´-ω-`)", message.chat_stream.stream_id)
                        return True, "group not allowed", False
                    enabled_users = advanced_manager.list_enabled_users()
                    enabled_groups = advanced_manager.list_enabled_groups()
                    enabled_ids = enabled_users + enabled_groups
                    reply_text = "主人，当前开启高级模式的用户: " + (", ".join(enabled_ids) if enabled_ids else "无")
                    if force_allowed:
                        _apply_force_command()
                    await send_api.text_to_stream(reply_text, message.chat_stream.stream_id)
                    return True, reply_text, False

                if stripped == "#check_blocked_user":
                    if message.chat_stream and message.chat_stream.group_info:
                        message.is_command = True
                        if force_requested:
                            _apply_command_text()
                        stream_id = message.chat_stream.stream_id
                        from src.chat.heart_flow.heartflow import heartflow
                        from src.chat.heart_flow.heartFC_chat import HeartFChatting

                        hfc = heartflow.heartflow_chat_list.get(stream_id)
                        if not hfc or not isinstance(hfc, HeartFChatting):
                            await send_api.text_to_stream(
                                "当前聊天没有活跃的心流实例或不支持屏蔽功能。",
                                stream_id,
                            )
                            return True, "no heartflow", False
                        # 清理过期条目
                        now = time.time()
                        expired = [uid for uid, exp in hfc.blocked_users.items() if now > exp]
                        for uid in expired:
                            del hfc.blocked_users[uid]
                        if not hfc.blocked_users:
                            await send_api.text_to_stream("当前没有被屏蔽的用户", stream_id)
                            return True, "no blocked users", False
                        lines = ["当前被屏蔽的用户："]
                        for user_id_str, expire_time in hfc.blocked_users.items():
                            remaining = expire_time - now
                            if remaining <= 0:
                                continue
                            minutes = int(remaining // 60)
                            seconds = int(remaining % 60)
                            lines.append(f"- QQ: {user_id_str}，剩余 {minutes}分{seconds}秒")
                        if len(lines) == 1:
                            await send_api.text_to_stream("当前没有被屏蔽的用户", stream_id)
                            return True, "no blocked users", False
                        await send_api.text_to_stream("\n".join(lines), stream_id)
                        return True, "blocked users listed", False

                # #unban_<QQ号>: 管理员立即解除指定用户的屏蔽
                unban_match = re.match(r"^#unban_(\d+)$", stripped)
                if unban_match:
                    if message.chat_stream and message.chat_stream.group_info:
                        message.is_command = True
                        if force_requested:
                            _apply_command_text()
                        target_qq = unban_match.group(1)
                        if not advanced_manager.is_admin(user_id):
                            await send_api.text_to_stream(
                                "只有管理员才能使用此指令哦~(´-ω-`)",
                                message.chat_stream.stream_id,
                            )
                            return True, "not admin", False
                        stream_id = message.chat_stream.stream_id
                        from src.chat.heart_flow.heartflow import heartflow
                        from src.chat.heart_flow.heartFC_chat import HeartFChatting

                        hfc = heartflow.heartflow_chat_list.get(stream_id)
                        if not hfc or not isinstance(hfc, HeartFChatting):
                            await send_api.text_to_stream(
                                "当前聊天没有活跃的心流实例。",
                                stream_id,
                            )
                            return True, "no heartflow", False
                        if target_qq in hfc.blocked_users:
                            del hfc.blocked_users[target_qq]
                            await send_api.text_to_stream(
                                f"已解除对用户 {target_qq} 的屏蔽",
                                stream_id,
                            )
                            return True, f"unblocked {target_qq}", False
                        else:
                            await send_api.text_to_stream(
                                f"用户 {target_qq} 当前未被屏蔽。",
                                stream_id,
                            )
                            return True, "not blocked", False

                # ========== 多平台账号绑定拦截 ==========
                # 1. 拦截发起绑定指令：#bind_平台_账号
                bind_match = re.match(r"^#bind_([a-zA-Z0-9]+)_(.+)$", stripped)
                if bind_match:
                    message.is_command = True
                    target_platform = bind_match.group(1)
                    # 从原始大小写文本中提取 target_user_id，避免 .lower() 破坏标识符大小写
                    original_bind_match = re.match(r"^#bind_([a-zA-Z0-9]+)_(.+)$", command_text.strip(), re.IGNORECASE)
                    target_user_id = original_bind_match.group(2) if original_bind_match else bind_match.group(2)

                    # 从 message 中获取当前用户的 person_id
                    from src.person_info.person_info import get_person_id

                    current_person_id = get_person_id(message.message_info.platform, user_id)

                    # 生成验证码
                    result = bind_manager.request_bind(current_person_id, target_platform, target_user_id)

                    if result == "ERR_ALREADY_BOUND":
                        reply_text = (
                            f"✨ 账号 {target_platform}:{target_user_id} 已经与当前身份绑定过啦，无需重复绑定哦~"
                        )
                        await send_api.text_to_stream(reply_text, message.chat_stream.stream_id)
                        return True, "already bound", False

                    if result == "ERR_TARGET_TAKEN":
                        reply_text = f"绑定失败：账号 {target_platform}:{target_user_id} 已经被其他身份占用了。如需解绑请联系管理员。"
                        await send_api.text_to_stream(reply_text, message.chat_stream.stream_id)
                        return True, "target taken", False

                    if result == "ERR_PLATFORM_CONFLICT":
                        reply_text = "绑定失败：身份冲突啦！当前操作会导致同一个身份下出现多个同一平台的账号，这是不被允许的哦~"
                        await send_api.text_to_stream(reply_text, message.chat_stream.stream_id)
                        return True, "platform conflict", False

                    auth_code = result
                    reply_text = (
                        f"绑定请求已生成。\n"
                        f"请在 5 分钟内，使用你的 {target_platform} 账号（{target_user_id}）\n"
                        f"向我发送以下验证码（忽略大小写）：\n\n"
                        f"{auth_code}\n\n"
                        f"完成验证后，该平台的数据与记忆将与当前账号自动互通。"
                    )
                    await send_api.text_to_stream(reply_text, message.chat_stream.stream_id)
                    return True, "bind request created", False

                # 2. 拦截验证码提交：<平台>-<5位数字>
                # 把文本清洗和严格匹配交给 bind_manager 处理
                success, msg = bind_manager.confirm_bind(message.message_info.platform, user_id, stripped)
                if success:
                    message.is_command = True
                    await send_api.text_to_stream(msg, message.chat_stream.stream_id)
                    return True, "bind confirmed", False
                elif msg != "":
                    # 如果返回了具体的错误提示（说明格式完全正确，但是验证码不对/过期）才拦截
                    message.is_command = True
                    await send_api.text_to_stream(msg, message.chat_stream.stream_id)
                    return True, "bind failed", False
                # 如果 msg == ""，说明完全不符合验证码格式（如 SB-114514），直接放行，当作普通消息

                # 3. 拦截解除绑定指令：#unbind_平台_账号
                unbind_match = re.match(r"^#unbind_([a-zA-Z0-9]+)_(.+)$", stripped)
                if unbind_match:
                    message.is_command = True
                    if force_requested:
                        _apply_command_text()
                    if not advanced_manager.is_admin(user_id):
                        await send_api.text_to_stream(
                            "只有管理员才能使用解绑指令哦~(´-ω-`)",
                            message.chat_stream.stream_id,
                        )
                        return True, "unbind not admin", False

                    target_platform = unbind_match.group(1)
                    # 从原始大小写文本中提取 target_user_id
                    original_unbind_match = re.match(
                        r"^#unbind_([a-zA-Z0-9]+)_(.+)$", command_text.strip(), re.IGNORECASE
                    )
                    target_user_id = original_unbind_match.group(2) if original_unbind_match else unbind_match.group(2)
                    success, msg = bind_manager.admin_unbind(target_platform, target_user_id)

                    await send_api.text_to_stream(msg, message.chat_stream.stream_id)
                    return True, "unbind executed", False

                # 4. 拦截查询绑定状态指令：#check_binding
                if stripped == "#check_binding":
                    message.is_command = True
                    from src.person_info.person_info import get_person_id

                    current_person_id = get_person_id(message.message_info.platform, user_id)
                    reply_text = bind_manager.check_binding(current_person_id)
                    await send_api.text_to_stream(reply_text, message.chat_stream.stream_id)
                    return True, "check binding", False

            # 使用新的组件注册中心查找命令
            command_result = component_registry.find_command_by_text(command_text)
            if command_result:
                command_class, matched_groups, command_info = command_result
                plugin_name = command_info.plugin_name
                command_name = command_info.name
                if (
                    message.chat_stream
                    and message.chat_stream.stream_id
                    and command_name
                    in global_announcement_manager.get_disabled_chat_commands(message.chat_stream.stream_id)
                    and not force_allowed
                ):
                    logger.info("用户禁用的命令，跳过处理")
                    return False, None, True

                message.is_command = True
                if force_allowed:
                    _apply_force_command()
                elif force_requested:
                    _apply_command_text()

                # 获取插件配置
                plugin_config = component_registry.get_plugin_config(plugin_name)

                # 创建命令实例
                command_instance: BaseCommand = command_class(message, plugin_config)
                command_instance.set_matched_groups(matched_groups)

                try:
                    # 执行命令
                    success, response, intercept_message = await command_instance.execute()

                    # 记录命令执行结果
                    if success:
                        logger.info(f"命令执行成功: {command_class.__name__} (拦截: {intercept_message})")
                    else:
                        logger.warning(f"命令执行失败: {command_class.__name__} - {response}")

                    # 根据命令的拦截设置决定是否继续处理消息
                    return True, response, not intercept_message  # 找到命令，根据intercept_message决定是否继续

                except Exception as e:
                    logger.error(f"执行命令时出错: {command_class.__name__} - {e}")
                    logger.error(traceback.format_exc())

                    try:
                        await command_instance.send_text(f"命令执行出错: {str(e)}")
                    except Exception as send_error:
                        logger.error(f"发送错误消息失败: {send_error}")

                    # 命令出错时，根据命令的拦截设置决定是否继续处理消息
                    return True, str(e), False  # 出错时继续处理消息

            # 近似匹配提示：以 # 开头但未匹配到命令
            if command_text and command_text.strip().startswith("#"):
                suggestions = component_registry.suggest_command(command_text, max_suggestions=2, cutoff=0.75)
                if suggestions:
                    message.is_command = True
                    stream_id = message.chat_stream.stream_id if message.chat_stream else None
                    suggestion_text = "笨蛋，错误的指令是执行不了的哦(´-ω-`) 你是不是想输入：" + " 或 ".join(
                        f"#{cmd}" for cmd in suggestions
                    )
                    if stream_id:
                        await send_api.text_to_stream(suggestion_text, stream_id)
                    return True, suggestion_text, False

            # 没有找到命令，继续处理消息
            return False, None, True

        except Exception as e:
            logger.error(f"处理命令时出错: {e}")
            return False, None, True  # 出错时继续处理消息

    async def handle_notice_message(self, message: MessageRecv) -> bool:
        system_event = get_system_event(message)
        is_system_event = system_event is not None
        is_legacy_notice = message.message_info.message_id == "notice"

        if not is_system_event and not is_legacy_notice:
            return False

        # 旧 notice 使用固定 message_id，会被 message_repository 判重；继续为其生成唯一 ID。
        # 其他适配器的 system_event 若已有稳定 message_id，则保持原值。
        if is_legacy_notice:
            message.message_info.message_id = f"notice_{int(time.time() * 1000)}"

        message.is_notify = True

        if is_system_event:
            logger.info(
                "系统事件处理: type=%s, message_id=%s",
                system_event.get("type"),
                message.message_info.message_id,
            )
            event_data = system_event.get("data")
            fast_poke = event_data.get("fast_poke") if isinstance(event_data, dict) else None
            if isinstance(fast_poke, dict) and fast_poke.get("triggered"):
                logger.info(
                    "快速回戳事件: result=%s, message_id=%s",
                    fast_poke.get("result", "unknown"),
                    message.message_info.message_id,
                )
        else:
            logger.info(f"notice消息处理: {message.message_info.message_id}")

        return True

    async def _register_fast_poke_action(self, message: MessageRecv, chat: ChatStream) -> bool:
        """Persist an adapter fast-poke as a Bot action in this chat's context.

        Napcat performs this shortcut before Core receives the structured event,
        so the normal action executor never sees it.  The event metadata is the
        acknowledgement boundary: only an explicitly triggered fast-poke is
        registered, and the system-event message id provides an idempotent
        action key if the same in-memory message is processed again.
        """

        system_event = get_system_event(message)
        if system_event is None or system_event.get("type") != "qq.poke":
            return False

        event_data = system_event.get("data")
        fast_poke = event_data.get("fast_poke") if isinstance(event_data, dict) else None
        if not isinstance(fast_poke, dict) or fast_poke.get("triggered") is not True:
            return False

        result = str(fast_poke.get("result") or "unknown")
        actor = system_event.get("actor")
        actor_data = dict(actor) if isinstance(actor, dict) else {}
        actor_label = str(
            actor_data.get("name")
            or actor_data.get("nickname")
            or actor_data.get("user_id")
            or "对方"
        )
        result_descriptions = {
            "success": f"你快速回戳了{actor_label}",
            "failed": f"你尝试快速回戳{actor_label}，但平台返回失败",
            "timeout": f"你尝试快速回戳{actor_label}，但平台响应超时",
            "error": f"你尝试快速回戳{actor_label}，但执行异常",
            "pending": f"你正在尝试快速回戳{actor_label}",
        }
        action_prompt_display = result_descriptions.get(
            result,
            f"你尝试快速回戳{actor_label}，结果未知",
        )

        message_id = str(message.message_info.message_id or "unknown")
        stream_id = str(getattr(chat, "stream_id", "") or "unknown")
        action_id = f"system_event.fast_poke:{stream_id}:{message_id}"
        action_data = {
            "source": "system_event.fast_poke",
            "system_event_type": system_event.get("type"),
            "system_event_message_id": message_id,
            "result": result,
            "actor": actor_data or None,
        }
        group_info = getattr(message.message_info, "group_info", None)
        if group_info is not None and getattr(group_info, "group_id", None) is not None:
            action_data["group_id"] = str(group_info.group_id)

        stored_action = await database_api.store_action_info(
            chat_stream=chat,
            action_build_into_prompt=True,
            action_prompt_display=action_prompt_display,
            action_done=result == "success",
            thinking_id=action_id,
            action_data=action_data,
            action_name="active_poke",
        )
        if stored_action is None:
            logger.warning(
                "快速回戳 Bot 动作注册失败: result=%s, message_id=%s",
                result,
                message_id,
            )
            return False

        logger.info(
            "快速回戳已注册为 Bot 动作: result=%s, message_id=%s",
            result,
            message_id,
        )
        return True

    async def echo_message_process(self, raw_data: Dict[str, Any]) -> None:
        """
        用于专门处理回送消息ID的函数
        """
        message_data: Dict[str, Any] = raw_data.get("content", {})
        if not message_data:
            return
        message_type = message_data.get("type")
        if message_type != "echo":
            return
        mmc_message_id = message_data.get("echo")
        actual_message_id = message_data.get("actual_id")
        echo_platform = raw_data.get("platform")
        if not isinstance(echo_platform, str) or not echo_platform:
            echo_platform = message_data.get("platform")

        # Resolve the receipt only when both the message id and the platform
        # match a pending send.  A mismatched platform must not satisfy a
        # waiter belonging to another adapter route.
        ack_resolved = send_api.resolve_message_ack(mmc_message_id, echo_platform, actual_message_id)
        stored = MessageStorage.update_message(mmc_message_id, actual_message_id)
        if stored:
            logger.debug(f"更新消息ID成功: {mmc_message_id} -> {actual_message_id}")
        elif ack_resolved:
            # Media receipts may deliberately use storage_message=False.  The
            # upstream ACK is still valid even though there is no DB row.
            logger.debug(f"消息已收到平台ACK但未存储消息: {mmc_message_id}")
        else:
            logger.warning(f"更新消息ID失败: {mmc_message_id} -> {actual_message_id}")

    async def message_process(self, message_data: Dict[str, Any]) -> None:
        """处理转化后的统一格式消息
        这个函数本质是预处理一些数据，根据配置信息和消息内容，预处理消息，并分发到合适的消息处理器中
        heart_flow模式：使用思维流系统进行回复
        - 包含思维流状态管理
        - 在回复前进行观察和状态更新
        - 回复后更新思维流状态
        - 消息过滤
        - 记忆激活
        - 意愿计算
        - 消息生成和发送
        - 表情包处理
        - 性能计时
        """
        try:
            # 确保所有任务已启动
            await self._ensure_started()

            platform = message_data["message_info"].get("platform")

            # Debug Log: Trace incoming platform
            logger.debug(f"Incoming Message Platform: {platform}, Message Type: {message_data.get('type')}")
            logger.debug(f"Full message data: {message_data}")

            if message_data["message_info"].get("group_info") is not None:
                message_data["message_info"]["group_info"]["group_id"] = str(
                    message_data["message_info"]["group_info"]["group_id"]
                )
            if message_data["message_info"].get("user_info") is not None:
                message_data["message_info"]["user_info"]["user_id"] = str(
                    message_data["message_info"]["user_info"]["user_id"]
                )
            # print(message_data)
            # logger.debug(str(message_data))
            message = MessageRecv(message_data)
            # Classify immediately after construction.  A malformed envelope or
            # sender-bearing event is never allowed to fall through as an ordinary
            # user message, and no user-message-only hooks may run first.
            system_event_result = classify_system_event(message)
            message.system_event_state = system_event_result.state
            is_system_event = system_event_result.state is SystemEventState.VALID
            if system_event_result.state is SystemEventState.INVALID:
                logger.warning("丢弃包含无效 structured system_event 的消息")
                return
            routing_user_info = None
            if is_system_event:
                if message.message_info.user_info is not None:
                    logger.warning("丢弃同时带有 user_info 的 structured system_event 消息")
                    return
                if message.message_info.sender_info is not None:
                    logger.warning("丢弃同时带有 sender_info 的 structured system_event 消息")
                    return
                try:
                    routing_user_info = _routing_user_info_for_system_event(
                        message,
                        system_event_result.event,  # type: ignore[arg-type]
                    )
                except ValueError as exc:
                    logger.warning("丢弃无法确定路由的 structured system_event 消息: %s", exc)
                    return
            if is_system_event:
                # Normalize the validated envelope while retaining every unrelated
                # additional_config key.  This is the sole Core-side contract edge.
                additional_config = message.message_info.additional_config
                if isinstance(additional_config, str):
                    try:
                        additional_config = json.loads(additional_config)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        additional_config = {}
                if not isinstance(additional_config, dict):
                    additional_config = {}
                additional_config["system_event"] = system_event_result.event
                if routing_user_info is not None:
                    route_result = classify_system_event_route(additional_config)
                    if route_result.is_valid and route_result.route is not None:
                        additional_config["system_event_route"] = route_result.route
                message.message_info.additional_config = additional_config
            else:
                # Activity accounting is a user-message concern.  Keep it
                # after the shared classifier so malformed and sender-bearing
                # system-event payloads cannot update ordinary activity state.
                promise_cache_manager.touch_activity()

            group_info = message.message_info.group_info
            user_info = message.message_info.user_info

            if not is_system_event:
                continue_flag, modified_message = await events_manager.handle_nacho_events(
                    EventType.ON_MESSAGE_PRE_PROCESS, message
                )
                if not continue_flag:
                    return
                if modified_message and modified_message._modify_flags.modify_message_segments:
                    message.message_segment = Seg(type="seglist", data=modified_message.message_segments)

            if await self.handle_notice_message(message):
                # return
                pass

            get_chat_manager().register_message(
                message,
                routing_user_info=routing_user_info,
            )

            chat = await get_chat_manager().get_or_create_stream(
                platform=message.message_info.platform,  # type: ignore
                user_info=user_info,  # type: ignore
                group_info=group_info,
                message=message,
                routing_user_info=routing_user_info,
            )

            message.update_chat_stream(chat)

            if is_system_event:
                # Adapter-side shortcuts bypass the Core action executor.  Register
                # their acknowledged result after routing so the action is tied to
                # the correct group/private stream and is visible to this turn's
                # Planner/Replyer context.
                await self._register_fast_poke_action(message, chat)

            # 处理消息内容，生成纯文本
            await message.process()
            if is_system_event and not (message.processed_plain_text or "").strip():
                message.processed_plain_text = system_event_fallback_text(system_event_result.event)

            # 全局监听：处理适配器声明的打赏/会员事件
            asyncio.create_task(track_platform_event(message))

            if not is_system_event:
                # 约定/誓言缓存处理
                promise_cache_hits = promise_cache_manager.handle_message(message)
                if promise_cache_hits:
                    message.promise_cache_hits = promise_cache_hits  # 动态附加，供后续流程使用

            # if await self.check_ban_content(message):
            #     logger.warning(f"检测到消息中含有违法，色情，暴力，反动，敏感内容，消息内容：{message.processed_plain_text}，发送者：{message.message_info.user_info.user_nickname}")
            #     return

            # 过滤检查
            if not is_system_event and (
                _check_ban_words(message.processed_plain_text, chat, user_info)
                or _check_ban_regex(message.raw_message, chat, user_info)
            ):
                return

            if not is_system_event and await consume_sandbox_callback_reply(message, chat):
                await MessageStorage.store_message(message, chat)
                logger.info("replyer 已将 sandbox CALL_BACK 用户答复回传，跳过普通消息处理")
                return

            if is_system_event:
                is_command, cmd_result, continue_process = False, None, True
            else:
                # 命令处理 - 使用新插件系统检查并处理命令
                is_command, cmd_result, continue_process = await self._process_commands_with_new_system(message)

            # 如果是命令且不需要继续处理，则直接返回
            if is_command and not continue_process:
                await MessageStorage.store_message(message, chat)
                logger.info(f"命令处理完成，跳过后续消息处理: {cmd_result}")
                return

            if not is_system_event:
                continue_flag, modified_message = await events_manager.handle_nacho_events(EventType.ON_MESSAGE, message)
                if not continue_flag:
                    return
                if modified_message and modified_message._modify_flags.modify_plain_text:
                    message.processed_plain_text = modified_message.plain_text

            # 确认从接口发来的message是否有自定义的prompt模板信息
            if message.message_info.template_info and not message.message_info.template_info.template_default:
                template_group_name: Optional[str] = message.message_info.template_info.template_name  # type: ignore
                template_items = message.message_info.template_info.template_items
                async with global_prompt_manager.async_message_scope(template_group_name):
                    if isinstance(template_items, dict):
                        for k in template_items.keys():
                            await Prompt.create_async(template_items[k], k)
            else:
                template_group_name = None

            async def preprocess():
                await self.heartflow_message_receiver.process_message(message)

            if template_group_name:
                async with global_prompt_manager.async_message_scope(template_group_name):
                    await preprocess()
            else:
                await preprocess()

        except Exception as e:
            logger.error(f"预处理消息失败: {e}")
            traceback.print_exc()


# 创建全局ChatBot实例
chat_bot = ChatBot()
