import asyncio
from typing import List, Dict, Any, Optional
from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.utils.url_fetcher import extract_urls
from src.person_info.person_info import Person

logger = get_logger("context_builder")

async def build_tool_info(
    chat_history: str,
    sender: str,
    target: str,
    url_fetcher: Any,
    web_search_manager: Any,
    tool_executor: Any,
    mcp_executor: Any,
    has_mcp_permission: bool,
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
        tool_executor: ToolExecutor 实例
        mcp_executor: MCPExecutor 实例
        has_mcp_permission: 此用户是否有 MCP 权限
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

        search_info = ""
        search_url_info = ""
        if not urls:
            logger.info("未检测到URL，尝试联网搜索判定")
            try:
                search_info = await web_search_manager.build_search_info(
                    chat_history=chat_history,
                    sender=sender,
                    target=target,
                    bot_name=global_config.bot.nickname,
                )
            except Exception as e:
                logger.debug(f"联网搜索信息获取失败: {e}")
            if search_info:
                logger.info("联网搜索已返回结果")
                search_urls = []
                seen_urls = set()
                for url in extract_urls(search_info):
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    search_urls.append(url)
                    if len(search_urls) >= 3:
                        break
                if search_urls:
                    logger.info("开始抓取搜索结果正文")
                    try:
                        search_url_info = await url_fetcher.build_url_info(search_urls)
                    except Exception as e:
                        logger.debug(f"搜索结果正文抓取失败: {e}")
            else:
                logger.info("联网搜索未触发或无结果")

        tool_results: List[Dict[str, Any]] = []
        try:
            # 并行执行两个工具执行器
            tasks = []

            # 1. 标准工具 (Standard)
            tasks.append(
                tool_executor.execute_from_chat_message(
                    sender=sender, target_message=target, chat_history=chat_history, return_details=False
                )
            )

            # 2. MCP工具 (High-Intelligence) - 仅在权限校验通过时执行
            if has_mcp_permission:
                tasks.append(
                    mcp_executor.execute_from_chat_message(
                        sender=sender, target_message=target, chat_history=chat_history, return_details=False
                    )
                )
            else:
                logger.info("用户无 MCP 权限，跳过 MCP 执行器 (Cached)")

            results = await asyncio.gather(*tasks, return_exceptions=True)

            standard_res = results[0]
            mcp_res = results[1] if has_mcp_permission and len(results) > 1 else None

            # 处理 Standard 结果
            if isinstance(standard_res, Exception):
                logger.error(f"Standard 工具执行器失败: {standard_res}")
            else:
                t_res, _, _ = standard_res
                if t_res:
                    tool_results.extend(t_res)

            # 处理 MCP 结果
            if mcp_res:
                if isinstance(mcp_res, Exception):
                    logger.error(f"MCP 工具执行器失败: {mcp_res}")
                else:
                    t_res, _, _ = mcp_res
                    if t_res:
                        tool_results.extend(t_res)
        except Exception as e:
            logger.error(f"工具执行器失败，跳过工具结果: {e}")

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
