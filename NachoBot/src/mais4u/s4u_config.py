import os
import tomlkit
import shutil
from datetime import datetime
from tomlkit import TOMLDocument
from tomlkit.items import Table
from dataclasses import dataclass, fields, MISSING, field
from typing import TypeVar, Type, Any, get_origin, get_args, Literal
from src.common.logger import get_logger

logger = get_logger("s4u_config")


# 新增：兼容dict和tomlkit Table
def is_dict_like(obj):
    return isinstance(obj, (dict, Table))


# 新增：递归将Table转为dict
def table_to_dict(obj):
    if isinstance(obj, Table):
        return {k: table_to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, dict):
        return {k: table_to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [table_to_dict(i) for i in obj]
    else:
        return obj


# 获取mais4u模块目录
MAIS4U_ROOT = os.path.dirname(__file__)
CONFIG_DIR = os.path.join(MAIS4U_ROOT, "config")
TEMPLATE_PATH = os.path.join(CONFIG_DIR, "s4u_config_template.toml")
CONFIG_PATH = os.path.join(CONFIG_DIR, "s4u_config.toml")

# S4U配置版本
S4U_VERSION = "1.1.0"

T = TypeVar("T", bound="S4UConfigBase")


@dataclass
class S4UConfigBase:
    """S4U配置类的基类"""

    @classmethod
    def from_dict(cls: Type[T], data: dict[str, Any]) -> T:
        """从字典加载配置字段"""
        data = table_to_dict(data)  # 递归转dict，兼容tomlkit Table
        if not is_dict_like(data):
            raise TypeError(f"Expected a dictionary, got {type(data).__name__}")

        init_args: dict[str, Any] = {}

        for f in fields(cls):
            field_name = f.name

            if field_name.startswith("_"):
                # 跳过以 _ 开头的字段
                continue

            if field_name not in data:
                if f.default is not MISSING or f.default_factory is not MISSING:
                    # 跳过未提供且有默认值/默认构造方法的字段
                    continue
                else:
                    raise ValueError(f"Missing required field: '{field_name}'")

            value = data[field_name]
            field_type = f.type

            try:
                init_args[field_name] = cls._convert_field(value, field_type)  # type: ignore
            except TypeError as e:
                raise TypeError(f"Field '{field_name}' has a type error: {e}") from e
            except Exception as e:
                raise RuntimeError(f"Failed to convert field '{field_name}' to target type: {e}") from e

        return cls(**init_args)

    @classmethod
    def _convert_field(cls, value: Any, field_type: Type[Any]) -> Any:
        """转换字段值为指定类型"""
        # 如果是嵌套的 dataclass，递归调用 from_dict 方法
        if isinstance(field_type, type) and issubclass(field_type, S4UConfigBase):
            if not is_dict_like(value):
                raise TypeError(f"Expected a dictionary for {field_type.__name__}, got {type(value).__name__}")
            return field_type.from_dict(value)

        # 处理泛型集合类型（list, set, tuple）
        field_origin_type = get_origin(field_type)
        field_type_args = get_args(field_type)

        if field_origin_type in {list, set, tuple}:
            if not isinstance(value, list):
                raise TypeError(f"Expected an list for {field_type.__name__}, got {type(value).__name__}")

            if field_origin_type is list:
                if (
                    field_type_args
                    and isinstance(field_type_args[0], type)
                    and issubclass(field_type_args[0], S4UConfigBase)
                ):
                    return [field_type_args[0].from_dict(item) for item in value]
                return [cls._convert_field(item, field_type_args[0]) for item in value]
            elif field_origin_type is set:
                return {cls._convert_field(item, field_type_args[0]) for item in value}
            elif field_origin_type is tuple:
                if len(value) != len(field_type_args):
                    raise TypeError(
                        f"Expected {len(field_type_args)} items for {field_type.__name__}, got {len(value)}"
                    )
                return tuple(cls._convert_field(item, arg) for item, arg in zip(value, field_type_args, strict=False))

        if field_origin_type is dict:
            if not is_dict_like(value):
                raise TypeError(f"Expected a dictionary for {field_type.__name__}, got {type(value).__name__}")

            if len(field_type_args) != 2:
                raise TypeError(f"Expected a dictionary with two type arguments for {field_type.__name__}")
            key_type, value_type = field_type_args

            return {cls._convert_field(k, key_type): cls._convert_field(v, value_type) for k, v in value.items()}

        # 处理基础类型，例如 int, str 等
        if field_origin_type is type(None) and value is None:  # 处理Optional类型
            return None

        # 处理Literal类型
        if field_origin_type is Literal or get_origin(field_type) is Literal:
            allowed_values = get_args(field_type)
            if value in allowed_values:
                return value
            else:
                raise TypeError(f"Value '{value}' is not in allowed values {allowed_values} for Literal type")

        if field_type is Any or isinstance(value, field_type):
            return value

        # 其他类型，尝试直接转换
        try:
            return field_type(value)
        except (ValueError, TypeError) as e:
            raise TypeError(f"Cannot convert {type(value).__name__} to {field_type.__name__}") from e


@dataclass
class S4UModelConfig(S4UConfigBase):
    """S4U模型配置类"""

    # 主要对话模型配置
    chat: dict[str, Any] = field(default_factory=lambda: {})
    """主要对话模型配置"""

    # 规划模型配置（原model_motion）
    motion: dict[str, Any] = field(default_factory=lambda: {})
    """规划模型配置"""

    # 情感分析模型配置
    emotion: dict[str, Any] = field(default_factory=lambda: {})
    """情感分析模型配置"""

    # 记忆模型配置
    memory: dict[str, Any] = field(default_factory=lambda: {})
    """记忆模型配置"""

    # 工具使用模型配置
    tool_use: dict[str, Any] = field(default_factory=lambda: {})
    """工具使用模型配置"""

    # 嵌入模型配置
    embedding: dict[str, Any] = field(default_factory=lambda: {})
    """嵌入模型配置"""

    # 视觉语言模型配置
    vlm: dict[str, Any] = field(default_factory=lambda: {})
    """视觉语言模型配置"""

    # 知识库模型配置
    knowledge: dict[str, Any] = field(default_factory=lambda: {})
    """知识库模型配置"""

    # 实体提取模型配置
    entity_extract: dict[str, Any] = field(default_factory=lambda: {})
    """实体提取模型配置"""

    # 问答模型配置
    qa: dict[str, Any] = field(default_factory=lambda: {})
    """问答模型配置"""


@dataclass
class S4UConfig(S4UConfigBase):
    """S4U聊天系统配置类"""

    enable_s4u: bool = False
    """是否启用S4U聊天系统"""

    message_timeout_seconds: int = 120
    """普通消息存活时间（秒），超过此时间的消息将被丢弃"""

    at_bot_priority_bonus: float = 100.0
    """@机器人时的优先级加成分数"""

    recent_message_keep_count: int = 6
    """保留最近N条消息，超出范围的普通消息将被移除"""

    typing_delay: float = 0.1
    """打字延迟时间（秒），模拟真实打字速度"""

    chars_per_second: float = 15.0
    """每秒字符数，用于计算动态打字延迟"""

    min_typing_delay: float = 0.2
    """最小打字延迟（秒）"""

    max_typing_delay: float = 2.0
    """最大打字延迟（秒）"""

    enable_dynamic_typing_delay: bool = False
    """是否启用基于文本长度的动态打字延迟"""

    vip_queue_priority: bool = True
    """是否启用VIP队列优先级系统"""

    enable_message_interruption: bool = True
    """是否允许高优先级消息中断当前回复"""

    enable_old_message_cleanup: bool = True
    """是否自动清理过旧的普通消息"""

    enable_streaming_output: bool = True
    """是否启用流式输出，false时全部生成后一次性发送"""

    max_context_message_length: int = 20
    """上下文消息最大长度"""

    max_core_message_length: int = 30
    """核心消息最大长度"""

    # 模型配置
    models: S4UModelConfig = field(default_factory=S4UModelConfig)
    """S4U模型配置"""

    # 兼容性字段，保持向后兼容


@dataclass
class S4UGlobalConfig(S4UConfigBase):
    """S4U总配置类"""

    s4u: S4UConfig
    S4U_VERSION: str = S4U_VERSION


def update_s4u_config():
    """更新S4U配置文件"""
    # 创建配置目录（如果不存在）
    os.makedirs(CONFIG_DIR, exist_ok=True)

    # 检查模板文件是否存在
    if not os.path.exists(TEMPLATE_PATH):
        logger.error(f"S4U配置模板文件不存在: {TEMPLATE_PATH}")
        logger.error("请确保模板文件存在后重新运行")
        raise FileNotFoundError(f"S4U配置模板文件不存在: {TEMPLATE_PATH}")

    # 检查配置文件是否存在
    if not os.path.exists(CONFIG_PATH):
        logger.info("S4U配置文件不存在，从模板创建新配置")
        shutil.copy2(TEMPLATE_PATH, CONFIG_PATH)
        logger.info(f"已创建S4U配置文件: {CONFIG_PATH}")
        return

    # 读取旧配置文件和模板文件
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        old_config = tomlkit.load(f)
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        new_config = tomlkit.load(f)

    # 检查version是否相同
    if old_config and "inner" in old_config and "inner" in new_config:
        old_version = old_config["inner"].get("version")  # type: ignore
        new_version = new_config["inner"].get("version")  # type: ignore
        if old_version and new_version and old_version == new_version:
            logger.info(f"检测到S4U配置文件版本号相同 (v{old_version})，跳过更新")
            return
        else:
            logger.info(f"检测到S4U配置版本号不同: 旧版本 v{old_version} -> 新版本 v{new_version}")
    else:
        logger.info("S4U配置文件未检测到版本号，可能是旧版本。将进行更新")

    # 创建备份目录
    old_config_dir = os.path.join(CONFIG_DIR, "old")
    os.makedirs(old_config_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    old_backup_path = os.path.join(old_config_dir, f"s4u_config_{timestamp}.toml")

    # 移动旧配置文件到old目录
    shutil.move(CONFIG_PATH, old_backup_path)
    logger.info(f"已备份旧S4U配置文件到: {old_backup_path}")

    # 复制模板文件到配置目录
    shutil.copy2(TEMPLATE_PATH, CONFIG_PATH)
    logger.info(f"已创建新S4U配置文件: {CONFIG_PATH}")

    def update_dict(target: TOMLDocument | dict | Table, source: TOMLDocument | dict):
        """
        将source字典的值更新到target字典中（如果target中存在相同的键）
        """
        for key, value in source.items():
            # 跳过version字段的更新
            if key == "version":
                continue
            if key in target:
                target_value = target[key]
                if isinstance(value, dict) and isinstance(target_value, (dict, Table)):
                    update_dict(target_value, value)
                else:
                    try:
                        # 对数组类型进行特殊处理
                        if isinstance(value, list):
                            target[key] = tomlkit.array(str(value)) if value else tomlkit.array()
                        else:
                            # 其他类型使用item方法创建新值
                            target[key] = tomlkit.item(value)
                    except (TypeError, ValueError):
                        # 如果转换失败，直接赋值
                        target[key] = value

    # 将旧配置的值更新到新配置中
    logger.info("开始合并S4U新旧配置...")
    update_dict(new_config, old_config)

    # 保存更新后的配置（保留注释和格式）
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(new_config))

    logger.info("S4U配置文件更新完成")


def load_s4u_config(config_path: str) -> S4UGlobalConfig:
    """
    加载S4U配置文件
    :param config_path: 配置文件路径
    :return: S4UGlobalConfig对象
    """
    # 读取配置文件
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = tomlkit.load(f)

    # 创建S4UGlobalConfig对象
    try:
        return S4UGlobalConfig.from_dict(config_data)
    except Exception as e:
        logger.critical("S4U配置文件解析失败")
        raise e

    # 初始化S4U配置


logger.info(f"S4U当前版本: {S4U_VERSION}")
update_s4u_config()

s4u_config_main = load_s4u_config(config_path=CONFIG_PATH)
logger.info("S4U配置文件加载完成！")

s4u_config: S4UConfig = s4u_config_main.s4u
