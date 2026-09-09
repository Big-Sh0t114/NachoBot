"""
日记插件工具模块

本模块包含日记插件的通用工具类和函数，提供跨模块共享的功能组件。

主要组件：
- ChatIdResolver: 聊天ID解析器，将用户友好的配置转换为真实的聊天ID
- DiaryConstants: 日记插件常量定义
- MockChatStream: 虚拟聊天流，用于定时任务中的Action初始化
"""

import os
import json
import time
import hashlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.chat.message_receive.chat_stream import ChatStream
from src.common.data_models.database_data_model import DatabaseMessages
from src.plugin_system.apis import config_api, generator_api, get_logger, message_api


logger = get_logger("diary_plugin.utils")


async def style_send(chat_stream: ChatStream, text: str, send_func: Callable):
    result_status, data = await generator_api.rewrite_reply(
        chat_stream=chat_stream,
        reply_data={
            "raw_reply": text,
            "reason": "想要输出一段符合风格的文本",
        },
    )
        
    if result_status:
        for reply_seg in data.reply_set.reply_data:
            send_data = reply_seg.content
            await send_func(send_data)

    
async def get_bot_personality() -> Dict[str, str]:
    """
    实时获取bot人设信息
    
    从全局配置中获取Bot的人格设置，用于生成个性化的日记内容。
    适配MaiBot 0.10.2版本的新配置项结构。
    
    Returns:
        Dict[str, str]: 包含Bot人设信息的字典，包含以下键：
            - core: 核心人设描述
            - side: 情感特征/人设补充
            - style: 回复风格
            - interest: 兴趣爱好
    
    Examples:
        >>> personality = await action.get_bot_personality()
        >>> print(personality['core'])  # "是一个活泼可爱的AI助手"
        >>> print(personality['style'])  # "温和友善，偶尔调皮"
    """
    personality = config_api.get_global_config("personality.personality", "是一个机器人助手")
    reply_style = config_api.get_global_config("personality.reply_style", "")
    interest = config_api.get_global_config("personality.interest", "")
    nickname = config_api.get_global_config("bot.nickname", "")
    alias_names = config_api.get_global_config("bot.alias_names", [])
    
    return {
        "core": personality,
        "style": reply_style,
        "interest": interest,
        "nickname": nickname,
        "alias_names": alias_names
    }



def format_date_str(date_input: Any) -> str:
    """
    统一的日期格式化函数,确保YYYY-MM-DD格式。
    
    支持多种日期格式的输入，包括datetime对象和多种字符串格式。
    如果所有解析方法都失败，将抛出ValueError异常。
    
    Args:
        date_input (Any): 输入的日期，可以是datetime对象或字符串
        
    Returns:
        str: 格式化后的日期字符串，格式为YYYY-MM-DD
        
    Raises:
        ValueError: 当输入的日期格式无法识别时抛出异常
        
    Examples:
        >>> format_date_str("2025/08/24")
        "2025-08-24"
        >>> format_date_str(datetime.datetime(2025, 8, 24))
        "2025-08-24"
    """
    import datetime
    import re
    
    if isinstance(date_input, datetime.datetime):
        return date_input.strftime("%Y-%m-%d")
    elif isinstance(date_input, str):
        try:
            # 尝试多种日期格式
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]:
                try:
                    date_obj = datetime.datetime.strptime(date_input, fmt)
                    return date_obj.strftime("%Y-%m-%d")
                except ValueError:
                    continue
            
            # 如果已经是正确格式，直接返回
            if re.match(r'^\d{4}-\d{1,2}-\d{1,2}$', date_input):
                return date_input
                
        except Exception as e:
            logger.debug(f"日期格式化失败: {e}")
    
    # 不再使用后备方案，而是抛出异常
    error_msg = f"无法识别的日期格式: {date_input}。支持的格式有: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD"
    logger.debug(error_msg)
    raise ValueError(error_msg)


class DiaryConstants:
    """
    日记插件常量定义类
    
    包含日记插件运行所需的各种常量配置，如消息数量限制、
    token限制、日记长度限制等核心参数。
    
    Attributes:
        MIN_MESSAGE_COUNT (int): 生成日记所需的最少消息数量
        TOKEN_LIMIT_50K (int): 50K token限制
        TOKEN_LIMIT_126K (int): 126K token限制  
        MAX_DIARY_LENGTH (int): 日记最大长度限制
        DEFAULT_QZONE_WORD_COUNT (int): QQ空间默认字数
    """
    MIN_MESSAGE_COUNT = 3
    TOKEN_LIMIT_50K = 50000
    TOKEN_LIMIT_126K = 126000
    MAX_DIARY_LENGTH = 8000
    DEFAULT_QZONE_WORD_COUNT = 300


class MockChatStream:
    """
    虚拟聊天流类
    
    用于定时任务中的Action初始化，提供一个模拟的聊天环境。
    当定时任务需要创建Action实例时，由于没有真实的聊天流，
    使用此类提供必要的属性和接口。
    
    Attributes:
        stream_id (str): 虚拟流ID，标识为定时任务
        platform (str): 平台标识，默认为qq
        group_info: 群组信息，定时任务中为None
        user_info: 用户信息，定时任务中为None
    
    Usage:
        >>> mock_stream = MockChatStream()
        >>> action = SomeAction(chat_stream=mock_stream, ...)
    """
    
    def __init__(self, stream_id: str = "diary_scheduled_task", platform: str = "scheduler"):
        """初始化虚拟聊天流"""
        self.stream_id = stream_id
        self.platform = platform
        self.group_info = None
        self.user_info = None


def create_scheduler_action_message(
    stream_id: str = "diary_scheduled_task",
    platform: str = "scheduler",
    user_id: str = "diary_scheduler",
    user_nickname: str = "DiaryScheduler",
) -> DatabaseMessages:
    """
    构建用于定时任务的虚拟消息对象

    BaseAction 初始化会访问 action_message.chat_info 与 action_message.user_info。
    定时任务没有真实消息上下文，通过构建完整的 DatabaseMessages 来满足依赖。

    Args:
        stream_id: 虚拟聊天流 ID
        platform: 虚拟平台标识
        user_id: 虚拟用户 ID
        user_nickname: 虚拟用户昵称

    Returns:
        DatabaseMessages: 可用于 Action 初始化的虚拟消息对象
    """
    timestamp = time.time()
    return DatabaseMessages(
        message_id=stream_id,
        chat_id=stream_id,
        time=timestamp,
        user_id=user_id,
        user_nickname=user_nickname,
        user_platform=platform,
        chat_info_stream_id=stream_id,
        chat_info_platform=platform,
        chat_info_create_time=timestamp,
        chat_info_last_active_time=timestamp,
        chat_info_user_id=user_id,
        chat_info_user_nickname=user_nickname,
        chat_info_user_platform=platform,
    )


class ChatIdResolver:
    """
    聊天ID解析器 - 将用户友好的配置转换为真实的聊天ID
    
    该类负责处理用户配置中的聊天标识符（如"group:123456"、"private:789012"）
    到系统内部真实聊天ID的映射和转换。提供智能缓存机制以提高解析效率。
    
    主要功能：
    - 解析用户友好的聊天配置格式
    - 查询数据库获取真实的聊天ID
    - 缓存映射关系以提高性能
    - 支持白名单和黑名单过滤模式
    - 自动检测配置变更并更新缓存
    
    配置格式：
    - 群聊：group:群号 (如 "group:123456789")
    - 私聊：private:用户QQ号 (如 "private:987654321")
    
    过滤模式：
    - whitelist: 白名单模式，只处理指定的聊天
    - blacklist: 黑名单模式，处理除指定外的所有聊天
    
    使用场景：
    - 定时任务中解析目标聊天列表
    - 手动命令中指定处理范围
    - 插件配置验证和转换
    
    缓存机制：
    - 基于配置哈希值检测变更
    - 自动验证缓存的聊天ID有效性
    - 支持增量更新和完整重建
    """
    
    def __init__(self):
        self.cache_file = os.path.join(os.path.dirname(__file__), "..", "data", "chat_mapping.json")
        self.cache = {}
        self.last_config_hash = ""
        
    def _get_config_hash(self, groups: List[str], privates: List[str]) -> str:
        """计算配置的哈希值,用于检测配置变更"""
        config_str = f"groups:{','.join(sorted(groups))};privates:{','.join(sorted(privates))}"
        return hashlib.md5(config_str.encode()).hexdigest()
    
    def _load_cache(self) -> bool:
        """加载缓存文件"""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    self.cache = cache_data.get("mapping", {})
                    self.last_config_hash = cache_data.get("config_hash", "")
                    return True
        except Exception as e:
            logger.error(f"加载聊天ID缓存失败: {e}")
        return False
    
    def _save_cache(self, config_hash: str):
        """保存缓存文件"""
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            cache_data = {
                "mapping": self.cache,
                "config_hash": config_hash,
                "last_update": time.time(),
            }
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存聊天ID缓存失败: {e}")
    
    def _query_chat_id_from_database(self, qq_number: str, is_group: bool) -> Optional[str]:
        """从数据库查询聊天ID"""
        try:
            from src.common.database.database_model import ChatStreams
            
            if is_group:
                # 查找群聊
                stream = ChatStreams.get_or_none(ChatStreams.group_id == str(qq_number))
            else:
                # 查找私聊（user_id匹配且group_id为空）
                stream = ChatStreams.get_or_none(
                    (ChatStreams.user_id == str(qq_number)) & 
                    (ChatStreams.group_id.is_null() | (ChatStreams.group_id == ""))
                )
            
            return stream.stream_id if stream else None
        except Exception as e:
            logger.error(f"查询聊天ID失败 ({qq_number}, {'群聊' if is_group else '私聊'}): {e}")
            return None
    
    def _validate_chat_id(self, chat_id: str) -> bool:
        """验证聊天ID是否有效"""
        try:
            # 尝试获取该聊天的消息来验证ID有效性
            message_api.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=0,
                end_time=time.time(),
                limit=1,
                filter_mai=False,
                filter_command=False
            )
            return True  # 能成功调用就说明chat_id有效
        except Exception:
            return False
    
    def resolve_filter_mode(self, filter_mode: str, target_chats: List[str], _recursion_depth: int = 0) -> Tuple[str, List[str]]:
        """
        根据过滤模式和目标列表解析处理策略，防止无限递归
        
        这是一个公共方法，用于解析用户配置的过滤模式并返回相应的处理策略。
        支持白名单、黑名单两种过滤模式，并提供递归保护机制。
        
        Args:
            filter_mode (str): 过滤模式，可选值为"whitelist"或"blacklist"
            target_chats (List[str]): 目标聊天配置列表
            _recursion_depth (int): 递归深度，用于防止无限递归
        
        Returns:
            Tuple[str, List[str]]: (处理策略, 有效配置列表)
        """
        # 防止无限递归，最大递归深度限制为1
        if _recursion_depth > 1:
            logger.error("过滤模式解析递归过深，使用默认处理")
            return "PROCESS_ALL", []
        
        # 根据过滤模式处理
        if filter_mode == "whitelist":
            if target_chats:
                logger.debug(f"白名单模式:处理指定的{len(target_chats)}个聊天")
                return "PROCESS_WHITELIST", target_chats
            else:
                logger.debug("白名单模式:空列表,禁用定时任务")
                return "DISABLE_SCHEDULER", []
    
        elif filter_mode == "blacklist":
            if target_chats:
                logger.debug(f"黑名单模式:排除指定的{len(target_chats)}个聊天")
                return "PROCESS_BLACKLIST", target_chats
            else:
                logger.debug("黑名单模式:空列表,处理全部聊天")
                return "PROCESS_ALL", []
        
        else:
            logger.warning(f"未知的过滤模式: {filter_mode},使用默认白名单模式")
            return self.resolve_filter_mode("whitelist", target_chats, _recursion_depth + 1)
    
    def _parse_target_config(self, target_chats: List[str]) -> Tuple[List[str], List[str]]:
        """解析target_chats配置为群聊和私聊列表"""
        groups = []
        privates = []
        
        for chat_config in target_chats:
            if chat_config.startswith("group:"):
                group_id = chat_config[6:]  # 移除"group:"前缀
                groups.append(group_id)
            elif chat_config.startswith("private:"):
                user_id = chat_config[8:]  # 移除"private:"前缀
                privates.append(user_id)
            else:
                logger.warning(f"无效的聊天配置格式: {chat_config}")
        
        return groups, privates
    
    def resolve_target_chats(self, filter_mode: str, target_chats: List[str]) -> Tuple[str, List[str]]:
        """
        根据过滤模式解析目标聊天配置
        
        这是一个公共方法，用于将用户配置的聊天列表解析为实际的聊天ID列表。
        根据不同的过滤模式采用不同的处理策略。
        
        Args:
            filter_mode (str): 过滤模式，可选值为"whitelist"或"blacklist"
            target_chats (List[str]): 目标聊天配置列表
        
        Returns:
            Tuple[str, List[str]]: (处理策略, 解析后的聊天ID列表)
        """
        # 使用新的过滤模式解析
        strategy, valid_configs = self.resolve_filter_mode(filter_mode, target_chats)
        
        if strategy == "DISABLE_SCHEDULER":
            return strategy, []
        
        if strategy == "PROCESS_ALL":
            return strategy, []  # 空列表表示处理所有聊天
        
        if strategy in ["PROCESS_WHITELIST", "PROCESS_BLACKLIST"]:
            # 解析有效配置为聊天ID
            chat_ids = self._resolve_configs_to_chat_ids(valid_configs)
            return strategy, chat_ids
        
        return "PROCESS_ALL", []
    
    def _resolve_configs_to_chat_ids(self, target_configs: List[str]) -> List[str]:
        """将配置列表解析为聊天ID列表"""
        if not target_configs:
            return []
        
        # 解析配置
        groups, privates = self._parse_target_config(target_configs)
        
        # 计算当前配置的哈希值
        current_config_hash = self._get_config_hash(groups, privates)
        
        # 检查配置是否变更
        self._load_cache()
        config_changed = (current_config_hash != self.last_config_hash)
        
        valid_chat_ids = []
        
        # 处理群聊配置
        for group_qq in groups:
            cache_key = f"group_{group_qq}"
            
            # 优先使用缓存
            if not config_changed and cache_key in self.cache:
                cached_chat_id = self.cache[cache_key]
                if self._validate_chat_id(cached_chat_id):
                    valid_chat_ids.append(cached_chat_id)
                    continue
            
            # 缓存失效或不存在,重新查询
            chat_id = self._query_chat_id_from_database(group_qq, True)
            if chat_id and self._validate_chat_id(chat_id):
                valid_chat_ids.append(chat_id)
                self.cache[cache_key] = chat_id
                logger.debug(f"群聊映射: {group_qq} → {chat_id}")
            else:
                logger.debug(f"未找到群 {group_qq} 的聊天记录,可能尚未加入该群")
        
        # 处理私聊配置
        for user_qq in privates:
            cache_key = f"private_{user_qq}"
            
            # 优先使用缓存
            if not config_changed and cache_key in self.cache:
                cached_chat_id = self.cache[cache_key]
                if self._validate_chat_id(cached_chat_id):
                    valid_chat_ids.append(cached_chat_id)
                    continue
            
            # 缓存失效或不存在,重新查询
            chat_id = self._query_chat_id_from_database(user_qq, False)
            if chat_id and self._validate_chat_id(chat_id):
                valid_chat_ids.append(chat_id)
                self.cache[cache_key] = chat_id
                logger.debug(f"私聊映射: {user_qq} → {chat_id}")
            else:
                logger.debug(f"未找到用户 {user_qq} 的聊天记录,可能尚未建立私聊")
        
        # 保存更新后的缓存
        if config_changed or valid_chat_ids:
            self._save_cache(current_config_hash)
        
        logger.debug(f"聊天ID解析完成: 配置{len(groups + privates)}个,有效{len(valid_chat_ids)}个")
        return valid_chat_ids
