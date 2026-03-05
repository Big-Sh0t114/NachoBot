import asyncio
import json
import os
import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.common.data_models.database_data_model import DatabaseMessages
from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat
from src.llm_models.utils_model import LLMRequest

if TYPE_CHECKING:
    from src.chat.message_receive.message import MessageRecv


class PromiseCacheManager:
    def __init__(self) -> None:
        self._logger = get_logger("promise_cache")
        self._active_captures: Dict[Tuple[str, str], dict] = {}
        self._repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        self._last_activity_ts = 0.0
        self._idle_task: Optional[asyncio.Task] = None
        self._idle_seconds = 180
        self._scan_lock: Optional[asyncio.Lock] = None
        self._summary_model = LLMRequest(
            model_set=model_config.model_task_config.replyer,
            request_type="promise_cache_summary",
        )

    def touch_activity(self) -> None:
        cfg = global_config.promise_cache
        if not (cfg.enable and cfg.keywords):
            return
        self._last_activity_ts = time.time()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = loop.create_task(self._idle_wait_then_scan())

    async def _idle_wait_then_scan(self) -> None:
        try:
            await asyncio.sleep(self._idle_seconds)
            if time.time() - self._last_activity_ts < self._idle_seconds:
                return
            await self._scan_and_summarize_all()
        except asyncio.CancelledError:
            return

    def handle_message(self, message: "MessageRecv") -> List[str]:
        """处理单条消息：写入/更新缓存，并返回已命中的缓存片段（若存在历史约定）。"""
        cfg = global_config.promise_cache
        if not (cfg.enable and cfg.keywords):
            return []

        # 群聊不启用誓言/约定缓存
        if getattr(message.message_info, "group_info", None):
            return []

        chat_id = getattr(message.chat_stream, "stream_id", "") if hasattr(message, "chat_stream") else ""
        if not chat_id:
            return []

        record = self._message_to_record(message)
        if record:
            self._append_to_active_captures(chat_id, record)

        matched_keywords = self._match_keywords(getattr(message, "processed_plain_text", "") or "")
        if not matched_keywords:
            return []

        hits: List[str] = []
        for kw in matched_keywords:
            caches = self._load_caches(chat_id, kw)
            for cache in caches:
                formatted = self._format_cache(kw, cache)
                if formatted:
                    hits.append(formatted)
                    self._logger.info(
                        f"[promise_cache] 注入片段 chat={chat_id} kw={kw} file={cache.get('file_path', '')}"
                    )
            self._start_new_capture(chat_id, kw, message)
        return hits

    def collect_snippets_for_messages(self, chat_id: str, messages: Iterable[DatabaseMessages]) -> List[str]:
        """根据消息列表检测关键词并返回对应缓存片段，用于构建回复上下文。"""
        cfg = global_config.promise_cache
        if not (cfg.enable and cfg.keywords):
            return []

        keywords_to_fetch: Set[str] = set()
        for msg in messages:
            if getattr(msg, "chat_id", None) and getattr(msg, "chat_id") != chat_id:
                continue  # 只处理当前会话，避免串流
            text = self._extract_text(msg)
            keywords_to_fetch.update(self._match_keywords(text))

        snippets: List[str] = []
        for kw in keywords_to_fetch:
            caches = self._load_caches(chat_id, kw)
            for cache in caches:
                formatted = self._format_cache(kw, cache)
                if formatted:
                    snippets.append(formatted)
                    self._logger.info(
                        f"[promise_cache] 注入片段 chat={chat_id} kw={kw} file={cache.get('file_path', '')}"
                    )
        return snippets

    def _append_to_active_captures(self, chat_id: str, record: dict) -> None:
        cfg = global_config.promise_cache
        if cfg.post_context_size <= 0:
            return

        to_remove: List[Tuple[str, str]] = []
        for (cid, kw), capture in list(self._active_captures.items()):
            if cid != chat_id:
                continue
            if capture.get("remaining_after", 0) <= 0:
                to_remove.append((cid, kw))
                continue

            msg_id = record.get("message_id")
            seen_ids: Set[str] = capture.setdefault("seen_ids", set())
            if msg_id and msg_id in seen_ids:
                continue

            capture["records"].append(record)
            if msg_id:
                seen_ids.add(msg_id)
            capture["remaining_after"] = max(0, capture.get("remaining_after", 0) - 1)
            capture["completed"] = capture["remaining_after"] == 0
            self._persist_capture(capture)
            if capture["completed"]:
                to_remove.append((cid, kw))

        for key in to_remove:
            self._active_captures.pop(key, None)

    def _start_new_capture(self, chat_id: str, keyword: str, message: "MessageRecv") -> None:
        cfg = global_config.promise_cache
        active_key = (chat_id, keyword)
        if active_key in self._active_captures:
            self._active_captures.pop(active_key, None)

        records: List[dict] = []
        try:
            before_limit = cfg.context_size if cfg.context_size > 0 else 0
            before_msgs = get_raw_msg_before_timestamp_with_chat(
                chat_id=chat_id,
                timestamp=float(getattr(message.message_info, "time", time.time())),
                limit=before_limit,
            )
            for msg in before_msgs:
                rec = self._message_to_record(msg)
                if rec:
                    records.append(rec)
        except Exception as exc:
            self._logger.debug(f"加载历史上下文失败: {exc}", exc_info=True)

        current_record = self._message_to_record(message)
        if current_record:
            records.append(current_record)

        cache_dir = self._get_keyword_dir(chat_id, keyword, "raw")
        os.makedirs(cache_dir, exist_ok=True)
        date_str = time.strftime("%Y%m%d")
        session_code = self._get_session_identifier(message)
        file_path = os.path.join(cache_dir, f"{date_str}_{session_code}_{int(time.time())}.json")
        capture = {
            "chat_id": chat_id,
            "keyword": keyword,
            "records": records,
            "remaining_after": cfg.post_context_size,
            "file_path": file_path,
            "created_at": time.time(),
            "context_size": cfg.context_size,
            "post_context_size": cfg.post_context_size,
            "completed": cfg.post_context_size == 0,
            "seen_ids": {rec.get("message_id") for rec in records if rec.get("message_id")},
        }
        self._persist_capture(capture)
        self._trim_old_caches(cache_dir, cfg.max_cache_per_keyword)
        if capture["remaining_after"] > 0:
            self._active_captures[active_key] = capture

    def _message_to_record(self, message) -> Optional[dict]:
        try:
            if hasattr(message, "message_info"):
                info = message.message_info
                user_info = info.user_info
                group_info = info.group_info
                return {
                    "time": float(getattr(info, "time", time.time())),
                    "user_id": str(getattr(user_info, "user_id", "")) if user_info else "",
                    "user_nickname": getattr(user_info, "user_nickname", "") if user_info else "",
                    "platform": getattr(user_info, "platform", "") if user_info else "",
                    "group_id": getattr(group_info, "group_id", "") if group_info else "",
                    "content": getattr(message, "processed_plain_text", "") or "",
                    "message_id": getattr(info, "message_id", "") or getattr(message, "message_id", ""),
                }
            if isinstance(message, DatabaseMessages):
                user_info = getattr(message, "user_info", None)
                group_info = getattr(message, "group_info", None)
                return {
                    "time": float(getattr(message, "time", time.time())),
                    "user_id": str(getattr(user_info, "user_id", getattr(message, "user_id", "")))
                    if user_info
                    else str(getattr(message, "user_id", "")),
                    "user_nickname": getattr(user_info, "user_nickname", getattr(message, "user_nickname", ""))
                    if user_info
                    else getattr(message, "user_nickname", ""),
                    "platform": getattr(user_info, "platform", getattr(message, "user_platform", ""))
                    if user_info
                    else getattr(message, "user_platform", ""),
                    "group_id": getattr(group_info, "group_id", getattr(message, "chat_id", ""))
                    if group_info
                    else getattr(message, "chat_id", ""),
                    "content": getattr(message, "processed_plain_text", "")
                    or getattr(message, "display_message", "")
                    or "",
                    "message_id": getattr(message, "message_id", ""),
                }
        except Exception as exc:
            self._logger.debug(f"标准化消息失败: {exc}", exc_info=True)
        return None

    def _match_keywords(self, text: str) -> Set[str]:
        cfg = global_config.promise_cache
        if not text:
            return set()
        target = text if cfg.case_sensitive else text.lower()
        matched: Set[str] = set()
        for kw in cfg.keywords:
            if not kw:
                continue
            check_kw = kw if cfg.case_sensitive else kw.lower()
            if check_kw in target:
                matched.add(kw)
        return matched

    def _extract_text(self, message: DatabaseMessages) -> str:
        return (getattr(message, "processed_plain_text", "") or getattr(message, "display_message", "") or "").strip()

    def _get_cache_root(self) -> str:
        cfg = global_config.promise_cache
        base_dir = cfg.cache_dir
        if os.path.isabs(base_dir):
            return base_dir
        return os.path.abspath(os.path.join(self._repo_root, base_dir))

    def _get_keyword_dir(self, chat_id: str, keyword: str, bucket: Optional[str] = None) -> str:
        cache_root = self._get_cache_root()
        safe_keyword = re.sub(r'[<>:"/\\|?*]+', "_", keyword).strip() or "keyword"
        base_dir = os.path.join(cache_root, chat_id, safe_keyword)
        if bucket:
            return os.path.join(base_dir, bucket)
        return base_dir

    def _get_session_identifier(self, message: "MessageRecv") -> str:
        try:
            if message.message_info.group_info:
                session_id = str(message.message_info.group_info.group_id)
            else:
                session_id = str(message.message_info.user_info.user_id)
        except Exception:
            session_id = "unknown"
        return re.sub(r"[^0-9A-Za-z_-]+", "_", session_id)

    def _persist_capture(self, capture: dict) -> None:
        file_path = capture.get("file_path")
        if not file_path:
            return
        data = {
            "chat_id": capture.get("chat_id", ""),
            "keyword": capture.get("keyword", ""),
            "created_at": capture.get("created_at", time.time()),
            "context_size": capture.get("context_size", 0),
            "post_context_size": capture.get("post_context_size", 0),
            "completed": capture.get("completed", False),
            "records": capture.get("records", []),
        }
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._logger.warning(f"写入约定缓存失败: {exc}", exc_info=True)

    def _persist_processed_cache(self, processed_path: str, raw_data: dict, summary: str, raw_path: str) -> None:
        data = {
            "chat_id": raw_data.get("chat_id", ""),
            "keyword": raw_data.get("keyword", ""),
            "created_at": raw_data.get("created_at", time.time()),
            "processed_at": time.time(),
            "summary": summary.strip(),
            "raw_file": os.path.basename(raw_path),
        }
        try:
            os.makedirs(os.path.dirname(processed_path), exist_ok=True)
            with open(processed_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._logger.warning(f"写入约定缓存摘要失败: {exc}", exc_info=True)

    def _trim_old_caches(self, cache_dir: str, max_keep: int) -> None:
        try:
            files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
            if len(files) <= max_keep:
                return
            files = sorted(files, key=lambda name: os.path.getmtime(os.path.join(cache_dir, name)))
            for old in files[:-max_keep]:
                raw_path = os.path.join(cache_dir, old)
                try:
                    os.remove(raw_path)
                except Exception:
                    continue
                processed_path = self._raw_to_processed_path(raw_path)
                if processed_path and os.path.exists(processed_path):
                    try:
                        os.remove(processed_path)
                    except Exception:
                        continue
        except Exception as exc:
            self._logger.debug(f"清理旧约定缓存失败: {exc}", exc_info=True)

    def _raw_to_processed_path(self, raw_path: str) -> Optional[str]:
        raw_dir = os.path.dirname(raw_path)
        if os.path.basename(raw_dir) != "raw":
            return None
        base_dir = os.path.dirname(raw_dir)
        return os.path.join(base_dir, "processed", os.path.basename(raw_path))

    def _load_latest_cache(self, chat_id: str, keyword: str, cache_type: str = "processed") -> Optional[dict]:
        caches = self._load_caches(chat_id, keyword, limit=1, cache_type=cache_type)
        return caches[0] if caches else None

    def _load_caches(
        self, chat_id: str, keyword: str, limit: Optional[int] = None, cache_type: str = "processed"
    ) -> List[dict]:
        cache_dir = self._get_keyword_dir(chat_id, keyword, cache_type)
        if not os.path.isdir(cache_dir):
            return []
        try:
            files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
            if not files:
                return []
            files = sorted(files, key=lambda name: os.path.getmtime(os.path.join(cache_dir, name)), reverse=True)
            if limit and limit > 0:
                files = files[:limit]

            caches: List[dict] = []
            for name in files:
                cache_path = os.path.join(cache_dir, name)
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        data["file_path"] = cache_path
                        caches.append(data)
                except Exception as exc:
                    self._logger.warning(f"读取约定缓存失败: {exc}", exc_info=True)
            return caches
        except Exception as exc:
            self._logger.warning(f"读取约定缓存失败: {exc}", exc_info=True)
            return []

    def _iter_raw_cache_files(self) -> Iterable[str]:
        cache_root = self._get_cache_root()
        if not os.path.isdir(cache_root):
            return
        for root, _, files in os.walk(cache_root):
            if os.path.basename(root) != "raw":
                continue
            for name in files:
                if name.endswith(".json"):
                    yield os.path.join(root, name)

    def _load_raw_cache(self, raw_path: str) -> Optional[dict]:
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["file_path"] = raw_path
                return data
        except Exception as exc:
            self._logger.warning(f"读取约定缓存失败: {exc}", exc_info=True)
            return None

    def _build_transcript(self, records: List[dict]) -> str:
        lines = []
        for rec in records:
            name = rec.get("user_nickname") or rec.get("user_id") or ""
            content = rec.get("content") or ""
            if not content:
                continue
            if name:
                lines.append(f"{name}: {content}")
            else:
                lines.append(content)
        return "\n".join(lines)

    async def _summarize_raw_cache(self, keyword: str, raw_data: dict) -> str:
        records = raw_data.get("records") or []
        if not records:
            return ""
        transcript = self._build_transcript(records)
        if not transcript:
            return ""
        prompt = (
            "你是对话摘要助手。请根据以下对话记录，提炼与关键词相关的约定/承诺/共识/限制。\n"
            f"关键词：{keyword}\n"
            "输出要求：\n"
            "1) 使用简洁中文，3-6条要点，逐行列出。\n"
            "2) 只保留事实与约定，不要解释过程，不要加入臆测。\n"
            "3) 不要包含说话人或时间戳。\n"
            "对话记录：\n"
            f"{transcript}\n"
        )
        try:
            content, _ = await self._summary_model.generate_response_async(prompt)
        except Exception as exc:
            self._logger.warning(f"[promise_cache] 生成摘要失败: {exc}", exc_info=True)
            return ""
        return (content or "").strip()

    async def _scan_and_summarize_all(self) -> None:
        cfg = global_config.promise_cache
        if not (cfg.enable and cfg.keywords):
            return
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        async with self._scan_lock:
            if time.time() - self._last_activity_ts < self._idle_seconds:
                return
            raw_files = list(self._iter_raw_cache_files())
            if not raw_files:
                return
            raw_files.sort(key=lambda path: os.path.getmtime(path))
            self._logger.info(f"[promise_cache] 空闲{self._idle_seconds}s，开始整理{len(raw_files)}条raw缓存")
            for raw_path in raw_files:
                if time.time() - self._last_activity_ts < self._idle_seconds:
                    self._logger.info("[promise_cache] 收到新消息，停止整理")
                    return
                processed_path = self._raw_to_processed_path(raw_path)
                if not processed_path:
                    continue
                if os.path.exists(processed_path):
                    continue
                raw_data = self._load_raw_cache(raw_path)
                if not raw_data:
                    continue
                if not raw_data.get("completed", True):
                    continue
                summary = await self._summarize_raw_cache(raw_data.get("keyword", ""), raw_data)
                if not summary:
                    continue
                self._persist_processed_cache(processed_path, raw_data, summary, raw_path)
                self._logger.info(f"[promise_cache] 写入摘要 file={processed_path}")

    def _format_cache(self, keyword: str, cache: dict) -> str:
        summary = (cache.get("summary") or "").strip()
        created_at = cache.get("processed_at") or cache.get("created_at") or time.time()
        header = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        if summary:
            return "\n".join([f"[关键词:{keyword}] 摘要于 {header}", summary]).strip()
        records = cache.get("records") or []
        if not records:
            return ""
        lines = [f"[关键词:{keyword}] 缓存于 {header}"]
        for rec in records:
            ts = rec.get("time") or 0
            ts_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""
            name = rec.get("user_nickname") or rec.get("user_id") or ""
            content = rec.get("content") or ""
            lines.append(f"{ts_str} {name}: {content}")
        if not cache.get("completed", True):
            lines.append("(缓存仍在补充后续消息)")
        return "\n".join(lines)


promise_cache_manager = PromiseCacheManager()
