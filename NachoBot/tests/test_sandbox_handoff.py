from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import time
import unittest
from dataclasses import is_dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.chat.focus import bind_lease, current_context_lease
from src.chat.focus.models import FocusLease
from src.chat.sandbox.sandbox_agent import (
    FinalizeIntent,
    SandboxAgent,
    SandboxAgentConfig,
    SandboxAgentCoordinator,
    SandboxAgentOutcome,
    SandboxAgentResult,
    SandboxProvider,
)
from src.chat.sandbox.sandbox_delivery import SandboxDeliveryGate
from src.chat.sandbox.sandbox_handoff import (
    SandboxEditCandidate,
    SandboxEditHandoff,
    parse_sandbox_confirmation,
)
from src.chat.sandbox.sandbox_manager import MAX_UPLOAD_BYTES, SandboxManager, SandboxPathError
from src.chat.replyer.prompt_build_result import ReplyPromptBuildResult
from src.common.data_models.llm_data_model import LLMGenerationDataModel
from src.llm_models.utils_model import _ObservedAsyncIterator
from src.plugin_system.apis import generator_api
from src.plugin_system.apis.send_api import SendReceipt, SendStatus


def _handoff(
    query: str = "edit the text",
    *,
    stream_id: str = "stream-1",
    platform: str = "qq",
    group_id: str | None = "123",
    actor_id: str = "42",
    source_message_id: str = "message-1",
    acknowledgement: str | None = None,
) -> SandboxEditHandoff:
    candidate = SandboxEditCandidate.mint(
        stream_id=stream_id,
        platform=platform,
        group_id=group_id,
        actor_id=actor_id,
        source_message_id=source_message_id,
        query=query,
    )
    return SandboxEditHandoff.mint(candidate, acknowledgement=acknowledgement)


class SandboxStorageTests(unittest.TestCase):
    def test_save_upload_is_bounded_basename_collision_safe_and_group_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            upload_args = {
                "stream_id": "group-stream",
                "platform": "qq",
                "group_id": "123",
                "actor_id": "42",
            }
            actor = manager.get_scope(**upload_args)
            other = manager.get_scope(
                stream_id="other-stream",
                platform="qq",
                group_id="123",
                actor_id="77",
            )

            exact_limit = manager.save_upload(b"x" * MAX_UPLOAD_BYTES, "limit.bin", **upload_args)
            self.assertTrue(Path(exact_limit).is_absolute())
            self.assertEqual(Path(exact_limit), actor.write_root / "limit.bin")
            self.assertEqual(Path(exact_limit).stat().st_size, MAX_UPLOAD_BYTES)
            with self.assertRaises(ValueError):
                manager.save_upload(b"x" * (MAX_UPLOAD_BYTES + 1), "too-large.bin", **upload_args)

            first = manager.save_upload(b"first", "nested/original.txt", **upload_args)
            second = manager.save_upload(b"second", "nested/original.txt", **upload_args)
            first_path = Path(first)
            second_path = Path(second)
            self.assertTrue(first_path.is_absolute())
            self.assertEqual(first_path, actor.write_root / "original.txt")
            self.assertEqual(second_path, actor.write_root / "original_1.txt")
            self.assertEqual(first_path.read_bytes(), b"first")
            self.assertEqual(second_path.read_bytes(), b"second")
            self.assertEqual(first_path.parent, actor.write_root)
            self.assertEqual(second_path.parent, actor.write_root)
            self.assertFalse((other.write_root / "original.txt").exists())
            self.assertFalse((other.write_root / "original_1.txt").exists())

            for unsafe_name in ("../escape.txt", r"..\escape.txt", "/absolute.txt", r"C:\absolute.txt"):
                with self.subTest(filename=unsafe_name), self.assertRaises(SandboxPathError):
                    manager.save_upload(b"unsafe", unsafe_name, **upload_args)

    def test_group_layout_visibility_and_actor_write_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            actor = manager.get_scope(stream_id="s1", platform="qq", group_id="123", actor_id="42")
            other = manager.get_scope(stream_id="s2", platform="qq", group_id="123", actor_id="77")
            (other.write_root / "input.txt").write_text("other", encoding="utf-8")
            provider = SandboxProvider(actor, _handoff(), manager=manager)

            self.assertEqual(provider.read_text("77/input.txt")["content"], "other")
            self.assertTrue(provider.write_text("result.txt", "mine")["ok"])
            self.assertTrue(provider.write_text("42/from-list.txt", "listed path")["ok"])
            forbidden = provider.write_text("77/forbidden.txt", "must not write")
            self.assertFalse(forbidden["ok"])
            self.assertIn("another user's subtree", forbidden["error"])
            self.assertFalse((actor.write_root / "result.txt").exists())
            with self.assertRaises(SandboxPathError):
                actor.path_for_write("../77/input.txt")

            intent = provider.finalize()["intent"]
            self.assertIsInstance(intent, FinalizeIntent)
            self.assertFalse((actor.write_root / "result.txt").exists())
            provider.commit(intent)
            self.assertEqual((actor.write_root / "result.txt").read_text(encoding="utf-8"), "mine")
            self.assertEqual((actor.write_root / "from-list.txt").read_text(encoding="utf-8"), "listed path")
            self.assertTrue((actor.group_root or Path(temp)).is_dir())
            self.assertTrue(actor.write_root.is_dir())
            self.assertTrue(other.write_root.is_dir())

    def test_provider_tool_surface_and_path_forms_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id=None, actor_id="42")
            provider = SandboxProvider(scope, _handoff(stream_id="s", group_id=None), manager=manager)
            definitions = provider.tool_definitions()
            self.assertEqual(
                [definition["name"] for definition in definitions],
                ["list_tree", "read_text", "search_text", "write_text", "finalize"],
            )
            self.assertTrue(all("input_schema" in definition for definition in definitions))
            self.assertNotIn("shell", {definition["name"] for definition in definitions})
            for unsafe in ("../escape.txt", r"..\escape.txt", "/tmp/escape.txt", r"C:\escape.txt", r"\\host\share\x", "C:escape.txt"):
                result = provider.execute("read_text", {"path": unsafe})
                self.assertFalse(result["ok"], unsafe)

    def test_read_only_finalize_requires_observation_and_bounded_response(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id=None, actor_id="42")
            handoff = _handoff(stream_id="s", group_id=None)

            no_inspection = SandboxProvider(scope, handoff, manager=manager)
            self.assertFalse(no_inspection.finalize(response="目录为空")["ok"])
            no_inspection.abort()

            empty_response = SandboxProvider(scope, handoff, manager=manager)
            self.assertTrue(empty_response.list_tree("")["ok"])
            self.assertFalse(empty_response.finalize(response="   ")["ok"])
            empty_response.abort()

            oversized = SandboxProvider(scope, handoff, manager=manager)
            self.assertTrue(oversized.list_tree("")["ok"])
            self.assertFalse(oversized.finalize(response="x" * 4001)["ok"])
            oversized.abort()

            staged = SandboxProvider(scope, handoff, manager=manager)
            self.assertTrue(staged.write_text("draft.txt", "staged")["ok"])
            self.assertFalse(staged.finalize(paths=[], response="已检查")["ok"])
            staged.abort()

            valid = SandboxProvider(scope, handoff, manager=manager)
            self.assertTrue(valid.list_tree("")["ok"])
            result = valid.finalize(response=f"已检查 {handoff.handoff_id} C:\\secret.txt")
            self.assertTrue(result["ok"])
            intent = result["intent"]
            self.assertEqual(intent.changed_paths, ())
            self.assertEqual(intent.response, "已检查 [id] [path]")
            valid.abort()

    def test_repeated_write_is_suppressed_even_after_revision_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id=None, actor_id="42")
            provider = SandboxProvider(scope, _handoff(stream_id="s", group_id=None), manager=manager)
            self.assertTrue(provider.write_text("out.txt", "one")["ok"])
            # A revision change must not make the same write callable again.
            (scope.write_root / "external.txt").write_text("outside model staging", encoding="utf-8")
            duplicate = provider.write_text("out.txt", "one")
            self.assertTrue(duplicate["duplicate"])

    def test_revision_hashes_content_and_search_skips_oversized_files(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id=None, actor_id="42")
            source = scope.write_root / "same-size.txt"
            source.write_text("old", encoding="utf-8")
            original_stat = source.stat()
            provider = SandboxProvider(scope, _handoff(stream_id="s", group_id=None), manager=manager)
            self.assertTrue(provider.write_text("same-size.txt", "new")["ok"])
            source.write_text("new", encoding="utf-8")
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            intent = provider.finalize(["same-size.txt"])["intent"]
            with self.assertRaises(RuntimeError):
                provider.commit(intent)
            provider.abort()

            oversized = scope.read_root / "too-large.txt"
            oversized.write_text("x" * (provider.config.max_text_bytes + 1), encoding="utf-8")
            search = provider.search_text("x")
            self.assertEqual(search["matches"], [])

    def test_scope_rejects_group_or_platform_parent_replaced_by_link(self):
        for replaced_name in ("groups", "platform"):
            with self.subTest(replaced_name=replaced_name), tempfile.TemporaryDirectory() as temp:
                manager = SandboxManager(temp)
                manager.get_scope(stream_id="s", platform="qq", group_id="123", actor_id="42")
                if replaced_name == "groups":
                    original = Path(temp) / "groups"
                    target = Path(temp) / "outside-groups"
                else:
                    original = Path(temp) / "groups" / "qq"
                    target = Path(temp) / "outside-platform"
                target.mkdir(parents=True)
                backup = original.with_name(original.name + "-real")
                original.rename(backup)
                try:
                    original.symlink_to(target, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"directory symlink unavailable: {exc}")
                with self.assertRaises(SandboxPathError):
                    manager.get_scope(stream_id="s2", platform="qq", group_id="123", actor_id="42")

    def test_legacy_tools_are_not_registered_for_one_shot_models(self):
        self.assertEqual(
            SandboxProvider.TOOL_NAMES,
            ("list_tree", "read_text", "search_text", "write_text", "finalize"),
        )

    def test_private_cleanup_remains_stream_scoped_and_group_roots_survive(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            private = manager.get_scope(stream_id="private-1", platform="qq", group_id=None, actor_id="42")
            group = manager.get_scope(stream_id="group-stream", platform="qq", group_id="123", actor_id="42")
            old_private = private.write_root / "old.txt"
            old_group = group.write_root / "old.txt"
            old_private.write_text("old", encoding="utf-8")
            old_group.write_text("old", encoding="utf-8")
            stale = time.time() - 100
            os.utime(old_private, (stale, stale))
            os.utime(old_group, (stale, stale))
            os.utime(private.read_root, (stale, stale))
            manager.cleanup_old_sessions(max_age_seconds=10, now=time.time())
            self.assertFalse(private.read_root.exists())
            self.assertTrue(group.read_root.exists())
            self.assertTrue(group.write_root.exists())
            self.assertFalse(old_group.exists())

    def test_binary_and_symlink_reads_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id=None, actor_id="42")
            (scope.read_root / "data.bin").write_bytes(b"\xff\x00")
            provider = SandboxProvider(scope, SandboxEditHandoff.mint(SandboxEditCandidate.mint(
                stream_id="s", platform="qq", group_id=None, actor_id="42", source_message_id="m", query="read"
            )), manager=manager)
            self.assertIn("unsupported", provider.read_text("data.bin")["error"])
            outside = Path(temp) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            try:
                (scope.read_root / "link.txt").symlink_to(outside)
                (scope.read_root / "dangling.txt").symlink_to(Path(temp) / "missing.txt")
            except (OSError, NotImplementedError):
                return
            self.assertIn("symlink", provider.read_text("link.txt")["error"])
            self.assertIn("symlink", provider.read_text("dangling.txt")["error"])


class SandboxEnvelopeAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_commits_and_publishes_only_after_finalize(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class Model:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return "", ("", "fake", [Call("write_text", {"path": "out.txt", "content": "done"})])
                return "", ("", "fake", [Call("finalize", {"paths": ["out.txt"]})])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            handoff = SandboxEditHandoff.mint(
                SandboxEditCandidate.mint(
                    stream_id="s",
                    platform="qq",
                    group_id=None,
                    actor_id="42",
                    source_message_id="m",
                    query="write out.txt",
                )
            )
            published: list[tuple[str, tuple[str, ...]]] = []

            async def publish(value, paths):
                published.append((value.handoff_id, paths))

            def factory(value, scope, *, manager):
                return SandboxAgent(
                    value,
                    scope,
                    llm=Model(),
                    manager=manager,
                    config=SandboxAgentConfig(first_output_timeout_seconds=2),
                )

            result = await SandboxAgentCoordinator(
                manager=manager,
                agent_factory=factory,
                authorize=lambda _handoff: True,
                publish=publish,
            ).run_handoff(handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual((Path(temp) / "s" / "out.txt").read_text(encoding="utf-8"), "done")
            self.assertEqual(published[0][1], ("out.txt",))

    async def test_read_only_finalize_reports_response_without_commit_or_publish(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class Model:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return "", ("", "fake", [Call("list_tree", {"path": ""})])
                return "", (
                    "",
                    "fake",
                    [Call("finalize", {"response": "沙盒中有一个文件。"})],
                )

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            handoff = _handoff(stream_id="s", group_id=None)
            source = SimpleNamespace(
                message_id=handoff.source_message_id,
                chat_id=handoff.stream_id,
                user_id=handoff.actor_id,
                user_info=SimpleNamespace(user_id=handoff.actor_id, platform=handoff.platform),
                chat_info=SimpleNamespace(
                    stream_id=handoff.stream_id,
                    platform=handoff.platform,
                    group_info=None,
                ),
            )
            generated: list[dict[str, object]] = []
            sent: list[dict[str, object]] = []

            class Generator:
                async def generate_reply(self, **kwargs):
                    generated.append(kwargs)
                    return True, SimpleNamespace(content="已列出当前沙盒目录。")

            class Sender:
                async def background_text_to_stream_receipt(self, **kwargs):
                    sent.append(kwargs)
                    return SendReceipt(SendStatus.DELIVERED, kwargs["stream_id"], message_id="report")

            def factory(value, scope, *, manager):
                return SandboxAgent(value, scope, llm=Model(), manager=manager)

            published = AsyncMock(side_effect=AssertionError("read-only task must not publish"))
            result = await SandboxAgentCoordinator(
                manager=manager,
                agent_factory=factory,
                authorize=lambda _handoff: True,
                publish=published,
                source_message_lookup=lambda _stream, _message: [source],
                generator_api=Generator(),
                send_api=Sender(),
                completion_report_claims=set(),
            ).run_handoff(handoff)

            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(result.changed_paths, ())
            self.assertEqual(result.response, "沙盒中有一个文件。")
            self.assertFalse(published.await_count)
            self.assertEqual(len(generated), 1)
            self.assertIn("沙盒中有一个文件", generated[0]["extra_info"])
            self.assertIn("不需要附件", generated[0]["extra_info"])
            self.assertEqual(len(sent), 1)

    async def test_read_only_coordinator_revalidates_revision_before_reporting(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            handoff = _handoff(stream_id="s", group_id=None)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id=None, actor_id="42")
            provider = SandboxProvider(scope, handoff, manager=manager)
            self.assertTrue(provider.list_tree("")["ok"])
            intent = provider.finalize(response="只读结果")["intent"]
            terminal = SandboxAgentResult(
                SandboxAgentOutcome.FINALIZED,
                handoff.handoff_id,
                intent=intent,
                response=intent.response,
            )

            class StubAgent:
                def __init__(self, result, active_provider=provider):
                    self.provider = active_provider
                    self.result = result

                async def run(self):
                    return self.result

            reporter = AsyncMock()
            published = AsyncMock()
            with (
                patch.object(provider, "validate_read_only_intent", wraps=provider.validate_read_only_intent) as validate,
                patch.object(provider, "commit", side_effect=AssertionError("read-only task must not commit")) as commit,
            ):
                result = await SandboxAgentCoordinator(
                    manager=manager,
                    agent_factory=lambda *_args, **_kwargs: StubAgent(terminal),
                    authorize=lambda _handoff: True,
                    publish=published,
                    completion_reporter=reporter,
                    completion_report_claims=set(),
                ).run_handoff(handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            validate.assert_called_once_with(intent)
            commit.assert_not_called()
            published.assert_not_awaited()
            reporter.assert_awaited_once()

            changed_handoff = _handoff(stream_id="s", group_id=None, source_message_id="changed")
            changed_provider = SandboxProvider(scope, changed_handoff, manager=manager)
            self.assertTrue(changed_provider.list_tree("")["ok"])
            changed_intent = changed_provider.finalize(response="只读结果")["intent"]
            changed_provider.scope.write_root.joinpath("outside.txt").write_text("changed", encoding="utf-8")
            changed_result = SandboxAgentResult(
                SandboxAgentOutcome.FINALIZED,
                changed_handoff.handoff_id,
                intent=changed_intent,
                response=changed_intent.response,
            )
            changed_source = SimpleNamespace(
                message_id=changed_handoff.source_message_id,
                chat_id=changed_handoff.stream_id,
                user_id=changed_handoff.actor_id,
                user_info=SimpleNamespace(
                    user_id=changed_handoff.actor_id,
                    platform=changed_handoff.platform,
                ),
                chat_info=SimpleNamespace(
                    stream_id=changed_handoff.stream_id,
                    platform=changed_handoff.platform,
                    group_info=None,
                ),
            )

            class ChangedGenerator:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                async def generate_reply(self, **kwargs):
                    self.calls.append(kwargs)
                    return True, SimpleNamespace(content="只报告修订失败")

            class ChangedSender:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                async def background_text_to_stream_receipt(self, **kwargs):
                    self.calls.append(kwargs)
                    return SendReceipt(SendStatus.DELIVERED, kwargs["stream_id"], message_id="changed-report")

            changed_generator = ChangedGenerator()
            changed_sender = ChangedSender()
            changed_published = AsyncMock()
            with patch.object(
                changed_provider,
                "commit",
                side_effect=AssertionError("changed read-only task must not commit"),
            ) as changed_commit:
                result = await SandboxAgentCoordinator(
                    manager=manager,
                    agent_factory=lambda *_args, **_kwargs: StubAgent(changed_result, changed_provider),
                    authorize=lambda _handoff: True,
                    publish=changed_published,
                    source_message_lookup=lambda _stream, _message: [changed_source],
                    generator_api=changed_generator,
                    send_api=changed_sender,
                    completion_report_claims=set(),
                ).run_handoff(changed_handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.MODEL_ERROR)
            self.assertEqual(result.response, "")
            changed_commit.assert_not_called()
            changed_published.assert_not_awaited()
            self.assertEqual(len(changed_generator.calls), 1)
            self.assertNotIn("只读结果", changed_generator.calls[0]["extra_info"])
            self.assertNotIn("只读结果", changed_sender.calls[0]["text"])
            self.assertEqual(len(changed_sender.calls), 1)

    async def test_publication_false_is_not_reported_as_finalized(self):
        class Model:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                calls = [
                    type(
                        "Call",
                        (),
                        {
                            "func_name": "write_text",
                            "args": {"path": "out.txt", "content": "done"},
                        },
                    )(),
                ]
                if self.calls == 1:
                    return "", ("", "fake", calls)
                return "", (
                    "",
                    "fake",
                    [type("Call", (), {"func_name": "finalize", "args": {"paths": ["out.txt"]}})()],
                )

        with tempfile.TemporaryDirectory() as temp, patch(
            "src.plugin_system.apis.send_api.custom_to_stream", new=AsyncMock(return_value=False)
        ) as send_mock:
            manager = SandboxManager(temp)
            handoff = _handoff(stream_id="stream-1", group_id=None)

            def factory(value, scope, *, manager):
                return SandboxAgent(value, scope, llm=Model(), manager=manager)

            result = await SandboxAgentCoordinator(
                manager=manager,
                agent_factory=factory,
                authorize=lambda _handoff: True,
            ).run_handoff(handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.PUBLICATION_FAILED)
            self.assertEqual(result.changed_paths, ("out.txt",))
            self.assertEqual((Path(temp) / "stream-1" / "out.txt").read_text(encoding="utf-8"), "done")
            # Publication is attempted once; the completion reporter is
            # fail-closed because this test intentionally has no source row.
            self.assertEqual(send_mock.await_count, 1)

    async def test_publication_exception_is_not_reported_as_finalized(self):
        class Model:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return "", (
                        "",
                        "fake",
                        [
                            type(
                                "Call",
                                (),
                                {
                                    "func_name": "write_text",
                                    "args": {"path": "out.txt", "content": "done"},
                                },
                            )()
                        ],
                    )
                return "", (
                    "",
                    "fake",
                    [type("Call", (), {"func_name": "finalize", "args": {"paths": ["out.txt"]}})()],
                )

        with tempfile.TemporaryDirectory() as temp, patch(
            "src.plugin_system.apis.send_api.custom_to_stream",
            new=AsyncMock(side_effect=RuntimeError("adapter unavailable")),
        ):
            manager = SandboxManager(temp)
            handoff = _handoff(stream_id="stream-1", group_id=None)

            def factory(value, scope, *, manager):
                return SandboxAgent(value, scope, llm=Model(), manager=manager)

            result = await SandboxAgentCoordinator(
                manager=manager,
                agent_factory=factory,
                authorize=lambda _handoff: True,
            ).run_handoff(handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.PUBLICATION_FAILED)
            self.assertIn("publication exception", result.detail)
            self.assertEqual((Path(temp) / "stream-1" / "out.txt").read_text(encoding="utf-8"), "done")

    async def test_non_finalized_agent_aborts_staging_and_does_not_publish(self):
        class Model:
            async def generate_response_async(self, **_kwargs):
                return "plain answer", ("", "fake", [])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            handoff = SandboxEditHandoff.mint(
                SandboxEditCandidate.mint(
                    stream_id="s",
                    platform="qq",
                    group_id=None,
                    actor_id="42",
                    source_message_id="m",
                    query="write out.txt",
                )
            )
            published: list[str] = []

            def factory(value, scope, *, manager):
                return SandboxAgent(value, scope, llm=Model(), manager=manager)

            result = await SandboxAgentCoordinator(
                manager=manager,
                agent_factory=factory,
                authorize=lambda _handoff: True,
                publish=lambda _value, _paths: published.append("published"),
            ).run_handoff(handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.NO_FINALIZE)
            self.assertEqual(published, [])
            self.assertFalse((Path(temp) / "s" / "out.txt").exists())

    async def test_default_coordinator_notifies_once_for_terminal_failures(self):
        for outcome in (
            SandboxAgentOutcome.TIMEOUT,
            SandboxAgentOutcome.MODEL_ERROR,
            SandboxAgentOutcome.NO_FINALIZE,
        ):
            with self.subTest(outcome=outcome), patch(
                "src.plugin_system.apis.send_api.custom_to_stream", new=AsyncMock(return_value=True)
            ) as send_mock:
                class StubAgent:
                    def __init__(self, result):
                        self.result = result
                        self.provider = None

                    async def run(self):
                        return self.result

                with tempfile.TemporaryDirectory() as temp:
                    manager = SandboxManager(temp)
                    handoff = _handoff(stream_id="stream-1", group_id=None)
                    terminal_result = SandboxAgentResult(outcome, handoff.handoff_id, detail="typed failure")
                    result = await SandboxAgentCoordinator(
                        manager=manager,
                        agent_factory=lambda *_args, result=terminal_result, **_kwargs: StubAgent(result),
                        authorize=lambda _handoff: True,
                    ).run_handoff(handoff)
                self.assertEqual(result.outcome, outcome)
                # No exact source row is available, so the production default
                # reporter must not fall back to the current/latest message.
                self.assertEqual(send_mock.await_count, 0)

    async def test_successful_default_publication_has_no_failure_notice(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class Model:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return "", ("", "fake", [Call("write_text", {"path": "out.txt", "content": "done"})])
                return "", ("", "fake", [Call("finalize", {"paths": ["out.txt"]})])

        with tempfile.TemporaryDirectory() as temp, patch(
            "src.plugin_system.apis.send_api.custom_to_stream", new=AsyncMock(return_value=True)
        ) as send_mock:
            manager = SandboxManager(temp)
            handoff = _handoff(stream_id="stream-1", group_id=None)

            def factory(value, scope, *, manager):
                return SandboxAgent(value, scope, llm=Model(), manager=manager)

            result = await SandboxAgentCoordinator(
                manager=manager,
                agent_factory=factory,
                authorize=lambda _handoff: True,
            ).run_handoff(handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(send_mock.await_count, 1)
            self.assertEqual(send_mock.await_args.kwargs["message_type"], "file")

    async def test_default_publication_resolves_relative_manager_path_to_absolute_file(self):
        with tempfile.TemporaryDirectory() as temp:
            relative_base = os.path.relpath(temp, Path.cwd())
            manager = SandboxManager(relative_base)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id="123", actor_id="42")
            expected = scope.path_for_write("out.txt")
            expected.write_text("done", encoding="utf-8")
            handoff = _handoff(stream_id="stream-1", group_id="123")

            with patch(
                "src.plugin_system.apis.send_api.custom_to_stream", new=AsyncMock(return_value=True)
            ) as send_mock:
                published = await SandboxAgentCoordinator(
                    manager=manager,
                    authorize=lambda _handoff: True,
                )._publish_finalized(handoff, ("out.txt",))

            self.assertTrue(published)
            sent_path = Path(send_mock.await_args.kwargs["content"])
            self.assertTrue(sent_path.is_absolute())
            self.assertTrue(sent_path.is_file())
            self.assertEqual(sent_path, expected.resolve())

    async def test_terminal_logging_omits_query_content_and_absolute_paths(self):
        sentinel_query = "SENTINEL_USER_QUERY"
        sentinel_content = "SENTINEL_FILE_CONTENT"
        sentinel_path = str(Path(tempfile.gettempdir()) / "SENTINEL_ABSOLUTE_PATH.txt")

        class Model:
            async def generate_response_async(self, **_kwargs):
                return sentinel_content, ("", "fake", [])

        with tempfile.TemporaryDirectory() as temp, patch(
            "src.chat.sandbox.sandbox_agent.logger"
        ) as log_mock:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(sentinel_query, stream_id="stream-1", group_id=None),
                scope,
                llm=Model(),
                manager=manager,
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.NO_FINALIZE)
            logged = repr(log_mock.method_calls)
            self.assertNotIn(sentinel_query, logged)
            self.assertNotIn(sentinel_content, logged)
            self.assertNotIn(sentinel_path, logged)

    async def test_agent_budget_and_model_errors_are_structured_and_abort(self):
        class WriteForeverModel:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                return "", (
                    "",
                    "fake",
                    [
                        type(
                            "Call",
                            (),
                            {
                                "func_name": "write_text",
                                "args": {"path": f"out-{self.calls}.txt", "content": "x"},
                            },
                        )()
                    ],
                )

        class FailingModel:
            async def generate_response_async(self, **_kwargs):
                raise RuntimeError("model unavailable")

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            handoff = _handoff()
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id="123", actor_id="42")
            budget_agent = SandboxAgent(
                handoff,
                scope,
                llm=WriteForeverModel(),
                manager=manager,
                config=SandboxAgentConfig(max_rounds=2, max_tool_calls=4, first_output_timeout_seconds=2),
            )
            budget_result = await budget_agent.run()
            self.assertEqual(budget_result.outcome, SandboxAgentOutcome.BUDGET_EXHAUSTED)
            self.assertFalse((scope.write_root / "out.txt").exists())

            model_error_agent = SandboxAgent(
                handoff,
                scope,
                llm=FailingModel(),
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=2),
            )
            error_result = await model_error_agent.run()
            self.assertEqual(error_result.outcome, SandboxAgentOutcome.MODEL_ERROR)
            self.assertFalse((scope.write_root / "out.txt").exists())

    async def test_stream_first_output_timeout_is_not_whole_agent_timeout(self):
        class SlowAfterFirstDelta:
            def __init__(self):
                self.calls = 0

            async def stream_response_async(self, **_kwargs):
                self.calls += 1

                async def source():
                    yield {"content": "first output"}
                    await asyncio.sleep(0.06)
                    yield {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "write_text",
                                    "arguments": '{"path":"out.txt","content":"done"}',
                                },
                            },
                            {
                                "index": 1,
                                "function": {
                                    "name": "finalize",
                                    "arguments": '{"paths":["out.txt"]}',
                                },
                            },
                        ]
                    }

                return source()

        self.assertEqual(SandboxAgentConfig().first_output_timeout_seconds, 15.0)
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            model = SlowAfterFirstDelta()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=model,
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.03, max_rounds=4),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(model.calls, 1)

    async def test_model_fallback_progress_resets_to_first_model(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class ModelA:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise asyncio.TimeoutError()
                return "", ("", "a", [Call("finalize", {"paths": ["out.txt"]})])

        class ModelB:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                return "", ("", "b", [Call("write_text", {"path": "out.txt", "content": "done"})])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            first, fallback = ModelA(), ModelB()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[first, fallback],
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01, max_rounds=5),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(first.calls, 2)
            self.assertEqual(fallback.calls, 1)

    async def test_completed_no_tool_response_falls_through_to_next_model(self):
        class StreamingModel:
            def __init__(self, events):
                self.events = events
                self.calls = 0

            async def stream_response_async(self, **_kwargs):
                self.calls += 1
                owner = self

                async def source():
                    for event in owner.events:
                        yield event

                return source()

        no_tools = StreamingModel(({"content": "I can explain this inline."},))
        writer = StreamingModel(
            (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {
                                "name": "write_text",
                                "arguments": '{"path":"out.txt","content":"done"}',
                            },
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "function": {
                                "name": "finalize",
                                "arguments": '{"paths":["out.txt"]}',
                            },
                        }
                    ]
                },
            )
        )
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[no_tools, writer],
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01, max_rounds=1),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(no_tools.calls, 1)
            self.assertEqual(writer.calls, 1)
            self.assertEqual(result.rounds, 1)

    async def test_all_models_are_tried_even_when_model_count_exceeds_round_budget(self):
        class NoToolModel:
            def __init__(self):
                self.calls = 0

            async def stream_response_async(self, **_kwargs):
                self.calls += 1

                async def source():
                    yield {"content": "no tool call"}

                return source()

        models = [NoToolModel() for _ in range(3)]
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=models,
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01, max_rounds=1),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.NO_FINALIZE)
            self.assertEqual([model.calls for model in models], [1, 1, 1])
            self.assertEqual(result.rounds, 0)

    async def test_all_models_exhausted_returns_typed_failure(self):
        class FailingModel:
            async def generate_response_async(self, **_kwargs):
                raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[FailingModel(), FailingModel()],
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01, max_rounds=6),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.MODEL_ERROR)
            self.assertEqual(result.rounds, 0)

    async def test_non_progressing_tool_round_does_not_reset_model_cursor(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class ModelA:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                raise RuntimeError("model A unavailable")

        class ModelB:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                return "", ("", "b", [Call("write_text", {"path": "../escape.txt", "content": "nope"})])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            first, fallback = ModelA(), ModelB()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[first, fallback],
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01, max_rounds=6),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.MODEL_ERROR)
            self.assertEqual(first.calls, 1)
            # A rejected sandbox tool gets bounded same-model correction turns
            # before the configured model is considered exhausted.
            self.assertEqual(fallback.calls, 3)

    async def test_single_model_recovers_after_invalid_tool_call(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class CorrectingModel:
            def __init__(self):
                self.calls = 0
                self.prompts: list[str] = []

            async def generate_response_async(self, **kwargs):
                self.calls += 1
                self.prompts.append(kwargs["prompt"])
                if self.calls == 1:
                    return "", ("", "fake", [Call("write_text", {"path": "../escape.txt", "content": "secret"})])
                if self.calls == 2:
                    return "", ("", "fake", [Call("write_text", {"path": "out.txt", "content": "done"})])
                return "", ("", "fake", [Call("finalize", {"paths": ["out.txt"]})])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            model = CorrectingModel()
            with patch("src.chat.sandbox.sandbox_agent.logger") as log_mock:
                result = await SandboxAgent(
                    _handoff(stream_id="stream-1", group_id=None),
                    scope,
                    llm=model,
                    manager=manager,
                    config=SandboxAgentConfig(first_output_timeout_seconds=0.01, max_rounds=6),
                ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(model.calls, 3)
            displayed_rounds = [
                call.args[1]
                for call in log_mock.info.call_args_list
                if call.args and call.args[0].startswith("sandbox agent round:")
            ]
            self.assertEqual(displayed_rounds, [1, 2, 3])
            self.assertIn("invalid_path", model.prompts[1])
            self.assertNotIn("../escape.txt", model.prompts[1])
            self.assertNotIn("secret", model.prompts[1])

    async def test_non_progress_threshold_fails_over_to_next_model(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class RejectingModel:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                return "", ("", "rejecting", [Call("write_text", {"path": "../escape.txt", "content": "x"})])

        class FallbackModel:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                return "", (
                    "",
                    "fallback",
                    [
                        Call("write_text", {"path": "out.txt", "content": "done"}),
                        Call("finalize", {"paths": ["out.txt"]}),
                    ],
                )

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            rejecting, fallback = RejectingModel(), FallbackModel()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[rejecting, fallback],
                manager=manager,
                config=SandboxAgentConfig(
                    first_output_timeout_seconds=0.01,
                    max_rounds=8,
                    non_progress_limit=2,
                ),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(rejecting.calls, 2)
            self.assertEqual(fallback.calls, 1)

    async def test_duplicate_retry_reaches_duplicate_limit_without_model_error(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class DuplicateModel:
            def __init__(self):
                self.calls = 0

            async def generate_response_async(self, **_kwargs):
                self.calls += 1
                return "", ("", "duplicate", [Call("write_text", {"path": "out.txt", "content": "same"})])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            model = DuplicateModel()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=model,
                manager=manager,
                config=SandboxAgentConfig(
                    first_output_timeout_seconds=0.01,
                    duplicate_limit=2,
                    non_progress_limit=6,
                ),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.DUPLICATE_STALL)
            self.assertEqual(model.calls, 3)

    async def test_post_first_output_complete_attempt_timeout_is_bounded(self):
        class SlowAfterFirstOutput:
            async def stream_response_async(self, **_kwargs):
                async def source():
                    yield {"content": "started"}
                    await asyncio.sleep(10)

                return source()

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            started = time.monotonic()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=SlowAfterFirstOutput(),
                manager=manager,
                config=SandboxAgentConfig(
                    first_output_timeout_seconds=0.01,
                    complete_attempt_timeout_seconds=0.03,
                ),
            ).run()
            elapsed = time.monotonic() - started
            self.assertEqual(result.outcome, SandboxAgentOutcome.TIMEOUT)
            self.assertLess(elapsed, 0.5)

    async def test_cancellation_resistant_stream_cleanup_is_bounded(self):
        release = asyncio.Event()

        class ResistantModel:
            def __init__(self):
                self.task_done = False

            async def stream_response_async(self, **_kwargs):
                owner = self

                async def source():
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        await release.wait()
                        raise
                    finally:
                        owner.task_done = True
                    if False:
                        yield None

                return source()

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            model = ResistantModel()
            started = time.monotonic()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=model,
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01),
            ).run()
            elapsed = time.monotonic() - started
            self.assertEqual(result.outcome, SandboxAgentOutcome.TIMEOUT)
            self.assertLess(elapsed, 0.5)
            release.set()
            await asyncio.sleep(0)

    async def test_failed_observation_uses_stable_codes_and_validated_paths_only(self):
        class Call:
            def __init__(self, name, args):
                self.func_name = name
                self.args = args

        class ObservationModel:
            def __init__(self):
                self.calls = 0
                self.prompts: list[str] = []

            async def generate_response_async(self, **kwargs):
                self.calls += 1
                self.prompts.append(kwargs["prompt"])
                if self.calls == 1:
                    return "", (
                        "",
                        "fake",
                        [Call("write_text", {"path": r"C:\secret\file.txt", "content": "SENTINEL_CONTENT"})],
                    )
                return "plain", ("", "fake", [])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            model = ObservationModel()
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=model,
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01, max_rounds=8),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.MODEL_ERROR)
            self.assertIn('"error_code":"invalid_path"', model.prompts[1])
            self.assertNotIn(r"C:\secret\file.txt", model.prompts[1])
            self.assertNotIn("SENTINEL_CONTENT", model.prompts[1])

    def test_tool_descriptions_state_relative_path_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id="123", actor_id="42")
            provider = SandboxProvider(scope, _handoff(stream_id="s"), manager=manager)
            descriptions = {item["name"]: item["description"] for item in provider.tool_definitions()}
            combined = " ".join(descriptions.values()).lower()
            self.assertIn("sandbox-relative", combined)
            self.assertIn("empty string", combined)
            self.assertIn("never use absolute", combined)
            self.assertIn("group-visible", combined)
            self.assertIn("actor-relative", combined)
            self.assertIn("actor-prefixed", combined)
            self.assertIn("side-effect-free", combined)

    async def test_prompt_requests_direct_write_and_finalize_but_keeps_inspection_tools(self):
        prompts = []
        tool_names = []

        class Model:
            async def generate_response_async(self, **kwargs):
                prompts.append(kwargs["prompt"])
                tool_names.extend(item["name"] for item in kwargs["tools"])
                return "plain", ("", "fake", [])

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff("create a new hello file", stream_id="stream-1", group_id=None),
                scope,
                llm=Model(),
                manager=manager,
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.NO_FINALIZE)
            self.assertIn("write_text followed by finalize", prompts[0])
            self.assertTrue({"list_tree", "read_text", "search_text", "write_text", "finalize"}.issubset(tool_names))

    async def test_stream_first_output_watchdog_ignores_empty_chunks_and_falls_through(self):
        class StreamingModel:
            def __init__(self, *, delay=0.0, events=(), error=None):
                self.delay = delay
                self.events = events
                self.error = error
                self.calls = 0
                self.closed = False

            async def stream_response_async(self, **_kwargs):
                self.calls += 1
                owner = self

                async def source():
                    try:
                        for event in owner.events:
                            if isinstance(event, (int, float)):
                                await asyncio.sleep(event)
                            else:
                                yield event
                        if owner.delay:
                            await asyncio.sleep(owner.delay)
                        if owner.error is not None:
                            raise owner.error
                    finally:
                        owner.closed = True

                return source()

        empty = StreamingModel(
            events=(type("Empty", (), {"choices": [], "usage": None})(),),
            delay=0.05,
        )
        fallback = StreamingModel(events=({"content": "fallback"},))
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[empty, fallback],
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.NO_FINALIZE)
            self.assertEqual(empty.calls, 1)
            self.assertEqual(fallback.calls, 1)
            self.assertTrue(empty.closed)

    async def test_stream_timeout_closes_async_close_only_stream_and_leaves_no_task(self):
        class CloseOnlyStream:
            def __init__(self):
                self.close_calls = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(10)
                raise StopAsyncIteration

            async def close(self):
                self.close_calls += 1

        class StreamingModel:
            def __init__(self):
                self.stream = None

            async def stream_response_async(self, **_kwargs):
                self.stream = CloseOnlyStream()
                return self.stream

        model = StreamingModel()
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=model,
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.TIMEOUT)
            self.assertIsNotNone(model.stream)
            self.assertEqual(model.stream.close_calls, 1)
            await asyncio.sleep(0)
            self.assertFalse(
                [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
            )

    async def test_observed_provider_stream_close_is_idempotent_for_async_close_only_api(self):
        class CloseOnlyStream:
            def __init__(self):
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1

        stream = CloseOnlyStream()
        observed = _ObservedAsyncIterator(stream, lambda _event: None)
        await observed.aclose()
        await observed.aclose()
        self.assertEqual(stream.close_calls, 1)

    async def test_stream_meaningful_delta_allows_generation_past_first_output_deadline(self):
        class StreamingModel:
            async def stream_response_async(self, **_kwargs):
                async def source():
                    yield {"reasoning_content": "thinking"}
                    await asyncio.sleep(0.03)
                    yield {"content": "finished"}

                return source()

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=StreamingModel(),
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.NO_FINALIZE)

    async def test_stream_partial_then_error_falls_through_to_next_model(self):
        class StreamingModel:
            def __init__(self, events=(), error=None):
                self.events = events
                self.error = error
                self.calls = 0

            async def stream_response_async(self, **_kwargs):
                self.calls += 1
                owner = self

                async def source():
                    for event in owner.events:
                        yield event
                    if owner.error is not None:
                        raise owner.error

                return source()

        partial = StreamingModel(events=({"content": "partial"},), error=RuntimeError("stream failed"))
        complete = StreamingModel(
            events=(
                {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": '{"path":"out.txt","content":"done"}'}}]},
                {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": '{"paths":["out.txt"]}'}}]},
            )
        )
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[partial, complete],
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(partial.calls, 1)
            self.assertEqual(complete.calls, 1)

    async def test_streamed_tool_call_deltas_are_assembled_and_executed_in_order(self):
        class StreamingModel:
            async def stream_response_async(self, **_kwargs):
                async def source():
                    yield {"tool_calls": [{"index": 0, "function": {"name": "write_text", "arguments": '{"path":"out.txt",'}}]}
                    yield {"tool_calls": [{"index": 0, "function": {"arguments": '"content":"done"}'}}]}
                    yield {"tool_calls": [{"index": 1, "function": {"name": "finalize", "arguments": '{"paths":["out.txt"]}'}}]}

                return source()

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            handoff = _handoff(stream_id="stream-1", group_id=None)

            def factory(value, scope, *, manager):
                return SandboxAgent(
                    value,
                    scope,
                    llm=StreamingModel(),
                    manager=manager,
                    config=SandboxAgentConfig(first_output_timeout_seconds=0.01),
                )

            result = await SandboxAgentCoordinator(
                manager=manager,
                agent_factory=factory,
                authorize=lambda _handoff: True,
                publish=lambda _handoff, _paths: True,
            ).run_handoff(handoff)
            self.assertEqual(result.outcome, SandboxAgentOutcome.FINALIZED)
            self.assertEqual(result.tool_calls, 2)
            self.assertEqual((Path(temp) / "stream-1" / "out.txt").read_text(encoding="utf-8"), "done")

    async def test_stream_all_models_fail_and_no_agent_wide_timeout(self):
        class FailingStream:
            async def stream_response_async(self, **_kwargs):
                raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="stream-1", platform="qq", group_id=None, actor_id="42")
            result = await SandboxAgent(
                _handoff(stream_id="stream-1", group_id=None),
                scope,
                llm=[FailingStream(), FailingStream()],
                manager=manager,
                config=SandboxAgentConfig(first_output_timeout_seconds=0.01),
            ).run()
            self.assertEqual(result.outcome, SandboxAgentOutcome.MODEL_ERROR)
            self.assertNotIn("wait_for(self._run_loop", inspect.getsource(SandboxAgent.run))

    async def test_finalize_intent_rechecks_actor_and_staging_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SandboxManager(temp)
            scope = manager.get_scope(stream_id="s", platform="qq", group_id=None, actor_id="42")
            provider = SandboxProvider(scope, _handoff(stream_id="s", group_id=None), manager=manager)
            self.assertTrue(provider.write_text("out.txt", "done")["ok"])
            intent = provider.finalize()["intent"]
            with self.assertRaises(ValueError):
                provider.commit(replace(intent, actor_id="77"))
            with self.assertRaises(ValueError):
                provider.commit(replace(intent, staging_dir=Path(temp) / "forged-staging"))
            provider.abort()

    async def test_confirmation_is_strict_and_raw_json_never_surfaces(self):
        candidate = SandboxEditCandidate.mint(
            stream_id="s", platform="qq", group_id=None, actor_id="42", source_message_id="m", query="edit"
        )
        accepted = parse_sandbox_confirmation(
            '{"sandbox_edit_decision":true,"reply_to_user":"好的，我来处理。","file_edit_query":"edit"}',
            candidate,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.content, "好的，我来处理。")
        self.assertIsNotNone(accepted.handoff)
        self.assertTrue(accepted.handoff.matches_acknowledgement(accepted.content))
        invalid = parse_sandbox_confirmation('{"sandbox_edit_decision":true}', candidate)
        self.assertFalse(invalid.accepted)
        self.assertNotIn("sandbox_edit_decision", invalid.content)
        refused = parse_sandbox_confirmation(
            '{"sandbox_edit_decision":false,"reply_to_user":"我先不修改文件。","file_edit_query":""}',
            candidate,
        )
        self.assertFalse(refused.accepted)
        self.assertIsNone(refused.handoff)
        self.assertEqual(refused.content, "我先不修改文件。")

    async def test_accepted_ack_survives_replyset_postprocessing_and_delivery_gate(self):
        candidate = SandboxEditCandidate.mint(
            stream_id="stream-1",
            platform="qq",
            group_id=None,
            actor_id="42",
            source_message_id="message-1",
            query="edit the text",
        )
        envelope = parse_sandbox_confirmation(
            '{"sandbox_edit_decision":true,"reply_to_user":"请稍等，我来处理。",'
            '"file_edit_query":"edit the text"}',
            candidate,
        )
        self.assertTrue(envelope.accepted)
        self.assertIsNotNone(envelope.handoff)
        handoff = envelope.handoff

        class Replyer:
            chat_stream = SimpleNamespace(stream_id="stream-1")

            async def generate_reply_with_context(self, **_kwargs):
                return True, LLMGenerationDataModel(
                    content=envelope.content,
                    sandbox_edit_handoff=handoff,
                )

        with patch.object(generator_api, "get_replyer", return_value=Replyer()), patch.object(
            generator_api,
            "process_human_text",
            side_effect=AssertionError("sandbox acknowledgement must bypass human-text processing"),
        ):
            success, response = await generator_api.generate_reply(
                chat_stream=SimpleNamespace(stream_id="stream-1"),
                enable_splitter=True,
                enable_chinese_typo=True,
            )

        self.assertTrue(success)
        self.assertIsNotNone(response)
        self.assertIsNotNone(response.reply_set)
        delivered_content = "".join(
            item.content for item in response.reply_set.reply_data if item.content_type.value == "text"
        )
        self.assertEqual(delivered_content, envelope.content)

        started: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        gate = SandboxDeliveryGate(runner=runner)
        receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="ack-1")
        self.assertTrue(
            await gate.claim_after_delivery(
                handoff,
                [receipt],
                delivered_content=delivered_content,
            )
        )
        await gate.wait_for_tasks()
        self.assertEqual(started, [handoff.handoff_id])

        filtered_gate = SandboxDeliveryGate(runner=runner)
        filtered_receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="ack-filtered")
        self.assertFalse(
            await filtered_gate.claim_after_delivery(
                handoff,
                [filtered_receipt],
                delivered_content="Filtered",
            )
        )
        await filtered_gate.wait_for_tasks()
        self.assertEqual(started, [handoff.handoff_id])

    async def test_upload_uses_actual_sender_subtree_in_group(self):
        from src.chat.message_receive.message import MessageRecv

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "incoming.txt"
            source.write_text("uploaded", encoding="utf-8")
            manager = SandboxManager(Path(temp) / "sandbox")
            message = object.__new__(MessageRecv)
            message.chat_stream = SimpleNamespace(
                stream_id="stream-1",
                platform="qq",
                group_info=SimpleNamespace(group_id="123"),
                user_info=SimpleNamespace(user_id="group-owner"),
            )
            message.message_info = SimpleNamespace(
                sender_info=SimpleNamespace(user_id="77"),
                user_info=SimpleNamespace(user_id="group-owner"),
            )
            with patch("src.chat.message_receive.message.sandbox_manager", manager):
                saved = await message._save_file_to_sandbox(str(source), "incoming.txt")
            self.assertEqual(Path(saved).parent.name, "77")
            self.assertEqual(Path(saved).read_text(encoding="utf-8"), "uploaded")

    async def test_delivery_gate_requires_delivered_and_claims_once(self):
        acknowledgement = "好的，我来处理。"
        handoff = _handoff(acknowledgement=acknowledgement)
        started: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        gate = SandboxDeliveryGate(runner=runner)
        suppressed = type("Receipt", (), {"delivered": False, "message_id": "x", "status": "suppressed"})()
        delivered = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="m1")
        self.assertFalse(await gate.claim_after_delivery(handoff, [suppressed]))
        results = await asyncio.gather(
            gate.claim_after_delivery(handoff, [delivered], delivered_content=acknowledgement),
            gate.claim_after_delivery(handoff, [delivered], delivered_content=acknowledgement),
        )
        await gate.wait_for_tasks()
        self.assertEqual(sum(results), 1)
        self.assertEqual(started, [handoff.handoff_id])

    async def test_delivery_gate_rejects_string_status_even_when_value_matches(self):
        handoff = _handoff(acknowledgement="好的，我来处理。")
        started: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        gate = SandboxDeliveryGate(runner=runner)
        fake = type(
            "Receipt",
            (),
            {"status": "delivered", "stream_id": "stream-1", "message_id": "fake"},
        )()
        self.assertFalse(await gate.claim_after_delivery(handoff, [fake]))
        await gate.wait_for_tasks()
        self.assertEqual(started, [])

    async def test_delivery_gate_requires_real_status_and_rechecks_authorization(self):
        acknowledgement = "好的，我来处理。"
        handoff = _handoff(acknowledgement=acknowledgement)
        started: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        gate = SandboxDeliveryGate(runner=runner)
        inconsistent = type("Receipt", (), {"delivered": True, "message_id": "bad", "status": SendStatus.FAILED})()
        self.assertFalse(await gate.claim_after_delivery(handoff, [inconsistent]))
        real_receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="good")
        self.assertFalse(
            await gate.claim_after_delivery(
                handoff,
                [real_receipt],
                authorize=lambda _value: False,
                delivered_content=acknowledgement,
            )
        )
        self.assertTrue(
            await gate.claim_after_delivery(
                handoff,
                [real_receipt],
                authorize=lambda _value: True,
                delivered_content=acknowledgement,
            )
        )
        await gate.wait_for_tasks()
        self.assertEqual(started, [handoff.handoff_id])

    async def test_delivery_gate_rejects_delivered_receipt_for_other_stream(self):
        handoff = _handoff(stream_id="stream-1", acknowledgement="好的，我来处理。")
        started: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        gate = SandboxDeliveryGate(runner=runner)
        wrong_stream = SendReceipt(SendStatus.DELIVERED, "stream-2", message_id="wrong-stream")
        self.assertFalse(await gate.claim_after_delivery(handoff, [wrong_stream]))
        await gate.wait_for_tasks()
        self.assertEqual(started, [])

    async def test_delivery_gate_requires_exact_acknowledgement_content(self):
        handoff = _handoff(acknowledgement="好的，我来处理。")
        started: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        gate = SandboxDeliveryGate(runner=runner)
        receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="ack")
        self.assertFalse(await gate.claim_after_delivery(handoff, [receipt], delivered_content="Filtered"))
        self.assertTrue(
            await gate.claim_after_delivery(
                handoff,
                [receipt],
                delivered_content="好的，我来处理。",
            )
        )
        await gate.wait_for_tasks()
        self.assertEqual(started, [handoff.handoff_id])

    async def test_delivery_gate_runner_inherits_lease_and_uses_legacy_send_path(self):
        from src.plugin_system.apis import send_api

        acknowledgement = "好的，我来处理。"
        handoff = _handoff(acknowledgement=acknowledgement)
        origin = FocusLease("focus-group", "stream-1", 7, "origin-turn")
        runner_leases: list[FocusLease | None] = []
        permitted_sender = AsyncMock(return_value=SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="published"))
        fenced_sender = AsyncMock(side_effect=AssertionError("sandbox runner used fenced receipt sender"))

        async def runner(_value):
            runner_leases.append(current_context_lease())
            self.assertTrue(
                await send_api.custom_to_stream(
                    message_type="text",
                    content="sandbox publication",
                    stream_id="stream-1",
                )
            )

        gate = SandboxDeliveryGate(runner=runner)
        receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="ack-inherited")
        with (
            patch.object(send_api, "_send_to_target_receipt_permitted", permitted_sender),
            patch.object(send_api, "_send_to_target_receipt", fenced_sender),
            bind_lease(origin),
        ):
            self.assertIs(current_context_lease(), origin)
            self.assertTrue(
                await gate.claim_after_delivery(
                    handoff,
                    [receipt],
                    authorize=lambda _value: True,
                    delivered_content=acknowledgement,
                )
            )
            self.assertIs(current_context_lease(), origin)
            await gate.wait_for_tasks()
            self.assertIs(current_context_lease(), origin)

        self.assertEqual(runner_leases, [origin])
        permitted_sender.assert_awaited_once()
        fenced_sender.assert_not_awaited()

    async def test_delivery_gate_rejects_missing_acknowledgement_and_content(self):
        handoff = _handoff()
        started: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        gate = SandboxDeliveryGate(runner=runner)
        receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="ack-missing")
        self.assertFalse(await gate.claim_after_delivery(handoff, [receipt]))
        self.assertFalse(
            await gate.claim_after_delivery(
                handoff,
                [receipt],
                delivered_content="好的，我来处理。",
            )
        )
        await gate.wait_for_tasks()
        self.assertEqual(started, [])

    async def test_delivery_gate_observes_background_failure_without_unhandled_task(self):
        acknowledgement = "好的，我来处理。"
        handoff = _handoff(acknowledgement=acknowledgement)

        async def runner(_value):
            raise RuntimeError("background model failure")

        with patch("src.chat.sandbox.sandbox_delivery.logger.error") as log_exception:
            gate = SandboxDeliveryGate(runner=runner)
            receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="m-failure")
            self.assertTrue(
                await gate.claim_after_delivery(
                    handoff,
                    [receipt],
                    delivered_content=acknowledgement,
                )
            )
            await gate.wait_for_tasks()
            await asyncio.sleep(0)
            self.assertEqual(log_exception.call_count, 1)
            self.assertNotIn(handoff.handoff_id, repr(log_exception.call_args))

    async def test_reply_prompt_build_result_is_typed_and_call_local(self):
        self.assertTrue(is_dataclass(ReplyPromptBuildResult))
        self.assertEqual(
            {field.name for field in ReplyPromptBuildResult.__dataclass_fields__.values()},
            {"prompt", "selected_expressions", "sandbox_candidate"},
        )
        result = ReplyPromptBuildResult("prompt")
        self.assertEqual(result.prompt, "prompt")
        self.assertIsNone(result.sandbox_candidate)

    async def test_group_replyer_disabled_tool_modes_do_not_keep_sandbox_candidate(self):
        from src.chat.replyer.group_generator import DefaultReplyer

        class _MCP:
            def get_tool_catalog_summary(self, **_kwargs):
                return ""

        chat_stream = SimpleNamespace(
            platform="qq",
            group_info=SimpleNamespace(group_id="123"),
            user_info=SimpleNamespace(user_id="42"),
            context=SimpleNamespace(
                message=SimpleNamespace(
                    message_info=SimpleNamespace(
                        additional_config={"runtime_capabilities": {"tool_mode": "standard"}}
                    )
                )
            ),
        )
        replyer = object.__new__(DefaultReplyer)
        replyer.chat_stream = chat_stream
        replyer.mcp_executor = _MCP()
        replyer.web_search_manager = SimpleNamespace(is_available=False)
        fake_global = SimpleNamespace(
            advanced=SimpleNamespace(admins=["42"]),
            bot=SimpleNamespace(sandbox_whitelist=[], nickname="bot"),
        )
        fake_models = SimpleNamespace(
            model_task_config=SimpleNamespace(file_edit=SimpleNamespace(model_list=["file-edit"]))
        )
        with (
            patch("src.chat.replyer.group_generator.global_config", fake_global),
            patch("src.chat.replyer.group_generator.model_config", fake_models),
            patch("src.chat.replyer.group_generator.access_context_from_stream", return_value=None),
        ):
            for mode, disable_tools in (("mcp_only", False), ("disabled", False), ("standard", True)):
                chat_stream.context.message.message_info.additional_config = {
                    "runtime_capabilities": {"tool_mode": mode},
                    "disable_tools": disable_tools,
                }
                result = await replyer.build_tool_info("history", "sender", "edit file", user_id="42")
                self.assertIsNone(result.sandbox_edit_candidate, mode)

    async def test_delivery_gate_default_authorizer_runs_before_claim(self):
        acknowledgement = "好的，我来处理。"
        handoff = _handoff(acknowledgement=acknowledgement)
        started: list[str] = []
        authorized: list[str] = []

        async def runner(value):
            started.append(value.handoff_id)

        def authorize(value):
            authorized.append(value.actor_id)
            return True

        gate = SandboxDeliveryGate(runner=runner, authorize=authorize)
        receipt = SendReceipt(SendStatus.DELIVERED, "stream-1", message_id="m-auth")
        self.assertTrue(
            await gate.claim_after_delivery(
                handoff,
                [receipt],
                delivered_content=acknowledgement,
            )
        )
        await gate.wait_for_tasks()
        self.assertEqual(authorized, [handoff.actor_id])
        self.assertEqual(started, [handoff.handoff_id])


class SandboxCompletionReportTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _source_message(handoff: SandboxEditHandoff, *, actor_id: str | None = None) -> SimpleNamespace:
        actor = actor_id or handoff.actor_id
        group_info = (
            SimpleNamespace(group_id=handoff.group_id, group_platform=handoff.platform)
            if handoff.group_id
            else None
        )
        return SimpleNamespace(
            message_id=handoff.source_message_id,
            chat_id=handoff.stream_id,
            user_id=actor,
            user_info=SimpleNamespace(user_id=actor, platform=handoff.platform),
            chat_info=SimpleNamespace(
                stream_id=handoff.stream_id,
                platform=handoff.platform,
                group_info=group_info,
            ),
        )

    @staticmethod
    def _generator(content: str = "完成报告。", *, handoff: object = None) -> SimpleNamespace:
        class Generator:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def generate_reply(self, **kwargs):
                self.calls.append(kwargs)
                return True, SimpleNamespace(content=content, sandbox_edit_handoff=handoff)

        return Generator()

    @staticmethod
    def _sender(statuses: list[SendStatus] | None = None) -> SimpleNamespace:
        class Sender:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.statuses = list(statuses or [SendStatus.DELIVERED])

            async def background_text_to_stream_receipt(self, **kwargs):
                self.calls.append(kwargs)
                status = self.statuses.pop(0) if self.statuses else SendStatus.DELIVERED
                return SendReceipt(status, kwargs["stream_id"], message_id=f"report-{len(self.calls)}")

        return Sender()

    async def test_default_report_uses_exact_source_and_natural_bounded_context(self):
        handoff = _handoff(
            query='edit <task attr="x">the text</task>',
            stream_id="report-stream",
            group_id="group-1",
            source_message_id="source-1",
        )
        source = self._source_message(handoff)
        generator = self._generator()
        sender = self._sender()
        result = SandboxAgentResult(
            SandboxAgentOutcome.FINALIZED,
            handoff.handoff_id,
            changed_paths=("done.txt", "../escape.txt", r"C:\\secret.txt"),
            detail="RAW_DETAIL_MUST_NOT_REACH_REPORT",
            rounds=7,
            tool_calls=11,
            response='已检查 C:\\secret.txt，<answer>handoff id abcdef0123456789。</answer>',
        )
        coordinator = SandboxAgentCoordinator(
            completion_report_claims=set(),
            source_message_lookup=lambda stream_id, message_id: [source]
            if (stream_id, message_id) == (handoff.stream_id, handoff.source_message_id)
            else [],
            generator_api=generator,
            send_api=sender,
        )

        await coordinator._finish(handoff, result)

        self.assertEqual(len(generator.calls), 1)
        generation_call = generator.calls[0]
        self.assertEqual(generation_call["chat_id"], handoff.stream_id)
        self.assertIs(generation_call["reply_message"], source)
        self.assertFalse(generation_call["enable_tool"])
        self.assertFalse(generation_call["enable_splitter"])
        self.assertFalse(generation_call["enable_chinese_typo"])
        self.assertEqual(generation_call["request_type"], "sandbox.completion_report")
        serialized = generation_call["extra_info"]
        self.assertIn("已经结束的 Sandbox 任务", serialized)
        self.assertIn("不要继续执行任务", serialized)
        self.assertIn("不要让用户等待", serialized)
        self.assertIn("不要请求工具", serialized)
        self.assertIn("原始任务是", serialized)
        self.assertIn("代理回答是", serialized)
        self.assertNotIn("不可信数据", serialized)
        self.assertNotIn("untrusted", serialized)
        self.assertNotIn("<", serialized)
        self.assertNotIn(">", serialized)
        self.assertNotIn("<sandbox", serialized)
        self.assertNotIn("</", serialized)
        self.assertNotIn("{", serialized)
        self.assertNotIn("}", serialized)
        self.assertNotIn("FINALIZED", serialized)
        self.assertNotIn("NO_FINALIZE", serialized)
        self.assertNotIn("RAW_DETAIL_MUST_NOT_REACH_REPORT", serialized)
        self.assertNotIn("C:\\\\secret.txt", serialized)
        self.assertIn("[path]", serialized)
        self.assertEqual(len(sender.calls), 1)
        self.assertIs(sender.calls[0]["reply_message"], source)
        self.assertTrue(sender.calls[0]["set_reply"])
        self.assertEqual(sender.calls[0]["text"], "完成报告。")

    async def test_missing_ambiguous_and_mismatched_source_fail_closed(self):
        for lookup_result in (
            [],
            [SimpleNamespace(), SimpleNamespace()],
        ):
            with self.subTest(kind="missing_or_ambiguous"):
                handoff = _handoff(group_id=None)
                generator = self._generator()
                sender = self._sender()
                coordinator = SandboxAgentCoordinator(
                    completion_report_claims=set(),
                    source_message_lookup=lambda _stream, _message, value=lookup_result: value,
                    generator_api=generator,
                    send_api=sender,
                )
                await coordinator._finish(
                    handoff,
                    SandboxAgentResult(SandboxAgentOutcome.NO_FINALIZE, handoff.handoff_id),
                )
                self.assertEqual(generator.calls, [])
                self.assertEqual(len(sender.calls), 1)
                self.assertEqual(sender.calls[0]["stream_id"], handoff.stream_id)
                self.assertFalse(sender.calls[0]["set_reply"])
                self.assertIsNone(sender.calls[0]["reply_message"])

        handoff = _handoff(group_id="group-1")
        mismatched = self._source_message(handoff, actor_id="other-actor")
        generator = self._generator()
        sender = self._sender()
        coordinator = SandboxAgentCoordinator(
            completion_report_claims=set(),
            source_message_lookup=lambda _stream, _message: [mismatched],
            generator_api=generator,
            send_api=sender,
        )
        await coordinator._finish(
            handoff,
            SandboxAgentResult(SandboxAgentOutcome.MODEL_ERROR, handoff.handoff_id),
        )
        self.assertEqual(generator.calls, [])
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0]["stream_id"], handoff.stream_id)
        self.assertFalse(sender.calls[0]["set_reply"])
        self.assertIsNone(sender.calls[0]["reply_message"])

    async def test_generation_failures_get_one_bound_fixed_fallback(self):
        handoff = _handoff(group_id=None)
        source = self._source_message(handoff)

        class Generator:
            def __init__(self, behavior: str) -> None:
                self.behavior = behavior
                self.calls: list[dict[str, object]] = []

            async def generate_reply(self, **kwargs):
                self.calls.append(kwargs)
                if self.behavior == "exception":
                    raise RuntimeError("generator unavailable")
                if self.behavior == "failed":
                    return False, None
                if self.behavior == "invalid":
                    return {"not": "a tuple"}
                if self.behavior == "second_handoff":
                    return True, SimpleNamespace(content="must not send", sandbox_edit_handoff=handoff)
                return True, SimpleNamespace(content="")

        for behavior in ("exception", "failed", "invalid", "second_handoff", "empty"):
            with self.subTest(behavior=behavior):
                generator = Generator(behavior)
                sender = self._sender()
                coordinator = SandboxAgentCoordinator(
                    completion_report_claims=set(),
                    source_message_lookup=lambda _stream, _message: [source],
                    generator_api=generator,
                    send_api=sender,
                )
                await coordinator._finish(
                    handoff,
                    SandboxAgentResult(SandboxAgentOutcome.PUBLICATION_FAILED, handoff.handoff_id),
                )
                self.assertEqual(len(generator.calls), 1)
                self.assertEqual(len(sender.calls), 1)
                self.assertEqual(sender.calls[0]["stream_id"], handoff.stream_id)
                self.assertTrue(sender.calls[0]["set_reply"])
                self.assertIs(sender.calls[0]["reply_message"], source)
                self.assertEqual(
                    sender.calls[0]["text"],
                    "唔…文件已经改好了，可是附件投递出了问题，猫猫没能送到你这里(´･ω･`)",
                )

    async def test_concurrent_finish_claims_one_report(self):
        handoff = _handoff(group_id=None)
        calls: list[str] = []
        entered = asyncio.Event()

        async def reporter(value, _result):
            calls.append(value.handoff_id)
            entered.set()
            await asyncio.sleep(0)

        coordinator = SandboxAgentCoordinator(completion_reporter=reporter, completion_report_claims=set())
        result = SandboxAgentResult(SandboxAgentOutcome.FINALIZED, handoff.handoff_id)
        await asyncio.gather(
            coordinator._finish(handoff, result),
            coordinator._finish(handoff, result),
        )
        self.assertTrue(entered.is_set())
        self.assertEqual(calls, [handoff.handoff_id])

    async def test_suppressed_stale_and_exception_never_trigger_second_send(self):
        handoff = _handoff(group_id=None)
        source = self._source_message(handoff)
        for status in (SendStatus.SUPPRESSED, SendStatus.STALE_LEASE):
            with self.subTest(status=status):
                generator = self._generator()
                sender = self._sender([status])
                coordinator = SandboxAgentCoordinator(
                    completion_report_claims=set(),
                    source_message_lookup=lambda _stream, _message: [source],
                    generator_api=generator,
                    send_api=sender,
                )
                await coordinator._finish(
                    handoff,
                    SandboxAgentResult(SandboxAgentOutcome.TIMEOUT, handoff.handoff_id),
                )
                self.assertEqual(len(sender.calls), 1)

        class RaisingSender:
            def __init__(self):
                self.calls = 0

            async def background_text_to_stream_receipt(self, **_kwargs):
                self.calls += 1
                raise RuntimeError("unknown delivery")

        generator = self._generator()
        sender = RaisingSender()
        coordinator = SandboxAgentCoordinator(
            completion_report_claims=set(),
            source_message_lookup=lambda _stream, _message: [source],
            generator_api=generator,
            send_api=sender,
        )
        await coordinator._finish(
            handoff,
            SandboxAgentResult(SandboxAgentOutcome.MODEL_ERROR, handoff.handoff_id),
        )
        self.assertEqual(sender.calls, 1)

    async def test_explicit_failed_receipt_allows_one_fixed_fallback(self):
        handoff = _handoff(group_id=None)
        source = self._source_message(handoff)
        generator = self._generator("generated report")
        sender = self._sender([SendStatus.FAILED, SendStatus.DELIVERED])
        coordinator = SandboxAgentCoordinator(
            completion_report_claims=set(),
            source_message_lookup=lambda _stream, _message: [source],
            generator_api=generator,
            send_api=sender,
        )
        await coordinator._finish(
            handoff,
            SandboxAgentResult(SandboxAgentOutcome.PUBLICATION_FAILED, handoff.handoff_id),
        )
        self.assertEqual(len(sender.calls), 2)
        self.assertIs(sender.calls[0]["reply_message"], source)
        self.assertIs(sender.calls[1]["reply_message"], source)
        self.assertIn("文件已经改好了", sender.calls[1]["text"])
        self.assertIn("附件投递出了问题", sender.calls[1]["text"])

    async def test_report_generation_rejects_second_sandbox_handoff(self):
        handoff = _handoff(group_id=None)
        source = self._source_message(handoff)
        generator = self._generator("must not send", handoff=handoff)
        sender = self._sender()
        coordinator = SandboxAgentCoordinator(
            completion_report_claims=set(),
            source_message_lookup=lambda _stream, _message: [source],
            generator_api=generator,
            send_api=sender,
        )
        await coordinator._finish(
            handoff,
            SandboxAgentResult(SandboxAgentOutcome.FINALIZED, handoff.handoff_id),
        )
        self.assertEqual(len(generator.calls), 1)
        self.assertEqual(len(sender.calls), 1)
        self.assertTrue(sender.calls[0]["set_reply"])
        self.assertIs(sender.calls[0]["reply_message"], source)
        self.assertEqual(sender.calls[0]["text"], "唔…猫猫已经查完啦，这次没有要交付的文件(´･ω･`)")

    async def test_notify_false_cancellation_is_silent(self):
        handoff = _handoff(group_id=None)
        reporter = AsyncMock()
        coordinator = SandboxAgentCoordinator(completion_reporter=reporter, completion_report_claims=set())
        result = SandboxAgentResult(SandboxAgentOutcome.CANCELLED, handoff.handoff_id)
        await coordinator._finish(handoff, result, notify=False)
        reporter.assert_not_awaited()

    async def test_notify_true_cancellation_reports_once(self):
        handoff = _handoff(group_id=None)
        reporter = AsyncMock()
        coordinator = SandboxAgentCoordinator(completion_reporter=reporter, completion_report_claims=set())
        result = SandboxAgentResult(SandboxAgentOutcome.CANCELLED, handoff.handoff_id)
        await coordinator._finish(handoff, result)
        reporter.assert_awaited_once_with(handoff, result)


if __name__ == "__main__":
    unittest.main()
