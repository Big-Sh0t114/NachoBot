from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tomlkit

import webUI.config_manager as config_manager
import webUI.process_manager as process_manager
import webUI.setup_checks as setup_checks
import webUI.setup_deployment as setup_deployment


V2_TEMPLATE = '''\
config_version = 2

[discord]
token = "YOUR_DISCORD_BOT_TOKEN"
app_id = ""
proxy_enabled = false
proxy_url = ""

[nachobot]
host = "127.0.0.1"
port = 8000
'''


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _migration_result(*, success: bool = True, migrated: bool = True):
    return SimpleNamespace(
        success=success,
        migrated=migrated,
        message="migration result",
        error=None if success else "sanitized migration failure",
    )


def _patch_discord_template(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", root)
    monkeypatch.setattr(
        setup_deployment,
        "TEMPLATE_MAP",
        {setup_deployment.DISCORD_TEMPLATE: setup_deployment.DISCORD_TARGET},
    )
    monkeypatch.setattr(
        setup_deployment,
        "BACKUP_DIR",
        root / "config-save" / "setup_backups",
    )
    _write(root, setup_deployment.DISCORD_TEMPLATE, V2_TEMPLATE)
    validator_source = (
        Path(__file__).resolve().parents[2]
        / "NachoBot-Discord-Adapter"
        / "config_validation.py"
    )
    validator_fixture = root / "NachoBot-Discord-Adapter" / "config_validation.py"
    validator_fixture.parent.mkdir(parents=True, exist_ok=True)
    if not validator_fixture.exists():
        shutil.copyfile(validator_source, validator_fixture)


def test_registry_template_and_install_plan_have_one_discord_target(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    monkeypatch.setattr(config_manager, "ROOT_DIR", tmp_path)
    process_manager._register_services(tmp_path)

    assert process_manager.GROUP_DEFS["discord"].services == ["discord_adapter"]
    assert "koishi" not in process_manager.SERVICE_DEFS
    assert "koishi_adapter" not in process_manager.SERVICE_DEFS
    assert "discordvc" not in process_manager.SERVICE_DEFS
    discord_service = process_manager.SERVICE_DEFS["discord_adapter"]
    assert discord_service.cwd == "NachoBot-Discord-Adapter"
    assert discord_service.cmd == ["uv", "run", "python", "main.py"]

    discord_configs = [
        item for item in config_manager.CONFIG_REGISTRY if item["group"] == "Discord 适配器"
    ]
    assert [item["path"] for item in discord_configs] == [setup_deployment.DISCORD_TARGET]
    assert setup_checks.TEMPLATE_MAP[setup_deployment.DISCORD_TEMPLATE] == (
        setup_deployment.DISCORD_TARGET
    )
    assert not any("Koishi" in target for target in setup_checks.TEMPLATE_MAP.values())

    _write(tmp_path, "NachoBot/.env", "qq_adapter=napcat\n")
    tasks = setup_deployment.DependencyInstaller.get_install_tasks(["discord"])
    discord_tasks = [task for task in tasks if "discord" in task["id"].casefold()]
    assert discord_tasks == [
        {
            "id": "discord_adapter",
            "type": "uv",
            "name": "Discord Adapter",
            "dir": "NachoBot-Discord-Adapter",
        }
    ]
    assert setup_deployment.DependencyInstaller._resolve_task_project(discord_tasks[0]) == (
        tmp_path / "NachoBot-Discord-Adapter"
    )


def test_fresh_discord_config_uses_v2_placeholder_template(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    monkeypatch.setattr(
        setup_deployment.ConfigInitializer,
        "_discord_migration_api",
        mock.Mock(side_effect=AssertionError("fresh setup should not migrate")),
    )

    authoritative, errors = setup_deployment.ConfigInitializer._prepare_discord_config()

    assert (authoritative, errors) == (False, [])
    assert not (tmp_path / setup_deployment.DISCORD_TARGET).exists()
    template = tomlkit.parse((tmp_path / setup_deployment.DISCORD_TEMPLATE).read_text("utf-8"))
    assert template["discord"]["token"] == setup_deployment.DISCORD_PLACEHOLDER


def test_existing_v2_discord_config_is_authoritative_and_not_rewritten(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    live = V2_TEMPLATE.replace('token = "YOUR_DISCORD_BOT_TOKEN"', 'token = "live-secret"')
    live = live.replace('host = "127.0.0.1"', 'host = "discord-core.internal"')
    target = _write(tmp_path, setup_deployment.DISCORD_TARGET, live)
    before = target.read_bytes()
    needs_migration = mock.Mock(return_value=False)
    migration = mock.Mock(side_effect=AssertionError("v2 config must not migrate"))
    monkeypatch.setattr(
        setup_deployment.ConfigInitializer,
        "_discord_migration_api",
        mock.Mock(return_value=(needs_migration, migration)),
    )

    authoritative, errors = setup_deployment.ConfigInitializer._prepare_discord_config()

    assert (authoritative, errors) == (True, [])
    needs_migration.assert_called_once_with(target)
    migration.assert_not_called()
    assert target.read_bytes() == before


def test_setup_migrates_v1_in_place_using_adapter_owned_helper(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    target = _write(
        tmp_path,
        setup_deployment.DISCORD_TARGET,
        '[discord]\ntoken = "legacy-secret"\n',
    )
    needs_migration = mock.Mock(return_value=True)
    received: dict[str, Path] = {}

    def migrate(**kwargs):
        received.update(kwargs)
        target.write_text(V2_TEMPLATE.replace(
            'token = "YOUR_DISCORD_BOT_TOKEN"', 'token = "migrated-secret"'
        ), encoding="utf-8")
        return _migration_result()

    monkeypatch.setattr(
        setup_deployment.ConfigInitializer,
        "_discord_migration_api",
        mock.Mock(return_value=(needs_migration, migrate)),
    )

    authoritative, errors = setup_deployment.ConfigInitializer._prepare_discord_config()

    assert (authoritative, errors) == (True, [])
    assert received == {
        "target_config_path": target,
        "legacy_discordvc_config_path": target,
        "legacy_koishi_adapter_config_path": tmp_path / setup_deployment.LEGACY_KOISHI_ADAPTER_CONFIG,
        "koishi_config_path": tmp_path / setup_deployment.LEGACY_KOISHI_CONFIG,
        "identity_path": tmp_path / setup_deployment.DISCORD_IDENTITY_MAP,
        "koishi_db_path": tmp_path / setup_deployment.LEGACY_KOISHI_DATABASE,
    }


def test_pending_legacy_sources_migrate_before_any_new_template_is_written(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    legacy = _write(tmp_path, setup_deployment.LEGACY_KOISHI_CONFIG, "legacy data")
    target = tmp_path / setup_deployment.DISCORD_TARGET
    received: dict[str, Path] = {}

    def migrate(**kwargs):
        received.update(kwargs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(V2_TEMPLATE, encoding="utf-8")
        return _migration_result()

    monkeypatch.setattr(
        setup_deployment.ConfigInitializer,
        "_discord_migration_api",
        mock.Mock(return_value=(mock.Mock(), migrate)),
    )

    authoritative, errors = setup_deployment.ConfigInitializer._prepare_discord_config()

    assert (authoritative, errors) == (True, [])
    assert received["legacy_discordvc_config_path"] == (
        tmp_path / setup_deployment.LEGACY_DISCORDVC_CONFIG
    )
    assert received["koishi_config_path"] == legacy
    assert target.is_file()


def test_malformed_live_config_fails_closed_without_echoing_secrets(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    secret = "never-return-this-discord-secret"
    target = _write(
        tmp_path,
        setup_deployment.DISCORD_TARGET,
        f'config_version = 2\n[discord]\ntoken = "{secret}"\n',
    )
    monkeypatch.setattr(
        setup_deployment.ConfigInitializer,
        "_discord_migration_api",
        mock.Mock(side_effect=ValueError("invalid config")),
    )
    result = setup_deployment.ConfigInitializer.generate_configs(
        {"components": ["discord"], "discord": {"token": "request-token"}}
    )
    assert result["errors"]
    assert secret not in str(result)
    assert target.read_text(encoding="utf-8").count(secret) == 1


def test_discord_uv_install_requires_locked_sync(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    captured: list[object] = []

    class _Stdout:
        async def readline(self):
            return b""

    class _Process:
        stdout = _Stdout()
        returncode = 0

        async def wait(self):
            return 0

    async def fake_create(*command, **kwargs):
        captured.extend(command)
        captured.append(kwargs["cwd"])
        return _Process()

    async def run() -> None:
        with mock.patch.object(
            setup_deployment.asyncio,
            "create_subprocess_exec",
            new=fake_create,
        ):
            result = await setup_deployment.DependencyInstaller._run_uv_sync(
                tmp_path / "NachoBot-Discord-Adapter", None, locked=True
            )
        assert result["status"] == "ok"

    asyncio.run(run())
    assert captured[:3] == ["uv", "sync", "--locked"]
    assert captured[3] == str(tmp_path / "NachoBot-Discord-Adapter")


def test_discord_setup_defaults_never_return_token(tmp_path: Path, monkeypatch) -> None:
    _patch_discord_template(monkeypatch, tmp_path)
    live_secret = "live-discord-secret"
    _write(
        tmp_path,
        setup_deployment.DISCORD_TARGET,
        V2_TEMPLATE.replace('token = "YOUR_DISCORD_BOT_TOKEN"', f'token = "{live_secret}"'),
    )
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)

    defaults = setup_deployment.ConfigInitializer.get_defaults()

    assert defaults["discord"]["token"] == ""
    assert live_secret not in str(defaults)


def test_discord_group_start_requires_a_ready_core(tmp_path: Path) -> None:
    process_manager._register_services(tmp_path)
    manager = process_manager.ProcessManager(tmp_path)

    async def scenario() -> None:
        manager._assert_legacy_discord_stopped = mock.AsyncMock()  # type: ignore[method-assign]
        manager.refresh_adapter_observation_for_mutation = mock.AsyncMock(  # type: ignore[method-assign]
            return_value={
                "discord_adapter": process_manager.AdapterObservation(
                    "discord_adapter", "absent", 0.0
                )
            }
        )
        manager.refresh_core_observation = mock.AsyncMock(  # type: ignore[method-assign]
            return_value=process_manager.CoreObservation("unreachable", "potato", 0.0)
        )
        with mock.patch.object(manager, "_validate_group_start") as validate:
            try:
                await manager.prepare_start_group("discord")
            except RuntimeError:
                pass
            else:
                raise AssertionError("Discord start should require a ready Core")
            validate.assert_not_called()

    asyncio.run(scenario())
