import ssl
import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ncnk_message import MessageServer
from ncnk_message.router import (
    RouteConfig,
    Router,
    TargetConfig,
    get_core_token_from_env,
)
from ncnk_message.ws_connection import (
    _browser_origin_allowed,
    _create_client_ssl_context,
)
from src.common.server import (
    Server,
    is_loopback_host,
    supports_message_server_token_auth,
)


@contextmanager
def isolated_message_api_module():
    """Load common.message.api without importing the full config/runtime."""
    module_name = "_isolated_common_message_api"
    names = {
        module_name,
        "src.common.server",
        "src.common.logger",
        "src.config.config",
    }
    previous = {name: sys.modules.get(name) for name in names}
    server_stub = types.ModuleType("src.common.server")
    server_stub.get_global_server = lambda: None
    server_stub.is_loopback_host = lambda _host: True
    server_stub.supports_message_server_token_auth = lambda _server: True
    logger_stub = types.ModuleType("src.common.logger")
    logger_stub.get_logger = lambda _name: object()
    config_stub = types.ModuleType("src.config.config")
    config_stub.global_config = types.SimpleNamespace()
    sys.modules["src.common.server"] = server_stub
    sys.modules["src.common.logger"] = logger_stub
    sys.modules["src.config.config"] = config_stub
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[1] / "src/common/message/api.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load common.message.api")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class CoreAuthenticationTests(unittest.TestCase):
    def _build_client(self, tokens: list[str]) -> tuple[Server, TestClient]:
        server = Server(host="127.0.0.1", port=8000)
        server.configure_auth(tokens)
        router = APIRouter()

        @router.get("/value")
        async def value() -> dict[str, bool]:
            return {"ok": True}

        server.register_router(router, prefix="/api/test")
        return server, TestClient(server.get_app())

    def test_health_is_public_but_api_requires_configured_token(self) -> None:
        _server, client = self._build_client(["transport-secret"])

        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/api/test/value").status_code, 401)
        self.assertEqual(
            client.get(
                "/api/test/value",
                headers={"Authorization": "Bearer transport-secret"},
            ).status_code,
            200,
        )
        # 兼容历史 ncnk_message 客户端使用的裸令牌格式。
        self.assertEqual(
            client.get(
                "/api/test/value",
                headers={"Authorization": "transport-secret"},
            ).status_code,
            200,
        )

    def test_non_loopback_listener_without_token_fails_closed(self) -> None:
        server = Server(host="0.0.0.0", port=8000)
        server.configure_auth([])
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "拒绝"),
        ):
            server.validate_security()

        server.configure_auth(["configured"])
        server.validate_security()

    def test_environment_core_token_protects_http_and_non_loopback_bind(self) -> None:
        with mock.patch.dict(
            "os.environ", {"NACHOBOT_CORE_TOKEN": "environment-secret"}, clear=True
        ):
            server, client = self._build_client([])
            server.set_address("0.0.0.0", 8000)
            server.validate_security()
            self.assertEqual(client.get("/api/test/value").status_code, 401)
            self.assertEqual(
                client.get(
                    "/api/test/value",
                    headers={"Authorization": "Bearer environment-secret"},
                ).status_code,
                200,
            )

    def test_token_capability_checks_the_imported_class(self) -> None:
        class OldMessageServer:
            def __init__(self, host: str = "127.0.0.1") -> None:
                self.host = host

        class TokenMessageServer:
            def __init__(self, enable_token: bool = False) -> None:
                self.enable_token = enable_token

            def add_valid_token(self, token: str) -> None:
                del token

        self.assertFalse(supports_message_server_token_auth(OldMessageServer))
        self.assertTrue(supports_message_server_token_auth(TokenMessageServer))

    def test_explicit_trusted_container_network_allows_internal_bind(self) -> None:
        server = Server(host="0.0.0.0", port=8000)
        with (
            mock.patch.dict(
                "os.environ", {"NACHOBOT_TRUSTED_CONTAINER_NETWORK": "1"}
            ),
            self.assertWarnsRegex(RuntimeWarning, "容器网络"),
        ):
            server.validate_security()

    def test_loopback_detection_does_not_trust_arbitrary_hostnames(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("core.internal"))


class TransportTlsTests(unittest.TestCase):
    def test_shared_core_token_environment_is_trimmed(self) -> None:
        with mock.patch.dict(
            "os.environ", {"NACHOBOT_CORE_TOKEN": "  shared-secret  "}, clear=True
        ):
            self.assertEqual(get_core_token_from_env(), "shared-secret")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(get_core_token_from_env())

    def test_wss_context_verifies_system_ca_and_hostname_by_default(self) -> None:
        context = _create_client_ssl_context()

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_insecure_mode_must_be_explicit(self) -> None:
        context = _create_client_ssl_context(insecure_skip_verify=True)

        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_legacy_router_config_is_migrated_without_weakening_defaults(self) -> None:
        default_config = TargetConfig.from_dict({"url": "wss://example.test/ws"})
        legacy_ca_config = TargetConfig.from_dict(
            {"url": "wss://example.test/ws", "ssl_verify": "custom-ca.pem"}
        )
        explicit_insecure_config = TargetConfig.from_dict(
            {"url": "wss://example.test/ws", "ssl_verify": False}
        )

        self.assertFalse(default_config.insecure_skip_verify)
        self.assertEqual(legacy_ca_config.ca_file, "custom-ca.pem")
        self.assertTrue(explicit_insecure_config.insecure_skip_verify)

    def test_insecure_skip_verify_rejects_string_booleans(self) -> None:
        for raw_value in ("false", "0", 0, 1):
            with self.subTest(raw_value=raw_value), self.assertRaisesRegex(
                ValueError, "必须是布尔值"
            ):
                TargetConfig.from_dict(
                    {
                        "url": "wss://example.test/ws",
                        "insecure_skip_verify": raw_value,
                    }
                )

    def test_custom_wss_requires_ws_mode_complete_readable_certificates(self) -> None:
        with isolated_message_api_module() as module, tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            cert_file = temp_path / "cert.pem"
            key_file = temp_path / "key.pem"
            cert_file.write_text("certificate", encoding="utf-8")
            key_file.write_text("key", encoding="utf-8")
            params = {"ssl_certfile", "ssl_keyfile"}

            module._validate_custom_message_server_config(
                types.SimpleNamespace(
                    mode="ws",
                    use_wss=True,
                    cert_file=str(cert_file),
                    key_file=str(key_file),
                ),
                params,
            )

            invalid_configs = (
                types.SimpleNamespace(mode="ws", use_wss=True, cert_file="", key_file=str(key_file)),
                types.SimpleNamespace(mode="ws", use_wss=True, cert_file=str(temp_path / "missing"), key_file=str(key_file)),
                types.SimpleNamespace(mode="tcp", use_wss=True, cert_file=str(cert_file), key_file=str(key_file)),
                types.SimpleNamespace(mode="udp", use_wss=False, cert_file="", key_file=""),
                types.SimpleNamespace(mode="ws", use_wss="true", cert_file=str(cert_file), key_file=str(key_file)),
            )
            for config in invalid_configs:
                with self.subTest(config=config), self.assertRaises(RuntimeError):
                    module._validate_custom_message_server_config(config, params)

    def test_custom_wss_fails_closed_when_imported_server_lacks_tls_parameters(self) -> None:
        with isolated_message_api_module() as module, tempfile.TemporaryDirectory() as temp_dir:
            cert_file = Path(temp_dir) / "cert.pem"
            key_file = Path(temp_dir) / "key.pem"
            cert_file.write_text("certificate", encoding="utf-8")
            key_file.write_text("key", encoding="utf-8")
            config = types.SimpleNamespace(
                mode="ws",
                use_wss=True,
                cert_file=str(cert_file),
                key_file=str(key_file),
            )
            with self.assertRaisesRegex(RuntimeError, "TLS"):
                module._validate_custom_message_server_config(
                    config,
                    {"mode", "enable_custom_uvicorn_logger"},
                )

    def test_custom_wss_passes_complete_certificate_paths_to_message_server(self) -> None:
        with isolated_message_api_module() as module, tempfile.TemporaryDirectory() as temp_dir:
            cert_file = Path(temp_dir) / "cert.pem"
            key_file = Path(temp_dir) / "key.pem"
            cert_file.write_text("certificate", encoding="utf-8")
            key_file.write_text("key", encoding="utf-8")

            config = types.SimpleNamespace(
                use_custom=True,
                host="127.0.0.1",
                port=8090,
                mode="ws",
                use_wss=True,
                cert_file=str(cert_file),
                key_file=str(key_file),
                auth_token=[],
            )
            module.global_config = types.SimpleNamespace(ncnk_message=config)
            module.get_global_server = lambda: types.SimpleNamespace(
                configure_auth=lambda _tokens: None,
                validate_security=lambda: None,
                auth_tokens=(),
                get_app=lambda: object(),
            )
            captured = {}

            class CapturingMessageServer:
                def __init__(
                    self,
                    host,
                    port,
                    mode="ws",
                    ssl_certfile=None,
                    ssl_keyfile=None,
                    enable_custom_uvicorn_logger=False,
                ):
                    captured.update(locals())

            module.MessageServer = CapturingMessageServer
            with mock.patch.dict("os.environ", {"HOST": "127.0.0.1", "PORT": "8080"}, clear=True):
                module.get_global_api()

            self.assertEqual(captured["mode"], "ws")
            self.assertEqual(captured["ssl_certfile"], str(cert_file))
            self.assertEqual(captured["ssl_keyfile"], str(key_file))


class RouterTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_uses_environment_token_as_fallback(self) -> None:
        router = Router(
            RouteConfig(
                route_config={"test": TargetConfig(url="ws://127.0.0.1:8000/ws")}
            )
        )
        with (
            mock.patch.dict(
                "os.environ", {"NACHOBOT_CORE_TOKEN": "environment-secret"}, clear=True
            ),
            mock.patch(
                "ncnk_message.router.MessageClient.connect",
                new=mock.AsyncMock(),
            ) as connect,
        ):
            await router.connect("test")

        self.assertEqual(connect.await_args.kwargs["token"], "environment-secret")

    async def test_explicit_router_token_overrides_environment(self) -> None:
        router = Router(
            RouteConfig(
                route_config={
                    "test": TargetConfig(
                        url="ws://127.0.0.1:8000/ws",
                        token="explicit-secret",
                    )
                }
            )
        )
        with (
            mock.patch.dict(
                "os.environ", {"NACHOBOT_CORE_TOKEN": "environment-secret"}, clear=True
            ),
            mock.patch(
                "ncnk_message.router.MessageClient.connect",
                new=mock.AsyncMock(),
            ) as connect,
        ):
            await router.connect("test")

        self.assertEqual(connect.await_args.kwargs["token"], "explicit-secret")


class WebSocketOriginTests(unittest.TestCase):
    def test_browser_origin_is_denied_by_default_but_non_browser_clients_work(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(_browser_origin_allowed(None))
            self.assertFalse(_browser_origin_allowed("https://hostile.example"))
            self.assertFalse(_browser_origin_allowed("http://127.0.0.1:8088"))

    def test_browser_origin_requires_an_exact_explicit_allowlist_entry(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"NACHOBOT_WS_ALLOWED_ORIGINS": "https://panel.example.com"},
            clear=True,
        ):
            self.assertTrue(_browser_origin_allowed("https://panel.example.com"))
            self.assertTrue(_browser_origin_allowed("https://PANEL.example.com:443"))
            self.assertFalse(_browser_origin_allowed("https://panel.example.com.evil"))

    def test_untrusted_origin_and_bad_token_are_rejected_before_session_use(self) -> None:
        app = FastAPI()
        message_server = MessageServer(app=app, enable_token=True)
        message_server.add_valid_token("expected")
        client = TestClient(app)

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            self.assertRaises(WebSocketDisconnect) as origin_rejection,
        ):
            with client.websocket_connect(
                "/ws",
                headers={"origin": "https://hostile.example"},
            ):
                self.fail("untrusted browser origin was accepted")
        self.assertEqual(origin_rejection.exception.code, 1008)

        with self.assertRaises(WebSocketDisconnect) as token_rejection:
            with client.websocket_connect(
                "/ws",
                headers={"authorization": "wrong"},
            ):
                self.fail("invalid token was accepted")
        self.assertEqual(token_rejection.exception.code, 1008)

        with client.websocket_connect(
            "/ws",
            headers={"authorization": "expected", "platform": "test"},
        ):
            pass


if __name__ == "__main__":
    unittest.main()
