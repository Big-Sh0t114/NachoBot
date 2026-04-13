import time
import json
import hashlib
import random
from src.common.logger import get_logger
from src.common.database.database import db
from src.common.database.database_model import PersonInfo, PersonBinding, BindRequest
from src.person_info.person_info import get_person_id

logger = get_logger("bind_manager")

# 聚合行的固定平台标识
MERGED_PLATFORM = "__merged__"


class BindManager:
    def __init__(self):
        self.expire_time = 300  # 验证码有效期 5 分钟

    # ──────────────────────────────────────────────────────────────
    #  工具方法
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_original_person_id(platform: str, user_id: str) -> str:
        """计算某个平台账号的原始 person_id（与 get_person_id 的 MD5 逻辑一致）"""
        return hashlib.md5(f"{platform}_{user_id}".encode()).hexdigest()

    @staticmethod
    def _compute_group_id(original_ids: list[str]) -> str:
        """根据所有成员的原始 person_id 计算聚合组 ID（排序后 MD5，保证稳定性）"""
        key = ",".join(sorted(original_ids))
        return hashlib.md5(key.encode()).hexdigest()

    @staticmethod
    def _find_binding_flexible(target_platform: str, target_user_id: str):
        """
        灵活查找 PersonBinding 映射行。
        适配器可能使用 'bilibili.live' 而用户指令只写 'bilibili'，
        因此先精确匹配，再用 LIKE 前缀匹配。
        """
        target_user_id = str(target_user_id)
        # 精确匹配
        row = PersonBinding.get_or_none(
            PersonBinding.platform == target_platform, PersonBinding.platform_user_id == target_user_id
        )
        if row:
            return row
        # 前缀匹配：bilibili → bilibili.live 等
        row = PersonBinding.get_or_none(
            PersonBinding.platform.startswith(target_platform), PersonBinding.platform_user_id == target_user_id
        )
        return row

    @staticmethod
    def _resolve_user_id(target_platform: str, identifier: str) -> str:
        """
        尝试将用户输入的标识符解析为真实的 user_id。
        默认将输入作为 UID 处理。只有当输入包含非数字字符时，
        才依次尝试按 person_name、nickname 进行查找匹配。
        """
        identifier = str(identifier).strip()

        # 如果是纯数字，默认其为真实的 user_id，直接返回
        if identifier.isdigit():
            return identifier

        # 1. 首先按 person_name（NachoBot 内部名称）查找
        person = PersonInfo.get_or_none(
            PersonInfo.platform.startswith(target_platform), PersonInfo.person_name == identifier
        )
        if person and person.user_id:
            logger.info(f"成功将 person_name '{identifier}' 解析为真实 UID: {person.user_id}")
            return person.user_id

        # 2. 再按 nickname（平台显示昵称）查找，用户更可能输入的是平台昵称
        person = PersonInfo.get_or_none(
            PersonInfo.platform.startswith(target_platform), PersonInfo.nickname == identifier
        )
        if person and person.user_id:
            logger.info(f"成功将 nickname '{identifier}' 解析为真实 UID: {person.user_id}")
            return person.user_id

        # 3. 大小写不敏感的模糊匹配（适用于 Discord 等大小写敏感的平台昵称）
        try:
            from src.common.database.database_model import _peewee
            fn = _peewee.fn
            person = PersonInfo.get_or_none(
                PersonInfo.platform.startswith(target_platform),
                fn.LOWER(PersonInfo.nickname) == identifier.lower()
            )
            if person and person.user_id:
                logger.info(f"成功将 nickname '{identifier}'（大小写不敏感）解析为真实 UID: {person.user_id}")
                return person.user_id
        except Exception as e:
            logger.debug(f"大小写不敏感匹配时出错: {e}")

        # 如果都没找到，仍然返回原输入
        # （可能会导致后续查不到已有绑定或绑定失败，属于预期内的校验拒绝）
        logger.warning(f"无法将标识符 '{identifier}' 解析为平台 '{target_platform}' 的真实 UID，将原样使用")
        return identifier

    def generate_auth_code(self, target_platform: str) -> str:
        """生成 <平台>-<5位数字> 格式的验证码"""
        digits = "".join(random.choices("0123456789", k=5))
        # 平台名首字母大写，例如 Discord-12345
        platform_display = target_platform.capitalize()
        return f"{platform_display}-{digits}"

    @staticmethod
    def _get_merged_row_for_person(person_id: str):
        """查找某个 person_id 所在的聚合行，返回 PersonBinding 或 None"""
        # 先判断当前 person_id 是否已经直接指向聚合行
        merged = PersonBinding.get_or_none(
            PersonBinding.platform == MERGED_PLATFORM, PersonBinding.person_id == person_id
        )
        if merged:
            return merged

        # 再从所有聚合行中搜索 merged_ids 是否包含该 person_id
        for row in PersonBinding.select().where(PersonBinding.platform == MERGED_PLATFORM):
            if row.merged_ids:
                ids = [x.strip() for x in row.merged_ids.split(",")]
                if person_id in ids:
                    return row
        return None

    @staticmethod
    def _collect_all_memories(person_ids: list[str]) -> list:
        """从多个 PersonInfo 中收集并合并去重记忆点"""
        all_memories = []
        for pid in person_ids:
            info = PersonInfo.get_or_none(PersonInfo.person_id == pid)
            if info and info.memory_points:
                try:
                    memories = json.loads(info.memory_points)
                    if isinstance(memories, list):
                        all_memories.extend(memories)
                except (json.JSONDecodeError, TypeError):
                    pass
        # 去重并去除 None
        return list(set(m for m in all_memories if m is not None))

    @staticmethod
    def _get_primary_info(person_ids: list[str]):
        """在多个候选 PersonInfo 中选择数据最丰富的作为 '基底' 信息"""
        best = None
        best_score = -1
        for pid in person_ids:
            info = PersonInfo.get_or_none(PersonInfo.person_id == pid)
            if not info:
                continue
            # 评估丰富程度：已认识 > 对话次数 > 记忆点数量
            score = 0
            if info.is_known:
                score += 10000
            score += (info.know_times or 0) * 10
            if info.memory_points:
                try:
                    pts = json.loads(info.memory_points)
                    score += len(pts) if isinstance(pts, list) else 0
                except Exception:
                    pass
            if score > best_score:
                best_score = score
                best = info
        return best

    # ──────────────────────────────────────────────────────────────
    #  聚合行管理
    # ──────────────────────────────────────────────────────────────

    def _build_merged_group(self, member_original_ids: list[str]) -> str:
        """
        创建或更新聚合行。

        Args:
            member_original_ids: 所有成员的原始 person_id 列表

        Returns:
            聚合后的统一 person_id
        """
        group_id = self._compute_group_id(member_original_ids)
        merged_memories = self._collect_all_memories(member_original_ids)
        merged_ids_str = ",".join(sorted(member_original_ids))
        merged_memory_json = json.dumps([m for m in merged_memories if m], ensure_ascii=False)

        # 选取基底 PersonInfo 的 know_times 累加
        total_know_times = 0
        for pid in member_original_ids:
            info = PersonInfo.get_or_none(PersonInfo.person_id == pid)
            if info:
                total_know_times += info.know_times or 0

        # 创建或更新聚合行
        merged_row, created = PersonBinding.get_or_create(
            platform=MERGED_PLATFORM,
            platform_user_id=group_id,
            defaults={
                "person_id": group_id,
                "merged_ids": merged_ids_str,
                "merged_memory": merged_memory_json,
            },
        )
        if not created:
            merged_row.person_id = group_id
            merged_row.merged_ids = merged_ids_str
            merged_row.merged_memory = merged_memory_json
            merged_row.save()

        # 将所有成员的映射行 person_id 统一指向 group_id
        for pid in member_original_ids:
            PersonBinding.update(person_id=group_id).where(
                PersonBinding.person_id == pid, PersonBinding.platform != MERGED_PLATFORM
            ).execute()

        logger.info(
            f"聚合行已{'创建' if created else '更新'}: group_id={group_id}, "
            f"成员={merged_ids_str}, 记忆点={len(merged_memories)}"
        )
        return group_id

    def _dissolve_merged_group(self, merged_row) -> list[str]:
        """
        拆解聚合组，恢复各账号独立状态。
        解绑时将聚合记忆复制保存进各个平台的 PersonInfo。

        Args:
            merged_row: 聚合行 PersonBinding 对象

        Returns:
            被解绑的所有原始 person_id 列表
        """
        if not merged_row or not merged_row.merged_ids:
            return []

        original_ids = [x.strip() for x in merged_row.merged_ids.split(",")]
        group_id = merged_row.person_id

        # 1. 将聚合期间的记忆复制到每个成员的 PersonInfo 中
        if merged_row.merged_memory:
            try:
                merged_memories = json.loads(merged_row.merged_memory)
                if isinstance(merged_memories, list):
                    for pid in original_ids:
                        info = PersonInfo.get_or_none(PersonInfo.person_id == pid)
                        if info:
                            # 将聚合记忆与该账号原始记忆合并去重
                            original_memories = []
                            if info.memory_points:
                                try:
                                    original_memories = json.loads(info.memory_points)
                                except Exception:
                                    pass
                            combined = list(set((m for m in (original_memories + merged_memories) if m is not None)))
                            info.memory_points = json.dumps(combined, ensure_ascii=False)
                            info.save()
                            logger.info(
                                f"已将聚合记忆复制到 {pid}，原始记忆={len(original_memories)}，合并后={len(combined)}"
                            )
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"解析聚合记忆时出错: {e}")

        # 2. 恢复各映射行的 person_id 为原始值
        for pid in original_ids:
            # 查找属于该成员的映射行（通过 platform + platform_user_id 反查）
            # 由于映射行目前的 person_id 都是 group_id，需要通过原始 ID 反推 platform 信息
            # 原始 person_id = MD5(platform_user_id)，但我们不能反推
            # 所以我们遍历所有指向 group_id 的映射行，逐一恢复
            pass  # 统一在下面处理

        # 遍历所有指向 group_id 的普通映射行，恢复其原始 person_id
        mapping_rows = list(
            PersonBinding.select().where(PersonBinding.person_id == group_id, PersonBinding.platform != MERGED_PLATFORM)
        )
        for row in mapping_rows:
            original_pid = self._compute_original_person_id(row.platform, row.platform_user_id)
            row.person_id = original_pid
            row.save()
            logger.info(f"已恢复映射: {row.platform}:{row.platform_user_id} -> {original_pid}")

        # 3. 删除聚合行
        merged_row.delete_instance()
        logger.info(f"已删除聚合行: group_id={group_id}")

        return original_ids

    # ──────────────────────────────────────────────────────────────
    #  公开 API：发起绑定 / 确认绑定 / 管理员解绑
    # ──────────────────────────────────────────────────────────────

    def request_bind(self, requester_person_id: str, target_platform: str, target_user_id: str) -> str:
        """
        发起绑定请求
        返回: 验证码(auth_code) 或 错误信息字符串
        """
        target_platform = target_platform.lower()
        target_user_id = str(target_user_id)

        # 解析目标 user_id（支持通过 person_name 识别）
        target_user_id = self._resolve_user_id(target_platform, target_user_id)

        # 1. 检查是否已绑定（灵活匹配平台名，如 bilibili 匹配 bilibili.live）
        existing_binding = self._find_binding_flexible(target_platform, target_user_id)

        if existing_binding and existing_binding.person_id == requester_person_id:
            return "ERR_ALREADY_BOUND"

        # 2. 平台冲突检测：合并后每个平台只能有一个账号
        # 收集请求者当前的所有平台
        requester_bindings = PersonBinding.select(PersonBinding.platform).where(
            PersonBinding.person_id == requester_person_id, PersonBinding.platform != MERGED_PLATFORM
        )
        requester_platforms = {b.platform for b in requester_bindings}

        # 收集目标账号当前的所有平台
        if existing_binding:
            target_bindings = PersonBinding.select(PersonBinding.platform).where(
                PersonBinding.person_id == existing_binding.person_id, PersonBinding.platform != MERGED_PLATFORM
            )
            target_platforms = {b.platform for b in target_bindings}
        else:
            target_platforms = {target_platform}

        overlap = requester_platforms.intersection(target_platforms)
        if overlap:
            return "ERR_PLATFORM_CONFLICT"

        # 3. 清理过期验证码并在数据库中生成新记录
        current_time = time.time()
        BindRequest.delete().where(BindRequest.expire_time < current_time).execute()

        auth_code = self.generate_auth_code(target_platform)

        BindRequest.create(
            auth_code=auth_code.lower(),
            req_person_id=requester_person_id,
            target_platform=target_platform.lower(),
            target_user_id=str(target_user_id),
            expire_time=current_time + self.expire_time,
            display_code=auth_code,
        )
        logger.info(f"用户 {requester_person_id} 发起绑定 {target_platform}:{target_user_id}，验证码: {auth_code}")
        return auth_code

    def confirm_bind(self, submitter_platform: str, submitter_user_id: str, auth_code: str) -> tuple[bool, str]:
        """
        确认绑定（用户在目标平台发送验证码）

        核心原则：不修改任何 PersonInfo 行，仅操作 PersonBinding 表。
        """
        auth_code_lower = auth_code.strip().lower()

        import re
        if not re.match(r"^[a-z0-9_.]+-\d{5}$", auth_code_lower):
            return False, ""

        bind_info = BindRequest.get_or_none(BindRequest.auth_code == auth_code_lower)

        if not bind_info:
            return False, "验证码无效或已过期。"

        # 校验：提交验证码的平台和账号，必须与请求时填写的目标一致
        if not submitter_platform.lower().startswith(bind_info.target_platform) or bind_info.target_user_id != str(
            submitter_user_id
        ):
            return False, "验证码不属于当前平台账号，请使用发起绑定时指定的账号发送。"

        if time.time() > bind_info.expire_time:
            bind_info.delete_instance()
            return False, "验证码已过期，请重新发起绑定。"

        requester_person_id = bind_info.req_person_id
        submitter_platform = submitter_platform.lower()
        submitter_user_id = str(submitter_user_id)

        try:
            with db.atomic():
                # 1. 确定提交者的原始 person_id
                submitter_original_pid = self._compute_original_person_id(submitter_platform, submitter_user_id)

                # 2. 确保提交者在 PersonBinding 中有映射行
                PersonBinding.get_or_create(
                    platform=submitter_platform,
                    platform_user_id=submitter_user_id,
                    defaults={"person_id": submitter_original_pid},
                )

                # 3. 确定请求者的原始 person_id
                # 请求者的 person_id 可能是其原始 ID 或已有聚合组的 group_id
                # 查找请求者对应的映射行以确定原始 ID
                requester_mapping = PersonBinding.get_or_none(
                    PersonBinding.person_id == requester_person_id, PersonBinding.platform != MERGED_PLATFORM
                )

                # 4. 收集所有需要聚合的原始 person_id
                all_original_ids = set()
                old_group_ids = set()  # 记录需要清理的旧 group_id

                # 如果请求者已经在某个聚合组中，收集组内所有成员
                existing_merged = self._get_merged_row_for_person(requester_person_id)
                if existing_merged and existing_merged.merged_ids:
                    old_group_ids.add(existing_merged.person_id)
                    for mid in existing_merged.merged_ids.split(","):
                        all_original_ids.add(mid.strip())
                    # 删除旧的聚合行，准备创建新的
                    existing_merged.delete_instance()
                else:
                    # 请求者可能自身就是原始 ID
                    if requester_mapping:
                        orig_pid = self._compute_original_person_id(
                            requester_mapping.platform, requester_mapping.platform_user_id
                        )
                        all_original_ids.add(orig_pid)
                    else:
                        # 请求者没有映射行，把当前 person_id 当作原始 ID
                        all_original_ids.add(requester_person_id)

                # 如果提交者也已经在某个聚合组中，也收集其组内成员
                submitter_merged = self._get_merged_row_for_person(submitter_original_pid)
                if submitter_merged and submitter_merged.merged_ids:
                    old_group_ids.add(submitter_merged.person_id)
                    for mid in submitter_merged.merged_ids.split(","):
                        all_original_ids.add(mid.strip())
                    submitter_merged.delete_instance()
                else:
                    all_original_ids.add(submitter_original_pid)

                # 4.5 恢复旧聚合组成员的映射行到原始 person_id
                # 删除旧聚合行后，成员的映射行 person_id 仍指向旧 group_id，
                # 需要先恢复为原始值，_build_merged_group 才能正确匹配并更新它们
                for old_gid in old_group_ids:
                    orphaned_rows = list(
                        PersonBinding.select().where(
                            PersonBinding.person_id == old_gid,
                            PersonBinding.platform != MERGED_PLATFORM,
                        )
                    )
                    for row in orphaned_rows:
                        original_pid = self._compute_original_person_id(row.platform, row.platform_user_id)
                        row.person_id = original_pid
                        row.save()
                        logger.debug(f"恢复映射行: {row.platform}:{row.platform_user_id} -> {original_pid}")

                all_original_ids = list(all_original_ids)

                # 5. 构建新的聚合组
                group_id = self._build_merged_group(all_original_ids)

            bind_info.delete_instance()
            logger.info(f"账号绑定成功: {submitter_platform}:{submitter_user_id} -> group={group_id}")
            return True, "账号绑定成功！多个平台的数据与记忆已智能互通。"

        except Exception as e:
            logger.error(f"绑定账号时发生数据库错误: {e}")
            return False, "系统错误，绑定失败。"

    def admin_unbind(self, target_platform: str, target_user_id: str) -> tuple[bool, str]:
        """
        管理员解绑指定账号所在的所有关联身份。

        核心原则：
        - 将聚合记忆复制回各个平台的 PersonInfo
        - 删除聚合行，恢复各映射行的原始 person_id
        - 各账号立即回归独立状态
        """
        target_platform = target_platform.lower()

        # 解析目标 user_id（支持通过 person_name 识别）
        target_user_id = self._resolve_user_id(target_platform, target_user_id)

        # 灵活匹配平台名（如 bilibili 匹配 bilibili.live）
        binding = self._find_binding_flexible(target_platform, target_user_id)
        if not binding:
            return False, f"未找到 {target_platform}:{target_user_id} 的绑定记录。"

        group_id = binding.person_id

        # 查找聚合行
        merged_row = PersonBinding.get_or_none(
            PersonBinding.platform == MERGED_PLATFORM, PersonBinding.person_id == group_id
        )
        if not merged_row:
            return False, "该账号未与其他账号跨平台绑定，无需解绑。"

        try:
            with db.atomic():
                dissolved_ids = self._dissolve_merged_group(merged_row)

            platforms_info = []
            for did in dissolved_ids:
                # 查找恢复后的映射行信息
                restored = PersonBinding.get_or_none(PersonBinding.person_id == did)
                if restored:
                    platforms_info.append(f"{restored.platform}:{restored.platform_user_id}")
                else:
                    platforms_info.append(f"id:{did[:8]}")

            logger.info(f"账号解绑成功: {', '.join(platforms_info)}")
            return True, (
                f"成功拆分绑定！涉及账号：{', '.join(platforms_info)}。\n"
                f"绑定期间的记忆已复制保存到各个平台账号中，各账号已恢复独立状态。"
            )

        except Exception as e:
            logger.error(f"拆分解绑失败: {e}")
            return False, "系统错误，拆分解绑失败。"

    # ──────────────────────────────────────────────────────────────
    #  针对 Person 类的查询接口
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_merged_memory_for_person(person_id: str) -> list | None:
        """
        查询某个 person_id 是否属于某个绑定组，如果是则返回聚合记忆列表。
        如果不属于任何绑定组，返回 None。

        供 Person.load_from_database() 使用。
        """
        # 查找该 person_id 所在的聚合行
        merged_row = PersonBinding.get_or_none(
            PersonBinding.platform == MERGED_PLATFORM, PersonBinding.person_id == person_id
        )
        if not merged_row or not merged_row.merged_memory:
            return None

        try:
            memories = json.loads(merged_row.merged_memory)
            if isinstance(memories, list):
                return [m for m in memories if m is not None]
        except (json.JSONDecodeError, TypeError):
            pass

        return None

    @staticmethod
    def update_merged_memory(person_id: str, memory_points: list) -> bool:
        """
        更新聚合行的记忆数据。
        如果该 person_id 属于某个绑定组，将记忆写入聚合行而非原始 PersonInfo。

        供 Person.sync_to_database() 使用。

        Returns:
            True 如果成功更新了聚合行，False 表示未找到聚合行（应写入原始 PersonInfo）
        """
        merged_row = PersonBinding.get_or_none(
            PersonBinding.platform == MERGED_PLATFORM, PersonBinding.person_id == person_id
        )
        if not merged_row:
            return False

        merged_row.merged_memory = json.dumps([m for m in memory_points if m is not None], ensure_ascii=False)
        merged_row.save()
        return True


bind_manager = BindManager()
