from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable

from src.chat.sandbox.sandbox_agent import (
    SandboxAgent,
    SandboxAgentConfig,
    SandboxAgentOutcome,
    SandboxProvider,
    SandboxErrorCode,
)
from src.chat.sandbox.sandbox_handoff import SandboxEditCandidate, SandboxEditHandoff
from src.chat.sandbox.sandbox_manager import SandboxManager


class _ToolCall:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.func_name = name
        self.args = args


def _call(name: str, **args: Any) -> _ToolCall:
    return _ToolCall(name, args)


class _FakeLLM:
    def __init__(self, responses: Iterable[Any], name: str) -> None:
        self.responses = iter(responses)
        self.name = name
        self.calls = 0
        self.prompts: list[str] = []

    async def generate_response_async(self, *, prompt: str, tools: Any, raise_when_empty: bool) -> tuple[str, tuple[str, str, list[Any]]]:
        del tools, raise_when_empty
        self.calls += 1
        self.prompts.append(prompt)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return "", ("", self.name, list(response))


class _StreamLLM:
    def __init__(self, delay: float, name: str, events: list[dict[str, Any]]) -> None:
        self.delay = delay
        self.name = name
        self.events = events
        self.calls = 0

    async def generate_response_stream_async(
        self,
        *,
        prompt: str,
        tools: Any,
        raise_when_empty: bool,
        interrupt_flag: asyncio.Event | None = None,
        on_delta: Any = None,
    ) -> Any:
        del prompt, tools, raise_when_empty, interrupt_flag, on_delta

        async def stream() -> Any:
            self.calls += 1
            await asyncio.sleep(self.delay)
            for event in self.events:
                yield event

        return stream()


class _CallbackTupleLLM:
    """A provider-shaped tuple response that reports raw stream deltas."""

    def __init__(self, events: list[dict[str, Any]], name: str) -> None:
        self.events = events
        self.name = name
        self.calls = 0
        self.interrupt_flag: asyncio.Event | None = None

    async def generate_response_stream_async(
        self,
        *,
        prompt: str,
        tools: Any,
        raise_when_empty: bool,
        interrupt_flag: asyncio.Event | None = None,
        on_delta: Any = None,
    ) -> tuple[str, tuple[str, str, list[Any]]]:
        del prompt, tools, raise_when_empty
        self.calls += 1
        self.interrupt_flag = interrupt_flag
        for event in self.events:
            if on_delta is not None:
                on_delta(event)
            await asyncio.sleep(0)
        return "", ("", self.name, [])


class _CallbackDirectStreamLLM(_StreamLLM):
    """A direct iterator that also reports each item through the callback."""

    async def generate_response_stream_async(
        self,
        *,
        prompt: str,
        tools: Any,
        raise_when_empty: bool,
        interrupt_flag: asyncio.Event | None = None,
        on_delta: Any = None,
    ) -> Any:
        del prompt, tools, raise_when_empty, interrupt_flag

        async def stream() -> Any:
            self.calls += 1
            for event in self.events:
                if on_delta is not None:
                    on_delta(event)
                yield event

        return stream()


class _KeepaliveLLM:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def generate_response_stream_async(
        self,
        *,
        prompt: str,
        tools: Any,
        raise_when_empty: bool,
        interrupt_flag: asyncio.Event | None = None,
        on_delta: Any = None,
    ) -> Any:
        del prompt, tools, raise_when_empty, interrupt_flag, on_delta

        async def stream() -> Any:
            self.calls += 1
            while True:
                yield {"usage": {}}
                await asyncio.sleep(0.1)

        return stream()


class _PacedStreamLLM:
    def __init__(self, steps: list[tuple[float, dict[str, Any]]], name: str) -> None:
        self.steps = steps
        self.name = name
        self.calls = 0

    async def generate_response_stream_async(
        self,
        *,
        prompt: str,
        tools: Any,
        raise_when_empty: bool,
        interrupt_flag: asyncio.Event | None = None,
        on_delta: Any = None,
    ) -> Any:
        del prompt, tools, raise_when_empty, interrupt_flag, on_delta

        async def stream() -> Any:
            self.calls += 1
            for delay, event in self.steps:
                await asyncio.sleep(delay)
                yield event

        return stream()


class _ControlledStream:
    """A stream that blocks until the watchdog cancels its consumer."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.consumer_finished = asyncio.Event()
        self._blocked = asyncio.Event()
        self._yielded = False
        self.close_count = 0
        self.consumer_task: asyncio.Task[Any] | None = None

    def __aiter__(self) -> "_ControlledStream":
        return self

    async def __anext__(self) -> dict[str, str]:
        self.consumer_task = asyncio.current_task()
        try:
            if not self._yielded:
                self._yielded = True
                self.started.set()
                return {"content": "started"}
            await self._blocked.wait()
        finally:
            self.consumer_finished.set()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_count += 1
        self._blocked.set()


class _ControlledStreamLLM:
    def __init__(self, stream: _ControlledStream, name: str) -> None:
        self.stream = stream
        self.name = name
        self.calls = 0

    async def generate_response_stream_async(
        self,
        *,
        prompt: str,
        tools: Any,
        raise_when_empty: bool,
        interrupt_flag: asyncio.Event | None = None,
        on_delta: Any = None,
    ) -> Any:
        del prompt, tools, raise_when_empty, interrupt_flag, on_delta
        self.calls += 1
        return self.stream


class _CloseCheckingFallbackLLM:
    def __init__(self, first_stream: _ControlledStream) -> None:
        self.first_stream = first_stream
        self.name = "fallback"
        self.calls = 0
        self.close_count_at_call: int | None = None

    async def generate_response_stream_async(
        self,
        *,
        prompt: str,
        tools: Any,
        raise_when_empty: bool,
        interrupt_flag: asyncio.Event | None = None,
        on_delta: Any = None,
    ) -> Any:
        del prompt, tools, raise_when_empty, interrupt_flag, on_delta
        self.calls += 1
        self.close_count_at_call = self.first_stream.close_count
        write_args = json.dumps({"path": "fallback.txt", "content": "ok"})
        finalize_args = json.dumps({"paths": ["fallback.txt"], "response": "done"})

        async def stream() -> Any:
            yield {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": write_args}}]}
            yield {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]}

        return stream()


class SandboxAgentProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = SandboxManager(Path(self.tempdir.name) / "sandbox")
        self.scope = self.manager.get_scope(
            stream_id="stream-1",
            platform="test",
            group_id=None,
            actor_id="actor-1",
        )
        candidate = SandboxEditCandidate.mint(
            stream_id="stream-1",
            platform="test",
            group_id=None,
            actor_id="actor-1",
            source_message_id="message-1",
            query="edit a file",
        )
        self.handoff = SandboxEditHandoff.mint(candidate, handoff_id="a" * 32)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _agent(
        self,
        llm: Any,
        config: SandboxAgentConfig | None = None,
    ) -> SandboxAgent:
        return SandboxAgent(
            self.handoff,
            self.scope,
            llm=llm,
            manager=self.manager,
            config=config or SandboxAgentConfig(),
        )

    def _provider(self, config: SandboxAgentConfig | None = None) -> SandboxProvider:
        return SandboxProvider(self.scope, self.handoff, manager=self.manager, config=config or SandboxAgentConfig())

    async def test_progress_continues_past_compatibility_round_and_call_caps(self) -> None:
        writes = [
            [_call("write_text", path=f"file-{index}.txt", content=str(index))]
            for index in range(40)
        ]
        responses = [*writes, [_call("finalize", paths=[f"file-{index}.txt" for index in range(40)], response="done")]]
        llm = _FakeLLM(responses, "primary")
        config = SandboxAgentConfig(max_rounds=2, max_tool_calls=3)

        result = await self._agent(llm, config).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertGreater(result.rounds, 8)
        self.assertGreater(result.tool_calls, 32)
        self.assertEqual(result.rounds, 41)
        self.assertEqual(result.tool_calls, 41)

    async def test_progress_resets_failover_state_and_returns_to_model_zero(self) -> None:
        model_zero = _FakeLLM(
            [
                [_call("unknown")],
                [_call("unknown")],
                [_call("write_text", path="zero.txt", content="zero")],
                [_call("finalize", paths=["zero.txt", "one.txt"], response="done")],
            ],
            "zero",
        )
        model_one = _FakeLLM([[_call("write_text", path="one.txt", content="one")]], "one")
        result = await self._agent(
            [model_zero, model_one],
            SandboxAgentConfig(non_progress_limit=2),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(model_zero.calls, 4)
        self.assertEqual(model_one.calls, 1)
        self.assertEqual(result.changed_paths, ("zero.txt", "one.txt"))

    async def test_consecutive_all_model_failures_keep_model_error_classification(self) -> None:
        model_zero = _FakeLLM([RuntimeError("zero failed")], "zero")
        model_one = _FakeLLM([RuntimeError("one failed")], "one")

        result = await self._agent([model_zero, model_one]).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.MODEL_ERROR)
        self.assertEqual(model_zero.calls, 1)
        self.assertEqual(model_one.calls, 1)

    async def test_consecutive_no_tool_failures_keep_no_finalize_classification(self) -> None:
        model_zero = _FakeLLM([[]], "zero")
        model_one = _FakeLLM([[]], "one")

        result = await self._agent([model_zero, model_one]).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.NO_FINALIZE)

    async def test_consecutive_rejected_tools_across_models_keep_model_error(self) -> None:
        model_zero = _FakeLLM([[_call("unknown")], [_call("unknown")]], "zero")
        model_one = _FakeLLM([[_call("unknown")], [_call("unknown")]], "one")

        result = await self._agent(
            [model_zero, model_one],
            SandboxAgentConfig(non_progress_limit=2),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.MODEL_ERROR)
        self.assertEqual(result.tool_calls, 4)

    async def test_duplicate_stall_is_reset_by_a_non_duplicate_rejection(self) -> None:
        llm = _FakeLLM(
            [
                [_call("write_text", path="same.txt", content="one")],
                [_call("write_text", path="same.txt", content="one")],
                [_call("unknown")],
                [_call("write_text", path="same.txt", content="one")],
                [_call("finalize", paths=["same.txt"], response="done")],
            ],
            "primary",
        )

        result = await self._agent(
            llm,
            SandboxAgentConfig(non_progress_limit=5, duplicate_limit=3),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(result.tool_calls, 5)

    async def test_quota_rejection_is_correctable_non_progress(self) -> None:
        llm = _FakeLLM(
            [
                [_call("write_text", path="small.txt", content="toolong")],
                [_call("write_text", path="small.txt", content="ok")],
                [_call("finalize", paths=["small.txt"], response="done")],
            ],
            "primary",
        )

        result = await self._agent(
            llm,
            SandboxAgentConfig(max_staged_bytes=2, non_progress_limit=2),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(result.tool_calls, 3)

    def test_observation_and_fingerprint_bookkeeping_are_bounded(self) -> None:
        config = SandboxAgentConfig(observation_max_chars=32, duplicate_fingerprint_window=2)
        agent = self._agent(None, config)
        observations: list[str] = []
        for index in range(20):
            agent._append_observation(observations, {"index": index, "text": "x" * 80})
        self.assertLessEqual(sum(len(value) for value in observations), 32)

        provider = self._provider(config)
        for index in range(3):
            self.assertIsNone(provider._duplicate("test", {"index": index}, revision_sensitive=False))
        self.assertLessEqual(len(provider._seen_fingerprints), 2)
        self.assertIsNone(provider._duplicate("test", {"index": 0}, revision_sensitive=False))
        self.assertLessEqual(len(provider._seen_fingerprints), 2)

    def test_staging_file_and_byte_bounds_account_for_overwrites(self) -> None:
        provider = self._provider(
            SandboxAgentConfig(max_staged_files=2, max_staged_bytes=10),
        )

        first = provider.write_text("a.txt", "12345")
        second = provider.write_text("b.txt", "12")
        overwrite = provider.write_text("a.txt", "12345678")
        too_many_files = provider.write_text("c.txt", "1")
        too_many_bytes = provider.write_text("b.txt", "123")
        shrink = provider.write_text("a.txt", "12")

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(overwrite["ok"])
        self.assertEqual(too_many_files["error_code"], SandboxErrorCode.SIZE_LIMIT.value)
        self.assertEqual(too_many_bytes["error_code"], SandboxErrorCode.SIZE_LIMIT.value)
        self.assertTrue(shrink["ok"])
        self.assertEqual(len(provider._staged_sizes), 2)
        self.assertEqual(provider._staged_total_bytes, 4)

    async def test_default_first_output_deadline_is_extended(self) -> None:
        self.assertEqual(SandboxAgentConfig().first_output_timeout_seconds, 60.0)

        write_args = json.dumps({"path": "delayed.txt", "content": "ok"})
        finalize_args = json.dumps({"paths": ["delayed.txt"], "response": "done"})
        llm = _StreamLLM(
            0.2,
            "delayed",
            [
                {"reasoning_content": "private reasoning"},
                {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": write_args}}]},
                {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]},
            ],
        )
        self.assertGreater(llm.delay, 0.1)
        self.assertLess(llm.delay, 0.8)

        result = await self._agent(
            llm,
            SandboxAgentConfig(
                # The 200 ms delay is safely beyond the old 100 ms scaled
                # deadline, but remains well inside this configured 800 ms
                # first-output deadline.
                first_output_timeout_seconds=0.8,
                complete_attempt_timeout_seconds=1.0,
            ),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(llm.calls, 1)

    async def test_first_output_timeout_switches_after_configured_deadline(self) -> None:
        write_args = json.dumps({"path": "fallback.txt", "content": "ok"})
        finalize_args = json.dumps({"paths": ["fallback.txt"], "response": "done"})
        slow = _StreamLLM(0.8, "slow", [{"content": "too late"}])
        fast = _StreamLLM(
            0.0,
            "fast",
            [
                {"content": "ready"},
                {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": write_args}}]},
                {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]},
            ],
        )

        result = await self._agent(
            [slow, fast],
            SandboxAgentConfig(
                first_output_timeout_seconds=0.3,
                complete_attempt_timeout_seconds=1.0,
            ),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(slow.calls, 1)
        self.assertEqual(fast.calls, 1)

    async def test_empty_keepalive_frames_do_not_count_as_first_output(self) -> None:
        result = await self._agent(
            _KeepaliveLLM("keepalive"),
            SandboxAgentConfig(first_output_timeout_seconds=0.3, complete_attempt_timeout_seconds=0.5),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.TIMEOUT)

    async def test_meaningful_activity_extends_idle_deadline_and_assembles_fragments(self) -> None:
        finalize_args = json.dumps({"paths": ["paced.txt"], "response": "done"})
        llm = _PacedStreamLLM(
            [
                (0.0, {"content": "started"}),
                (0.1, {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": '{"path":"paced.txt",'}}]}),
                (0.1, {"tool_calls": [{"index": 0, "function": {"arguments": '"content":"'}}]}),
                (0.1, {"tool_calls": [{"index": 0, "function": {"arguments": 'ok'}}]}),
                (0.1, {"tool_calls": [{"index": 0, "function": {"arguments": '"'}}]}),
                (0.1, {"tool_calls": [{"index": 0, "function": {"arguments": '}'}}]}),
                (0.1, {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]}),
            ],
            "paced",
        )

        result = await self._agent(
            llm,
            # Six 100 ms gaps total roughly 600 ms, exceeding this 300 ms idle
            # ceiling while keeping every meaningful gap at one-third of it.
            SandboxAgentConfig(first_output_timeout_seconds=0.4, complete_attempt_timeout_seconds=0.3),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(result.changed_paths, ("paced.txt",))
        self.assertEqual(result.tool_calls, 2)

    async def test_post_output_silence_uses_inactivity_timeout_and_switches_model(self) -> None:
        slow = _PacedStreamLLM(
            [(0.0, {"content": "started"}), (0.8, {"content": "too late"})],
            "slow",
        )
        write_args = json.dumps({"path": "fallback.txt", "content": "ok"})
        finalize_args = json.dumps({"paths": ["fallback.txt"], "response": "done"})
        fast = _PacedStreamLLM(
            [
                (0.0, {"content": "ready"}),
                (0.0, {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": write_args}}]}),
                (0.0, {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]}),
            ],
            "fast",
        )

        result = await self._agent(
            [slow, fast],
            SandboxAgentConfig(first_output_timeout_seconds=0.4, complete_attempt_timeout_seconds=0.3),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(slow.calls, 1)
        self.assertEqual(fast.calls, 1)

    async def test_id_only_tool_delta_does_not_reset_inactivity_timeout(self) -> None:
        result = await self._agent(
            _PacedStreamLLM(
                [
                    (0.0, {"content": "started"}),
                    (0.1, {"tool_calls": [{"id": "bookkeeping-only"}]}),
                    (0.8, {"content": "too late"}),
                ],
                "id-only",
            ),
            SandboxAgentConfig(first_output_timeout_seconds=0.4, complete_attempt_timeout_seconds=0.3),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.TIMEOUT)

    async def test_usage_only_frame_does_not_reset_inactivity_timeout(self) -> None:
        result = await self._agent(
            _PacedStreamLLM(
                [
                    (0.0, {"content": "started"}),
                    (0.1, {"usage": {"total_tokens": 1}}),
                    (0.8, {"content": "too late"}),
                ],
                "usage-only",
            ),
            SandboxAgentConfig(first_output_timeout_seconds=0.4, complete_attempt_timeout_seconds=0.3),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.TIMEOUT)

    async def test_timeout_closes_stream_once_before_failover_and_leaves_no_task(self) -> None:
        blocked_stream = _ControlledStream()
        slow = _ControlledStreamLLM(blocked_stream, "blocked")
        fast = _CloseCheckingFallbackLLM(blocked_stream)
        run_task = asyncio.create_task(
            self._agent(
                [slow, fast],
                SandboxAgentConfig(first_output_timeout_seconds=0.4, complete_attempt_timeout_seconds=0.3),
            ).run()
        )

        await asyncio.wait_for(blocked_stream.started.wait(), timeout=1.0)
        result = await asyncio.wait_for(run_task, timeout=2.0)

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(slow.calls, 1)
        self.assertEqual(fast.calls, 1)
        self.assertEqual(blocked_stream.close_count, 1)
        self.assertEqual(fast.close_count_at_call, 1)
        self.assertTrue(blocked_stream.consumer_finished.is_set())
        self.assertIsNotNone(blocked_stream.consumer_task)
        self.assertTrue(blocked_stream.consumer_task.done())

    async def test_stream_buffer_overflow_is_a_model_failure_with_failover(self) -> None:
        overflowing = _StreamLLM(0.0, "overflow", [{"content": "x" * 2048}])
        write_args = json.dumps({"path": "fallback.txt", "content": "ok"})
        finalize_args = json.dumps({"paths": ["fallback.txt"], "response": "done"})
        fallback = _StreamLLM(
            0.0,
            "fallback",
            [
                {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": write_args}}]},
                {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]},
            ],
        )

        result = await self._agent(
            [overflowing, fallback],
            SandboxAgentConfig(
                stream_buffer_max_bytes=1024,
                first_output_timeout_seconds=0.2,
                complete_attempt_timeout_seconds=0.2,
            ),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(overflowing.calls, 1)
        self.assertEqual(fallback.calls, 1)

    async def test_direct_iterator_callback_does_not_double_count_exact_limit(self) -> None:
        write_args = json.dumps({"path": "exact.txt", "content": "ok"})
        finalize_args = json.dumps({"paths": ["exact.txt"], "response": "done"})
        events = [
            {"content": "x" * 256},
            {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": write_args}}]},
            {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]},
        ]
        stream_limit = sum(SandboxAgent._stream_event_bytes(event, 100_000) for event in events)
        llm = _CallbackDirectStreamLLM(0.0, "exact", events)

        result = await self._agent(
            llm,
            SandboxAgentConfig(
                stream_buffer_max_bytes=stream_limit,
                first_output_timeout_seconds=0.2,
                complete_attempt_timeout_seconds=0.2,
            ),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(llm.calls, 1)

    async def test_tuple_stream_callback_buffer_overflow_fails_over(self) -> None:
        overflowing = _CallbackTupleLLM([{"content": "x" * 2048}], "tuple-overflow")
        write_args = json.dumps({"path": "fallback.txt", "content": "ok"})
        finalize_args = json.dumps({"paths": ["fallback.txt"], "response": "done"})
        fallback = _StreamLLM(
            0.0,
            "fallback",
            [
                {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": write_args}}]},
                {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": finalize_args}}]},
            ],
        )

        result = await self._agent(
            [overflowing, fallback],
            SandboxAgentConfig(
                stream_buffer_max_bytes=1024,
                first_output_timeout_seconds=0.2,
                complete_attempt_timeout_seconds=0.2,
            ),
        ).run()

        self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
        self.assertEqual(overflowing.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertIsNotNone(overflowing.interrupt_flag)
        self.assertTrue(overflowing.interrupt_flag.is_set())


if __name__ == "__main__":
    unittest.main()
