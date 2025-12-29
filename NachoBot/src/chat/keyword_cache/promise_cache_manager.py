import json
import os
import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

from src.config.config import global_config
from src.common.logger import get_logger
from src.common.data_models.database_data_model import DatabaseMessages
from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat

if TYPE_CHECKING:
    from src.chat.message_receive.message import MessageRecv


class PromiseCacheManager:
    def __init__(self) -> None:
        self._logger = get_logger("promise_cache")
        self._active_captures: Dict[Tuple[str, str], dict] = {}
        self._repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

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
            existing = self._load_latest_cache(chat_id, kw)
            if existing:
                formatted = self._format_cache(kw, existing)
                if formatted:
                    hits.append(formatted)
            self._start_new_capture(chat_id, kw, message)
        return hits

    def collect_snippets_for_messages(
        self, chat_id: str, messages: Iterable[DatabaseMessages]
    ) -> List[str]:
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
            cache = self._load_latest_cache(chat_id, kw)
            if cache:
                formatted = self._format_cache(kw, cache)
                if formatted:
                    snippets.append(formatted)
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

        cache_dir = self._get_keyword_dir(chat_id, keyword)
        os.makedirs(cache_dir, exist_ok=True)
        date_str = time.strftime("%Y%m%d")
        session_code = self._get_session_identifier(message)
        file_path = os.path.join(
            cache_dir, f"{date_str}_{session_code}_{int(time.time())}.json"
        )
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

    def _get_keyword_dir(self, chat_id: str, keyword: str) -> str:
        cfg = global_config.promise_cache
        base_dir = cfg.cache_dir
        cache_root = base_dir if os.path.isabs(base_dir) else os.path.abspath(os.path.join(self._repo_root, base_dir))
        safe_keyword = re.sub(r'[<>:"/\\|?*]+', "_", keyword).strip() or "keyword"
        return os.path.join(cache_root, chat_id, safe_keyword)

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

    def _trim_old_caches(self, cache_dir: str, max_keep: int) -> None:
        try:
            files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
            if len(files) <= max_keep:
                return
            files = sorted(files, key=lambda name: os.path.getmtime(os.path.join(cache_dir, name)))
            for old in files[:-max_keep]:
                try:
                    os.remove(os.path.join(cache_dir, old))
                except Exception:
                    continue
        except Exception as exc:
            self._logger.debug(f"清理旧约定缓存失败: {exc}", exc_info=True)

    def _load_latest_cache(self, chat_id: str, keyword: str) -> Optional[dict]:
        cache_dir = self._get_keyword_dir(chat_id, keyword)
        if not os.path.isdir(cache_dir):
            return None
        try:
            files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
            if not files:
                return None
            files = sorted(files, key=lambda name: os.path.getmtime(os.path.join(cache_dir, name)), reverse=True)
            cache_path = os.path.join(cache_dir, files[0])
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["file_path"] = cache_path
                return data
        except Exception as exc:
            self._logger.warning(f"读取约定缓存失败: {exc}", exc_info=True)
            return None

    def _format_cache(self, keyword: str, cache: dict) -> str:
        records = cache.get("records") or []
        if not records:
            return ""
        created_at = cache.get("created_at") or time.time()
        header = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
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
