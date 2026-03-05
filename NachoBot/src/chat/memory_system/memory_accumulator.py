"""
记忆沉淀积累器
在每日的凌晨2点到3点进行记忆沉淀。
提取从上一次沉淀到现在的新消息，使用大模型压缩并写入到关系网络记忆图谱中。
"""

import asyncio
import time
from datetime import datetime

from src.common.logger import get_logger
from src.config.config import global_config
from src.plugin_system.apis import message_api
from src.chat.memory_system.Hippocampus import hippocampus_manager
from src.manager.async_task_manager import AsyncTask
from src.common.database.database_model import ChatStreams

logger = get_logger("memory_accumulator")


class MemoryAccumulator(AsyncTask):
    """后台监控任务：每日定时扫描和沉淀所有聊天流的记忆图谱"""

    def __init__(self):
        super().__init__(task_name="MemoryAccumulator", run_interval=60)
        self.log_prefix = "[MemoryAccumulator]"

        # 时间记录与状态标记
        self.last_scan_time: dict[str, float] = {}  # 记录每个 chat_id 的上次扫描时间

        # 记录上一次成功执行沉淀的日期天数，比如 "2024-05-01"
        self.last_executed_day = "1970-01-01"

    async def run(self):
        """检查并执行当天的沉淀"""
        if not global_config.relationship.enable_relationship:
            # logger.debug(f"{self.log_prefix} 关系系统未启用，跳过记忆沉淀器")
            return

        now = datetime.now()
        current_date_str = now.strftime("%Y-%m-%d")

        # 1. 判断时间是否在 02:00:00 到 03:00:00 之间 (测试阶段临时注释)
        if now.hour != 2:
            return

        # 2. 判断今天是否已经成功执行过
        if current_date_str == self.last_executed_day:
            return

        logger.info(f"{self.log_prefix} 触发每日记忆沉淀时间 (当前: {now.strftime('%H:%M:%S')})")

        # 获取数据库中所有的聊天流 ID
        def _get_all_stream_ids():
            return [stream.stream_id for stream in ChatStreams.select()]

        try:
            stream_ids = await asyncio.to_thread(_get_all_stream_ids)
        except Exception as e:
            logger.error(f"{self.log_prefix} 获取聊天流列表失败: {e}", exc_info=True)
            return

        logger.info(f"{self.log_prefix} 开始进行图谱记忆沉淀，共需处理 {len(stream_ids)} 个聊天流")

        current_time_float = time.time()
        success = True

        for chat_id in stream_ids:
            try:
                await self._process_single_stream(chat_id, current_time_float)
            except Exception as e:
                logger.error(f"{self.log_prefix} 处理聊天流 {chat_id} 发生异常: {e}", exc_info=True)
                success = False

        if success:
            logger.info(f"{self.log_prefix} 今日的所有记忆沉淀任务完成")
            self.last_executed_day = current_date_str

    async def _process_single_stream(self, chat_id: str, current_time_float: float):
        """处理单一聊天流的记忆沉淀"""
        # 如果是首次运行（last_scan_time 是启动时间但想要捞取更早的消息），
        # 默认捞取过去 24 小时（86400秒）内的消息作为兜底，防止错失。
        # 这里为了严谨，我们直接用上次扫描时间获取 (测试阶段临时改为写死过去48小时)
        last_scan = self.last_scan_time.get(chat_id, current_time_float)
        # start_time_limit = max(last_scan, current_time_float - 86400 * 2)
        start_time_limit = current_time_float - 86400 * 2
        _ = last_scan  # 消除未使用变量警告，测试完毕后恢复使用

        # 获取新消息
        new_messages = message_api.get_messages_by_time_in_chat(
            chat_id=chat_id,
            start_time=start_time_limit,
            end_time=current_time_float,
            limit=0,
            limit_mode="latest",
            filter_mai=True,  # 过滤掉系统产生的命令/不重要消息
            filter_command=True,
        )

        # 消息条数太少，说明这一天没有怎么聊天，不需要沉淀
        # if not new_messages or len(new_messages) <= 3:
        #     self.last_scan_time[chat_id] = current_time_float
        #     return

        if len(new_messages) > 0:
            logger.info(f"{self.log_prefix} [{chat_id[:8]}...] 开始沉淀，共 {len(new_messages)} 条对话")

        try:
            # 第一阶段：压缩提取记忆主题和摘要
            hc = hippocampus_manager.get_hippocampus()
            if not hc:
                logger.warning(f"{self.log_prefix} hippocampus_manager 未初始化完毕！")
                return

            pg = hc.parahippocampal_gyrus

            # 调用大模型生成压缩记忆 (压缩率按默认 0.1)
            compressed_memory, similar_topics_dict = await pg.memory_compress(messages=new_messages, compress_rate=0.1)

            if not compressed_memory:
                # logger.debug(f"{self.log_prefix} [{chat_id[:8]}] 本次沉淀未提取出有效记忆主题。")
                pass
            else:
                # 第二阶段：遍历提取出的主题与内容，写入节点和边，并同步数据库
                success_count = 0
                for item in compressed_memory:
                    try:
                        topic, summary = item
                        # 为了兼容 add_memory_with_similar，我们只把对应的相似字典交过去
                        single_dict = {topic: similar_topics_dict.get(topic, [])}

                        success = await pg.add_memory_with_similar(memory_item=summary, similar_topics_dict=single_dict)
                        if success:
                            success_count += 1
                    except Exception as e:
                        logger.error(f"{self.log_prefix} 同步单一记忆节点 '{topic}' 失败: {e}")

                logger.info(
                    f"{self.log_prefix} [{chat_id[:8]}] 沉淀完成！共提取 {len(compressed_memory)} 个话题，入库 {success_count} 个。"
                )

            # 4. 执行成功后，更新状态和时间
            self.last_scan_time[chat_id] = current_time_float

        except Exception as e:
            logger.error(f"{self.log_prefix} [{chat_id[:8]}] 执行 memory_compress 发生严重异常: {e}", exc_info=True)
            # 向上抛出以标记此流失败
            raise
