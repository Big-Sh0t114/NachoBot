import time
from typing import List, Dict, Tuple, Optional, Any
from src.plugin_system.apis.tool_api import get_llm_available_tool_definitions, get_tool_instance
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.core.global_announcement_manager import global_announcement_manager
from src.llm_models.utils_model import LLMRequest
from src.llm_models.payload_content import ToolCall
from src.config.config import global_config, model_config
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.logger import get_logger

logger = get_logger("tool_use")


def init_tool_executor_prompt():
    """初始化工具执行器的提示词"""
    tool_executor_prompt = """
你是一个专门执行工具的助手。你的名字是{bot_name}。现在是{time_now}。
群里正在进行的聊天内容：
{chat_history}

现在，{sender}发送了内容:{target_message},你想要回复ta。

**重要**：仅在**确有必要**时才使用工具。请仔细判断：
1. 用户的请求是否必须通过工具才能完成？（如必应搜索、知识库检索）
2. 如果可以通过常识直接回答，或者只是简单的闲聊/情感交流，请**不要调用工具**。
3. 频繁调用工具会严重拖慢回复速度，请务必克制。
4. **严禁使用 write_file 来回复聊天消息！** write_file 仅用于用户**明确要求生成、创建或编写文件**的场景。普通的聊天回复、情感互动、日常对话**绝对不能**调用 write_file。

If you need to use a tool, please directly call the corresponding tool function. If you do not need to use any tool, simply output "No tool needed".
"""
    prompt = Prompt(tool_executor_prompt, "tool_executor_prompt", _should_register=False)
    global_prompt_manager.register(prompt)

    mcp_tool_executor_prompt = """
你是一个专门执行工具的助手。你的名字是{bot_name}。现在是{time_now}。
群里正在进行的聊天内容：
{chat_history}

现在，{sender}发送了内容:{target_message},你想要回复ta。

**任务指示**：
1. 你拥有通过 MCP 连接的扩展工具（通常是浏览器、文件操作、API调用等）。
2. 用户可能在寻找娱乐、需要执行具体操作，或者仅仅是想玩。
3. 请尝试判断用户的意图，如果工具能带来**实际帮助**或**娱乐价值**（如截屏、搜索、文件操作），请**大胆调用**。
4. **如果用户只是进行简单的日常闲聊（如打招呼、表达情绪），且没有任何工具有助于增强回复体验，请输出 "No tool needed"。**
5. 不要强行调用不相关的工具。

**浏览器使用技巧 (Puppeteer)**：
- **搜索/填表**：通常需要组合使用 `navigate` -> `puppeteer_fill` (输入框) -> `puppeteer_click` (搜索按钮) 或 `puppeteer_evaluate` (提交表单)。
- **Bilibili/百度等搜索**：
  - 导航: `https://www.bilibili.com`
  - 搜索框通常是 `.nav-search-input` 或 `input[type="text"]`。
  - 搜索按钮通常是 `.nav-search-btn` 或可尝试模拟回车。
- 如果不确定选择器，可以先 `navigate` 然后 `screenshot` 或 `evaluate` ("document.body.innerHTML") 来分析页面。

Let's try to use the tools provided!
If you need to use a tool, please directly call the corresponding tool function. If you do not need to use any tool, simply output "No tool needed".
"""
    mcp_prompt = Prompt(mcp_tool_executor_prompt, "mcp_tool_executor_prompt", _should_register=False)
    global_prompt_manager.register(mcp_prompt)


# 初始化提示词
init_tool_executor_prompt()


class ToolExecutor:
    """独立的工具执行器组件

    可以直接输入聊天消息内容，自动判断并执行相应的工具，返回结构化的工具执行结果。
    """

    def __init__(
        self,
        chat_id: str,
        enable_cache: bool = True,
        cache_ttl: int = 3,
        model_set: Optional[Any] = None,
        include_prefix: Optional[str] = None,
        exclude_prefix: Optional[str] = None,
        prompt_template: str = "tool_executor_prompt",
    ):
        """初始化工具执行器

        Args:
            executor_id: 执行器标识符，用于日志记录
            enable_cache: 是否启用缓存机制
            cache_ttl: 缓存生存时间（周期数）
            model_set: 自定义模型配置（可选）
            include_prefix: 仅包含此前缀的工具（可选）
            exclude_prefix: 排除此前缀的工具（可选）
        """
        self.chat_id = chat_id
        self.chat_stream = get_chat_manager().get_stream(self.chat_id)
        self.log_prefix = f"[{get_chat_manager().get_stream_name(self.chat_id) or self.chat_id}]"
        self.include_prefix = include_prefix
        self.exclude_prefix = exclude_prefix
        self.prompt_template = prompt_template

        target_model_set = model_set or model_config.model_task_config.tool_use
        self.llm_model = LLMRequest(model_set=target_model_set, request_type="tool_executor")

        # 缓存配置
        self.enable_cache = enable_cache
        self.cache_ttl = cache_ttl
        self.tool_cache = {}  # 格式: {cache_key: {"result": result, "ttl": ttl, "timestamp": timestamp}}

        logger.info(
            f"{self.log_prefix}工具执行器初始化完成，模式={'启用' if enable_cache else '禁用'}，过滤器=[+{include_prefix or 'All'}, -{exclude_prefix or 'None'}]"
        )

    async def execute_from_chat_message(
        self, target_message: str, chat_history: str, sender: str, return_details: bool = False
    ) -> Tuple[List[Dict[str, Any]], List[str], str]:
        """从聊天消息执行工具

        Args:
            target_message: 目标消息内容
            chat_history: 聊天历史
            sender: 发送者
            return_details: 是否返回详细信息(使用的工具列表和提示词)

        Returns:
            如果return_details为False: Tuple[List[Dict], List[str], str] - (工具执行结果列表, 空, 空)
            如果return_details为True: Tuple[List[Dict], List[str], str] - (结果列表, 使用的工具, 提示词)
        """

        # 首先检查缓存
        cache_key = self._generate_cache_key(target_message, chat_history, sender)
        if cached_result := self._get_from_cache(cache_key):
            logger.info(f"{self.log_prefix}使用缓存结果，跳过工具执行")
            if not return_details:
                return cached_result, [], ""

            # 从缓存结果中提取工具名称
            used_tools = [result.get("tool_name", "unknown") for result in cached_result]
            return cached_result, used_tools, ""

        # 缓存未命中，执行工具调用
        # 获取可用工具
        tools = self._get_tool_definitions()

        # print(f"tools: {tools}")

        # 获取当前时间
        time_now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        bot_name = global_config.bot.nickname

        # 构建工具调用提示词
        prompt = await global_prompt_manager.format_prompt(
            self.prompt_template,
            target_message=target_message,
            chat_history=chat_history,
            sender=sender,
            bot_name=bot_name,
            time_now=time_now,
        )

        logger.debug(f"{self.log_prefix}开始LLM工具调用分析")

        # 调用LLM进行工具决策
        model_list = self.llm_model.model_for_task.model_list
        logger.info(f"{self.log_prefix} 工具执行器正在使用模型: {model_list}")

        response, (reasoning_content, model_name, tool_calls) = await self.llm_model.generate_response_async(
            prompt=prompt, tools=tools, raise_when_empty=False
        )

        # 动态模型切换逻辑：如果普通模型决定调用文件相关工具，强制切换为 file_edit 模型重新生成
        if tool_calls and self.llm_model.request_type != "file_edit":
            file_tool_names = {
                "read_file",
                "write_file",
                "list_files",
                "execute_python_code",
                "search_file",
                "replace_file_content",
            }
            if any(call.func_name in file_tool_names for call in tool_calls):
                # 沙盒白名单检查：非白名单用户不允许触发任何文件工具操作
                # 注意: chat_stream.user_info 在群聊中可能是流创建者而非当前发送者
                # 必须从 context.message.message_info 获取真实发送者
                user_id = ""
                if self.chat_stream and self.chat_stream.context:
                    msg_info = self.chat_stream.context.message.message_info
                    if msg_info.sender_info and msg_info.sender_info.user_id:
                        user_id = str(msg_info.sender_info.user_id)
                    elif msg_info.user_info and msg_info.user_info.user_id:
                        user_id = str(msg_info.user_info.user_id)
                if not user_id and self.chat_stream and self.chat_stream.user_info:
                    user_id = str(self.chat_stream.user_info.user_id)
                is_admin = user_id in global_config.advanced.admins
                is_whitelisted = user_id in global_config.bot.sandbox_whitelist
                if not (is_admin or is_whitelisted):
                    logger.warning(f"{self.log_prefix} 用户 {user_id} 不在沙盒白名单中，拒绝执行文件工具操作！")
                    # 不返回错误信息给LLM，直接返回空结果，当成普通消息处理
                    return [], [], ""
                logger.info(
                    f"{self.log_prefix} 检测到文件操作意图(如 {tool_calls[0].func_name})，强制切换至 file_edit 模型重新生成以保证代码质量！"
                )

                original_tool_calls = tool_calls
                file_edit_model_set = getattr(
                    model_config.model_task_config, "file_edit", self.llm_model.model_for_task
                )
                file_edit_llm = LLMRequest(model_set=file_edit_model_set, request_type="file_edit")

                bot_info = global_config.personality
                rule_addition = (
                    f"\n\n你的人设是：\n{bot_info.personality}\n"
                    f"【核心编码规则】当涉及编写代码、调试或专业功能说明时，你必须进入“认真模式”。\n"
                    f"代码的底层算法、语法结构和逻辑必须100%严谨规范，不可带有任何“笨笨的”或无条理的特征。\n"
                    f"但是，你必须将你的人设无缝融入代码的“观感层”：请使用可爱的风格来命名变量/函数（在符合命名规范的前提下），并使用慵懒傲娇的语气和颜文字来编写代码注释。\n"
                    f"代码文件中不要刻意提及以上内容。"
                )

                file_edit_prompt = (
                    prompt
                    + rule_addition
                    + "\n\n【系统强制指令】：你必须且只能使用提供的文件工具（如 write_file）输出最终内容。绝对不要仅在回复中输出纯文本代码片段！"
                )
                
                # Fetch recent reads context from sandbox and inject it
                from src.chat.sandbox.sandbox_manager import sandbox_manager
                if hasattr(self, "chat_stream") and self.chat_stream:
                    sandbox = sandbox_manager.get_sandbox(self.chat_stream.stream_id)
                    recent_context = sandbox.get_recent_reads_context()
                    if recent_context:
                        file_edit_prompt += f"\n\n{recent_context}"
                        logger.info(f"{self.log_prefix} 已成功附加最近读取的文件上下文。")
                        
                logger.info(f"{self.log_prefix} 工具执行器已动态切换模型: {file_edit_model_set.model_list}")
                logger.info(f"{self.log_prefix} ------------- file_edit 模型原始 Prompt 开始 -------------")
                logger.info(f"\n{file_edit_prompt}")
                logger.info(f"{self.log_prefix} ------------- file_edit 模型原始 Prompt 结束 -------------")
                response, (reasoning_content, model_name, new_tool_calls) = await file_edit_llm.generate_response_async(
                    prompt=file_edit_prompt, tools=tools, raise_when_empty=False
                )

                if new_tool_calls:
                    tool_calls = new_tool_calls
                else:
                    logger.warning(
                        f"{self.log_prefix} file_edit 模型未正确调用工具，尝试从 response 中提取代码块并强行注入原工具调用！"
                    )
                    import re

                    code_match = re.search(r"```(?:\w+)?\n(.*?)```", response, re.DOTALL)
                    extracted_content = code_match.group(1).strip() if code_match else response.strip()

                    # 强行将内容塞回给原本意图（gpt-4o-mini 分析出的 tool_call）
                    for call in original_tool_calls:
                        if call.func_name in ["write_file", "replace_file_content"] and isinstance(call.args, dict):
                            if "content" in call.args:
                                call.args["content"] = extracted_content
                            elif "replacementContent" in call.args:
                                call.args["replacementContent"] = extracted_content
                            logger.info(f"{self.log_prefix} 已成功将提取出的代码注入到 {call.func_name} 工具链中。")

                    tool_calls = original_tool_calls

        # 执行工具调用
        tool_results, used_tools = await self.execute_tool_calls(tool_calls)

        # 缓存结果
        if tool_results:
            self._set_cache(cache_key, tool_results)

        if used_tools:
            logger.info(f"{self.log_prefix}工具执行完成，共执行{len(used_tools)}个工具: {used_tools}")

        if return_details:
            return tool_results, used_tools, prompt
        else:
            return tool_results, [], ""

    def _get_tool_definitions(self) -> List[Dict[str, Any]]:
        all_tools = get_llm_available_tool_definitions()
        # Debug: Print all available tools
        logger.debug(f"{self.log_prefix} DEBUG: ALL Available Tools: {[name for name, _ in all_tools]}")

        user_disabled_tools = global_announcement_manager.get_disabled_chat_tools(self.chat_id)

        filtered_tools = []
        for name, definition in all_tools:
            if name in user_disabled_tools:
                continue

            # 过滤器逻辑
            if self.include_prefix and not name.startswith(self.include_prefix):
                continue
            if self.exclude_prefix and name.startswith(self.exclude_prefix):
                continue

            filtered_tools.append(definition)

        if self.include_prefix or self.exclude_prefix:
            logger.info(
                f"{self.log_prefix} 工具Filter: 总数={len(all_tools)}, 剩余={len(filtered_tools)}, Include={self.include_prefix}, Exclude={self.exclude_prefix}"
            )
            if self.include_prefix and filtered_tools:
                logger.info(f"{self.log_prefix} MCP工具可见: {[t['name'] for t in filtered_tools]}")
            if not filtered_tools and self.include_prefix:
                logger.warning(f"{self.log_prefix} MCP工具列表为空! 请检查是否已连接服务器或权限设置。")

        return filtered_tools

    async def execute_tool_calls(self, tool_calls: Optional[List[ToolCall]]) -> Tuple[List[Dict[str, Any]], List[str]]:
        """执行工具调用

        Args:
            tool_calls: LLM返回的工具调用列表

        Returns:
            Tuple[List[Dict], List[str]]: (工具执行结果列表, 使用的工具名称列表)
        """
        tool_results: List[Dict[str, Any]] = []
        used_tools = []

        if not tool_calls:
            logger.debug(f"{self.log_prefix}无需执行工具")
            return [], []

        # 提取tool_calls中的函数名称
        func_names = [call.func_name for call in tool_calls if call.func_name]

        logger.info(f"{self.log_prefix}开始执行工具调用: {func_names}")

        # 执行每个工具调用
        for tool_call in tool_calls:
            try:
                tool_name = tool_call.func_name
                logger.debug(f"{self.log_prefix}执行工具: {tool_name}")

                # 执行工具
                result = await self.execute_tool_call(tool_call)

                if result:
                    tool_info = {
                        "type": result.get("type", "unknown_type"),
                        "id": result.get("id", f"tool_exec_{time.time()}"),
                        "content": result.get("content", ""),
                        "tool_name": tool_name,
                        "timestamp": time.time(),
                    }
                    content = tool_info["content"]
                    if not isinstance(content, (str, list, tuple)):
                        tool_info["content"] = str(content)

                    tool_results.append(tool_info)
                    used_tools.append(tool_name)
                    logger.info(f"{self.log_prefix}工具{tool_name}执行成功，类型: {tool_info['type']}")
                    preview = content[:200]
                    logger.debug(f"{self.log_prefix}工具{tool_name}结果内容: {preview}...")
            except Exception as e:
                logger.error(f"{self.log_prefix}工具{tool_name}执行失败: {e}")
                # 添加错误信息到结果中
                error_info = {
                    "type": "tool_error",
                    "id": f"tool_error_{time.time()}",
                    "content": f"工具{tool_name}执行失败: {str(e)}",
                    "tool_name": tool_name,
                    "timestamp": time.time(),
                }
                tool_results.append(error_info)

        return tool_results, used_tools

    async def execute_tool_call(
        self, tool_call: ToolCall, tool_instance: Optional[BaseTool] = None
    ) -> Optional[Dict[str, Any]]:
        # sourcery skip: use-assigned-variable
        """执行单个工具调用

        Args:
            tool_call: 工具调用对象

        Returns:
            Optional[Dict]: 工具调用结果，如果失败则返回None
        """
        try:
            function_name = tool_call.func_name
            function_args = tool_call.args or {}
            function_args["llm_called"] = True  # 标记为LLM调用

            # 获取对应工具实例
            tool_instance = tool_instance or get_tool_instance(function_name)
            if not tool_instance:
                logger.warning(f"未知工具名称: {function_name}")
                return None

            # 2. 安全检查：验证工具是否允许在此执行器中运行 (Fix Permission Leak)
            if self.include_prefix and not function_name.startswith(self.include_prefix):
                logger.warning(
                    f"{self.log_prefix} 拒绝执行非包含前缀工具: {function_name} (Filter: +{self.include_prefix})"
                )
                return None

            if self.exclude_prefix and function_name.startswith(self.exclude_prefix):
                logger.warning(
                    f"{self.log_prefix} 拒绝执行排除前缀工具: {function_name} (Filter: -{self.exclude_prefix})"
                )
                return None

            # 注入 chat_stream (如果工具支持)
            try:
                tool_instance.chat_stream = self.chat_stream
            except Exception:
                pass  # 忽略错误

            # 执行工具
            result = await tool_instance.execute(function_args)
            if result:
                content_val = result.get("content", result.get("error", str(result)))
                return {
                    "tool_call_id": tool_call.call_id,
                    "role": "tool",
                    "name": function_name,
                    "type": "function",
                    "content": str(content_val),
                }
            return None
        except Exception as e:
            logger.error(f"执行工具调用时发生错误: {str(e)}")
            raise e

    def _generate_cache_key(self, target_message: str, chat_history: str, sender: str) -> str:
        """生成缓存键

        Args:
            target_message: 目标消息内容
            chat_history: 聊天历史
            sender: 发送者

        Returns:
            str: 缓存键
        """
        import hashlib

        # 使用消息内容和群聊状态生成唯一缓存键
        content = f"{target_message}_{chat_history}_{sender}"
        return hashlib.md5(content.encode()).hexdigest()

    def _get_from_cache(self, cache_key: str) -> Optional[List[Dict]]:
        """从缓存获取结果

        Args:
            cache_key: 缓存键

        Returns:
            Optional[List[Dict]]: 缓存的结果，如果不存在或过期则返回None
        """
        if not self.enable_cache or cache_key not in self.tool_cache:
            return None

        cache_item = self.tool_cache[cache_key]
        if cache_item["ttl"] <= 0:
            # 缓存过期，删除
            del self.tool_cache[cache_key]
            logger.debug(f"{self.log_prefix}缓存过期，删除缓存键: {cache_key}")
            return None

        # 减少TTL
        cache_item["ttl"] -= 1
        logger.debug(f"{self.log_prefix}使用缓存结果，剩余TTL: {cache_item['ttl']}")
        return cache_item["result"]

    def _set_cache(self, cache_key: str, result: List[Dict]):
        """设置缓存

        Args:
            cache_key: 缓存键
            result: 要缓存的结果
        """
        if not self.enable_cache:
            return

        self.tool_cache[cache_key] = {"result": result, "ttl": self.cache_ttl, "timestamp": time.time()}
        logger.debug(f"{self.log_prefix}设置缓存，TTL: {self.cache_ttl}")

    def _cleanup_expired_cache(self):
        """清理过期的缓存"""
        if not self.enable_cache:
            return

        expired_keys = []
        expired_keys.extend(cache_key for cache_key, cache_item in self.tool_cache.items() if cache_item["ttl"] <= 0)
        for key in expired_keys:
            del self.tool_cache[key]

        if expired_keys:
            logger.debug(f"{self.log_prefix}清理了{len(expired_keys)}个过期缓存")

    async def execute_specific_tool_simple(self, tool_name: str, tool_args: Dict) -> Optional[Dict]:
        """直接执行指定工具

        Args:
            tool_name: 工具名称
            tool_args: 工具参数
            validate_args: 是否验证参数

        Returns:
            Optional[Dict]: 工具执行结果，失败时返回None
        """
        try:
            tool_call = ToolCall(
                call_id=f"direct_tool_{time.time()}",
                func_name=tool_name,
                args=tool_args,
            )

            logger.info(f"{self.log_prefix}直接执行工具: {tool_name}")

            result = await self.execute_tool_call(tool_call)

            if result:
                tool_info = {
                    "type": result.get("type", "unknown_type"),
                    "id": result.get("id", f"direct_tool_{time.time()}"),
                    "content": result.get("content", ""),
                    "tool_name": tool_name,
                    "timestamp": time.time(),
                }
                logger.info(f"{self.log_prefix}直接工具执行成功: {tool_name}")
                return tool_info

        except Exception as e:
            logger.error(f"{self.log_prefix}直接工具执行失败 {tool_name}: {e}")

        return None

    def clear_cache(self):
        """清空所有缓存"""
        if self.enable_cache:
            cache_count = len(self.tool_cache)
            self.tool_cache.clear()
            logger.info(f"{self.log_prefix}清空了{cache_count}个缓存项")

    def get_cache_status(self) -> Dict:
        """获取缓存状态信息

        Returns:
            Dict: 包含缓存统计信息的字典
        """
        if not self.enable_cache:
            return {"enabled": False, "cache_count": 0}

        # 清理过期缓存
        self._cleanup_expired_cache()

        total_count = len(self.tool_cache)
        ttl_distribution = {}

        for cache_item in self.tool_cache.values():
            ttl = cache_item["ttl"]
            ttl_distribution[ttl] = ttl_distribution.get(ttl, 0) + 1

        return {
            "enabled": True,
            "cache_count": total_count,
            "cache_ttl": self.cache_ttl,
            "ttl_distribution": ttl_distribution,
        }

    def set_cache_config(self, enable_cache: Optional[bool] = None, cache_ttl: int = -1):
        """动态修改缓存配置

        Args:
            enable_cache: 是否启用缓存
            cache_ttl: 缓存TTL
        """
        if enable_cache is not None:
            self.enable_cache = enable_cache
            logger.info(f"{self.log_prefix}缓存状态修改为: {'启用' if enable_cache else '禁用'}")

        if cache_ttl > 0:
            self.cache_ttl = cache_ttl
            logger.info(f"{self.log_prefix}缓存TTL修改为: {cache_ttl}")


"""
ToolExecutor使用示例：

# 1. 基础使用 - 从聊天消息执行工具（启用缓存，默认TTL=3）
executor = ToolExecutor(executor_id="my_executor")
results, _, _ = await executor.execute_from_chat_message(
    talking_message_str="今天天气怎么样？现在几点了？",
    is_group_chat=False
)

# 2. 禁用缓存的执行器
no_cache_executor = ToolExecutor(executor_id="no_cache", enable_cache=False)

# 3. 自定义缓存TTL
long_cache_executor = ToolExecutor(executor_id="long_cache", cache_ttl=10)

# 4. 获取详细信息
results, used_tools, prompt = await executor.execute_from_chat_message(
    talking_message_str="帮我查询Python相关知识",
    is_group_chat=False,
    return_details=True
)

# 5. 直接执行特定工具
result = await executor.execute_specific_tool_simple(
    tool_name="get_knowledge",
    tool_args={"query": "机器学习"}
)

# 6. 缓存管理
cache_status = executor.get_cache_status()  # 查看缓存状态
executor.clear_cache()  # 清空缓存
executor.set_cache_config(cache_ttl=5)  # 动态修改缓存配置
"""
