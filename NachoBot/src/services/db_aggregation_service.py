"""
数据库聚合服务

将 llm_usage 和 messages 表中的明细数据按小时增量聚合到汇总表中，
避免统计查询时对百万级原始表进行全量扫描。

聚合完成后可安全地清理旧的明细数据。

设计参考: MaiBot statistics_aggregation_service.py
"""

import asyncio
import time
from datetime import datetime, timedelta

from src.common.database.database import db
from src.common.database.database_model import (
    LLMUsage,
    Messages,
    LLMUsageHourly,
    MessageHourly,
    AggregationCursor,
)
from src.common.logger import get_logger
from src.manager.async_task_manager import AsyncTask

logger = get_logger("db_aggregation")

# 每批处理的记录数
BATCH_SIZE = 5000

# 聚合完成后，明细表保留最近多少天的数据（早于此的将被删除）
DEFAULT_LLM_KEEP_DAYS = 14
DEFAULT_MSG_KEEP_DAYS = 30


def _bucket_from_datetime(dt: datetime) -> str:
    """将 datetime 截断到小时粒度，返回标准格式字符串。"""
    return dt.strftime("%Y-%m-%d %H:00:00")


def _get_cursor(source_name: str) -> int:
    """获取指定聚合源的游标（上次处理到的最大 ID）。"""
    try:
        cursor_row = AggregationCursor.get(AggregationCursor.source_name == source_name)
        return cursor_row.last_processed_id
    except AggregationCursor.DoesNotExist:
        return 0


def _set_cursor(source_name: str, last_id: int) -> None:
    """更新指定聚合源的游标。"""
    now = datetime.now()
    AggregationCursor.insert(
        source_name=source_name,
        last_processed_id=last_id,
        updated_at=now,
    ).on_conflict(
        conflict_target=[AggregationCursor.source_name],
        update={
            AggregationCursor.last_processed_id: last_id,
            AggregationCursor.updated_at: now,
        },
    ).execute()


def aggregate_llm_usage(batch_size: int = BATCH_SIZE) -> int:
    """
    增量聚合 llm_usage → statistics_llm_usage_hourly。

    返回本次处理的记录数。
    """
    last_id = _get_cursor("llm_usage")
    total_processed = 0

    while True:
        # 取一批明细
        records = list(
            LLMUsage.select()
            .where(LLMUsage.id > last_id)
            .order_by(LLMUsage.id.asc())
            .limit(batch_size)
        )
        if not records:
            break

        # 按 (bucket_time, model_name, request_type) 分组聚合
        buckets: dict[tuple[str, str, str], dict] = {}

        for r in records:
            try:
                ts = r.timestamp
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                bucket = _bucket_from_datetime(ts)
            except Exception:
                bucket = "1970-01-01 00:00:00"

            model = r.model_assign_name or r.model_name or "unknown"
            req_type = r.request_type or "unknown"
            key = (bucket, model, req_type)

            if key not in buckets:
                buckets[key] = {
                    "request_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "total_cost": 0.0,
                    "time_cost_sum": 0.0,
                    "time_cost_sq_sum": 0.0,
                }

            b = buckets[key]
            b["request_count"] += 1
            b["prompt_tokens"] += r.prompt_tokens or 0
            b["completion_tokens"] += r.completion_tokens or 0
            b["total_tokens"] += r.total_tokens or 0
            b["total_cost"] += r.cost or 0.0
            tc = r.time_cost or 0.0
            b["time_cost_sum"] += tc
            b["time_cost_sq_sum"] += tc * tc

        # 写入聚合表（INSERT ... ON CONFLICT DO UPDATE）
        with db.atomic():
            for (bucket, model, req_type), agg in buckets.items():
                LLMUsageHourly.insert(
                    bucket_time=bucket,
                    model_name=model,
                    request_type=req_type,
                    request_count=agg["request_count"],
                    prompt_tokens=agg["prompt_tokens"],
                    completion_tokens=agg["completion_tokens"],
                    total_tokens=agg["total_tokens"],
                    total_cost=agg["total_cost"],
                    time_cost_sum=agg["time_cost_sum"],
                    time_cost_sq_sum=agg["time_cost_sq_sum"],
                ).on_conflict(
                    conflict_target=[
                        LLMUsageHourly.bucket_time,
                        LLMUsageHourly.model_name,
                        LLMUsageHourly.request_type,
                    ],
                    update={
                        LLMUsageHourly.request_count: LLMUsageHourly.request_count + agg["request_count"],
                        LLMUsageHourly.prompt_tokens: LLMUsageHourly.prompt_tokens + agg["prompt_tokens"],
                        LLMUsageHourly.completion_tokens: LLMUsageHourly.completion_tokens + agg["completion_tokens"],
                        LLMUsageHourly.total_tokens: LLMUsageHourly.total_tokens + agg["total_tokens"],
                        LLMUsageHourly.total_cost: LLMUsageHourly.total_cost + agg["total_cost"],
                        LLMUsageHourly.time_cost_sum: LLMUsageHourly.time_cost_sum + agg["time_cost_sum"],
                        LLMUsageHourly.time_cost_sq_sum: LLMUsageHourly.time_cost_sq_sum + agg["time_cost_sq_sum"],
                    },
                ).execute()

        # 更新游标
        last_id = records[-1].id
        _set_cursor("llm_usage", last_id)
        total_processed += len(records)

        # 如果不到 batch_size 说明已处理完毕
        if len(records) < batch_size:
            break

    return total_processed


def aggregate_messages(batch_size: int = BATCH_SIZE) -> int:
    """
    增量聚合 messages → statistics_message_hourly。

    返回本次处理的记录数。
    """
    last_id = _get_cursor("messages")
    total_processed = 0

    while True:
        records = list(
            Messages.select()
            .where(Messages.id > last_id)
            .order_by(Messages.id.asc())
            .limit(batch_size)
        )
        if not records:
            break

        # 按 (bucket_time, chat_id) 分组聚合
        buckets: dict[tuple[str, str], dict] = {}

        for r in records:
            try:
                ts = datetime.fromtimestamp(r.time)
                bucket = _bucket_from_datetime(ts)
            except Exception:
                bucket = "1970-01-01 00:00:00"

            # 确定 chat_id 和 chat_name
            if r.chat_info_group_id:
                chat_id = f"g{r.chat_info_group_id}"
                chat_name = r.chat_info_group_name or f"群{r.chat_info_group_id}"
                chat_type = "group"
            elif r.user_id:
                chat_id = f"u{r.user_id}"
                chat_name = r.user_nickname or r.user_id
                chat_type = "private"
            else:
                chat_id = "unknown"
                chat_name = "unknown"
                chat_type = "unknown"

            key = (bucket, chat_id)
            if key not in buckets:
                buckets[key] = {
                    "chat_name": chat_name,
                    "chat_type": chat_type,
                    "message_count": 0,
                }
            b = buckets[key]
            b["message_count"] += 1
            # 保留最新的 chat_name
            b["chat_name"] = chat_name

        # 写入聚合表
        with db.atomic():
            for (bucket, chat_id), agg in buckets.items():
                MessageHourly.insert(
                    bucket_time=bucket,
                    chat_id=chat_id,
                    chat_name=agg["chat_name"],
                    chat_type=agg["chat_type"],
                    message_count=agg["message_count"],
                ).on_conflict(
                    conflict_target=[
                        MessageHourly.bucket_time,
                        MessageHourly.chat_id,
                    ],
                    update={
                        MessageHourly.message_count: MessageHourly.message_count + agg["message_count"],
                        MessageHourly.chat_name: agg["chat_name"],
                    },
                ).execute()

        last_id = records[-1].id
        _set_cursor("messages", last_id)
        total_processed += len(records)

        if len(records) < batch_size:
            break

    return total_processed


def cleanup_old_records(
    llm_keep_days: int = DEFAULT_LLM_KEEP_DAYS,
    msg_keep_days: int = DEFAULT_MSG_KEEP_DAYS,
) -> dict[str, int]:
    """
    清理已被聚合的旧明细数据。

    仅删除早于保留天数的记录，确保最近的数据仍可通过明细表访问。

    Returns:
        各表删除的行数。
    """
    results = {}

    # 清理 llm_usage（timestamp 是 DateTimeField）
    llm_cutoff = datetime.now() - timedelta(days=llm_keep_days)
    with db.atomic():
        deleted = LLMUsage.delete().where(LLMUsage.timestamp < llm_cutoff).execute()
    results["llm_usage"] = deleted

    # 清理 messages（time 是 DoubleField，unix timestamp）
    msg_cutoff = (datetime.now() - timedelta(days=msg_keep_days)).timestamp()
    with db.atomic():
        deleted = Messages.delete().where(Messages.time < msg_cutoff).execute()
    results["messages"] = deleted

    return results


def vacuum_database() -> None:
    """
    执行 WAL checkpoint + VACUUM 回收磁盘空间。

    注意：VACUUM 需要在没有其他活跃连接/事务时执行。
    """
    try:
        db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute_sql("VACUUM")
        logger.info("数据库 VACUUM 完成")
    except Exception as e:
        logger.warning(f"VACUUM 执行失败: {e}")


def run_full_aggregation_cycle(
    llm_keep_days: int = DEFAULT_LLM_KEEP_DAYS,
    msg_keep_days: int = DEFAULT_MSG_KEEP_DAYS,
    do_cleanup: bool = False,
    do_vacuum: bool = False,
) -> dict:
    """
    执行完整的聚合 + 清理周期。

    Returns:
        聚合和清理的统计信息。
    """
    start_time = time.time()
    stats = {}

    # 1. 聚合
    llm_count = aggregate_llm_usage()
    msg_count = aggregate_messages()
    stats["aggregated_llm_usage"] = llm_count
    stats["aggregated_messages"] = msg_count

    if llm_count > 0 or msg_count > 0:
        logger.info(f"聚合完成: llm_usage={llm_count}条, messages={msg_count}条")

    # 2. 清理旧数据（仅在明确启用时执行）
    if do_cleanup:
        cleanup_stats = cleanup_old_records(llm_keep_days, msg_keep_days)
        stats["cleanup"] = cleanup_stats

        if any(v > 0 for v in cleanup_stats.values()):
            logger.info(f"旧数据清理完成: {cleanup_stats}")

        # 3. 可选的 VACUUM
        if do_vacuum and any(v > 0 for v in cleanup_stats.values()):
            vacuum_database()
            stats["vacuumed"] = True
        else:
            stats["vacuumed"] = False
    else:
        stats["cleanup"] = {}
        stats["vacuumed"] = False

    stats["elapsed_seconds"] = round(time.time() - start_time, 2)
    return stats


class DBAggregationTask(AsyncTask):
    """
    定时数据库聚合任务。

    - 每隔 interval 秒执行一次增量聚合
    - 每隔 cleanup_interval 次聚合后执行一次旧数据清理
    - 每隔 vacuum_interval 次清理后执行一次 VACUUM
    """

    def __init__(
        self,
        run_interval: int = 3600,         # 默认每小时聚合一次
        cleanup_every_n: int = 6,         # 每 6 次聚合后清理一次旧数据（即 6 小时）
        vacuum_every_n_cleanup: int = 4,  # 每 4 次清理后 VACUUM 一次（即 24 小时）
        llm_keep_days: int = DEFAULT_LLM_KEEP_DAYS,
        msg_keep_days: int = DEFAULT_MSG_KEEP_DAYS,
    ):
        super().__init__(
            task_name="DB Aggregation Task",
            wait_before_start=60,  # 启动后 60 秒开始第一次聚合
            run_interval=run_interval,
        )
        self._cleanup_every_n = cleanup_every_n
        self._vacuum_every_n = vacuum_every_n_cleanup
        self._llm_keep_days = llm_keep_days
        self._msg_keep_days = msg_keep_days
        self._run_count = 0
        self._cleanup_count = 0

    async def run(self):
        """每次执行时进行增量聚合，周期性清理和 VACUUM。"""
        self._run_count += 1
        should_cleanup = (self._run_count % self._cleanup_every_n == 0)
        should_vacuum = False

        if should_cleanup:
            self._cleanup_count += 1
            should_vacuum = (self._cleanup_count % self._vacuum_every_n == 0)

        try:
            loop = asyncio.get_event_loop()
            stats = await loop.run_in_executor(
                None,
                lambda: run_full_aggregation_cycle(
                    llm_keep_days=self._llm_keep_days,
                    msg_keep_days=self._msg_keep_days,
                    do_cleanup=should_cleanup,
                    do_vacuum=should_vacuum,
                ),
            )
            total_agg = stats.get("aggregated_llm_usage", 0) + stats.get("aggregated_messages", 0)
            if total_agg > 0:
                logger.info(
                    f"数据库维护完成 (第{self._run_count}次): "
                    f"聚合={total_agg}条, "
                    f"清理={'是' if should_cleanup else '否'}, "
                    f"VACUUM={'是' if stats.get('vacuumed') else '否'}, "
                    f"耗时={stats.get('elapsed_seconds', 0)}s"
                )
        except Exception as e:
            logger.error(f"数据库聚合任务执行失败: {e}", exc_info=True)
