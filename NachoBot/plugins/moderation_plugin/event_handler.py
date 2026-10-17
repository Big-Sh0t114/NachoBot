import asyncio
import re
import os
import io
import base64
import json
import stat
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional, Tuple, Dict, Any, Mapping

from PIL import Image

from src.plugin_system.base import BaseEventHandler, EventType, NachoMessages, CustomEventHandlerResult
from src.common.logger import get_logger

logger = get_logger("moderation_plugin")


@dataclass(frozen=True, slots=True)
class _ImageFileEntry:
    signature: Tuple[int, int, int, int, int, int]
    dhash: int
    filename: str


@dataclass(frozen=True, slots=True)
class _ImageCache:
    directory: Optional[str]
    files: Mapping[str, _ImageFileEntry]
    hashes: Mapping[int, str]


_EMPTY_IMAGE_CACHE = _ImageCache(None, MappingProxyType({}), MappingProxyType({}))


def _file_signature(stat_result: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    """Return metadata that detects replacement and in-place file changes."""
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mode,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


class _ImageChangedDuringRead(OSError):
    """Raised when a rule file changes while its bytes are being read."""


def _read_stable_image_hash(path: str, expected_signature: Tuple[int, int, int, int, int, int]) -> int:
    """Hash one stable file snapshot, raising if the path changes during the read."""
    with open(path, "rb") as image_file:
        before = os.fstat(image_file.fileno())
        # On Windows, path stat and fstat can expose ctime differently; compare
        # the cross-source identity and mtime fields, then compare fstat to fstat.
        if (
            not stat.S_ISREG(before.st_mode)
            or _file_signature(before)[:5] != expected_signature[:5]
        ):
            raise _ImageChangedDuringRead(f"文件在读取前已发生变化: {path}")

        image_bytes = image_file.read()

        after = os.fstat(image_file.fileno())
        try:
            path_after = os.stat(path)
        except OSError as exc:
            raise _ImageChangedDuringRead(f"文件在读取期间已不可用: {path}") from exc
        if (
            not stat.S_ISREG(after.st_mode)
            or _file_signature(after) != _file_signature(before)
            or _file_signature(path_after) != expected_signature
        ):
            raise _ImageChangedDuringRead(f"文件在读取期间已发生变化: {path}")

    return _compute_dhash(image_bytes)


def _scan_banned_image_files(
    directory: str, previous_files: Mapping[str, _ImageFileEntry]
) -> Mapping[str, _ImageFileEntry]:
    """Build a local filesystem snapshot without mutating the published cache."""
    try:
        # os.walk follows a configured symlink root, while its default behavior
        # does not recurse through symlinked child directories.
        root_stat = os.stat(directory)
    except FileNotFoundError:
        # A missing root is a valid empty rule set; later calls will scan again.
        return MappingProxyType({})
    if not stat.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError(directory)

    files: Dict[str, _ImageFileEntry] = {}

    def walk(current_directory: str) -> None:
        before = os.stat(current_directory)
        if not stat.S_ISDIR(before.st_mode):
            raise NotADirectoryError(current_directory)
        with os.scandir(current_directory) as iterator:
            entries = sorted(
                ((entry, entry.is_symlink()) for entry in iterator),
                key=lambda item: item[0].name,
            )
        after = os.stat(current_directory)
        if _file_signature(before) != _file_signature(after):
            raise _ImageChangedDuringRead(f"目录在扫描期间已发生变化: {current_directory}")

        for entry, is_symlink in entries:
            entry_path = os.path.join(current_directory, entry.name)
            # Use os.stat for a consistent signature: Windows DirEntry.stat may
            # omit file identity fields that fstat reports for the opened handle.
            try:
                entry_stat = os.stat(entry_path)
            except FileNotFoundError as exc:
                if not is_symlink or not os.path.lexists(entry_path):
                    # The entry vanished during traversal; retain the prior
                    # publication and retry a coherent scan on the next message.
                    raise _ImageChangedDuringRead(f"文件在扫描期间已消失: {entry_path}") from exc
                logger.error(f"违规图片符号链接目标不可用，已跳过: {entry_path}")
                old_entry = previous_files.get(os.path.normcase(os.path.abspath(entry_path)))
                if old_entry is not None:
                    files[os.path.normcase(os.path.abspath(entry_path))] = old_entry
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                if is_symlink:
                    continue
                walk(entry_path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                continue

            cache_key = os.path.normcase(os.path.abspath(entry_path))
            signature = _file_signature(entry_stat)
            old_entry = previous_files.get(cache_key)
            if old_entry is not None and old_entry.signature == signature:
                files[cache_key] = old_entry
                continue

            try:
                image_hash = _read_stable_image_hash(entry_path, signature)
            except Exception as exc:
                logger.error(f"读取违规图片文件 {entry_path} 失败，将在后续图片消息中重试: {exc}")
                # Keep a known rule until a changed or unreadable file can be read.
                if old_entry is not None:
                    files[cache_key] = old_entry
                continue
            files[cache_key] = _ImageFileEntry(signature, image_hash, entry.name)

    walk(directory)
    return MappingProxyType(files)


def _make_image_cache(directory: str, files: Mapping[str, _ImageFileEntry]) -> _ImageCache:
    hashes: Dict[int, str] = {}
    for _, entry in sorted(files.items()):
        # Rebuild from per-path entries so duplicate hashes survive one file's removal.
        hashes.setdefault(entry.dhash, entry.filename)
    return _ImageCache(directory, MappingProxyType(dict(files)), MappingProxyType(hashes))

# ---------------------------------------------------------------------------
# 感知哈希 (dHash) —— 对 JPEG 重压缩 / WebP 转码 / 轻微缩放 有鲁棒性
# ---------------------------------------------------------------------------


def _compute_dhash(image_bytes: bytes, hash_size: int = 16) -> int:
    """计算 dHash (差异哈希)。

    将图片缩放到 (hash_size+1) x hash_size 的灰度图，
    比较每行相邻像素的亮度差来生成一个整型指纹。

    Args:
        image_bytes: 图片原始字节
        hash_size: 哈希尺寸，越大越精确但也越慢。默认 16 -> 256-bit 指纹。

    Returns:
        int: 哈希指纹整数
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    img = img.resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())

    hash_val = 0
    for row in range(hash_size):
        for col in range(hash_size):
            offset = row * (hash_size + 1) + col
            if pixels[offset] < pixels[offset + 1]:
                hash_val |= 1 << (row * hash_size + col)
    return hash_val


def _hamming_distance(h1: int, h2: int) -> int:
    """计算两个整型哈希的汉明距离（不同的 bit 数）"""
    return bin(h1 ^ h2).count("1")


# ---------------------------------------------------------------------------
# 事件处理器
# ---------------------------------------------------------------------------


class ModerationEventHandler(BaseEventHandler):
    event_type = EventType.ON_MESSAGE
    handler_name = "moderation_event_handler"
    handler_description = "处理消息的违规词正则匹配及图片感知哈希比对"
    weight = 999  # 设置高权重，在其他处理前拦截
    intercept_message = True

    # dHash 汉明距离阈值：<=threshold 视为同一张图
    # 256-bit dHash 下，<=10 基本就是同源图
    DHASH_THRESHOLD = 15

    def __init__(self):
        super().__init__()
        self._image_cache = _EMPTY_IMAGE_CACHE
        self._image_cache_lock = asyncio.Lock()
        
        self.stats_file = "data/moderation_stats.json"
        self._stats: Dict[str, Dict[str, Any]] = {
            "text_patterns": {},
            "images": {}
        }
        self._load_stats()

    def _load_stats(self):
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, "r", encoding="utf-8") as f:
                    self._stats = json.load(f)
        except Exception as e:
            logger.error(f"读取撤回统计数据失败: {e}")

    def _save_stats(self):
        try:
            os.makedirs(os.path.dirname(self.stats_file), exist_ok=True)
            with open(self.stats_file, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存撤回统计数据失败: {e}")

    def _update_stats(self, category: str, item_key: str, user_id: str):
        """更新统计信息
        category: 'text_patterns' 或 'images'
        item_key: 命中的正则 或 图片文件名
        user_id: 触发者的 QQ 号
        """
        if category not in self._stats:
            self._stats[category] = {}
            
        if item_key not in self._stats[category]:
            self._stats[category][item_key] = {
                "count": 0,
                "triggered_by": {}
            }
            
        record = self._stats[category][item_key]
        record["count"] += 1
        
        user_id_str = str(user_id) if user_id else "unknown"
        if user_id_str not in record["triggered_by"]:
            record["triggered_by"][user_id_str] = 0
        record["triggered_by"][user_id_str] += 1
        
        self._save_stats()

    @property
    def _banned_image_hashes(self) -> Mapping[int, str]:
        """当前完整发布的 dHash -> 文件名映射。"""
        return self._image_cache.hashes

    async def _refresh_banned_image_hashes(self, directory: str) -> Optional[_ImageCache]:
        """Refresh one directory under a lock and publish only complete snapshots."""
        try:
            directory_path = os.fspath(directory)
            if not isinstance(directory_path, str):
                raise TypeError("违规图片目录必须是字符串路径")
            normalized_directory = os.path.normcase(os.path.abspath(directory_path))
        except (TypeError, ValueError, OSError) as exc:
            logger.error(f"违规图片目录路径无效，无法刷新图片规则: {exc}")
            return None

        async with self._image_cache_lock:
            previous = self._image_cache
            same_directory = previous.directory == normalized_directory
            previous_files = previous.files if same_directory else MappingProxyType({})
            try:
                files = await asyncio.to_thread(
                    _scan_banned_image_files, normalized_directory, previous_files
                )
            except asyncio.CancelledError:
                # The worker only builds local state, so cancellation cannot publish it.
                raise
            except Exception as exc:
                logger.error(f"扫描违规图片目录 {normalized_directory} 失败，将在后续图片消息中重试: {exc}")
                # Keep the old publication for the same root. A changed root must never
                # be checked against hashes collected from the previous directory.
                return previous if same_directory else None

            cache = _make_image_cache(normalized_directory, files)
            self._image_cache = cache
            if previous.directory != cache.directory or previous.files != cache.files:
                logger.info(f"已刷新 {len(cache.files)} 个违规图片感知哈希 (dHash)")
            return cache

    def _is_banned_image(
        self, image_bytes: bytes, hashes: Optional[Mapping[int, str]] = None
    ) -> Tuple[bool, Optional[str]]:
        """判断图片是否命中违规图片库，返回 (是否命中, 命中文件名)"""
        try:
            h = _compute_dhash(image_bytes)
        except Exception as e:
            logger.error(f"计算 dHash 时出错: {e}")
            return False, None

        for banned_h, filename in (hashes if hashes is not None else self._banned_image_hashes).items():
            dist = _hamming_distance(h, banned_h)
            if dist <= self.DHASH_THRESHOLD:
                logger.debug(f"图片 dHash 匹配成功，汉明距离 = {dist}，匹配文件: {filename}")
                return True, filename
        return False, None

    async def execute(
        self, message: Optional[NachoMessages]
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[NachoMessages]]:
        if not message:
            return True, True, None, None, None

        base_info = message.message_base_info or {}

        # 1. 检查白名单
        whitelist_qq = self.get_config("moderation.whitelist_qq", [])
        user_id = base_info.get("user_id")
        if user_id is not None and str(user_id) in [str(qq) for qq in whitelist_qq]:
            return True, True, "白名单用户，放行", None, None

        # 2. 检查违规词（正则）
        ban_regex_list = self.get_config("moderation.ban_regex_list", [])
        text_to_check = message.plain_text or message.raw_message or ""

        for pattern in ban_regex_list:
            try:
                if pattern and re.search(pattern, text_to_check):
                    logger.info(f"消息命中违规正则 [{pattern}]，触发撤回")
                    self._update_stats("text_patterns", pattern, user_id)
                    await self._do_recall(message)
                    return True, False, "违规拦截(正则)", None, None
            except re.error as e:
                logger.error(f"违规正则 [{pattern}] 无效: {e}")

        # 3. Refresh and check only when this message contains an image or emoji.
        image_segments = [
            seg
            for seg in (getattr(message, "message_segments", None) or ())
            if getattr(seg, "type", "") in ("image", "emoji")
        ]
        if image_segments:
            banned_images_dir = self.get_config("moderation.banned_images_dir", "data/banned_images")
            cache = await self._refresh_banned_image_hashes(banned_images_dir)
            if cache is not None and cache.hashes:
                for seg in image_segments:
                    seg_data = getattr(seg, "data", None)
                    if not isinstance(seg_data, str):
                        continue
                    try:
                        image_bytes = base64.b64decode(seg_data)
                        is_banned, matched_filename = await asyncio.to_thread(
                            self._is_banned_image, image_bytes, cache.hashes
                        )
                        if is_banned:
                            logger.info(f"消息中的图片/表情命中违规感知哈希 ({matched_filename})，触发撤回")
                            self._update_stats("images", matched_filename, user_id)
                            await self._do_recall(message)
                            return True, False, "违规拦截(图片)", None, None
                    except Exception as e:
                        logger.error(f"处理图片哈希时出错: {e}")

        return True, True, None, None, None

    async def _do_recall(self, message: NachoMessages):
        base_info = message.message_base_info or {}
        stream_id = message.stream_id
        message_id = base_info.get("message_id")

        if not stream_id:
            logger.warning("缺少 stream_id，无法发送撤回指令")
            return
        if message_id is None:
            logger.warning("缺少 message_id，无法发送撤回指令")
            return

        logger.info(f"发送撤回指令: message_id={message_id}")
        await self.send_command(
            stream_id=stream_id,
            command_name="DELETE_MSG",
            command_args={"message_id": message_id},
            storage_message=False,
        )
        recall_msg = self.get_config("moderation.recall_message", "")
        if recall_msg:
            await self.send_text(stream_id, recall_msg, storage_message=False)
