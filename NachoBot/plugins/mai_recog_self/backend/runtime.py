"""Torch-free host adapter for a persistent, isolated inference worker."""

from __future__ import annotations

from pathlib import Path
import base64
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import quote

from .paths import CORE_ROOT
from .environment import select_worker_python, worker_environment
from .diagnostics import runtime_event, worker_error_summary


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CAPTURED_STDERR_BYTES = 8192


class _StderrCapture:
    """Tee one worker's stderr to inference.log and retain only a bounded tail."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._lock = threading.Lock()
        self._tail = bytearray()
        self._thread: threading.Thread | None = None

    def start(self, stream) -> None:
        if stream is None:
            return
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream) -> None:
        try:
            try:
                log = self.log_path.open("ab", buffering=0)
            except OSError:
                log = open(os.devnull, "ab", buffering=0)
            with log:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    try:
                        log.write(chunk)
                    except OSError:
                        pass
                    with self._lock:
                        self._tail.extend(chunk)
                        if len(self._tail) > MAX_CAPTURED_STDERR_BYTES:
                            del self._tail[:-MAX_CAPTURED_STDERR_BYTES]
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def finish(self, timeout: float = 0.25) -> str | None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            tail = bytes(self._tail)
        return worker_error_summary(tail)


def _setting(root: Path, key: str, default: str = "") -> str:
    database = root / "catalog.sqlite3"
    if not database.is_file():
        return default
    uri = "file:" + quote(database.as_posix(), safe="/:\\") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.25)
        try:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default
        finally:
            connection.close()
    except sqlite3.Error:
        return default


def _select_python(root: Path, explicit: str) -> str:
    return select_worker_python(root, explicit)


def _command(python: str, root: Path) -> list[str]:
    return [
        python,
        "-X",
        "utf8",
        "-u",
        "-m",
        "plugins.mai_recog_self.backend.worker",
        "serve",
        str(root),
    ]


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


class InferenceService:
    """Synchronous inference facade; construction never imports torch.

    A worker warms in the background with a separate 45-second budget. Chat
    callers keep their own short deadline: if the worker is still warming,
    infer returns ``busy`` and leaves the single warm worker alive for reuse.
    """

    def __init__(self, root: Path, worker_python: str = ""):
        self.root = Path(root).expanduser().resolve()
        self._worker_python_override = worker_python
        # Configured paths are validated immediately. Automatic probing runs
        # inside the existing warmup thread so the first image does not block
        # the event loop while Torch and torchvision import.
        self._worker_python_selected = bool(worker_python)
        self.worker_python = (
            _select_python(self.root, worker_python)
            if worker_python
            else str(Path(sys.executable).absolute())
        )
        self._gate = threading.RLock()
        self._io_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._stderr_capture: _StderrCapture | None = None
        self._closed = False
        self._generation = 0
        self._ready = False
        self._loaded_version = ""
        self._failed_candidate_version = ""
        self._last_error = ""
        self._warm_thread: threading.Thread | None = None
        if worker_python:
            runtime_event("worker_selected", worker=self.worker_python, status="explicit")
        if self._desired():
            self._schedule_warmup()

    def _desired(self) -> str:
        return _setting(self.root, "active")

    def _schedule_warmup(self) -> bool:
        with self._gate:
            if self._closed or (self._warm_thread and self._warm_thread.is_alive()):
                return False
            desired = self._desired()
            if not desired:
                return False
            if self._failed_candidate_version == desired and self._last_error:
                return False
            generation = self._generation
            thread = threading.Thread(target=self._warm_worker, args=(generation, desired), daemon=True)
            self._warm_thread = thread
            thread.start()
            return True

    def _spawn(self) -> subprocess.Popen:
        log_path = self.root / "inference.log"
        capture = _StderrCapture(log_path)
        # Associate each attempted launch with a fresh capture before Popen so
        # an exception here cannot accidentally reuse a prior worker's tail.
        with self._gate:
            self._stderr_capture = capture
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        process = subprocess.Popen(
            _command(self.worker_python, self.root),
            cwd=CORE_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            creationflags=flags,
            env=worker_environment(self.root),
            **kwargs,
        )
        capture.start(getattr(process, "stderr", None))
        with self._gate:
            if self._closed:
                _stop_process(process)
                raise RuntimeError("推理服务已关闭")
            self._process = process
            self._stderr_capture = capture
        runtime_event("worker_started", worker=self.worker_python, desired_version=self._desired())
        return process

    def _exchange(self, process: subprocess.Popen, request: dict | None, timeout: float) -> dict | None:
        output: list[bytes] = []
        payload = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8") if request else b""

        def io() -> None:
            try:
                if payload:
                    view = memoryview(payload)
                    while view:
                        written = process.stdin.write(view)
                        if not written:
                            raise BrokenPipeError("推理worker请求管道提前关闭")
                        view = view[written:]
                    process.stdin.flush()
                output.append(process.stdout.readline())
            except (BrokenPipeError, OSError, ValueError):
                output.append(b"")

        thread = threading.Thread(target=io, daemon=True)
        thread.start()
        thread.join(max(0.0, timeout))
        if thread.is_alive() or not output or not output[0]:
            _stop_process(process)
            thread.join(0.5)
            return None
        try:
            result = json.loads(output[0].decode("utf-8"))
        except (UnicodeError, ValueError):
            return {"status": "model_error", "error": "推理worker返回了无效JSON"}
        return result if isinstance(result, dict) else {"status": "model_error", "error": "推理worker返回格式错误"}

    def _warm_worker(self, generation: int, desired: str) -> None:
        with self._io_lock:
            with self._gate:
                if self._closed or generation != self._generation:
                    return
                process = self._process
            try:
                if not self._worker_python_selected:
                    selected = _select_python(self.root, self._worker_python_override)
                    with self._gate:
                        if self._closed or generation != self._generation:
                            return
                        self.worker_python = selected
                        self._worker_python_selected = True
                    runtime_event("worker_selected", worker=selected, status="automatic")
                if process is None or process.poll() is not None:
                    process = self._spawn()
                result = self._exchange(process, None, 45.0)
                capture = self._stderr_capture
                launch_error = capture.finish() if capture is not None else None
                with self._gate:
                    if self._closed or generation != self._generation:
                        return
                    if result is None:
                        self._process = None
                        self._ready = False
                        self._loaded_version = ""
                        self._failed_candidate_version = desired
                        self._last_error = launch_error or "推理worker启动超过45秒或提前退出"
                        runtime_event(
                            "worker_start_failed",
                            level=logging.WARNING,
                            worker=self.worker_python,
                            desired_version=desired,
                            status="timeout_or_exit",
                            error=self._last_error,
                        )
                        return
                    self._ready = result.get("status") in {"ready", "no_model", "model_error"}
                    self._loaded_version = str(result.get("loaded_version") or result.get("version") or "")
                    if result.get("status") == "model_error":
                        self._failed_candidate_version = str(result.get("desired_version") or desired)
                        self._last_error = str(result.get("error") or "模型加载失败")
                        runtime_event(
                            "worker_start_failed",
                            level=logging.WARNING,
                            worker=self.worker_python,
                            desired_version=desired,
                            actual_version=self._loaded_version,
                            status="model_error",
                            error=self._last_error or launch_error,
                        )
                    else:
                        self._last_error = ""
                        self._failed_candidate_version = ""
                        runtime_event(
                            "worker_ready",
                            worker=self.worker_python,
                            desired_version=desired,
                            actual_version=self._loaded_version,
                            status=str(result.get("status") or "ready"),
                        )
            except Exception as exc:
                capture = self._stderr_capture
                launch_error = capture.finish() if capture is not None else None
                error = launch_error or str(exc)
                with self._gate:
                    if generation == self._generation:
                        self._process = None
                        self._ready = False
                        self._loaded_version = ""
                        self._failed_candidate_version = desired
                        self._last_error = error
                        runtime_event(
                            "worker_start_failed",
                            level=logging.WARNING,
                            worker=self.worker_python,
                            desired_version=desired,
                            status="exception",
                            error=error,
                        )

    def _prepare_worker(self, desired: str) -> tuple[subprocess.Popen | None, str | None]:
        with self._gate:
            if self._closed:
                return None, "stopped"
            process = self._process
            if (
                self._failed_candidate_version == desired
                and self._last_error
                and not self._loaded_version
            ):
                return None, "model_error"
            if process is not None and process.poll() is None and self._ready:
                return process, None
            if process is not None and process.poll() is not None:
                self._process = None
                self._ready = False
                self._loaded_version = ""
                if not self._last_error:
                    self._last_error = "推理worker已退出"
                    self._failed_candidate_version = desired
            if self._failed_candidate_version == desired and self._last_error:
                return None, "model_error"
            self._schedule_warmup()
            return None, "busy"

    def infer(self, raw: bytes, timeout: float = 6) -> dict:
        started = time.monotonic()
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_IMAGE_BYTES:
            return {"status": "model_error", "error": "图片为空或超过20MiB"}
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            return {"status": "busy", "error": "推理时间预算无效"}
        with self._gate:
            if self._closed:
                return {"status": "stopped"}
            generation = self._generation
        desired = self._desired()
        if not desired:
            return {"status": "no_model", "desired_version": "", "loaded_version": self._loaded_version}
        process, failure = self._prepare_worker(desired)
        if failure:
            return {"status": failure, "desired_version": desired, "loaded_version": self._loaded_version}
        budget = max(0.0, float(timeout) - (time.monotonic() - started))
        if not self._io_lock.acquire(timeout=budget):
            return {"status": "busy", "desired_version": desired, "loaded_version": self._loaded_version}
        try:
            with self._gate:
                if self._closed or generation != self._generation:
                    return {"status": "stopped"}
                if process is not self._process or process.poll() is not None:
                    return {"status": "busy", "desired_version": desired, "loaded_version": self._loaded_version}
                loaded = self._loaded_version
                failed_candidate = self._failed_candidate_version
            version = loaded if failed_candidate == desired and loaded else desired
            remaining = max(0.0, float(timeout) - (time.monotonic() - started))
            result = self._exchange(
                process,
                {"version": version, "image": base64.b64encode(raw).decode("ascii")},
                remaining,
            )
            if result is None:
                capture = self._stderr_capture
                worker_error = capture.finish(timeout=0.5) if capture is not None else None
                with self._gate:
                    if generation == self._generation:
                        self._process = None
                        self._ready = False
                        self._loaded_version = ""
                        self._failed_candidate_version = desired
                        self._last_error = worker_error or f"推理worker超过{timeout:g}秒无响应"
                        runtime_event(
                            "worker_inference_failed",
                            level=logging.WARNING,
                            worker=self.worker_python,
                            desired_version=desired,
                            actual_version=loaded,
                            status="timeout",
                            error=self._last_error,
                        )
                return {"status": "model_error", "error": self._last_error, "desired_version": desired, "loaded_version": ""}
            with self._gate:
                if self._closed or generation != self._generation:
                    return {"status": "stopped"}
                worker_loaded = str(result.get("loaded_version") or result.get("version") or "")
                if worker_loaded:
                    self._loaded_version = worker_loaded
                if result.get("status") == "ok":
                    if worker_loaded == desired:
                        self._failed_candidate_version = ""
                        self._last_error = ""
                    elif self._failed_candidate_version == desired:
                        result["model_error"] = self._last_error
                    result["desired_version"] = desired
                    result["loaded_version"] = self._loaded_version
                    return result
                if result.get("status") == "model_error" and self._loaded_version and self._loaded_version != desired:
                    self._failed_candidate_version = desired
                    self._last_error = str(result.get("error") or "候选模型加载失败")
                    runtime_event(
                        "model_load_failed",
                        level=logging.WARNING,
                        worker=self.worker_python,
                        desired_version=desired,
                        actual_version=self._loaded_version,
                        status="fallback",
                        error=self._last_error,
                    )
                    fallback_version = self._loaded_version
                elif result.get("status") == "model_error":
                    self._failed_candidate_version = desired
                    self._last_error = str(result.get("error") or "模型推理失败")
                    runtime_event(
                        "worker_inference_failed",
                        level=logging.WARNING,
                        worker=self.worker_python,
                        desired_version=desired,
                        actual_version=self._loaded_version,
                        status="model_error",
                        error=self._last_error,
                    )
                    result["desired_version"] = desired
                    result["loaded_version"] = self._loaded_version
                    return result
                else:
                    result["desired_version"] = desired
                    result["loaded_version"] = self._loaded_version
                    return result
            remaining = max(0.0, float(timeout) - (time.monotonic() - started))
            fallback = self._exchange(
                process,
                {"version": fallback_version, "image": base64.b64encode(raw).decode("ascii")},
                remaining,
            )
            if fallback and fallback.get("status") == "ok":
                fallback["desired_version"] = desired
                fallback["loaded_version"] = fallback_version
                fallback["model_error"] = self._last_error
                return fallback
            return {
                "status": "model_error",
                "error": self._last_error or "候选模型和已加载模型均不可用",
                "desired_version": desired,
                "loaded_version": self._loaded_version,
            }
        finally:
            self._io_lock.release()

    def status(self) -> dict:
        with self._gate:
            if self._process is not None and self._process.poll() is not None:
                self._process = None
                self._ready = False
                self._loaded_version = ""
            return {
                "desired_version": self._desired(),
                "loaded_version": self._loaded_version,
                "failed_candidate_version": self._failed_candidate_version,
                "last_error": self._last_error,
                "ready": self._ready,
                "warming": bool(self._warm_thread and self._warm_thread.is_alive()),
                "worker_python": self.worker_python,
                "closed": self._closed,
            }

    def close(self) -> None:
        with self._gate:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            process = self._process
            self._process = None
            self._ready = False
            self._loaded_version = ""
        _stop_process(process)
        capture = self._stderr_capture
        if capture is not None:
            capture.finish(timeout=0.5)
        thread = self._warm_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.0)
