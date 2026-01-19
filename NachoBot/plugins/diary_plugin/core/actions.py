"""
日记生成核心模块

本模块包含日记生成的核心业务逻辑，是整个日记插件的核心组件。主要负责：
- 从聊天记录中提取和分析消息数据
- 根据Bot人设和聊天内容生成个性化日记
- 支持自定义模型和默认模型两种生成方式
- 处理Token限制和消息截断逻辑
- 集成QQ空间发布功能

该模块设计为独立的Action组件，可以被定时任务、手动命令等多种方式调用，
提供了完整的日记生成工作流程。

Dependencies:
    - src.plugin_system: 插件系统基础组件
    - src.plugin_system.apis: 内置API接口
    - .storage: 日记存储模块
    - .qzone: QQ空间API模块
    - .resolver: 聊天ID解析模块

Author: MaiBot Diary Plugin
Version: 2.1.0
"""

import datetime
import time
import random
from typing import List, Tuple, Any
from openai import AsyncOpenAI

from src.plugin_system import (
    BaseAction,
    ActionActivationType
)
from src.plugin_system.apis import (
    config_api,
    llm_api,
    message_api,
    chat_api,
    get_logger
)

from .storage import DiaryStorage, DiaryQzoneAPI
from .utils import ChatIdResolver, DiaryConstants
from .diary_service import DiaryService

logger = get_logger("diary_actions")


class OptimizedMessageFetcher:
    """优化的消息获取器，智能选择最适合的API"""
    
    def get_messages_by_config(self, configs: List[str], start_time: float, end_time: float) -> List[Any]:
        """根据配置智能选择最适合的API获取消息"""
        all_messages = []
        private_qqs, group_qqs = self._parse_configs(configs)
        
        # 私聊：使用 chat_api.get_stream_by_user_id + get_messages_by_time_in_chat，获取完整对话（包含Bot回复）
        if private_qqs:
            private_messages = self._get_private_messages_optimized(private_qqs, start_time, end_time)
            all_messages.extend(private_messages)
        
        # 群聊：使用改进的chat_id解析 + get_messages_by_time_in_chat
        if group_qqs:
            group_messages = self._get_group_messages_optimized(group_qqs, start_time, end_time)
            all_messages.extend(group_messages)
        
        return sorted(all_messages, key=lambda x: x.time)
    
    def _get_private_messages_optimized(self, qq_numbers: List[str], start_time: float, end_time: float) -> List[Any]:
        """通过QQ号获取私聊消息（包含Bot回复，严格遵守黑白名单）"""
        all_private_messages = []
        
        for user_qq in qq_numbers:
            try:
                # 1. 精确定位私聊流（严格按用户ID匹配）
                private_stream = chat_api.get_stream_by_user_id(user_qq)
                if not private_stream:
                    logger.warning(f"未找到用户{user_qq}的私聊流，跳过")
                    continue
                
                chat_id = private_stream.stream_id
                
                # 2. 获取该私聊中的完整对话（包括Bot回复）
                messages = message_api.get_messages_by_time_in_chat(
                    chat_id=chat_id,
                    start_time=start_time,
                    end_time=end_time,
                    limit=0,
                    limit_mode="earliest",
                    filter_mai=False,  # 关键：包含Bot消息
                    filter_command=False
                )
                
                all_private_messages.extend(messages)
                logger.info(f"[完美获取] 私聊{user_qq} -> {chat_id} 获取到{len(messages)}条消息（包含Bot回复）")
                
            except Exception as e:
                logger.error(f"获取私聊{user_qq}消息失败: {e}")
        
        return all_private_messages
    
    def _get_group_messages_optimized(self, group_qqs: List[str], start_time: float, end_time: float) -> List[Any]:
        """通过群号获取群聊消息，纯API实现"""
        all_group_messages = []
        
        for group_qq in group_qqs:
            try:
                # 使用chat_api获取群聊的stream_id
                stream = chat_api.get_stream_by_group_id(group_qq)
                if not stream:
                    logger.warning(f"无法获取群聊{group_qq}的stream信息")
                    continue
                
                chat_id = stream.stream_id
                
                # 使用 message_api 获取消息
                messages = message_api.get_messages_by_time_in_chat(
                    chat_id=chat_id,
                    start_time=start_time,
                    end_time=end_time,
                    limit=0,
                    limit_mode="earliest",
                    filter_mai=False,
                    filter_command=False
                )
                
                all_group_messages.extend(messages)
                logger.debug(f"[优化获取] 群聊{group_qq} -> {chat_id} 获取到{len(messages)}条消息")
                
            except Exception as e:
                logger.error(f"获取群聊{group_qq}消息失败: {e}")
        return all_group_messages
    
    def _is_private_message(self, msg: Any) -> bool:
        """判断是否为私聊消息"""
        # 检查消息是否没有群组ID
        try:
            # 检查chat_info中的group_id
            if hasattr(msg, 'chat_info') and hasattr(msg.chat_info, 'group_id'):
                group_id = msg.chat_info.group_id
                return not group_id or group_id.strip() == ""
            
            # 备用检查：直接检查消息的group_id属性
            if hasattr(msg, 'group_id'):
                group_id = msg.group_id
                return not group_id or group_id.strip() == ""
            
            # 如果都没有group_id属性，默认认为是私聊
            return True
            
        except Exception as e:
            logger.debug(f"判断私聊消息时出错: {e}")
            return True  # 出错时默认认为是私聊
    
    def _parse_configs(self, configs: List[str]) -> Tuple[List[str], List[str]]:
        """解析配置，分离私聊和群聊"""
        private_qqs = []
        group_qqs = []
        
        for config in configs:
            if config.startswith('private:'):
                private_qqs.append(config[8:])  # 去掉'private:'前缀
            elif config.startswith('group:'):
                group_qqs.append(config[6:])  # 去掉'group:'前缀
            else:
                # 默认作为群聊处理
                group_qqs.append(config)
        
        return private_qqs, group_qqs


class SmartFilterSystem:
    """智能过滤系统，支持多种过滤模式"""
    
    def __init__(self):
        self.fetcher = OptimizedMessageFetcher()
    
    def apply_filter_mode(self, filter_mode: str, configs: List[str], start_time: float, end_time: float) -> List[Any]:
        """应用过滤模式，智能选择最佳策略"""
        if filter_mode == "whitelist":
            if not configs:
                # 空白名单：返回空列表，避免不必要的查询
                logger.info("[智能过滤] 白名单为空，返回空消息列表")
                return []
            logger.debug(f"[智能过滤] 白名单模式，处理{len(configs)}个配置")
            return self.fetcher.get_messages_by_config(configs, start_time, end_time)
        elif filter_mode == "blacklist":
            if not configs:
                # 空黑名单：获取所有消息
                logger.debug("[智能过滤] 黑名单为空，获取所有消息")
                return self._get_all_messages(start_time, end_time)
            
            # 非空黑名单：获取所有消息后过滤
            logger.debug(f"[智能过滤] 黑名单模式，排除{len(configs)}个配置")
            all_messages = self._get_all_messages(start_time, end_time)
            return self._filter_excluded_messages(all_messages, configs)
        
        elif filter_mode == "all":
            logger.debug("[智能过滤] 全部消息模式")
            return self._get_all_messages(start_time, end_time)
        
        logger.warning(f"[智能过滤] 未知的过滤模式: {filter_mode}")
        return []
    
    def _get_all_messages(self, start_time: float, end_time: float) -> List[Any]:
        """获取所有消息，纯API实现"""
        try:
            messages = message_api.get_messages_by_time(
                start_time=start_time,
                end_time=end_time,
                limit=0,
                limit_mode="earliest",
                filter_mai=False
            )
            logger.debug(f"[智能过滤] 获取到{len(messages)}条全部消息")
            return messages
        except Exception as e:
            logger.error(f"获取所有消息失败: {e}")
            return []
    
    def _filter_excluded_messages(self, all_messages: List[Any], excluded_configs: List[str]) -> List[Any]:
        """过滤掉黑名单中的消息"""
        excluded_privates, excluded_groups = self.fetcher._parse_configs(excluded_configs)
        filtered_messages = []
        excluded_count = 0
        
        # 预先获取所有排除群聊的chat_id
        excluded_chat_ids = set()
        for group_qq in excluded_groups:
            try:
                # 使用chat_api获取群聊的stream_id
                stream = chat_api.get_stream_by_group_id(group_qq)
                if stream:
                    excluded_chat_ids.add(stream.stream_id)
                    logger.debug(f"[智能过滤] 黑名单群聊 {group_qq} -> {stream.stream_id}")
            except Exception as e:
                logger.error(f"获取黑名单群聊{group_qq}的chat_id失败: {e}")
        
        for msg in all_messages:
            is_excluded = False
            
            # 检查是否为排除的私聊用户
            if self.fetcher._is_private_message(msg):
                user_id = getattr(msg.user_info, 'user_id', None)
                if user_id and user_id in excluded_privates:
                    is_excluded = True
                    excluded_count += 1
                    logger.debug(f"[智能过滤] 排除私聊用户 {user_id} 的消息")
            
            # 检查是否为排除的群聊
            if not is_excluded:
                chat_id = getattr(msg, 'chat_id', None)
                if chat_id and chat_id in excluded_chat_ids:
                    is_excluded = True
                    excluded_count += 1
                    logger.debug(f"[智能过滤] 排除群聊 {chat_id} 的消息")
            
            if not is_excluded:
                filtered_messages.append(msg)
        
        logger.debug(f"[智能过滤] 黑名单过滤完成:原始{len(all_messages)}条 -> 过滤后{len(filtered_messages)}条，排除{excluded_count}条")
        return filtered_messages


# 常量定义已移至utils模块


# _format_date_str函数已移至utils模块


class DiaryGeneratorAction(BaseAction):
    """
    日记生成Action - 日记插件的核心业务逻辑组件
    
    这是日记插件最重要的组件，负责完整的日记生成工作流程。主要功能包括：
    
    核心功能：
    - 智能获取和分析指定日期的聊天记录
    - 根据Bot人设生成个性化的日记内容
    - 支持情感分析和天气生成
    - 自动处理Token限制和消息截断
    - 集成QQ空间自动发布功能
    
    技术特性：
    - 支持自定义模型和系统默认模型两种生成方式
    - 智能的聊天ID解析和过滤机制
    - 完善的错误处理和重试机制
    - 灵活的配置系统支持
    
    使用场景：
    - 定时任务自动生成日记
    - 手动命令触发生成
    - 测试和调试场景
    
    配置依赖：
    - diary_generation.*: 日记生成相关配置
    - custom_model.*: 自定义模型配置
    - qzone_publishing.*: QQ空间发布配置
    - schedule.*: 定时任务配置
    
    Examples:
        # 创建日记生成器实例
        diary_action = DiaryGeneratorAction(
            action_data={"date": "2025-01-15", "target_chats": []},
            action_reasoning="手动生成日记",
            cycle_timers={},
            thinking_id="manual_diary",
            chat_stream=chat_stream,
            log_prefix="[DiaryGenerate]",
            plugin_config=plugin_config,
            action_message=None
        )
        
        # 执行日记生成
        success, result = await diary_action.generate_diary("2025-01-15")
        if success:
            print(f"日记生成成功: {result}")
        else:
            print(f"日记生成失败: {result}")
    
    Note:
        该类继承自BaseAction，遵循MaiBot插件系统的Action规范。
        所有的配置获取都通过self.get_config()方法进行，确保配置的一致性。
        日记生成过程中会自动保存到本地存储，并可选择发布到QQ空间。
    """
    
    action_name = "diary_generator"
    action_description = "根据当天聊天记录生成个性化日记"
    activation_type = ActionActivationType.NEVER
    
    action_parameters = {
        "date": "要生成日记的日期 (YYYY-MM-DD格式)",
        "target_chats": "目标聊天ID列表,为空则处理所有活跃聊天"
    }
    action_require = [
        "需要生成日记时使用",
        "总结当天的聊天内容",
        "生成个性化的回忆录"
    ]
    associated_types = ["text"]

    def __init__(self, *args, **kwargs):
        """
        初始化日记生成 Action。

        为了兼容旧版本或外部错误调用，这里做了参数兜底处理：
        - 允许使用 legacy 参数名 "reasoning"，并映射为 BaseAction 所需的
          "action_reasoning"；
        - 当调用方未提供 "action_reasoning" 时，自动填充为空字符串，避免
          BaseAction.__init__ 的必填参数错误。
        """
        # 兼容旧代码：统一收敛为 BaseAction 需要的 reasoning 参数
        reason = ""
        if "action_reasoning" in kwargs:
            reason = kwargs.pop("action_reasoning")
        elif "reasoning" in kwargs:
            reason = kwargs.pop("reasoning")
        kwargs["reasoning"] = reason

        super().__init__(*args, **kwargs)
        self.storage = DiaryStorage()
        self.qzone_api = DiaryQzoneAPI()
        self.diary_service = DiaryService(plugin_config=self.plugin_config)
        self.chat_resolver = ChatIdResolver()

    async def get_daily_messages(self, date: str, target_chats: List[str] = None, end_hour: int = None, end_minute: int = None) -> List[Any]:
        """
        获取指定日期的聊天记录（使用优化的智能API）
        
        这是日记生成的核心数据获取方法，负责从消息数据库中提取指定日期的聊天记录。
        支持多种过滤模式和聊天范围配置，确保获取到合适的消息数据用于日记生成。
        
        Args:
            date (str): 目标日期，格式为YYYY-MM-DD
            target_chats (List[str], optional): 指定的聊天ID列表，为None时根据配置自动解析
            end_hour (int, optional): 结束小时，用于定时任务指定截止时间
            end_minute (int, optional): 结束分钟，用于定时任务指定截止时间
        
        Returns:
            List[Any]: 按时间排序的消息列表，每个消息包含完整的用户信息和内容
        
        Raises:
            Exception: 当消息获取过程中出现错误时
        
        Note:
            - 该方法会根据配置的过滤模式（白名单/黑名单）智能选择消息范围
            - 支持min_messages_per_chat配置，过滤掉消息数量不足的聊天
            - 所有消息都包含Bot消息（filter_mai=False），确保日记内容完整
            - 消息按时间顺序排序，便于构建时间线
        
        Examples:
            # 获取今天的所有消息
            messages = await action.get_daily_messages("2025-01-15")
            
            # 获取指定聊天的消息
            messages = await action.get_daily_messages("2025-01-15", ["chat_id_1", "chat_id_2"])
            
            # 获取到指定时间的消息（用于定时任务）
            messages = await action.get_daily_messages("2025-01-15", None, 23, 30)
        """
        try:
            # 计算时间范围
            date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
            start_time = date_obj.timestamp()
            
            if end_hour is not None and end_minute is not None:
                end_time = date_obj.replace(hour=end_hour, minute=end_minute, second=0).timestamp()
            else:
                current_time = datetime.datetime.now()
                if current_time.strftime("%Y-%m-%d") == date:
                    end_time = current_time.timestamp()
                else:
                    end_time = (date_obj + datetime.timedelta(days=1)).timestamp()
            
            all_messages = []
            
            if target_chats:
                # 处理指定聊天
                for chat_id in target_chats:
                    try:
                        # 关键:设置 filter_mai=False 来包含Bot消息
                        messages = message_api.get_messages_by_time_in_chat(
                            chat_id=chat_id,
                            start_time=start_time,
                            end_time=end_time,
                            limit=0,
                            limit_mode="earliest",
                            filter_mai=False,  # 不过滤Bot消息
                            filter_command=False  # 不过滤命令消息
                        )
                        all_messages.extend(messages)
                    except Exception as e:
                        logger.error(f"获取聊天 {chat_id} 消息失败: {e}")
            else:
                # 从配置文件读取聊天配置
                config_target_chats = self.get_config("schedule.target_chats", [])
                filter_mode = self.get_config("schedule.filter_mode", "whitelist")
                
                # 使用新的智能过滤系统
                filter_system = SmartFilterSystem()
                all_messages = filter_system.apply_filter_mode(filter_mode, config_target_chats, start_time, end_time)
                
                # 检查是否为手动命令且配置为空的特殊情况
                if not all_messages and filter_mode == "whitelist" and not config_target_chats:
                    is_manual = self.action_data.get("is_manual", False)
                    if is_manual:
                        # 手动命令:处理所有聊天（用于测试）
                        logger.debug("手动命令检测到空白名单,处理所有聊天用于测试")
                        try:
                            all_messages = message_api.get_messages_by_time(
                                start_time=start_time,
                                end_time=end_time,
                                limit=0,
                                limit_mode="earliest",
                                filter_mai=False  # 不过滤Bot消息
                            )
                        except Exception as e:
                            logger.error(f"获取所有消息失败: {e}")
                    else:
                        # 定时任务:跳过处理,返回空消息
                        logger.debug("定时任务检测到空白名单,取消执行")
                        return []
            
            # 按时间排序
            all_messages.sort(key=lambda x: x.time)
            
            # 实现min_messages_per_chat过滤逻辑
            min_messages_per_chat = self.get_config("diary_generation.min_messages_per_chat", DiaryConstants.MIN_MESSAGE_COUNT)
            logger.debug(f"[过滤调试] min_messages_per_chat配置: {min_messages_per_chat}")
            
            if min_messages_per_chat > 0:
                # 按聊天ID分组消息
                chat_message_counts = {}
                for msg in all_messages:
                    chat_id = msg.chat_id
                    if chat_id not in chat_message_counts:
                        chat_message_counts[chat_id] = []
                    chat_message_counts[chat_id].append(msg)
                
                logger.info("[过滤调试] 消息按聊天ID分组结果:")
                for chat_id, messages in chat_message_counts.items():
                    logger.info(f"[过滤调试] 聊天 {chat_id}: {len(messages)}条消息")
                
                # 过滤出满足最少消息数量要求的聊天
                filtered_messages = []
                kept_chats = 0
                filtered_chats = 0
                
                for chat_id, messages in chat_message_counts.items():
                    if len(messages) >= min_messages_per_chat:
                        filtered_messages.extend(messages)
                        kept_chats += 1
                        logger.debug(f"[过滤调试] 聊天 {chat_id} 保留: {len(messages)}条消息 >= {min_messages_per_chat}")
                    else:
                        filtered_chats += 1
                        logger.debug(f"[过滤调试] 聊天 {chat_id} 过滤: {len(messages)}条消息 < {min_messages_per_chat}")
                
                # 重新按时间排序
                filtered_messages.sort(key=lambda x: x.time)
                logger.info(f"[过滤调试] 消息过滤结果: 原始{len(all_messages)}条 → 过滤后{len(filtered_messages)}条")
                logger.info(f"[过滤调试] 聊天过滤结果: 总聊天{len(chat_message_counts)}个 → 保留{kept_chats}个,过滤{filtered_chats}个")
                return filtered_messages
            
            return all_messages
            
        except Exception as e:
            logger.error(f"获取日期消息失败: {e}")
            return []

    def get_weather_by_emotion(self, messages: List[Any]) -> str:
        """
        根据聊天内容的情感分析生成天气
        
        通过分析聊天记录中的情感词汇，智能生成符合当天情感氛围的天气描述。
        这个功能为日记增加了情感色彩，让天气描述更贴合实际的聊天氛围。
        
        Args:
            messages (List[Any]): 消息列表，用于情感分析
        
        Returns:
            str: 生成的天气描述，如"晴"、"多云"、"雨"等
        
        Note:
            - 情感分析基于预定义的情感词汇库
            - 天气映射规则：开心→晴天，难过→雨天，愤怒→阴天等
        Examples:
            >>> weather = action.get_weather_by_emotion(messages)
            >>> print(weather)  # "晴" 或 "多云" 等
        """
        if not messages:
            weather_options = ["晴", "多云", "阴", "多云转晴"]
            return random.choice(weather_options)
        
        all_content = " ".join([msg.processed_plain_text or '' for msg in messages])
        
        happy_words = ["哈哈", "笑", "开心", "高兴", "棒", "好", "赞", "爱", "喜欢"]
        sad_words = ["难过", "伤心", "哭", "痛苦", "失望"]
        angry_words = ["无语", "醉了", "服了", "烦", "气", "怒"]
        calm_words = ["平静", "安静", "淡定", "还好", "一般"]
        
        happy_count = sum(1 for word in happy_words if word in all_content)
        sad_count = sum(1 for word in sad_words if word in all_content)
        angry_count = sum(1 for word in angry_words if word in all_content)
        calm_count = sum(1 for word in calm_words if word in all_content)
        
        if happy_count >= 2:
            return "晴"
        elif happy_count >= 1:
            return "多云转晴"
        elif sad_count >= 1:
            return "雨"
        elif angry_count >= 1:
            return "阴"
        elif calm_count >= 1:
            return "多云"
        else:
            return "多云"
    
    def get_date_with_weather(self, date: str, weather: str) -> str:
        """
        生成带天气的日期字符串,兼容跨平台
        
        将日期和天气信息组合成适合日记开头的格式化字符串。
        
        Args:
            date (str): 日期字符串，格式为YYYY-MM-DD
            weather (str): 天气描述
        
        Returns:
            str: 格式化的日期天气字符串
        
        Examples:
            >>> date_weather = action.get_date_with_weather("2025-01-15", "晴")
            >>> print(date_weather)  # "2025年1月15日,星期三,晴。"
        """
        try:
            date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            weekday = weekdays[date_obj.weekday()]
            
            year = date_obj.year
            month = date_obj.month
            day = date_obj.day
            
            return f"{year}年{month}月{day}日,{weekday},{weather}。"
            
        except Exception as e:
            logger.error(f"日期格式化失败: {e}")
            return f"{date},{weather}。"

    def build_chat_timeline(self, messages: List[Any]) -> str:
        """
        构建完整对话时间线（包含图片消息）
        
        将消息列表转换为结构化的时间线文本，支持文本和图片消息的统一展示。
        按时间段分组显示消息，并区分Bot消息和用户消息。图片消息以[图片]前缀标识。
        
        Args:
            messages (List[Any]): 按时间排序的消息列表
        
        Returns:
            str: 格式化的时间线文本，包含时间段标记和消息内容（文本+图片）
        
        Note:
            - 消息按小时分组，显示为"上午X点"、"下午X点"、"晚上X点"
            - Bot消息显示为"我:"，用户消息显示为"昵称:"
            - 图片消息显示为"[图片]描述"格式
            - 长文本消息会被截断为50字符并添加省略号
            - 统计信息存储在self._timeline_stats中供后续使用
        
        Examples:
            >>> timeline = action.build_chat_timeline(messages)
            >>> print(timeline)
            # 【上午9点】
            # 张三: 早上好！
            # 张三: [图片]早餐照片
            # 我: 早上好，今天天气不错呢
            # 【下午2点】
            # 李四: 下午有什么安排吗？
        """
        if not messages:
            return "今天没有什么特别的对话。"
        
        timeline_parts = []
        current_hour = -1
        bot_qq_account = str(config_api.get_global_config("bot.qq_account", ""))
        
        # 初始化图片处理器
        from .image_processor import ImageProcessor
        image_processor = ImageProcessor()
        
        bot_message_count = 0
        user_message_count = 0
        
        for msg in messages:
            msg_time = datetime.datetime.fromtimestamp(msg.time)
            hour = msg_time.hour
            # 按时间段分组
            if hour != current_hour:
                if 6 <= hour < 12:
                    time_period = f"上午{hour}点"
                elif 12 <= hour < 18:
                    time_period = f"下午{hour}点"
                else:
                    time_period = f"晚上{hour}点"
                timeline_parts.append(f"\n【{time_period}】")
                current_hour = hour
            
            # 获取用户信息
            nickname = msg.user_info.user_nickname or '某人'
            user_id = str(msg.user_info.user_id)
            
            # 判断消息类型并处理
            if image_processor._is_image_message(msg):
                # 图片消息处理
                description = image_processor._get_image_description(msg)
                if user_id == bot_qq_account:
                    timeline_parts.append(f"我: [图片]{description}")
                    bot_message_count += 1
                else:
                    timeline_parts.append(f"{nickname}: [图片]{description}")
                    user_message_count += 1
            else:
                # 文本消息处理（保持原有逻辑）
                content = msg.processed_plain_text or ''
                if content and len(content) > 50:
                    content = content[:50] + "..."
                # 判断是否为Bot消息
                if user_id == bot_qq_account:
                    timeline_parts.append(f"我: {content}")
                    bot_message_count += 1
                else:
                    timeline_parts.append(f"{nickname}: {content}")
                    user_message_count += 1
        
        # 存储统计信息
        self._timeline_stats = {
            "total_messages": len(messages),
            "bot_messages": bot_message_count,
            "user_messages": user_message_count
        }
        
        return "\n".join(timeline_parts)

    def _estimate_tokens(self, text: str) -> int:
        """估算文本的token数量"""
        import re
        
        # 中文字符数
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        # 其他字符数
        other_chars = len(text) - chinese_chars
        # 中文约1.5字符=1token,英文约4字符=1token
        estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)
        return estimated_tokens

    def estimate_token_count(self, text: str) -> int:
        """
        估算文本的token数量（公共方法）
        
        这是一个公共方法，用于外部调用估算文本的token数量。
        主要用于commands模块中的token限制检查。
        
        Args:
            text (str): 要估算的文本内容
            
        Returns:
            int: 估算的token数量
            
        Note:
            - 中文字符约1.5字符=1token
            - 英文字符约4字符=1token
            - 这是一个近似估算，实际token数可能有差异
        """
        return self._estimate_tokens(text)

    def _truncate_messages(self, timeline: str, max_tokens: int) -> str:
        """按token数量截断时间线"""
        current_tokens = self._estimate_tokens(timeline)
        
        if current_tokens <= max_tokens:
            return timeline
        
        # 按比例截断
        ratio = max_tokens / current_tokens
        target_length = int(len(timeline) * ratio * 0.95)  # 留5%余量
        
        # 智能截断,保持语句完整
        truncated = timeline[:target_length]
        
        # 找到最后一个完整句子
        for i in range(len(truncated) - 1, len(truncated) // 2, -1):  # 1为偏移量，2为半分除数
            if truncated[i] in ['。', '！', '？', '\n']:
                truncated = truncated[:i+1]
                break
        
        logger.info(f"时间线截断: {current_tokens}→{self._estimate_tokens(truncated)} tokens")
        return truncated + "\n\n[聊天记录过长,已截断]"

    def truncate_timeline_by_tokens(self, timeline: str, max_tokens: int) -> str:
        """
        按token数量截断时间线（公共方法）
        
        这是一个公共方法，用于外部调用按token数量截断时间线内容。
        主要用于commands模块中的50k token限制处理。
        
        Args:
            timeline (str): 要截断的时间线文本
            max_tokens (int): 最大token数量限制
            
        Returns:
            str: 截断后的时间线文本
            
        Note:
            - 使用智能截断，保持语句完整性
            - 会在截断处添加提示信息
            - 预留5%的token余量以确保安全
        """
        return self._truncate_messages(timeline, max_tokens)

    def smart_truncate(self, text: str, max_length: int = DiaryConstants.MAX_DIARY_LENGTH) -> str:
        """智能截断文本,保持语句完整性"""
        if len(text) <= max_length:
            return text
        
        for i in range(max_length - 3, max_length // 2, -1):  # 3为截断后缀长度，2为半分除数
            if text[i] in ['。', '！', '？', '~']:
                return text[:i+1]
        
        return text[:max_length-3] + "..."

    async def generate_with_custom_model(self, prompt: str) -> Tuple[bool, str]:
        """
        使用自定义模型生成日记
        
        调用用户配置的自定义模型API来生成日记内容。支持OpenAI格式的API接口，
        包括各种第三方服务商。提供完整的错误处理和超时控制。
        
        Args:
            prompt (str): 生成日记的提示词，包含完整的上下文信息
        
        Returns:
            Tuple[bool, str]: (是否成功, 生成的内容或错误信息)
        
        Raises:
            Exception: 当API调用失败时
        
        Note:
            - 需要配置custom_model.api_key和相关参数
            - 支持自定义超时时间和温度参数
            - 自动处理上下文长度限制
        
        Examples:
            >>> success, content = await action.generate_with_custom_model(prompt)
            >>> if success:
            >>>     print(f"生成成功: {content}")
            >>> else:
            >>>     print(f"生成失败: {content}")
        """
        try:
            api_key = self.get_config("custom_model.api_key", "")
            if not api_key or api_key == "sk-your-siliconflow-key-here":
                return False, "自定义模型API密钥未配置"
            
            # 创建OpenAI客户端
            client = AsyncOpenAI(
                base_url=self.get_config("custom_model.api_url", "https://api.siliconflow.cn/v1"),
                api_key=api_key
            )
            
            # 获取并验证API超时配置
            api_timeout = self.get_config("custom_model.api_timeout", 300)
            # 验证API超时是否在合理范围内（1-6000秒，即100分钟）
            if not (1 <= api_timeout <= 6000):
                logger.info(f"API超时配置不合理: {api_timeout}秒，将使用默认值")
                api_timeout = 300
            
            # 调用模型
            completion = await client.chat.completions.create(
                model=self.get_config("custom_model.model_name", "Pro/deepseek-ai/DeepSeek-V3"),
                messages=[{"role": "user", "content": prompt}],
                temperature=self.get_config("custom_model.temperature", 0.7),
                timeout=api_timeout
            )
            
            if completion.choices and len(completion.choices) > 0:
                content = completion.choices[0].message.content
            else:
                raise RuntimeError("模型返回的响应为空或格式错误")
            logger.info(f"自定义模型调用成功: {self.get_config('custom_model.model_name')}")
            return True, content
            
        except Exception as e:
            logger.error(f"自定义模型调用失败: {e}")
            return False, f"自定义模型调用出错: {str(e)}"

    async def generate_with_default_model(self, prompt: str, timeline: str) -> Tuple[bool, str]:
        """
        使用默认模型生成日记（带50k截断）
        
        调用系统配置的默认模型来生成日记内容。自动处理50k token限制，
        确保输入不会超过模型的上下文长度。
        
        Args:
            prompt (str): 生成日记的提示词
            timeline (str): 时间线文本，用于token计算和截断
        
        Returns:
            Tuple[bool, str]: (是否成功, 生成的内容或错误信息)
        
        Note:
            - 强制执行50k token限制，确保兼容性
            - 当超过限制时自动截断时间线内容
            - 使用系统的replyer模型配置
        
        Examples:
            >>> success, content = await action.generate_with_default_model(prompt, timeline)
            >>> if success:
            >>>     print(f"生成成功: {content}")
        """
        try:
            # 默认模型使用50k截断，确保更好的兼容性
            max_tokens = DiaryConstants.TOKEN_LIMIT_50K
            current_tokens = self._estimate_tokens(timeline)
            
            if current_tokens > max_tokens:
                logger.debug("默认模型:聊天记录超过50k tokens,进行截断")
                # 重新构建截断后的prompt
                truncated_timeline = self._truncate_messages(timeline, max_tokens)
                prompt = prompt.replace(timeline, truncated_timeline)
            
            models = llm_api.get_available_models()
            model = models.get("replyer")
            if not model:
                return False, "未找到默认模型: replyer"
            
            success, diary_content, _, _ = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model,
                request_type="plugin.diary_generation"
            )
            
            if not success or not diary_content:
                return False, "默认模型生成日记失败"
            
            return True, diary_content
            
        except Exception as e:
            logger.error(f"默认模型调用失败: {e}")
            return False, f"默认模型调用出错: {str(e)}"

    async def _publish_to_qzone(self, diary_content: str, date: str) -> bool:
        """
        发布日记到QQ空间
        
        将生成的日记内容发布到QQ空间，并更新本地存储的发布状态。
        
        Args:
            diary_content (str): 要发布的日记内容
            date (str): 日记日期
        
        Returns:
            bool: 发布是否成功
        
        Note:
            - 需要配置Napcat服务的主机和端口
            - 发布结果会更新到本地存储中
            - 失败时会记录详细的错误信息
        """
        try:
            napcat_host = self.get_config("qzone_publishing.napcat_host", "127.0.0.1")
            napcat_port = self.get_config("qzone_publishing.napcat_port", "9998")
            napcat_token = self.get_config("qzone_publishing.napcat_token", "")
            success = await self.qzone_api.publish_diary(diary_content, napcat_host, napcat_port, napcat_token)
            
            diary_data = await self.storage.get_diary(date)
            if diary_data:
                if success:
                    diary_data["is_published_qzone"] = True
                    diary_data["qzone_publish_time"] = time.time()
                    diary_data["status"] = "一切正常"
                    diary_data["error_message"] = ""
                else:
                    diary_data["is_published_qzone"] = False
                    diary_data["status"] = "报错:发说说失败"
                    diary_data["error_message"] = "原因:QQ空间发布失败,可能是cookie过期或网络问题"
                
                await self.storage.save_diary(diary_data)
            
            return success
                
        except Exception as e:
            logger.error(f"发布QQ空间失败: {e}")
            
            diary_data = await self.storage.get_diary(date)
            if diary_data:
                diary_data["is_published_qzone"] = False
                diary_data["status"] = "报错:发说说失败"
                diary_data["error_message"] = f"原因:发布异常 - {str(e)}"
                await self.storage.save_diary(diary_data)
            
            return False

    async def generate_diary(self, date: str, target_chats: List[str] = None) -> Tuple[bool, str]:
        """
        生成日记的核心逻辑（使用内置API）
        
        这是日记生成的主要入口方法，协调整个日记生成流程。包括消息获取、
        人设分析、内容生成、格式化和存储等完整步骤。
        
        Args:
            date (str): 要生成日记的日期，格式为YYYY-MM-DD
            target_chats (List[str], optional): 指定的聊天ID列表，为None时使用配置
        
        Returns:
            Tuple[bool, str]: (是否成功, 生成的日记内容或错误信息)
        
        Workflow:
            1. 获取Bot人设信息
            2. 获取指定日期的聊天消息
            3. 验证消息数量是否足够
            4. 构建对话时间线
            5. 生成情感化的天气信息
            6. 构建生成提示词
            7. 选择模型并生成内容
            8. 进行字数控制和格式化
            9. 保存到本地存储
        Note:
            - 支持自定义模型和默认模型两种生成方式
            - 自动处理Token限制和消息截断
            - 生成的日记会自动保存到本地JSON文件
            - 包含完整的错误处理和状态记录
        
        Examples:
            >>> success, result = await action.generate_diary("2025-01-15")
            >>> if success:
            >>>     print(f"日记生成成功: {result}")
            >>> else:
            >>>     print(f"生成失败: {result}")
        """
        try:
            # 1. 获取bot人设（由服务内部处理，无需在此处获取）
            # 2. 获取当天消息（使用内置API）
            messages = await self.get_daily_messages(date, target_chats)
            
            if len(messages) < self.get_config("diary_generation.min_message_count", DiaryConstants.MIN_MESSAGE_COUNT):
                return False, f"当天消息数量不足({len(messages)}条),无法生成日记"
            
            # 由共享服务负责完整生成逻辑（含token截断/模型选择/保存）
            success, diary_content = await self.diary_service.generate_diary_from_messages(date, messages, force_50k=True)
            
            if not success or not diary_content:
                return False, diary_content or "模型生成日记失败"
            
            # 7. 字数控制：仅使用最大上限
            max_length = self.get_config("qzone_publishing.qzone_max_word_count", 350)
            if not isinstance(max_length, int):
                max_length = 350
            if max_length > DiaryConstants.MAX_DIARY_LENGTH:
                max_length = DiaryConstants.MAX_DIARY_LENGTH
            if len(diary_content) > max_length:
                diary_content = self.smart_truncate(diary_content, max_length)
            
            return True, diary_content
            
        except Exception as e:
            logger.error(f"生成日记失败: {e}")
            try:
                failed_record = {
                    "date": date,
                    "diary_content": "",
                    "word_count": 0,
                    "generation_time": time.time(),
                    "weather": "阴",
                    "bot_messages": 0,
                    "user_messages": 0,
                    "is_published_qzone": False,
                    "qzone_publish_time": None,
                    "status": "报错:生成失败",
                    "error_message": f"原因:{str(e)}"
                }
                await self.storage.save_diary(failed_record)
            except Exception as save_error:
                logger.error(f"保存失败记录出错: {save_error}")
            
            return False, f"生成日记时出错: {str(e)}"

    async def execute(self) -> Tuple[bool, str]:
        """
        执行日记生成
        
        Action的标准执行入口，从action_data中获取参数并执行日记生成流程。
        
        Returns:
            Tuple[bool, str]: (是否成功, 执行结果描述)
        
        Note:
            - 这是BaseAction接口的实现方法
            - 会自动发送生成结果到聊天流
            - 支持手动和定时两种调用方式
        """
        date = self.action_data.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))
        target_chats = self.action_data.get("target_chats", [])
        
        success, result = await self.generate_diary(date, target_chats)
        
        if success:
            await self.send_text(f"📖 {date} 的日记已生成:\n\n{result}")
            return True, f"成功生成{date}的日记"
        else:
            await self.send_text(f"❌ 日记生成失败:{result}")
            return False, result
