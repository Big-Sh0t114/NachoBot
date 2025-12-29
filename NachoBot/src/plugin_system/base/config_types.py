"""
插件系统配置类型定义
"""

from typing import Any, Optional, List
from dataclasses import dataclass, field


@dataclass
class ConfigField:
    """配置字段定义"""

    type: type  # 字段类型
    default: Any  # 默认值
    description: str  # 字段描述
    example: Optional[str] = None  # 示例值
    required: bool = False  # 是否必需
    choices: Optional[List[Any]] = field(default_factory=list)  # 可选值列表
