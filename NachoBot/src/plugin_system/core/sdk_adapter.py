from typing import Dict, List, Tuple, Type, Any, Union
from src.common.logger import get_logger
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.component_types import (
    CommandInfo,
    ActionInfo,
    EventHandlerInfo,
    ToolInfo,
)
from src.plugin_system.base.config_types import ConfigField

logger = get_logger("sdk_adapter")

def create_adapter_class(plugin_dir: str, manifest: Dict[str, Any], module: Any) -> Type[BasePlugin]:
    # Extract info from manifest
    v2_plugin_name = manifest.get("name", "v2_adapted_plugin")
    plugin_name = v2_plugin_name.replace(".", "_").replace(" ", "_").lower()
    description = manifest.get("description", "Adapted V2 SDK Plugin")
    version = manifest.get("version", "1.0.0")
    
    # 1. Scan configuration schema
    # Create a temporary instance to inspect properties
    v2_plugin_inst = module.create_plugin()
    config_model = getattr(v2_plugin_inst, "config_model", None)
    
    schema = {}
    section_descriptions = {}
    if config_model:
        try:
            for section_name, field_info in config_model.model_fields.items():
                section_cls = field_info.annotation
                if hasattr(section_cls, "model_fields"):
                    section_fields = {}
                    section_descriptions[section_name] = section_cls.__doc__ or f"{section_name} section config"
                    for field_name, f_info in section_cls.model_fields.items():
                        default_val = f_info.default
                        from pydantic_core import PydanticUndefined
                        if default_val is PydanticUndefined:
                            default_val = ""
                        
                        desc_str = f_info.description or f"{field_name} configuration"
                        section_fields[field_name] = ConfigField(
                            type=f_info.annotation or str,
                            default=default_val,
                            description=desc_str,
                            example=None,
                            required=False,
                            choices=[]
                        )
                    schema[section_name] = section_fields
        except Exception as e:
            logger.error(f"Error compiling config model schema: {e}")
            
    # 2. Closure wrappers for command / action / tool / event execution
    def make_cmd_execute(v2_plugin, func):
        async def execute(self_cmd):
            if not getattr(v2_plugin, "_on_load_called", False):
                try:
                    await v2_plugin.on_load()
                    v2_plugin._on_load_called = True
                except Exception as e:
                    logger.error(f"Error calling V2 on_load: {e}")
            
            res = await func(
                stream_id=self_cmd.message.chat_stream.stream_id,
                text=self_cmd.message.processed_plain_text,
                matched_groups=self_cmd.matched_groups,
                message=self_cmd.message
            )
            if isinstance(res, tuple):
                res_list = list(res)
                while len(res_list) < 3:
                    res_list.append(None)
                if res_list[2] is None:
                    res_list[2] = False
                return tuple(res_list[:3])
            elif isinstance(res, str):
                return True, res, False
            return True, None, False
        return execute

    def make_action_execute(v2_plugin, func):
        async def execute(self_action):
            if not getattr(v2_plugin, "_on_load_called", False):
                try:
                    await v2_plugin.on_load()
                    v2_plugin._on_load_called = True
                except Exception as e:
                    logger.error(f"Error calling V2 on_load: {e}")
            
            kwargs = {
                "stream_id": self_action.chat_id,
                **self_action.action_data
            }
            res = await func(**kwargs)
            if isinstance(res, tuple):
                return res
            return True, str(res) if res is not None else ""
        return execute

    def make_tool_execute(v2_plugin, func):
        async def execute(self_tool, function_args):
            if not getattr(v2_plugin, "_on_load_called", False):
                try:
                    await v2_plugin.on_load()
                    v2_plugin._on_load_called = True
                except Exception as e:
                    logger.error(f"Error calling V2 on_load: {e}")
            
            res = await func(**function_args)
            if isinstance(res, dict):
                return res
            return {"result": str(res)}
        return execute

    def make_ev_execute(v2_plugin, func):
        async def execute(self_ev, message):
            if not getattr(v2_plugin, "_on_load_called", False):
                try:
                    await v2_plugin.on_load()
                    v2_plugin._on_load_called = True
                except Exception as e:
                    logger.error(f"Error calling V2 on_load: {e}")
            
            msg_arg = None
            stream_id = ""
            if message:
                msg_arg = {
                    "raw_message": message.processed_plain_text,
                    "plain_text": message.processed_plain_text,
                    "processed_plain_text": message.processed_plain_text,
                }
                if hasattr(message, "to_dict"):
                    try:
                        msg_arg.update(message.to_dict())
                    except Exception:
                        pass
                if hasattr(message, "chat_stream") and message.chat_stream:
                    stream_id = message.chat_stream.stream_id
            
            res = await func(message=msg_arg, stream_id=stream_id)
            if isinstance(res, tuple):
                res_list = list(res)
                while len(res_list) < 5:
                    res_list.append(None)
                return tuple(res_list[:5])
            return True, True, None, None, None
        return execute

    # 3. Dynamic functions to assign to constructed class
    def init_fn(self, plugin_dir: str):
        super(type(self), self).__init__(plugin_dir)
        self.v2_plugin = module.create_plugin()
        self.v2_plugin.ctx.plugin = self  # Context connection
        
        # Map configuration properties into Pydantic config model
        if self.v2_plugin.config_model:
            try:
                self.v2_plugin.config = self.v2_plugin.config_model(**self.config)
            except Exception as e:
                logger.error(f"Error configuring V2 Pydantic model: {e}")
                self.v2_plugin.config = self.v2_plugin.config_model()

    def load_manifest_fn(self):
        super(type(self), self)._load_manifest()
            
    def validate_manifest_fn(self):
        if self.manifest_data:
            # Spoof manifest fields for V1 validation compatibility
            if self.manifest_data.get("manifest_version") == 2:
                self.manifest_data["manifest_version"] = 1
            if "host_application" in self.manifest_data:
                self.manifest_data.pop("host_application")
            if "sdk" in self.manifest_data:
                self.manifest_data.pop("sdk")
            if "capabilities" in self.manifest_data:
                self.manifest_data.pop("capabilities")
        super(type(self), self)._validate_manifest()

    def get_plugin_components_fn(self) -> List[Union[
        Tuple[ActionInfo, Type[BaseAction]],
        Tuple[CommandInfo, Type[BaseCommand]],
        Tuple[EventHandlerInfo, Type[BaseEventHandler]],
        Tuple[ToolInfo, Type[BaseTool]],
    ]]:
        components = []
        
        # Scan decorated callbacks
        for attr_name in dir(self.v2_plugin):
            attr = getattr(self.v2_plugin, attr_name)
            if not hasattr(attr, "__maibot_decorators__"):
                continue
                
            for deco in attr.__maibot_decorators__:
                dec_type = deco["type"]
                
                if dec_type == "command":
                    cmd_cls_name = f"{self.plugin_name}_cmd_{deco['name']}"
                    dyn_cmd_class = type(
                        cmd_cls_name,
                        (BaseCommand,),
                        {
                            "command_name": deco["name"],
                            "command_description": deco["description"],
                            "command_pattern": deco["pattern"],
                            "execute": make_cmd_execute(self.v2_plugin, attr)
                        }
                    )
                    components.append((dyn_cmd_class.get_command_info(), dyn_cmd_class))
                    
                elif dec_type == "action":
                    act_cls_name = f"{self.plugin_name}_act_{deco['name']}"
                    activation_type = deco.get("activation_type", "always")
                    
                    dyn_action_class = type(
                        act_cls_name,
                        (BaseAction,),
                        {
                            "action_name": deco["name"],
                            "action_description": deco["description"],
                            "activation_type": activation_type,
                            "action_parameters": deco.get("action_parameters", {}),
                            "action_require": deco.get("action_require", []),
                            "associated_types": deco.get("associated_types", []),
                            "activation_keywords": deco.get("activation_keywords", []),
                            "keyword_case_sensitive": deco.get("keyword_case_sensitive", False),
                            "parallel_action": deco.get("parallel_action", True),
                            "execute": make_action_execute(self.v2_plugin, attr)
                        }
                    )
                    components.append((dyn_action_class.get_action_info(), dyn_action_class))
                    
                elif dec_type == "tool":
                    tool_cls_name = f"{self.plugin_name}_tool_{deco['name']}"
                    
                    # Translate V2 tool parameter specs
                    v1_params = []
                    for param in deco.get("parameters", []):
                        if isinstance(param, dict):
                            p_name = param.get("name", "")
                            p_type = param.get("param_type", "string")
                            p_desc = param.get("description", "")
                            p_req = param.get("required", False)
                            p_choices = param.get("choices", None)
                        else:
                            p_name = getattr(param, "name", "")
                            p_type = getattr(param, "param_type", "string")
                            p_desc = getattr(param, "description", "")
                            p_req = getattr(param, "required", False)
                            p_choices = getattr(param, "choices", None)
                        
                        from src.plugin_system.base.component_types import ToolParamType as V1ToolParamType
                        v1_type = V1ToolParamType.STRING
                        p_type_str = str(p_type).lower()
                        if "int" in p_type_str:
                            v1_type = V1ToolParamType.INTEGER
                        elif "float" in p_type_str or "number" in p_type_str:
                            v1_type = V1ToolParamType.FLOAT
                        elif "bool" in p_type_str:
                            v1_type = V1ToolParamType.BOOLEAN
                            
                        v1_params.append((p_name, v1_type, p_desc, p_req, p_choices))
                        
                    dyn_tool_class = type(
                        tool_cls_name,
                        (BaseTool,),
                        {
                            "name": deco["name"],
                            "description": deco["description"],
                            "parameters": v1_params,
                            "available_for_llm": True,
                            "execute": make_tool_execute(self.v2_plugin, attr)
                        }
                    )
                    components.append((dyn_tool_class.get_tool_info(), dyn_tool_class))
                    
                elif dec_type == "event_handler":
                    ev_cls_name = f"{self.plugin_name}_ev_{deco['name']}"
                    
                    v2_ev_type = deco["event_type"]
                    v2_ev_val = getattr(v2_ev_type, "value", str(v2_ev_type))
                    from src.plugin_system.base.component_types import EventType as V1EventType
                    try:
                        v1_ev_type = V1EventType(v2_ev_val)
                    except ValueError:
                        v1_ev_type = V1EventType.UNKNOWN
                        
                    dyn_ev_class = type(
                        ev_cls_name,
                        (BaseEventHandler,),
                        {
                            "event_type": v1_ev_type,
                            "handler_name": deco["name"],
                            "handler_description": deco["description"],
                            "intercept_message": deco.get("intercept_message", False),
                            "execute": make_ev_execute(self.v2_plugin, attr)
                        }
                    )
                    components.append((dyn_ev_class.get_handler_info(), dyn_ev_class))
                    
        return components

    # 4. Construct class via type() to bypass Python class body enclosing-scope gotcha
    class_attrs = {
        "plugin_name": plugin_name,
        "enable_plugin": True,
        "dependencies": manifest.get("dependencies", []),
        "python_dependencies": [],
        "config_file_name": "config.toml",
        "config_schema": schema,
        "get_plugin_components": get_plugin_components_fn,
        "__init__": init_fn,
        "_load_manifest": load_manifest_fn,
        "_validate_manifest": validate_manifest_fn,
    }
    
    SDKPluginAdapter = type("SDKPluginAdapter", (BasePlugin,), class_attrs)
    return SDKPluginAdapter