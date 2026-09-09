"""Lifecycle-safe Bilibili QR login support for the setup wizard.

The WebUI deliberately owns only the process lifecycle and the QR bitmap.  The
fixed adapter helper owns all network interaction and credential persistence.
The manager never captures child output and never returns parsed credentials.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import struct
import time
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from .setup_checks import ROOT_DIR
except ImportError:  # pragma: no cover - script-style WebUI startup
    from setup_checks import ROOT_DIR


ADAPTER_NAME = "NachoBot-Bilibili-Adapter"
QR_FILENAME = "qr_login.png"
QR_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUIRED_CREDENTIAL_FIELDS = ("sessdata", "bili_jct", "dede_user_id")

ProcessFactory = Callable[..., Awaitable[Any]]


@dataclass
class _LoginJob:
    job_id: str
    qr_path: Path
    started_at: float
    process: Any = None
    status: str = "starting"
    message: str = "正在启动 Bilibili 登录..."
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


class BilibiliLoginNotReady(RuntimeError):
    """Raised when the wizard has not completed config/dependency phases."""


class BilibiliLoginCleanupError(RuntimeError):
    """Raised when the fixed QR artifact cannot be removed safely."""


class BilibiliLoginProcessError(RuntimeError):
    """Raised when the fixed login process cannot be proven to have exited."""


class BilibiliLoginManager:
    """Manage one cancellable, current-generation QR login job."""

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        process_factory: ProcessFactory | None = None,
        poll_interval: float = 0.25,
        stop_timeout: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # ``root_dir`` is dependency injection for isolated tests.  The live
        # route always uses the repository root and fixed relative paths.
        self.root_dir = Path(root_dir or ROOT_DIR).resolve()
        self.adapter_dir = self.root_dir / ADAPTER_NAME
        self.config_path = self.adapter_dir / "config.toml"
        self.qr_path = self.adapter_dir / QR_FILENAME
        self.script_path = self.adapter_dir / "qr_login.py"
        python_rel = (
            Path(".venv") / "Scripts" / "python.exe"
            if os.name == "nt"
            else Path(".venv") / "bin" / "python"
        )
        self.python_path = (self.adapter_dir / python_rel).resolve()
        self.command = (
            str(self.python_path),
            str(self.script_path),
            "--config",
            str(self.config_path),
            "--qr-output",
            str(self.qr_path),
        )
        # Keep the default lookup late so tests and embedding applications can
        # patch the subprocess creator after importing this module.  The live
        # command itself remains fixed below.
        self._process_factory = process_factory
        self._poll_interval = max(0.02, float(poll_interval))
        self._stop_timeout = max(0.1, float(stop_timeout))
        self._clock = clock or time.monotonic

        # Lifecycle serialization is intentionally separate from state access.
        # It is held only while stopping/spawning or regenerating config, never
        # during the 180-second child wait.
        self._lifecycle_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._current: _LoginJob | None = None
        self._config_ready = False
        self._dependencies_ready = False
        self._closed = False
        self._cleanup_failed = False
        self._lifecycle_failed = False

    async def startup(self) -> None:
        """Re-open the singleton for a fresh application lifespan."""
        async with self._lifecycle_lock:
            self._closed = False

    def readiness(self) -> dict[str, bool]:
        """Return phase readiness without exposing any config content."""
        return {
            "config_ready": self._config_ready,
            "dependencies_ready": self._dependencies_ready,
        }

    @property
    def cleanup_failed(self) -> bool:
        """Expose only whether the fixed QR cleanup last failed."""
        return self._cleanup_failed

    @property
    def lifecycle_failed(self) -> bool:
        """Expose only whether a process lifecycle operation failed."""
        return self._lifecycle_failed

    def mark_config_generation(self, result: dict[str, Any] | bool) -> None:
        """Record whether the selected Bilibili config generation succeeded."""
        if isinstance(result, dict):
            ready = not bool(result.get("errors"))
        else:
            ready = bool(result)
        self._config_ready = ready
        if not ready:
            self._dependencies_ready = False

    def mark_dependency_task(self, task_id: str, status: str) -> None:
        """Record completion of the fixed Bilibili dependency task."""
        if str(task_id) != "bilibili":
            return
        self._dependencies_ready = str(status) == "ok"

    @asynccontextmanager
    async def config_generation(self):
        """Stop the current job and serialize a Bilibili config write."""
        async with self._lifecycle_lock:
            await self._stop_current_unlocked()
            self._cleanup_qr()
            self._config_ready = False
            self._dependencies_ready = False
            yield

    async def start(self, *, require_ready: bool = True) -> dict[str, Any]:
        """Start a fresh QR login generation and return a safe snapshot."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Bilibili 登录管理器已关闭")
            if require_ready:
                ready = self.readiness()
                if not ready["config_ready"] or not ready["dependencies_ready"]:
                    raise BilibiliLoginNotReady("请先完成 Bilibili 配置生成和依赖安装")
            if not self.config_path.exists():
                raise RuntimeError("Bilibili 配置尚未生成")
            if not self.script_path.is_file():
                raise RuntimeError("Bilibili 登录脚本不存在")
            if not self.python_path.is_file():
                raise RuntimeError("Bilibili 适配器运行环境尚未准备好")

            await self._stop_current_unlocked()
            self._cleanup_qr()
            job = _LoginJob(
                job_id=secrets.token_urlsafe(24),
                qr_path=self.qr_path,
                started_at=self._clock(),
            )
            async with self._state_lock:
                self._current = job
            job.task = asyncio.create_task(self._run(job))
            return self._snapshot_unlocked(job)

    async def status(self, job_id: str) -> dict[str, Any] | None:
        """Return a current-generation status, or ``None`` for stale IDs."""
        async with self._state_lock:
            job = self._current
            if job is None or not secrets.compare_digest(str(job_id), job.job_id):
                return None
            return self._snapshot_unlocked(job)

    async def read_qr(self, job_id: str) -> bytes | None:
        """Read only the complete QR for the current waiting generation."""
        async with self._state_lock:
            job = self._current
            if (
                job is None
                or not secrets.compare_digest(str(job_id), job.job_id)
                or job.status not in {"starting", "waiting"}
            ):
                return None
            # Read while holding the state lock.  Retry/config-generation can
            # otherwise replace the fixed path between the generation check
            # and read, allowing an old job ID to receive the new job's QR.
            try:
                payload = job.qr_path.read_bytes()
            except (FileNotFoundError, OSError):
                return None
            if not self._is_complete_png(payload):
                return None
            return payload

    async def shutdown(self) -> None:
        """Terminate and clean the current child and published image."""
        async with self._lifecycle_lock:
            self._closed = True
            try:
                await self._stop_current_unlocked()
            except BilibiliLoginCleanupError:
                # Shutdown must still release the child and mark the cleanup
                # failure without returning a path or filesystem exception.
                self._cleanup_failed = True
            except BilibiliLoginProcessError:
                # Shutdown is best effort, but retain the failure so startup
                # cannot make an unproven prior process look clean.
                self._lifecycle_failed = True
            except Exception:
                # A best-effort shutdown must still attempt artifact cleanup
                # if a process wrapper reports an unexpected failure.
                self._lifecycle_failed = True
            try:
                self._cleanup_qr()
            except BilibiliLoginCleanupError:
                self._cleanup_failed = True
            self._config_ready = False
            self._dependencies_ready = False

    async def _run(self, job: _LoginJob) -> None:
        process = None
        try:
            # The command and cwd are constants owned by this module.  Child
            # output is discarded so neither cookies nor login payloads can
            # enter logs.
            process_factory = self._process_factory or asyncio.create_subprocess_exec
            process = await process_factory(
                *self.command,
                cwd=str(self.adapter_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with self._state_lock:
                if self._current is not job:
                    stale = True
                else:
                    stale = False
                    job.process = process
                    job.status = "waiting"
                    job.message = "请使用 Bilibili App 扫描并确认二维码"
            if stale:
                await self._terminate_process(process)
                return

            return_code = await process.wait()
            async with self._state_lock:
                if self._current is not job:
                    return
            if return_code == 0 and self._config_has_credentials():
                await self._finish(job, "success", "Bilibili 登录成功，凭据已写入配置")
            elif return_code == 0:
                await self._finish(job, "error", "登录进程完成，但凭据校验未通过，请重试")
            else:
                await self._finish(job, "error", "二维码登录失败或已过期，请重试")
        except asyncio.CancelledError:
            # Cancellation is expected during retry/config regeneration/shutdown.
            if process is not None:
                try:
                    await self._terminate_process(process)
                except BilibiliLoginProcessError:
                    await self._mark_process_failure(job)
                except Exception:
                    await self._mark_process_failure(job)
            raise
        except FileNotFoundError:
            await self._finish(job, "error", "Bilibili 适配器运行环境无法启动，请重试")
        except Exception:
            # Do not include exception text: child/setup paths and credentials
            # must never leak through an API error or WebUI log.
            await self._finish(job, "error", "Bilibili 登录启动失败，请重试")
        finally:
            async with self._state_lock:
                is_current = self._current is job
            if is_current and job.status in {"success", "error", "expired"}:
                try:
                    self._cleanup_qr()
                except BilibiliLoginCleanupError:
                    # The terminal status remains safe and QR reads are already
                    # disabled; a later start/retry will fail closed on cleanup.
                    pass

    async def _finish(self, job: _LoginJob, status: str, message: str) -> None:
        async with self._state_lock:
            if self._current is job:
                job.status = status
                job.message = message

    async def _stop_current_unlocked(self) -> None:
        async with self._state_lock:
            job = self._current
        if job is None:
            self._cleanup_qr()
            return

        try:
            process = job.process
            if process is not None:
                await self._terminate_process(process)
            task = job.task
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=self._stop_timeout)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    if not task.done():
                        raise BilibiliLoginProcessError(
                            "Bilibili 登录进程无法确认退出"
                        ) from None

            # A process can be attached while a just-cancelled factory task is
            # unwinding. Re-read it after task cancellation and prove that the
            # descendant exited before releasing the current job.
            latest_process = job.process
            if latest_process is not None and latest_process is not process:
                await self._terminate_process(latest_process)
        except BilibiliLoginProcessError:
            await self._mark_process_failure(job)
            raise
        except Exception:
            await self._mark_process_failure(job)
            raise BilibiliLoginProcessError(
                "Bilibili 登录进程无法确认退出"
            ) from None

        async with self._state_lock:
            if self._current is job:
                self._current = None
        self._lifecycle_failed = False
        self._cleanup_qr()

    async def _terminate_process(self, process: Any) -> None:
        if self._process_has_exited(process):
            return
        try:
            process.terminate()
        except Exception:
            pass
        if await self._wait_for_process_exit(process):
            return
        try:
            process.kill()
        except Exception:
            pass
        if await self._wait_for_process_exit(process):
            return
        raise BilibiliLoginProcessError(
            "Bilibili 登录进程无法确认退出"
        ) from None

    @staticmethod
    def _process_has_exited(process: Any) -> bool:
        try:
            return getattr(process, "returncode", None) is not None
        except Exception:
            return False

    async def _wait_for_process_exit(self, process: Any) -> bool:
        if self._process_has_exited(process):
            return True
        try:
            waiter = process.wait()
            await asyncio.wait_for(waiter, timeout=self._stop_timeout)
        except asyncio.CancelledError:
            return False
        except Exception:
            return False
        return True

    async def _mark_process_failure(self, job: _LoginJob) -> None:
        self._lifecycle_failed = True
        async with self._state_lock:
            if self._current is job:
                job.status = "error"
                job.message = "Bilibili 登录进程无法确认退出，请重试"

    def _snapshot_unlocked(self, job: _LoginJob) -> dict[str, Any]:
        qr_ready = job.status in {"starting", "waiting"} and self._is_complete_png_path(job.qr_path)
        terminal = job.status in {"success", "error", "expired"}
        return {
            "job_id": job.job_id,
            "status": job.status,
            "state": job.status,
            "qr_ready": qr_ready,
            "qr_available": qr_ready,
            "terminal": terminal,
            "retryable": terminal and job.status != "success",
            "cleanup_failed": self._cleanup_failed,
            "lifecycle_failed": self._lifecycle_failed,
            "message": job.message,
        }

    def _config_has_credentials(self) -> bool:
        try:
            document = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
            section = document.get("bilibili", {})
            return all(
                isinstance(section.get(name), str) and section.get(name, "").strip()
                for name in REQUIRED_CREDENTIAL_FIELDS
            )
        except Exception:
            return False

    def _cleanup_qr(self) -> None:
        try:
            self.qr_path.unlink(missing_ok=True)
        except Exception:
            self._cleanup_failed = True
            raise BilibiliLoginCleanupError("二维码清理失败") from None
        self._cleanup_failed = False

    @classmethod
    def _is_complete_png_path(cls, path: Path) -> bool:
        try:
            return cls._is_complete_png(path.read_bytes())
        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    def _is_complete_png(payload: bytes) -> bool:
        if not payload.startswith(QR_PNG_SIGNATURE):
            return False
        offset = len(QR_PNG_SIGNATURE)
        length = len(payload)
        while offset + 12 <= length:
            chunk_size = struct.unpack(">I", payload[offset : offset + 4])[0]
            end = offset + 12 + chunk_size
            if end > length:
                return False
            chunk_type = payload[offset + 4 : offset + 8]
            offset = end
            if chunk_type == b"IEND":
                return offset == length
        return False


bilibili_login_manager = BilibiliLoginManager()

__all__ = [
    "BilibiliLoginManager",
    "BilibiliLoginCleanupError",
    "BilibiliLoginProcessError",
    "BilibiliLoginNotReady",
    "bilibili_login_manager",
]
