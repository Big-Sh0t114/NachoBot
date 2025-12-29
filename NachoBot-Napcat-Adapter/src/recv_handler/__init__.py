from enum import Enum


class MetaEventType:
    lifecycle = "lifecycle"  # 生命周期

    class Lifecycle:
        connect = "connect"  # 生命周期 - WebSocket 连接成功

    heartbeat = "heartbeat"  # 心跳


class MessageType:  # 接受消息大类
    private = "private"  # 私聊消息

    class Private:
        friend = "friend"  # 私聊消息 - 好友
        group = "group"  # 私聊消息 - 群临时
        group_self = "group_self"  # 私聊消息 - 群中自身发送
        other = "other"  # 私聊消息 - 其他

    group = "group"  # 群聊消息

    class Group:
        normal = "normal"  # 群聊消息 - 普通
        anonymous = "anonymous"  # 群聊消息 - 匿名消息
        notice = "notice"  # 群聊消息 - 系统提示


class NoticeType:  # 通知事件
    friend_recall = "friend_recall"  # 私聊消息撤回
    group_recall = "group_recall"  # 群聊消息撤回
    notify = "notify"
    group_ban = "group_ban"  # 群禁言

    class Notify:
        poke = "poke"  # 戳一戳

    class GroupBan:
        ban = "ban"  # 禁言
        lift_ban = "lift_ban"  # 解除禁言


class RealMessageType:  # 实际消息分类
    text = "text"  # 纯文本
    face = "face"  # qq表情
    image = "image"  # 图片
    record = "record"  # 语音
    video = "video"  # 视频
    at = "at"  # @某人
    rps = "rps"  # 猜拳魔法表情
    dice = "dice"  # 骰子
    shake = "shake"  # 私聊窗口抖动（只收）
    poke = "poke"  # 群聊戳一戳
    share = "share"  # 链接分享（json形式）
    reply = "reply"  # 回复消息
    forward = "forward"  # 转发消息
    node = "node"  # 转发消息节点


class MessageSentType:
    private = "private"

    class Private:
        friend = "friend"
        group = "group"

    group = "group"

    class Group:
        normal = "normal"


class CommandType(Enum):
    """命令类型"""

    GROUP_BAN = "set_group_ban"  # 禁言用户
    GROUP_WHOLE_BAN = "set_group_whole_ban"  # 群全体禁言
    GROUP_KICK = "set_group_kick"  # 踢出群聊
    SEND_POKE = "send_poke"  # 戳一戳
    DELETE_MSG = "delete_msg"  # 撤回消息

    def __str__(self) -> str:
        return self.value


ACCEPT_FORMAT = [
    "text",
    "image",
    "emoji",
    "reply",
    "voice",
    "command",
    "voiceurl",
    "music",
    "videourl",
    "file",
    "imageurl",
    "forward",
    "video",
]
