"""个人信息API模块

提供个人信息查询功能，用于插件获取用户相关信息
使用方式：
    from src.plugin_system.apis import person_api
    person_id = person_api.get_person_id("qq", 123456)
    value = await person_api.get_person_value(person_id, "nickname")
"""

from typing import Any
from src.common.logger import get_logger
from src.person_info.person_info import Person

logger = get_logger("person_api")


# =============================================================================
# 个人信息API函数
# =============================================================================


def get_person_id(platform: str, user_id: int | str) -> str:
    """根据平台和用户ID获取person_id

    Args:
        platform: 平台名称，如 "qq", "telegram" 等
        user_id: 用户ID

    Returns:
        str: 唯一的person_id（MD5哈希值）

    示例:
        person_id = person_api.get_person_id("qq", 123456)
    """
    try:
        return Person(platform=platform, user_id=str(user_id)).person_id
    except Exception as e:
        logger.error(f"[PersonAPI] 获取person_id失败: platform={platform}, user_id={user_id}, error={e}")
        return ""


async def get_person_value(person_id: str, field_name: str, default: Any = None) -> Any:
    """根据person_id和字段名获取某个值

    Args:
        person_id: 用户的唯一标识ID
        field_name: 要获取的字段名，如 "nickname", "impression" 等
        default: 当字段不存在或获取失败时返回的默认值

    Returns:
        Any: 字段值或默认值

    示例:
        nickname = await person_api.get_person_value(person_id, "nickname", "未知用户")
        impression = await person_api.get_person_value(person_id, "impression")
    """
    try:
        person = Person(person_id=person_id)
        value = getattr(person, field_name)
        return value if value is not None else default
    except Exception as e:
        logger.error(f"[PersonAPI] 获取用户信息失败: person_id={person_id}, field={field_name}, error={e}")
        return default


def get_person_id_by_name(person_name: str) -> str:
    """根据用户名获取person_id

    Args:
        person_name: 用户名

    Returns:
        str: person_id，如果未找到返回空字符串

    示例:
        person_id = person_api.get_person_id_by_name("张三")
    """
    try:
        person = Person(person_name=person_name)
        return person.person_id
    except Exception as e:
        logger.error(f"[PersonAPI] 根据用户名获取person_id失败: person_name={person_name}, error={e}")
        return ""


def get_person_platform_user_id(person_id: str, target_platform: str) -> str:
    """获取指定平台的 user_id

    当用户绑定了多个平台时，Person.user_id 可能是任意平台的 ID。
    使用此函数可精确获取目标平台的 user_id。

    Args:
        person_id: 用户的唯一标识ID
        target_platform: 目标平台名称，如 "qq", "bilibili"

    Returns:
        str: 该平台的 user_id，未找到返回空字符串

    示例:
        qq_id = person_api.get_person_platform_user_id(person_id, "qq")
    """
    try:
        person = Person(person_id=person_id)
        return person.get_user_id_for_platform(target_platform)
    except Exception as e:
        logger.error(f"[PersonAPI] 获取平台user_id失败: person_id={person_id}, platform={target_platform}, error={e}")
        return ""


def get_person_platform_user_ids(person_id: str) -> dict:
    """获取该用户所有已绑定平台的 user_id 映射

    Args:
        person_id: 用户的唯一标识ID

    Returns:
        dict: {platform: user_id} 映射，如 {"qq": "12345", "bilibili": "67890"}

    示例:
        ids = person_api.get_person_platform_user_ids(person_id)
        qq_id = ids.get("qq", "")
    """
    try:
        person = Person(person_id=person_id)
        return person.get_platform_user_ids()
    except Exception as e:
        logger.error(f"[PersonAPI] 获取平台映射失败: person_id={person_id}, error={e}")
        return {}

