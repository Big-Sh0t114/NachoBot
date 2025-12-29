from dataclasses import dataclass, fields, MISSING
from typing import TypeVar, Type, Any, get_origin, get_args, Literal, Dict, Union

T = TypeVar("T", bound="ConfigBase")

TOML_DICT_TYPE = {
    int,
    float,
    str,
    bool,
    list,
    dict,
}


@dataclass
class ConfigBase:
    """配置类的基类"""

    @classmethod
    def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
        """从字典加载配置字段"""
        if not isinstance(data, dict):
            raise TypeError(f"Expected a dictionary, got {type(data).__name__}")

        init_args: Dict[str, Any] = {}

        for f in fields(cls):
            field_name = f.name
            field_type = f.type
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
            try:
                init_args[field_name] = cls._convert_field(value, field_type)
            except TypeError as e:
                raise TypeError(f"字段 '{field_name}' 出现类型错误: {e}") from e
            except Exception as e:
                raise RuntimeError(f"无法将字段 '{field_name}' 转换为目标类型，出现错误: {e}") from e

        return cls(**init_args)

    @classmethod
    def _convert_field(cls, value: Any, field_type: Type[Any]) -> Any:
        """
        转换字段值为指定类型

        1. 对于嵌套的 dataclass，递归调用相应的 from_dict 方法
        2. 对于泛型集合类型（list, set, tuple），递归转换每个元素
        3. 对于基础类型（int, str, float, bool），直接转换
        4. 对于其他类型，尝试直接转换，如果失败则抛出异常
        """
        # 如果是嵌套的 dataclass，递归调用 from_dict 方法
        if isinstance(field_type, type) and issubclass(field_type, ConfigBase):
            return field_type.from_dict(value)

        field_origin_type = get_origin(field_type)
        field_args_type = get_args(field_type)

        # 处理泛型集合类型（list, set, tuple）
        if field_origin_type in {list, set, tuple}:
            # 检查提供的value是否为list
            if not isinstance(value, list):
                raise TypeError(f"Expected an list for {field_type.__name__}, got {type(value).__name__}")

            if field_origin_type is list:
                return [cls._convert_field(item, field_args_type[0]) for item in value]
            if field_origin_type is set:
                return {cls._convert_field(item, field_args_type[0]) for item in value}
            if field_origin_type is tuple:
                # 检查提供的value长度是否与类型参数一致
                if len(value) != len(field_args_type):
                    raise TypeError(
                        f"Expected {len(field_args_type)} items for {field_type.__name__}, got {len(value)}"
                    )
                return tuple(cls._convert_field(item, arg_type) for item, arg_type in zip(value, field_args_type))

        if field_origin_type is dict:
            # 检查提供的value是否为dict
            if not isinstance(value, dict):
                raise TypeError(f"Expected a dictionary for {field_type.__name__}, got {type(value).__name__}")

            # 检查字典的键值类型
            if len(field_args_type) != 2:
                raise TypeError(f"Expected a dictionary with two type arguments for {field_type.__name__}")
            key_type, value_type = field_args_type

            return {cls._convert_field(k, key_type): cls._convert_field(v, value_type) for k, v in value.items()}

        # 处理Optional类型
        if field_origin_type is Union:  # assert get_origin(Optional[Any]) is Union
            if value is None:
                return None
            # 如果有数据，检查实际类型
            if type(value) not in field_args_type:
                raise TypeError(f"Expected {field_args_type} for {field_type.__name__}, got {type(value).__name__}")
            return cls._convert_field(value, field_args_type[0])

        # 处理int, str, float, bool等基础类型
        if field_origin_type is None:
            if isinstance(value, field_type):
                return field_type(value)
            else:
                raise TypeError(f"Expected {field_type.__name__}, got {type(value).__name__}")

        # 处理Literal类型
        if field_origin_type is Literal:
            # 获取Literal的允许值
            allowed_values = get_args(field_type)
            if value in allowed_values:
                return value
            else:
                raise TypeError(f"Value '{value}' is not in allowed values {allowed_values} for Literal type")

        # 处理其他类型
        if field_type is Any:
            return value

        # 其他类型直接转换
        try:
            return field_type(value)
        except (ValueError, TypeError) as e:
            raise TypeError(f"无法将 {type(value).__name__} 转换为 {field_type.__name__}") from e

    def __str__(self):
        """返回配置类的字符串表示"""
        return f"{self.__class__.__name__}({', '.join(f'{f.name}={getattr(self, f.name)}' for f in fields(self))})"
