from __future__ import annotations

import base64
import importlib.util
import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEST_ROOT = Path(__file__).parents[1]
DIAGNOSTICS_PATH = TEST_ROOT / "src" / "llm_models" / "cache_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("llm_cache_diagnostics_isolated", DIAGNOSTICS_PATH)
assert SPEC is not None and SPEC.loader is not None
cache_diagnostics = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cache_diagnostics
SPEC.loader.exec_module(cache_diagnostics)


def _model(*, name="test-model", identifier="model-id", provider="test-provider"):
    return SimpleNamespace(name=name, model_identifier=identifier, api_provider=provider)


def _message(text: str):
    return SimpleNamespace(role="user", content=text, tool_calls=None)


class CacheDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        cache_diagnostics.reset()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="llm-cache-diag-")
        self.output_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        cache_diagnostics.flush()
        cache_diagnostics.reset()
        self.temp_dir.cleanup()

    def test_history_pool_matches_older_prefix_after_intervening_prompt(self) -> None:
        model = _model()
        first = cache_diagnostics.record_request(
            messages=[_message("stable system prefix A")],
            model_info=model,
            request_type="replyer",
            session_id="chat-a",
            enabled=True,
            output_dir=self.output_dir,
        )
        intervening = cache_diagnostics.record_request(
            messages=[_message("different prompt B")],
            model_info=model,
            request_type="replyer",
            session_id="chat-a",
            enabled=True,
            output_dir=self.output_dir,
        )
        returned = cache_diagnostics.record_request(
            messages=[_message("stable system prefix A")],
            model_info=model,
            request_type="replyer",
            session_id="chat-a",
            enabled=True,
            output_dir=self.output_dir,
        )
        self.assertIsNone(first["estimated_prefix_chars"])
        self.assertLess(intervening["estimated_prefix_chars"], len("stable system prefix A"))
        self.assertEqual(returned["estimated_prefix_chars"], len("stable system prefix A"))
        self.assertIsNone(returned["first_changed_field"])
        self.assertEqual(returned["comparison_reference_age"], 2)

    def test_sessions_and_underlying_model_identifiers_have_separate_histories(self) -> None:
        first = cache_diagnostics.record_request(
            messages=[_message("same prompt")],
            model_info=_model(name="alias", identifier="model-a"),
            request_type="planner",
            session_id="chat-one",
            enabled=True,
            output_dir=self.output_dir,
        )
        other_session = cache_diagnostics.record_request(
            messages=[_message("same prompt")],
            model_info=_model(name="alias", identifier="model-a"),
            request_type="planner",
            session_id="chat-two",
            enabled=True,
            output_dir=self.output_dir,
        )
        other_identifier = cache_diagnostics.record_request(
            messages=[_message("same prompt")],
            model_info=_model(name="alias", identifier="model-b"),
            request_type="planner",
            session_id="chat-one",
            enabled=True,
            output_dir=self.output_dir,
        )
        callsite = cache_diagnostics.record_request(
            messages=[_message("same prompt")],
            model_info=_model(name="alias", identifier="model-a"),
            request_type="planner",
            session_id=None,
            enabled=True,
            output_dir=self.output_dir,
        )
        self.assertIsNone(first["estimated_prefix_chars"])
        self.assertIsNone(other_session["estimated_prefix_chars"])
        self.assertIsNone(other_identifier["estimated_prefix_chars"])
        self.assertEqual(callsite["session_scope"], "callsite")
        self.assertEqual(callsite["diagnostic_scope_kind"], "callsite")
        self.assertNotIn("chat-one", json.dumps(first))

    def test_tool_and_response_format_changes_are_fingerprinted(self) -> None:
        model = _model()
        shared = {"messages": [_message("same prompt")], "model_info": model, "request_type": "replyer",
                  "session_id": "chat", "enabled": True, "output_dir": self.output_dir}
        cache_diagnostics.record_request(
            **shared,
            tools=[SimpleNamespace(name="lookup", description="find", params=[], input_schema=None)],
            response_format=None,
        )
        changed_tool = cache_diagnostics.record_request(
            **shared,
            tools=[SimpleNamespace(name="search", description="find", params=[], input_schema=None)],
            response_format=None,
        )
        self.assertEqual(changed_tool["first_changed_field"], "request.tools")
        self.assertEqual(changed_tool["estimated_prefix_chars"], len("same prompt"))

        cache_diagnostics.reset()
        cache_diagnostics.record_request(
            **shared,
            tools=None,
            response_format=SimpleNamespace(format_type="json", schema={"properties": {"a": {"type": "string"}}}),
        )
        changed_format = cache_diagnostics.record_request(
            **shared,
            tools=None,
            response_format=SimpleNamespace(format_type="json", schema={"properties": {"b": {"type": "string"}}}),
        )
        self.assertEqual(changed_format["first_changed_field"], "request.response_format")

    def test_schema_beyond_signature_depth_is_reported_as_partial(self) -> None:
        def deep_schema(leaf: str):
            value = leaf
            for _ in range(12):
                value = {"nested": value}
            return SimpleNamespace(format_type="json", schema=value)

        self.assertTrue(cache_diagnostics._bounded_value(deep_schema("schema-a"), 4096)[1])

        options = {
            "messages": [_message("same prompt")],
            "model_info": _model(),
            "request_type": "replyer",
            "session_id": "chat",
            "tools": None,
            "enabled": True,
            "output_dir": self.output_dir,
        }
        cache_diagnostics.record_request(**options, response_format=deep_schema("schema-a"))
        second = cache_diagnostics.record_request(**options, response_format=deep_schema("schema-b"))
        self.assertTrue(second["prompt_snapshot_truncated"])
        self.assertFalse(second["estimate_complete"])

    def test_binary_data_and_private_values_are_not_written(self) -> None:
        payload = b"private-binary-payload-that-must-not-appear"
        encoded = base64.b64encode(payload).decode("ascii")
        record = cache_diagnostics.record_request(
            messages=[SimpleNamespace(role="user", content=[("png", encoded)], tool_calls=None)],
            model_info=_model(name="<img src=x onerror=alert(1)>", identifier="provider-model"),
            request_type="replyer",
            session_id="private-chat-id",
            usage=SimpleNamespace(
                prompt_tokens=20,
                prompt_cache_hit_tokens=5,
                prompt_cache_miss_tokens=15,
                prompt_cache_reported=True,
            ),
            enabled=True,
            output_dir=self.output_dir,
        )
        self.assertTrue(record["actual_provider_counts_reported"])
        self.assertEqual(record["actual_prompt_cache_hit_tokens"], 5)
        self.assertTrue(cache_diagnostics.flush())

        jsonl = (self.output_dir / "llm_cache_stats.jsonl").read_text(encoding="utf-8")
        html = (self.output_dir / "llm_cache_stats.html").read_text(encoding="utf-8")
        self.assertNotIn(payload.decode("ascii"), jsonl)
        self.assertNotIn(encoded, jsonl)
        self.assertNotIn("private-chat-id", jsonl)
        self.assertNotIn(payload.decode("ascii"), html)
        self.assertNotIn(encoded, html)
        self.assertNotIn("private-chat-id", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn(record["prompt_fingerprint"], jsonl)

    def test_disk_io_is_queued_and_html_window_stays_current_at_file_cap(self) -> None:
        original_limit = cache_diagnostics.MAX_OUTPUT_FILE_BYTES
        cache_diagnostics.MAX_OUTPUT_FILE_BYTES = 1200
        writer_names = []
        original_write = cache_diagnostics._write_record_to_files

        def observe_writer(directory, record):
            writer_names.append(threading.current_thread().name)
            return original_write(directory, record)

        try:
            with patch.object(cache_diagnostics, "_write_record_to_files", side_effect=observe_writer):
                for index in range(20):
                    cache_diagnostics.record_request(
                        messages=[_message(f"prompt {index}")],
                        model_info=_model(name=f"model-{index}"),
                        request_type="replyer",
                        session_id="bounded-session",
                        enabled=True,
                        output_dir=self.output_dir,
                    )
                self.assertTrue(cache_diagnostics.flush())
            self.assertTrue(writer_names)
            self.assertTrue(all(name == "llm-cache-stats-writer" for name in writer_names))
            current_jsonl = self.output_dir / "llm_cache_stats.jsonl"
            backup_jsonl = self.output_dir / "llm_cache_stats.jsonl.1"
            html = self.output_dir / "llm_cache_stats.html"
            self.assertTrue(current_jsonl.exists())
            self.assertTrue(backup_jsonl.exists())
            self.assertTrue(html.exists())
            self.assertLessEqual(current_jsonl.stat().st_size, cache_diagnostics.MAX_OUTPUT_FILE_BYTES)
            self.assertLessEqual(backup_jsonl.stat().st_size, cache_diagnostics.MAX_OUTPUT_FILE_BYTES)
            self.assertLessEqual(html.stat().st_size, cache_diagnostics.MAX_OUTPUT_FILE_BYTES)
            self.assertIn("model-19", html.read_text(encoding="utf-8"))
            snapshot = cache_diagnostics.snapshot()
            self.assertLessEqual(snapshot["keys"], cache_diagnostics.MAX_KEYS)
            self.assertLessEqual(snapshot["estimated_retained_bytes"], cache_diagnostics.MAX_GLOBAL_BYTES)
        finally:
            cache_diagnostics.MAX_OUTPUT_FILE_BYTES = original_limit

    def test_history_entry_key_and_unicode_byte_budgets_evict_old_state(self) -> None:
        with patch.multiple(
            cache_diagnostics,
            MAX_KEYS=2,
            MAX_ENTRIES_PER_KEY=2,
            MAX_GLOBAL_BYTES=1_000_000,
        ):
            def record(name: str, text: str):
                return cache_diagnostics.record_request(
                    messages=[_message(text)],
                    model_info=_model(name=name, identifier=f"{name}-identifier"),
                    request_type="replyer",
                    session_id="bounded-session",
                    enabled=True,
                    output_dir=self.output_dir,
                )

            record("model-a", "prompt a0")
            record("model-a", "prompt a1")
            third_a = record("model-a", "prompt a2")
            self.assertEqual(third_a["history_depth"], 2)
            self.assertLessEqual(cache_diagnostics.snapshot()["entries"], 2)
            record("model-b", "prompt b0")
            record("model-c", "prompt c0")
            returned_a = record("model-a", "prompt a2")
            self.assertIsNone(returned_a["estimated_prefix_chars"])
            self.assertEqual(returned_a["history_depth"], 1)
            self.assertLessEqual(cache_diagnostics.snapshot()["keys"], 2)

        cache_diagnostics.reset()
        with patch.object(cache_diagnostics, "MAX_GLOBAL_BYTES", 1024):
            unicode_prompt = "汉🙂" * 4000
            first = cache_diagnostics.record_request(
                messages=[_message(unicode_prompt)],
                model_info=_model(name="unicode"),
                request_type="replyer",
                session_id="unicode-session",
                enabled=True,
                output_dir=self.output_dir,
            )
            snapshot = cache_diagnostics.snapshot()
            self.assertEqual(first["history_depth"], 0)
            self.assertEqual(snapshot["entries"], 0)
            self.assertLessEqual(snapshot["estimated_retained_bytes"], 1024)
            second = cache_diagnostics.record_request(
                messages=[_message(unicode_prompt)],
                model_info=_model(name="unicode"),
                request_type="replyer",
                session_id="unicode-session",
                enabled=True,
                output_dir=self.output_dir,
            )
            self.assertIsNone(second["estimated_prefix_chars"])

    def test_full_writer_queue_drops_report_without_blocking_recording(self) -> None:
        with patch.object(cache_diagnostics, "_ensure_writer"), patch.object(
            cache_diagnostics._write_queue, "put_nowait", side_effect=queue.Full
        ):
            record = cache_diagnostics.record_request(
                messages=[_message("inference still succeeds")],
                model_info=_model(),
                request_type="replyer",
                session_id="queue-session",
                enabled=True,
                output_dir=self.output_dir,
            )
        self.assertIsNotNone(record)
        snapshot = cache_diagnostics.snapshot()
        self.assertEqual(snapshot["dropped_records"], 1)
        self.assertEqual(snapshot["entries"], 1)


if __name__ == "__main__":
    unittest.main()
