from __future__ import annotations

import asyncio
import base64
import importlib.util
import threading
import time
from types import ModuleType
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_utils_without_application_side_effects():
    missing = object()
    names = (
        "src",
        "src.database",
        "src.logger",
        "src.response_pool",
        "src._napcat_image_utils_qa",
        "websockets",
        "PIL",
    )
    previous = {name: sys.modules.get(name, missing) for name in names}

    src_stub = ModuleType("src")
    src_stub.__path__ = []
    database_stub = ModuleType("src.database")
    database_stub.BanUser = object
    database_stub.db_manager = object()
    logger_stub = ModuleType("src.logger")

    class NoOpLogger:
        def debug(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    logger_stub.logger = NoOpLogger()
    response_pool_stub = ModuleType("src.response_pool")

    async def unused_get_response(*_args, **_kwargs):
        return None

    response_pool_stub.get_response = unused_get_response
    websockets_stub = ModuleType("websockets")
    websockets_stub.ServerConnection = type("ServerConnection", (), {})
    pil_stub = ModuleType("PIL")
    pil_stub.Image = object()
    replacements = {
        "src": src_stub,
        "src.database": database_stub,
        "src.logger": logger_stub,
        "src.response_pool": response_pool_stub,
        "websockets": websockets_stub,
        "PIL": pil_stub,
    }
    sys.modules.update(replacements)
    try:
        spec = importlib.util.spec_from_file_location(
            "src._napcat_image_utils_qa",
            PROJECT_ROOT / "src" / "utils.py",
        )
        if spec is None or spec.loader is None:
            raise AssertionError("utils_source_loader_unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


utils = _load_utils_without_application_side_effects()


class _Response:
    def __init__(self, status: int = 200, data: bytes = b"image-bytes"):
        self.status = status
        self.data = data


def test_slow_http_and_tls_setup_do_not_block_event_loop(monkeypatch):
    worker_threads: list[int] = []
    request_args: list[tuple[str, str, int]] = []
    loop_ticks = 0

    class SlowHttp:
        def __init__(self):
            worker_threads.append(threading.get_ident())
            time.sleep(0.03)

        def request(self, method: str, url: str, *, timeout: int):
            worker_threads.append(threading.get_ident())
            request_args.append((method, url, timeout))
            time.sleep(0.12)
            return _Response(data=b"image-bytes")

    monkeypatch.setattr(utils, "SSLAdapter", SlowHttp)

    async def scenario() -> tuple[str, int, int]:
        nonlocal loop_ticks
        loop_thread = threading.get_ident()
        running = True

        async def ticker() -> None:
            nonlocal loop_ticks
            while running:
                await asyncio.sleep(0.005)
                loop_ticks += 1

        tick_task = asyncio.create_task(ticker())
        try:
            result = await asyncio.wait_for(
                utils.get_image_base64("https://images.invalid/fake.png"),
                timeout=2,
            )
        finally:
            running = False
            tick_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tick_task
        return result, loop_thread, loop_ticks

    result, loop_thread, ticks = asyncio.run(scenario())
    assert result == base64.b64encode(b"image-bytes").decode("utf-8")
    assert ticks >= 8
    assert worker_threads and all(thread_id != loop_thread for thread_id in worker_threads)
    assert request_args == [("GET", "https://images.invalid/fake.png", 10)]


def test_http_status_error_is_preserved(monkeypatch):
    class ErrorHttp:
        def request(self, method: str, url: str, *, timeout: int):
            return _Response(status=503)

    monkeypatch.setattr(utils, "SSLAdapter", ErrorHttp)

    with pytest.raises(Exception, match="HTTP Error: 503"):
        asyncio.run(utils.get_image_base64("https://images.invalid/unavailable.png"))


def test_request_exception_identity_is_preserved(monkeypatch):
    failure = OSError("fake request failure")

    class ErrorHttp:
        def request(self, method: str, url: str, *, timeout: int):
            raise failure

    monkeypatch.setattr(utils, "SSLAdapter", ErrorHttp)

    with pytest.raises(OSError) as caught:
        asyncio.run(utils.get_image_base64("https://images.invalid/failure.png"))
    assert caught.value is failure


def test_cancelled_caller_keeps_worker_slot_until_native_request_finishes(monkeypatch):
    request_started = threading.Event()
    release_first_request = threading.Event()
    request_lock = threading.Lock()
    active_requests = 0
    max_active_requests = 0
    request_count = 0
    submit_count = 0
    original_submit = utils._IMAGE_DOWNLOAD_EXECUTOR.submit

    def counted_submit(*args, **kwargs):
        nonlocal submit_count
        submit_count += 1
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(utils._IMAGE_DOWNLOAD_EXECUTOR, "submit", counted_submit)

    class BlockingHttp:
        def request(self, method: str, url: str, *, timeout: int):
            nonlocal active_requests, max_active_requests, request_count
            with request_lock:
                request_count += 1
                ordinal = request_count
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
            try:
                if ordinal == 1:
                    request_started.set()
                    if not release_first_request.wait(timeout=2):
                        raise TimeoutError("test request release timed out")
                return _Response(data=f"image-{ordinal}".encode("ascii"))
            finally:
                with request_lock:
                    active_requests -= 1

    monkeypatch.setattr(utils, "SSLAdapter", BlockingHttp)

    async def wait_for_thread_event(event: threading.Event, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not event.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("worker_event_timeout")
            await asyncio.sleep(0.005)

    async def scenario() -> None:
        first = asyncio.create_task(utils.get_image_base64("https://images.invalid/first.png"))
        await wait_for_thread_event(request_started)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        queued = [
            asyncio.create_task(utils.get_image_base64(f"https://images.invalid/queued-{index}.png"))
            for index in range(3)
        ]
        await asyncio.sleep(0.05)
        assert request_count == 1
        assert submit_count == 1

        release_first_request.set()
        results = await asyncio.wait_for(asyncio.gather(*queued), timeout=3)
        assert results == [
            base64.b64encode(b"image-2").decode("utf-8"),
            base64.b64encode(b"image-3").decode("utf-8"),
            base64.b64encode(b"image-4").decode("utf-8"),
        ]

    asyncio.run(scenario())
    assert request_count == 4
    assert submit_count == 4
    assert max_active_requests == 1
