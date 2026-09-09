import asyncio
from typing import List, Dict, Any, Optional

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.utils.url_fetcher import extract_urls
from src.chat.utils.capability_router import build_search_after_decision, execute_mcp_after_decision
from src.person_info.person_info import Person
from src.mcp.types import MCPAccessContext

logger = get_logger("context_builder")

async def build_tool_info(
    chat_history: str,
    sender: str,
    target: str,
    url_fetcher: Any,
    web_search_manager: Any,
    capability_router: Any,
    tool_executor: Any,
    mcp_executor: Any,
    mcp_access_context: Optional[MCPAccessContext] = None,
    enable_tool: bool = True,
    chat_id: Optional[str] = None,
) -> str:
    """构建工具信息块 (从 PrivateGenerator 移植)

    Args:
        chat_history: 聊天历史记录
        sender: 发送者名称
        target: 目标消息内容
        url_fetcher: UrlContentFetcher 实例
        web_search_manager: WebSearchManager 实例
        capability_router: 共享的联网/MCP 能力路由器
        tool_executor: ToolExecutor 实例
        mcp_executor: MCPExecutor 实例
        mcp_access_context: 核心 MCP 服务用于目录过滤和执行鉴权的上下文
        enable_tool: 是否启用工具调用

    Returns:
        str: 工具信息字符串
    """
    if not enable_tool:
        logger.info("工具信息跳过: enable_tool=False")
        return ""

    try:
        url_info = ""
        urls = extract_urls(target)
        if urls:
            url_preview = ", ".join(urls[:3])
            if len(urls) > 3:
                url_preview += " ..."
            logger.info(f"检测到URL，开始抓取: {url_preview}")
            try:
                url_info = await url_fetcher.build_url_info(urls)
            except Exception as e:
                logger.debug(f"URL解析失败: {e}")

        # === 并行执行：搜索 + 工具判定 ===
        search_info = ""
        search_url_info = ""
        tool_results: List[Dict[str, Any]] = []

        # 构建并行任务列表
        parallel_tasks = {}

        mcp_catalog = mcp_executor.get_tool_catalog_summary(access_context=mcp_access_context)
        allow_web_search = bool(not urls and web_search_manager.is_available)
        allow_mcp = bool(mcp_catalog)
        decision_task = None
        if allow_web_search or allow_mcp:
            decision_task = asyncio.create_task(
                capability_router.decide(
                    chat_history=chat_history,
                    sender=sender,
                    target=target,
                    bot_name=global_config.bot.nickname,
                    allow_web_search=allow_web_search,
                    allow_mcp=allow_mcp,
                    mcp_catalog=mcp_catalog,
                )
            )

        # 1. 搜索任务（仅在无 URL 且能力路由命中时触发）
        if allow_web_search and decision_task:
            logger.info("未检测到URL，尝试联网搜索判定")
            parallel_tasks["search"] = build_search_after_decision(
                decision_task,
                web_search_manager,
                chat_history=chat_history,
                sender=sender,
                target=target,
                bot_name=global_config.bot.nickname,
            )

        # 2. 标准工具 (Standard)
        parallel_tasks["standard_tool"] = tool_executor.execute_from_chat_message(
            sender=sender, target_message=target, chat_history=chat_history, return_details=False
        )

        # 3. MCP 独立工具链 - 权限和能力路由均通过后才执行
        if allow_mcp and decision_task:
            parallel_tasks["mcp_tool"] = execute_mcp_after_decision(
                decision_task,
                mcp_executor,
                sender=sender,
                target=target,
                chat_history=chat_history,
                return_details=False,
                access_context=mcp_access_context,
            )
        elif not mcp_catalog:
            logger.info("当前用户没有获准使用的 MCP 工具，跳过 MCP 能力检查")
        else:
            logger.info("跳过 MCP 能力检查")

        # 并行执行所有任务
        task_keys = list(parallel_tasks.keys())
        task_coros = list(parallel_tasks.values())
        raw_results = await asyncio.gather(*task_coros, return_exceptions=True)
        results_map = dict(zip(task_keys, raw_results, strict=True))

        # 处理搜索结果
        if "search" in results_map:
            search_res = results_map["search"]
            if isinstance(search_res, Exception):
                logger.debug(f"联网搜索信息获取失败: {search_res}")
            elif search_res:
                search_info = search_res
                logger.info("联网搜索已返回结果")
                # 搜索结果 URL 抓取（限制为最多 1 个 URL，HTTP 优先）
                search_urls = []
                seen_urls = set()
                for url in extract_urls(search_info):
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    search_urls.append(url)
                    if len(search_urls) >= 1:
                        break
                if search_urls:
                    logger.info("开始抓取搜索结果正文")
                    try:
                        search_url_info = await url_fetcher.build_url_info(search_urls)
                    except Exception as e:
                        logger.debug(f"搜索结果正文抓取失败: {e}")
            else:
                logger.info("联网搜索未触发或无结果")

        # 处理 Standard 工具结果
        standard_res = results_map.get("standard_tool")
        if standard_res is not None:
            if isinstance(standard_res, Exception):
                logger.error(f"Standard 工具执行器失败: {standard_res}")
            else:
                t_res, _, _ = standard_res
                if t_res:
                    tool_results.extend(t_res)

        # 处理 MCP 工具结果
        mcp_res = results_map.get("mcp_tool")
        if mcp_res is not None:
            if isinstance(mcp_res, Exception):
                logger.error(f"MCP 工具执行器失败: {mcp_res}")
            else:
                t_res, _, _ = mcp_res
                if t_res:
                    tool_results.extend(t_res)

        if tool_results or search_info or url_info:
            tool_info_str = "以下是你获取到的实时信息：\n"
            if url_info:
                tool_info_str += f"【网页内容】\n{url_info}\n"
            if search_info:
                tool_info_str += f"【联网搜索】\n{search_info}\n"
            if search_url_info:
                tool_info_str += f"【搜索结果正文】\n{search_url_info}\n"
            for tool_result in tool_results:
                tool_name = tool_result.get("tool_name", "unknown")
                content = tool_result.get("content", "")
                result_type = tool_result.get("type", "tool_result")

                tool_info_str += f"- 【{tool_name}】{result_type}: {content}\n"

            tool_info_str += "以上是你获取到的实时信息，请在回复时参考这些信息。"
            if tool_results:
                logger.info(f"获取到 {len(tool_results)} 个工具结果")
            if search_info:
                logger.info("获取到联网搜索结果")
            if search_url_info:
                logger.info("获取到搜索结果正文")
            if url_info:
                logger.info("获取到网页解析结果")

            return tool_info_str

        # 即使没有其他工具结果，也检查是否有沙盒文件概述需要注入
        if chat_id:
            try:
                from src.chat.sandbox.sandbox_manager import sandbox_manager
                sandbox = sandbox_manager.get_sandbox(chat_id)
                file_summaries_text = sandbox.get_active_summaries()
                if file_summaries_text:
                    logger.info(f"[context_builder] 已注入沙盒文件概述 (chat_id={chat_id})")
                    sandbox.tick_summaries()
                    return file_summaries_text
                sandbox.tick_summaries()
            except Exception as e:
                logger.debug(f"获取沙盒文件概述失败: {e}")

        logger.debug("未获取到任何工具结果")
        return ""

    except Exception as e:
        logger.error(f"工具信息获取失败: {e}")
        return ""


async def build_relation_info(chat_content: str, sender: str, user_info: Any = None) -> str:
    """构建关系记忆信息 (从 PrivateGenerator 移植)
    
    Args:
        chat_content: 聊天历史内容
        sender: 发送者昵称
        user_info: (可选) 用户信息对象，包含 Platform 和 user_id
    
    Returns:
        str: 关系描述
    """
    if not global_config.relationship.enable_relationship:
        return ""

    if not sender:
        return ""

    if sender == global_config.bot.nickname:
        return ""

    person = None
    if user_info and getattr(user_info, "user_id", None) and getattr(user_info, "platform", None):
        person = Person(platform=user_info.platform, user_id=user_info.user_id)
    else:
        logger.warning("缺少用户信息，无法构建关系记忆，使用昵称降级匹配")

    if (not person or not person.is_known) and sender:
        person = Person(person_name=sender)

    if not person or not person.is_known:
        logger.warning(f"未找到用户 {sender} 的ID，跳过信息提取")
        return f"你完全不认识{sender}，不理解ta的相关信息。"

    sender_relation = await person.build_relationship(chat_content)
    return f"{sender_relation}"

async def build_lpmm_knowledge_info(message: str, sender: str, target: str, tool_executor: Any) -> str:
    """构建 LPMM 知识库信息 (从 PrivateGenerator 移植)

    Args:
        message: 聊天历史内容
        sender: 发送者
        target: 目标消息
        tool_executor: ToolExecutor 实例

    Returns:
        str: 知识库检索结果字符串
    """
    import time
    from src.plugin_system.apis import llm_api
    from src.config.config import model_config
    from src.chat.utils.prompt_builder import global_prompt_manager

    related_info = ""
    start_time = time.time()
    try:
        from src.plugins.built_in.knowledge.lpmm_get_knowledge import SearchKnowledgeFromLPMMTool
    except ImportError:
        logger.debug("未安装或无法导入 LPMM 知识库组件，跳过获取知识库内容")
        return ""

    logger.debug(f"获取知识库内容，元消息：{message[:30]}...，消息长度: {len(message)}")
    try:
        if not getattr(global_config, "lpmm_knowledge", None) or not getattr(global_config.lpmm_knowledge, "enable", False):
            logger.debug("LPMM知识库未启用，跳过获取知识库内容")
            return ""
        
        time_now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        bot_name = global_config.bot.nickname

        prompt = await global_prompt_manager.format_prompt(
            "lpmm_get_knowledge_prompt",
            bot_name=bot_name,
            time_now=time_now,
            chat_history=message,
            sender=sender,
            target_message=target,
        )
        _, _, _, _, tool_calls = await llm_api.generate_with_model_with_tools(
            prompt,
            model_config=model_config.model_task_config.tool_use,
            tool_options=[SearchKnowledgeFromLPMMTool.get_tool_definition()],
        )
        
        if tool_calls:
            result = await tool_executor.execute_tool_call(tool_calls[0], SearchKnowledgeFromLPMMTool())
            end_time = time.time()
            if not result or not result.get("content"):
                logger.debug("从LPMM知识库获取知识失败，返回空知识...")
                return ""
            found_knowledge_from_lpmm = result.get("content", "")
            logger.debug(
                f"从LPMM知识库获取知识，相关信息：{found_knowledge_from_lpmm[:100]}...，信息长度: {len(found_knowledge_from_lpmm)}"
            )
            related_info += found_knowledge_from_lpmm
            logger.debug(f"获取知识库内容耗时: {(end_time - start_time):.3f}秒")
            
            return f"你有以下这些**知识**：\n{related_info}\n请你**记住上面的知识**，之后可能会用到。\n"
        else:
            logger.debug("模型认为不需要使用LPMM知识库")
            return ""
    except Exception as e:
        logger.error(f"获取知识库内容时发生异常: {str(e)}")
        return ""
