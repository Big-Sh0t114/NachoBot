"""
日记插件核心模块包

这个包包含了日记插件的核心功能组件，负责处理日记的创建、管理、存储、调度和命令处理等核心业务逻辑。

主要模块：
- storage: 日记存储模块，处理日记数据的持久化和管理
- actions: 日记生成Action，负责日记的自动生成和发布
- scheduler: 日记调度器，处理定时任务和自动生成
- commands: 日记命令处理器，处理用户的日记管理命令
- image_processor: 图片消息处理，支持图片识别和描述获取

每个模块都遵循单一职责原则，提供清晰的接口和完善的错误处理机制。
"""

# 核心组件导入
from .storage import DiaryStorage, DiaryQzoneAPI
from .actions import DiaryGeneratorAction
from .scheduler import DiaryScheduler, EmotionAnalysisTool
from .commands import (
    DiaryManageCommand,
    DiaryGenerateCommand,
    DiaryGenerateAllCommand,
    DiaryListCommand,
    DiaryViewCommand,
    DiaryDebugCommand,
    DiaryHelpCommand,
)
from .image_processor import ImageProcessor, ImageData
from .utils import ChatIdResolver, DiaryConstants, MockChatStream, format_date_str

# 定义公开的API接口
__all__ = [
    # 核心存储和API
    'DiaryStorage',
    'DiaryQzoneAPI',
    
    # 功能组件
    'DiaryGeneratorAction',
    'DiaryScheduler',
    'DiaryManageCommand',
    'DiaryGenerateCommand',
    'DiaryGenerateAllCommand',
    'DiaryListCommand',
    'DiaryViewCommand',
    'DiaryDebugCommand',
    'DiaryHelpCommand',
    
    # 图片处理
    'ImageProcessor',
    'ImageData',
    
    # 工具和常量
    'ChatIdResolver',
    'DiaryConstants',
    'MockChatStream',
    'format_date_str','EmotionAnalysisTool',
]

# 版本信息
__version__ = '2.1.0'
__author__ = 'MaiBot Team'
__description__ = '日记插件核心功能模块'
