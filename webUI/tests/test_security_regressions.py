from __future__ import annotations

import asyncio
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import chat_media  # noqa: E402
import memory_manager  # noqa: E402
import multimodal_runtime  # noqa: E402
import process_manager  # noqa: E402
import setup_checks  # noqa: E402
import setup_deployment  # noqa: E402
import server  # noqa: E402
from secure_paths import (  # noqa: E402
    ensure_within,
    resolve_external_path,
    resolve_named_dir,
    resolve_named_file,
    resolve_relative_to_root,
)


class SecurePathRegressionTests(unittest.TestCase):
    def test_root_relative_resolvers_reject_traversal_and_windows_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for value in (
                "../outside",
                "nested/../inside.toml",
                r"..\outside",
                r"nested\inside.toml",
                r"C:\outside\secret.toml",
                r"\\server\share\secret.toml",
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    resolve_relative_to_root(root, value)
                with self.subTest(filename=value), self.assertRaises(ValueError):
                    resolve_named_file(root, value)
                with self.subTest(dirname=value), self.assertRaises(ValueError):
                    resolve_named_dir(root, value)

    def test_ensure_within_rejects_traversal_even_when_normalized_inside(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                ensure_within(root, root / "nested" / ".." / "inside.txt")

    def test_ensure_within_resolves_symlink_before_accepting_target(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(root_dir)
            outside = Path(outside_dir)
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with self.assertRaises(ValueError):
                ensure_within(root, link / "secret.txt", must_exist=True)

    def test_external_paths_keep_operator_selected_absolute_directories(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(root_dir)
            outside = Path(outside_dir)
            resolved = resolve_external_path(
                outside,
                base_dir=root,
                must_exist=True,
                must_be_dir=True,
            )
            self.assertEqual(resolved, outside.resolve())
            relative_external = resolve_external_path("../models", base_dir=root)
            self.assertEqual(relative_external, root.parent / "models")

    def test_root_confined_names_reject_ntfs_alternate_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for resolver, value in (
                (resolve_named_file, "config.toml:secret"),
                (resolve_named_dir, "plugins:secret"),
                (resolve_relative_to_root, "config/config.toml:secret"),
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    resolver(root, value)

    def test_media_store_accepts_only_generated_ids_and_blocks_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(root_dir)
            store = chat_media.ChatMediaStore(root)
            for name, expected_suffix in (
                ("notes.TXT", ".txt"),
                ("乐曲.音频", ".音频"),
                ("bounded.12345678901", ".12345678901"),
                # The 12-character maximum includes the leading dot.
                ("too-long.123456789012", ""),
            ):
                with self.subTest(name=name):
                    metadata = store.save_bytes("file", b"payload", name=name)
                    self.assertTrue(metadata["media_id"].endswith(expected_suffix))
                    self.assertEqual(
                        store.resolve(metadata["media_id"]).read_bytes(), b"payload"
                    )

            overlong_suffix = "b" * 32 + ".123456789012"
            (root / overlong_suffix).write_bytes(b"payload")
            with self.assertRaises(chat_media.ChatMediaError):
                store.resolve(overlong_suffix)

            for invalid in ("../../secret", r"..\secret", "arbitrary.txt", r"C:\secret.txt"):
                with self.subTest(media_id=invalid), self.assertRaises(chat_media.ChatMediaError):
                    store.resolve(invalid)

            outside = Path(outside_dir) / "secret.txt"
            outside.write_text("secret", encoding="utf-8")
            symlink_id = "a" * 32 + ".txt"
            try:
                (root / symlink_id).symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises(chat_media.ChatMediaError):
                store.resolve(symlink_id)

    def test_runtime_project_builder_validates_profile_before_path_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "runtime"
            with patch.object(multimodal_runtime.MultimodalRuntimeManager, "RUNTIME_DIR", runtime_root):
                with self.assertRaises(ValueError):
                    multimodal_runtime.MultimodalRuntimeManager._prepare_project("../../outside")
            self.assertFalse(runtime_root.exists())

    def test_tts_launch_selects_supported_cuda_wheel_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entrypoint = root / "NachoBot-Multimodal-Adapter" / "scripts" / "container_tts_entrypoint.py"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("", encoding="utf-8")
            manager = process_manager.ProcessManager(root)
            with (
                patch.object(multimodal_runtime.MultimodalRuntimeManager, "require_python", return_value=Path("python")),
                patch.object(process_manager, "_read_tts_runtime_settings", return_value=("127.0.0.1", 9880, 9881)),
            ):
                manager._launch_runtime = "gpu"
                _, _, gpu_env = manager._resolve_tts_runtime_cmd()
                manager._launch_runtime = "cpu"
                _, _, cpu_env = manager._resolve_tts_runtime_cmd()

        self.assertEqual(
            gpu_env["NACHOBOT_TTS_TORCH_INDEX"],
            "https://download.pytorch.org/whl/cu126",
        )
        self.assertEqual(
            cpu_env["NACHOBOT_TTS_TORCH_INDEX"],
            "https://download.pytorch.org/whl/cpu",
        )


class SetupErrorRedactionTests(unittest.TestCase):
    def test_environment_checks_do_not_return_exception_text(self) -> None:
        diagnostic = "PRIVATE-CREDENTIAL-AND-PATH"
        with patch.object(setup_checks.subprocess, "run", side_effect=OSError(diagnostic)):
            python_result = setup_checks.EnvironmentChecker.check_python()
            git_result = setup_checks.EnvironmentChecker.check_git()

        self.assertEqual(python_result["message"], "检测 Python 失败")
        self.assertEqual(git_result["message"], "检测 Git 失败")
        self.assertNotIn(diagnostic, str(python_result))
        self.assertNotIn(diagnostic, str(git_result))

    def test_snowluma_discovery_status_uses_sanitized_locator_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = server.SnowLumaManager.installation_status(
                Path(temp_dir), "snowluma"
            )

        self.assertEqual(
            result["runtime_error"],
            "SnowLuma Runtime 未找到有效的目录，请部署 SnowLuma Runtime",
        )
        self.assertNotIn(temp_dir, str(result))

    def test_napcat_path_verification_does_not_return_os_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir)
            (install_dir / "launcher-user.bat").write_text("", encoding="utf-8")
            with patch.object(
                setup_checks.tempfile,
                "NamedTemporaryFile",
                side_effect=OSError("PRIVATE-CREDENTIAL-AND-PATH"),
            ):
                result = setup_checks.PathVerifier._check_napcat(
                    install_dir,
                    "https://example.invalid/download",
                )

        self.assertFalse(result["valid"])
        self.assertNotIn("PRIVATE-CREDENTIAL-AND-PATH", str(result))
        self.assertNotIn(str(install_dir), result["message"])

    def test_napcat_path_verification_rejects_config_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
            install_dir = Path(root_dir)
            (install_dir / "launcher-user.bat").write_text("", encoding="utf-8")
            try:
                (install_dir / "config").symlink_to(
                    Path(outside_dir), target_is_directory=True
                )
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            result = setup_checks.PathVerifier._check_napcat(
                install_dir, "https://example.invalid/download"
            )
            self.assertEqual(list(Path(outside_dir).iterdir()), [])

        self.assertFalse(result["valid"])
        self.assertEqual(result["message"], "❌ NapCat 配置目录路径无效")

    def test_napcat_configurator_rejects_account_config_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
            config_dir = Path(root_dir) / "config"
            config_dir.mkdir()
            outside_config = Path(outside_dir) / "onebot11_12345.json"
            sentinel = b'{"operatorOwned":true}'
            outside_config.write_bytes(sentinel)
            try:
                (config_dir / outside_config.name).symlink_to(outside_config)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with self.assertRaises(ValueError):
                setup_deployment.NapCatConfigurator.configure(root_dir, "12345")

            self.assertEqual(outside_config.read_bytes(), sentinel)

    def test_config_generation_returns_safe_error_without_raw_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diagnostic = "PRIVATE-CREDENTIAL-AND-PATH"
            with (
                patch.object(setup_deployment, "ROOT_DIR", root),
                patch.object(setup_deployment, "TEMPLATE_MAP", {"template.toml": "test-component/config.toml"}),
                patch.object(setup_deployment.ConfigInitializer, "_should_generate", return_value=True),
                patch.object(setup_deployment.ConfigInitializer, "_read_template", return_value="[test]\nvalue = 1\n"),
                patch.object(setup_deployment.ConfigInitializer, "_apply_overrides", side_effect=OSError(diagnostic)),
            ):
                result = setup_deployment.ConfigInitializer.generate_configs({})

        self.assertEqual(result["errors"], ["test-component/config.toml: 配置生成失败"])
        self.assertNotIn(diagnostic, str(result))
        self.assertIn("generated", result)
        self.assertIn("skipped", result)
        self.assertIn("backups", result)

    def test_discord_migration_error_does_not_return_adapter_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / setup_deployment.DISCORD_TARGET
            target.parent.mkdir(parents=True)
            target.write_text("[discord]\n", encoding="utf-8")
            secret_detail = "PRIVATE-TOKEN-and-local-path"
            migration_result = SimpleNamespace(success=False, error=secret_detail)
            with (
                patch.object(setup_deployment, "ROOT_DIR", root),
                patch.object(
                    setup_deployment.ConfigInitializer,
                    "_discord_migration_api",
                    return_value=(lambda _path: True, lambda **_paths: migration_result),
                ),
            ):
                success, errors = setup_deployment.ConfigInitializer._prepare_discord_config()

        self.assertFalse(success)
        self.assertEqual(
            errors,
            ["Discord 旧配置迁移失败；请保留旧配置并检查迁移日志"],
        )
        self.assertNotIn(secret_detail, str(errors))

    def test_napcat_configure_keeps_error_shape_without_returning_path_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "PRIVATE-CREDENTIAL-AND-PATH"
            result = setup_deployment.NapCatConfigurator.configure(str(missing))

        self.assertEqual(set(result), {"configured", "skipped", "errors"})
        self.assertEqual(result["errors"], ["NapCat 目录无效，请检查安装路径"])
        self.assertNotIn(str(missing), str(result))

        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostic = "PRIVATE-CREDENTIAL-AND-PATH"
            with (
                patch.object(
                    setup_deployment.NapCatConfigurator,
                    "_load_adapter_ws_entry",
                    return_value=dict(setup_deployment.NapCatConfigurator._WS_CLIENT_ENTRY),
                ),
                patch.object(
                    setup_deployment.NapCatConfigurator,
                    "_configure_file",
                    side_effect=OSError(diagnostic),
                ),
            ):
                result = setup_deployment.NapCatConfigurator.configure(temp_dir)

        self.assertEqual(
            result["errors"], ["onebot11.json: 配置文件处理失败"]
        )
        self.assertNotIn(diagnostic, str(result))

    def test_memory_timeout_log_escapes_and_bounds_user_action(self) -> None:
        async def timeout_without_awaiting(awaitable, timeout):
            self.assertEqual(timeout, memory_manager.MEMORY_MAINTAIN_TIMEOUT_SECONDS)
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError

        action = "reinforce\r\nFORGED-LOG-LINE" + ("x" * 100)
        with (
            patch.object(memory_manager, "_is_enabled_from_config", return_value=True),
            patch.object(memory_manager, "_core_api_request", return_value=None),
            patch.object(memory_manager.asyncio, "wait_for", side_effect=timeout_without_awaiting),
            self.assertLogs("webui.memory", level="WARNING") as captured,
        ):
            result = asyncio.run(memory_manager.maintain(action))

        self.assertFalse(result["success"])
        self.assertEqual(len(captured.records), 1)
        rendered = captured.output[0]
        self.assertNotIn("\nFORGED-LOG-LINE", rendered)
        safe_action = captured.records[0].args[0]
        self.assertIn(r"\r\n", safe_action)
        self.assertNotIn("\r", safe_action)
        self.assertNotIn("\n", safe_action)
        self.assertLessEqual(len(safe_action), 54)

    def test_napcat_configure_log_records_only_counts(self) -> None:
        returned = {
            "configured": ["onebot11_12345.json"],
            "skipped": [],
            "errors": ["operator path\r\nFORGED-LOG-LINE"],
        }
        with (
            patch.object(server, "read_qq_adapter", return_value="napcat"),
            patch.object(server.NapCatConfigurator, "configure", return_value=returned),
            self.assertLogs("webui", level="INFO") as captured,
        ):
            response = asyncio.run(
                server.setup_configure_napcat(
                    server.NapCatConfigRequest(napcat_dir="operator-selected")
                )
            )

        self.assertEqual(response, returned)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(
            captured.records[0].getMessage(),
            "[Setup] napcat configure: configured_count=1, skipped_count=0, error_count=1",
        )
        self.assertNotIn("FORGED-LOG-LINE", captured.output[0])


if __name__ == "__main__":
    unittest.main()
