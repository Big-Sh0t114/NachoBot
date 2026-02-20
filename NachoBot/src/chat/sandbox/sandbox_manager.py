import shutil
import time
from typing import Dict, List, Optional
from dataclasses import dataclass
from pathlib import Path

from src.common.logger import get_logger

logger = get_logger("sandbox_manager")


@dataclass
class FileRecord:
    """Record of a file in the sandbox"""

    filename: str
    local_path: str
    upload_time: float
    is_temp: bool = True  # Whether to auto-clean


class Sandbox:
    """
    Manages a temporary directory for a specific chat session.
    Isolates files per chat_id.
    """

    def __init__(self, chat_id: str, base_dir: str = "data/sandbox"):
        self.chat_id = chat_id
        self.base_dir = Path(base_dir)
        self.session_dir = self.base_dir / chat_id
        self.files: Dict[str, FileRecord] = {}

        # Ensure directory exists
        self._ensure_dir()

    def _ensure_dir(self):
        if not self.session_dir.exists():
            self.session_dir.mkdir(parents=True, exist_ok=True)

    def save_file(self, file_data: bytes, filename: str, overwrite: bool = False) -> str:
        """
        Save a file to the sandbox.
        Returns the absolute local path.
        """
        self._ensure_dir()

        # Sanitize filename
        safe_filename = Path(filename).name
        if not overwrite:
            # Handle duplicates: file.txt -> file_1.txt
            stem = Path(safe_filename).stem
            suffix = Path(safe_filename).suffix
            counter = 1
            while (self.session_dir / safe_filename).exists():
                safe_filename = f"{stem}_{counter}{suffix}"
                counter += 1

        file_path = self.session_dir / safe_filename

        with open(file_path, "wb") as f:
            f.write(file_data)

        # Record entry
        self.files[safe_filename] = FileRecord(
            filename=safe_filename, local_path=str(file_path.absolute()), upload_time=time.time()
        )

        logger.info(f"Saved file {safe_filename} to sandbox {self.chat_id}")
        return str(file_path.absolute())

    def get_file_path(self, filename: str) -> Optional[str]:
        """Get absolute path of a file if it exists"""
        file_path = self.session_dir / filename
        if file_path.exists():
            return str(file_path.absolute())
        return None

    def list_files(self) -> List[str]:
        """List all files in this sandbox"""
        if not self.session_dir.exists():
            return []
        return [f.name for f in self.session_dir.iterdir() if f.is_file()]

    def read_file(self, filename: str) -> Optional[str]:
        """Read text file content"""
        file_path = self.session_dir / filename
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            return "[Error: Binary file or non-utf8 encoding]"
        except Exception as e:
            return f"[Error reading file: {e}]"

    def write_file(self, filename: str, content: str) -> str:
        """Write text content to a file"""
        self._ensure_dir()
        file_path = self.session_dir / filename
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        self.files[filename] = FileRecord(
            filename=filename, local_path=str(file_path.absolute()), upload_time=time.time()
        )
        return str(file_path.absolute())

    def clear(self):
        """Delete all files in this sandbox"""
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir)
            self._ensure_dir()
            self.files.clear()


class SandboxManager:
    """
    Global manager for all sandboxes.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.sandboxes = {}
            cls._instance.base_path = "data/sandbox"
        return cls._instance

    def get_sandbox(self, chat_id: str) -> Sandbox:
        if chat_id not in self.sandboxes:
            self.sandboxes[chat_id] = Sandbox(chat_id, self.base_path)
        return self.sandboxes[chat_id]

    def cleanup_old_sessions(self, max_age_seconds: int = 86400):
        """Cleanup sessions inactive for > 24 hours (example)"""
        base_dir = Path(self.base_path)
        if not base_dir.exists():
            return

        current_time = time.time()
        cleaned_count = 0

        for session_dir in base_dir.iterdir():
            if not session_dir.is_dir():
                continue

            # Check modification time of the session dir
            # Alternatively, if empty or all files inside are old
            is_old = False
            try:
                mtime = session_dir.stat().st_mtime
                if current_time - mtime > max_age_seconds:
                    is_old = True
            except OSError:
                continue

            if is_old:
                try:
                    shutil.rmtree(session_dir)
                    session_id = session_dir.name
                    logger.info(f"[Sandbox] 自动清理已过期 (> {max_age_seconds}s) 的沙盒会话: {session_id}")
                    if session_id in self.sandboxes:
                        del self.sandboxes[session_id]
                    cleaned_count += 1
                except Exception as e:
                    logger.error(f"[Sandbox] 清理过期沙盒 {session_dir.name} 时出错: {e}")

        if cleaned_count > 0:
            logger.info(f"[Sandbox] 本次自动清理完成，共移除 {cleaned_count} 个过期沙盒。")

    async def start_periodic_cleanup(self, interval_seconds: int = 3600, max_age_seconds: int = 86400):
        """
        后台连续执行的清理协程。
        interval_seconds: 每隔多长时间(秒)执行一次清理检查，默认 1 小时
        max_age_seconds: 判断为过期的存活时间(秒)，默认 24 小时
        """
        import asyncio

        logger.info(f"[Sandbox] 沙盒自动清理任务已启动，检查周期: {interval_seconds}s，过期阈值: {max_age_seconds}s")
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.cleanup_old_sessions(max_age_seconds)
            except Exception as e:
                logger.error(f"[Sandbox] 周期清理任务执行出错: {e}")


sandbox_manager = SandboxManager()
