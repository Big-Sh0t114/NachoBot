from __future__ import annotations

import atexit
from importlib.machinery import ModuleSpec
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEST_ROOT = Path(__file__).parents[1]
SRC_ROOT = TEST_ROOT / "src"
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))


def _stub_package(name: str, path: Path) -> types.ModuleType:
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    package.__package__ = name
    spec = ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    package.__spec__ = spec
    sys.modules[name] = package
    return package


def _install_safe_import_boundary() -> tuple[object, tempfile.TemporaryDirectory]:
    """Bind every host-sensitive import to stubs or a temporary SQLite file first."""
    import peewee

    temp_root = tempfile.TemporaryDirectory(prefix="llm-cache-usage-")
    db_path = Path(temp_root.name) / "legacy.sqlite3"
    sqlite = sqlite3.connect(db_path)
    sqlite.executescript(
        """
        CREATE TABLE llm_usage (
            id INTEGER NOT NULL PRIMARY KEY,
            model_name TEXT NOT NULL,
            model_assign_name TEXT,
            model_api_provider TEXT,
            user_id TEXT NOT NULL,
            request_type TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            cost DOUBLE NOT NULL,
            time_cost DOUBLE,
            status TEXT NOT NULL,
            timestamp DATETIME NOT NULL
        );
        INSERT INTO llm_usage (
            model_name, model_assign_name, model_api_provider, user_id, request_type,
            endpoint, prompt_tokens, completion_tokens, total_tokens, cost, time_cost,
            status, timestamp
        ) VALUES (
            'legacy-provider-id', 'legacy-model', 'legacy-provider', 'legacy-user',
            'replyer', '/chat/completions', 90, 10, 100, 0.01, 0.5, 'success',
            '2026-10-28 10:00:00'
        );
        CREATE TABLE statistics_llm_usage_hourly (
            id INTEGER NOT NULL PRIMARY KEY,
            bucket_time TEXT NOT NULL,
            model_name TEXT NOT NULL,
            request_type TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            total_cost DOUBLE NOT NULL DEFAULT 0.0,
            time_cost_sum DOUBLE NOT NULL DEFAULT 0.0,
            time_cost_sq_sum DOUBLE NOT NULL DEFAULT 0.0
        );
        CREATE UNIQUE INDEX legacy_llm_usage_hourly_key
            ON statistics_llm_usage_hourly(bucket_time, model_name, request_type);
        """
    )
    sqlite.commit()
    sqlite.close()

    # Install the src root stub before any source import. Its child packages
    # point at source files, but their host initializers will never execute.
    _stub_package("src", SRC_ROOT)
    _stub_package("src.common", SRC_ROOT / "common")
    _stub_package("src.common.database", SRC_ROOT / "common" / "database")
    _stub_package("src.config", SRC_ROOT / "config")
    _stub_package("src.llm_models", SRC_ROOT / "llm_models")
    _stub_package("src.llm_models.model_client", SRC_ROOT / "llm_models" / "model_client")
    _stub_package("src.llm_models.payload_content", SRC_ROOT / "llm_models" / "payload_content")
    _stub_package("src.services", SRC_ROOT / "services")
    _stub_package("src.manager", SRC_ROOT / "manager")

    logger_module = types.ModuleType("src.common.logger")
    logger_cache: dict[str, logging.Logger] = {}

    def get_logger(name: str) -> logging.Logger:
        if name not in logger_cache:
            logger = logging.getLogger(f"llm-cache-test.{name}")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
            logger_cache[name] = logger
        return logger_cache[name]

    logger_module.get_logger = get_logger
    sys.modules[logger_module.__name__] = logger_module

    config_module = types.ModuleType("src.config.config")
    config_module.global_config = SimpleNamespace(
        debug=SimpleNamespace(enable_llm_cache_stats=True),
        focus=SimpleNamespace(mode="off"),
    )
    config_module.model_config = SimpleNamespace(model_task_config=SimpleNamespace(replyer=None))
    sys.modules[config_module.__name__] = config_module

    database_module = types.ModuleType("src.common.database.database")
    database_module.db = peewee.SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    database_module.db.connect()
    sys.modules[database_module.__name__] = database_module

    async_task_module = types.ModuleType("src.manager.async_task_manager")

    class AsyncTask:
        def __init__(self, *args, **kwargs):
            del args, kwargs

    async_task_module.AsyncTask = AsyncTask
    sys.modules[async_task_module.__name__] = async_task_module
    return database_module.db, temp_root


TEST_DB, _TEMP_ROOT = _install_safe_import_boundary()


def _close_test_resources() -> None:
    try:
        TEST_DB.close()
    except Exception:
        pass
    _TEMP_ROOT.cleanup()


atexit.register(_close_test_resources)

# All source imports below are guarded by the temporary package/DB boundary.
from src.config.api_ada_configs import ModelInfo, TaskConfig  # noqa: E402
from src.llm_models.exceptions import RespNotOkException  # noqa: E402
from src.llm_models.model_client.base_client import (  # noqa: E402
    APIResponse,
    UsageRecord,
    normalize_prompt_cache_usage,
)
from src.llm_models.model_client.openai_client import OpenaiClient, _default_normal_response_parser  # noqa: E402
from src.llm_models.model_client.gemini_client import (  # noqa: E402
    _default_normal_response_parser as _gemini_normal_parser,
    _default_stream_response_handler as _gemini_stream_handler,
)
from src.llm_models.payload_content.message import Message, RoleType  # noqa: E402
from src.llm_models.utils import llm_usage_recorder  # noqa: E402
from src.common.database.database_model import AggregationCursor, LLMUsage, LLMUsageHourly  # noqa: E402
from src.services.db_aggregation_service import aggregate_llm_usage  # noqa: E402
from src.llm_models import cache_diagnostics  # noqa: E402
from src.llm_models import utils_model  # noqa: E402
from src.llm_models.utils_model import LLMRequest, RequestType  # noqa: E402


def _openai_response(usage: object) -> SimpleNamespace:
    message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=[])
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


class _AsyncEvents:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


class _OpenAICompletions:
    def __init__(self, response=None, stream=None):
        self.response = response
        self.stream = stream
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.stream if kwargs.get("stream") else self.response


class CacheUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        LLMUsage.delete().where(LLMUsage.id > 1).execute()
        LLMUsageHourly.delete().execute()
        AggregationCursor.delete().execute()

    def test_normalizer_preserves_absent_zero_and_clamps_provider_aliases(self) -> None:
        self.assertEqual(normalize_prompt_cache_usage(100, (None, None), None), (0, 0, False))
        self.assertEqual(normalize_prompt_cache_usage(100, (0,), None), (0, 100, True))
        self.assertEqual(normalize_prompt_cache_usage(100, (140,), 10), (100, 0, True))
        self.assertEqual(normalize_prompt_cache_usage(100, (-1, 25), 10), (25, 75, True))
        self.assertEqual(normalize_prompt_cache_usage(100, (None,), 120), (0, 100, True))

    def test_openai_nonstream_fields_and_explicit_zero(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=7,
            total_tokens=107,
            prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            prompt_cache_hit_tokens=25,
            prompt_cache_miss_tokens=11,
        )
        parsed, totals = _default_normal_response_parser(_openai_response(usage))
        self.assertEqual(totals, (100, 7, 107))
        self.assertEqual(parsed.cache_usage, (25, 75, True))

        zero_usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=1,
            total_tokens=101,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        zero_parsed, _ = _default_normal_response_parser(_openai_response(zero_usage))
        self.assertEqual(zero_parsed.cache_usage, (0, 100, True))

        absent_usage = SimpleNamespace(prompt_tokens=100, completion_tokens=1, total_tokens=101)
        absent_parsed, _ = _default_normal_response_parser(_openai_response(absent_usage))
        self.assertIsNone(absent_parsed.cache_usage)

    async def test_openai_get_response_sets_usage_for_stream_and_nonstream(self) -> None:
        nonstream_client = object.__new__(OpenaiClient)
        nonstream_client.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=_OpenAICompletions(
                    response=_openai_response(
                        SimpleNamespace(
                            prompt_tokens=100,
                            completion_tokens=3,
                            total_tokens=103,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                        )
                    )
                )
            )
        )
        model = ModelInfo("openai-id", "openai-model", "provider", price_in=2, cache_price_in=0)
        nonstream = await nonstream_client.get_response(model, [Message(RoleType.User, "hello")])
        self.assertIsNotNone(nonstream.usage)
        self.assertTrue(nonstream.usage.prompt_cache_reported)
        self.assertEqual(nonstream.usage.prompt_cache_hit_tokens, 40)
        self.assertEqual(nonstream.usage.prompt_cache_miss_tokens, 60)

        delta = SimpleNamespace(content="streamed", reasoning_content=None, tool_calls=None)
        usage_frame = SimpleNamespace(
            prompt_tokens=80,
            completion_tokens=2,
            total_tokens=82,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=5,
        )
        stream = _AsyncEvents(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None),
                SimpleNamespace(choices=[], usage=usage_frame),
            ]
        )
        stream_client = object.__new__(OpenaiClient)
        stream_client.client = SimpleNamespace(
            chat=SimpleNamespace(completions=_OpenAICompletions(stream=stream))
        )
        stream_model = ModelInfo(
            "openai-id", "openai-model", "provider", force_stream_mode=True, cache_price_in=0
        )
        streamed = await stream_client.get_response(stream_model, [Message(RoleType.User, "hello")])
        self.assertEqual(streamed.content, "streamed")
        self.assertIsNotNone(streamed.usage)
        self.assertTrue(streamed.usage.prompt_cache_reported)
        self.assertEqual(streamed.usage.prompt_cache_hit_tokens, 0)
        self.assertEqual(streamed.usage.prompt_cache_miss_tokens, 80)

    async def test_gemini_nonstream_and_stream_cache_metadata(self) -> None:
        response = SimpleNamespace(
            candidates=[],
            text="ok",
            function_calls=[],
            usage_metadata=SimpleNamespace(
                prompt_token_count=200,
                candidates_token_count=4,
                thoughts_token_count=2,
                total_token_count=206,
                cached_content_token_count=45,
            ),
        )
        parsed, totals = _gemini_normal_parser(response)
        self.assertEqual(totals, (200, 6, 206))
        self.assertEqual(parsed.cache_usage, (45, 155, True))

        no_cache = SimpleNamespace(
            candidates=[],
            text="ok",
            function_calls=[],
            usage_metadata=SimpleNamespace(
                prompt_token_count=12,
                candidates_token_count=1,
                thoughts_token_count=0,
                total_token_count=13,
            ),
        )
        absent, _ = _gemini_normal_parser(no_cache)
        self.assertIsNone(absent.cache_usage)

        zero_chunk = SimpleNamespace(
            candidates=[SimpleNamespace()],
            text="hello",
            function_calls=[],
            usage_metadata=SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=1,
                thoughts_token_count=0,
                total_token_count=11,
                cached_content_token_count=0,
            ),
        )
        stream_response, stream_totals = await _gemini_stream_handler(_AsyncEvents([zero_chunk]), None)
        self.assertEqual(stream_response.content, "hello")
        self.assertEqual(stream_totals, (10, 1, 11))
        self.assertEqual(stream_response.cache_usage, (0, 10, True))

    def test_cache_price_validation_and_positional_compatibility(self) -> None:
        legacy = ModelInfo("id", "name", "provider", 2.0, 3.0, True, {"k": "v"})
        self.assertEqual(legacy.extra_params, {"k": "v"})
        self.assertIsNone(legacy.cache_price_in)
        self.assertEqual(ModelInfo("id", "name", "provider", cache_price_in=0).cache_price_in, 0)
        for value in (-0.01, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ModelInfo("id", "name", "provider", cache_price_in=value)

    def test_usage_recorder_applies_hit_price_only_when_reported(self) -> None:
        usage = UsageRecord("model", "provider", 1000, 0, 1000, 400, 600, True)
        free_hits = ModelInfo("id", "model", "provider", price_in=2.0, cache_price_in=0.0)
        llm_usage_recorder.record_usage_to_database(
            free_hits, usage, "u", "replyer", "/chat/completions"
        )
        row = LLMUsage.select().order_by(LLMUsage.id.desc()).first()
        self.assertAlmostEqual(row.cost, 0.0012)
        self.assertEqual(row.prompt_cache_hit_tokens, 400)
        self.assertEqual(row.prompt_cache_miss_tokens, 600)
        self.assertTrue(row.prompt_cache_reported)

        inherited_price = ModelInfo("id", "model", "provider", price_in=2.0, cache_price_in=None)
        llm_usage_recorder.record_usage_to_database(
            inherited_price, usage, "u", "replyer", "/chat/completions"
        )
        row = LLMUsage.select().order_by(LLMUsage.id.desc()).first()
        self.assertAlmostEqual(row.cost, 0.002)
        self.assertEqual(row.prompt_cache_hit_tokens, 400)

        partial_miss = UsageRecord("model", "provider", 1000, 0, 1000, 400, 0, True)
        llm_usage_recorder.record_usage_to_database(
            free_hits, partial_miss, "u", "replyer", "/chat/completions"
        )
        row = LLMUsage.select().order_by(LLMUsage.id.desc()).first()
        self.assertAlmostEqual(row.cost, 0.0012)
        self.assertEqual(row.prompt_cache_miss_tokens, 600)

        unreported = UsageRecord("model", "provider", 1000, 0, 1000, 400, 600, False)
        llm_usage_recorder.record_usage_to_database(
            free_hits, unreported, "u", "replyer", "/chat/completions"
        )
        row = LLMUsage.select().order_by(LLMUsage.id.desc()).first()
        self.assertAlmostEqual(row.cost, 0.002)
        self.assertEqual(row.prompt_cache_hit_tokens, 0)
        self.assertFalse(row.prompt_cache_reported)

    def test_legacy_schema_adds_defaults_and_hourly_aggregation_retains_counts(self) -> None:
        legacy = LLMUsage.get_by_id(1)
        self.assertEqual(legacy.prompt_cache_hit_tokens, 0)
        self.assertEqual(legacy.prompt_cache_miss_tokens, 0)
        self.assertFalse(legacy.prompt_cache_reported)

        columns = {row[1]: row for row in TEST_DB.execute_sql("PRAGMA table_info('llm_usage')").fetchall()}
        self.assertIn("prompt_cache_hit_tokens", columns)
        self.assertIn("prompt_cache_miss_tokens", columns)
        self.assertIn("prompt_cache_reported", columns)
        image_columns = {row[1] for row in TEST_DB.execute_sql("PRAGMA table_info('images')").fetchall()}
        self.assertIn("last_used_time", image_columns)
        self.assertIn("no_file_flag", image_columns)

        LLMUsage.create(
            model_name="cache-provider-id",
            model_assign_name="cache-model",
            model_api_provider="provider",
            user_id="cache-user",
            request_type="replyer",
            endpoint="/chat/completions",
            prompt_tokens=100,
            completion_tokens=5,
            total_tokens=105,
            prompt_cache_hit_tokens=30,
            prompt_cache_miss_tokens=70,
            prompt_cache_reported=True,
            cost=0.01,
            time_cost=0.5,
            status="success",
            timestamp=datetime(2026, 10, 28, 10, 5),
        )
        self.assertEqual(aggregate_llm_usage(batch_size=10), 2)
        cached_hour = LLMUsageHourly.get(
            (LLMUsageHourly.model_name == "cache-model") & (LLMUsageHourly.request_type == "replyer")
        )
        self.assertEqual(cached_hour.prompt_cache_hit_tokens, 30)
        self.assertEqual(cached_hour.prompt_cache_miss_tokens, 70)
        self.assertEqual(cached_hour.prompt_cache_reported_calls, 1)
        legacy_hour = LLMUsageHourly.get(
            (LLMUsageHourly.model_name == "legacy-model") & (LLMUsageHourly.request_type == "replyer")
        )
        self.assertEqual(legacy_hour.prompt_cache_reported_calls, 0)

    async def test_request_glue_records_only_final_model_and_compressed_413_prompt(self) -> None:
        info_a = ModelInfo("model-a-id", "model-a", "provider-a")
        info_b = ModelInfo("model-b-id", "model-b", "provider-b")
        provider_a = SimpleNamespace(max_retry=2, retry_interval=0)
        provider_b = SimpleNamespace(max_retry=2, retry_interval=0)

        class FakeClient:
            def __init__(self, name: str):
                self.name = name
                self.seen_messages = []
                self.calls = 0

            async def get_response(self, **kwargs):
                self.calls += 1
                self.seen_messages.append(kwargs["message_list"])
                if self.name == "model-a":
                    raise RespNotOkException(404, "model unavailable")
                if self.calls == 1:
                    raise RespNotOkException(413, "payload too large")
                return APIResponse(
                    content="done",
                    usage=UsageRecord("model-b", "provider-b", 50, 5, 55, 20, 30, True),
                )

        clients = {"model-a": FakeClient("model-a"), "model-b": FakeClient("model-b")}
        task = TaskConfig(model_list=["model-a", "model-b"], max_tokens=64, temperature=0.1)
        request_config = SimpleNamespace(
            get_model_info=lambda name: {"model-a": info_a, "model-b": info_b}[name],
            get_provider=lambda name: {"provider-a": provider_a, "provider-b": provider_b}[name],
        )
        request = LLMRequest(task, request_type="replyer", config=request_config, session_id="trusted-chat")

        def select_model(*, exclude_models=None, eligible_models=None):
            del eligible_models
            name = "model-a" if not exclude_models else "model-b"
            return (
                {"model-a": info_a, "model-b": info_b}[name],
                {"model-a": provider_a, "model-b": provider_b}[name],
                clients[name],
            )

        request._select_model = select_model
        original_message = Message(RoleType.User, "original prompt")
        compressed_message = Message(RoleType.User, "compressed prompt")
        captured = []
        real_record = cache_diagnostics.record_request

        def record(**kwargs):
            captured.append(kwargs)
            return real_record(**kwargs, output_dir=Path(_TEMP_ROOT.name) / "request-glue")

        cache_diagnostics.reset()
        with patch.object(utils_model, "compress_messages", return_value=[compressed_message]), patch.object(
            cache_diagnostics, "record_request", side_effect=record
        ):
            response, chosen_model = await request._execute_request(
                RequestType.RESPONSE,
                message_factory=lambda _client: [original_message],
                tool_options=[SimpleNamespace(name="tool", description="desc", params=[], input_schema=None)],
            )
        self.assertEqual(chosen_model.name, "model-b")
        self.assertEqual(response.content, "done")
        self.assertEqual(clients["model-a"].calls, 1)
        self.assertEqual(clients["model-b"].calls, 2)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["messages"], [compressed_message])
        self.assertEqual(captured[0]["session_id"], "trusted-chat")
        self.assertEqual(captured[0]["tools"][0].name, "tool")
        self.assertTrue(cache_diagnostics.flush())
        self.assertTrue((Path(_TEMP_ROOT.name) / "request-glue" / "llm_cache_stats.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
