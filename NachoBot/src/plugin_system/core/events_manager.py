import asyncio
from typing import List, Dict, Optional, Set, Type, Tuple, TYPE_CHECKING

from src.chat.message_receive.message import MessageRecv, MessageSending
from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.logger import get_logger
from src.plugin_system.base.component_types import EventType, EventHandlerInfo, NachoMessages, CustomEventHandlerResult
from src.plugin_system.base.base_events_handler import BaseEventHandler
from .global_announcement_manager import global_announcement_manager

if TYPE_CHECKING:
    from src.common.data_models.llm_data_model import LLMGenerationDataModel

logger = get_logger("events_manager")


class EventsManager:
    def __init__(self, handler_cancel_timeout: float = 5.0):
        # 有权重的 events 订阅者注册表
        self._events_subscribers: Dict[EventType | str, List[BaseEventHandler]] = {}
        self._handler_mapping: Dict[str, Type[BaseEventHandler]] = {}  # 事件处理器映射表
        self._handler_instances: Dict[str, BaseEventHandler] = {}
        self._handler_generations: Dict[str, int] = {}
        self._handler_generation_counter = 0
        self._handler_tasks: Dict[str, Set[asyncio.Task]] = {}  # 事件处理器正在处理的任务
        self._closing_handlers: Set[str] = set()
        self._handler_cancel_timeout = max(0.0, handler_cancel_timeout)
        self._events_result_history: Dict[EventType | str, List[CustomEventHandlerResult]] = {}  # 事件的结果历史记录
        self._history_enable_map: Dict[EventType | str, bool] = {}  # 是否启用历史记录的映射表，同时作为events注册表

        # 事件注册（同时作为注册样例）
        for event in EventType:
            self.register_event(event, enable_history_result=False)

    def register_event(self, event_type: EventType | str, enable_history_result: bool = False):
        if event_type in self._events_subscribers:
            raise ValueError(f"事件类型 {event_type} 已存在")
        self._events_subscribers[event_type] = []
        self._history_enable_map[event_type] = enable_history_result
        if enable_history_result:
            self._events_result_history[event_type] = []

    def register_event_subscriber(self, handler_info: EventHandlerInfo, handler_class: Type[BaseEventHandler]) -> bool:
        """注册事件处理器

        Args:
            handler_info (EventHandlerInfo): 事件处理器信息
            handler_class (Type[BaseEventHandler]): 事件处理器类

        Returns:
            bool: 是否注册成功
        """
        if not issubclass(handler_class, BaseEventHandler):
            logger.error(f"类 {handler_class.__name__} 不是 BaseEventHandler 的子类")
            return False

        handler_name = handler_info.name

        if handler_name in self._closing_handlers:
            logger.warning(f"事件处理器 {handler_name} 仍在退出，拒绝重新注册")
            return False
        if handler_name in self._handler_mapping:
            logger.warning(f"事件处理器 {handler_name} 已存在，跳过注册")
            return False

        if handler_info.event_type not in self._history_enable_map:
            logger.error(f"事件类型 {handler_info.event_type} 未注册，无法为其注册处理器 {handler_name}")
            return False

        if not self._insert_event_handler(handler_class, handler_info):
            return False
        handler_instance = self._find_event_handler_instance(handler_class, handler_name)
        if handler_instance is None:
            logger.error(f"事件处理器 {handler_name} 注册后未找到实例")
            self.rollback_event_subscriber_registration(handler_name)
            return False
        self._handler_mapping[handler_name] = handler_class
        self._handler_instances[handler_name] = handler_instance
        self._handler_generation_counter += 1
        self._handler_generations[handler_name] = self._handler_generation_counter
        return True

    async def handle_nacho_events(
        self,
        event_type: EventType,
        message: Optional[MessageRecv | MessageSending] = None,
        llm_prompt: Optional[str] = None,
        llm_response: Optional["LLMGenerationDataModel"] = None,
        stream_id: Optional[str] = None,
        action_usage: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[NachoMessages]]:
        """
        处理所有事件，根据事件类型分发给订阅的处理器。
        """
        from src.plugin_system.core import component_registry

        continue_flag = True

        # 1. 没有订阅者时不要触碰聊天流。Focus 事件轮次可能尚未创建消息上下文。
        handlers = tuple(
            (handler, self._handler_generations.get(handler.handler_name))
            for handler in self._events_subscribers.get(event_type, ())
            if (
                handler.handler_name not in self._closing_handlers
                and self._handler_instances.get(handler.handler_name) is handler
            )
        )
        if not handlers:
            return True, None

        await self._after_handler_snapshot()

        # 2. 准备消息
        transformed_message = self._prepare_message(
            event_type, message, llm_prompt, llm_response, stream_id, action_usage
        )
        if transformed_message:
            transformed_message = transformed_message.deepcopy()

        current_stream_id = transformed_message.stream_id if transformed_message else None
        modified_message: Optional[NachoMessages] = None
        for handler, generation in handlers:
            if not self._handler_is_current(handler, generation):
                continue
            # 3. 前置检查和配置加载
            if (
                current_stream_id
                and handler.handler_name
                in global_announcement_manager.get_disabled_chat_event_handlers(current_stream_id)
            ):
                continue

            # 统一加载插件配置
            plugin_config = component_registry.get_plugin_config(handler.plugin_name) or {}
            handler.set_plugin_config(plugin_config)

            if not self._handler_is_current(handler, generation):
                continue

            # 4. 根据类型分发任务
            if (
                handler.intercept_message or event_type == EventType.ON_STOP
            ):  # 让ON_STOP的所有事件处理器都发挥作用，防止还没执行即被取消
                # 阻塞执行，并更新 continue_flag
                should_continue, modified_message = await self._dispatch_intercepting_handler_task(
                    handler, event_type, modified_message or transformed_message, generation
                )
                continue_flag = continue_flag and should_continue
            else:
                # 异步执行，不阻塞
                self._dispatch_handler_task(handler, event_type, transformed_message, generation)

        return continue_flag, modified_message

    async def cancel_handler_tasks(self, handler_name: str) -> bool:
        tasks_to_be_cancelled = tuple(self._handler_tasks.get(handler_name, ()))
        if remaining_tasks := [task for task in tasks_to_be_cancelled if not task.done()]:
            for task in remaining_tasks:
                task.cancel()
            _, pending = await asyncio.wait(
                remaining_tasks,
                timeout=self._handler_cancel_timeout,
            )
            if pending:
                logger.warning(
                    f"取消事件处理器 {handler_name} 的 {len(pending)} 个任务超时；"
                    "保留跟踪并拒绝卸载"
                )
                return False
            try:
                logger.info(f"已取消事件处理器 {handler_name} 的所有任务")
            except Exception as e:
                logger.error(f"取消事件处理器 {handler_name} 的任务时发生异常: {e}")
                return False
        tasks = self._handler_tasks.get(handler_name)
        if tasks is not None:
            tasks.difference_update(task for task in tasks if task.done())
            if not tasks:
                self._handler_tasks.pop(handler_name, None)
        return True

    def has_event_subscriber(self, handler_name: str) -> bool:
        """返回事件处理器是否已注册。"""
        return handler_name in self._handler_mapping

    def rollback_event_subscriber_registration(self, handler_name: str) -> None:
        """回滚尚未投入运行的处理器注册。"""
        self._closing_handlers.discard(handler_name)
        handler_class = self._handler_mapping.pop(handler_name, None)
        handler_instance = self._handler_instances.pop(handler_name, None)
        self._handler_generations.pop(handler_name, None)
        if handler_class is not None:
            self._remove_event_handler_instance(handler_class, handler_instance)
            return

        # A type-specific registration can fail after inserting the instance
        # but before publishing ``_handler_mapping`` (for example an
        # arbitrary RuntimeError from a registry hook).  Find and remove that
        # unpublished instance as well, otherwise a failed plugin load leaves
        # an event handler that is invisible to every registry index.
        for handlers in self._events_subscribers.values():
            for index, handler in enumerate(handlers):
                if getattr(handler, "handler_name", None) == handler_name:
                    del handlers[index]
                    return

    async def unregister_event_subscriber(self, handler_name: str) -> bool:
        """取消注册事件处理器"""
        if handler_name not in self._handler_mapping:
            # 取消注册是幂等操作；同时清理可能残留的任务。
            if not await self.cancel_handler_tasks(handler_name):
                return False
            self._closing_handlers.discard(handler_name)
            logger.debug(f"事件处理器 {handler_name} 未注册，无需取消")
            return True

        self._closing_handlers.add(handler_name)
        handler_class = self._handler_mapping[handler_name]
        handler_instance = self._handler_instances.get(handler_name)
        # Drain before mutating the event list or registries.  A
        # cancellation-resistant task must not leave an otherwise healthy
        # handler invisible after a bounded unload attempt times out.
        if not await self.cancel_handler_tasks(handler_name):
            self._closing_handlers.discard(handler_name)
            return False
        removed = self._remove_event_handler_instance(handler_class, handler_instance)
        self._handler_mapping.pop(handler_name, None)
        self._handler_instances.pop(handler_name, None)
        self._handler_generations.pop(handler_name, None)
        self._closing_handlers.discard(handler_name)
        if not removed:
            logger.warning(f"事件处理器 {handler_name} 的实例已不存在，已清理注册映射")

        logger.info(f"事件处理器 {handler_name} 已成功取消注册")
        return True

    async def get_event_result_history(self, event_type: EventType | str) -> List[CustomEventHandlerResult]:
        """获取事件的结果历史记录"""
        if event_type == EventType.UNKNOWN:
            raise ValueError("未知事件类型")
        if event_type not in self._history_enable_map:
            raise ValueError(f"事件类型 {event_type} 未注册")
        if not self._history_enable_map[event_type]:
            raise ValueError(f"事件类型 {event_type} 的历史记录未启用")

        return self._events_result_history[event_type]

    async def clear_event_result_history(self, event_type: EventType | str) -> None:
        """清空事件的结果历史记录"""
        if event_type == EventType.UNKNOWN:
            raise ValueError("未知事件类型")
        if event_type not in self._history_enable_map:
            raise ValueError(f"事件类型 {event_type} 未注册")
        if not self._history_enable_map[event_type]:
            raise ValueError(f"事件类型 {event_type} 的历史记录未启用")

        self._events_result_history[event_type] = []

    def _insert_event_handler(self, handler_class: Type[BaseEventHandler], handler_info: EventHandlerInfo) -> bool:
        """插入事件处理器到对应的事件类型列表中并设置其插件配置"""
        if handler_class.event_type == EventType.UNKNOWN:
            logger.error(f"事件处理器 {handler_class.__name__} 的事件类型未知，无法注册")
            return False
        if handler_class.event_type not in self._events_subscribers:
            self._events_subscribers[handler_class.event_type] = []
        handler_instance = handler_class()
        handler_instance.set_plugin_name(handler_info.plugin_name or "unknown")
        self._events_subscribers[handler_class.event_type].append(handler_instance)
        self._events_subscribers[handler_class.event_type].sort(key=lambda x: x.weight, reverse=True)

        return True

    def _find_event_handler_instance(
        self,
        handler_class: Type[BaseEventHandler],
        handler_name: str,
    ) -> Optional[BaseEventHandler]:
        handlers = self._events_subscribers.get(handler_class.event_type, [])
        for handler in handlers:
            if isinstance(handler, handler_class) and handler.handler_name == handler_name:
                return handler
        return None

    def _remove_event_handler_instance(
        self,
        handler_class: Type[BaseEventHandler],
        handler_instance: Optional[BaseEventHandler] = None,
    ) -> bool:
        """从事件类型列表中移除事件处理器"""
        display_handler_name = handler_class.handler_name or handler_class.__name__
        if handler_class.event_type == EventType.UNKNOWN:
            logger.warning(f"事件处理器 {display_handler_name} 的事件类型未知，不存在于处理器列表中")
            return False

        handlers = self._events_subscribers.get(handler_class.event_type, [])
        for i, handler in enumerate(handlers):
            if handler_instance is not None and handler is handler_instance:
                del handlers[i]
                logger.debug(f"事件处理器 {display_handler_name} 已移除")
                return True
            if handler_instance is None and isinstance(handler, handler_class):
                del handlers[i]
                logger.debug(f"事件处理器 {display_handler_name} 已移除")
                return True

        logger.warning(f"未找到事件处理器 {display_handler_name}，无法移除")
        return False

    def _transform_event_message(
        self,
        message: MessageRecv | MessageSending,
        llm_prompt: Optional[str] = None,
        llm_response: Optional["LLMGenerationDataModel"] = None,
    ) -> NachoMessages:
        """转换事件消息格式"""
        # 直接赋值部分内容
        transformed_message = NachoMessages(
            llm_prompt=llm_prompt,
            llm_response_content=llm_response.content if llm_response else None,
            llm_response_reasoning=llm_response.reasoning if llm_response else None,
            llm_response_model=llm_response.model if llm_response else None,
            llm_response_tool_call=llm_response.tool_calls if llm_response else None,
            raw_message=message.raw_message,
            additional_data=message.message_info.additional_config or {},
        )

        # 消息段处理
        if message.message_segment.type == "seglist":
            transformed_message.message_segments = list(message.message_segment.data)  # type: ignore
        else:
            transformed_message.message_segments = [message.message_segment]

        # stream_id 处理
        if hasattr(message, "chat_stream") and message.chat_stream:
            transformed_message.stream_id = message.chat_stream.stream_id

        # 处理后文本
        transformed_message.plain_text = message.processed_plain_text

        # 基本信息
        if hasattr(message, "message_info") and message.message_info:
            if message.message_info.platform:
                transformed_message.message_base_info["platform"] = message.message_info.platform
            if getattr(message.message_info, "message_id", None) is not None:
                transformed_message.message_base_info["message_id"] = message.message_info.message_id
            if message.message_info.group_info:
                transformed_message.is_group_message = True
                transformed_message.message_base_info.update(
                    {
                        "group_id": message.message_info.group_info.group_id,
                        "group_name": message.message_info.group_info.group_name,
                    }
                )
            if message.message_info.user_info:
                if not transformed_message.is_group_message:
                    transformed_message.is_private_message = True
                transformed_message.message_base_info.update(
                    {
                        "user_id": message.message_info.user_info.user_id,
                        "user_cardname": message.message_info.user_info.user_cardname,  # 用户群昵称
                        "user_nickname": message.message_info.user_info.user_nickname,  # 用户昵称（用户名）
                    }
                )

        return transformed_message

    def _build_message_from_stream(
        self, stream_id: str, llm_prompt: Optional[str] = None, llm_response: Optional["LLMGenerationDataModel"] = None
    ) -> NachoMessages:
        """从流ID构建消息"""
        chat_stream = get_chat_manager().get_stream(stream_id)
        assert chat_stream, f"未找到流ID为 {stream_id} 的聊天流"
        context = getattr(chat_stream, "context", None)
        message = context.get_last_message() if context is not None else None
        if message is None:
            logger.debug(
                f"流 {stream_id} 尚无消息上下文，使用无原始消息的事件对象"
            )
            return self._transform_event_without_message(stream_id, llm_prompt, llm_response)
        return self._transform_event_message(message, llm_prompt, llm_response)

    def _transform_event_without_message(
        self,
        stream_id: str,
        llm_prompt: Optional[str] = None,
        llm_response: Optional["LLMGenerationDataModel"] = None,
        action_usage: Optional[List[str]] = None,
    ) -> NachoMessages:
        """没有message对象时进行转换"""
        chat_stream = get_chat_manager().get_stream(stream_id)
        assert chat_stream, f"未找到流ID为 {stream_id} 的聊天流"
        return NachoMessages(
            stream_id=stream_id,
            llm_prompt=llm_prompt,
            llm_response_content=(llm_response.content if llm_response else None),
            llm_response_reasoning=(llm_response.reasoning if llm_response else None),
            llm_response_model=(llm_response.model if llm_response else None),
            llm_response_tool_call=(llm_response.tool_calls if llm_response else None),
            is_group_message=(not (not chat_stream.group_info)),
            is_private_message=(not chat_stream.group_info),
            action_usage=action_usage,
            additional_data={"response_is_processed": True},
        )

    def _prepare_message(
        self,
        event_type: EventType,
        message: Optional[MessageRecv | MessageSending] = None,
        llm_prompt: Optional[str] = None,
        llm_response: Optional["LLMGenerationDataModel"] = None,
        stream_id: Optional[str] = None,
        action_usage: Optional[List[str]] = None,
    ) -> Optional[NachoMessages]:
        """根据事件类型和输入，准备和转换消息对象。"""
        if message:
            return self._transform_event_message(message, llm_prompt, llm_response)

        if event_type not in [EventType.ON_START, EventType.ON_STOP]:
            assert stream_id, "如果没有消息，必须为非启动/关闭事件提供流ID"
            if event_type in [EventType.ON_MESSAGE, EventType.ON_PLAN, EventType.POST_LLM, EventType.AFTER_LLM]:
                return self._build_message_from_stream(stream_id, llm_prompt, llm_response)
            else:
                return self._transform_event_without_message(stream_id, llm_prompt, llm_response, action_usage)

        return None  # ON_START, ON_STOP事件没有消息体

    def _dispatch_handler_task(
        self,
        handler: BaseEventHandler,
        event_type: EventType | str,
        message: Optional[NachoMessages] = None,
        generation: Optional[int] = None,
    ):
        """分发一个非阻塞（异步）的事件处理任务。"""
        if event_type == EventType.UNKNOWN:
            raise ValueError("未知事件类型")
        if generation is None:
            generation = self._handler_generations.get(handler.handler_name)
        if not self._handler_is_current(handler, generation):
            return
        try:
            task = asyncio.create_task(handler.execute(message))

            task_name = f"{handler.plugin_name}-{handler.handler_name}"
            task.set_name(task_name)
            task.add_done_callback(
                lambda t, handler_name=handler.handler_name: self._task_done_callback(t, event_type, handler_name)
            )

            self._handler_tasks.setdefault(handler.handler_name, set()).add(task)
        except Exception as e:
            logger.error(f"创建事件处理器任务 {handler.handler_name} 时发生异常: {e}", exc_info=True)

    async def _dispatch_intercepting_handler_task(
        self,
        handler: BaseEventHandler,
        event_type: EventType | str,
        message: Optional[NachoMessages] = None,
        generation: Optional[int] = None,
    ) -> Tuple[bool, Optional[NachoMessages]]:
        """分发并等待一个阻塞（同步）的事件处理器，返回是否应继续处理。"""
        if event_type == EventType.UNKNOWN:
            raise ValueError("未知事件类型")
        if event_type not in self._history_enable_map:
            raise ValueError(f"事件类型 {event_type} 未注册")
        if generation is None:
            generation = self._handler_generations.get(handler.handler_name)
        if not self._handler_is_current(handler, generation):
            return True, None
        try:
            task = asyncio.create_task(handler.execute(message))
            task.set_name(f"{handler.plugin_name}-{handler.handler_name}-intercept")
            self._handler_tasks.setdefault(handler.handler_name, set()).add(task)
            try:
                success, continue_processing, return_message, custom_result, modified_message = await task
            finally:
                tasks = self._handler_tasks.get(handler.handler_name)
                if tasks is not None:
                    tasks.discard(task)
                    if not tasks:
                        self._handler_tasks.pop(handler.handler_name, None)

            if not success:
                logger.error(f"EventHandler {handler.handler_name} 执行失败: {return_message}")
            else:
                logger.debug(f"EventHandler {handler.handler_name} 执行成功: {return_message}")

            if self._history_enable_map[event_type] and custom_result:
                self._events_result_history[event_type].append(custom_result)
            return continue_processing, modified_message
        except KeyError:
            logger.error(f"事件 {event_type} 注册的历史记录启用情况与实际不符合")
            return True, None
        except Exception as e:
            logger.error(f"EventHandler {handler.handler_name} 发生异常: {e}", exc_info=True)
            return True, None  # 发生异常时默认不中断其他处理

    def _handler_is_current(self, handler: BaseEventHandler, generation: Optional[int]) -> bool:
        """Check identity, generation, and admission before creating work."""
        handler_name = handler.handler_name
        if handler_name in self._closing_handlers:
            return False
        current_instance = self._handler_instances.get(handler_name)
        if current_instance is None:
            return False
        return current_instance is handler and self._handler_generations.get(handler_name) == generation

    async def _after_handler_snapshot(self) -> None:
        """Scheduling seam used by isolated lifecycle tests; production is a no-op."""
        return None

    def _task_done_callback(
        self,
        task: asyncio.Task[Tuple[bool, bool, str | None, CustomEventHandlerResult | None, NachoMessages | None]],
        event_type: EventType | str,
        handler_name: str,
    ):
        """任务完成回调"""
        task_name = task.get_name() or "Unknown Task"
        if event_type == EventType.UNKNOWN:
            raise ValueError("未知事件类型")
        if event_type not in self._history_enable_map:
            raise ValueError(f"事件类型 {event_type} 未注册")
        try:
            success, _, result, custom_result, _ = task.result()  # 忽略是否继续的标志和消息的修改，因为消息本身未被拦截
            if success:
                logger.debug(f"事件处理任务 {task_name} 已成功完成: {result}")
            else:
                logger.error(f"事件处理任务 {task_name} 执行失败: {result}")

            if self._history_enable_map[event_type] and custom_result:
                self._events_result_history[event_type].append(custom_result)
        except asyncio.CancelledError:
            pass
        except KeyError:
            logger.error(f"事件 {event_type} 注册的历史记录启用情况与实际不符合")
        except Exception as e:
            logger.error(f"事件处理任务 {task_name} 发生异常: {e}")
        finally:
            tasks = self._handler_tasks.get(handler_name)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    self._handler_tasks.pop(handler_name, None)


events_manager = EventsManager()
