from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


QQCALL_ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = QQCALL_ROOT / "scripts" / "adapter_extra.py"
VALIDATOR_SOURCE = QQCALL_ROOT / "service" / "config.py"
REPO_ROOT = QQCALL_ROOT.parent
EXTRA = "qqcall-audio"
NONE = "none"
WARNING = "[WARN] QQCall audio extra unavailable; using ordinary adapter startup.\n"


def _load_defaults() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("adapter_extra_test_config", VALIDATOR_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DEFAULT_CONFIG


DEFAULT_CONFIG = _load_defaults()


def _toml_value(value: Any) -> str:
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        if type(value) is float and math.isnan(value):
            return "nan"
        if type(value) is float and math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + pairs + " }"
    raise TypeError(type(value).__name__)


def _toml_document(value: dict[str, Any]) -> str:
    lines: list[str] = []

    def table(current: dict[str, Any], prefix: str) -> None:
        if prefix:
            if lines:
                lines.append("")
            lines.append(f"[{prefix}]")
        for key, item in current.items():
            if not isinstance(item, dict):
                lines.append(f"{key} = {_toml_value(item)}")
        for key, item in current.items():
            if isinstance(item, dict):
                table(item, f"{prefix}.{key}" if prefix else key)

    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(f"{key} = {_toml_value(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            table(item, key)
    return "\n".join(lines) + "\n"


def _enabled_config(adapter: str) -> dict[str, Any]:
    value = copy.deepcopy(DEFAULT_CONFIG)
    value["plugin"]["enabled"] = True
    value["qqcall"]["adapter"] = adapter
    value["qqcall"]["account_id"] = "12345"
    value["qqcall"]["onebot"]["url"] = "http://127.0.0.1:3001"
    value["qqcall"]["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
    value["qqcall"]["audio"]["capture_device"] = "Virtual Output (loopback)"
    value["qqcall"]["audio"]["playback_device"] = "Virtual Input"
    return value


class AdapterExtraTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.helper = self.root / "NachoBot-QQCall" / "scripts" / "adapter_extra.py"
        self.validator = self.root / "NachoBot-QQCall" / "service" / "config.py"
        self.config = self.root / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
        self.helper.parent.mkdir(parents=True)
        self.validator.parent.mkdir(parents=True)
        self.config.parent.mkdir(parents=True)
        shutil.copyfile(HELPER_SOURCE, self.helper)
        shutil.copyfile(VALIDATOR_SOURCE, self.validator)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, value: dict[str, Any] | None = None, *, raw: str | None = None) -> None:
        if raw is not None:
            self.config.write_text(raw, encoding="utf-8")
        else:
            assert value is not None
            self.config.write_text(_toml_document(value), encoding="utf-8")

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(self.helper), *args],
            cwd=self.temporary.name,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def assert_cli_result(
        self,
        expected: str,
        *args: str,
        warning: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = self.run_helper(*args)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, expected + "\n")
        self.assertEqual(result.stderr, WARNING if warning else "")
        return result

    def test_enabled_exact_adapter_selects_extra_under_isolated_stdlib_python(self) -> None:
        for adapter in ("napcat", "snowluma"):
            with self.subTest(adapter=adapter):
                self.write_config(_enabled_config(adapter))
                result = self.assert_cli_result(EXTRA, "--adapter", adapter)
                self.assertNotIn("ncnk_message", result.stdout + result.stderr)
                self.assertNotIn("qqcall_pcm", result.stdout + result.stderr)

    def test_disabled_and_mismatched_adapter_stay_text_only(self) -> None:
        disabled = copy.deepcopy(DEFAULT_CONFIG)
        disabled["qqcall"]["adapter"] = "snowluma"
        self.write_config(disabled)
        self.assert_cli_result(NONE, "--adapter", "snowluma")

        self.write_config(_enabled_config("napcat"))
        self.assert_cli_result(NONE, "--adapter", "snowluma")
        self.write_config(_enabled_config("snowluma"))
        self.assert_cli_result(NONE, "--adapter", "napcat")

    def test_auto_selection_enables_both_adapter_dependencies(self) -> None:
        self.write_config(_enabled_config("auto"))
        for adapter in ("napcat", "snowluma"):
            self.assert_cli_result(EXTRA, "--adapter", adapter)

    def test_missing_invalid_schema_oversize_duplicate_and_unknown_config_fail_closed(self) -> None:
        cases: list[tuple[str, str | None]] = [
            ("missing", None),
            ("malformed", "schema = [\nprivate-marker-4821"),
            ("duplicate", "schema = 6\nschema = 6\n"),
            ("oversize", " " * (64 * 1024 + 1)),
        ]
        for label, raw in cases:
            with self.subTest(label=label):
                if self.config.exists():
                    self.config.unlink()
                if raw is not None:
                    self.write_config(raw=raw)
                self.assert_cli_result(NONE, "--adapter", "snowluma", warning=True)

        unknown_schema = copy.deepcopy(DEFAULT_CONFIG)
        unknown_schema["schema"] = 55
        self.write_config(unknown_schema)
        self.assert_cli_result(NONE, "--adapter", "snowluma", warning=True)

        unknown_field = _enabled_config("snowluma")
        unknown_field["untrusted"] = "private-marker-4821"
        self.write_config(unknown_field)
        result = self.assert_cli_result(NONE, "--adapter", "snowluma", warning=True)
        self.assertNotIn("private-marker-4821", result.stdout + result.stderr)

    def test_invalid_cli_adapter_emits_only_fixed_fallback_and_warning(self) -> None:
        self.write_config(_enabled_config("snowluma"))
        result = self.assert_cli_result(NONE, "--adapter", "private-marker-4821", warning=True)
        self.assertNotIn("private-marker-4821", result.stdout + result.stderr)

    def test_untrusted_config_value_is_never_imported_or_echoed(self) -> None:
        marker = Path(self.temporary.name) / "must-not-be-created"
        value = _enabled_config("snowluma")
        value["qqcall"]["adapter"] = (
            "__import__('pathlib').Path(" + repr(str(marker)) + ").write_text('executed')"
        )
        self.write_config(value)
        result = self.assert_cli_result(NONE, "--adapter", "snowluma", warning=True)
        self.assertFalse(marker.exists())
        self.assertNotIn(str(marker), result.stdout + result.stderr)

        source = HELPER_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("import_module(", source)
        self.assertNotIn("__import__(", source)
        self.assertIn('"_nachobot_qqcall_adapter_extra_validator"', source)

    @unittest.skipUnless(os.name == "nt", "cmd.exe fake-uv harness is Windows-specific")
    def test_batch_fake_uv_routes_extra_once_and_preserves_text_fallback(self) -> None:
        source_paths = (
            REPO_ROOT / "launchbot.bat",
            REPO_ROOT / "launchbot_lite.bat",
            REPO_ROOT / "launchbot_potato.bat",
            REPO_ROOT / "NachoBot-SnowLuma-Adapter" / "run.bat",
        )
        for path in source_paths:
            text = path.read_text(encoding="utf-8")
            self.assertIn("adapter_extra.py", text, path.name)
            self.assertIn('set "QQCALL_EXTRA=none"', text, path.name)
            self.assertIn("uv sync --extra qqcall-audio --python", text, path.name)
            helper_index = text.index("adapter_extra.py")
            sync_index = text.index("uv sync --extra qqcall-audio --python")
            self.assertLess(helper_index, sync_index, path.name)

        for path in source_paths[:3]:
            text = path.read_text(encoding="utf-8")
            self.assertIn('set "PYTHON_CMD=uv run python"', text, path.name)
            self.assertIn('set "ADAPTER_PYTHON_CMD=uv run --no-sync python"', text, path.name)
            self.assertIn("%PYTHON_CMD% %NACHOBOT_MAIN%", text, path.name)
            self.assertIn("%ADAPTER_PYTHON_CMD% %ADAPTER_MAIN%", text, path.name)
            self.assertIn(
                'uv run --project "%NACHOBOT_DIR%" --no-sync python '
                '"%ROOT%NachoBot-QQCall\\scripts\\adapter_extra.py" --adapter "%QQ_ADAPTER%"',
                text,
                path.name,
            )
        standalone_text = source_paths[3].read_text(encoding="utf-8")
        self.assertIn("uv run --no-project --python", standalone_text)
        self.assertIn("--adapter snowluma", standalone_text)
        self.assertIn('uv run --no-sync python main.py', standalone_text)
        standalone_helper_index = standalone_text.index("adapter_extra.py")
        standalone_sync_index = standalone_text.index("uv sync --extra qqcall-audio --python")
        self.assertLess(standalone_helper_index, standalone_sync_index)

        self._run_fake_uv_harness("qqcall-audio", expect_extra=True, standalone=False)
        self._run_fake_uv_harness("none", expect_extra=False, standalone=False)
        self._run_fake_uv_harness("fail", expect_extra=False, standalone=True)

    def _run_fake_uv_harness(self, output: str, *, expect_extra: bool, standalone: bool) -> None:
        fake_bin = Path(self.temporary.name) / f"fake-uv-{output}-{standalone}"
        fake_bin.mkdir()
        log_path = Path(self.temporary.name) / f"uv-{output}-{standalone}.log"
        fake_uv = fake_bin / "uv.cmd"
        fake_uv.write_text(
            # ``call`` in the generated harness models an external uv executable;
            # the test double is a .cmd script and must be returned to explicitly.
            "@echo off\n"
            '>>"%UV_LOG%" echo(%*\n'
            'echo(%* | findstr /c:"adapter_extra.py" >nul\n'
            "if not errorlevel 1 (\n"
            '  if /i "%UV_HELPER_MODE%"=="fail" exit /b 1\n'
            '  if defined UV_HELPER_OUTPUT echo %UV_HELPER_OUTPUT%\n'
            ")\n"
            "exit /b 0\n",
            encoding="ascii",
        )
        helper_command = (
            'uv run --no-project --python ">=3.11,<=3.13" python '
            '"C:\\repo\\NachoBot-QQCall\\scripts\\adapter_extra.py" --adapter snowluma'
            if standalone
            else 'uv run --project "C:\\repo\\NachoBot" --no-sync python '
            '"C:\\repo\\NachoBot-QQCall\\scripts\\adapter_extra.py" --adapter snowluma'
        )
        harness = Path(self.temporary.name) / f"harness-{output}-{standalone}.cmd"
        helper_loop = (
            'for /f "usebackq delims=" %%E in (`'
            + helper_command
            + '`) do if "%%E"=="qqcall-audio" set "QQCALL_EXTRA=qqcall-audio"\n'
        )
        harness_source = "".join(
            (
                "@echo off\n",
                'set "PATH=' + str(fake_bin) + ';%PATH%"\n',
                'set "UV_LOG=' + str(log_path) + '"\n',
                'set "QQCALL_EXTRA=none"\n',
                'call uv run python bot.py\n',
                helper_loop,
                'if /i "%QQCALL_EXTRA%"=="qqcall-audio" (\n',
                '  call uv sync --extra qqcall-audio --python ">=3.11,<=3.13"\n',
                ') else (\n',
                '  call uv sync --python ">=3.11,<=3.13"\n',
                ')\n',
                'call uv run --no-sync python main.py\n',
            )
        )
        harness.write_text(harness_source, encoding="ascii")
        env = dict(os.environ)
        env["UV_HELPER_OUTPUT"] = "" if output == "fail" else output
        env["UV_HELPER_MODE"] = output
        completed = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(harness)],
            cwd=self.temporary.name,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        commands = log_path.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(
            len(commands),
            2,
            f"fake uv did not see helper call; output={completed.stdout!r}; "
            f"stderr={completed.stderr!r}; harness={harness.read_text(encoding='ascii')!r}; "
            f"fake_uv={fake_uv.read_text(encoding='ascii')!r}",
        )
        self.assertEqual(commands[0], "run python bot.py")
        self.assertIn("adapter_extra.py", commands[1])
        sync_commands = [line for line in commands if line.startswith("sync ")]
        self.assertEqual(len(sync_commands), 1)
        if expect_extra:
            self.assertIn("sync --extra qqcall-audio --python", sync_commands[0])
        else:
            self.assertTrue(sync_commands[0].startswith("sync --python "))
            self.assertNotIn("--extra", sync_commands[0])
        self.assertEqual(commands[-1], "run --no-sync python main.py")


if __name__ == "__main__":
    unittest.main()
