import json
import os
import asyncio
from src.common.database.database_model import GraphNodes
from src.common.logger import get_logger

logger = get_logger("migrate")


async def migrate_memory_items_to_string():
    """
    将数据库中记忆节点的memory_items从list格式迁移到string格式
    并根据原始list的项目数量设置weight值
    """
    logger.info("开始迁移记忆节点格式...")

    migration_stats = {
        "total_nodes": 0,
        "converted_nodes": 0,
        "already_string_nodes": 0,
        "empty_nodes": 0,
        "error_nodes": 0,
        "weight_updated_nodes": 0,
        "truncated_nodes": 0,
    }

    try:
        # 获取所有图节点
        all_nodes = GraphNodes.select()
        migration_stats["total_nodes"] = all_nodes.count()

        logger.info(f"找到 {migration_stats['total_nodes']} 个记忆节点")

        for node in all_nodes:
            try:
                concept = node.concept
                memory_items_raw = node.memory_items.strip() if node.memory_items else ""
                original_weight = node.weight if hasattr(node, "weight") and node.weight is not None else 1.0

                # 如果为空，跳过
                if not memory_items_raw:
                    migration_stats["empty_nodes"] += 1
                    logger.debug(f"跳过空节点: {concept}")
                    continue

                try:
                    # 尝试解析JSON
                    parsed_data = json.loads(memory_items_raw)

                    if isinstance(parsed_data, list):
                        # 如果是list格式，需要转换
                        if parsed_data:
                            # 转换为字符串格式
                            new_memory_items = " | ".join(str(item) for item in parsed_data)
                            original_length = len(new_memory_items)

                            # 检查长度并截断
                            if len(new_memory_items) > 100:
                                new_memory_items = new_memory_items[:100]
                                migration_stats["truncated_nodes"] += 1
                                logger.debug(f"节点 '{concept}' 内容过长，从 {original_length} 字符截断到 100 字符")

                            new_weight = float(len(parsed_data))  # weight = list项目数量

                            # 更新数据库
                            node.memory_items = new_memory_items
                            node.weight = new_weight
                            node.save()

                            migration_stats["converted_nodes"] += 1
                            migration_stats["weight_updated_nodes"] += 1

                            length_info = f" (截断: {original_length}→100)" if original_length > 100 else ""
                            logger.info(
                                f"转换节点 '{concept}': {len(parsed_data)} 项 -> 字符串{length_info}, weight: {original_weight} -> {new_weight}"
                            )
                        else:
                            # 空list，设置为空字符串
                            node.memory_items = ""
                            node.weight = 1.0
                            node.save()

                            migration_stats["converted_nodes"] += 1
                            logger.debug(f"转换空list节点: {concept}")

                    elif isinstance(parsed_data, str):
                        # 已经是字符串格式，检查长度和weight
                        current_content = parsed_data
                        original_length = len(current_content)
                        content_truncated = False

                        # 检查长度并截断
                        if len(current_content) > 100:
                            current_content = current_content[:100]
                            content_truncated = True
                            migration_stats["truncated_nodes"] += 1
                            node.memory_items = current_content
                            logger.debug(f"节点 '{concept}' 字符串内容过长，从 {original_length} 字符截断到 100 字符")

                        # 检查weight是否需要更新
                        update_needed = False
                        if original_weight == 1.0:
                            # 如果weight还是默认值，可以根据内容复杂度估算
                            content_parts = (
                                current_content.split(" | ") if " | " in current_content else [current_content]
                            )
                            estimated_weight = max(1.0, float(len(content_parts)))

                            if estimated_weight != original_weight:
                                node.weight = estimated_weight
                                update_needed = True
                                logger.debug(f"更新字符串节点权重 '{concept}': {original_weight} -> {estimated_weight}")

                        # 如果内容被截断或权重需要更新，保存到数据库
                        if content_truncated or update_needed:
                            node.save()
                            if update_needed:
                                migration_stats["weight_updated_nodes"] += 1
                            if content_truncated:
                                migration_stats["converted_nodes"] += 1  # 算作转换节点
                            else:
                                migration_stats["already_string_nodes"] += 1
                        else:
                            migration_stats["already_string_nodes"] += 1

                    else:
                        # 其他JSON类型，转换为字符串
                        new_memory_items = str(parsed_data) if parsed_data else ""
                        original_length = len(new_memory_items)

                        # 检查长度并截断
                        if len(new_memory_items) > 100:
                            new_memory_items = new_memory_items[:100]
                            migration_stats["truncated_nodes"] += 1
                            logger.debug(f"节点 '{concept}' 其他类型内容过长，从 {original_length} 字符截断到 100 字符")

                        node.memory_items = new_memory_items
                        node.weight = 1.0
                        node.save()

                        migration_stats["converted_nodes"] += 1
                        length_info = f" (截断: {original_length}→100)" if original_length > 100 else ""
                        logger.debug(f"转换其他类型节点: {concept}{length_info}")

                except json.JSONDecodeError:
                    # 不是JSON格式，假设已经是纯字符串
                    # 检查是否是带引号的字符串
                    if memory_items_raw.startswith('"') and memory_items_raw.endswith('"'):
                        # 去掉引号
                        clean_content = memory_items_raw[1:-1]
                        original_length = len(clean_content)

                        # 检查长度并截断
                        if len(clean_content) > 100:
                            clean_content = clean_content[:100]
                            migration_stats["truncated_nodes"] += 1
                            logger.debug(f"节点 '{concept}' 去引号内容过长，从 {original_length} 字符截断到 100 字符")

                        node.memory_items = clean_content
                        node.save()

                        migration_stats["converted_nodes"] += 1
                        length_info = f" (截断: {original_length}→100)" if original_length > 100 else ""
                        logger.debug(f"去除引号节点: {concept}{length_info}")
                    else:
                        # 已经是纯字符串格式，检查长度
                        current_content = memory_items_raw
                        original_length = len(current_content)

                        # 检查长度并截断
                        if len(current_content) > 100:
                            current_content = current_content[:100]
                            node.memory_items = current_content
                            node.save()

                            migration_stats["converted_nodes"] += 1  # 算作转换节点
                            migration_stats["truncated_nodes"] += 1
                            logger.debug(f"节点 '{concept}' 纯字符串内容过长，从 {original_length} 字符截断到 100 字符")
                        else:
                            migration_stats["already_string_nodes"] += 1
                            logger.debug(f"已是字符串格式节点: {concept}")

            except Exception as e:
                migration_stats["error_nodes"] += 1
                logger.error(f"处理节点 {concept} 时发生错误: {e}")
                continue

    except Exception as e:
        logger.error(f"迁移过程中发生严重错误: {e}")
        raise

    # 输出迁移统计
    logger.info("=== 记忆节点迁移完成 ===")
    logger.info(f"总节点数: {migration_stats['total_nodes']}")
    logger.info(f"已转换节点: {migration_stats['converted_nodes']}")
    logger.info(f"已是字符串格式: {migration_stats['already_string_nodes']}")
    logger.info(f"空节点: {migration_stats['empty_nodes']}")
    logger.info(f"错误节点: {migration_stats['error_nodes']}")
    logger.info(f"权重更新节点: {migration_stats['weight_updated_nodes']}")
    logger.info(f"内容截断节点: {migration_stats['truncated_nodes']}")

    success_rate = (
        (migration_stats["converted_nodes"] + migration_stats["already_string_nodes"])
        / migration_stats["total_nodes"]
        * 100
        if migration_stats["total_nodes"] > 0
        else 0
    )
    logger.info(f"迁移成功率: {success_rate:.1f}%")

    return migration_stats


async def set_all_person_known():
    """
    将person_info库中所有记录的is_known字段设置为True
    在设置之前，先清理掉user_id或platform为空的记录
    """
    logger.info("开始设置所有person_info记录为已认识...")

    try:
        from src.common.database.database_model import PersonInfo

        # 获取所有PersonInfo记录
        all_persons = PersonInfo.select()
        total_count = all_persons.count()

        logger.info(f"找到 {total_count} 个人员记录")

        if total_count == 0:
            logger.info("没有找到任何人员记录")
            return {"total": 0, "deleted": 0, "updated": 0, "known_count": 0}

        # 删除user_id或platform为空的记录
        deleted_count = 0
        invalid_records = PersonInfo.select().where(
            (PersonInfo.user_id.is_null())
            | (PersonInfo.user_id == "")
            | (PersonInfo.platform.is_null())
            | (PersonInfo.platform == "")
        )

        # 记录要删除的记录信息
        for record in invalid_records:
            user_id_info = f"'{record.user_id}'" if record.user_id else "NULL"
            platform_info = f"'{record.platform}'" if record.platform else "NULL"
            person_name_info = f"'{record.person_name}'" if record.person_name else "无名称"
            logger.debug(
                f"删除无效记录: person_id={record.person_id}, user_id={user_id_info}, platform={platform_info}, person_name={person_name_info}"
            )

        # 执行删除操作
        deleted_count = (
            PersonInfo.delete()
            .where(
                (PersonInfo.user_id.is_null())
                | (PersonInfo.user_id == "")
                | (PersonInfo.platform.is_null())
                | (PersonInfo.platform == "")
            )
            .execute()
        )

        if deleted_count > 0:
            logger.info(f"删除了 {deleted_count} 个user_id或platform为空的记录")
        else:
            logger.info("没有发现user_id或platform为空的记录")

        # 重新获取剩余记录数量
        remaining_count = PersonInfo.select().count()
        logger.info(f"清理后剩余 {remaining_count} 个有效记录")

        if remaining_count == 0:
            logger.info("清理后没有剩余记录")
            return {"total": total_count, "deleted": deleted_count, "updated": 0, "known_count": 0}

        # 批量更新剩余记录的is_known字段为True
        updated_count = PersonInfo.update(is_known=True).execute()

        logger.info(f"成功更新 {updated_count} 个人员记录的is_known字段为True")

        # 验证更新结果
        known_count = PersonInfo.select().where(PersonInfo.is_known).count()

        result = {"total": total_count, "deleted": deleted_count, "updated": updated_count, "known_count": known_count}

        logger.info("=== person_info更新完成 ===")
        logger.info(f"原始记录数: {result['total']}")
        logger.info(f"删除记录数: {result['deleted']}")
        logger.info(f"更新记录数: {result['updated']}")
        logger.info(f"已认识记录数: {result['known_count']}")

        return result

    except Exception as e:
        logger.error(f"更新person_info过程中发生错误: {e}")
        raise


async def check_and_run_migrations():
    # 获取根目录
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    data_dir = os.path.join(project_root, "data")
    temp_dir = os.path.join(data_dir, "temp")
    done_file = os.path.join(temp_dir, "done.mem")

    # 检查done.mem是否存在
    if not os.path.exists(done_file):
        # 如果temp目录不存在则创建
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
        # 执行迁移函数
        # 依次执行两个异步函数
        await asyncio.sleep(3)
        await migrate_memory_items_to_string()
        await set_all_person_known()
        # 创建done.mem文件
        with open(done_file, "w", encoding="utf-8") as f:
            f.write("done")
