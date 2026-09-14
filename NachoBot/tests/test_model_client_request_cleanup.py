from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from src.config.api_ada_configs import ModelInfo
from src.llm_models.exceptions import ReqAbortException
from src.llm_models.model_client.base_client import (
    _DETACHED_REQUEST_TASKS,
    _cancel_and_drain_request_task,
)
from src.llm_models.model_client.gemini_client import GeminiClient
from src.llm_models.model_client.openai_client import OpenaiClient
from src.llm_models.payload_content.message import Message, RoleType


class _PendingRequest:
    def __init__(self, *, resistant: float = 0.0, error: BaseException | None = None) -> None:
        self.resistant = resistant
        self.error = error
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None

    async def __call__(self, **kwargs: Any) -> Any:
        del kwargs
        self.task = asyncio.current_task()
        self.started.set()
        try:
            if self.error is not None:
                raise self.error
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.resistant:
                await asyncio.sleep(self.resistant)
                raise RuntimeError("resistant request failure") from None
            raise
        finally:
            self.finished.set()


class _OpenAICompletions:
    def __init__(self, request: Callable[..., Awaitable[Any]]) -> None:
        self.create = request


class _OpenAIClientStub:
    def __init__(self, request: Callable[..., Awaitable[Any]]) -> None:
        self.chat = SimpleNamespace(completions=_OpenAICompletions(request))


class _GeminiModels:
    def __init__(self, request: Callable[..., Awaitable[Any]]) -> None:
        self.generate_content = request
        self.generate_content_stream = request


class _GeminiClientStub:
    def __init__(self, request: Callable[..., Awaitable[Any]]) -> None:
        self.aio = SimpleNamespace(models=_GeminiModels(request))


class ModelClientRequestCleanupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _model(*, stream: bool = False) -> ModelInfo:
        return ModelInfo(
            model_identifier="test-model",
            name="test-model",
            api_provider="test-provider",
            force_stream_mode=stream,
        )

    @staticmethod
    def _messages() -> list[Message]:
        return [Message(RoleType.User, "hello")]

    async def test_shared_helper_drains_normal_cancelled_task(self) -> None:
        finished = asyncio.Event()

        async def request() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                finished.set()
                raise

        task = asyncio.create_task(request())
        await asyncio.sleep(0)
        await _cancel_and_drain_request_task(task, timeout=0.2)

        self.assertTrue(finished.is_set())
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        self.assertNotIn(task, _DETACHED_REQUEST_TASKS)

    async def test_shared_helper_tracks_resistant_task_and_consumes_exception(self) -> None:
        finished = asyncio.Event()
        loop_errors: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def request() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                finished.set()
                raise RuntimeError("late request failure") from None

        try:
            task = asyncio.create_task(request())
            await asyncio.sleep(0)
            await _cancel_and_drain_request_task(task, timeout=0.05)
            self.assertFalse(task.done())
            self.assertIn(task, _DETACHED_REQUEST_TASKS)

            await asyncio.wait_for(finished.wait(), timeout=1.0)
            await asyncio.sleep(0)
            self.assertTrue(task.done())
            self.assertNotIn(task, _DETACHED_REQUEST_TASKS)
            self.assertEqual(loop_errors, [])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_helper_preserves_caller_cancel_when_child_finishes_concurrently(self) -> None:
        started = asyncio.Event()

        async def request() -> None:
            started.set()
            await asyncio.Event().wait()

        child = asyncio.create_task(request())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        cleanup = asyncio.create_task(_cancel_and_drain_request_task(child, timeout=1.0))
        await asyncio.sleep(0)
        cleanup.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(cleanup, timeout=1.0)
        self.assertTrue(child.done())
        self.assertTrue(child.cancelled())

    async def test_openai_interrupt_drains_nested_request_before_abort(self) -> None:
        request = _PendingRequest()
        client = object.__new__(OpenaiClient)
        client.client = _OpenAIClientStub(request)
        interrupt = asyncio.Event()
        call = asyncio.create_task(
            client.get_response(
                self._model(),
                self._messages(),
                interrupt_flag=interrupt,
            )
        )

        await asyncio.wait_for(request.started.wait(), timeout=1.0)
        interrupt.set()
        with self.assertRaises(ReqAbortException):
            await asyncio.wait_for(call, timeout=1.0)

        self.assertTrue(request.cancelled.is_set())
        self.assertTrue(request.finished.is_set())
        self.assertIsNotNone(request.task)
        self.assertTrue(request.task.done())

    async def test_openai_outer_cancellation_drains_nested_request(self) -> None:
        request = _PendingRequest()
        client = object.__new__(OpenaiClient)
        client.client = _OpenAIClientStub(request)
        call = asyncio.create_task(client.get_response(self._model(), self._messages()))

        await asyncio.wait_for(request.started.wait(), timeout=1.0)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=1.0)

        self.assertTrue(request.cancelled.is_set())
        self.assertTrue(request.finished.is_set())
        self.assertIsNotNone(request.task)
        self.assertTrue(request.task.done())

    async def test_openai_resistant_request_is_bounded_and_exception_consumed(self) -> None:
        request = _PendingRequest(resistant=0.3)
        client = object.__new__(OpenaiClient)
        client.client = _OpenAIClientStub(request)
        interrupt = asyncio.Event()
        loop_errors: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        try:
            call = asyncio.create_task(
                client.get_response(
                    self._model(),
                    self._messages(),
                    interrupt_flag=interrupt,
                )
            )
            await asyncio.wait_for(request.started.wait(), timeout=1.0)
            interrupt.set()
            with self.assertRaises(ReqAbortException):
                await asyncio.wait_for(call, timeout=1.0)

            self.assertIsNotNone(request.task)
            self.assertFalse(request.task.done())
            await asyncio.wait_for(request.finished.wait(), timeout=1.0)
            await asyncio.sleep(0)
            self.assertTrue(request.task.done())
            self.assertEqual(loop_errors, [])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_gemini_interrupt_drains_nested_request_before_abort(self) -> None:
        request = _PendingRequest()
        client = object.__new__(GeminiClient)
        client.client = _GeminiClientStub(request)
        interrupt = asyncio.Event()
        call = asyncio.create_task(
            client.get_response(
                self._model(stream=True),
                self._messages(),
                interrupt_flag=interrupt,
            )
        )

        await asyncio.wait_for(request.started.wait(), timeout=1.0)
        interrupt.set()
        with self.assertRaises(ReqAbortException):
            await asyncio.wait_for(call, timeout=1.5)

        self.assertTrue(request.cancelled.is_set())
        self.assertTrue(request.finished.is_set())
        self.assertIsNotNone(request.task)
        self.assertTrue(request.task.done())

    async def test_gemini_preserves_req_abort_instead_of_retry_translation(self) -> None:
        request = _PendingRequest(error=ReqAbortException("already aborted"))
        client = object.__new__(GeminiClient)
        client.client = _GeminiClientStub(request)

        with self.assertRaises(ReqAbortException):
            await client.get_response(self._model(), self._messages())

        self.assertTrue(request.started.is_set())
        self.assertTrue(request.finished.is_set())
        self.assertIsNotNone(request.task)
        self.assertTrue(request.task.done())


if __name__ == "__main__":
    unittest.main()
