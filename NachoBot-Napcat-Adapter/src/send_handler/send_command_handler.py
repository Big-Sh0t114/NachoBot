from maim_message import GroupInfo
from typing import Any, Dict, Tuple

from src import CommandType


class SendCommandHandleClass:
    @classmethod
    def handle_command(cls, raw_command_data: Dict[str, Any], group_info: GroupInfo):
        command_name: str = raw_command_data.get("name")
        try:
            match command_name:
                case CommandType.GROUP_BAN.name:
                    return cls.handle_ban_command(raw_command_data.get("args", {}), group_info)
                case CommandType.GROUP_WHOLE_BAN.name:
                    return cls.handle_whole_ban_command(raw_command_data.get("args", {}), group_info)
                case CommandType.GROUP_KICK.name:
                    return cls.handle_kick_command(raw_command_data.get("args", {}), group_info)
                case CommandType.SEND_POKE.name:
                    return cls.handle_poke_command(raw_command_data.get("args", {}), group_info)
                case CommandType.DELETE_MSG.name:
                    return cls.delete_msg_command(raw_command_data.get("args", {}))
                case CommandType.AI_VOICE_SEND.name:
                    return cls.handle_ai_voice_send_command(raw_command_data.get("args", {}), group_info)
                case CommandType.MESSAGE_LIKE.name:
                    return cls.handle_message_like_command(raw_command_data.get("args", {}))
                case _:
                    raise RuntimeError(f"未知的命令类型: {command_name}")
        except Exception as e:
            raise RuntimeError(f"处理命令时出错: {str(e)}") from e

    @staticmethod
    def handle_ban_command(args: Dict[str, Any], group_info: GroupInfo) -> Tuple[str, Dict[str, Any]]:
        """处理封禁命令

        Args:
            args (Dict[str, Any]): 参数字典
            group_info (GroupInfo): 群聊信息（对应目标群聊）

        Returns:
            Tuple[CommandType, Dict[str, Any]]
        """
        duration: int = int(args["duration"])
        user_id: int = int(args["qq_id"])
        group_id: int = int(group_info.group_id)
        if duration < 0:
            raise ValueError("封禁时间必须大于等于0")
        if not user_id or not group_id:
            raise ValueError("封禁命令缺少必要参数")
        if duration > 2592000:
            raise ValueError("封禁时间不能超过30天")
        return (
            CommandType.GROUP_BAN.value,
            {
                "group_id": group_id,
                "user_id": user_id,
                "duration": duration,
            },
        )

    @staticmethod
    def handle_whole_ban_command(args: Dict[str, Any], group_info: GroupInfo) -> Tuple[str, Dict[str, Any]]:
        """处理全体禁言命令

        Args:
            args (Dict[str, Any]): 参数字典
            group_info (GroupInfo): 群聊信息（对应目标群聊）

        Returns:
            Tuple[CommandType, Dict[str, Any]]
        """
        enable = args["enable"]
        assert isinstance(enable, bool), "enable参数必须是布尔值"
        group_id: int = int(group_info.group_id)
        if group_id <= 0:
            raise ValueError("群组ID无效")
        return (
            CommandType.GROUP_WHOLE_BAN.value,
            {
                "group_id": group_id,
                "enable": enable,
            },
        )

    @staticmethod
    def handle_kick_command(args: Dict[str, Any], group_info: GroupInfo) -> Tuple[str, Dict[str, Any]]:
        """处理群成员踢出命令

        Args:
            args (Dict[str, Any]): 参数字典
            group_info (GroupInfo): 群聊信息（对应目标群聊）

        Returns:
            Tuple[CommandType, Dict[str, Any]]
        """
        user_id: int = int(args["qq_id"])
        group_id: int = int(group_info.group_id)
        if group_id <= 0:
            raise ValueError("群组ID无效")
        if user_id <= 0:
            raise ValueError("用户ID无效")
        return (
            CommandType.GROUP_KICK.value,
            {
                "group_id": group_id,
                "user_id": user_id,
                "reject_add_request": False,  # 不拒绝加群请求
            },
        )

    @staticmethod
    def handle_poke_command(args: Dict[str, Any], group_info: GroupInfo) -> Tuple[str, Dict[str, Any]]:
        """处理戳一戳命令

        Args:
            args (Dict[str, Any]): 参数字典
            group_info (GroupInfo): 群聊信息（对应目标群聊）

        Returns:
            Tuple[CommandType, Dict[str, Any]]
        """
        user_id: int = int(args["qq_id"])
        if group_info is None:
            group_id = None
        else:
            group_id: int = int(group_info.group_id)
            if group_id <= 0:
                raise ValueError("群组ID无效")
        if user_id <= 0:
            raise ValueError("用户ID无效")
        return (
            CommandType.SEND_POKE.value,
            {
                "group_id": group_id,
                "user_id": user_id,
            },
        )

    @staticmethod
    def delete_msg_command(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """处理撤回消息命令

        Args:
            args (Dict[str, Any]): 参数字典

        Returns:
            Tuple[CommandType, Dict[str, Any]]
        """
        try:
            message_id = int(args["message_id"])
            if message_id <= 0:
                raise ValueError("消息ID无效")
        except KeyError:
            raise ValueError("缺少必需参数: message_id") from None
        except (ValueError, TypeError) as e:
            raise ValueError(f"消息ID无效: {args['message_id']} - {str(e)}") from None

        return (
            CommandType.DELETE_MSG.value,
            {
                "message_id": message_id,
            },
        )

    @staticmethod
    def handle_ai_voice_send_command(args: Dict[str, Any], group_info: GroupInfo) -> Tuple[str, Dict[str, Any]]:
        """
        处理AI语音发送命令的逻辑。
        并返回 NapCat 兼容的 (action, params) 元组。
        """
        if not group_info or not group_info.group_id:
            raise ValueError("AI语音发送命令必须在群聊上下文中使用")
        if not args:
            raise ValueError("AI语音发送命令缺少参数")

        group_id: int = int(group_info.group_id)
        character_id = args.get("character")
        text_content = args.get("text")

        if not character_id or not text_content:
            raise ValueError(f"AI语音发送命令参数不完整: character='{character_id}', text='{text_content}'")

        return (
            CommandType.AI_VOICE_SEND.value,
            {
                "group_id": group_id,
                "text": text_content,
                "character": character_id,
            },
        )

    @staticmethod
    def handle_message_like_command(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """
        处理给消息贴表情的逻辑。
        """
        if not args:
            raise ValueError("消息贴表情命令缺少参数")

        message_id = args.get("message_id")
        emoji_id = args.get("emoji_id")
        if not message_id:
            raise ValueError("消息贴表情命令缺少必要参数: message_id")
        if not emoji_id:
            raise ValueError("消息贴表情命令缺少必要参数: emoji_id")

        message_id = int(message_id)
        emoji_id = int(emoji_id)
        if message_id <= 0:
            raise ValueError("消息ID无效")
        if emoji_id <= 0:
            raise ValueError("表情ID无效")

        return (
            CommandType.MESSAGE_LIKE.value,
            {
                "message_id": message_id,
                "emoji_id": emoji_id,
                "set": True,
            },
        )
