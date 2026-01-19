import os
from pathlib import Path
from typing import List, Tuple, Type, Optional
from src.plugin_system import (
    BasePlugin, register_plugin, BaseAction, BaseCommand, 
    ComponentInfo, ActionActivationType
)
from src.plugin_system.base.config_types import ConfigField
import requests
import json
import re
import logging
from src.plugin_system.core.plugin_manager import plugin_manager
import toml
import shutil
import http.client
import random

logger = logging.getLogger("poke_plugin")

def match_poke_keyword(text: str) -> Optional[str]:
    keywords = [r"戳我"]
    for kw in keywords:
        if re.search(kw, text, re.IGNORECASE):
            return kw
    return None

@register_plugin
class PokePlugin(BasePlugin):
    """QQ戳一戳功能插件，支持群聊和好友戳一戳"""
    plugin_name = "poke_plugin"
    plugin_description = "QQ戳一戳功能插件，支持群聊和好友戳一戳"
    plugin_version = "0.4.2"
    plugin_author = "Neorestim"
    enable_plugin = True
    config_file_name = "config.toml"
    dependencies = []
    python_dependencies = []
    config_section_descriptions = {
        "plugin": "插件启用配置",
        "poke": "戳戳功能配置"
    }
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件")
        },
        "poke": {
            "reaction_probability": ConfigField(type=float, default=1, description="戳戳反击概率，0~1之间"),
            "host": ConfigField(type=str, default="127.0.0.1", description="Napcat服务主机地址"),
            "port": ConfigField(type=str, default="4999", description="Napcat服务端口"),
            "token": ConfigField(type=str, default="", description="Napcat接口鉴权token，可选"),
            "debug": ConfigField(type=bool, default=True, description="是否开启调试模式（显示请求头和执行情况）"),
            "allow_normal_active_poke": ConfigField(type=bool, default=True, description="允许normal模式下主动戳戳"),
        }
    }

    def _to_bool(self, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        return bool(v)

    def _to_float(self, v, default=0.0):
        try:
            return float(v)
        except Exception:
            return default

    def _to_int(self, v, default=0):
        try:
            return int(v)
        except Exception:
            return default

    def __init__(self, *args, **kwargs):
        logger.info("[TRACE] PokePlugin.__init__ called")
        super().__init__(*args, **kwargs)
        config_path = Path(__file__).parent / self.config_file_name
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self.plugin_config = toml.load(f)
            logger.info(f"config.toml已加载")
        except Exception as e:
            logger.error(f"[TRACE] 读取config.toml失败: {e}，使用空配置。")
            self.plugin_config = {}
        poke_cfg = self.plugin_config.get("poke", {})
        self.napcat_host = poke_cfg.get("host", "127.0.0.1")
        self.napcat_port = str(poke_cfg.get("port", "4999"))
        self.poke_debug = self._to_bool(poke_cfg.get("debug", True))
        self.poke_react_probability = self._to_float(poke_cfg.get("reaction_probability", 0.3), 0.3)
        self.allow_normal_active_poke = self._to_bool(poke_cfg.get("allow_normal_active_poke", True))
        self.token = poke_cfg.get("token", "")


    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (ActivePokeAction.get_action_info(), ActivePokeAction),
        ]

class ActivePokeAction(BaseAction):
    async def napcat_get_group_member_id_by_name(self, target_name, group_id, napcat_host, napcat_port, token):
        """
        通过Napcat接口获取群成员列表，模糊匹配昵称或备注，返回user_id。
        """
        import http.client
        import json
        conn = http.client.HTTPConnection(napcat_host, napcat_port)
        payload = json.dumps({"group_id": group_id, "no_cache": False})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            conn.request("POST", "/get_group_member_list", payload, headers)
            res = conn.getresponse()
            data = res.read()
            result = data.decode("utf-8")
            data_json = json.loads(result)
            if data_json.get("status") == "ok" and isinstance(data_json.get("data"), list):
                for member in data_json["data"]:
                    nickname = member.get("nickname", "")
                    card = member.get("card", "")
                    remark = member.get("remark", "")
                    if target_name in nickname or target_name in card or target_name in remark:
                        return member.get("user_id")
            return None
        except Exception as e:
            logger.error(f"[napcat_get_group_member_id_by_name] Napcat查找群成员失败: {e}")
            return None
    action_name = "active_poke" # 主动戳一戳
    action_description = "主动戳一戳群聊或好友"
    focus_activation_type = ActionActivationType.ALWAYS
    action_parameters = {
        "poke_keywords": "请在这里输入你想戳的人所发送的信息内容。"
    }
    action_require = [
        "当你想要戳一戳某人时可选择调用",
        "当你想要和某人人友好互动时可选择调用",
        "当你想要提醒某人时可选择调用",
        "当你想要提醒某人时可选择调用",
        "当你不知道如何回复某人信息时调用",
        "提示：keywords的内容应该全字匹配。",
        "比如，当你收到一条消息是“Restim：笨蛋小九揉揉揉揉”时，你想戳Restim，就在poke_keywords里输入“笨蛋小九揉揉揉揉”。错误的输入会导致active执行失败，所以需要严格按照格式来。",
    ]
    associated_types = ["text"]

    def __init__(
        self,
        action_data: Optional[dict] = None,
        reasoning: str = "",
        cycle_timers: Optional[dict] = None,
        thinking_id: str = "",
        chat_stream=None,
        log_prefix: str = "",
        shutting_down: bool = False,
        plugin_config: Optional[dict] = None,
        **kwargs,
    ):
        if action_data is None:
            action_data = {}
        if cycle_timers is None:
            cycle_timers = {}
        super().__init__(
            action_data=action_data,
            reasoning=reasoning,
            cycle_timers=cycle_timers,
            thinking_id=thinking_id,
            chat_stream=chat_stream,
            log_prefix=log_prefix,
            shutting_down=shutting_down,
            plugin_config=plugin_config,
            **kwargs,
        )
        self.plugin_config = plugin_config or getattr(self, 'plugin_config', {}) or {}
        self.in_group = False
        try:
            group = getattr(self.message.message_info, 'group_info', None)
            if group and getattr(group, 'group_id', None):
                self.in_group = True
        except Exception:
            self.in_group = False
        # 动态设置 normal_activation_type
        allow_normal = True
        if hasattr(self, "plugin_config") and self.plugin_config:
            allow_normal = self.plugin_config.get("poke", {}).get("allow_normal_active_poke", True)
        self.normal_activation_type = ActionActivationType.ALWAYS if allow_normal else ActionActivationType.NEVER
        # 读取配置
        self.napcat_host = self.plugin_config.get("poke", {}).get("host", "127.0.0.1")
        self.napcat_port = self.plugin_config.get("poke", {}).get("port", "4999")
        self.poke_debug = self.plugin_config.get("poke", {}).get("debug", True)
        self.token = self.plugin_config.get("poke", {}).get("token", "")

    async def napcat_get_user_id_by_name(self, target_name, group_id, napcat_host, napcat_port, token):
        """
        通过Napcat接口获取用户ID，模糊匹配昵称或备注，返回user_id。
        """
        import http.client
        import urllib.parse
        import json
        conn = http.client.HTTPConnection(napcat_host, napcat_port)
        payload = json.dumps({"no_cache": False})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            conn.request("POST", "/get_friend_list", payload, headers)
            res = conn.getresponse()
            data = res.read()
            result = data.decode("utf-8")
            data_json = json.loads(result)
            if data_json.get("status") == "ok" and isinstance(data_json.get("data"), list):
                for friend in data_json["data"]:
                    # 支持昵称和备注的模糊匹配
                    nickname = friend.get("nickname", "")
                    remark = friend.get("remark", "")
                    if target_name in nickname or target_name in remark:
                        return friend.get("user_id")
            return None
        except Exception as e:
            logger.error(f"[napcat_get_user_id_by_name] Napcat查找用户失败: {e}")
            return None

    async def napcat_get_group_id_by_name(self, target_name, napcat_host, napcat_port, token):
        """
        通过Napcat接口获取群列表，模糊匹配群名或备注，返回group_id。
        """
        import http.client
        import urllib.parse
        import json
        conn = http.client.HTTPConnection(napcat_host, napcat_port)
        payload = json.dumps({"no_cache": False})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            conn.request("POST", "/get_group_list", payload, headers)
            res = conn.getresponse()
            data = res.read()
            result = data.decode("utf-8")
            data_json = json.loads(result)
            if data_json.get("status") == "ok" and isinstance(data_json.get("data"), list):
                for group in data_json["data"]:
                    # 支持群名和群备注模糊匹配
                    group_name = group.get("group_name", "")
                    group_remark = group.get("group_remark", "")
                    if target_name in group_name or target_name in group_remark:
                        return group.get("group_id")
            return None
        except Exception as e:
            logger.error(f"[napcat_get_group_id_by_name] Napcat查找群失败: {e}")
            return None

    async def napcat_get_friend_id_by_name(self, target_name, napcat_host, napcat_port, token):
        """
        通过Napcat接口获取好友列表，模糊匹配昵称或备注，返回user_id。
        """
        import http.client
        import json
        conn = http.client.HTTPConnection(napcat_host, napcat_port)
        payload = json.dumps({"no_cache": False})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            conn.request("POST", "/get_friend_list", payload, headers)
            res = conn.getresponse()
            data = res.read()
            result = data.decode("utf-8")
            data_json = json.loads(result)
            if data_json.get("status") == "ok" and isinstance(data_json.get("data"), list):
                for friend in data_json["data"]:
                    nickname = friend.get("nickname", "")
                    remark = friend.get("remark", "")
                    if target_name in nickname or target_name in remark:
                        return friend.get("user_id")
            return None
        except Exception as e:
            logger.error(f"[napcat_get_friend_id_by_name] Napcat查找好友失败: {e}")
            return None

    async def get_ids(self):
        """
        通过poke_keywords决定戳目标，获取对应目标的user_id。
        """
        group_id = None
        message = getattr(self, 'message', None)
        if message and hasattr(message, 'message_info'):
            group_id = getattr(getattr(message, 'message_info', None), 'group_id', None)
        if group_id is None and hasattr(self, "chat_stream") and self.chat_stream and hasattr(self.chat_stream, "group_id"):
            group_id = getattr(self.chat_stream, "group_id", None)
        if group_id is None and hasattr(self, "action_data"):
            group_id = self.action_data.get("group_id", None)
        if group_id is None and hasattr(self, 'group_id') and self.group_id:
            group_id = self.group_id

        poke_keywords = None
        if hasattr(self, "action_data"):
            poke_keywords = self.action_data.get("poke_keywords", None)
        if not poke_keywords:
            # 没有关键词时用触发消息的发送者兜底，避免丢失 user_id
            fallback_user_id = getattr(self, "user_id", None)
            return fallback_user_id, group_id

        napcat_host = self.napcat_host
        napcat_port = int(self.napcat_port)
        token = self.token
        user_id = None

        # 通过poke_keywords匹配群聊上下文消息内容，获取发送者user_id
        if group_id:
            user_id = await self.napcat_get_user_id_from_group_history_by_msg(poke_keywords, group_id, napcat_host, napcat_port, token)
            # 若未匹配到，则降级用群成员名单模糊匹配
            if not user_id:
                user_id = await self.napcat_get_group_member_id_by_name(poke_keywords, group_id, napcat_host, napcat_port, token)
        else:
            user_id = await self.napcat_get_user_id_by_name(poke_keywords, None, napcat_host, napcat_port, token)
        # group_id 仍为空时，尝试通过Napcat群列表接口获取
        if not group_id:
            group_id = await self.napcat_get_group_id_by_name(poke_keywords, napcat_host, napcat_port, token)

        # Napcat未查到user_id时，降级用core属性
        if not user_id:
            user_id = getattr(self, 'user_id', None)
        return user_id, group_id

    async def napcat_get_user_id_from_group_history_by_msg(self, poke_keywords, group_id, napcat_host, napcat_port, token):
        """
        通过Napcat群历史消息接口，遍历消息上下文，匹配poke_keywords于raw_message，提取发送者user_id。
        """
        import http.client
        import json
        conn = http.client.HTTPConnection(napcat_host, napcat_port)
        payload = json.dumps({"group_id": group_id, "message_seq": 0, "count": 20, "reverseOrder": False})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            conn.request("POST", "/get_group_msg_history", payload, headers)
            res = conn.getresponse()
            data = res.read()
            result = data.decode("utf-8")
            data_json = json.loads(result)
            if data_json.get("status") == "ok" and isinstance(data_json.get("data", {}).get("messages", []), list):
                for msg in data_json["data"]["messages"]:
                    raw_message = msg.get("raw_message", "")
                    sender = msg.get("sender", {})
                    if poke_keywords in raw_message:
                        return sender.get("user_id")
            return None
        except Exception as e:
            logger.error(f"[napcat_get_user_id_from_group_history_by_msg] Napcat查找群历史消息失败: {e}")
            return None

    async def napcat_get_user_id_from_group_history(self, target_name, group_id, napcat_host, napcat_port, token):
        """
        通过Napcat群历史消息接口，遍历消息上下文，匹配target_name，提取对应user_id。
        """
        import http.client
        import json
        conn = http.client.HTTPConnection(napcat_host, napcat_port)
        payload = json.dumps({"group_id": group_id, "message_seq": 0, "count": 20, "reverseOrder": False})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            conn.request("POST", "/get_group_msg_history", payload, headers)
            res = conn.getresponse()
            data = res.read()
            result = data.decode("utf-8")
            data_json = json.loads(result)
            if data_json.get("status") == "ok" and isinstance(data_json.get("data", {}).get("messages", []), list):
                for msg in data_json["data"]["messages"]:
                    # 支持昵称、card、raw_message等模糊匹配
                    sender = msg.get("sender", {})
                    nickname = sender.get("nickname", "")
                    card = sender.get("card", "")
                    raw_message = msg.get("raw_message", "")
                    if (target_name in nickname) or (target_name in card) or (target_name in raw_message):
                        return sender.get("user_id")
            return None
        except Exception as e:
            logger.error(f"[napcat_get_user_id_from_group_history] Napcat查找群历史消息失败: {e}")
            return None

    async def execute(self) -> Tuple[bool, str]:
        # 每次主动戳戳前检测并reload config
        plugin = getattr(self, 'plugin', None)
        if plugin and hasattr(plugin, '_check_and_update_config_version'):
            plugin._check_and_update_config_version()
            # reload config
            config_path = Path(plugin.__file__).parent / plugin.config_file_name
            if config_path.exists():
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        self.plugin_config = toml.load(f)
                except Exception:
                    pass

        # 直接使用 self.plugin_config，不再获取插件实例
        user_id, group_id = await self.get_ids()
        self_id = self.action_data.get("self_id", None)
        try:
            if self_id and str(user_id) == str(self_id):
                logger.info("戳一戳目标为自己，忽略。")
                return False, "不能戳自己"
            success, result = self.send_poke(user_id, group_id)
        except Exception as e:
            logger.error(f"执行戳一戳操作时异常: {e}")
            return False, f"戳一戳操作异常: {e}"
        if "user_id" in self.action_data:
            self.action_data["user_id"] = None
        if success:
            logger.info(f"戳一戳操作成功: {result}")
            return True, "戳一戳操作成功"
        else:
            logger.error(f"戳一戳操作失败，返回内容: {result!r}")
            error_msg = f"戳一戳操作失败: {result.get('error_message', str(result)) if isinstance(result, dict) else str(result) or '未知错误'}"
            return False, error_msg

    def send_poke(self, user_id, group_id):
        import http.client
        napcat_host = self.napcat_host
        napcat_port = int(self.napcat_port)
        poke_debug = self.poke_debug
        token = self.token
        conn = http.client.HTTPConnection(napcat_host, napcat_port)
        payload = {"user_id": user_id}
        if group_id is not None:
            payload["group_id"] = group_id
        payload = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        debug_msgs = []
        if poke_debug:
            debug_msgs.append(f"戳一戳请求头: {headers}, user_id: {user_id}, group_id: {group_id}")
        try:
            conn.request("POST", "/send_poke", payload, headers)
            res = conn.getresponse()
            data = res.read()
            result = data.decode("utf-8")
            if poke_debug:
                debug_msgs.append(f"戳一戳成功! 响应: {result}")
            # 尝试解析json
            try:
                data_json = json.loads(result)
                return data_json.get("status") == "ok", '\n'.join(debug_msgs + [str(data_json)])
            except Exception:
                return True, '\n'.join(debug_msgs + [result])
        except Exception as e:
            error_info = {
                "error_type": type(e).__name__,
                "error_message": str(e)
            }
            debug_msgs.append(f"戳一戳异常: {error_info}")
            return False, '\n'.join(debug_msgs)
