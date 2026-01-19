"""
图片消息处理模块

本模块为日记插件提供图片消息的识别、描述获取和发送者信息提取功能。
与现有的文本消息处理逻辑集成，支持统一时间线的生成。

主要功能:
- 图片消息识别：检测消息是否包含图片
- 图片描述获取：从MaiBot数据库获取图片描述信息  
- 发送者信息提取：获取图片发送者的昵称信息
- 防御性编程：完善的错误处理和降级策略

Dependencies:
    - src.plugin_system.apis: MaiBot内置API接口
    - src.common.data_models: 数据模型定义

Author: MaiBot Diary Plugin
Version: 2.1.0
"""

import datetime
import re
from dataclasses import dataclass
from typing import Any, Optional

from src.plugin_system.apis import (
    message_api,
    get_logger
)

logger = get_logger("diary_image_processor")


@dataclass
class ImageData:
    """
    图片数据结构
    
    用于存储从消息中提取的图片相关信息，包含图片标识、
    发送者信息、描述内容和时间戳等核心数据。
    
    Attributes:
        image_id (str): 图片的唯一标识符
        sender_nickname (str): 发送者的显示昵称
        description (str): 图片的描述内容
        timestamp (datetime): 消息的发送时间戳
    
    Examples:
        >>> img_data = ImageData(
        ...     image_id="pic_123456",
        ...     sender_nickname="张三",
        ...     description="早餐照片",
        ...     timestamp=datetime.datetime.now()
        ... )
    """
    image_id: str
    sender_nickname: str
    description: str
    timestamp: datetime.datetime


class ImageProcessor:
    """
    图片消息处理器
    
    负责处理日记插件中的图片消息相关功能，包括消息识别、
    信息提取和数据转换等核心操作。设计为工具类，提供
    静态方法供时间线构建时调用。
    
    主要方法:
    - _is_image_message: 检测消息是否为图片消息
    - _get_image_description: 获取图片描述信息
    - _get_sender_nickname: 获取发送者昵称
    - _generate_image_id: 生成图片唯一标识
    
    Note:
        所有方法都采用防御性编程设计，确保在API调用失败时
        能够提供合理的默认值，不会影响整体的日记生成流程。
    
    Examples:
        >>> processor = ImageProcessor()
        >>> is_image = processor._is_image_message(message)
        >>> if is_image:
        ...     description = processor._get_image_description(message)
        ...     nickname = processor._get_sender_nickname(message)
    """
    
    def __init__(self):
        """初始化图片处理器"""
        pass
    
    def _is_image_message(self, msg: Any) -> bool:
        """
        检测消息是否为图片消息
        
        通过多种方式判断消息是否包含图片内容，优先使用
        MaiBot提供的is_picid字段，备选方案检查消息文本内容。
        
        Args:
            msg (Any): MaiBot的DatabaseMessages消息对象
        
        Returns:
            bool: 如果消息包含图片则返回True，否则返回False
        
        Detection Logic:
            1. 优先检查is_picid字段（最可靠）
            2. 检查processed_plain_text中的[picid:xxx]格式
            3. 检查常见的图片标记[图片]和[image]
        
        Examples:
            >>> processor = ImageProcessor()
            >>> is_img = processor._is_image_message(message)
            >>> print(is_img)  # True or False
        """
        try:
            # 方法1: 检查is_picid字段（最可靠的方式）
            if hasattr(msg, 'is_picid') and msg.is_picid:
                logger.debug(f"通过is_picid字段检测到图片消息: {msg.is_picid}")
                return True
            
            # 方法2: 检查消息文本内容中的[picid:xxx]格式
            plain_text = getattr(msg, 'processed_plain_text', None) or ""
            if re.search(r'\[picid:[a-f0-9\-]+\]', plain_text):
                logger.debug("通过[picid:xxx]格式检测到图片消息")
                return True
            
            # 方法3: 检查常见的图片标记
            image_markers = ['[图片', '[image']
            for marker in image_markers:
                if marker in plain_text.lower():
                    logger.debug(f"通过{marker}标记检测到图片消息")
                    return True
            
            return False
            
        except Exception as e:
            logger.debug(f"图片消息检测失败: {e}")
            return False
    
    def _get_image_description(self, msg: Any) -> str:
        """
        获取图片描述信息
        
        从MaiBot数据库中获取图片的描述内容，优先使用
        translate_pid_to_description API获取存储的描述信息。
        
        Args:
            msg (Any): MaiBot的DatabaseMessages消息对象
        
        Returns:
            str: 图片的描述信息，失败时返回默认描述
        
        Fallback Strategy:
            1. 尝试使用is_picid从数据库获取描述
            2. 尝试从消息文本中提取[picid:xxx]格式的图片ID并获取描述
            3. 检查返回的描述是否有效（非空且非空白）
            4. 失败时返回默认描述"这是一张图片"
        
        Examples:
            >>> processor = ImageProcessor()
            >>> desc = processor._get_image_description(message)
            >>> print(desc)  # "风景照片" 或 "这是一张图片"
        """
        try:
            # 方法1: 优先从消息文本中提取真实的图片ID（修复关键问题）
            plain_text = getattr(msg, 'processed_plain_text', None) or ""
            picid_match = re.search(r'\[picid:([a-f0-9\-]+)\]', plain_text)
            if picid_match:
                real_image_id = picid_match.group(1)
                logger.debug(f"从消息文本中提取到真实图片ID: {real_image_id}")
                description = message_api.translate_pid_to_description(real_image_id)
                
                # 验证描述是否有效（不是默认值）
                if description and description.strip() and description.strip() != "[图片]":
                    logger.debug(f"成功获取真实图片描述: {description}")
                    return description.strip()
                else:
                    logger.debug(f"真实图片ID返回默认值: {description}")
            
            # 方法2: 尝试使用message_id作为图片ID（备选方案）
            if hasattr(msg, 'message_id') and msg.message_id:
                message_id = str(msg.message_id)
                logger.debug(f"尝试使用message_id作为图片ID: {message_id}")
                description = message_api.translate_pid_to_description(message_id)
                
                # 验证描述是否有效（不是默认值）
                if description and description.strip() and description.strip() != "[图片]":
                    logger.debug(f"通过message_id成功获取图片描述: {description}")
                    return description.strip()
                else:
                    logger.debug(f"message_id方式返回默认值: {description}")
            
            # 方法3: 检查是否有其他可能的图片ID字段
            for possible_field in ['pic_id', 'image_id', 'file_id']:
                if hasattr(msg, possible_field):
                    field_value = getattr(msg, possible_field)
                    if field_value and str(field_value) not in ['True', 'False', '']:
                        logger.debug(f"尝试使用{possible_field}字段作为图片ID: {field_value}")
                        description = message_api.translate_pid_to_description(str(field_value))
                        
                        if description and description.strip() and description.strip() != "[图片]":
                            logger.debug(f"通过{possible_field}字段成功获取图片描述: {description}")
                            return description.strip()
            
            # 获取发送者昵称，提供更有意义的默认描述
            sender_nickname = self._get_sender_nickname(msg)
            if sender_nickname and sender_nickname != "未知用户":
                default_desc = f"{sender_nickname}分享的图片"
            else:
                default_desc = "用户分享的图片"
            
            logger.debug(f"使用增强的默认图片描述: {default_desc}")
            return default_desc
            
        except Exception as e:
            logger.debug(f"获取图片描述失败: {e}")
            return "用户分享的图片"
    
    def _get_sender_nickname(self, msg: Any) -> str:
        """
        获取消息发送者的昵称
        
        按优先级顺序获取发送者的显示名称，优先使用群昵称，
        其次使用用户昵称，最后使用用户ID作为备选。
        
        Args:
            msg (Any): MaiBot的DatabaseMessages消息对象
        
        Returns:
            str: 发送者的显示昵称，失败时返回"未知用户"
        
        Priority Order:
            1. user_cardname (群聊中的昵称)
            2. user_nickname (用户的昵称)  
            3. user_id (用户ID作为备选)
            4. "未知用户" (完全失败时的默认值)
        
        Examples:
            >>> processor = ImageProcessor()
            >>> nickname = processor._get_sender_nickname(message)
            >>> print(nickname)  # "张三" 或 "12345" 或 "未知用户"
        """
        try:
            user_info = getattr(msg, 'user_info', None)
            if not user_info:
                return "未知用户"
            
            # 优先使用群昵称（群聊中的显示名称）
            if hasattr(user_info, 'user_cardname') and user_info.user_cardname:
                cardname = user_info.user_cardname.strip()
                if cardname:
                    return cardname
            
            # 其次使用用户昵称
            if hasattr(user_info, 'user_nickname') and user_info.user_nickname:
                nickname = user_info.user_nickname.strip()
                if nickname:
                    return nickname
            
            # 最后使用用户ID
            if hasattr(user_info, 'user_id') and user_info.user_id:
                return str(user_info.user_id)
            
            return "未知用户"
            
        except Exception as e:
            logger.debug(f"获取发送者昵称失败: {e}")
            return "未知用户"
    
    def _generate_image_id(self, msg: Any) -> str:
        """
        生成图片的唯一标识符
        
        为图片消息生成唯一的标识符，优先使用MaiBot提供的
        is_picid，备选方案从消息文本中提取图片ID。
        
        Args:
            msg (Any): MaiBot的DatabaseMessages消息对象
        
        Returns:
            str: 图片的唯一标识符
        
        ID Generation Logic:
            1. 优先使用is_picid作为图片ID
            2. 尝试从消息文本中提取[picid:xxx]格式的图片ID
            3. 备选使用"img_" + message_id的格式
            4. 确保返回的ID是字符串类型
        
        Examples:
            >>> processor = ImageProcessor()
            >>> img_id = processor._generate_image_id(message)
            >>> print(img_id)  # "pic_123456" 或 "img_msg_789"
        """
        try:
            # 方法1: 优先使用is_picid
            if hasattr(msg, 'is_picid') and msg.is_picid:
                logger.debug(f"使用is_picid作为图片ID: {msg.is_picid}")
                return str(msg.is_picid)
            
            # 方法2: 尝试从消息文本中提取图片ID
            plain_text = getattr(msg, 'processed_plain_text', None) or ""
            picid_match = re.search(r'\[picid:([a-f0-9\-]+)\]', plain_text)
            if picid_match:
                image_id = picid_match.group(1)
                logger.debug(f"从消息文本中提取图片ID: {image_id}")
                return image_id
            
            # 方法3: 备选方案使用消息ID
            if hasattr(msg, 'message_id') and msg.message_id:
                logger.debug(f"使用消息ID作为图片ID: img_{msg.message_id}")
                return f"img_{msg.message_id}"
            
            # 方法4: 最后的备选方案使用时间戳
            if hasattr(msg, 'time'):
                logger.debug(f"使用时间戳作为图片ID: img_{int(msg.time)}")
                return f"img_{int(msg.time)}"
            
            # 极端情况的默认值
            logger.debug(f"使用默认图片ID: img_unknown_{id(msg)}")
            return f"img_unknown_{id(msg)}"
            
        except Exception as e:
            logger.debug(f"生成图片ID失败: {e}")
            return f"img_error_{id(msg)}"
    
    def extract_image_data(self, msg: Any) -> Optional[ImageData]:
        """
        从消息中提取完整的图片数据
        
        这是一个便利方法，将图片消息的所有相关信息提取并
        封装为ImageData对象。
        
        原始设计意图：
        - 为未来的图片统计和分析功能预留接口
        - 用于调试和测试场景的完整数据提取
        - 支持图片相关的统计报告生成
        - 为图片消息的高级处理提供数据结构
        
        Args:
            msg (Any): MaiBot的DatabaseMessages消息对象
        
        Returns:
            Optional[ImageData]: 提取的图片数据对象，失败时返回None
        
        Note:
            此方法目前未在主要流程中使用，属于预留功能接口。
            保留此方法是为了未来可能的功能扩展，如：
            - 图片消息统计分析
            - 图片发送者活跃度分析
            - 图片内容分类和标签
            - 调试工具和数据导出
        
        Examples:
            >>> processor = ImageProcessor()
            >>> img_data = processor.extract_image_data(message)
            >>> if img_data:
            ...     print(f"{img_data.sender_nickname}: {img_data.description}")
        """
        try:
            if not self._is_image_message(msg):
                return None
            
            return ImageData(
                image_id=self._generate_image_id(msg),
                sender_nickname=self._get_sender_nickname(msg),
                description=self._get_image_description(msg),
                timestamp=datetime.datetime.fromtimestamp(msg.time)
            )
            
        except Exception as e:
            logger.error(f"提取图片数据失败: {e}")
            return None
