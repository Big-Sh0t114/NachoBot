from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import config_manager
import process_manager
import setup_deployment
from qq_adapter_selector import QQAdapterSelectorError, parse_qq_adapter_env


def test_selector_parser_supports_legacy_default_and_rejects_ambiguous_values() -> None:
    assert parse_qq_adapter_env("HOST=127.0.0.1\n") == "napcat"
    assert parse_qq_adapter_env("qq_adapter=SnowLuma\n") == "snowluma"
    with pytest.raises(QQAdapterSelectorError):
        parse_qq_adapter_env("qq_adapter=\n")
    with pytest.raises(QQAdapterSelectorError):
        parse_qq_adapter_env("qq_adapter=napcat\nQQ_ADAPTER=snowluma\n")
    with pytest.raises(QQAdapterSelectorError):
        parse_qq_adapter_env("qq_adapter=other\n")


@pytest.mark.parametrize(
    "launcher_name",
    ("launchbot.bat", "launchbot_lite.bat", "launchbot_potato.bat"),
)
def test_launchers_default_missing_selector_and_reject_duplicates(launcher_name: str) -> None:
    source = (WEBUI_DIR.parent / launcher_name).read_text(encoding="utf-8")
    assert "if($values.Count -ne 1)" not in source
    assert "if($values.Count -eq 0) { 'napcat'; exit 0 }" in source
    assert "if($values.Count -gt 1) { exit 1 }" in source


@pytest.mark.parametrize(
    "launcher_name",
    ("launchbot.bat", "launchbot_lite.bat", "launchbot_potato.bat"),
)
def test_launchers_resolve_custom_napcat_port_from_root_adapter_path(
    tmp_path: Path, launcher_name: str
) -> None:
    """The BAT source must not depend on a stale %ADAPTER_DIR% expansion."""
    source = (WEBUI_DIR.parent / launcher_name).read_text(encoding="utf-8")
    marker = "$p='%ROOT%NachoBot-Napcat-Adapter\\config.toml'"
    assert marker in source
    assert "$p='%ADAPTER_DIR%\\config.toml'" not in source

    # Keep the regression non-launching while proving the intended path carries
    # a non-default port that the embedded PowerShell probe should read.
    config_path = tmp_path / "NachoBot-Napcat-Adapter" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("[napcat_server]\nport = 18195\n", encoding="utf-8")
    import tomllib

    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["napcat_server"]["port"] == 18195


@pytest.mark.parametrize(
    "launcher_name",
    ("launchbot.bat", "launchbot_lite.bat", "launchbot_potato.bat"),
)
def test_qq_launchers_fail_closed_when_selected_adapter_sync_fails(launcher_name: str) -> None:
    source = (WEBUI_DIR.parent / launcher_name).read_text(encoding="utf-8")
    sync_start = source.rfind('uv sync --python ">=3.11,<=3.13"')
    assert sync_start >= 0
    project_dir_start = source.rfind('if not exist "%ADAPTER_DIR%\\."', 0, sync_start)
    project_file_start = source.rfind(
        'if not exist "%ADAPTER_DIR%\\pyproject.toml"', 0, sync_start
    )
    cd_start = source.rfind('cd /d "%ADAPTER_DIR%"', 0, sync_start)
    assert project_dir_start >= 0
    assert project_file_start > project_dir_start
    assert cd_start > project_file_start
    assert cd_start < sync_start

    guard_start = source.find("if errorlevel 1 (", sync_start + len('uv sync --python ">=3.11,<=3.13"'))
    assert guard_start >= 0
    first_service_start = source.find(
        'if exist "%NACHOBOT_DIR%\\%NACHOBOT_MAIN%"', sync_start
    )
    assert first_service_start > guard_start
    guard = source[guard_start:first_service_start]
    assert "adapter dependency sync failed" in guard
    assert "goto :EXIT" in guard or "exit /b 1" in guard

    project_guard = source[project_dir_start:cd_start]
    assert "adapter setup failed" in project_guard
    assert "project directory not found" in project_guard
    assert "pyproject.toml not found" in project_guard
    cd_guard = source[cd_start:sync_start]
    assert "project directory could not be entered" in cd_guard


@pytest.mark.parametrize(
    "launcher_name",
    ("launchbot.bat", "launchbot_lite.bat", "launchbot_potato.bat"),
)
def test_qq_launchers_gate_full_snowluma_manifest_and_order_runtime_before_adapter(
    launcher_name: str,
) -> None:
    source = (WEBUI_DIR.parent / launcher_name).read_text(encoding="utf-8")
    for required in (
        "package.json",
        "index.mjs",
        "utils-tSVKpzEf.js",
        "logger-BAozzyTt.js",
        "config-GJCFWjtq.js",
        "server-CLw7fwOG.js",
        "node.exe",
        "launcher.bat",
        "client\\index.html",
        "native\\snowluma-win32-x64.dll",
        "native\\snowluma-win32-x64.node",
        "native\\websocket-win32-x64.node",
        "NachoBot-SnowLuma-Adapter/main.py",
        "NachoBot-SnowLuma-Adapter/pyproject.toml",
    ):
        assert required in source
    assert "runtime.json" in source
    assert "ConvertFrom-Json" in source
    assert "https://github.com/SnowLuma/SnowLuma/releases/latest" in source
    verify_call = source.index("call :VERIFY_SNOWLUMA_COMPONENTS")
    adapter_sync = source.index('uv sync --python ">=3.11,<=3.13"', verify_call)
    runtime_call = source.index("call :START_SNOWLUMA_RUNTIME", adapter_sync)
    runtime_helper = source.index("\n:START_SNOWLUMA_RUNTIME")
    runtime_start = source.index('start "SnowLuma Runtime"', runtime_helper)
    adapter_start = source.index('start "NachoBot-SnowLuma"', runtime_call)
    assert verify_call < adapter_sync < runtime_call < adapter_start
    assert 'cmd /k ""!SNOWLUMA_NODE!" index.mjs"' in source
    assert "SNOWLUMA_HOST" in source
    assert "webuiHost" in source
    assert "if ($h -ne '127.0.0.1')" in source
    assert "localhost" not in source[source.index("SNOWLUMA_HOST"):runtime_start]
    assert "::1" not in source[source.index("SNOWLUMA_HOST"):runtime_start]
    assert "SNOWLUMA_ONEBOT_PORT" in source
    assert source.index('call :CHECK_SNOWLUMA_PORT_FREE "!SNOWLUMA_PORT!" "WebUI"') < source.index(
        'call :CHECK_SNOWLUMA_PORT_FREE "!SNOWLUMA_ONEBOT_PORT!" "OneBot"'
    ) < runtime_start


def test_snowluma_manifest_and_standalone_launcher_provision_dependencies() -> None:
    import tomllib

    project = tomllib.loads(
        (WEBUI_DIR.parent / "NachoBot-SnowLuma-Adapter" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    dependency_names = {
        dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].strip().lower()
        for dependency in project["project"]["dependencies"]
    }
    assert {"aiohttp", "cryptography", "fastapi", "loguru", "starlette", "uvicorn"} <= dependency_names

    source = (WEBUI_DIR.parent / "NachoBot-SnowLuma-Adapter" / "run.bat").read_text(
        encoding="utf-8"
    )
    assert "where uv" in source
    sync_start = source.index('uv sync --python ">=3.11,<=3.13"')
    run_start = source.index("uv run --no-sync python main.py")
    assert sync_start < run_start
    sync_failure_guard = source.find("if errorlevel 1 (", sync_start)
    assert sync_start < sync_failure_guard < run_start
    assert "SnowLuma adapter dependency sync failed" in source[sync_failure_guard:run_start]
    assert "endlocal & exit /b 1" in source[sync_failure_guard:run_start]


@pytest.mark.parametrize(
    ("env_text", "raw_fragment"),
    (
        ("qq_adapter=\n", "qq_adapter="),
        ("qq_adapter=napcat\nQQ_ADAPTER=snowluma\n", "QQ_ADAPTER=snowluma"),
        ("qq_adapter=untrusted-selector\n", "untrusted-selector"),
    ),
)
def test_invalid_selector_blocks_every_qq_start_before_relay_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_text: str,
    raw_fragment: str,
) -> None:
    env_path = tmp_path / "NachoBot" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(env_text, encoding="utf-8")
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services()

    def relay_validation_must_not_run(_: str) -> None:
        pytest.fail("invalid QQ selector must be rejected before relay validation")

    monkeypatch.setattr(manager, "_require_relay_owner", relay_validation_must_not_run)
    for service_id in process_manager.QQ_SERVICE_IDS:
        with pytest.raises(RuntimeError) as raised:
            manager._validate_service_start(service_id)
        assert str(raised.value) == process_manager.QQ_ADAPTER_SELECTOR_START_ERROR
        assert raw_fragment not in str(raised.value)


def test_direct_start_rejects_unselected_qq_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services()

    with pytest.raises(RuntimeError, match="not selected"):
        manager._validate_service_start("napcat_adapter")


def test_pending_qq_group_start_blocks_selector_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)

    async def scenario() -> None:
        manager = process_manager.ProcessManager(tmp_path)
        process_manager._register_services()
        monkeypatch.setattr(manager, "_validate_group_start", lambda _group_id: None)
        manager.request_start_group("qq_adapter")
        with pytest.raises(ValueError, match="QQ 适配器"):
            process_manager.assert_qq_adapter_switch_allowed(manager)
        pending = manager._operation_tasks["group:qq_adapter"]
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


def test_snowluma_config_generation_preserves_connection_and_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text(
        "HOST=127.0.0.1\nPORT=8000\nqq_adapter=snowluma\n", encoding="utf-8"
    )
    snow_dir = tmp_path / "NachoBot-SnowLuma-Adapter"
    snow_dir.mkdir()
    live = (
        "[snowluma]\n"
        "host = \"snow-host\"\nport = 3009\npath = \"/custom/ws\"\n"
        "token = \"preserved-token\"\n\n"
        "[nachobot_server]\nhost = \"relay-host\"\nport = 8123\n"
        "platform_name = \"qq\"\n\n[voice]\nuse_tts = true\n"
    )
    (snow_dir / "config.toml").write_text(live, encoding="utf-8")
    (snow_dir / "template_config.toml").write_text(
        "[snowluma]\nhost = \"127.0.0.1\"\nport = 3001\n"
        "path = \"/onebot/v11/ws\"\ntoken = \"\"\n\n"
        "[nachobot_server]\nhost = \"127.0.0.1\"\nport = 8070\n"
        "platform_name = \"qq\"\n\n[voice]\nuse_tts = false\n",
        encoding="utf-8",
    )
    (tmp_path / "NachoBot" / "template").mkdir()
    (tmp_path / "NachoBot" / "template" / "template.env").write_text(
        "HOST=127.0.0.1\nPORT=8000\nqq_adapter=napcat\n", encoding="utf-8"
    )

    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        setup_deployment,
        "TEMPLATE_MAP",
        {
            "NachoBot/template/template.env": "NachoBot/.env",
            "NachoBot-SnowLuma-Adapter/template_config.toml": "NachoBot-SnowLuma-Adapter/config.toml",
        },
    )
    monkeypatch.setattr(setup_deployment, "BACKUP_DIR", tmp_path / "backups")

    result = setup_deployment.ConfigInitializer.generate_configs(
        {
            "components": ["qq"],
            "env": {
                "host": "127.0.0.1",
                "port": "8000",
                "qq_adapter": "snowluma",
            },
        }
    )

    assert result["errors"] == []
    rendered = (snow_dir / "config.toml").read_text(encoding="utf-8")
    assert 'host = "snow-host"' in rendered
    assert 'port = 3009' in rendered
    assert 'path = "/custom/ws"' in rendered
    assert 'token = "preserved-token"' in rendered
    assert 'host = "relay-host"' in rendered
    assert 'port = 8123' in rendered
    assert "use_tts = false" in rendered
    assert "NachoBot-Napcat-Adapter/config.toml" not in result["generated"]


def test_snowluma_regeneration_preserves_all_user_tables_except_tts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tomllib

    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    (tmp_path / "NachoBot" / "template").mkdir()
    (tmp_path / "NachoBot" / "template" / "template.env").write_text(
        "HOST=127.0.0.1\nPORT=8000\nqq_adapter=napcat\n", encoding="utf-8"
    )
    snow_dir = tmp_path / "NachoBot-SnowLuma-Adapter"
    snow_dir.mkdir()
    live = (
        "[inner]\nversion = \"user-version\"\n\n"
        "[snowluma]\nhost = \"snow-host\"\nport = 3009\ntoken = \"synthetic-token\"\n\n"
        "[nachobot_server]\nhost = \"relay-host\"\nport = 8123\n\n"
        "[chat]\nenable_chat_list_filter = true\ngroup_list_type = \"whitelist\"\n"
        "group_list = [1001]\nprivate_list_type = \"whitelist\"\nprivate_list = [2002]\n"
        "ban_user_id = [3003]\nban_qq_bot = true\nenable_poke = false\n\n"
        "[send]\nmin_interval_sec = 9.0\n\n"
        "[visual.image]\ntemperature = 0.9\nmax_tokens = 99\nextra_params = { custom = true }\n\n"
        "[debug]\nlevel = \"TRACE\"\nraw_payload = true\nraw_outbound = true\n\n"
        "[voice]\nuse_tts = true\n"
    )
    (snow_dir / "config.toml").write_text(live, encoding="utf-8")
    (snow_dir / "template_config.toml").write_text(
        "[snowluma]\nhost = \"127.0.0.1\"\nport = 3001\ntoken = \"\"\n\n"
        "[nachobot_server]\nhost = \"127.0.0.1\"\nport = 8070\n\n"
        "[chat]\nenable_chat_list_filter = true\ngroup_list_type = \"whitelist\"\n"
        "group_list = []\nprivate_list_type = \"blacklist\"\nprivate_list = []\n"
        "ban_user_id = []\nban_qq_bot = false\nenable_poke = true\n\n"
        "[send]\nmin_interval_sec = 0.5\n\n[voice]\nuse_tts = false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        setup_deployment,
        "TEMPLATE_MAP",
        {
            "NachoBot/template/template.env": "NachoBot/.env",
            "NachoBot-SnowLuma-Adapter/template_config.toml": "NachoBot-SnowLuma-Adapter/config.toml",
        },
    )
    monkeypatch.setattr(setup_deployment, "BACKUP_DIR", tmp_path / "backups")

    result = setup_deployment.ConfigInitializer.generate_configs(
        {"components": ["qq"], "env": {"qq_adapter": "snowluma"}}
    )

    assert result["errors"] == []
    rendered = tomllib.loads((snow_dir / "config.toml").read_text(encoding="utf-8"))
    assert rendered["inner"]["version"] == "user-version"
    assert rendered["snowluma"]["token"] == "synthetic-token"
    assert rendered["nachobot_server"]["port"] == 8123
    assert rendered["chat"]["group_list"] == [1001]
    assert rendered["send"]["min_interval_sec"] == 9.0
    assert rendered["visual"]["image"]["max_tokens"] == 99
    assert rendered["debug"]["raw_payload"] is True
    assert rendered["voice"]["use_tts"] is False


def test_first_snowluma_generation_seeds_only_napcat_chat_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tomllib

    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    (tmp_path / "NachoBot" / "template").mkdir()
    (tmp_path / "NachoBot" / "template" / "template.env").write_text(
        "HOST=127.0.0.1\nPORT=8000\nqq_adapter=napcat\n", encoding="utf-8"
    )
    napcat_dir = tmp_path / "NachoBot-Napcat-Adapter"
    napcat_dir.mkdir()
    napcat_config = (
        "[napcat_server]\ntoken = \"synthetic-napcat-token\"\n\n"
        "[chat]\ngroup_list_type = \"whitelist\"\ngroup_list = [1001, 1002]\n"
        "private_list_type = \"whitelist\"\nprivate_list = [2001]\n"
        "ban_user_id = [3001]\nban_qq_bot = true\nenable_poke = false\n"
    )
    (napcat_dir / "config.toml").write_text(napcat_config, encoding="utf-8")
    snow_dir = tmp_path / "NachoBot-SnowLuma-Adapter"
    snow_dir.mkdir()
    snow_template = (
        "[snowluma]\nhost = \"127.0.0.1\"\nport = 3001\ntoken = \"\"\n\n"
        "[nachobot_server]\nhost = \"127.0.0.1\"\nport = 8070\n\n"
        "[chat]\nenable_chat_list_filter = true\ngroup_list_type = \"whitelist\"\n"
        "group_list = []\nprivate_list_type = \"blacklist\"\nprivate_list = []\n"
        "ban_user_id = []\nban_qq_bot = false\nenable_poke = true\n\n[voice]\nuse_tts = false\n"
    )
    (snow_dir / "template_config.toml").write_text(snow_template, encoding="utf-8")
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        setup_deployment,
        "TEMPLATE_MAP",
        {
            "NachoBot/template/template.env": "NachoBot/.env",
            "NachoBot-SnowLuma-Adapter/template_config.toml": "NachoBot-SnowLuma-Adapter/config.toml",
        },
    )
    monkeypatch.setattr(setup_deployment, "BACKUP_DIR", tmp_path / "backups")

    result = setup_deployment.ConfigInitializer.generate_configs(
        {"components": ["qq"], "env": {"qq_adapter": "snowluma"}}
    )

    assert result["errors"] == []
    rendered = tomllib.loads((snow_dir / "config.toml").read_text(encoding="utf-8"))
    source_chat = tomllib.loads(napcat_config)["chat"]
    assert rendered["chat"]["enable_chat_list_filter"] is True
    for key in (
        "group_list_type",
        "group_list",
        "private_list_type",
        "private_list",
        "ban_user_id",
        "ban_qq_bot",
        "enable_poke",
    ):
        assert rendered["chat"][key] == source_chat[key]
    assert "napcat_server" not in rendered
    assert "synthetic-napcat-token" not in (snow_dir / "config.toml").read_text(encoding="utf-8")


def test_unrelated_generation_preserves_live_qq_selector_when_request_omits_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "NachoBot" / "template").mkdir(parents=True)
    (tmp_path / "NachoBot" / ".env").write_text(
        "HOST=127.0.0.1\nPORT=8000\nqq_adapter=snowluma\n", encoding="utf-8"
    )
    (tmp_path / "NachoBot" / "template" / "template.env").write_text(
        "HOST=127.0.0.1\nPORT=8000\nqq_adapter=napcat\n", encoding="utf-8"
    )
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        setup_deployment,
        "TEMPLATE_MAP",
        {"NachoBot/template/template.env": "NachoBot/.env"},
    )
    monkeypatch.setattr(setup_deployment, "BACKUP_DIR", tmp_path / "backups")

    result = setup_deployment.ConfigInitializer.generate_configs(
        {"components": [], "env": {"host": "127.0.0.1", "port": "8000"}}
    )

    assert result["errors"] == []
    assert "qq_adapter=snowluma" in (tmp_path / "NachoBot" / ".env").read_text(
        encoding="utf-8"
    )


def test_dependency_task_uses_distinct_allowlisted_snowluma_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text(
        "qq_adapter=snowluma\n", encoding="utf-8"
    )
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    tasks = setup_deployment.DependencyInstaller.get_install_tasks(
        ["qq"], qq_adapter="snowluma"
    )
    qq_tasks = [task for task in tasks if task["id"].startswith("qq")]
    assert [task["id"] for task in qq_tasks] == ["qq_snowluma"]
    assert setup_deployment.DependencyInstaller._resolve_task_project(qq_tasks[0]).name == (
        "NachoBot-SnowLuma-Adapter"
    )
    with pytest.raises(ValueError):
        setup_deployment.DependencyInstaller._resolve_task_project(
            {"id": "qq_snowluma", "type": "uv", "dir": "NachoBot-Napcat-Adapter"}
        )


def test_dependency_plan_requires_request_selector_to_match_live_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text(
        "qq_adapter=snowluma\n", encoding="utf-8"
    )
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)

    matching = setup_deployment.DependencyInstaller.get_install_tasks(
        ["qq"], qq_adapter="SnowLuma"
    )
    assert [task["id"] for task in matching if task["id"].startswith("qq")] == [
        "qq_snowluma"
    ]

    with pytest.raises(ValueError, match="不一致"):
        setup_deployment.DependencyInstaller.get_install_tasks(
            ["qq"], qq_adapter="napcat"
        )


def test_dependency_plan_sanitizes_invalid_selector_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text(
        "qq_adapter=untrusted-live-selector\n", encoding="utf-8"
    )
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)

    with pytest.raises(ValueError) as raised:
        setup_deployment.DependencyInstaller.get_install_tasks(["qq"])
    assert "untrusted-live-selector" not in str(raised.value)


@pytest.mark.parametrize(
    "retained_state",
    (
        process_manager.ServiceState(
            status=process_manager.ServiceStatus.ERROR,
            process=SimpleNamespace(returncode=None),
        ),
        process_manager.ServiceState(
            status=process_manager.ServiceStatus.ERROR,
            process_group_id=12345,
        ),
    ),
    ids=("live-process", "retained-posix-group"),
)
def test_retained_qq_runtime_blocks_switch_and_conflicting_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_state: process_manager.ServiceState,
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text(
        "qq_adapter=snowluma\n", encoding="utf-8"
    )
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services()
    manager.states["napcat_adapter"] = retained_state

    with pytest.raises(ValueError, match="QQ 适配器"):
        process_manager.assert_qq_adapter_switch_allowed(manager)

    monkeypatch.setattr(manager, "_require_relay_owner", lambda _service_id: None)
    with pytest.raises(RuntimeError, match="already active"):
        manager._validate_service_start("snowluma_adapter")


def test_terminal_qq_error_without_retained_runtime_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text(
        "qq_adapter=snowluma\n", encoding="utf-8"
    )
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services()
    manager.states["napcat_adapter"] = process_manager.ServiceState(
        status=process_manager.ServiceStatus.ERROR
    )

    process_manager.assert_qq_adapter_switch_allowed(manager)
    monkeypatch.setattr(manager, "_require_relay_owner", lambda _service_id: None)
    manager._validate_service_start("snowluma_adapter")


def test_dependency_install_rejects_stale_qq_task_before_running_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    napcat_dir = tmp_path / "NachoBot-Napcat-Adapter"
    napcat_dir.mkdir()
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    calls: list[Path] = []

    async def forbidden_sync(project_dir: Path, callback: object) -> dict[str, str]:
        calls.append(project_dir)
        return {"status": "ok"}

    monkeypatch.setattr(setup_deployment.DependencyInstaller, "_run_uv_sync", forbidden_sync)
    result = asyncio.run(
        setup_deployment.DependencyInstaller.install(
            {"id": "qq", "type": "uv", "dir": "NachoBot-Napcat-Adapter"}
        )
    )

    assert result["status"] == "error"
    assert "不匹配" in result["message"]
    assert calls == []


def test_setup_generation_prevalidates_before_entering_qr_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server
    from fastapi import HTTPException

    class ContextSpy:
        entered = False

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    spy = ContextSpy()
    monkeypatch.setattr(server.bilibili_login_manager, "config_generation", lambda: spy)

    async def scenario() -> None:
        with pytest.raises(HTTPException) as raised:
            await server.setup_generate_configs(
                server.SetupWizardData(
                    components=["qq"],
                    env={"qq_adapter": ""},
                )
            )
        assert raised.value.status_code == 400
        assert "不能为空" in str(raised.value.detail)

    asyncio.run(scenario())
    assert spy.entered is False


def test_setup_backend_component_checks_use_live_selector_and_report_both_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server
    from fastapi import HTTPException

    env_path = tmp_path / "NachoBot" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("qq_adapter=snowluma\n", encoding="utf-8")
    monkeypatch.setattr(server.config_mgr, "root", tmp_path)
    status = asyncio.run(server.setup_qq_adapter_status())
    assert set(status) >= {"selected", "napcat", "snowluma"}
    assert status["selected"] == "snowluma"
    assert status["napcat"]["download_url"].startswith("https://github.com/NapNeko/")
    assert status["snowluma"]["download_url"].endswith("/releases/latest")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            server.setup_verify_path(
                server.VerifyPathRequest(type="napcat", path="", qq_adapter="napcat")
            )
        )
    assert raised.value.status_code == 409
    assert "选择已变化" in str(raised.value.detail)


def test_snowluma_process_handlers_offload_whole_blocking_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    calls: list[tuple[object, tuple[object, ...]]] = []

    async def fake_to_thread(function: object, *args: object) -> object:
        calls.append((function, args))
        return function(*args)  # type: ignore[operator]

    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        server,
        "_snowluma_list_processes",
        lambda password: [{"pid": 123, "uin": "10001"}],
    )
    monkeypatch.setattr(
        server,
        "_snowluma_probe_login",
        lambda password, pid: {"pid": pid},
    )
    monkeypatch.setattr(
        server,
        "_snowluma_process_action",
        lambda password, pid, action: {"pid": pid, "action": action},
    )

    password = "request-only-password"
    listed = asyncio.run(server.setup_snowluma_processes(server.SnowLumaProcessRequest(password=password)))
    probed = asyncio.run(
        server.setup_snowluma_probe_login("123", server.SnowLumaProcessActionRequest(password=password))
    )
    acted = asyncio.run(
        server.setup_snowluma_process_action(
            "123", "refresh", server.SnowLumaProcessActionRequest(password=password)
        )
    )

    assert listed["list"][0]["uin"] == "10001"
    assert probed["info"]["pid"] == 123
    assert acted["action"] == "refresh"
    assert [args for _, args in calls] == [(password,), (password, 123), (password, 123, "refresh")]


def test_snowluma_server_helpers_logout_on_success_and_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server
    from snowluma_manager import SnowLumaApiError

    secret = "request-only-password"

    class FakeClient:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.closed = 0

        def list_processes(self):
            if self.fail:
                raise SnowLumaApiError("primary list failure")
            return [{"pid": 123}]

        def probe_login(self, pid: int):
            if self.fail:
                raise SnowLumaApiError("primary probe failure")
            return {"pid": pid}

        def process_action(self, pid: int, action: str):
            if self.fail:
                raise SnowLumaApiError("primary action failure")
            return {"pid": pid, "action": action}

        def close(self):
            self.closed += 1
            raise SnowLumaApiError("logout failure must not replace primary error")

    cases = (
        ("_snowluma_list_processes", (secret,), [{"pid": 123}], "primary list failure"),
        ("_snowluma_probe_login", (secret, 123), {"pid": 123}, "primary probe failure"),
        (
            "_snowluma_process_action",
            (secret, 123, "refresh"),
            {"pid": 123, "action": "refresh"},
            "primary action failure",
        ),
    )
    for helper_name, args, expected, failure_message in cases:
        for fail in (False, True):
            client = FakeClient(fail)
            monkeypatch.setattr(server, "_snowluma_client", lambda _password, c=client: c)
            helper = getattr(server, helper_name)
            if fail:
                with pytest.raises(SnowLumaApiError, match=failure_message):
                    helper(*args)
            else:
                assert helper(*args) == expected
            assert client.closed == 1
            assert secret not in repr(expected)


def test_snowluma_login_failure_also_closes_partial_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server
    from snowluma_manager import SnowLumaApiError

    class LoginFailClient:
        closed = 0

        def login(self, password: str) -> None:
            raise SnowLumaApiError("primary login failure")

        def close(self) -> None:
            type(self).closed += 1
            raise SnowLumaApiError("logout failure")

    fake = LoginFailClient()
    monkeypatch.setattr(server, "SnowLumaAPIClient", lambda _root: fake)
    with pytest.raises(SnowLumaApiError, match="primary login failure"):
        server._snowluma_client("request-only-password")
    assert fake.closed == 1


def test_busy_switch_prevalidation_also_skips_qr_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server
    from fastapi import HTTPException

    class ContextSpy:
        entered = False

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    spy = ContextSpy()
    monkeypatch.setattr(server.bilibili_login_manager, "config_generation", lambda: spy)
    monkeypatch.setattr(
        server.ConfigInitializer,
        "prevalidate_qq_adapter_selection",
        staticmethod(lambda _data: ("", "QQ 适配器正在运行或切换中，请先停止当前 QQ 服务")),
    )

    async def scenario() -> None:
        with pytest.raises(HTTPException) as raised:
            await server.setup_generate_configs(server.SetupWizardData(env={"qq_adapter": "napcat"}))
        assert raised.value.status_code == 400
        assert "QQ 适配器正在运行" in str(raised.value.detail)

    asyncio.run(scenario())
    assert spy.entered is False


def test_process_registry_retains_both_services_but_selects_snowluma_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    snow_dir = tmp_path / "NachoBot-SnowLuma-Adapter"
    snow_dir.mkdir()
    (snow_dir / "config.toml").write_text(
        "[snowluma]\nhost = '127.0.0.1'\nport = 3001\npath = '/ws'\n\n"
        "[nachobot_server]\nport = 8123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    process_manager._register_services()
    assert {"napcat_adapter", "snowluma_runtime", "snowluma_adapter", "napcat_shell"}.issubset(
        process_manager.SERVICE_DEFS
    )
    assert process_manager.GROUP_DEFS["qq_adapter"].services == [
        "snowluma_runtime",
        "snowluma_adapter",
    ]
    runtime_def = process_manager.SERVICE_DEFS["snowluma_runtime"]
    assert runtime_def.cmd[0] == str((tmp_path / "SnowLuma" / "node.exe").resolve())
    assert runtime_def.cmd[1:] == ["index.mjs"]
    assert runtime_def.cwd == "SnowLuma"
    snow_def = process_manager.SERVICE_DEFS["snowluma_adapter"]
    assert snow_def.port is None
    assert snow_def.wait_port is False


def test_process_manager_rejects_unsafe_snowluma_launch_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services()

    import snowluma_manager

    def reject_boundary(root: Path, **kwargs: object) -> None:
        raise snowluma_manager.SnowLumaError("SnowLuma WebUI webuiHost 必须是本机回环地址")

    monkeypatch.setattr(snowluma_manager.SnowLumaManager, "validate_launch_boundary", reject_boundary)
    with pytest.raises(RuntimeError, match="webuiHost"):
        manager._validate_service_start("snowluma_runtime")


def test_snowluma_group_recovery_reuses_managed_running_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services()
    monkeypatch.setattr(manager, "_ensure_required_components", lambda _services: None)
    monkeypatch.setattr(manager, "_require_relay_owner", lambda _service_id: None)

    calls: list[bool] = []
    import snowluma_manager

    def boundary(_root: Path, *, require_free_ports: bool = True) -> dict[str, object]:
        calls.append(require_free_ports)
        return {}

    monkeypatch.setattr(snowluma_manager.SnowLumaManager, "validate_launch_boundary", boundary)
    manager.states["snowluma_runtime"] = process_manager.ServiceState(
        status=process_manager.ServiceStatus.RUNNING
    )
    manager.states["snowluma_adapter"] = process_manager.ServiceState(
        status=process_manager.ServiceStatus.STOPPED
    )

    manager._validate_group_start("qq_adapter")
    assert calls and all(require_free_ports is False for require_free_ports in calls)


def test_snowluma_group_start_rejects_external_port_conflict_without_running_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "NachoBot").mkdir()
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services()
    monkeypatch.setattr(manager, "_ensure_required_components", lambda _services: None)

    import snowluma_manager

    def reject(_root: Path, *, require_free_ports: bool = True) -> dict[str, object]:
        assert require_free_ports is True
        raise snowluma_manager.SnowLumaError("SnowLuma OneBot :3001 已被占用")

    monkeypatch.setattr(snowluma_manager.SnowLumaManager, "validate_launch_boundary", reject)
    with pytest.raises(RuntimeError, match="3001"):
        manager._validate_group_start("qq_adapter")


def test_env_switch_is_rejected_before_backup_when_managed_qq_is_running(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "NachoBot" / ".env"
    env_path.parent.mkdir()
    env_path.write_text("qq_adapter=napcat\n", encoding="utf-8")
    manager = process_manager.ProcessManager(tmp_path)
    manager.states["napcat_adapter"] = process_manager.ServiceState(
        status=process_manager.ServiceStatus.RUNNING
    )
    config = config_manager.ConfigManager(tmp_path)
    with pytest.raises(ValueError, match="QQ 适配器"):
        config.write_config_raw("env", "qq_adapter=snowluma\n")
    assert env_path.read_text(encoding="utf-8") == "qq_adapter=napcat\n"
    assert not list(env_path.parent.glob(".auto.*.bak"))


def test_setup_sources_expose_selector_and_remove_stale_http_claims() -> None:
    html = (WEBUI_DIR / "static" / "index.html").read_text(encoding="utf-8")
    js = (WEBUI_DIR / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert 'id="setup-qq-adapter"' in html
    assert "SnowLuma" in html
    assert "wizardData.env.qq_adapter" in js
    assert "日记HTTP" not in js
    assert "B站视频HTTP" not in js
