from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

import pytest

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
V3_TEMPLATE = V2_TEMPLATE.replace("config_version = 2", "config_version = 3")


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _setup_root(tmp_path: Path, monkeypatch, *, template: str = V2_TEMPLATE) -> None:
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        setup_deployment,
        "TEMPLATE_MAP",
        {setup_deployment.DISCORD_TEMPLATE: setup_deployment.DISCORD_TARGET},
    )
    monkeypatch.setattr(
        setup_deployment,
        "BACKUP_DIR",
        tmp_path / "config-save" / "setup_backups",
    )
    _write(tmp_path, setup_deployment.DISCORD_TEMPLATE, template)
    validator_source = (
        Path(__file__).resolve().parents[2]
        / "NachoBot-Discord-Adapter"
        / "config_validation.py"
    )
    validator_fixture = tmp_path / "NachoBot-Discord-Adapter" / "config_validation.py"
    validator_fixture.parent.mkdir(parents=True, exist_ok=True)
    if not validator_fixture.exists():
        shutil.copyfile(validator_source, validator_fixture)

    monkeypatch.setattr(
        setup_deployment.ConfigInitializer,
        "_discord_migration_api",
        mock.Mock(return_value=(mock.Mock(return_value=False), mock.Mock())),
    )


@pytest.mark.parametrize(
    "invalid_config",
    [
        V2_TEMPLATE.replace("config_version = 2", "config_version = 4"),
        V2_TEMPLATE.replace("config_version = 2", "config_version = true"),
        V2_TEMPLATE + '[voice]\nenabled = "true"\n',
    ],
)
def test_invalid_live_discord_schema_is_rejected_before_any_writes(
    tmp_path: Path,
    monkeypatch,
    invalid_config: str,
) -> None:
    _setup_root(tmp_path, monkeypatch)
    target = _write(
        tmp_path,
        setup_deployment.DISCORD_TARGET,
        invalid_config.replace(
            'token = "YOUR_DISCORD_BOT_TOKEN"',
            'token = "never-return-this-test-secret"',
        ),
    )
    target_before = target.read_bytes()
    unrelated = _write(tmp_path, "NachoBot/config.toml", "sentinel = true\n")
    unrelated_before = unrelated.read_bytes()

    result = setup_deployment.ConfigInitializer.generate_configs(
        {"components": ["discord"], "discord": {"token": "replacement-token"}}
    )

    assert result["errors"]
    assert "never-return-this-test-secret" not in str(result)
    assert target.read_bytes() == target_before
    assert unrelated.read_bytes() == unrelated_before
    assert not (tmp_path / "config-save" / "setup_backups").exists()


@pytest.mark.parametrize("template", [V2_TEMPLATE, V3_TEMPLATE])
@pytest.mark.parametrize("initial_token", ["", setup_deployment.DISCORD_PLACEHOLDER])
def test_valid_live_schema_allows_setup_token_replacement(
    tmp_path: Path,
    monkeypatch,
    template: str,
    initial_token: str,
) -> None:
    _setup_root(tmp_path, monkeypatch, template=template)
    target = _write(
        tmp_path,
        setup_deployment.DISCORD_TARGET,
        template.replace(
            'token = "YOUR_DISCORD_BOT_TOKEN"',
            f'token = "{initial_token}"',
        ),
    )

    result = setup_deployment.ConfigInitializer.generate_configs(
        {"components": ["discord"], "discord": {"token": "replacement-token"}}
    )

    assert result["errors"] == []
    assert 'token = "replacement-token"' in target.read_text(encoding="utf-8")
    assert 'host = "127.0.0.1"' in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("template", [V2_TEMPLATE, V3_TEMPLATE])
def test_fresh_discord_template_accepts_schema_v2_and_v3(
    tmp_path: Path,
    monkeypatch,
    template: str,
) -> None:
    _setup_root(tmp_path, monkeypatch, template=template)

    assert setup_deployment.ConfigInitializer._validate_discord_template() == []
