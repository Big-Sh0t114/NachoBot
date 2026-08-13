import re
from difflib import SequenceMatcher

from typing import Dict, List, Optional, Any, Pattern, Tuple, Union, Type

from src.common.logger import get_logger
from src.plugin_system.base.component_types import (
    ComponentInfo,
    ActionInfo,
    ToolInfo,
    CommandInfo,
    EventHandlerInfo,
    PluginInfo,
    ComponentType,
)
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.base_events_handler import BaseEventHandler

logger = get_logger("component_registry")


class ComponentRegistry:
    """统一的组件注册中心

    负责管理所有插件组件的注册、查询和生命周期管理
    """

    def __init__(self):
        # 命名空间式组件名构成法 f"{component_type}.{component_name}"
        self._components: Dict[str, ComponentInfo] = {}
        """组件注册表 命名空间式组件名 -> 组件信息"""
        self._components_by_type: Dict[ComponentType, Dict[str, ComponentInfo]] = {types: {} for types in ComponentType}
        """类型 -> 组件原名称 -> 组件信息"""
        self._components_classes: Dict[str, Type[Union[BaseCommand, BaseAction, BaseTool, BaseEventHandler]]] = {}
        """命名空间式组件名 -> 组件类"""

        # 插件注册表
        self._plugins: Dict[str, PluginInfo] = {}
        """插件名 -> 插件信息"""

        # Action特定注册表
        self._action_registry: Dict[str, Type[BaseAction]] = {}
        """Action注册表 action名 -> action类"""
        self._default_actions: Dict[str, ActionInfo] = {}
        """默认动作集，即启用的Action集，用于重置ActionManager状态"""

        # Command特定注册表
        self._command_registry: Dict[str, Type[BaseCommand]] = {}
        """Command类注册表 command名 -> command类"""
        self._command_patterns: Dict[Pattern, str] = {}
        """编译后的正则 -> command名"""

        # 工具特定注册表
        self._tool_registry: Dict[str, Type[BaseTool]] = {}  # 工具名 -> 工具类
        self._llm_available_tools: Dict[str, Type[BaseTool]] = {}  # llm可用的工具名 -> 工具类

        # EventHandler特定注册表
        self._event_handler_registry: Dict[str, Type[BaseEventHandler]] = {}
        """event_handler名 -> event_handler类"""
        self._enabled_event_handlers: Dict[str, Type[BaseEventHandler]] = {}
        """启用的事件处理器 event_handler名 -> event_handler类"""

        logger.info("组件注册中心初始化完成")

    # == 注册方法 ==

    def register_plugin(self, plugin_info: PluginInfo) -> bool:
        """注册插件

        Args:
            plugin_info: 插件信息

        Returns:
            bool: 是否注册成功
        """
        plugin_name = plugin_info.name

        if plugin_name in self._plugins:
            logger.warning(f"插件 {plugin_name} 已存在，跳过注册")
            return False

        self._plugins[plugin_name] = plugin_info
        logger.debug(f"已注册插件: {plugin_name} (组件数量: {len(plugin_info.components)})")
        return True

    def register_component(
        self,
        component_info: ComponentInfo,
        component_class: Type[Union[BaseCommand, BaseAction, BaseEventHandler, BaseTool]],
    ) -> bool:
        """注册组件

        Args:
            component_info (ComponentInfo): 组件信息
            component_class (Type[Union[BaseCommand, BaseAction, BaseEventHandler]]): 组件类

        Returns:
            bool: 是否注册成功
        """
        component_name = component_info.name
        component_type = component_info.component_type
        plugin_name = getattr(component_info, "plugin_name", "unknown")
        if "." in component_name:
            logger.error(f"组件名称 '{component_name}' 包含非法字符 '.'，请使用下划线替代")
            return False
        if "." in plugin_name:
            logger.error(f"插件名称 '{plugin_name}' 包含非法字符 '.'，请使用下划线替代")
            return False

        namespaced_name = f"{component_type}.{component_name}"

        if namespaced_name in self._components:
            existing_info = self._components[namespaced_name]
            existing_plugin = getattr(existing_info, "plugin_name", "unknown")

            logger.warning(
                f"组件名冲突: '{plugin_name}' 插件的 {component_type} 类型组件 '{component_name}' 已被插件 '{existing_plugin}' 注册，跳过此组件注册"
            )
            return False

        # 先完成类型特定注册，成功后才提交到通用注册表，避免半注册状态。
        ret = False
        try:
            match component_type:
                case ComponentType.ACTION if isinstance(component_info, ActionInfo) and issubclass(
                    component_class, BaseAction
                ):
                    ret = self._register_action_component(component_info, component_class)
                case ComponentType.COMMAND if isinstance(component_info, CommandInfo) and issubclass(
                    component_class, BaseCommand
                ):
                    ret = self._register_command_component(component_info, component_class)
                case ComponentType.TOOL if isinstance(component_info, ToolInfo) and issubclass(
                    component_class, BaseTool
                ):
                    ret = self._register_tool_component(component_info, component_class)
                case ComponentType.EVENT_HANDLER if isinstance(component_info, EventHandlerInfo) and issubclass(
                    component_class, BaseEventHandler
                ):
                    ret = self._register_event_handler_component(component_info, component_class)
                case _:
                    logger.error(f"组件 {component_name} 的信息、类或类型不匹配: {component_type}")
        except Exception as exc:
            logger.error(f"注册组件 {component_name} 失败: {exc}")
            # A type-specific registration may have mutated several indexes
            # before an arbitrary exception was raised.  Roll back every
            # index, including event admission, before reporting failure.
            self.rollback_component_registration(component_name, component_type)
            return False

        if not ret:
            return False

        self._components[namespaced_name] = component_info
        self._components_by_type[component_type][component_name] = component_info
        self._components_classes[namespaced_name] = component_class
        logger.debug(
            f"已注册{component_type}组件: '{component_name}' -> '{namespaced_name}' "
            f"({component_class.__name__}) [插件: {plugin_name}]"
        )
        return True

    def _register_action_component(self, action_info: ActionInfo, action_class: Type[BaseAction]) -> bool:
        """注册Action组件到Action特定注册表"""
        if not (action_name := action_info.name):
            logger.error(f"Action组件 {action_class.__name__} 必须指定名称")
            return False
        if not isinstance(action_info, ActionInfo) or not issubclass(action_class, BaseAction):
            logger.error(f"注册失败: {action_name} 不是有效的Action")
            return False

        self._action_registry[action_name] = action_class

        # 如果启用，添加到默认动作集
        if action_info.enabled:
            self._default_actions[action_name] = action_info

        return True

    def _register_command_component(self, command_info: CommandInfo, command_class: Type[BaseCommand]) -> bool:
        """注册Command组件到Command特定注册表"""
        if not (command_name := command_info.name):
            logger.error(f"Command组件 {command_class.__name__} 必须指定名称")
            return False
        if not isinstance(command_info, CommandInfo) or not issubclass(command_class, BaseCommand):
            logger.error(f"注册失败: {command_name} 不是有效的Command")
            return False

        # 如果启用了且有匹配模式
        compiled_pattern = None
        if command_info.enabled and command_info.command_pattern:
            compiled_pattern = re.compile(command_info.command_pattern, re.IGNORECASE | re.DOTALL)

        self._command_registry[command_name] = command_class
        if compiled_pattern is not None:
            if compiled_pattern not in self._command_patterns:
                self._command_patterns[compiled_pattern] = command_name
            else:
                logger.warning(
                    f"'{command_name}' 对应的命令模式与 '{self._command_patterns[compiled_pattern]}' 重复，忽略此命令"
                )

        return True

    def _register_tool_component(self, tool_info: ToolInfo, tool_class: Type[BaseTool]) -> bool:
        """注册Tool组件到Tool特定注册表"""
        tool_name = tool_info.name

        self._tool_registry[tool_name] = tool_class

        # 如果是llm可用的且启用的工具,添加到 llm可用工具列表
        if tool_info.enabled:
            self._llm_available_tools[tool_name] = tool_class

        return True

    def _register_event_handler_component(
        self, handler_info: EventHandlerInfo, handler_class: Type[BaseEventHandler]
    ) -> bool:
        if not (handler_name := handler_info.name):
            logger.error(f"EventHandler组件 {handler_class.__name__} 必须指定名称")
            return False
        if not isinstance(handler_info, EventHandlerInfo) or not issubclass(handler_class, BaseEventHandler):
            logger.error(f"注册失败: {handler_name} 不是有效的EventHandler")
            return False

        if not handler_info.enabled:
            self._event_handler_registry[handler_name] = handler_class
            logger.warning(f"EventHandler组件 {handler_name} 未启用")
            return True  # 未启用，但是也是注册成功

        from .events_manager import events_manager  # 延迟导入防止循环导入问题

        if events_manager.register_event_subscriber(handler_info, handler_class):
            self._event_handler_registry[handler_name] = handler_class
            self._enabled_event_handlers[handler_name] = handler_class
            return True
        else:
            logger.error(f"注册事件处理器 {handler_name} 失败")
            return False

    # === 组件移除相关 ===

    async def remove_component(self, component_name: str, component_type: ComponentType, plugin_name: str) -> bool:
        namespaced_name = f"{component_type.value}.{component_name}"
        target_component_info = self._components.get(namespaced_name)
        if (
            target_component_info is not None
            and plugin_name
            and target_component_info.plugin_name
            and target_component_info.plugin_name != plugin_name
        ):
            logger.error(
                f"插件 {plugin_name} 不能移除属于 {target_component_info.plugin_name} 的组件 {component_name}"
            )
            return False

        try:
            match component_type:
                case ComponentType.ACTION:
                    self._action_registry.pop(component_name, None)
                    self._default_actions.pop(component_name, None)
                case ComponentType.COMMAND:
                    self._command_registry.pop(component_name, None)
                    keys_to_remove = [k for k, v in self._command_patterns.items() if v == component_name]
                    for key in keys_to_remove:
                        self._command_patterns.pop(key, None)
                case ComponentType.TOOL:
                    self._tool_registry.pop(component_name, None)
                    self._llm_available_tools.pop(component_name, None)
                case ComponentType.EVENT_HANDLER:
                    from .events_manager import events_manager  # 延迟导入防止循环导入问题

                    if not await events_manager.unregister_event_subscriber(component_name):
                        logger.error(f"事件处理器 {component_name} 仍有执行中的任务，拒绝移除组件")
                        return False
                    self._event_handler_registry.pop(component_name, None)
                    self._enabled_event_handlers.pop(component_name, None)
            self._components.pop(namespaced_name, None)
            self._components_by_type[component_type].pop(component_name, None)
            self._components_classes.pop(namespaced_name, None)
            logger.info(f"组件 {component_name} 已移除")
            return True
        except Exception as e:
            logger.error(f"移除组件 {component_name} 时发生错误: {e}")
            return False

    def rollback_component_registration(self, component_name: str, component_type: ComponentType) -> None:
        """仅用于插件初始注册失败时的同步回滚；此时组件尚未对外运行。"""
        namespaced_name = f"{component_type.value}.{component_name}"
        match component_type:
            case ComponentType.ACTION:
                self._action_registry.pop(component_name, None)
                self._default_actions.pop(component_name, None)
            case ComponentType.COMMAND:
                self._command_registry.pop(component_name, None)
                self._command_patterns = {
                    pattern: name for pattern, name in self._command_patterns.items() if name != component_name
                }
            case ComponentType.TOOL:
                self._tool_registry.pop(component_name, None)
                self._llm_available_tools.pop(component_name, None)
            case ComponentType.EVENT_HANDLER:
                from .events_manager import events_manager

                events_manager.rollback_event_subscriber_registration(component_name)
                self._event_handler_registry.pop(component_name, None)
                self._enabled_event_handlers.pop(component_name, None)
        self._components.pop(namespaced_name, None)
        self._components_by_type[component_type].pop(component_name, None)
        self._components_classes.pop(namespaced_name, None)

    def remove_plugin_registry(self, plugin_name: str) -> bool:
        """移除插件注册信息

        Args:
            plugin_name: 插件名称

        Returns:
            bool: 是否成功移除
        """
        if plugin_name not in self._plugins:
            logger.debug(f"插件 {plugin_name} 未注册，无需移除")
            return True
        self._plugins.pop(plugin_name, None)
        logger.info(f"插件 {plugin_name} 已移除")
        return True

    # === 组件全局启用/禁用方法 ===

    def enable_component(self, component_name: str, component_type: ComponentType) -> bool:
        """全局的启用某个组件
        Parameters:
            component_name: 组件名称
            component_type: 组件类型
        Returns:
            bool: 启用成功返回True，失败返回False
        """
        target_component_class = self.get_component_class(component_name, component_type)
        target_component_info = self.get_component_info(component_name, component_type)
        if not target_component_class or not target_component_info:
            logger.warning(f"组件 {component_name} 未注册，无法启用")
            return False
        try:
            match component_type:
                case ComponentType.ACTION:
                    if not isinstance(target_component_info, ActionInfo):
                        return False
                    self._default_actions[component_name] = target_component_info
                case ComponentType.COMMAND:
                    if not isinstance(target_component_info, CommandInfo):
                        return False
                    if target_component_info.command_pattern:
                        pattern = re.compile(
                            target_component_info.command_pattern,
                            re.IGNORECASE | re.DOTALL,
                        )
                        self._command_patterns[pattern] = component_name
                case ComponentType.TOOL:
                    if not isinstance(target_component_info, ToolInfo) or not issubclass(
                        target_component_class, BaseTool
                    ):
                        return False
                    self._llm_available_tools[component_name] = target_component_class
                case ComponentType.EVENT_HANDLER:
                    if not isinstance(target_component_info, EventHandlerInfo) or not issubclass(
                        target_component_class, BaseEventHandler
                    ):
                        return False
                    from .events_manager import events_manager  # 延迟导入防止循环导入问题

                    if not events_manager.has_event_subscriber(component_name) and not events_manager.register_event_subscriber(
                        target_component_info, target_component_class
                    ):
                        return False
                    self._enabled_event_handlers[component_name] = target_component_class
                case _:
                    return False
            target_component_info.enabled = True
            logger.info(f"组件 {component_name} 已启用")
            return True
        except (TypeError, ValueError, re.error) as exc:
            logger.error(f"启用组件 {component_name} 失败: {exc}")
            return False

    async def disable_component(self, component_name: str, component_type: ComponentType) -> bool:
        """全局的禁用某个组件
        Parameters:
            component_name: 组件名称
            component_type: 组件类型
        Returns:
            bool: 禁用成功返回True，失败返回False
        """
        target_component_class = self.get_component_class(component_name, component_type)
        target_component_info = self.get_component_info(component_name, component_type)
        if not target_component_class or not target_component_info:
            logger.warning(f"组件 {component_name} 未注册，无法禁用")
            return False
        try:
            match component_type:
                case ComponentType.ACTION:
                    self._default_actions.pop(component_name, None)
                case ComponentType.COMMAND:
                    self._command_patterns = {k: v for k, v in self._command_patterns.items() if v != component_name}
                case ComponentType.TOOL:
                    self._llm_available_tools.pop(component_name, None)
                case ComponentType.EVENT_HANDLER:
                    from .events_manager import events_manager  # 延迟导入防止循环导入问题

                    if not await events_manager.unregister_event_subscriber(component_name):
                        logger.error(f"事件处理器 {component_name} 仍有执行中的任务，拒绝禁用组件")
                        return False
                    self._enabled_event_handlers.pop(component_name, None)
                case _:
                    return False
            target_component_info.enabled = False
            logger.info(f"组件 {component_name} 已禁用")
            return True
        except Exception as e:
            logger.error(f"禁用组件 {component_name} 时发生错误: {e}")
            return False

    # === 组件查询方法 ===
    def get_component_info(
        self, component_name: str, component_type: Optional[ComponentType] = None
    ) -> Optional[ComponentInfo]:
        # sourcery skip: class-extract-method
        """获取组件信息，支持自动命名空间解析

        Args:
            component_name: 组件名称，可以是原始名称或命名空间化的名称
            component_type: 组件类型，如果提供则优先在该类型中查找

        Returns:
            Optional[ComponentInfo]: 组件信息或None
        """
        # 1. 如果已经是命名空间化的名称，直接查找
        if "." in component_name:
            return self._components.get(component_name)

        # 2. 如果指定了组件类型，构造命名空间化的名称查找
        if component_type:
            namespaced_name = f"{component_type}.{component_name}"
            return self._components.get(namespaced_name)

        # 3. 如果没有指定类型，尝试在所有命名空间中查找
        candidates = []
        for namespace_prefix in [types.value for types in ComponentType]:
            namespaced_name = f"{namespace_prefix}.{component_name}"
            if component_info := self._components.get(namespaced_name):
                candidates.append((namespace_prefix, namespaced_name, component_info))

        if len(candidates) == 1:
            # 只有一个匹配，直接返回
            return candidates[0][2]
        elif len(candidates) > 1:
            # 多个匹配，记录警告并返回第一个
            namespaces = [ns for ns, _, _ in candidates]
            logger.warning(
                f"组件名称 '{component_name}' 在多个命名空间中存在: {namespaces}，使用第一个匹配项: {candidates[0][1]}"
            )
            return candidates[0][2]

        # 4. 都没找到
        return None

    def get_component_class(
        self,
        component_name: str,
        component_type: Optional[ComponentType] = None,
    ) -> Optional[Union[Type[BaseCommand], Type[BaseAction], Type[BaseEventHandler], Type[BaseTool]]]:
        """获取组件类，支持自动命名空间解析

        Args:
            component_name: 组件名称，可以是原始名称或命名空间化的名称
            component_type: 组件类型，如果提供则优先在该类型中查找

        Returns:
            Optional[Union[BaseCommand, BaseAction]]: 组件类或None
        """
        # 1. 如果已经是命名空间化的名称，直接查找
        if "." in component_name:
            return self._components_classes.get(component_name)

        # 2. 如果指定了组件类型，构造命名空间化的名称查找
        if component_type:
            namespaced_name = f"{component_type.value}.{component_name}"
            return self._components_classes.get(namespaced_name)

        # 3. 如果没有指定类型，尝试在所有命名空间中查找
        candidates = []
        for namespace_prefix in [types.value for types in ComponentType]:
            namespaced_name = f"{namespace_prefix}.{component_name}"
            if component_class := self._components_classes.get(namespaced_name):
                candidates.append((namespace_prefix, namespaced_name, component_class))

        if len(candidates) == 1:
            # 只有一个匹配，直接返回
            _, full_name, cls = candidates[0]
            logger.debug(f"自动解析组件: '{component_name}' -> '{full_name}'")
            return cls
        elif len(candidates) > 1:
            # 多个匹配，记录警告并返回第一个
            namespaces = [ns for ns, _, _ in candidates]
            logger.warning(
                f"组件名称 '{component_name}' 在多个命名空间中存在: {namespaces}，使用第一个匹配项: {candidates[0][1]}"
            )
            return candidates[0][2]

        # 4. 都没找到
        return None

    def get_components_by_type(self, component_type: ComponentType) -> Dict[str, ComponentInfo]:
        """获取指定类型的所有组件"""
        return self._components_by_type.get(component_type, {}).copy()

    def get_enabled_components_by_type(self, component_type: ComponentType) -> Dict[str, ComponentInfo]:
        """获取指定类型的所有启用组件"""
        components = self.get_components_by_type(component_type)
        return {name: info for name, info in components.items() if info.enabled}

    # === Action特定查询方法 ===

    def get_action_registry(self) -> Dict[str, Type[BaseAction]]:
        """获取Action注册表"""
        return self._action_registry.copy()

    def get_registered_action_info(self, action_name: str) -> Optional[ActionInfo]:
        """获取Action信息"""
        info = self.get_component_info(action_name, ComponentType.ACTION)
        return info if isinstance(info, ActionInfo) else None

    def get_default_actions(self) -> Dict[str, ActionInfo]:
        """获取默认动作集"""
        return self._default_actions.copy()

    # === Command特定查询方法 ===

    def get_command_registry(self) -> Dict[str, Type[BaseCommand]]:
        """获取Command注册表"""
        return self._command_registry.copy()

    def get_registered_command_info(self, command_name: str) -> Optional[CommandInfo]:
        """获取Command信息"""
        info = self.get_component_info(command_name, ComponentType.COMMAND)
        return info if isinstance(info, CommandInfo) else None

    def get_command_patterns(self) -> Dict[Pattern, str]:
        """获取Command模式注册表"""
        return self._command_patterns.copy()

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        """归一化命令字符串，便于近似匹配"""
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def suggest_command(self, text: str, max_suggestions: int = 2, cutoff: float = 0.6) -> List[str]:
        """
        根据用户输入的文本给出最接近的命令建议（适用于以#开头但未匹配到命令的情况）
        """
        if not text:
            return []

        raw = text.strip()
        if not raw.startswith("#"):
            return []

        body = raw.lstrip("#").strip()
        if not body:
            return []

        # 构造候选：首词，以及首词+第二个词的组合（处理“#diary generate”）
        tokens = re.split(r"\s+", body)
        candidate_tokens: List[str] = []
        if tokens:
            candidate_tokens.append(tokens[0])
            candidate_tokens.append(tokens[0].replace("_", ""))
        if len(tokens) >= 2:
            candidate_tokens.append(f"{tokens[0]}_{tokens[1]}")
            candidate_tokens.append(f"{tokens[0]}{tokens[1]}")

        normalized_candidates = []
        seen = set()
        for token in candidate_tokens:
            norm = self._normalize_command_text(token)
            if norm and norm not in seen:
                normalized_candidates.append(norm)
                seen.add(norm)

        if not normalized_candidates:
            return []

        command_names = list(self._command_registry.keys())
        normalized_commands = [(name, self._normalize_command_text(name)) for name in command_names]

        scored: List[Tuple[float, str]] = []
        for cand in normalized_candidates:
            for cmd_name, norm_cmd in normalized_commands:
                if not norm_cmd:
                    continue
                ratio = SequenceMatcher(None, cand, norm_cmd).ratio()
                if ratio >= cutoff:
                    scored.append((ratio, cmd_name))

        if not scored:
            return []

        # 去重并按相似度排序
        scored.sort(key=lambda x: x[0], reverse=True)
        suggestions = []
        seen_cmd = set()
        for _, name in scored:
            if name not in seen_cmd:
                suggestions.append(name)
                seen_cmd.add(name)
            if len(suggestions) >= max_suggestions:
                break

        return suggestions

    def find_command_by_text(self, text: str) -> Optional[Tuple[Type[BaseCommand], dict, CommandInfo]]:
        # sourcery skip: use-named-expression, use-next
        """根据文本查找匹配的命令

        Args:
            text: 输入文本

        Returns:
            Tuple: (命令类, 匹配的命名组, 是否拦截消息, 插件名) 或 None
        """

        candidates = [pattern for pattern in self._command_patterns if pattern.match(text)]
        if not candidates:
            return None
        if len(candidates) > 1:
            logger.warning(f"文本 '{text}' 匹配到多个命令模式: {candidates}，使用第一个匹配")
        command_name = self._command_patterns[candidates[0]]
        command_info: CommandInfo = self.get_registered_command_info(command_name)  # type: ignore
        return (
            self._command_registry[command_name],
            candidates[0].match(text).groupdict(),  # type: ignore
            command_info,
        )

    # === Tool 特定查询方法 ===
    def get_tool_registry(self) -> Dict[str, Type[BaseTool]]:
        """获取Tool注册表"""
        return self._tool_registry.copy()

    def get_llm_available_tools(self) -> Dict[str, Type[BaseTool]]:
        """获取LLM可用的Tool列表"""
        return self._llm_available_tools.copy()

    def get_registered_tool_info(self, tool_name: str) -> Optional[ToolInfo]:
        """获取Tool信息

        Args:
            tool_name: 工具名称

        Returns:
            ToolInfo: 工具信息对象，如果工具不存在则返回 None
        """
        info = self.get_component_info(tool_name, ComponentType.TOOL)
        return info if isinstance(info, ToolInfo) else None

    # === EventHandler 特定查询方法 ===

    def get_event_handler_registry(self) -> Dict[str, Type[BaseEventHandler]]:
        """获取事件处理器注册表"""
        return self._event_handler_registry.copy()

    def get_registered_event_handler_info(self, handler_name: str) -> Optional[EventHandlerInfo]:
        """获取事件处理器信息"""
        info = self.get_component_info(handler_name, ComponentType.EVENT_HANDLER)
        return info if isinstance(info, EventHandlerInfo) else None

    def get_enabled_event_handlers(self) -> Dict[str, Type[BaseEventHandler]]:
        """获取启用的事件处理器"""
        return self._enabled_event_handlers.copy()

    # === 插件查询方法 ===

    def get_plugin_info(self, plugin_name: str) -> Optional[PluginInfo]:
        """获取插件信息"""
        return self._plugins.get(plugin_name)

    def get_all_plugins(self) -> Dict[str, PluginInfo]:
        """获取所有插件"""
        return self._plugins.copy()

    # def get_enabled_plugins(self) -> Dict[str, PluginInfo]:
    #     """获取所有启用的插件"""
    #     return {name: info for name, info in self._plugins.items() if info.enabled}

    def get_plugin_components(self, plugin_name: str) -> List[ComponentInfo]:
        """获取插件的所有组件"""
        plugin_info = self.get_plugin_info(plugin_name)
        return plugin_info.components if plugin_info else []

    def get_plugin_config(self, plugin_name: str) -> Optional[dict]:
        """获取插件配置

        Args:
            plugin_name: 插件名称

        Returns:
            Optional[dict]: 插件配置字典或None
        """
        # 从插件管理器获取插件实例的配置
        from src.plugin_system.core.plugin_manager import plugin_manager

        plugin_instance = plugin_manager.get_plugin_instance(plugin_name)
        return plugin_instance.config if plugin_instance else None

    def get_registry_stats(self) -> Dict[str, Any]:
        """获取注册中心统计信息"""
        action_components: int = 0
        command_components: int = 0
        tool_components: int = 0
        events_handlers: int = 0
        for component in self._components.values():
            if component.component_type == ComponentType.ACTION:
                action_components += 1
            elif component.component_type == ComponentType.COMMAND:
                command_components += 1
            elif component.component_type == ComponentType.TOOL:
                tool_components += 1
            elif component.component_type == ComponentType.EVENT_HANDLER:
                events_handlers += 1
        return {
            "action_components": action_components,
            "command_components": command_components,
            "tool_components": tool_components,
            "event_handlers": events_handlers,
            "total_components": len(self._components),
            "total_plugins": len(self._plugins),
            "components_by_type": {
                component_type.value: len(components) for component_type, components in self._components_by_type.items()
            },
            "enabled_components": len([c for c in self._components.values() if c.enabled]),
            "enabled_plugins": len([p for p in self._plugins.values() if p.enabled]),
        }


component_registry = ComponentRegistry()
