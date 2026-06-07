"""
NachoBot — 传统记忆 → A_Memorix 长期记忆迁移工具

将 SQLite 中的 ChatHistory（聊天摘要）和 PersonInfo（人物印象）
批量转化为 A_Memorix 的向量化长期记忆。

用法:
    cd NachoBot
    .venv\Scripts\python.exe scripts/migrate_to_a_memorix.py [--dry-run] [--skip-history] [--skip-person]

注意:
    - 需要先配好 [a_memorix] 的 embedding 配置（API 地址、模型名等）
    - A_Memorix kernel 会在脚本内部自动启动/关闭
    - 已存在的记忆（按 external_id 去重）不会重复写入
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ---- 项目路径注入 ----
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.common.logger import get_logger

logger = get_logger("migrate_to_a_memorix")


# =====================================================================
# 初始化
# =====================================================================

def _init_database():
    """初始化数据库连接和表结构检查。"""
    from src.common.database.database_model import initialize_database
    initialize_database()
    logger.info("数据库初始化完成")


async def _start_a_memorix():
    """启动 A_Memorix kernel（必须在 ingest 之前调用）。"""
    from src.A_memorix.host_service import a_memorix_host_service

    if not a_memorix_host_service.is_enabled():
        logger.error("A_Memorix 未启用，请在 bot_config.toml 的 [a_memorix.plugin] 中设置 enabled = true")
        return False

    logger.info("正在启动 A_Memorix kernel...")
    await a_memorix_host_service.start()
    logger.info("A_Memorix kernel 启动成功")
    return True


async def _stop_a_memorix():
    """关闭 A_Memorix kernel。"""
    from src.A_memorix.host_service import a_memorix_host_service
    await a_memorix_host_service.stop()
    logger.info("A_Memorix kernel 已关闭")


# =====================================================================
# ChatHistory 迁移
# =====================================================================

async def migrate_chat_history(*, dry_run: bool = False):
    """
    将 SQLite chat_history 表中的聊天摘要迁移到 A_Memorix。

    每条记录包含:
      - chat_id: 会话标识
      - theme: 话题标题
      - summary: 概括文本
      - key_point: 关键信息（JSON 列表）
      - keywords: 关键词（JSON 列表）
      - participants: 参与者昵称（JSON 列表）
      - start_time / end_time: 时间范围
    """
    from src.common.database.database_model import ChatHistory
    from src.memory_system.memory_service import memory_service

    records = list(ChatHistory.select())
    total = len(records)
    logger.info(f"=== ChatHistory 迁移开始 (共 {total} 条) ===")

    success, skipped, failed = 0, 0, 0

    for i, rec in enumerate(records, 1):
        try:
            theme = rec.theme or ""
            summary = rec.summary or ""
            key_point_raw = rec.key_point or ""

            # 解析 JSON 字段
            try:
                key_points = json.loads(key_point_raw) if key_point_raw else []
            except (json.JSONDecodeError, TypeError):
                key_points = [key_point_raw] if key_point_raw else []

            try:
                keywords = json.loads(rec.keywords) if rec.keywords else []
            except (json.JSONDecodeError, TypeError):
                keywords = []

            try:
                participants = json.loads(rec.participants) if rec.participants else []
            except (json.JSONDecodeError, TypeError):
                participants = []

            # 内容为空的跳过
            if not summary and not key_points:
                skipped += 1
                continue

            # 拼接文本（与双写时的格式一致）
            text_parts = []
            if theme:
                text_parts.append(f"话题：{theme}")
            if summary:
                text_parts.append(f"概括：{summary}")
            if key_points:
                if isinstance(key_points, list):
                    text_parts.append("关键信息：" + "；".join(str(k) for k in key_points))
                else:
                    text_parts.append(f"关键信息：{key_points}")

            text = "\n".join(text_parts)

            # 时间戳处理 (目标是float)
            time_start, time_end = None, None
            if rec.start_time:
                try:
                    time_start = float(rec.start_time)
                except ValueError:
                    time_start = datetime.fromisoformat(str(rec.start_time)).timestamp()
            
            if rec.end_time:
                try:
                    time_end = float(rec.end_time)
                except ValueError:
                    time_end = datetime.fromisoformat(str(rec.end_time)).timestamp()

            # 去重用的 external_id
            external_id = f"migrate:chat_history:{rec.id}"

            if dry_run:
                logger.info(
                    f"[DRY-RUN] #{i}/{total} chat_id={rec.chat_id} "
                    f"theme={theme[:30]}... text_len={len(text)}"
                )
                success += 1
                continue

            result = await memory_service.ingest_summary(
                external_id=external_id,
                chat_id=rec.chat_id,
                text=text,
                participants=participants,
                time_start=time_start,
                time_end=time_end,
                tags=keywords[:10],  # 限制标签数量
                metadata={
                    "source": "migrate_chat_history",
                    "theme": theme,
                    "key_point": key_points,
                    "original_id": rec.id,
                },
            )

            if isinstance(result, dict) and result.get("success") is not False:
                success += 1
            else:
                failed += 1
                logger.warning(f"ChatHistory #{rec.id} 写入返回异常: {result}")

        except Exception as e:
            failed += 1
            logger.error(f"ChatHistory #{rec.id} 迁移失败: {e}")

        # 进度报告
        if i % 20 == 0 or i == total:
            logger.info(f"进度: {i}/{total} (成功:{success} 跳过:{skipped} 失败:{failed})")

        # 速率控制：每 5 条暂停 100ms，避免 Embedding API 限流
        if not dry_run and i % 5 == 0:
            await asyncio.sleep(0.1)

    logger.info(
        f"=== ChatHistory 迁移完成 "
        f"(总计:{total} 成功:{success} 跳过:{skipped} 失败:{failed}) ==="
    )
    return {"total": total, "success": success, "skipped": skipped, "failed": failed}


# =====================================================================
# PersonInfo 迁移
# =====================================================================

async def migrate_person_info(*, dry_run: bool = False):
    """
    将 SQLite person_info 表中的人物印象迁移到 A_Memorix。

    使用 ingest_text（而非 ingest_summary），携带:
      - person_ids: 关联到具体人物
      - source_type: "person_fact" 标记为人物事实
      - 原始 memory_points 文本
    """
    from src.common.database.database_model import PersonInfo
    from src.memory_system.memory_service import memory_service

    records = list(PersonInfo.select())
    total = len(records)
    logger.info(f"=== PersonInfo 迁移开始 (共 {total} 条) ===")

    success, skipped, failed = 0, 0, 0

    for i, rec in enumerate(records, 1):
        try:
            # 只迁移有实际印象内容的用户
            if not rec.memory_points or not rec.memory_points.strip():
                skipped += 1
                continue

            name = rec.person_name or rec.nickname or "未知用户"
            person_id = rec.person_id or ""
            user_id = rec.user_id or ""
            platform = rec.platform or ""

            # 构造记忆文本
            text = f"关于 {name} 的印象和记忆：\n{rec.memory_points.strip()}"

            # 去重 external_id
            external_id = f"migrate:person_info:{rec.id}"

            # 时间元数据
            timestamp_float = None
            if rec.last_know:
                try:
                    timestamp_float = float(rec.last_know)
                except ValueError:
                    timestamp_float = datetime.fromisoformat(str(rec.last_know)).timestamp()
                except OSError:
                    pass

            if dry_run:
                logger.info(
                    f"[DRY-RUN] #{i}/{total} person={name} "
                    f"platform={platform} text_len={len(text)}"
                )
                success += 1
                continue

            result = await memory_service.ingest_text(
                external_id=external_id,
                source_type="person_fact",
                text=text,
                chat_id=f"person:{person_id}",
                person_ids=[person_id] if person_id else [],
                participants=[name],
                timestamp=timestamp_float,
                tags=["person_info", platform] if platform else ["person_info"],
                metadata={
                    "source": "migrate_person_info",
                    "person_name": name,
                    "person_id": person_id,
                    "user_id": user_id,
                    "platform": platform,
                    "original_id": rec.id,
                    "is_known": rec.is_known,
                },
            )

            if isinstance(result, dict) and result.get("success") is not False:
                success += 1
            else:
                failed += 1
                logger.warning(f"PersonInfo #{rec.id} ({name}) 写入返回异常: {result}")

        except Exception as e:
            failed += 1
            logger.error(f"PersonInfo #{rec.id} 迁移失败: {e}")

        if i % 20 == 0 or i == total:
            logger.info(f"进度: {i}/{total} (成功:{success} 跳过:{skipped} 失败:{failed})")

        if not dry_run and i % 5 == 0:
            await asyncio.sleep(0.1)

    logger.info(
        f"=== PersonInfo 迁移完成 "
        f"(总计:{total} 成功:{success} 跳过:{skipped} 失败:{failed}) ==="
    )
    return {"total": total, "success": success, "skipped": skipped, "failed": failed}


# =====================================================================
# 入口
# =====================================================================

async def main(args: argparse.Namespace):
    start_ts = time.time()
    logger.info("=" * 60)
    logger.info("NachoBot 传统记忆 → A_Memorix 迁移工具")
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("⚠️  DRY-RUN 模式：仅打印，不实际写入")

    # 1. 初始化数据库
    _init_database()

    # 2. 启动 A_Memorix kernel（非 dry-run 时需要）
    if not args.dry_run:
        if not await _start_a_memorix():
            return

    results = {}

    # 3. 迁移 ChatHistory
    if not args.skip_history:
        results["chat_history"] = await migrate_chat_history(dry_run=args.dry_run)
    else:
        logger.info("跳过 ChatHistory 迁移 (--skip-history)")

    # 4. 迁移 PersonInfo
    if not args.skip_person:
        results["person_info"] = await migrate_person_info(dry_run=args.dry_run)
    else:
        logger.info("跳过 PersonInfo 迁移 (--skip-person)")

    # 5. 关闭 kernel
    if not args.dry_run:
        await _stop_a_memorix()

    elapsed = time.time() - start_ts
    logger.info("=" * 60)
    logger.info(f"迁移完成，耗时 {elapsed:.1f}s")
    for name, stat in results.items():
        logger.info(f"  {name}: 总计={stat['total']} 成功={stat['success']} 跳过={stat['skipped']} 失败={stat['failed']}")
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="传统记忆 → A_Memorix 迁移")
    parser.add_argument("--dry-run", action="store_true", help="仅打印要迁移的内容，不实际写入")
    parser.add_argument("--skip-history", action="store_true", help="跳过 ChatHistory 迁移")
    parser.add_argument("--skip-person", action="store_true", help="跳过 PersonInfo 迁移")
    args = parser.parse_args()
    asyncio.run(main(args))
