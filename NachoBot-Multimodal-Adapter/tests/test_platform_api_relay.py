import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from fastapi import FastAPI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


def _write_test_config() -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".toml",
        encoding="utf-8",
        delete=False,
    ) as config_file:
        config_file.write(
            "[server]\nhost = '127.0.0.1'\nport = 9880\n"
            "[enabled_tts]\nenabled = []\n"
            "[tts_base_config]\nstream_mode = false\npost_process = false\n"
        )
        return Path(config_file.name)


class HttpFacadeBoundaryTests(unittest.TestCase):
    def test_main_has_no_platform_transport_boundary(self) -> None:
        source = Path(main.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "MessageServer",
            "Router",
            "RouteConfig",
            "TargetConfig",
            "MessageBase",
            "FormatInfo",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("FastAPI", source)

    def test_facade_registers_only_uniform_tts_routes(self) -> None:
        pipeline = main.TTSPipeline.__new__(main.TTSPipeline)
        # Exercise the constructor with a tiny injected client, so no model
        # weights or child process are needed for this route-boundary test.
        class FakeModel:
            _initialized = True
            emotion_ready = True

        config_path = _write_test_config()
        try:
            pipeline = main.TTSPipeline(config_path, backend="Vox", model=FakeModel())
        finally:
            config_path.unlink(missing_ok=True)

        routes = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in pipeline.app.routes
            if hasattr(route, "methods")
        }
        self.assertIn(("/api/tts", ("POST",)), routes)
        self.assertIn(("/api/health", ("GET",)), routes)
        self.assertNotIn(("/api/emotion_preset", ("GET",)), routes)
        self.assertFalse(any(getattr(route, "path", "").startswith("/ws") for route in pipeline.app.routes))

    def test_health_route_publishes_core_model_loaded_boundary(self) -> None:
        class FakeModel:
            _initialized = True
            emotion_ready = True

        config_path = _write_test_config()
        try:
            pipeline = main.TTSPipeline(config_path, backend="Vox", model=FakeModel())
        finally:
            config_path.unlink(missing_ok=True)
        health = next(route.endpoint for route in pipeline.app.routes if route.path == "/api/health")

        async def scenario() -> None:
            ready = await health()
            self.assertIs(ready["model_loaded"], True)
            self.assertIs(ready["ready"], True)

            pipeline._engine_ready = False
            not_loaded = await health()
            self.assertIs(not_loaded["model_loaded"], False)
            self.assertIs(not_loaded["ready"], False)

            pipeline._engine_ready = True
            pipeline.set_backend_alive(False)
            child_dead = await health()
            self.assertIs(child_dead["model_loaded"], True)
            self.assertIs(child_dead["ready"], False)
            self.assertIs(child_dead["backend_alive"], False)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
