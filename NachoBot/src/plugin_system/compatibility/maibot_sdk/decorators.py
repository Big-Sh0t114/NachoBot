import pydantic
from typing import Any, List, Optional, Dict

Field = pydantic.Field

def Tool(name: str, description: str = "", parameters: Optional[List[Any]] = None):
    def decorator(func):
        if not hasattr(func, "__maibot_decorators__"):
            func.__maibot_decorators__ = []
        func.__maibot_decorators__.append({
            "type": "tool",
            "name": name,
            "description": description,
            "parameters": parameters or []
        })
        return func
    return decorator

def Action(
    name: str,
    description: str = "",
    activation_type: Any = "always",
    action_parameters: Optional[Dict[str, str]] = None,
    action_require: Optional[List[str]] = None,
    associated_types: Optional[List[str]] = None,
    activation_keywords: Optional[List[str]] = None,
    keyword_case_sensitive: bool = False,
    parallel_action: bool = False
):
    def decorator(func):
        if not hasattr(func, "__maibot_decorators__"):
            func.__maibot_decorators__ = []
        func.__maibot_decorators__.append({
            "type": "action",
            "name": name,
            "description": description,
            "activation_type": activation_type,
            "action_parameters": action_parameters or {},
            "action_require": action_require or [],
            "associated_types": associated_types or [],
            "activation_keywords": activation_keywords or [],
            "keyword_case_sensitive": keyword_case_sensitive,
            "parallel_action": parallel_action
        })
        return func
    return decorator

def Command(name: str, description: str = "", pattern: str = ""):
    def decorator(func):
        if not hasattr(func, "__maibot_decorators__"):
            func.__maibot_decorators__ = []
        func.__maibot_decorators__.append({
            "type": "command",
            "name": name,
            "description": description,
            "pattern": pattern
        })
        return func
    return decorator

def EventHandler(name: str, description: str = "", event_type: Any = "on_message", intercept_message: bool = False):
    def decorator(func):
        if not hasattr(func, "__maibot_decorators__"):
            func.__maibot_decorators__ = []
        func.__maibot_decorators__.append({
            "type": "event_handler",
            "name": name,
            "description": description,
            "event_type": event_type,
            "intercept_message": intercept_message
        })
        return func
    return decorator
