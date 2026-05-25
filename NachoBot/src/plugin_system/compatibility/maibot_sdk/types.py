from enum import Enum
from dataclasses import dataclass
from typing import Optional, List, Any

class ActivationType(str, Enum):
    ALWAYS = "always"
    KEYWORD = "keyword"
    COMMAND = "command"
    AI = "ai"

class EventType(str, Enum):
    ON_MESSAGE = "on_message"
    ON_START = "on_start"
    ON_STOP = "on_stop"

class ToolParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"

@dataclass
class ToolParameterInfo:
    name: str
    param_type: ToolParamType | str
    description: str = ""
    required: bool = False
    choices: Optional[List[Any]] = None
