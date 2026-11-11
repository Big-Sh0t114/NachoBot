from enum import Enum
from copy import deepcopy
from typing import Any


class ToolParamType(Enum):
    """
    工具调用参数类型
    """

    STRING = "string"  # 字符串
    INTEGER = "integer"  # 整型
    FLOAT = "float"  # 浮点型
    BOOLEAN = "bool"  # 布尔型

    @property
    def json_schema_type(self) -> str:
        """Return the standard JSON Schema type for this parameter type."""
        return {
            ToolParamType.STRING: "string",
            ToolParamType.INTEGER: "integer",
            ToolParamType.FLOAT: "number",
            ToolParamType.BOOLEAN: "boolean",
        }[self]


class ToolParam:
    """
    工具调用参数
    """

    def __init__(
        self,
        name: str,
        param_type: ToolParamType,
        description: str,
        required: bool,
        enum_values: list[str | int | float | bool] | None = None,
    ):
        """
        初始化工具调用参数
        （不应直接修改ToolParam类，而应使用ToolOptionBuilder类来构建对象）
        :param name: 参数名称
        :param param_type: 参数类型
        :param description: 参数描述
        :param required: 是否必填
        """
        self.name: str = name
        self.param_type: ToolParamType = param_type
        self.description: str = description
        self.required: bool = required
        self.enum_values: list[str | int | float | bool] | None = enum_values


class ToolOption:
    """
    工具调用项
    """

    def __init__(
        self,
        name: str,
        description: str,
        params: list[ToolParam] | None = None,
        input_schema: dict[str, Any] | None = None,
    ):
        """
        初始化工具调用项
        （不应直接修改ToolOption类，而应使用ToolOptionBuilder类来构建对象）
        :param name: 工具名称
        :param description: 工具描述
        :param params: 工具参数列表
        """
        self.name: str = name
        self.description: str = description
        self.params: list[ToolParam] | None = params
        self.input_schema: dict[str, Any] | None = input_schema


def build_tool_input_schema(tool_option: ToolOption) -> dict[str, Any] | None:
    """Build a detached standard JSON Schema for a tool's input parameters.

    A provided raw schema is authoritative. Legacy parameter declarations are
    translated here so each provider receives the same schema representation.
    """
    if tool_option.input_schema is not None:
        return deepcopy(tool_option.input_schema)

    if not tool_option.params:
        return None

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in tool_option.params:
        property_schema: dict[str, Any] = {
            "type": param.param_type.json_schema_type,
            "description": param.description,
        }
        if param.enum_values:
            property_schema["enum"] = deepcopy(param.enum_values)
        properties[param.name] = property_schema
        if param.required:
            required.append(param.name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


class ToolOptionBuilder:
    """
    工具调用项构建器
    """

    def __init__(self):
        self.__name: str = ""
        self.__description: str = ""
        self.__params: list[ToolParam] = []
        self.__input_schema: dict[str, Any] | None = None

    def set_name(self, name: str) -> "ToolOptionBuilder":
        """
        设置工具名称
        :param name: 工具名称
        :return: ToolBuilder实例
        """
        if not name:
            raise ValueError("工具名称不能为空")
        self.__name = name
        return self

    def set_description(self, description: str) -> "ToolOptionBuilder":
        """
        设置工具描述
        :param description: 工具描述
        :return: ToolBuilder实例
        """
        if not description:
            raise ValueError("工具描述不能为空")
        self.__description = description
        return self

    def add_param(
        self,
        name: str,
        param_type: ToolParamType,
        description: str,
        required: bool = False,
        enum_values: list[str | int | float | bool] | None = None,
    ) -> "ToolOptionBuilder":
        """
        添加工具参数
        :param name: 参数名称
        :param param_type: 参数类型
        :param description: 参数描述
        :param required: 是否必填（默认为False）
        :return: ToolBuilder实例
        """
        if not name or not description:
            raise ValueError("参数名称/描述不能为空")

        self.__params.append(
            ToolParam(
                name=name,
                param_type=param_type,
                description=description,
                required=required,
                enum_values=enum_values,
            )
        )

        return self

    def set_input_schema(self, input_schema: dict[str, Any]) -> "ToolOptionBuilder":
        """Attach a raw JSON Schema for MCP and other schema-native tools."""
        if not isinstance(input_schema, dict):
            raise TypeError("input_schema 必须是字典")
        self.__input_schema = input_schema
        return self

    def build(self):
        """
        构建工具调用项
        :return: 工具调用项
        """
        if self.__name == "" or self.__description == "":
            raise ValueError("工具名称/描述不能为空")

        return ToolOption(
            name=self.__name,
            description=self.__description,
            params=None if len(self.__params) == 0 else self.__params,
            input_schema=self.__input_schema,
        )


class ToolCall:
    """
    来自模型反馈的工具调用
    """

    def __init__(
        self,
        call_id: str,
        func_name: str,
        args: dict | None = None,
    ):
        """
        初始化工具调用
        :param call_id: 工具调用ID
        :param func_name: 要调用的函数名称
        :param args: 工具调用参数
        """
        self.call_id: str = call_id
        self.func_name: str = func_name
        self.args: dict | None = args
