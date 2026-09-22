from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from live2d_adapter.config import AdapterConfig, RendererConfig, ServerConfig
from live2d_adapter.control_pipeline import ControlPipeline
from live2d_adapter.protocol import (
    AvatarCommand,
    AvatarEvent,
    AvatarInteraction,
    InteractionEvent,
    ProtocolError,
)
from live2d_adapter.runtime import AvatarRuntime


class ControlPipelineTests(unittest.TestCase):
    def test_protocol_1_1_adds_prepare_apply_without_changing_major(self) -> None:
        command = AvatarCommand.from_mapping(
            {
                "type": "avatar.command",
                "version": "1.0",
                "request_id": "request-1",
                "event": "prepare_reply",
                "payload": {"reply": "hello"},
            }
        )
        self.assertIs(command.event, AvatarEvent.PREPARE_REPLY)
        response = InteractionEvent(
            event=AvatarInteraction.REPLY_PREPARED,
            payload={
                "reply": "hello",
                "web_search": False,
                "search_query": "",
                "control_id": "request-1",
            },
            request_id=command.request_id,
        )
        self.assertEqual(response.to_mapping()["version"], "1.1")
        self.assertEqual(response.to_mapping()["request_id"], "request-1")

    def test_normalizes_json_and_maps_action_without_exposing_controls(self) -> None:
        pipeline = ControlPipeline()

        prepared = pipeline.prepare_reply(
            '{"reply":"你好","emotion":"angry",'
            '"action":"点头/同意","web_search":"true","search_query":"天气"}',
            "req-1",
        )

        self.assertEqual(prepared.to_payload(), {
            "reply": "你好",
            "web_search": True,
            "search_query": "天气",
            "control_id": "req-1",
        })
        self.assertNotIn("emotion", prepared.to_payload())
        self.assertNotIn("action", prepared.to_payload())

    def test_fenced_json_and_ignored_actions_are_supported(self) -> None:
        pipeline = ControlPipeline()
        prepared = pipeline.prepare_reply(
            '```json\n{"reply":"待机","emotion":"invalid",'
            '"action":"一般","web_search":false,"search_query":"ignored"}\n```',
            "req-2",
        )
        self.assertEqual(prepared.reply, "待机")
        self.assertFalse(prepared.web_search)
        self.assertEqual(prepared.search_query, "")

        applied: list[tuple[str | None, str | None]] = []
        outcome = pipeline.apply(
            "req-2",
            apply_callback=lambda staged: applied.append(
                (staged.emotion, staged.action_id)
            ),
        )
        self.assertTrue(outcome.applied)
        self.assertEqual(applied, [(None, None)])
        repeated = pipeline.apply("req-2")
        self.assertTrue(repeated.already_applied)

    def test_apply_is_scoped_and_unknown_ids_are_non_mutating(self) -> None:
        pipeline = ControlPipeline()
        pipeline.prepare_reply(
            '{"reply":"x","emotion":"shy","action":"摇头/否定"}',
            "req-3",
            client_id="client-a",
        )
        calls: list[str] = []
        unknown = pipeline.apply(
            "req-3",
            client_id="client-b",
            apply_callback=lambda _staged: calls.append("called"),
        )
        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(calls, [])

        applied = pipeline.apply(
            "req-3",
            client_id="client-a",
            apply_callback=lambda staged: calls.append(staged.action_id or ""),
        )
        self.assertEqual(calls, ["SHAKE_HEAD"])
        self.assertTrue(applied.applied)
        pipeline.discard_client("client-a")
        self.assertEqual(pipeline.apply("req-3", client_id="client-a").status, "unknown")

    def test_ttl_and_count_bounds(self) -> None:
        now = [0.0]
        pipeline = ControlPipeline(ttl_seconds=2, max_controls=2, clock=lambda: now[0])
        for index in range(3):
            pipeline.prepare_reply(str(index), f"req-{index}")
        self.assertEqual(pipeline.pending_count(), 2)
        self.assertEqual(pipeline.apply("req-0").status, "unknown")
        now[0] = 3.0
        pipeline.purge_expired()
        self.assertEqual(pipeline.pending_count(), 0)

    def test_clear_removes_pending_and_applied_controls_from_all_scopes(self) -> None:
        pipeline = ControlPipeline()
        pipeline.prepare_reply("default", "default-id")
        pipeline.prepare_reply("client", "client-id", client_id="client-a")
        self.assertTrue(pipeline.apply("default-id").applied)

        pipeline.clear()

        self.assertEqual(pipeline.pending_count(), 0)
        self.assertEqual(pipeline.pending_count("client-a"), 0)
        self.assertEqual(pipeline.applied_count(), 0)
        self.assertEqual(pipeline.applied_count("client-a"), 0)


class AvatarRuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self) -> AvatarRuntime:
        config = AdapterConfig(
            server=ServerConfig(),
            renderer=RendererConfig(model_path=Path("missing.model3.json")),
        )
        return AvatarRuntime(config, SimpleNamespace(info=lambda *args, **kwargs: None))

    async def test_stopped_runtime_rejects_commands_and_stop_clears_all_scopes(self) -> None:
        runtime = self._runtime()
        runtime.control_pipeline.prepare_reply("default", "default-id")
        runtime.control_pipeline.prepare_reply(
            "client", "client-id", client_id="client-a"
        )
        self.assertTrue(runtime.control_pipeline.apply("default-id").applied)

        await runtime.stop()

        self.assertEqual(runtime.control_pipeline.pending_count(), 0)
        self.assertEqual(runtime.control_pipeline.pending_count("client-a"), 0)
        self.assertEqual(runtime.control_pipeline.applied_count(), 0)
        self.assertEqual(runtime.control_pipeline.applied_count("client-a"), 0)

        with self.assertRaises(ProtocolError):
            await runtime.dispatch(
                AvatarCommand(
                    event=AvatarEvent.PREPARE_REPLY,
                    payload={"reply": "stopped"},
                    request_id="stopped-prepare",
                )
            )
        with self.assertRaises(ProtocolError):
            await runtime.dispatch(
                AvatarCommand(
                    event=AvatarEvent.APPLY_CONTROL,
                    payload={"control_id": "default-id"},
                    request_id="stopped-apply",
                )
            )
        with self.assertRaises(ProtocolError):
            await runtime.dispatch(
                AvatarCommand(
                    event=AvatarEvent.STATE,
                    payload={"state": "idle"},
                    request_id="stopped-state",
                )
            )

        pong = await runtime.dispatch(
            AvatarCommand(
                event=AvatarEvent.PING,
                request_id="stopped-ping",
            )
        )
        self.assertIsNotNone(pong)
        self.assertEqual(pong.event, AvatarInteraction.PONG)
        self.assertFalse(pong.payload["running"])


if __name__ == "__main__":
    unittest.main()
