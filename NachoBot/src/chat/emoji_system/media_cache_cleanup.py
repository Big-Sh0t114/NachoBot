"""Safe age-based cleanup and atomic restore for image cache files."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from src.common.media_conversion_cache import MEDIA_FILESYSTEM_LOCK


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
MANAGED_DIRECTORIES = {
    "image": IMAGE_EXTENSIONS,
    "images": IMAGE_EXTENSIONS,
    "emoji": IMAGE_EXTENSIONS,
    "video": frozenset({".mp4"}),
}
IMAGE_RESTORE_DIRECTORIES = frozenset({"image", "images"})
EMOJI_RESTORE_DIRECTORIES = frozenset({"emoji"})
REGISTERED_EMOJI_DIRECTORIES = frozenset({"emoji_registed", "emoji_registered"})


@dataclass(frozen=True)
class CleanupResult:
    files_deleted: int = 0
    images_deleted: int = 0
    descriptions_deleted: int = 0
    missing_files_marked: int = 0
    restored_file_flags_cleared: int = 0


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse_or_symlink(path: str | os.PathLike[str]) -> bool:
    try:
        path_stat = os.lstat(path)
    except OSError:
        return False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(file_attributes & reparse_attribute)


def _has_reparse_component(path: str | os.PathLike[str]) -> bool:
    current = Path(os.path.abspath(os.fspath(path)))
    components = [current, *current.parents]
    return any(_is_reparse_or_symlink(component) for component in components)


def _is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except (OSError, ValueError):
        return False


def _safe_scope_path(
    path: str | os.PathLike[str],
    root: str | os.PathLike[str],
    allowed_directories: Iterable[str],
    expected_extensions: Iterable[str],
    *,
    require_file: bool,
) -> bool:
    path_text = os.fspath(path)
    root_text = os.fspath(root)
    if not _is_within(path_text, root_text) or _has_reparse_component(root_text):
        return False
    try:
        root_resolved = Path(root_text).resolve(strict=True)
        absolute_path = Path(os.path.abspath(path_text))
        relative = absolute_path.relative_to(Path(os.path.abspath(root_text)))
        if len(relative.parts) != 2 or relative.parts[0].casefold() not in {
            name.casefold() for name in allowed_directories
        }:
            return False
        if absolute_path.suffix.casefold() not in {suffix.casefold() for suffix in expected_extensions}:
            return False
        if _has_reparse_component(absolute_path):
            return False
        parent_stat = absolute_path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent_stat.st_mode):
            return False
        resolved_parent = absolute_path.parent.resolve(strict=True)
        if os.path.commonpath((str(resolved_parent), str(root_resolved))) != str(root_resolved):
            return False
        if require_file:
            file_stat = absolute_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(file_stat.st_mode):
                return False
            resolved_path = absolute_path.resolve(strict=True)
            if os.path.commonpath((str(resolved_path), str(root_resolved))) != str(root_resolved):
                return False
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def restore_cached_file(
    path: str | os.PathLike[str],
    content: bytes | bytearray | memoryview,
    *,
    root: str | os.PathLike[str],
    allowed_directories: Iterable[str],
    expected_extensions: Iterable[str] = IMAGE_EXTENSIONS,
) -> bool:
    """Atomically restore a missing managed media file under the shared lock."""
    target_path = os.fspath(path)
    content_bytes = bytes(content)
    if not content_bytes:
        return False

    with MEDIA_FILESYSTEM_LOCK:
        if not _safe_scope_path(
            target_path,
            root,
            allowed_directories,
            expected_extensions,
            require_file=False,
        ):
            return False
        if os.path.exists(target_path):
            return _safe_scope_path(
                target_path,
                root,
                allowed_directories,
                expected_extensions,
                require_file=True,
            )

        temporary_path: str | None = None
        try:
            file_descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{Path(target_path).name}.",
                suffix=".restore",
                dir=os.path.dirname(target_path),
            )
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(content_bytes)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, target_path)
            temporary_path = None
            return _safe_scope_path(
                target_path,
                root,
                allowed_directories,
                expected_extensions,
                require_file=True,
            )
        except OSError:
            return False
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass


def ensure_managed_directory(
    root: str | os.PathLike[str],
    directory_name: str,
    *,
    allowed_directories: Iterable[str] = MANAGED_DIRECTORIES,
) -> bool:
    """Create one known direct cache child without traversing reparse points."""
    if directory_name.casefold() not in {name.casefold() for name in allowed_directories}:
        return False
    root_text = os.fspath(root)
    directory = os.path.join(root_text, directory_name)
    with MEDIA_FILESYSTEM_LOCK:
        if _has_reparse_component(root_text) or not os.path.isdir(root_text):
            return False
        if os.path.exists(directory):
            try:
                return (
                    not _has_reparse_component(directory)
                    and stat.S_ISDIR(os.stat(directory, follow_symlinks=False).st_mode)
                    and os.path.commonpath((str(Path(directory).resolve()), str(Path(root_text).resolve())))
                    == str(Path(root_text).resolve())
                )
            except (OSError, RuntimeError, ValueError):
                return False
        try:
            os.mkdir(directory)
            return not _has_reparse_component(directory)
        except OSError:
            return False


def touch_video_cache_files(video_hash: str, *, root: str | os.PathLike[str] = "data") -> int:
    """Refresh matching MP4 artifact mtimes after a cached video-description hit."""
    suffix = f"_{video_hash[:8]}.mp4"
    directory = os.path.join(os.fspath(root), "video")
    if len(video_hash) < 8 or not os.path.isdir(directory) or _has_reparse_component(directory):
        return 0
    touched = 0
    with MEDIA_FILESYSTEM_LOCK:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.casefold().endswith(suffix.casefold()):
                        continue
                    if _is_reparse_or_symlink(entry.path):
                        continue
                    if not _safe_scope_path(
                        entry.path,
                        root,
                        {"video"},
                        {".mp4"},
                        require_file=True,
                    ):
                        continue
                    try:
                        os.utime(entry.path, None, follow_symlinks=False)
                        touched += 1
                    except OSError:
                        continue
        except OSError:
            return touched
    return touched


def _has_model_field(model: Any, field_name: str) -> bool:
    if model is None:
        return False
    metadata = getattr(model, "_meta", None)
    fields = getattr(metadata, "fields", {})
    return hasattr(model, field_name) or field_name in fields


def _record_value(record: Any, field_name: str, default: Any = None) -> Any:
    try:
        return getattr(record, field_name)
    except (AttributeError, KeyError):
        return default


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iter_model_batches(model: Any, batch_size: int) -> Iterable[list[Any]]:
    if model is None:
        return
    primary_key = getattr(model, "id", None)
    last_id = None
    page = 1
    while True:
        query = model.select()
        if primary_key is not None:
            if last_id is not None:
                query = query.where(primary_key > last_id)
            query = query.order_by(primary_key).limit(batch_size)
        else:
            query = query.paginate(page, batch_size)
        batch = list(query.execute())
        if not batch:
            break
        yield batch
        if primary_key is None:
            page += 1
        else:
            last_id = getattr(batch[-1], "id", None)
            if last_id is None:
                break


def _positive_days(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _retention_for_type(media_type: str, image_days: int, emoji_days: int) -> int:
    return emoji_days if media_type.casefold() == "emoji" else image_days


def _is_expired(timestamp: float | None, now: float, retention_days: int, fallback_mtime: float) -> bool:
    reference_time = timestamp if timestamp is not None else fallback_mtime
    return now - reference_time >= retention_days * 86_400


def _same_physical_file(candidate: str, other: str) -> bool:
    if _path_key(candidate) == _path_key(other):
        return True
    try:
        return os.path.samefile(candidate, other)
    except OSError:
        return False


def _sync_image_flags(images_model: Any, root: str, batch_size: int) -> tuple[int, int]:
    missing_marked = 0
    restored_flags_cleared = 0
    has_no_file_flag = _has_model_field(images_model, "no_file_flag")
    if not has_no_file_flag:
        return missing_marked, restored_flags_cleared

    for batch in _iter_model_batches(images_model, batch_size):
        for record in batch:
            path = _record_value(record, "path")
            if not path:
                continue
            path_text = os.fspath(path)
            if str(_record_value(record, "type", "image") or "image").casefold() not in {"image", "emoji"}:
                continue

            if not _safe_scope_path(
                path_text,
                root,
                MANAGED_DIRECTORIES,
                set().union(*MANAGED_DIRECTORIES.values()),
                require_file=False,
            ):
                continue
            exists = _safe_scope_path(
                path_text,
                root,
                MANAGED_DIRECTORIES,
                set().union(*MANAGED_DIRECTORIES.values()),
                require_file=True,
            )
            if exists and bool(_record_value(record, "no_file_flag", False)):
                record.no_file_flag = False
                record.save()
                restored_flags_cleared += 1
            elif not exists and not bool(_record_value(record, "no_file_flag", False)):
                record.no_file_flag = True
                record.save()
                missing_marked += 1
    return missing_marked, restored_flags_cleared


def _scan_registered_emoji_state(emoji_model: Any, batch_size: int) -> tuple[list[str], set[str]]:
    paths: list[str] = []
    hashes: set[str] = set()
    if emoji_model is None or not hasattr(emoji_model, "full_path"):
        return paths, hashes
    registered_field = _has_model_field(emoji_model, "is_registered")
    for batch in _iter_model_batches(emoji_model, batch_size):
        for record in batch:
            path = _record_value(record, "full_path")
            if not path:
                continue
            path_text = os.fspath(path)
            registered = bool(_record_value(record, "is_registered", False)) if registered_field else False
            in_registered_directory = Path(path_text).parent.name.casefold() in {
                name.casefold() for name in REGISTERED_EMOJI_DIRECTORIES
            }
            if registered or in_registered_directory:
                paths.append(path_text)
                emoji_hash = _record_value(record, "emoji_hash")
                if emoji_hash:
                    hashes.add(str(emoji_hash))
    return paths, hashes


def _file_identity(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(file_stat.st_mode):
            return None
        return file_stat.st_dev, file_stat.st_ino
    except OSError:
        return None


def _scan_image_aliases(images_model: Any, candidates: list[tuple[float, str]], batch_size: int) -> dict[tuple[int, int], list[Any]]:
    identities = {identity for _mtime, path in candidates if (identity := _file_identity(path)) is not None}
    references: dict[tuple[int, int], list[Any]] = {}
    if not identities:
        return references
    for batch in _iter_model_batches(images_model, batch_size):
        for record in batch:
            path = _record_value(record, "path")
            if not path:
                continue
            identity = _file_identity(os.fspath(path))
            if identity in identities:
                references.setdefault(identity, []).append(record)
    return references


def _database_context(database: Any):
    context_factory = getattr(database, "connection_context", None)
    return context_factory() if context_factory else nullcontext()


def _query_model_rows_by_ids(model: Any, record_ids: list[Any]) -> list[Any]:
    if model is None or not record_ids:
        return []
    return list(model.select().where(model.id.in_(record_ids)).execute())


def _delete_expired_images(
    images_model: Any,
    emoji_model: Any,
    root: str,
    now: float,
    retention_days: int,
    batch_size: int,
) -> int:
    if batch_size <= 0 or (
        images_model is None
        or not _has_model_field(images_model, "no_file_flag")
        or not _has_model_field(images_model, "id")
    ):
        return 0

    deleted = 0
    for batch in _iter_model_batches(images_model, batch_size):
        expired_ids = []
        for record in batch:
            if deleted + len(expired_ids) >= batch_size:
                break
            if not bool(_record_value(record, "no_file_flag", False)):
                continue
            if str(_record_value(record, "type", "image") or "image").casefold() not in {"image", "emoji"}:
                continue
            path = _record_value(record, "path")
            if not path or os.path.exists(os.fspath(path)):
                continue
            path_text = os.fspath(path)
            if not _safe_scope_path(
                path_text,
                root,
                MANAGED_DIRECTORIES,
                set().union(*MANAGED_DIRECTORIES.values()),
                require_file=False,
            ):
                continue
            used_at = _float_or_none(_record_value(record, "last_used_time"))
            if used_at is None:
                used_at = _float_or_none(_record_value(record, "timestamp"))
            if used_at is not None and not _is_expired(used_at, now, retention_days, now):
                continue
            expired_ids.append(_record_value(record, "id"))

        expired_ids = [record_id for record_id in expired_ids if record_id is not None]
        if not expired_ids:
            continue

        with MEDIA_FILESYSTEM_LOCK:
            current_rows = _query_model_rows_by_ids(images_model, expired_ids)
            _registered_paths, registered_hashes = _scan_registered_emoji_state(emoji_model, batch_size)
            for record in current_rows:
                if not bool(_record_value(record, "no_file_flag", False)):
                    continue
                if str(_record_value(record, "type", "image") or "image").casefold() not in {"image", "emoji"}:
                    continue
                image_hash = str(_record_value(record, "emoji_hash", "") or "")
                if str(_record_value(record, "type", "image") or "image").casefold() == "emoji" and image_hash in registered_hashes:
                    continue
                path = _record_value(record, "path")
                if not path or os.path.exists(os.fspath(path)):
                    continue
                path_text = os.fspath(path)
                if not _safe_scope_path(
                    path_text,
                    root,
                    MANAGED_DIRECTORIES,
                    set().union(*MANAGED_DIRECTORIES.values()),
                    require_file=False,
                ):
                    continue
                used_at = _float_or_none(_record_value(record, "last_used_time"))
                if used_at is None:
                    used_at = _float_or_none(_record_value(record, "timestamp"))
                if used_at is not None and not _is_expired(used_at, now, retention_days, now):
                    continue
                record.delete_instance()
                deleted += 1
                if deleted >= batch_size:
                    return deleted
    return deleted


def _delete_expired_descriptions(model: Any, now: float, retention_days: int, batch_size: int) -> int:
    if batch_size <= 0 or model is None or not _has_model_field(model, "timestamp"):
        return 0
    deleted = 0
    cutoff = now - retention_days * 86_400
    for batch in _iter_model_batches(model, batch_size):
        expired_ids = [
            _record_value(record, "id")
            for record in batch
            if _float_or_none(_record_value(record, "timestamp")) is None
            or _float_or_none(_record_value(record, "timestamp")) <= cutoff
        ]
        expired_ids = [record_id for record_id in expired_ids if record_id is not None]
        if not expired_ids:
            continue
        with MEDIA_FILESYSTEM_LOCK:
            current_rows = _query_model_rows_by_ids(model, expired_ids)
            for record in current_rows:
                if deleted >= batch_size:
                    return deleted
                timestamp = _float_or_none(_record_value(record, "timestamp"))
                if timestamp is not None and timestamp > cutoff:
                    continue
                record.delete_instance()
                deleted += 1
    return deleted


def cleanup_media_cache(
    *,
    root: str | os.PathLike[str] = "data",
    images_model: Any = None,
    descriptions_model: Any = None,
    emoji_model: Any = None,
    database: Any = None,
    image_file_retention_days: int = 14,
    emoji_file_retention_days: int = 30,
    description_retention_days: int = 30,
    video_file_retention_days: int = 14,
    batch_size: int = 1000,
    now: float | None = None,
) -> CleanupResult:
    """Delete only expired files under known cache directories and expired metadata.

    The caller runs this function in a worker thread and supplies test database
    models directly. It never imports or initializes the production database.
    """
    root_text = os.fspath(root)
    if not os.path.isdir(root_text) or _has_reparse_component(root_text):
        return CleanupResult()

    now = time.time() if now is None else float(now)
    batch_size = max(1, int(batch_size))
    image_days = _positive_days(image_file_retention_days, 14)
    emoji_days = _positive_days(emoji_file_retention_days, 30)
    video_days = _positive_days(video_file_retention_days, 14)
    description_days = _positive_days(description_retention_days, 30)
    if database is None and images_model is not None:
        database = getattr(getattr(images_model, "_meta", None), "database", None)

    with _database_context(database):
        missing_marked, flags_cleared = _sync_image_flags(images_model, root_text, batch_size)
        deleted_files = 0

        def process_candidates(candidates: list[tuple[float, str]], directory_name: str) -> None:
            nonlocal deleted_files
            if not candidates:
                return
            with MEDIA_FILESYSTEM_LOCK:
                # Every database scan for this batch finishes before its first unlink.
                image_aliases = _scan_image_aliases(images_model, candidates, batch_size)
                registered_paths, registered_hashes = _scan_registered_emoji_state(emoji_model, batch_size)
                for _original_mtime, path in candidates:
                    if deleted_files >= batch_size:
                        return
                    allowed_extensions = MANAGED_DIRECTORIES[directory_name]
                    if not _safe_scope_path(
                        path,
                        root_text,
                        MANAGED_DIRECTORIES,
                        allowed_extensions,
                        require_file=True,
                    ):
                        continue
                    try:
                        current_stat = os.stat(path, follow_symlinks=False)
                    except OSError:
                        continue
                    file_mtime = current_stat.st_mtime
                    identity = (current_stat.st_dev, current_stat.st_ino)
                    retention_days = (
                        video_days if directory_name == "video" else emoji_days if directory_name == "emoji" else image_days
                    )
                    if now - current_stat.st_mtime < retention_days * 86_400:
                        continue

                    if any(
                        os.path.exists(registered_path) and _same_physical_file(path, registered_path)
                        for registered_path in registered_paths
                    ):
                        continue

                    references = image_aliases.get(identity, [])
                    active_reference = False
                    for record in references:
                        record_type = str(_record_value(record, "type", "image") or "image").casefold()
                        if record_type == "emoji" and str(_record_value(record, "emoji_hash", "") or "") in registered_hashes:
                            active_reference = True
                            break
                        used_at = _float_or_none(_record_value(record, "last_used_time"))
                        if used_at is None:
                            used_at = _float_or_none(_record_value(record, "timestamp"))
                        record_retention = _retention_for_type(record_type, image_days, emoji_days)
                        if not _is_expired(used_at, now, record_retention, file_mtime):
                            active_reference = True
                            break
                    if active_reference:
                        continue
                    if not references and not _is_expired(None, now, retention_days, current_stat.st_mtime):
                        continue

                    try:
                        os.remove(path)
                    except OSError:
                        continue
                    for record in references:
                        if _has_model_field(type(record), "no_file_flag"):
                            record.no_file_flag = True
                            record.save()
                    deleted_files += 1

        for directory_name, allowed_extensions in MANAGED_DIRECTORIES.items():
            if deleted_files >= batch_size:
                break
            directory = os.path.join(root_text, directory_name)
            if not os.path.isdir(directory) or _has_reparse_component(directory):
                continue
            candidate_batch: list[tuple[float, str]] = []
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if Path(entry.name).suffix.casefold() not in allowed_extensions:
                            continue
                        if _is_reparse_or_symlink(entry.path):
                            continue
                        try:
                            file_stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if not stat.S_ISREG(file_stat.st_mode):
                            continue
                        if not _safe_scope_path(
                            entry.path,
                            root_text,
                            MANAGED_DIRECTORIES,
                            allowed_extensions,
                            require_file=True,
                        ):
                            continue
                        candidate_batch.append((file_stat.st_mtime, entry.path))
                        if len(candidate_batch) >= batch_size:
                            candidate_batch.sort(key=lambda item: item[0])
                            process_candidates(candidate_batch, directory_name)
                            candidate_batch = []
                            if deleted_files >= batch_size:
                                break
            except OSError:
                continue
            if candidate_batch and deleted_files < batch_size:
                candidate_batch.sort(key=lambda item: item[0])
                process_candidates(candidate_batch, directory_name)

        remaining = max(0, batch_size - deleted_files)
        images_deleted = _delete_expired_images(
            images_model,
            emoji_model,
            root_text,
            now,
            description_days,
            remaining,
        )
        remaining = max(0, remaining - images_deleted)
        descriptions_deleted = _delete_expired_descriptions(
            descriptions_model,
            now,
            description_days,
            remaining,
        )
        return CleanupResult(
            files_deleted=deleted_files,
            images_deleted=images_deleted,
            descriptions_deleted=descriptions_deleted,
            missing_files_marked=missing_marked,
            restored_file_flags_cleared=flags_cleared,
        )
