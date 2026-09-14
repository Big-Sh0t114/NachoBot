"""Bounded, copy-first storage for the chat file-edit sandbox.

File editing uses :class:`SandboxScope` and the provider in
``sandbox_agent.py``. A group scope deliberately has two roots: the group
root is readable, while the actor root is the only writable tree.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Iterator, Optional

from src.common.logger import get_logger

logger = get_logger("sandbox_manager")

MAX_UPLOAD_BYTES = 1 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
REVISION_CHUNK_BYTES = 64 * 1024
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SandboxPathError(ValueError):
    """Raised when a user/model path is not a safe relative sandbox path."""


def validate_identifier(value: object, label: str = "identifier") -> str:
    """Validate an on-disk scope component without normalising its spelling."""

    text = str(value or "")
    if not text or text in {".", ".."} or not _SAFE_COMPONENT.fullmatch(text):
        raise SandboxPathError(f"unsafe {label}")
    return text


def validate_relative_path(value: object, *, allow_empty: bool = False) -> str:
    """Return a slash-normalised relative path or reject traversal/absolute paths."""

    text = str(value or "").strip()
    if not text:
        if allow_empty:
            return ""
        raise SandboxPathError("path is required")
    # Reject Windows and POSIX absolute/drive/UNC spellings on every platform.
    win = PureWindowsPath(text)
    posix = PurePosixPath(text.replace("\\", "/"))
    if win.is_absolute() or bool(win.drive) or win.root or posix.is_absolute():
        raise SandboxPathError("absolute, drive, or UNC paths are forbidden")
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise SandboxPathError("path traversal is forbidden")
    if not parts:
        if allow_empty:
            return ""
        raise SandboxPathError("path is required")
    for part in parts:
        if "\x00" in part:
            raise SandboxPathError("NUL is forbidden")
    return "/".join(parts)


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _resolved_under(path: Path, root: Path) -> bool:
    try:
        root_resolved = root.resolve(strict=False)
        path_resolved = path.resolve(strict=False)
        return os.path.commonpath((str(root_resolved), str(path_resolved))) == str(root_resolved)
    except (OSError, ValueError):
        return False


def _check_no_reparse_components(path: Path, stop: Path) -> None:
    """Reject a symlink/junction anywhere between ``stop`` and ``path``."""

    path = path.absolute()
    stop = stop.absolute()
    try:
        relative = path.relative_to(stop)
    except ValueError as exc:
        raise SandboxPathError("path escapes sandbox root") from exc
    current = stop
    if _is_reparse_or_symlink(current):
        raise SandboxPathError("symlink or reparse-point escape is forbidden")
    for component in relative.parts:
        current = current / component
        # ``Path.exists()`` follows links and is false for a dangling link;
        # inspect the component itself so a dangling symlink cannot become an
        # escape after a later upload creates its target.
        if _is_reparse_or_symlink(current):
            raise SandboxPathError("symlink or reparse-point escape is forbidden")


@dataclass(frozen=True)
class SandboxScope:
    """A server-resolved sandbox scope."""

    key: str
    read_root: Path
    write_root: Path
    group_root: Optional[Path] = None
    group_id: Optional[str] = None
    actor_id: str = ""
    platform: str = ""
    stream_id: str = ""
    storage_root: Optional[Path] = None

    @property
    def is_group(self) -> bool:
        return self.group_root is not None

    @property
    def trusted_storage_root(self) -> Path:
        # Older private callers may construct a scope directly. Manager-created
        # scopes always provide the explicit storage root.
        return Path(self.storage_root or self.read_root)

    def _check_storage_path(self, path: Path) -> None:
        storage_root = self.trusted_storage_root
        if not _resolved_under(path, storage_root):
            raise SandboxPathError("path escapes trusted sandbox storage")
        _check_no_reparse_components(path, storage_root)

    def ensure(self) -> None:
        storage_root = self.trusted_storage_root
        # Check the configured base before creating descendants, then check
        # every newly-created component again. This catches replacement of
        # groups/platform/user directories by links between calls.
        if storage_root.exists() and _is_reparse_or_symlink(storage_root):
            raise SandboxPathError("sandbox storage root cannot be a symlink or reparse point")
        storage_root.mkdir(parents=True, exist_ok=True)
        self._check_storage_path(storage_root)
        self._check_storage_path(self.read_root)
        self._check_storage_path(self.write_root)
        self.read_root.mkdir(parents=True, exist_ok=True)
        self.write_root.mkdir(parents=True, exist_ok=True)
        for root in {self.read_root, self.write_root}:
            self._check_storage_path(root)

    def path_for_read(self, relative: object = "") -> Path:
        self._check_storage_path(self.read_root)
        safe = validate_relative_path(relative, allow_empty=True)
        path = self.read_root / Path(safe) if safe else self.read_root
        if not _resolved_under(path, self.read_root):
            raise SandboxPathError("path escapes sandbox root")
        self._check_storage_path(path)
        _check_no_reparse_components(path, self.read_root)
        return path

    def path_for_write(self, relative: object) -> Path:
        self._check_storage_path(self.write_root)
        safe = validate_relative_path(relative)
        path = self.write_root / Path(safe)
        if not _resolved_under(path, self.write_root):
            raise SandboxPathError("path escapes actor root")
        self._check_storage_path(path)
        _check_no_reparse_components(path, self.write_root)
        return path

    def revision(self) -> str:
        """Return a stable snapshot token for commit-time conflict detection."""

        digest = hashlib.sha256()
        root = self.read_root
        self._check_storage_path(root)
        if not root.exists():
            return digest.hexdigest()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file() or _is_reparse_or_symlink(path):
                continue
            try:
                relative = path.relative_to(root).as_posix()
                self.path_for_read(relative)
                info = path.stat()
                digest.update(relative.encode("utf-8", "strict"))
                digest.update(str(info.st_size).encode("ascii"))
                digest.update(str(info.st_mtime_ns).encode("ascii"))
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(REVISION_CHUNK_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
            except OSError:
                continue
        return digest.hexdigest()


class SandboxManager:
    """Resolve scopes, coordinate mutations, and perform age-based cleanup."""

    def __init__(self, base_path: str = "data/sandbox") -> None:
        self.base_path = str(base_path)
        self.storage_root = Path(self.base_path)
        self._locks: Dict[str, threading.RLock] = {}
        self._manager_lock = threading.RLock()

    def _lock_for(self, key: str) -> threading.RLock:
        with self._manager_lock:
            return self._locks.setdefault(key, threading.RLock())

    def mutation_lock(self, scope: SandboxScope) -> threading.RLock:
        return self._lock_for(scope.key)

    def get_scope(
        self,
        *,
        stream_id: str,
        platform: str,
        group_id: Optional[str],
        actor_id: str,
    ) -> SandboxScope:
        platform = validate_identifier(platform or "unknown", "platform")
        actor_id = validate_identifier(actor_id, "actor_id")
        stream_id = str(stream_id or "")
        if group_id is not None and str(group_id) != "":
            group_id = validate_identifier(group_id, "group_id")
            group_root = Path(self.base_path) / "groups" / platform / group_id
            scope = SandboxScope(
                key=f"group:{platform}:{group_id}",
                read_root=group_root,
                write_root=group_root / actor_id,
                storage_root=self.storage_root,
                group_root=group_root,
                group_id=group_id,
                actor_id=actor_id,
                platform=platform,
                stream_id=stream_id,
            )
        else:
            if not stream_id:
                raise SandboxPathError("stream_id is required for private scope")
            stream_component = validate_identifier(stream_id, "stream_id")
            root = Path(self.base_path) / stream_component
            scope = SandboxScope(
                key=f"private:{stream_component}",
                read_root=root,
                write_root=root,
                storage_root=self.storage_root,
                actor_id=actor_id,
                platform=platform,
                stream_id=stream_id,
            )
        scope.ensure()
        return scope

    def save_upload(
        self,
        file_data: bytes,
        filename: str,
        *,
        stream_id: str,
        platform: str,
        group_id: Optional[str],
        actor_id: str,
    ) -> str:
        scope = self.get_scope(stream_id=stream_id, platform=platform, group_id=group_id, actor_id=actor_id)
        with self.mutation_lock(scope):
            if len(file_data) > MAX_UPLOAD_BYTES:
                raise ValueError("file exceeds 1MB limit")
            # Revalidate roots inside the mutation lock so a concurrent
            # replacement of a group/platform/actor directory cannot redirect
            # the upload between scope creation and the write.
            scope.ensure()
            safe_filename = Path(validate_relative_path(filename)).name
            stem, suffix = Path(safe_filename).stem, Path(safe_filename).suffix
            candidate = safe_filename
            counter = 1
            while True:
                file_path = scope.path_for_write(candidate)
                if not file_path.exists():
                    break
                candidate = f"{stem}_{counter}{suffix}"
                counter += 1
            file_path.parent.mkdir(parents=True, exist_ok=True)
            self._check_storage_path(file_path.parent)
            file_path = scope.path_for_write(candidate)
            with file_path.open("xb") as handle:
                handle.write(file_data)
            absolute_path = str(file_path.absolute())
            logger.info("已将文件“%s”保存到当前沙盒", candidate)
            return absolute_path

    def make_staging_dir(self, handoff_id: str) -> Path:
        safe = validate_identifier(handoff_id, "handoff_id")
        self._check_storage_path(self.storage_root)
        staging_root = self.storage_root / ".staging"
        self._check_storage_path(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)
        self._check_storage_path(staging_root)
        staging_dir = Path(tempfile.mkdtemp(prefix=f"{safe}-", dir=staging_root))
        self._check_storage_path(staging_dir)
        return staging_dir

    def _check_storage_path(self, path: Path) -> None:
        path = Path(path)
        if not _resolved_under(path, self.storage_root):
            raise SandboxPathError("path escapes trusted sandbox storage")
        _check_no_reparse_components(path, self.storage_root)

    def cleanup_staging(self, path: Path) -> None:
        try:
            staging_root = self.storage_root / ".staging"
            self._check_storage_path(staging_root)
            self._check_storage_path(path)
            if not _resolved_under(path, staging_root):
                return
            if path.exists():
                shutil.rmtree(path)
        except (OSError, ValueError) as exc:
            logger.warning("sandbox staging cleanup failed: %s", exc)

    def _iter_files(self, root: Path) -> Iterator[Path]:
        if not root.exists():
            return
        for path in root.rglob("*"):
            if path.is_file() and not _is_reparse_or_symlink(path):
                yield path

    def cleanup_old_sessions(self, max_age_seconds: int = 86400, *, now: Optional[float] = None) -> None:
        """Expire files while retaining group and user roots.

        Private stream roots retain the former behavior and are removed as a
        whole when stale. Group roots are never removed; only stale files are.
        """

        base_dir = Path(self.base_path)
        if not base_dir.exists():
            return
        try:
            self._check_storage_path(base_dir)
        except SandboxPathError:
            return
        current_time = time.time() if now is None else float(now)
        groups_root = base_dir / "groups"
        try:
            self._check_storage_path(groups_root)
        except SandboxPathError:
            groups_root = None
        if groups_root is not None and groups_root.exists():
            for file_path in list(self._iter_files(groups_root)):
                try:
                    if current_time - file_path.stat().st_mtime > max_age_seconds:
                        try:
                            relative = file_path.relative_to(groups_root)
                            lock_key = (
                                f"group:{relative.parts[0]}:{relative.parts[1]}"
                                if len(relative.parts) >= 2
                                else f"group-file:{file_path}"
                            )
                        except ValueError:
                            lock_key = f"group-file:{file_path}"
                        with self._lock_for(lock_key):
                            try:
                                if current_time - file_path.stat().st_mtime <= max_age_seconds:
                                    continue
                            except OSError:
                                continue
                            file_path.unlink(missing_ok=True)
                except OSError:
                    continue

        for session_dir in list(base_dir.iterdir()):
            if (
                not session_dir.is_dir()
                or _is_reparse_or_symlink(session_dir)
                or session_dir.name in {"groups", ".staging"}
            ):
                continue
            try:
                if current_time - session_dir.stat().st_mtime > max_age_seconds:
                    with self._lock_for(f"private:{session_dir.name}"):
                        try:
                            if current_time - session_dir.stat().st_mtime <= max_age_seconds:
                                continue
                        except OSError:
                            continue
                        shutil.rmtree(session_dir, ignore_errors=True)
            except OSError:
                continue

    async def start_periodic_cleanup(self, interval_seconds: int = 3600, max_age_seconds: int = 86400) -> None:
        import asyncio

        logger.info("沙盒定时清理任务已启动，执行间隔 %s 秒", interval_seconds)
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.cleanup_old_sessions(max_age_seconds)
            except Exception as exc:
                logger.error("sandbox cleanup failed: %s", exc)


sandbox_manager = SandboxManager()


__all__ = [
    "MAX_UPLOAD_BYTES",
    "MAX_TEXT_BYTES",
    "SandboxManager",
    "SandboxPathError",
    "SandboxScope",
    "sandbox_manager",
    "validate_identifier",
    "validate_relative_path",
]
